"""RM34/AC25: explicit configuration and one bounded cancellable catalog worker."""

import asyncio
import logging
import threading
from datetime import timedelta

import pytest

from helpers import T0, FakeClock
from launcher_helpers import write_config
from relaymind.assets import service as s
from relaymind.assets.settings import parse_asset_settings, parse_recommendation_settings
from relaymind.assets.sources import SourceFailure
from relaymind.launcher import LaunchError, build_app, load_config
from relaymind.store import RunStore
from relaymind.store.asset_catalog import AssetCatalogStore as facade
from relaymind.store.run_store import utc_iso
from test_asset_sources import imported, skill


def settings(path, *, kind="local_directory", refresh=None):
    return parse_asset_settings(
        {
            "sources": [
                {
                    "source_id": "example",
                    "kind": kind,
                    "path": str(path),
                    "adapter_contract": None,
                    "secret_ref": None,
                }
            ]
        },
        refresh or {},
        config_dir=path.parent,
    )


def at(clock, seconds):
    """Move a FakeClock to this many seconds after its start."""
    clock.advance(seconds - (clock.mono - 1000.0))


def test_config_paths_are_config_relative_and_settings_closed(tmp_path, monkeypatch):
    parse, rec = parse_asset_settings, parse_recommendation_settings
    monkeypatch.chdir(tmp_path.parent)
    value = {
        "sources": [
            {
                "source_id": "example",
                "kind": "local_directory",
                "path": "skills",
                "adapter_contract": None,
                "secret_ref": None,
            }
        ]
    }
    parsed = parse(value, {}, config_dir=tmp_path)
    assert parsed.sources[0].path == tmp_path / "skills"
    assert parsed.refresh.local_scan_seconds == 30
    assert parsed.refresh.remote_seconds == 3600
    assert parsed.refresh.backoff_initial_seconds == 60
    assert parsed.refresh.backoff_max_seconds == 3600
    with pytest.raises(ValueError):
        parse({"sources": value["sources"] * 2}, {}, config_dir=tmp_path)
    for key, val in (("local_scan_seconds", True), ("remote_seconds", 0), ("bogus", 1)):
        with pytest.raises(ValueError):
            parse(value, {key: val}, config_dir=tmp_path)
    with pytest.raises(ValueError):
        parse(
            value, {"backoff_initial_seconds": 100, "backoff_max_seconds": 60}, config_dir=tmp_path
        )
    for key, val in (("secret_ref", "env:SECRET"), ("extra", "x"), ("kind", "qualified_remote")):
        source = value["sources"][0] | {key: val}
        with pytest.raises(ValueError):
            parse({"sources": [source]}, {}, config_dir=tmp_path)
    assert not rec({}, source_count=0).enabled
    with pytest.raises(ValueError):
        rec({"enabled": True}, source_count=1)
    with pytest.raises(ValueError):
        rec({"enabled": True, "user_id": "u", "workspace_id": "w"}, source_count=0)
    for override in (
        {"total_ms": True},
        {"primary_ms": 501},
        {"primary": "hybrid"},
        {"primary": "http", "name": "x", "version": "1", "url": "https://127.0.0.1"},
        {"primary": "http", "name": "x", "version": "1", "url": "http://u:p@localhost"},
        {"unexpected": 1},
    ):
        with pytest.raises(ValueError):
            rec(override, source_count=1)
    good = rec(
        {
            "primary": "http",
            "name": "model_router",
            "version": "1",
            "url": "http://localhost:9000/select",
        },
        source_count=1,
    )
    assert good.primary == "http"


