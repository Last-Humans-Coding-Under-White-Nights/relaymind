"""RM16, RM38/AC29: scheduler evidence and warm-up startup review regressions."""

import asyncio
from datetime import UTC, datetime

import pytest

from helpers import FakeClock
from relaymind import launcher
from relaymind.api.server import build_server
from relaymind.assets import scheduler as scheduling
from relaymind.assets.sources import CaptureContext, SourceFailure
from test_asset_readiness import WarmupBarrier, configured_catalog
from test_chat_disconnect_cancel import until


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
