"""RM16, RM38/AC29: scheduler evidence and warm-up startup review regressions."""

import asyncio
import logging
import threading
from datetime import UTC, datetime

import pytest

from helpers import FakeClock
from relaymind import launcher
from relaymind.api.server import build_server
from relaymind.assets import scheduler as scheduling
from relaymind.assets.sources import CaptureContext, SourceFailure
from test_asset_readiness import WarmupBarrier, configured_catalog
from test_chat_disconnect_cancel import until


@pytest.mark.parametrize("gate", ["sync", "async"])
def test_foreground_start_during_second_busy_query_retains_actual_drain(monkeypatch, gate):
    """RM16, RM38/AC29: the admission after check_limits must retain its 70 ms drain."""
    from relaymind.store import RunStore

    async def go():
        clock = FakeClock()
        with RunStore(clock=clock) as store:
            run = store.create_run("accepted")
            calls, drained = 0, threading.Event()

            def busy():
                nonlocal calls
                calls += 1
                if calls == 2:
                    store.set_state(
                        run.run_id,
                        expected_version=1,
                        from_state="accepted",
                        to_state="routing",
                        holds_slot=True,
                    )
                    clock.advance(0.07)
                return store.active_run_id() is not None

            scheduler = scheduling.AssetWorkScheduler(busy=busy)
            context = CaptureContext(
                datetime.now(UTC),
                monotonic=clock.monotonic,
                observe_busy=busy,
                observe_busy_since=store.foreground_busy_since,
            )
            real_drain = scheduler._drain

            def observed_drain(context):
                real_drain(context)
                drained.set()

            monkeypatch.setattr(scheduler, "_drain", observed_drain)
            paused = asyncio.create_task(
                scheduler.async_checkpoint(context)
                if gate == "async"
                else asyncio.to_thread(scheduler.checkpoint, context)
            )
            try:
                assert await asyncio.to_thread(drained.wait, 1)
                assert context.drain_ms == pytest.approx(70)
                assert context.busy_observed_at is not None
            finally:
                store.set_state(
                    run.run_id, expected_version=2, from_state="routing", to_state="completed"
                )
                scheduler.wake()
                await paused

    asyncio.run(go())


@pytest.mark.parametrize("owner", ["execution", "resume", "probe", "warmup"])
@pytest.mark.parametrize("intervals", [(0.07,), (0.03, 0.04)])
def test_capture_retains_earliest_completed_foreground_interval(
    tmp_path, monkeypatch, owner, intervals
):
    """RM16, RM38/AC29: a completed interval still dates the first CPU drain."""
    from helpers import ScriptedAdapter
    from relaymind.assets import service as service_module

    async def go():
        config = configured_catalog(tmp_path, warmup=False, detect=False)
        launched = launcher.build_app(config, local_adapter=ScriptedAdapter())
        store, worker = launched.orchestrator.store, launched.app.state.asset_catalog
        clock = FakeClock()
        store.clock = worker.clock = clock
        real_capture = service_module.capture_source

        def capture_with_intervals(*args, context, **kwargs):
            context.check()
            for ordinal, duration in enumerate(intervals):
                if owner == "warmup":
                    warmup = launcher._ServingWarmup(config, launched)
                    clock.advance(duration)
                    asyncio.run(warmup.stop())
                elif owner == "probe":
                    token = f"p{ordinal}"
                    assert store.try_reserve_probe(token, "owner", model=token, after_seconds=0.001)
                    clock.advance(duration)
                    assert store.finish_probe(token, "owner")
                else:
                    run = store.create_run("accepted")
                    if owner == "resume":
                        run = store.set_state(
                            run.run_id,
                            expected_version=1,
                            from_state="accepted",
                            to_state="waiting",
                        )
                    run = store.set_state(
                        run.run_id,
                        expected_version=run.version,
                        from_state=run.state,
                        to_state="routing",
                        holds_slot=True,
                    )
                    clock.advance(duration)
                    store.set_state(
                        run.run_id,
                        expected_version=run.version,
                        from_state="routing",
                        to_state="completed",
                    )
                if ordinal + 1 < len(intervals):
                    clock.advance(0.01)
            context.check()
            return real_capture(*args, context=context, **kwargs)

        monkeypatch.setattr(service_module, "capture_source", capture_with_intervals)
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")
            job = worker.store.job_outcomes()[0]
            assert job["overlap_observed"]
            assert job["drain_ms"] == pytest.approx(70 if len(intervals) == 1 else 80)
        store.close()

    asyncio.run(go())


