"""RM38/AC29: one asset owner, cooperative CPU pauses and unchanged wall deadlines."""

import asyncio
import threading
from datetime import UTC, datetime

import pytest

from helpers import FakeClock, ScriptedAdapter
from recommendation_helpers import BODY, client_for
from relaymind import launcher
from relaymind.assets.sources import CaptureContext, SourceFailure
from relaymind.selection import asset_lexical
from test_asset_readiness import configured_catalog
from test_asset_sources import skill


@pytest.mark.parametrize("ending", ["idle", "cancelled", "deadline"])
def test_capture_checkpoint_waits_without_processing_and_keeps_original_deadline(ending):
    """RM38/AC29: no further record work while busy; cancel/deadline wake the same owner."""
    from relaymind.assets.scheduler import AssetWorkScheduler

    async def go():
        clock = FakeClock()
        busy, observed, cancelled, processed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        busy.set()

        def is_busy():
            observed.set()
            return busy.is_set()

        scheduler = AssetWorkScheduler(busy=is_busy)
        context = CaptureContext(
            datetime(2026, 10, 29, tzinfo=UTC),
            monotonic=clock.monotonic,
            observe_busy=is_busy,
            cancelled=cancelled.is_set,
            work_gate=scheduler.checkpoint,
        )
        original_deadline = context.deadline

        def work():
            context.check()
            processed.set()

        task = asyncio.create_task(asyncio.to_thread(work))
        assert await asyncio.to_thread(observed.wait, 1)
        await asyncio.sleep(0.01)
        assert not processed.is_set()
        if ending == "idle":
            busy.clear()
        elif ending == "cancelled":
            cancelled.set()
        else:
            clock.advance(60)
        scheduler.wake()
        if ending == "idle":
            await task
            assert processed.is_set()
        else:
            with pytest.raises(SourceFailure) as error:
                await task
            assert error.value.reason == ending
            if ending == "cancelled":
                assert error.value.rejected_count == 0
        assert context.deadline == original_deadline
        assert context.drain_ms is not None

    asyncio.run(go())


