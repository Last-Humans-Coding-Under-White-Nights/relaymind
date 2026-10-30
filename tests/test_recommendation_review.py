"""RM34/RM36 AC25/AC27: lifecycle ownership, shutdown and budget evidence."""

import asyncio
import logging
from datetime import timedelta

import pytest

from recommendation_helpers import NOW, SCOPE, accepted, claim, setup
from test_chat_disconnect_cancel import until
from test_recommendation_races import Gate, raw_client


def test_cancelled_create_before_acceptance_is_not_an_empty_success(tmp_path):
    """RM36: a handler cancelled before any receipt propagates, never an empty 200."""

    async def go():
        app, service, runs, _ = setup(tmp_path)
        await service.refresh_catalog()
        entered, never = asyncio.Event(), asyncio.Event()

        async def blocked(*args):
            entered.set()
            await never.wait()

        service.create = blocked
        client = raw_client(app).start()
        await entered.wait()
        client.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client.task
        assert not any(m["type"] == "http.response.start" for m in client.sent)
        await service.stop()
        runs.close()

    asyncio.run(go())


class SlowCancel(Gate):
    """A selector whose cancellation cleanup outlasts the budget drain."""

    async def select(self, request, context):
        self.calls.append(request)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            for _ in range(5):
                await asyncio.sleep(0.05)
            self.ended.set()
            raise


def test_stop_waits_for_cancelled_create_tasks(tmp_path):
    """RM36: no create task runs after stop() returns and the store closes."""

    async def go():
        gate = SlowCancel()
        app, service, runs, _ = setup(tmp_path, primary=gate)
        await service.refresh_catalog()
        client = raw_client(app).start()
        await gate.started.wait()
        work = next(iter(service._active.values()))
        await service.stop()
        assert work.task.done()
        runs.close()
        await asyncio.wait({client.task}, timeout=5)

    asyncio.run(go())


def test_sweeper_survives_a_failed_sweep(tmp_path, caplog):
    """RM36: one failed retention sweep is logged and the sweeper keeps running."""

    async def go():
        app, service, runs, _ = setup(tmp_path)
        service.sweep_seconds = 0.01
        calls = []
        real = service.store.sweep

        def flaky(*, now):
            calls.append(now)
            if len(calls) == 1:
                raise RuntimeError("secret sweep failure")
            return real(now=now)

        with caplog.at_level(logging.ERROR, logger="relaymind.recommendations.service"):
            await service.start()
            # After start: start() recovers, and recovery sweeps once itself.
            service.store.sweep = flaky
            await until(lambda: len(calls) >= 3)
            assert not service._sweeper.done()
            await service.stop()
        assert "recommendation retention sweep failed: RuntimeError" in caplog.text
        assert "secret sweep failure" not in caplog.text
        runs.close()

    asyncio.run(go())


def test_service_restarts_in_a_second_lifespan(tmp_path):
    """RM36: stop() then start() serves a usable catalog again."""

    async def go():
        app, service, runs, _ = setup(tmp_path)
        await service.start()
        assert service.readiness()["state"] == "ready"
        await service.stop()
        await service.start()
        assert service.readiness()["state"] == "ready"
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_startup_failure_stops_what_already_started(tmp_path, monkeypatch):
    """RM36: a component failing to start never leaks the probe or the sweeper."""
    from helpers import ScriptedAdapter
    from launcher_helpers import write_config
    from relaymind.launcher import build_app, load_config
    from test_asset_sources import skill

    skill(tmp_path / "ordinary")
    path = write_config(tmp_path)
    path.write_text(
        path.read_text() + '\n[recommendations]\nenabled=true\nuser_id="u"\nworkspace_id="w"\n'
        '\n[[assets.sources]]\nsource_id="example"\nkind="local_directory"\npath="ordinary"\n'
    )
    launched = build_app(load_config(path), local_adapter=ScriptedAdapter(turns=[]))
    catalog = launched.app.state.asset_catalog
    recommendations = launched.app.state.recommendations

    import relaymind.orchestrator.warmup as warmup_module

    stopped = []
    real_stop = warmup_module.IdleProbe.stop

    async def tracking_stop(probe):
        stopped.append(probe)
        await real_stop(probe)

    def broken_start():
        raise RuntimeError("injected start failure")

    monkeypatch.setattr(warmup_module.IdleProbe, "stop", tracking_stop)
    monkeypatch.setattr(catalog, "start", broken_start)

    async def run():
        with pytest.raises(RuntimeError, match="injected start failure"):
            async with launched.app.router.lifespan_context(launched.app):
                pass
        assert len(stopped) == 1
        assert recommendations._sweeper is None

    asyncio.run(run())