def test_native_io_interval_is_recorded_without_becoming_cpu_drain(tmp_path, monkeypatch, caplog):
    """RM16, RM38/AC29: actual native waiting stays visible outside the CPU window."""
    from helpers import ScriptedAdapter
    from relaymind.assets import sources

    caplog.set_level(logging.INFO, logger="relaymind.assets.service")

    async def go():
        launched = launcher.build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        store, worker = launched.orchestrator.store, launched.app.state.asset_catalog
        clock = FakeClock()
        store.clock = worker.clock = clock
        real_open = sources.open_source_tree
        contexts, injected = [], False

        class File:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def read(self, count):
                nonlocal injected
                if not injected:
                    injected = True
                    run = store.create_run("accepted")
                    run = store.set_state(
                        run.run_id,
                        expected_version=1,
                        from_state="accepted",
                        to_state="routing",
                        holds_slot=True,
                    )
                    clock.advance(0.07)
                    store.set_state(
                        run.run_id,
                        expected_version=run.version,
                        from_state="routing",
                        to_state="completed",
                    )
                return self.inner.read(count)

        class Tree:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def open_file(self, relative):
                return File(self.inner.open_file(relative))

        def open_tree(*args, control, **kwargs):
            contexts.append(control)
            return Tree(real_open(*args, control=control, **kwargs))

        monkeypatch.setattr(sources, "open_source_tree", open_tree)
        async with launched.app.router.lifespan_context(launched.app):
            await worker.rescan("example")
            context = contexts[0]
            assert max(context.native_io_ms) == pytest.approx(70)
            assert context.drain_ms is None
            assert not context.busy_observed_at
            assert all(batch["duration_ms"] < 1 for batch in context.work_batches)
            assert worker.store.job_outcomes()[0]["duration_ms"] == pytest.approx(70)
            records = [
                record for record in caplog.records if record.msg.startswith("asset native I/O")
            ]
            assert records and max(records[0].args[1]) == pytest.approx(70)
            assert not store._foreground_windows
        store.close()

    asyncio.run(go())


def test_cpu_window_excludes_preexisting_paused_and_rolled_back_owners():
    """RM17, RM38/AC29: only committed starts inside an admitted CPU window count."""
    from relaymind.store import RunStore

    clock = FakeClock()
    with RunStore(clock=clock) as store:
        window = store.foreground_window()
        try:
            run = store.create_run("accepted")
            run = store.set_state(
                run.run_id,
                expected_version=1,
                from_state="accepted",
                to_state="routing",
                holds_slot=True,
            )
            clock.advance(0.04)
            window.begin()
            assert window.end(store.foreground_busy_since())[:2] == (True, None)
            store.set_state(
                run.run_id, expected_version=run.version, from_state="routing", to_state="completed"
            )
            window.begin()
            run = store.create_run("accepted")
            with pytest.raises(ValueError, match="abort"):
                with store.transaction():
                    store.set_state(
                        run.run_id,
                        expected_version=1,
                        from_state="accepted",
                        to_state="routing",
                        holds_slot=True,
                    )
                    clock.advance(0.07)
                    raise ValueError("abort")
            assert window.end()[:2] == (True, None)
            run = store.set_state(
                run.run_id,
                expected_version=1,
                from_state="accepted",
                to_state="routing",
                holds_slot=True,
            )
            clock.advance(0.5)
            store.set_state(
                run.run_id, expected_version=run.version, from_state="routing", to_state="completed"
            )
            window.begin()
            assert window.end()[:2] == (True, None)
        finally:
            store.release_foreground_window(window)
        assert not store._foreground_windows