def test_source_and_derived_rebuild_share_one_worker_while_lexical_queries_continue(
    tmp_path, monkeypatch
):
    """RM38/AC29: a rebuild queues rescan work, never execution or foreground recommendations."""

    async def go():
        launched = launcher.build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        worker = launched.app.state.asset_catalog
        service = launched.app.state.recommendations
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")
            entered, release = asyncio.Event(), asyncio.Event()
            real = asset_lexical.build_lexical_index

            async def held(*args, **kwargs):
                entered.set()
                await release.wait()
                return await real(*args, **kwargs)

            monkeypatch.setattr(asset_lexical, "build_lexical_index", held)
            rebuild = asyncio.create_task(worker.rebuild())
            await asyncio.wait_for(entered.wait(), 1)
            running = worker._task
            skill(tmp_path / "ordinary", description="Next generation greeting")
            rescan = asyncio.create_task(worker.rescan("example"))
            await asyncio.sleep(0)
            assert worker.store.job_counts()["running"] == 1
            assert launched.orchestrator.store.active_run_id() is None
            assert launched.orchestrator.store.probe_reservation() is None
            async with await client_for(launched.app) as client:
                response = await client.post("/relay/v1/recommendations", json=BODY)
                assert response.status_code == 200, response.text
            from native_helpers import session, settled, start_run

            async with session(launched.app) as client:
                execution = await start_run(client, "Hello", execution_mode="local_only")
                assert execution.status_code == 202, execution.text
                # This empty model double may fail; admission must still own
                # its normal slot independently of the running lexical rebuild.
                await settled(client, execution.json()["run_id"])
            release.set()
            await rebuild
            await rescan
            assert worker._task is running
            assert service.readiness()["state"] == "ready"
            jobs = worker.store.job_outcomes()
            derived = next(j for j in jobs if j["source_id"] is None)
            events = worker.store.job_events(derived["job_id"])
            assert events[0]["payload"]["job_kind"] == "lexical"
            assert events[0]["payload"]["budgets"]["workers"] == 1
            assert events[0]["payload"]["budgets"]["cpu_batch_ms"] == 50
            assert events[-1]["event_type"] == "published"
            assert events[-1]["payload"]["index_revision"] is not None
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_cancelled_derived_rebuild_keeps_source_fresh_and_records_operator_cancel(
    tmp_path, monkeypatch
):
    """RM38/AC29: cancelling derived work never invents a source failure or rejection."""

    async def go():
        launched = launcher.build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        worker, service = launched.app.state.asset_catalog, launched.app.state.recommendations
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")
            before = worker.store.snapshot()
            entered, release = asyncio.Event(), asyncio.Event()
            real = asset_lexical.build_lexical_index

            async def held(*args, **kwargs):
                entered.set()
                await release.wait()
                return await real(*args, **kwargs)

            monkeypatch.setattr(asset_lexical, "build_lexical_index", held)
            rebuild = asyncio.create_task(worker.rebuild())
            await asyncio.wait_for(entered.wait(), 1)
            rebuild.cancel()
            with pytest.raises(asyncio.CancelledError):
                await rebuild
            release.set()
            from test_chat_disconnect_cancel import until

            await until(lambda: not worker.store.job_counts().get("running"))
            job = next(j for j in worker.store.job_outcomes() if j["source_id"] is None)
            assert (job["state"], job["reason"]) == ("cancelled", "operator")
            assert worker.store.snapshot() == before
            assert service.readiness()["state"] == "ready"
            events = worker.store.job_events(job["job_id"])
            assert events[-1]["event_type"] == "cancelled"
            assert events[-1]["payload"]["reason"] == "operator"
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_async_paused_rebuild_uses_original_wall_deadline(tmp_path, monkeypatch):
    """RM38/AC29: scheduler waiting cannot extend the fixed original 60-second job limit."""

    async def go():
        launched = launcher.build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        worker = launched.app.state.asset_catalog
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")
            before = worker.store.snapshot()
            clock, busy, entered = FakeClock(), False, asyncio.Event()
            worker.clock = clock
            worker.busy = lambda: busy
            real = asset_lexical.build_lexical_index

            async def overlap(*args, **kwargs):
                nonlocal busy
                busy = True
                entered.set()
                return await real(*args, **kwargs)

            monkeypatch.setattr(asset_lexical, "build_lexical_index", overlap)
            rebuild = asyncio.create_task(worker.rebuild())
            await asyncio.wait_for(entered.wait(), 1)
            clock.advance(60)
            with pytest.raises(SourceFailure) as error:
                await asyncio.wait_for(rebuild, 2)
            assert error.value.reason == "deadline"
            assert worker.store.snapshot() == before
            busy = False
            worker.scheduler.wake()
            job = next(j for j in worker.store.job_outcomes() if j["source_id"] is None)
            assert (job["state"], job["reason"]) == ("failed", "deadline")
            assert job["duration_ms"] == pytest.approx(60000)
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_failed_source_observer_defers_index_repair_until_inference_is_idle(tmp_path, monkeypatch):
    """RM38/AC29: failure notification cannot bypass the same background contention gate."""
    from relaymind.assets import service as service_module
    from test_chat_disconnect_cancel import until

    async def go():
        launched = launcher.build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        worker, service = launched.app.state.asset_catalog, launched.app.state.recommendations
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")
            busy, violations = False, []
            worker.busy = lambda: busy
            real_build = asset_lexical.build_lexical_index

            async def observed(*args, **kwargs):
                if busy:
                    violations.append("compiled while busy")
                return await real_build(*args, **kwargs)

            def failed_capture(*args, **kwargs):
                nonlocal busy
                busy = True
                raise SourceFailure("source_unavailable", rejected_count=0)

            monkeypatch.setattr(asset_lexical, "build_lexical_index", observed)
            monkeypatch.setattr(service_module, "capture_source", failed_capture)
            with pytest.raises(SourceFailure):
                await worker.rescan("example")
            try:
                assert not violations
                assert service.readiness()["state"] == "unavailable"
            finally:
                busy = False
                worker.scheduler.wake()
            await until(lambda: service.readiness()["state"] == "ready", timeout=2)
            assert worker.store.snapshot().sources[0]["stale"]
            assert any(j["source_id"] is None for j in worker.store.job_outcomes())
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_idle_source_failure_repairs_only_through_recorded_derived_job(tmp_path, monkeypatch):
    """RM38/AC29: even idle observer repairs use measured/cancellable background work."""
    from relaymind.assets import service as service_module
    from test_chat_disconnect_cancel import until

    async def go():
        launched = launcher.build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        worker, service = launched.app.state.asset_catalog, launched.app.state.recommendations
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")

            def failure(*args, **kwargs):
                raise SourceFailure("source_unavailable", rejected_count=0)

            monkeypatch.setattr(service_module, "capture_source", failure)
            with pytest.raises(SourceFailure):
                await worker.rescan("example")
            await until(lambda: service.readiness()["state"] == "ready", timeout=2)
            derived = [j for j in worker.store.job_outcomes() if j["source_id"] is None]
            assert len(derived) == 1 and derived[0]["state"] == "succeeded"
            assert worker.store.snapshot().sources[0]["stale"]
        launched.orchestrator.store.close()

    asyncio.run(go())