def test_queued_job_records_the_enforced_acquisition_budgets(tmp_path):
    """RM03/RM34: recorded job budgets are the limits capture enforces."""
    from relaymind.assets import catalog as contract
    from relaymind.assets.sources import CaptureContext, SourceFailure
    from relaymind.store import RunStore
    from relaymind.store.asset_catalog import AssetCatalogStore

    budgets = contract.acquisition_budgets()
    with RunStore() as runs:
        store = AssetCatalogStore(runs)
        job = store.begin_job("example", "0" * 64)
        queued = store.job_events(job)[0]
        assert queued["event_type"] == "queued"
        assert queued["payload"]["budgets"] == budgets
    context = CaptureContext(captured_at=NOW)
    for _ in range(budgets["max_records"]):
        context.add_record()
    with pytest.raises(SourceFailure, match="record_limit"):
        context.add_record()
    context = CaptureContext(captured_at=NOW)
    context.add_bytes(budgets["max_received_bytes"])
    with pytest.raises(SourceFailure, match="size_limit"):
        context.add_bytes(1)


def test_deleting_a_recommendation_keeps_the_singleton_slot(tmp_path):
    """RM36: the slot pointer is cleared, never the singleton row itself."""
    from relaymind.store import RunStore
    from relaymind.store.recommendations import RecommendationStore

    with RunStore() as runs:
        store = RecommendationStore(runs)
        rid = store.begin(accepted(), claim()).view["recommendation_id"]
        with runs.sql_transaction() as db:
            db.execute("DELETE FROM recommendations WHERE recommendation_id=?", (rid,))
            rows = db.execute("SELECT singleton, recommendation_id FROM recommendation_slot")
            assert [tuple(row) for row in rows] == [(1, None)]
        assert store.begin(accepted(), claim("next")).view["state"] == "computing"


def test_reads_hide_retained_rows_without_purging_them(tmp_path):
    """RM36: reads apply retention logically; physical purge belongs to the sweeper.

    This replaces the earlier rule that a read purged: privacy holds because a
    row past retain_until is never returned, and the sweeper deletes it.
    """
    from relaymind.store import RunStore
    from relaymind.store.recommendations import RecommendationError, RecommendationStore

    with RunStore() as runs:
        store = RecommendationStore(runs)
        rid = store.begin(accepted(), claim()).view["recommendation_id"]
        later = NOW + timedelta(days=30)
        with pytest.raises(RecommendationError, match="recommendation_not_found"):
            store.get(SCOPE, rid, now=later)
        with pytest.raises(RecommendationError, match="recommendation_not_found"):
            store.events(SCOPE, rid, after_seq=0, limit=10, now=later)
        assert store.lookup_claim(claim(), now=later) is None
        with runs.sql_transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 1
        store.sweep(now=later)
        with runs.sql_transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 0


def test_lifecycle_uses_the_recorded_budgets(tmp_path):
    """RM03/RM36: the choice window, display limit and retention are the accepted budgets."""
    from relaymind.store import RunStore
    from relaymind.store.recommendations import RecommendationStore, recommendation_budgets

    budgets = recommendation_budgets() | dict(choice_seconds=60, retention_days=2)
    with RunStore() as runs:
        store = RecommendationStore(runs)
        view = store.begin(accepted(budgets=budgets), claim()).view
        assert view["retain_until"] == "2026-10-31T00:00:00.000000Z"
        rid = view["recommendation_id"]
        from recommendation_helpers import empty_result

        ready = store.finish(SCOPE, rid, 0, "ready", result=empty_result(), now=NOW)
        assert ready.view["choice_expires_at"] == "2026-10-29T00:01:00.000000Z"


def test_event_schema_references_still_check_action_and_error_code():
    """RM36/RM37: the event schema reuses the view's action and error code definitions."""
    import copy

    from relaymind.store.recommendation_event import validate_event

    rid = "00000000-0000-4000-8000-000000000001"
    action = dict(
        adapter_id="adapter",
        scope=SCOPE,
        recommendation_id=rid,
        task_id="a" * 64,
        choice={"kind": "cancel"},
        catalog_revision=1,
        host_snapshot_revision=None,
        recorded_at="2026-10-29T00:00:00.000000Z",
        state_version=2,
    )
    base = dict(
        schema_version=1,
        recommendation_id=rid,
        seq=3,
        timestamp="2026-10-29T00:00:00.000000Z",
        elapsed_ms=1,
        actor="adapter",
        erased=False,
    )
    cancelled = base | dict(
        event_type="cancelled",
        payload=dict(
            state_version=2,
            origin="direct_user",
            reason="user_cancel",
            action=action,
            active_ms=1,
            attempts=[],
        ),
    )
    assert validate_event(cancelled)["payload"]["action"]["choice"] == {"kind": "cancel"}
    broken = copy.deepcopy(cancelled)
    broken["payload"]["action"]["choice"] = {"kind": "approve"}
    with pytest.raises(ValueError):
        validate_event(broken)
    failed = base | dict(
        actor="gateway",
        event_type="failed",
        payload=dict(
            state_version=1, code="deadline_exceeded", reason="deadline", active_ms=1, attempts=[]
        ),
    )
    assert validate_event(failed)["payload"]["code"] == "deadline_exceeded"
    with pytest.raises(ValueError):
        validate_event(failed | dict(payload=failed["payload"] | {"code": "made_up"}))