def test_failed_cpu_window_observer_does_not_relabel_committed_admission(monkeypatch):
    """RM17, RM38/AC29: evidence failure cannot discard a committed state receipt."""
    from relaymind.store import RunStore

    with RunStore() as store:
        window = store.foreground_window()
        window.begin()

        def broken(onset):
            raise RuntimeError("fixture observation failure")

        monkeypatch.setattr(window, "record", broken)
        run = store.create_run("accepted")
        acquired = store.set_state(
            run.run_id,
            expected_version=1,
            from_state="accepted",
            to_state="routing",
            holds_slot=True,
        )
        assert acquired.version == store.get_run(run.run_id).version == 2
        assert store.active_run_id() == run.run_id
        store.release_foreground_window(window)


def test_uncontended_cpu_fragments_aggregate_across_separately_measured_io():
    """RM16, RM38/AC29: native waits do not create a persisted CPU batch per I/O call."""
    from relaymind.store import RunStore

    clock = FakeClock()
    with RunStore(clock=clock) as store:
        window = store.foreground_window()
        scheduler = scheduling.AssetWorkScheduler(busy=lambda: False)
        context = CaptureContext(
            datetime.now(UTC),
            monotonic=clock.monotonic,
            foreground_window=window,
            finish_cpu=scheduler.end_batch,
            work_gate=scheduler.checkpoint,
        )
        context.check()
        for _ in range(20):
            clock.advance(0.001)
            context.io(lambda: clock.advance(0.005))
        context.end_cpu_window()
        scheduler.finish_batch(context, force=True)
        assert [batch["duration_ms"] for batch in context.work_batches] == pytest.approx([20])
        assert context.native_io_ms == pytest.approx([5] * 20)
        store.release_foreground_window(window)


@pytest.mark.parametrize("resource", ["tree", "file", "iterator"])
def test_cancel_after_native_open_closes_newly_owned_resource(tmp_path, monkeypatch, resource):
    """RM14, RM34/AC25, RM38/AC29: resume cancellation cannot strand a native handle."""
    from relaymind.assets import sources
    from test_asset_sources import skill

    skill(tmp_path / "source")
    (tmp_path / "source" / "extra").write_text("fixture")
    cancelled, opened = False, []
    context = CaptureContext(datetime.now(UTC), cancelled=lambda: cancelled)
    real_open = sources.open_source_tree

    def open_tree(*args, **kwargs):
        nonlocal cancelled
        tree = real_open(*args, **kwargs)
        if resource == "tree":
            opened.append(tree)
            cancelled = True
        elif resource == "file":
            real_file = tree.open_file

            def open_file(relative):
                nonlocal cancelled
                file = real_file(relative)
                opened.append(file)
                cancelled = True
                return file

            monkeypatch.setattr(tree, "open_file", open_file)
        else:
            real_names = tree.names

            def names():
                entries = real_names()
                opened.append(entries)

                class Entries:
                    def __next__(self):
                        nonlocal cancelled
                        name = next(entries)
                        cancelled = True
                        return name

                    def close(self):
                        entries.close()

                return Entries()

            monkeypatch.setattr(tree, "names", names)
        return tree

    monkeypatch.setattr(sources, "open_source_tree", open_tree)
    stage = None
    try:
        with pytest.raises(SourceFailure, match="cancelled") as failure:
            stage = sources._Stage(tmp_path / "source", context)
            if resource == "iterator":
                next(stage.names())
            else:
                stage.read("SKILL.md", 10000)
        assert failure.value.reason == "cancelled"
        with pytest.raises(StopIteration if resource == "iterator" else OSError):
            if resource == "tree":
                opened[0].info("SKILL.md")
            elif resource == "iterator":
                next(opened[0])
            else:
                opened[0].info()
    finally:
        for owned in opened:
            owned.close()
        if stage is not None:
            stage.close()