def test_async_index_batches_pause_without_blocking_lexical_requests(tmp_path, monkeypatch):
    """RM38/AC29: busy during index preparation yields the serving loop until idle."""

    async def go():
        launched = launcher.build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        worker = launched.app.state.asset_catalog
        service = launched.app.state.recommendations
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")
            busy, entered = False, asyncio.Event()
            worker.busy = lambda: busy
            real = asset_lexical.build_lexical_index

            async def overlap(*args, **kwargs):
                nonlocal busy
                busy = True
                entered.set()
                return await real(*args, **kwargs)

            monkeypatch.setattr(asset_lexical, "build_lexical_index", overlap)
            rebuild = asyncio.create_task(worker.rebuild())
            await asyncio.wait_for(entered.wait(), 1)
            try:
                await asyncio.sleep(0.02)
                assert not rebuild.done()
                async with await client_for(launched.app) as client:
                    response = await client.post("/relay/v1/recommendations", json=BODY)
                    assert response.status_code == 200, response.text
            finally:
                busy = False
                worker.scheduler.wake()
                # Only the first batch introduces contention in this fixture.
                monkeypatch.setattr(asset_lexical, "build_lexical_index", real)
                await asyncio.wait_for(rebuild, 2)
            assert service.readiness()["state"] == "ready"
            job = next(j for j in worker.store.job_outcomes() if j["source_id"] is None)
            assert job["overlap_observed"] and job["drain_ms"] is not None
            assert job["batch_ms"]
        launched.orchestrator.store.close()

    asyncio.run(go())


