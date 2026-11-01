"""RM36/AC27: deterministic task ownership, budget and commit fences."""

import asyncio
import json

import pytest

from recommendation_helpers import BODY, SCOPE, claim, client_for, setup
from relaymind.recommendations.service import CancellationSignal
from relaymind.store import TraceClock
from test_chat_disconnect_cancel import Client, response_of


class Gate:
    name, version = "gate", "1"

    def __init__(self, *, resist=False, fail=False):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.ended = asyncio.Event()
        self.calls = []
        self.resist = resist
        self.fail = fail

    async def select(self, request, context):
        self.calls.append(request)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            if not self.resist:
                self.ended.set()
                raise
            await self.release.wait()
        self.ended.set()
        return dict(
            schema_version=2,
            recommendation_id=request.data["recommendation_id"],
            catalog_revision=request.data["catalog_revision"],
            host_snapshot_revision=None,
            selector={"name": self.name, "version": self.version},
            mechanism="gate_rule",
            outcome="unavailable" if self.fail else "no_match",
            decision=None if self.fail else "no_match",
            candidates=[],
            index_revision=None,
            elapsed_ms=1,
            usage_claims=None,
        )


def raw_client(app, key="key"):
    c = Client(app, "POST", "/relay/v1/recommendations", json.dumps(BODY).encode())
    c._scope["headers"].append((b"idempotency-key", key.encode()))
    return c