@pytest.mark.parametrize("ending", ["idle", "cancelled"])
def test_async_checkpoint_wakes_from_another_thread_without_poll_timeout(monkeypatch, ending):
    """RM38/AC29: wake signals async idle/cancellation without a polling delay."""
    monkeypatch.setattr(scheduling, "_WAIT_SECONDS", 60)

    async def go():
        busy, cancelled = True, False
        observed = asyncio.Event()

        def is_busy():
            observed.set()
            return busy

        scheduler = scheduling.AssetWorkScheduler(busy=is_busy)
        context = CaptureContext(
            datetime(2026, 10, 29, tzinfo=UTC),
            observe_busy=is_busy,
            cancelled=lambda: cancelled,
        )
        original_deadline = context.deadline
        task = asyncio.create_task(scheduler.async_checkpoint(context))
        await observed.wait()
        await asyncio.sleep(0)
        assert not task.done()
        if ending == "idle":
            busy = False
        else:
            cancelled = True
        await asyncio.to_thread(scheduler.wake)
        if ending == "cancelled":
            with pytest.raises(SourceFailure, match="cancelled"):
                await asyncio.wait_for(task, 0.2)
        else:
            await asyncio.wait_for(task, 0.2)
        assert context.deadline == original_deadline

    asyncio.run(go())


def test_busy_checkpoint_records_one_elapsed_batch_and_no_empty_tail():
    """RM16, RM38/AC29: draining a just-finished batch adds no zero-length work."""
    clock = FakeClock()
    checks = iter([True, True, False])
    scheduler = scheduling.AssetWorkScheduler(busy=lambda: next(checks))
    context = CaptureContext(
        datetime(2026, 10, 29, tzinfo=UTC),
        monotonic=clock.monotonic,
        observe_busy=scheduler.busy,
    )
    clock.advance(0.06)
    scheduler.checkpoint(context)
    scheduler.finish_batch(context, force=True)
    assert [batch["duration_ms"] for batch in context.work_batches] == pytest.approx([60])


def test_default_build_server_starts_owned_warmup_after_listener(tmp_path):
    """RM38/AC29: default serving eventually releases the inference startup fence."""

    async def go():
        adapter = WarmupBarrier()
        launched = launcher.build_app(configured_catalog(tmp_path), local_adapter=adapter)
        owner = launched.app.state.startup_warmup
        server = build_server(launched.app, port=0)
        serving = asyncio.create_task(server.serve())
        try:
            await until(lambda: server.started)
            await asyncio.wait_for(adapter.entered.wait(), 0.2)
            assert server.servers[0].sockets
            assert launched.orchestrator.local_startup_pending
            adapter.release.set()
            await until(lambda: owner.task.done())
            assert not launched.orchestrator.local_startup_pending
            assert launched.orchestrator.local_status.usable
        finally:
            adapter.release.set()
            server.should_exit = True
            await serving
            launched.orchestrator.store.close()

    asyncio.run(go())


@pytest.mark.parametrize("gate", ["sync", "async"])
@pytest.mark.parametrize("known", [False, True])
def test_drain_uses_foreground_onset_or_reports_unknown(gate, known):
    """RM16, RM38/AC29: 40 ms since admission is not zero at first observation."""
    clock = FakeClock()
    onset = clock.monotonic()
    calls = 0

    def busy():
        nonlocal calls
        calls += 1
        return calls <= 2

    scheduler = scheduling.AssetWorkScheduler(busy=busy)
    context = CaptureContext(
        datetime(2026, 10, 29, tzinfo=UTC),
        monotonic=clock.monotonic,
        observe_busy=busy,
        observe_busy_since=lambda: onset if known else None,
    )
    clock.advance(0.04)
    if gate == "async":
        asyncio.run(scheduler.async_checkpoint(context))
    else:
        scheduler.checkpoint(context)
    assert context.busy_observed_at is not None
    assert context.drain_ms == (pytest.approx(40) if known else None)