@pytest.mark.parametrize(
    "phase", ["scanner", "characters", "advancement", "composer", "constructor"]
)
@pytest.mark.parametrize("ending", ["idle", "cancelled", "deadline"])
def test_valid_large_yaml_pauses_inside_parser_work(tmp_path, monkeypatch, phase, ending):
    """RM38/AC29: one valid record's parser CPU yields without reducing published input limits."""
    from relaymind.assets import sources
    from relaymind.assets.scheduler import AssetWorkScheduler
    from test_asset_sources import capture

    extra = (
        "x" * 40000
        if phase in ("characters", "advancement")
        else "[" + ",".join(["x"] * 20000) + "]"
    )
    skill(tmp_path / "source", metadata=f"extra: {extra}\n")
    busy, observed, cancelled = threading.Event(), threading.Event(), threading.Event()
    clock = FakeClock()
    method = {
        "scanner": "fetch_more_tokens",
        "characters": "peek",
        "advancement": "forward",
        "composer": "compose_node",
        "constructor": "construct_object",
    }[phase]
    owner = sources.yaml.reader.Reader if phase == "advancement" else sources._DataLoader
    real = getattr(owner, method)
    calls, armed = 0, True

    def counted(self, *args, **kwargs):
        nonlocal calls, armed
        calls += (args[0] if args else 1) if phase == "advancement" else 1
        if armed and calls >= (5000 if phase == "advancement" else 100):
            armed = False
            busy.set()
        return real(self, *args, **kwargs)

    monkeypatch.setattr(owner, method, counted)

    def is_busy():
        if busy.is_set():
            observed.set()
        return busy.is_set()

    scheduler = AssetWorkScheduler(busy=is_busy)

    async def go():
        work = asyncio.create_task(
            asyncio.to_thread(
                capture,
                tmp_path / "source",
                monotonic=clock.monotonic,
                observe_busy=is_busy,
                cancelled=cancelled.is_set,
                work_gate=scheduler.checkpoint,
            )
        )
        try:
            assert await asyncio.to_thread(observed.wait, 3)
            # The parser must stop inside its current phase, not at the next file/record gate.
            assert calls <= (
                5256 if phase == "advancement" else 400 if phase == "characters" else 102
            )
        finally:
            if ending == "deadline":
                clock.advance(60)
            elif ending == "cancelled":
                cancelled.set()
            busy.clear()
            scheduler.wake()
            if ending == "idle":
                result = await work
                assert len(result.documents) == 1
            else:
                with pytest.raises(SourceFailure, match=ending):
                    await work

    asyncio.run(go())


def test_import_schema_validation_pauses_between_records(tmp_path, monkeypatch):
    """RM38/AC29: full-document validation cannot bypass the existing source CPU owner."""
    from relaymind.assets import sources
    from relaymind.assets.scheduler import AssetWorkScheduler
    from test_asset_sources import capture, imported

    path, data = imported(tmp_path / "import")
    import json

    template = data["records"][0]
    data["records"] = [
        dict(template, record=dict(template["record"], asset_id=f"skill:{i:064x}"))
        for i in range(100)
    ]
    path.write_text(json.dumps(data))
    busy, observed = threading.Event(), threading.Event()
    real = sources.StrictValidator.VALIDATORS["type"]
    calls = 0

    def counted(*args):
        nonlocal calls
        calls += 1
        if calls == 100:
            busy.set()
        yield from real(*args)

    monkeypatch.setitem(sources.StrictValidator.VALIDATORS, "type", counted)

    def is_busy():
        if busy.is_set():
            observed.set()
        return busy.is_set()

    scheduler = AssetWorkScheduler(busy=is_busy)

    async def go():
        work = asyncio.create_task(
            asyncio.to_thread(
                capture,
                path,
                "metadata_import",
                observe_busy=is_busy,
                work_gate=scheduler.checkpoint,
            )
        )
        try:
            assert await asyncio.to_thread(observed.wait, 2)
            assert calls < 150
        finally:
            busy.clear()
            scheduler.wake()
            result = await work
            assert len(result.documents) == 100

    asyncio.run(go())