def test_concurrent_keys_disconnect_and_resistant_late_result(tmp_path):
    async def go():
        from relaymind.assets.settings import RecommendationSettings

        gate = Gate(resist=True)
        # The default 150 ms primary budget can expire between the three HTTP
        # calls on a slow runner, which would fall back and free the slot.
        settings = RecommendationSettings(
            enabled=True, user_id="u", workspace_id="w", total_ms=60_000, primary_ms=60_000
        )
        app, service, runs, _ = setup(tmp_path, settings=settings, primary=gate)
        await service.refresh_catalog()
        original = raw_client(app).start()
        await gate.started.wait()
        async with await client_for(app) as c:
            duplicate = await c.post("/relay/v1/recommendations", json=BODY)
            assert (
                duplicate.status_code == 409
                and duplicate.json()["error"]["code"] == "recommendation_in_progress"
            )
            assert (
                await c.post("/relay/v1/recommendations", json=BODY | {"task_text": "changed"})
            ).status_code == 409
            assert (
                await c.post(
                    "/relay/v1/recommendations", json=BODY, headers={"Idempotency-Key": "second"}
                )
            ).status_code == 429
            service.provider.cache.invalidate()
            assert (await c.post("/relay/v1/recommendations", json=BODY)).status_code == 503
            await service.refresh_catalog()
            await original.leave()
            replay = await c.post("/relay/v1/recommendations", json=BODY)
            assert replay.status_code == 200 and replay.json()["state"] == "cancelled"
            rid = replay.json()["recommendation_id"]
            before = service.store.events(
                SCOPE, rid, after_seq=0, limit=100, now=service.clock.utc_now()
            )
            gate.release.set()
            await gate.ended.wait()
            await asyncio.sleep(0)
            after = service.store.events(
                SCOPE, rid, after_seq=0, limit=100, now=service.clock.utc_now()
            )
            assert before == after and len(gate.calls) == 1
            assert after["events"][-1]["payload"]["reason"] == "disconnect"
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_retry_disconnect_never_cancels_original_and_receive_failure_is_not_disconnect(tmp_path):
    async def go():
        gate = Gate()
        app, service, runs, _ = setup(tmp_path, primary=gate)
        await service.refresh_catalog()
        original = raw_client(app).start()
        await gate.started.wait()
        duplicate = raw_client(app).start()
        await duplicate.task
        assert response_of(duplicate)[0] == 409
        await duplicate.leave()
        assert next(iter(service._active.values())).signal.reason is None
        gate.release.set()
        await original.task
        assert response_of(original)[1]["state"] == "ready"
        # A transport receive failure cannot authorize cancellation.
        gate2 = Gate()
        service.primary = gate2
        client = raw_client(app, "next")
        old = client._receive

        async def broken():
            if not client._requested:
                return await old()
            raise OSError("receive unavailable")

        client._receive = broken
        client.start()
        await gate2.started.wait()
        await asyncio.sleep(0)
        assert next(iter(service._active.values())).signal.reason is None
        gate2.release.set()
        await client.task
        assert response_of(client)[1]["state"] == "ready"
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_revision_changes_no_match_and_fallback_keep_original_pin(tmp_path):
    async def go():
        gate = Gate()
        app, service, runs, catalog = setup(tmp_path, primary=gate)
        await service.refresh_catalog()
        original = raw_client(app).start()
        await gate.started.wait()
        pinned = gate.calls[0].data["catalog_revision"]
        catalog.record_failure("example", rejected_count=1, exclusions=())
        await service.refresh_catalog()
        assert catalog.revision() != pinned
        gate.release.set()
        await original.task
        status, body = response_of(original)
        assert status == 503 and body["error"]["recommendation_id"]
        rid = body["error"]["recommendation_id"]
        events = service.store.events(
            SCOPE, rid, after_seq=0, limit=100, now=service.clock.utc_now()
        )["events"]
        finished = [
            e["payload"]["attempt_record"] for e in events if e["event_type"] == "selector.finished"
        ]
        assert len(finished) == 2 and all(
            a["result"]["catalog_revision"] == pinned for a in finished
        )
        assert events[-1]["payload"]["attempts"] == finished
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_observation_failure_is_terminal_and_ready_commit_guard_rolls_back(tmp_path, monkeypatch):
    async def go():
        app, service, runs, catalog = setup(tmp_path)
        await service.refresh_catalog()
        original = service.store.append_attempt_event

        def fail(*args, **kwargs):
            raise RuntimeError("private error")

        monkeypatch.setattr(service.store, "append_attempt_event", fail)
        async with await client_for(app) as c:
            result = await c.post("/relay/v1/recommendations", json=BODY)
            assert result.status_code == 500 and "private error" not in result.text
            rid = result.json()["error"]["recommendation_id"]
            events = service.store.events(
                SCOPE, rid, after_seq=0, limit=100, now=service.clock.utc_now()
            )["events"]
            assert [e["event_type"] for e in events] == ["accepted", "failed"]
            monkeypatch.setattr(service.store, "append_attempt_event", original)
            finish = service.store.finish

            def change(*args, **kwargs):
                if args[3] == "ready":
                    catalog.record_failure("example", rejected_count=1, exclusions=())
                return finish(*args, **kwargs)

            monkeypatch.setattr(service.store, "finish", change)
            result = await c.post(
                "/relay/v1/recommendations", json=BODY, headers={"Idempotency-Key": "second"}
            )
            assert result.status_code == 503
            rid = result.json()["error"]["recommendation_id"]
            events = service.store.events(
                SCOPE, rid, after_seq=0, limit=100, now=service.clock.utc_now()
            )["events"]
            assert "ready" not in [e["event_type"] for e in events]
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_shutdown_releases_slot_and_stores_cancelled_receipt(tmp_path):
    async def go():
        gate = Gate(resist=True)
        app, service, runs, _ = setup(tmp_path, primary=gate)
        await service.refresh_catalog()
        client = raw_client(app).start()
        await gate.started.wait()
        await service.stop()
        await client.task
        assert response_of(client)[1]["state"] == "cancelled"
        gate.release.set()
        await gate.ended.wait()
        await asyncio.sleep(0)
        with runs.sql_transaction() as db:
            assert (
                db.execute("SELECT recommendation_id FROM recommendation_slot").fetchone()[0]
                is None
            )
        runs.close()

    asyncio.run(go())