def test_execution_onset_survives_transitions_but_not_release_or_rollback(tmp_path):
    """RM17, RM38/AC29: committed slot acquisition owns its monotonic onset."""
    from relaymind.store import RunStore

    clock = FakeClock()
    with RunStore(tmp_path / "runs.sqlite", clock=clock) as store:
        run = store.create_run("accepted")
        clock.advance(0.01)
        run = store.set_state(
            run.run_id,
            expected_version=run.version,
            from_state=run.state,
            to_state="routing",
            holds_slot=True,
        )
        clock.advance(0.04)
        assert store.foreground_busy_since() == pytest.approx(1000.01)
        with RunStore(tmp_path / "runs.sqlite", clock=clock) as other:
            assert other.foreground_busy_since() is None
        run = store.set_state(
            run.run_id,
            expected_version=run.version,
            from_state=run.state,
            to_state="skill_selected",
            holds_slot=True,
        )
        assert store.foreground_busy_since() == pytest.approx(1000.01)
        with pytest.raises(RuntimeError, match="rollback"):
            with store.transaction():
                store.set_state(
                    run.run_id,
                    expected_version=run.version,
                    from_state=run.state,
                    to_state="waiting_confirmation",
                )
                raise RuntimeError("rollback")
        assert store.foreground_busy_since() == pytest.approx(1000.01)
        run = store.set_state(
            run.run_id,
            expected_version=run.version,
            from_state=run.state,
            to_state="waiting_confirmation",
        )
        assert store.foreground_busy_since() is None
        clock.advance(0.01)
        store.set_state(
            run.run_id,
            expected_version=run.version,
            from_state=run.state,
            to_state="routing",
            holds_slot=True,
        )
        assert store.foreground_busy_since() == pytest.approx(1000.06)


def test_probe_onset_is_recorded_only_for_committed_reservation():
    """RM17, RM38/AC29: rollback and release cannot leave a phantom busy onset."""
    from relaymind.store import RunStore

    clock = FakeClock()
    with RunStore(clock=clock) as store:
        with pytest.raises(RuntimeError, match="rollback"):
            with store.transaction():
                assert store.try_reserve_probe("p1", "owner", model="m", after_seconds=1)
                raise RuntimeError("rollback")
        assert store.foreground_busy_since() is None
        clock.advance(0.01)
        assert store.try_reserve_probe("p2", "owner", model="m", after_seconds=1)
        clock.advance(0.04)
        assert store.foreground_busy_since() == pytest.approx(1000.01)
        assert store.finish_probe("p2", "owner")
        assert store.foreground_busy_since() is None


def test_launcher_supplies_warmup_and_execution_onsets(tmp_path):
    """RM16, RM38/AC29: production wiring measures contention before checkpoints."""
    from helpers import ScriptedAdapter

    async def go():
        launched = launcher.build_app(configured_catalog(tmp_path), local_adapter=ScriptedAdapter())
        store = launched.orchestrator.store
        worker = launched.app.state.asset_catalog
        owner = launched.app.state.startup_warmup
        try:
            onset = worker.busy_since()
            assert onset is not None
            assert onset <= store.clock.monotonic()
            await owner.stop()
            assert worker.busy_since() is None
            clock = FakeClock()
            store.clock = clock
            run = store.create_run("accepted")
            run = store.set_state(
                run.run_id,
                expected_version=run.version,
                from_state=run.state,
                to_state="routing",
                holds_slot=True,
            )
            clock.advance(0.04)
            assert worker.busy_since() == 1000.0
            store.set_state(
                run.run_id,
                expected_version=run.version,
                from_state=run.state,
                to_state="completed",
            )
            owner.prepare()
            assert worker.busy_since() == 1000.04
        finally:
            await owner.stop()
            store.close()

    asyncio.run(go())