def test_repeated_cancel_of_stop_drains_paused_derived_owner_before_store_close(
    tmp_path, monkeypatch
):
    """RM38/AC29: cancelled shutdown retains the sole derived worker through finalization."""
    from test_chat_disconnect_cancel import until

    async def go():
        launched = launcher.build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        worker = launched.app.state.asset_catalog
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")
            entered, release = asyncio.Event(), asyncio.Event()
            real = asset_lexical.build_lexical_index

            async def held(*args, **kwargs):
                entered.set()
                await release.wait()
                return await real(*args, **kwargs)

            monkeypatch.setattr(asset_lexical, "build_lexical_index", held)
            rebuild = asyncio.create_task(worker.rebuild())
            await entered.wait()
            worker.busy = lambda: True
            stopping = asyncio.create_task(worker.stop())
            second = asyncio.create_task(worker.stop())
            await asyncio.sleep(0)
            stopping.cancel()
            await asyncio.sleep(0)
            stopping.cancel()
            await asyncio.sleep(0)
            try:
                assert not stopping.done() and not second.done()
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await stopping
                await second
            with pytest.raises(SourceFailure, match="cancelled"):
                await rebuild
            assert worker._task.done()
            assert not worker.store.job_counts().get("running")
            await until(lambda: not worker.running)
            launched.orchestrator.store.close()

    asyncio.run(go())


def test_source_json_object_parser_checks_between_values(tmp_path, monkeypatch):
    """RM38/AC29: a valid large fallback manifest yields within its object decoder loop."""
    from relaymind.assets import sources
    from relaymind.assets.scheduler import AssetWorkScheduler
    from test_asset_sources import capture

    skill(tmp_path / "source")
    (tmp_path / "source" / "skill.manifest.json").write_text(
        "{" + ",".join(f'"unused{i}":1.5' for i in range(20000)) + "}"
    )
    busy, observed = threading.Event(), threading.Event()
    real = sources.json.JSONDecoder
    calls = 0

    class CountValues(real):
        def __init__(self, **kwargs):
            original = kwargs["parse_float"]

            def counted(value):
                nonlocal calls
                calls += 1
                if calls == 100:
                    busy.set()
                return original(value)

            super().__init__(**(kwargs | {"parse_float": counted}))

    monkeypatch.setattr(sources.json, "JSONDecoder", CountValues)

    def is_busy():
        if busy.is_set():
            observed.set()
        return busy.is_set()

    scheduler = AssetWorkScheduler(busy=is_busy)

    async def go():
        work = asyncio.create_task(
            asyncio.to_thread(
                capture, tmp_path / "source", observe_busy=is_busy, work_gate=scheduler.checkpoint
            )
        )
        try:
            assert await asyncio.to_thread(observed.wait, 2)
            assert calls <= 101
        finally:
            busy.clear()
            scheduler.wake()
            result = await work
            assert len(result.documents) == 1

    asyncio.run(go())


def test_cancelled_stop_drains_a_real_scheduler_paused_rebuild(tmp_path, monkeypatch):
    """RM38/AC29: the real busy gate drains through cancelled job finalization."""

    async def go():
        launched = launcher.build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        worker = launched.app.state.asset_catalog
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")
            before = worker.store.snapshot()
            busy, paused = False, asyncio.Event()
            worker.busy = lambda: busy
            real_build, real_drain = asset_lexical.build_lexical_index, worker.scheduler._drain

            async def overlap(*args, **kwargs):
                nonlocal busy
                busy = True
                return await real_build(*args, **kwargs)

            def drain(context):
                real_drain(context)
                paused.set()

            monkeypatch.setattr(asset_lexical, "build_lexical_index", overlap)
            monkeypatch.setattr(worker.scheduler, "_drain", drain)
            rebuild = asyncio.create_task(worker.rebuild())
            await asyncio.wait_for(paused.wait(), 1)
            stopping = asyncio.create_task(worker.stop())
            await asyncio.sleep(0)
            stopping.cancel()
            await asyncio.sleep(0)
            stopping.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stopping
            with pytest.raises(SourceFailure, match="cancelled"):
                await rebuild
            assert worker._task.done()
            assert worker.store.snapshot() == before
            job = next(j for j in worker.store.job_outcomes() if j["source_id"] is None)
            assert (job["state"], job["reason"]) == ("cancelled", "shutdown")
            assert worker.store.job_events(job["job_id"])[-1]["event_type"] == "cancelled"
            launched.orchestrator.store.close()

    asyncio.run(go())