def test_real_task_cancellation_persists_once_and_propagates(tmp_path):
    """RM36/AC27: an externally cancelled owner keeps cancellation semantics."""

    async def go():
        gate = Gate()
        app, service, runs, _ = setup(tmp_path, primary=gate)
        await service.refresh_catalog()
        request = BODY | dict(
            conversation=[],
            language_tags=[],
            code_switched=False,
            host_snapshot_revision=None,
        )
        task = asyncio.create_task(
            service.create(
                request, claim("real-cancel"), service.new_budget(), CancellationSignal()
            )
        )
        await gate.started.wait()
        rid = next(iter(service._active))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        view = service.store.get(SCOPE, rid, now=service.clock.utc_now()).view
        assert view["state"] == "cancelled"
        events = service.store.events(
            SCOPE, rid, after_seq=0, limit=100, now=service.clock.utc_now()
        )["events"]
        assert [event["event_type"] for event in events].count("cancelled") == 1
        with runs.sql_transaction() as db:
            assert (
                db.execute("SELECT recommendation_id FROM recommendation_slot").fetchone()[0]
                is None
            )
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_total_deadline_includes_body_processing_without_acceptance(tmp_path):
    """RM36/AC27: parsing spends the original absolute budget; no orphan at expiry."""
    from helpers import FakeClock

    async def go():
        clock = FakeClock()
        app, service, runs, _ = setup(tmp_path, clock=clock)
        await service.refresh_catalog()
        client = raw_client(app)
        receive = client._receive

        async def slow():
            message = await receive()
            if message["type"] == "http.request":
                clock.advance(1)
            return message

        client._receive = slow
        client.start()
        await client.task
        assert response_of(client)[0] == 504
        with runs.sql_transaction() as db:
            assert db.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 0
        runs.close()

    asyncio.run(go())


@pytest.mark.parametrize("partial_body", [False, True])
def test_stalled_body_receive_uses_original_deadline_without_acceptance(tmp_path, partial_body):
    """RM36/AC27: an authenticated stalled first/remaining ASGI chunk ends at 504."""
    from relaymind.assets.settings import RecommendationSettings

    async def go():
        settings = RecommendationSettings(
            enabled=True, user_id="u", workspace_id="w", total_ms=30, primary_ms=30
        )
        app, service, runs, _ = setup(tmp_path, settings=settings, clock=TraceClock())
        await service.refresh_catalog()
        client = raw_client(app)
        blocked, cancelled = asyncio.Event(), asyncio.Event()
        first = True

        async def stalled_receive():
            nonlocal first
            if partial_body and first:
                first = False
                return {"type": "http.request", "body": b'{"schema_version":1,', "more_body": True}
            try:
                await blocked.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("body gate was released before the deadline")

        client._receive = stalled_receive
        client.start()
        await asyncio.wait_for(client.task, timeout=1)
        status, body = response_of(client)
        assert status == 504 and body["error"]["code"] == "deadline_exceeded"
        assert not blocked.is_set() and cancelled.is_set()
        with runs.sql_transaction() as db:
            for table in ("recommendations", "recommendation_events", "recommendation_claims"):
                assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            assert (
                db.execute("SELECT recommendation_id FROM recommendation_slot").fetchone()[0]
                is None
            )
        await service.stop()
        runs.close()

    asyncio.run(go())


class MeasuredGate(Gate):
    # This test selector performs no inference; that zero is directly known.
    observed_cost = dict(
        input_tokens=None,
        output_tokens=None,
        cached_tokens=None,
        reasoning_tokens=None,
        calls=0,
        cpu_ms=None,
        peak_bytes=None,
    )