def test_launcher_disabled_no_jobs_and_enabled_background_lifespan(tmp_path):
    # This is actual launcher wiring, with the existing deterministic model double.
    from helpers import ScriptedAdapter

    path = write_config(tmp_path)
    launched = build_app(load_config(path), local_adapter=ScriptedAdapter(turns=[]))
    assert getattr(launched.app.state, "asset_catalog", None) is None
    launched.orchestrator.store.close()
    skill(tmp_path / "ordinary")
    path.write_text(
        path.read_text() + '\n[recommendations]\nenabled=true\nuser_id="u"\nworkspace_id="w"\n'
        '\n[[assets.sources]]\nsource_id="example"\nkind="local_directory"\npath="ordinary"\n'
        # RM38: this source-only lifespan fixture performs no startup inference.
        "\n[placement]\nwarmup=false\ndetect=false\n"
    )
    launched = build_app(load_config(path), local_adapter=ScriptedAdapter(turns=[]))
    service = launched.app.state.asset_catalog
    assert service.store.snapshot().catalog_revision == 0

    async def run():
        async with launched.app.router.lifespan_context(launched.app):
            result = await asyncio.wait_for(service.rescan("example"), 2)
            assert result.revision == 1
            assert service.store.snapshot().assets[0].availability == "unknown"
            assert launched.orchestrator.store.active_run_id() is None
        assert not service.running

    asyncio.run(run())
    launched.orchestrator.store.close()


def test_static_launcher_rejects_unqualified_artifacts_and_keeps_model_requirement(tmp_path):
    path = write_config(tmp_path)
    for extra in (
        '\n[asset_adaptation]\nenabled=true\nconfig="x"\n',
        '\n[asset_hybrid]\nmanifest="x"\nconfig="x"\n',
        "\n[assets]\nunknown=[]\n",
    ):
        original = path.read_text()
        path.write_text(original + extra)
        with pytest.raises(LaunchError):
            load_config(path)
        path.write_text(original)
    path.write_text("[recommendations]\nenabled=false\n")
    with pytest.raises(LaunchError, match="configure at least one model"):
        load_config(path)


def test_timed_scan_busy_pause_explicit_rescan_and_no_execution_slot(tmp_path):
    skill(tmp_path / "source")
    clock, busy = FakeClock(), [True]
    with RunStore(clock=clock) as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(
            settings(tmp_path / "source"), catalog, clock=clock, busy=lambda: busy[0]
        )

        async def run():
            await service.tick()
            assert catalog.snapshot().catalog_revision == 0
            busy[0] = False
            await service.tick()
            assert catalog.snapshot().catalog_revision == 1
            skill(tmp_path / "source", description="Updated")
            at(clock, 29)
            await service.tick()
            assert catalog.snapshot().catalog_revision == 1
            at(clock, 30)
            await service.tick()
            assert catalog.snapshot().catalog_revision == 2
            assert run_store.active_run_id() is None
            result = await service.rescan("example")
            assert not result.changed
            await service.stop()
            assert all(j["duration_ms"] >= 0 for j in catalog.job_outcomes())

        asyncio.run(run())


def test_shutdown_drains_capture_and_never_commits_after_cancel(tmp_path, monkeypatch):
    skill(tmp_path / "source")
    entered, release = threading.Event(), threading.Event()
    real = s.capture_source

    def blocked(source, **kwargs):
        entered.set()
        release.wait(timeout=3)
        return real(source, **kwargs)

    monkeypatch.setattr(s, "capture_source", blocked)
    with RunStore() as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog)

        async def run():
            pending = asyncio.create_task(service.rescan("example"))
            assert await asyncio.to_thread(entered.wait, 1)
            stopping = asyncio.create_task(service.stop())
            await asyncio.sleep(0)
            release.set()
            await stopping
            with pytest.raises(SourceFailure) as caught:
                await pending
            assert str(caught.value) == "cancelled"
            assert catalog.snapshot().assets == ()
            assert catalog.job_outcomes()[0]["state"] == "cancelled"

        asyncio.run(run())


def test_failure_backoff_and_import_not_periodically_polled(tmp_path):
    manifest, _ = imported(tmp_path / "import")
    clock = FakeClock()
    with RunStore(clock=clock) as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(
            settings(manifest, kind="metadata_import"), catalog, clock=clock
        )

        async def run():
            await service.tick()
            assert catalog.snapshot().catalog_revision == 1
            manifest.write_text("bad")
            at(clock, 3600)
            await service.tick()
            assert len(catalog.job_outcomes()) == 1
            with pytest.raises(SourceFailure):
                await service.rescan("example")
            assert len(catalog.job_outcomes()) == 2
            clock.advance(59)
            await service.tick()
            assert len(catalog.job_outcomes()) == 2
            clock.advance(1)
            await service.tick()
            assert len(catalog.job_outcomes()) == 3
            clock.advance(119)
            await service.tick()
            assert len(catalog.job_outcomes()) == 3
            await service.stop()

        asyncio.run(run())