@pytest.mark.parametrize("gate", ["sync", "async"])
@pytest.mark.parametrize("arrival", ["before_begin", "after_begin"])
def test_cpu_admission_rechecks_owner_after_observation_activation(monkeypatch, gate, arrival):
    """RM17, RM38/AC29: a raced local owner pauses CPU before the gate returns."""
    from relaymind.store import RunStore

    async def go():
        clock = FakeClock()
        with RunStore(clock=clock) as store:
            scheduler = scheduling.AssetWorkScheduler(
                busy=lambda: store.active_run_id() is not None
            )
            window = store.foreground_window(monotonic=clock.monotonic)
            context = CaptureContext(
                datetime.now(UTC),
                monotonic=clock.monotonic,
                foreground_window=window,
                observe_busy=scheduler.busy,
                observe_busy_since=store.foreground_busy_since,
                work_gate=scheduler.checkpoint,
            )
            original_begin = scheduler._begin
            acquired, entered = [], threading.Event()

            def acquire():
                run = store.create_run("accepted")
                acquired.append(
                    store.set_state(
                        run.run_id,
                        expected_version=1,
                        from_state="accepted",
                        to_state="routing",
                        holds_slot=True,
                    )
                )
                clock.advance(0.001)
                entered.set()

            def interleaved_begin(context):
                if not acquired and arrival == "before_begin":
                    acquire()
                original_begin(context)
                if not acquired:
                    acquire()

            monkeypatch.setattr(scheduler, "_begin", interleaved_begin)
            task = asyncio.create_task(
                scheduler.async_checkpoint(context)
                if gate == "async"
                else asyncio.to_thread(scheduler.checkpoint, context)
            )
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                await asyncio.sleep(0)
                assert not task.done(), "foreground ownership must prevent CPU admission"
            finally:
                clock.advance(0.07)
                run = acquired[0]
                store.set_state(
                    run.run_id,
                    expected_version=run.version,
                    from_state="routing",
                    to_state="completed",
                )
                scheduler.wake()
                await asyncio.wait_for(task, 1)
            try:
                # The initial pause is quiescent. A new interval wholly inside
                # admitted CPU must still survive its owner's completed state.
                assert context.drain_ms is None
                clock.advance(0.02)
                run = store.create_run("accepted")
                run = store.set_state(
                    run.run_id,
                    expected_version=1,
                    from_state="accepted",
                    to_state="routing",
                    holds_slot=True,
                )
                clock.advance(0.07)
                store.set_state(
                    run.run_id,
                    expected_version=run.version,
                    from_state="routing",
                    to_state="completed",
                )
                context.check()
                scheduler.end_batch(context, force=True)
                assert context.drain_ms == pytest.approx(70)
                assert sum(batch["duration_ms"] for batch in context.work_batches) == pytest.approx(
                    90
                )
            finally:
                store.release_foreground_window(window)

    asyncio.run(go())


@pytest.mark.parametrize("cpu_seconds", [0, 0.02])
@pytest.mark.parametrize("ending", ["failure", "cancelled"])
def test_finalization_flushes_cpu_before_nonresuming_native_io(cpu_seconds, ending):
    """RM16, RM38/AC29: final flush keeps performed CPU and excludes failed native waiting."""
    from relaymind.store import RunStore

    clock, cancelled = FakeClock(), False
    with RunStore(clock=clock) as store:
        scheduler = scheduling.AssetWorkScheduler(busy=lambda: False)
        window = store.foreground_window(monotonic=clock.monotonic)
        context = CaptureContext(
            datetime.now(UTC),
            monotonic=clock.monotonic,
            foreground_window=window,
            finish_cpu=scheduler.end_batch,
            work_gate=scheduler.checkpoint,
            cancelled=lambda: cancelled,
        )
        try:
            context.check()
            clock.advance(cpu_seconds)

            def native_io():
                nonlocal cancelled
                clock.advance(0.07)
                if ending == "failure":
                    raise SourceFailure("source_unavailable")
                cancelled = True

            with pytest.raises(SourceFailure):
                context.io(native_io)
            scheduler.end_batch(context, force=True)
            scheduler.end_batch(context, force=True)
            assert context.native_io_ms == pytest.approx([70])
            assert [batch["duration_ms"] for batch in context.work_batches] == pytest.approx(
                [] if cpu_seconds == 0 else [20]
            )
            assert context.drain_ms is None
        finally:
            store.release_foreground_window(window)