def test_total_timeout_persists_dispatched_primary_measurement_once(tmp_path):
    """RM36/AC27: the coordinator's timed attempt survives observer expiry."""
    from relaymind.assets.settings import RecommendationSettings

    async def go():
        gate = MeasuredGate()
        settings = RecommendationSettings(
            enabled=True, user_id="u", workspace_id="w", total_ms=30, primary_ms=30, fallback=False
        )
        app, service, runs, _ = setup(tmp_path, settings=settings, primary=gate, clock=TraceClock())
        await service.refresh_catalog()
        async with await client_for(app) as client:
            first = await client.post("/relay/v1/recommendations", json=BODY)
            assert first.status_code == 504
            rid = first.json()["error"]["recommendation_id"]
            events = service.store.events(
                SCOPE, rid, after_seq=0, limit=100, now=service.clock.utc_now()
            )["events"]
            attempts = events[-1]["payload"]["attempts"]
            assert [attempt["attempt"] for attempt in attempts] == [1]
            assert attempts[0]["outcome"] == "timeout" and attempts[0]["observed_ms"] > 0
            assert attempts[0]["cost_observations"] == [
                {"provenance": "observed", "value": MeasuredGate.observed_cost}
            ]
            replay = await client.post("/relay/v1/recommendations", json=BODY)
            assert replay.status_code == 504 and replay.content == first.content
            assert len(gate.calls) == 1
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_total_timeout_during_floor_preserves_both_ordered_attempts(tmp_path, monkeypatch):
    """RM36/AC27: an early timer wake still reaches the real total deadline."""
    from helpers import FakeClock
    from relaymind.assets.settings import RecommendationSettings
    from relaymind.recommendations import service as service_module
    from relaymind.selection.asset_chain import SelectionBudget

    async def go():
        clock = FakeClock()
        timer_calls = 0

        async def early_once(_seconds):
            nonlocal timer_calls
            timer_calls += 1
            if timer_calls == 1:
                # The primary completes and cancels its unused timer.
                await asyncio.Event().wait()
            elif timer_calls == 2:
                # Emulate a platform timer waking before monotonic deadline.
                await asyncio.sleep(0)
            else:
                clock.advance(0.08)
                await asyncio.sleep(0)

        primary = Gate(fail=True)
        primary.release.set()
        floor = MeasuredGate()
        floor.mechanism = "lexical_overlap"
        monkeypatch.setattr(service_module, "LexicalAssetSelector", lambda index: floor)
        settings = RecommendationSettings(
            enabled=True, user_id="u", workspace_id="w", total_ms=80, primary_ms=25
        )
        app, service, runs, _ = setup(tmp_path, settings=settings, primary=primary, clock=clock)
        service.new_budget = lambda: SelectionBudget(80, clock=clock, sleep=early_once)
        await service.refresh_catalog()
        async with await client_for(app) as client:
            first = await client.post("/relay/v1/recommendations", json=BODY)
            assert first.status_code == 504
            rid = first.json()["error"]["recommendation_id"]
            events = service.store.events(
                SCOPE, rid, after_seq=0, limit=100, now=service.clock.utc_now()
            )["events"]
            attempts = events[-1]["payload"]["attempts"]
            assert [attempt["attempt"] for attempt in attempts] == [1, 2]
            assert [attempt["outcome"] for attempt in attempts] == ["unavailable", "timeout"]
            assert attempts[1]["observed_ms"] > 0
            assert attempts[1]["cost_observations"] == [
                {"provenance": "observed", "value": MeasuredGate.observed_cost}
            ]
            assert [event["event_type"] for event in events].count("selector.finished") == 1
            replay = await client.post("/relay/v1/recommendations", json=BODY)
            assert replay.status_code == 504 and replay.content == first.content
            assert len(primary.calls) == len(floor.calls) == 1
        await service.stop()
        runs.close()

    asyncio.run(go())


def test_terminal_accepted_timeout_replays_without_recomputation(tmp_path):
    async def go():
        gate = Gate()
        from relaymind.assets.settings import RecommendationSettings

        settings = RecommendationSettings(
            enabled=True, user_id="u", workspace_id="w", total_ms=30, primary_ms=30, fallback=False
        )
        app, service, runs, _ = setup(tmp_path, settings=settings, primary=gate, clock=TraceClock())
        await service.refresh_catalog()
        async with await client_for(app) as c:
            first = await c.post("/relay/v1/recommendations", json=BODY)
            assert first.status_code == 504 and first.json()["error"]["recommendation_id"]
            replay = await c.post("/relay/v1/recommendations", json=BODY)
            assert replay.content == first.content and len(gate.calls) == 1
        await service.stop()
        runs.close()

    asyncio.run(go())