def test_cancelling_direct_tick_drains_thread_before_return(tmp_path, monkeypatch):
    skill(tmp_path / "source")
    entered, release = threading.Event(), threading.Event()
    real = s.capture_source

    def blocked(source, **kwargs):
        entered.set()
        release.wait(timeout=3)
        return real(source, **kwargs)

    monkeypatch.setattr(s, "capture_source", blocked)
    with RunStore() as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog)

        async def run():
            task = asyncio.create_task(service.tick())
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            release.set()
            # The thread is drained first; then the cancellation propagates.
            with pytest.raises(asyncio.CancelledError):
                await task
            await service.stop()
            assert catalog.snapshot().assets == ()
            assert catalog.job_outcomes()[0]["state"] == "cancelled"

        asyncio.run(run())


def test_interrupted_recovery_observes_initial_backoff(tmp_path):
    skill(tmp_path / "source")
    clock = FakeClock()
    with RunStore(clock=clock) as run_store:
        catalog = facade(run_store)
        catalog.begin_job("example", "0" * 64)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog, clock=clock)

        async def run():
            await service.tick()
            assert catalog.snapshot().assets == ()
            at(clock, 60)
            await service.tick()
            assert catalog.snapshot().assets
            await service.stop()

        asyncio.run(run())


def test_inflight_overlap_and_batch_duration_are_observed(tmp_path, monkeypatch):
    """RM38/AC29: measured pause/drain is separate from the unchanged job wall budget."""
    skill(tmp_path / "source")
    clock, busy = FakeClock(), [False]
    entered, contexts = threading.Event(), []
    actual = s.capture_source

    def overlap(source, **kwargs):
        busy[0] = True
        at(clock, 0.02)
        contexts.append(kwargs["context"])
        entered.set()
        kwargs["context"].check()
        at(clock, 0.04)
        return actual(source, **kwargs)

    monkeypatch.setattr(s, "capture_source", overlap)
    with RunStore(clock=clock) as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(
            settings(tmp_path / "source"), catalog, clock=clock, busy=lambda: busy[0]
        )

        async def run():
            task = asyncio.create_task(service.tick())
            assert await asyncio.to_thread(entered.wait, 1)
            while contexts[0].drain_ms is None:
                await asyncio.sleep(0)
            assert not task.done()
            at(clock, 0.03)
            busy[0] = False
            service.scheduler.wake()
            await task
            outcome = catalog.job_outcomes()[0]
            assert outcome["overlap_observed"]
            assert outcome["drain_ms"] == pytest.approx(0)
            assert outcome["batch_ms"] == pytest.approx([20, 30, 10])
            assert outcome["duration_ms"] == pytest.approx(40)
            await service.stop()

        asyncio.run(run())


@pytest.mark.parametrize("phase", ["preparation", "transaction"])
def test_one_job_deadline_covers_preparation_and_rollback_before_commit(
    tmp_path, monkeypatch, phase
):
    """RM34/AC25: publication never gets a fresh 60-second budget."""
    import relaymind.store.asset_catalog as storage

    skill(tmp_path / "source")
    clock = FakeClock()
    real_capture, real_project = s.capture_source, storage.project_asset

    def near_deadline(*args, **kwargs):
        got = real_capture(*args, **kwargs)
        at(clock, 59.9)
        return got

    def expensive_projection(*args, **kwargs):
        clock.advance(1)
        return real_project(*args, **kwargs)

    monkeypatch.setattr(s, "capture_source", near_deadline)
    if phase == "preparation":
        monkeypatch.setattr(storage, "project_asset", expensive_projection)
    with RunStore(clock=clock) as store:
        catalog = facade(store)
        if phase == "transaction":
            with store.sql_transaction() as connection:
                connection.create_function("advance_job_clock", 0, lambda: at(clock, 60.9))
                connection.execute(
                    "CREATE TRIGGER slow_publish AFTER INSERT ON asset_catalog_assets "
                    "BEGIN SELECT advance_job_clock(); END"
                )
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog, clock=clock)

        async def run():
            await service.tick()
            await service.stop()
            outcome = catalog.job_outcomes()[0]
            assert outcome["duration_ms"] == pytest.approx(60900)
            assert outcome["state"] == "failed"
            assert outcome["reason"] == "deadline"
            # Only the failure observation is visible; no asset was committed.
            assert catalog.snapshot().sources[0]["exclusions"] == [
                {"asset_id": None, "reason": "deadline"}
            ]
            assert catalog.snapshot().assets == ()

        asyncio.run(run())