def test_configured_source_missing_provenance_is_a_configuration_change(tmp_path):
    """RM34/RM36: a configured asset source without a provenance row is not 'corrupt'."""
    import dataclasses

    from relaymind.assets.settings import SourceConfig
    from relaymind.assets.sources import source_config_digest

    async def go():
        _, service, runs, catalog = setup(tmp_path)
        provider = service.provider
        extra = SourceConfig("other", "local_directory", tmp_path / "other")
        provider.settings = dataclasses.replace(
            provider.settings, sources=(*provider.settings.sources, extra)
        )
        snapshot, _ = catalog.snapshot_with_provenance()
        # Only the extra source has provenance; the snapshot's own source has none.
        rows = {
            "other": {
                "source_id": "other",
                "config_digest": source_config_digest(extra),
                "last_success_at": "2026-10-29T00:00:00.000000Z",
                "captured_at": "2026-10-29T00:00:00Z",
                "content_hash": "a" * 64,
            }
        }
        catalog.snapshot_with_provenance = lambda: (snapshot, rows)
        await provider.refresh()
        assert provider.snapshot is None
        assert provider.reason == "no_lexical_generation"
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_unexpected_selection_event_fails_loudly(tmp_path, monkeypatch):
    """RM36: an unknown observer event is a TypeError, never an UnboundLocalError."""
    from recommendation_helpers import BODY, client_for
    from relaymind.recommendations import service as service_module
    from relaymind.selection.asset_chain import RecommendationFailure

    seen = []

    async def fake_select(selection, *, observe, **kwargs):
        try:
            observe(object())
        except Exception as error:  # the observer's own failure is what we check
            seen.append(error)
        raise RecommendationFailure("internal_error", "internal_error")

    monkeypatch.setattr(service_module, "select_assets", fake_select)

    async def go():
        app, service, runs, _ = setup(tmp_path)
        await service.refresh_catalog()
        async with await client_for(app) as c:
            await c.post("/relay/v1/recommendations", json=BODY)
        await service.stop()
        runs.close()

    asyncio.run(go())
    assert len(seen) == 1
    assert type(seen[0]) is TypeError and str(seen[0]) == "unexpected selection event"


def test_disconnected_create_records_the_cancel_but_sends_nothing(tmp_path):
    """RM36 (TESTING.md): a disconnected create is cancelled for replay; no reply is forced."""
    from recommendation_helpers import BODY, client_for

    async def go():
        gate = Gate()
        app, service, runs, _ = setup(tmp_path, primary=gate)
        await service.refresh_catalog()
        original = raw_client(app).start()
        await gate.started.wait()
        await original.leave()
        assert not any(m["type"] == "http.response.start" for m in original.sent)
        async with await client_for(app) as c:
            replay = await c.post("/relay/v1/recommendations", json=BODY)
        assert replay.status_code == 200 and replay.json()["state"] == "cancelled"
        await service.stop()
        runs.close()

    asyncio.run(go())


def _terminal_attempts(service, rid):
    page = service.store.events(SCOPE, rid, after_seq=0, limit=100, now=service.clock.utc_now())
    terminal = page["events"][-1]
    assert terminal["event_type"] == "cancelled"
    return terminal["payload"]["attempts"]


@pytest.mark.parametrize("how", ["disconnect", "shutdown"])
def test_cancel_mid_attempt_keeps_the_in_flight_attempt(tmp_path, how):
    """RM36, spec 8.1: a cancel after dispatch keeps that attempt, without waiting for it."""
    from recommendation_helpers import BODY, client_for
    from relaymind.store.recommendation_event import validate_event

    async def go():
        gate = Gate(resist=True)
        app, service, runs, _ = setup(tmp_path, primary=gate)
        try:
            await check(gate, app, service)
        finally:
            # Never leave the resisting selector blocked, even when an assertion fails.
            gate.release.set()
            await service.stop()
            runs.close()

    async def check(gate, app, service):
        await service.refresh_catalog()
        original = raw_client(app).start()
        await gate.started.wait()
        rid = next(iter(service._active))
        if how == "disconnect":
            await original.leave()
        else:
            await service.cancel_computing(rid, reason="shutdown")
        attempts = _terminal_attempts(service, rid)
        assert len(attempts) == 1
        (attempt,) = attempts
        assert attempt["attempt"] == 1 and attempt["selector"] == {"name": "gate", "version": "1"}
        assert (attempt["outcome"], attempt["reason"]) == ("cancelled", "cancelled")
        assert attempt["observed_ms"] >= 0 and attempt["result"] is None
        assert attempt["cost_observations"] == []
        page = service.store.events(SCOPE, rid, after_seq=0, limit=100, now=service.clock.utc_now())
        for event in page["events"]:
            validate_event(event)
        async with await client_for(app) as c:
            replay = await c.post("/relay/v1/recommendations", json=BODY)
            stored = await c.get(f"/relay/v1/recommendations/{rid}")
        assert replay.status_code == 200 and replay.json()["state"] == "cancelled"
        assert replay.json() == stored.json()
        # The resisting selector finishing later adds nothing after the terminal event.
        gate.release.set()
        await gate.ended.wait()
        await asyncio.sleep(0)
        assert _terminal_attempts(service, rid) == attempts

    asyncio.run(go())