@pytest.mark.parametrize("ending", ["failure", "cancelled", "cpu_failure"])
def test_failed_source_job_retains_cpu_when_final_window_is_quiescent(
    tmp_path, monkeypatch, caplog, ending
):
    """RM16, RM38/AC29: failed/cancelled job outcomes include their actual short CPU batch."""
    from helpers import ScriptedAdapter
    from relaymind.assets import service as module

    caplog.set_level(logging.INFO, logger="relaymind.assets.service")

    async def go():
        launched = launcher.build_app(
            configured_catalog(tmp_path, warmup=False, detect=False),
            local_adapter=ScriptedAdapter(),
        )
        store, worker = launched.orchestrator.store, launched.app.state.asset_catalog
        clock = FakeClock()
        store.clock = worker.clock = clock
        contexts = []

        def capture(*args, context, **kwargs):
            contexts.append(context)
            context.check()
            clock.advance(0.02)
            if ending == "cpu_failure":
                raise SourceFailure("source_unavailable")

            def native_io():
                clock.advance(0.07)
                if ending == "failure":
                    raise SourceFailure("source_unavailable")
                worker._cancel.set()

            context.io(native_io)

        monkeypatch.setattr(module, "capture_source", capture)
        try:
            async with launched.app.router.lifespan_context(launched.app):
                with pytest.raises(SourceFailure):
                    await worker.rescan("example")
                job = worker.store.job_outcomes()[0]
                assert job["state"] == ("cancelled" if ending == "cancelled" else "failed")
                assert job["duration_ms"] == pytest.approx(20 if ending == "cpu_failure" else 90)
                assert job["batch_ms"] == pytest.approx([20])
                assert [
                    batch["duration_ms"] for batch in contexts[0].work_batches
                ] == pytest.approx([20])
                assert contexts[0].native_io_ms == pytest.approx(
                    [] if ending == "cpu_failure" else [70]
                )
                if ending != "cpu_failure":
                    records = [
                        record
                        for record in caplog.records
                        if record.msg.startswith("asset native I/O")
                    ]
                    assert records and records[0].args[1] == pytest.approx([70])
                assert not store._foreground_windows
        finally:
            store.close()

    asyncio.run(go())


def test_failed_admission_query_keeps_previous_io_quiescent(monkeypatch):
    """RM16, RM38/AC29: a failed final idle query cannot turn native I/O into CPU work."""
    from relaymind.store import RunStore

    clock = FakeClock()
    with RunStore(clock=clock) as store:
        scheduler = scheduling.AssetWorkScheduler(busy=lambda: False)
        window = store.foreground_window(monotonic=clock.monotonic)
        context = CaptureContext(
            datetime.now(UTC),
            monotonic=clock.monotonic,
            foreground_window=window,
            finish_cpu=scheduler.end_batch,
            work_gate=scheduler.checkpoint,
        )
        try:
            context.check()
            clock.advance(0.02)
            context.io(lambda: clock.advance(0.07), resume=False)
            original_begin = scheduler._begin

            def broken_query():
                raise RuntimeError("fixture database query failure")

            def begin(context):
                original_begin(context)
                monkeypatch.setattr(scheduler, "busy", broken_query)

            monkeypatch.setattr(scheduler, "_begin", begin)
            with pytest.raises(RuntimeError, match="fixture database query failure"):
                context.check()
            scheduler.end_batch(context, force=True)
            assert [batch["duration_ms"] for batch in context.work_batches] == pytest.approx([20])
            assert context.native_io_ms == pytest.approx([70])
            assert context.drain_ms is None
        finally:
            store.release_foreground_window(window)