def test_interrupted_started_retry_uses_new_failure_backoff(tmp_path):
    """RM34/AC25: recovery cannot reuse the elapsed schedule of the retry that crashed."""
    skill(tmp_path / "source")
    clock = FakeClock()
    with RunStore(clock=clock) as store:
        catalog = facade(store)
        failure = SourceFailure("source_unavailable")
        catalog.record_failure(
            "example", rejected_count=failure.rejected_count, exclusions=failure.exclusions
        )
        catalog.schedule_retry("example", utc_iso(T0 + timedelta(seconds=60)))
        at(clock, 60)
        catalog.begin_job("example", "0" * 64)
        at(clock, 61)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog, clock=clock)

        async def run():
            await service.tick()
            assert catalog.snapshot().assets == ()
            at(clock, 180)
            await service.tick()
            assert catalog.snapshot().assets == ()
            at(clock, 181)
            await service.tick()
            assert catalog.snapshot().assets
            await service.stop()

        asyncio.run(run())


def blocking_capture(monkeypatch):
    """Hold capture_source in its worker thread until the test releases it."""
    entered, release = threading.Event(), threading.Event()
    real = s.capture_source

    def blocked(source, **kwargs):
        entered.set()
        release.wait(timeout=3)
        return real(source, **kwargs)

    monkeypatch.setattr(s, "capture_source", blocked)
    return entered, release


def test_cancelling_the_worker_task_mid_scan_ends_it(tmp_path, monkeypatch):
    """RM34/AC25: a task cancellation propagates; the worker never swallows it."""
    skill(tmp_path / "source")
    entered, release = blocking_capture(monkeypatch)
    with RunStore() as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog)

        async def run():
            service.start()
            assert await asyncio.to_thread(entered.wait, 1)
            service._task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(service._task, 2)
            assert service._task.cancelled()
            assert catalog.job_outcomes()[0]["state"] == "cancelled"
            await asyncio.wait_for(service.stop(), 2)

        asyncio.run(run())


def test_one_cancelled_rescan_waiter_does_not_cancel_the_others(tmp_path, monkeypatch):
    """RM34/AC25: a shared rescan is cancelled only when no caller still waits."""
    skill(tmp_path / "source")
    entered, release = blocking_capture(monkeypatch)
    with RunStore() as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog)

        async def run():
            first = asyncio.create_task(service.rescan("example"))
            second = asyncio.create_task(service.rescan("example"))
            assert await asyncio.to_thread(entered.wait, 1)
            first.cancel()
            await asyncio.sleep(0.01)
            release.set()
            result = await asyncio.wait_for(second, 2)
            assert result.revision == 1
            assert first.cancelled()
            assert catalog.job_outcomes()[0]["state"] == "succeeded"
            await service.stop()

        asyncio.run(run())


def test_the_last_cancelled_rescan_waiter_cancels_the_job(tmp_path, monkeypatch):
    """RM34/AC25: with no caller left waiting, the explicit job is cancelled."""
    skill(tmp_path / "source")
    entered, release = blocking_capture(monkeypatch)
    with RunStore() as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog)

        async def run():
            waiter = asyncio.create_task(service.rescan("example"))
            assert await asyncio.to_thread(entered.wait, 1)
            waiter.cancel()
            await asyncio.sleep(0.01)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            await service.stop()
            assert catalog.job_outcomes()[0]["state"] == "cancelled"
            assert catalog.snapshot().assets == ()

        asyncio.run(run())


def test_explicit_rescan_runs_during_failure_backoff(tmp_path):
    """RM34/AC25: an operator rescan does not wait out the failure backoff."""
    root = tmp_path / "source"
    clock = FakeClock()
    with RunStore(clock=clock) as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(
            settings(root, refresh={"backoff_initial_seconds": 3600}), catalog, clock=clock
        )

        async def run():
            await service.tick()
            assert catalog.job_outcomes()[0]["state"] == "failed"
            skill(root)
            result = await asyncio.wait_for(service.rescan("example"), 2)
            assert result.changed
            assert len(catalog.job_outcomes()) == 2
            await service.stop()

        asyncio.run(run())


def test_lifespan_stops_the_probe_and_logs_when_the_worker_died(tmp_path, monkeypatch, caplog):
    """RM34/AC25: a dead worker is logged, and shutdown still stops the idle probe."""
    from helpers import ScriptedAdapter

    skill(tmp_path / "ordinary")
    path = write_config(tmp_path)
    path.write_text(
        path.read_text() + '\n[recommendations]\nenabled=true\nuser_id="u"\nworkspace_id="w"\n'
        '\n[[assets.sources]]\nsource_id="example"\nkind="local_directory"\npath="ordinary"\n'
    )
    launched = build_app(load_config(path), local_adapter=ScriptedAdapter(turns=[]))
    service = launched.app.state.asset_catalog

    async def broken():
        raise RuntimeError("injected worker failure")

    async def failing_stop():
        raise RuntimeError("injected stop failure")

    import relaymind.orchestrator.warmup as warmup_module

    stopped = []
    real_stop = warmup_module.IdleProbe.stop

    async def tracking_stop(probe):
        stopped.append(probe)
        await real_stop(probe)

    monkeypatch.setattr(service, "tick", broken)
    monkeypatch.setattr(warmup_module.IdleProbe, "stop", tracking_stop)

    async def run():
        with caplog.at_level(logging.ERROR, logger="relaymind.assets.service"):
            async with launched.app.router.lifespan_context(launched.app):
                await asyncio.sleep(0.05)
                assert not service.running
        assert "asset catalog worker stopped" in caplog.text
        assert len(stopped) == 1
        # Even a failing catalog stop never skips the idle probe's stop.
        monkeypatch.setattr(service, "stop", failing_stop)
        with pytest.raises(RuntimeError, match="injected stop failure"):
            async with launched.app.router.lifespan_context(launched.app):
                pass
        assert len(stopped) == 2

    asyncio.run(run())
    launched.orchestrator.store.close()


def test_repeated_cancel_still_drains_the_thread_before_return(tmp_path, monkeypatch):
    """RM34/AC25: a second cancel while draining never abandons the staging thread."""
    skill(tmp_path / "source")
    entered, release = threading.Event(), threading.Event()
    finished = threading.Event()
    real = s.capture_source

    def blocked(source, **kwargs):
        entered.set()
        release.wait(timeout=3)
        try:
            return real(source, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(s, "capture_source", blocked)
    with RunStore() as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog)

        async def run():
            task = asyncio.create_task(service.tick())
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set()
            await service.stop()

        asyncio.run(run())


def test_cancelled_stop_still_fails_pending_rescans(tmp_path, monkeypatch):
    """RM38/AC29 + RM34/AC25: cancelled stop drains before resolving pending rescans."""
    skill(tmp_path / "source")
    entered, release = threading.Event(), threading.Event()
    real = s.capture_source

    def blocked(source, **kwargs):
        entered.set()
        release.wait(timeout=3)
        return real(source, **kwargs)

    monkeypatch.setattr(s, "capture_source", blocked)
    with RunStore() as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog)

        async def run():
            first = asyncio.create_task(service.rescan("example"))
            assert await asyncio.to_thread(entered.wait, 1)
            # A waiter queued behind the running job is still pending at shutdown.
            pending = asyncio.get_running_loop().create_future()
            service._pending["other"] = pending
            stopping = asyncio.create_task(service.stop())
            await asyncio.sleep(0.01)
            stopping.cancel()
            await asyncio.sleep(0.01)
            stopping.cancel()
            await asyncio.sleep(0.01)
            try:
                assert not stopping.done()
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await stopping
            with pytest.raises(SourceFailure):
                await first
            assert pending.done() and isinstance(pending.exception(), SourceFailure)
            assert str(pending.exception()) == "cancelled"
            assert service._pending == {}
            assert service._task is not None and service._task.done()
            run_store.close()

        asyncio.run(run())


def test_cancelled_jobs_reject_nothing_and_use_spec_reasons(tmp_path, monkeypatch):
    """RM34/AC25: a cancelled job records shutdown, rejects no record (spec 8.1)."""
    skill(tmp_path / "source")
    entered, release = threading.Event(), threading.Event()
    real = s.capture_source

    def blocked(source, **kwargs):
        entered.set()
        release.wait(timeout=3)
        return real(source, **kwargs)

    monkeypatch.setattr(s, "capture_source", blocked)
    with RunStore() as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog)

        async def run():
            task = asyncio.create_task(service.tick())
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await service.stop()

        asyncio.run(run())
        outcome = catalog.job_outcomes()[0]
        assert (outcome["state"], outcome["reason"]) == ("cancelled", "shutdown")
        assert catalog.snapshot().sources[0]["rejected_count"] == 0


def test_operator_cancel_records_operator_reason(tmp_path, monkeypatch):
    """RM34/AC25: the last rescan waiter cancelling is an operator cancel (spec 8.1)."""
    skill(tmp_path / "source")
    entered, release = threading.Event(), threading.Event()
    real = s.capture_source

    def blocked(source, **kwargs):
        entered.set()
        release.wait(timeout=3)
        return real(source, **kwargs)

    monkeypatch.setattr(s, "capture_source", blocked)
    with RunStore() as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog)

        async def run():
            waiter = asyncio.create_task(service.rescan("example"))
            assert await asyncio.to_thread(entered.wait, 1)
            waiter.cancel()
            await asyncio.sleep(0.01)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            for _ in range(200):
                if catalog.job_outcomes()[0]["state"] != "running":
                    break
                await asyncio.sleep(0.01)
            await service.stop()

        asyncio.run(run())
        outcome = catalog.job_outcomes()[0]
        assert (outcome["state"], outcome["reason"]) == ("cancelled", "operator")
        assert catalog.snapshot().sources[0]["rejected_count"] == 0


def test_catalog_conflict_is_recorded_as_index_error(tmp_path, monkeypatch):
    """RM34/AC25: job reasons stay in the closed spec 8.1 enum."""
    from relaymind.store.asset_catalog import CatalogConflict

    skill(tmp_path / "source")
    with RunStore() as run_store:
        catalog = facade(run_store)
        service = s.AssetCatalogService(settings(tmp_path / "source"), catalog)

        def conflict(*args, **kwargs):
            raise CatalogConflict("revision moved")

        monkeypatch.setattr(catalog, "commit", conflict)

        async def run():
            await service.tick()
            await service.stop()

        asyncio.run(run())
        outcome = catalog.job_outcomes()[0]
        assert (outcome["state"], outcome["reason"]) == ("failed", "index_error")
        source = catalog.snapshot().sources[0]
        assert [e["reason"] for e in source["exclusions"]] == ["index_error"]
        assert source["rejected_count"] == 0


def test_periodic_sources_are_scheduled_oldest_due_first(tmp_path):
    """RM34/AC25: slow scans cannot starve a later source (31 s scans, 30 s interval)."""
    sources = [
        {
            "source_id": name,
            "kind": "local_directory",
            "path": str(tmp_path / name),
            "adapter_contract": None,
            "secret_ref": None,
        }
        for name in ("a", "b", "c")
    ]
    parsed = parse_asset_settings({"sources": sources}, {}, config_dir=tmp_path)
    clock = FakeClock()
    with RunStore(clock=clock) as run_store:
        service = s.AssetCatalogService(parsed, facade(run_store), clock=clock)
        now = clock.monotonic()
        for key in ("a", "b", "c"):
            service._next[key] = now
            service._retry[key] = 0
        order = []
        for _ in range(6):
            key = service._due(now)
            order.append(key)
            now += 31  # the scan takes longer than the 30-second interval
            service._next[key] = now + 30
        assert order == ["a", "b", "c", "a", "b", "c"]
