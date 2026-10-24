"""AC21 / RM17 / RM27: maintenance shares the cross-connection execution slot."""

import asyncio

import pytest

from gateway_helpers import build_gateway
from helpers import FakeClock
from relaymind.orchestrator.placement import PlacementSettings
from relaymind.store import RunStore, SlotUnavailableError


def test_probe_reservation_blocks_new_run_and_requests_cancel(tmp_path):
    gateway = build_gateway(tmp_path, [], placement=PlacementSettings(policy="device_aware"))
    second = RunStore(tmp_path / "runs.sqlite3", clock=gateway.store.clock)
    assert second.try_reserve_probe("probe1", "owner", model="local", after_seconds=600)
    with pytest.raises(SlotUnavailableError):
        asyncio.run(gateway.orchestrator.run_text("hello"))
    assert second.probe_reservation()["cancel_requested"]
    assert gateway.store.active_run_id() is None
    assert gateway.store.unfinished_runs(()) == []
    assert not second.finish_probe("other", "owner")
    assert second.probe_reservation() is not None
    assert second.finish_probe("probe1", "owner")
    assert not second.try_reserve_probe("probe2", "owner", model="local", after_seconds=600)
    gateway.store.clock.advance(601)
    assert second.try_reserve_probe("probe2", "owner", model="local", after_seconds=600)


def test_store_open_never_reclaims_probe_but_genuine_recovery_does(tmp_path):
    gateway = build_gateway(tmp_path, [])
    assert gateway.store.try_reserve_probe("probe1", "old", model="local", after_seconds=600)
    second = RunStore(tmp_path / "runs.sqlite3", clock=gateway.store.clock)
    assert second.probe_reservation()["token"] == "probe1"
    gateway.orchestrator.recover_after_restart()
    assert second.probe_reservation() is None


def test_active_run_excludes_probe_and_stale_version_does_not_cancel(tmp_path):
    from helpers import ACCEPTED_PAYLOAD

    store = RunStore(tmp_path / "store", clock=FakeClock())
    run = store.create_run("accepted", accepted=("run.accepted", "gateway", ACCEPTED_PAYLOAD))
    run = store.set_state(
        run.run_id, expected_version=1, from_state="accepted", to_state="routing", holds_slot=True
    )
    assert not store.try_reserve_probe("p", "owner", model="local", after_seconds=600)
    store.set_state(
        run.run_id, expected_version=run.version, from_state="routing", to_state="waiting_input"
    )
    assert store.try_reserve_probe("p", "owner", model="local", after_seconds=600)
    from relaymind.store import StaleVersionError

    with pytest.raises(StaleVersionError):
        store.set_state(
            run.run_id,
            expected_version=1,
            from_state="waiting_input",
            to_state="routing",
            holds_slot=True,
        )
    assert not store.probe_reservation()["cancel_requested"]


def test_idle_probe_records_one_unowned_sample_and_cooldown(tmp_path):
    """AC21: one stale routing-shaped call; never a user's run or a skill warm-up."""
    from relaymind.adapters import Completed, Usage
    from relaymind.orchestrator.warmup import IdleProbe

    gateway = build_gateway(
        tmp_path,
        [[Usage(100, 10, 0), Completed("stop")]],
        external_turns=[],
        placement=PlacementSettings(policy="device_aware"),
    )

    async def go():
        probe = IdleProbe(gateway.orchestrator, cancel_wait_seconds=0.01)
        await probe.tick()
        await asyncio.sleep(0)
        await probe.tick()
        assert len(gateway.adapter.requests) == 1
        samples = gateway.store.model_timings()
        assert len(samples) == 1
        assert samples[0].kind == "probe" and samples[0].succeeded
        assert samples[0].run_id is None
        assert gateway.store.unfinished_runs(()) == []
        await probe.stop()

    asyncio.run(go())


def test_resistant_probe_keeps_slot_until_iterator_ends(tmp_path):
    """RM17/AC21: bounded shutdown is not authority to release a live reservation."""
    from relaymind.adapters import Completed
    from relaymind.orchestrator.warmup import IdleProbe

    gateway = build_gateway(
        tmp_path, [], external_turns=[], placement=PlacementSettings(policy="device_aware")
    )

    async def go():
        gate = asyncio.Event()

        async def resistant(request):
            gateway.adapter.requests.append(request)
            try:
                await gate.wait()
            except asyncio.CancelledError:
                await gate.wait()
            yield Completed("stop")

        gateway.adapter.generate = resistant
        probe = IdleProbe(gateway.orchestrator, cancel_wait_seconds=0.001)
        await probe.tick()
        await asyncio.sleep(0)
        await probe.stop()
        assert gateway.store.probe_reservation() is not None
        with pytest.raises(SlotUnavailableError):
            await gateway.orchestrator.run_text("hello")
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert gateway.store.probe_reservation() is None
        assert gateway.store.model_timings()[0].succeeded is False
        assert len(gateway.adapter.cancelled) == 1

    asyncio.run(go())


def test_probe_blocks_valid_confirmation_without_consuming_approval(tmp_path):
    """RM11/RM17: invalid token does not preempt; valid approval rolls back on 429."""
    from relaymind.orchestrator.local_loop import ApprovalRejectedError
    from test_ac05_confirmation import APPROVED_TAIL, decide, waiting_gateway

    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)
    other = RunStore(tmp_path / "runs.sqlite3", clock=gateway.store.clock)
    assert other.try_reserve_probe("p", "owner", model="different", after_seconds=600)
    with pytest.raises(ApprovalRejectedError):
        decide(gateway, waiting, token="wrong")
    assert not other.probe_reservation()["cancel_requested"]
    with pytest.raises(SlotUnavailableError):
        decide(gateway, waiting)
    assert other.probe_reservation()["cancel_requested"]
    other.finish_probe("p", "owner")
    assert decide(gateway, waiting).state.value == "completed"


def test_probe_blocks_input_resume_and_client_acceptance(tmp_path):
    """RM17/RM19: both acquisition paths share the store reservation boundary."""
    from gateway_helpers import route_turn, run
    from relaymind.adapters import Message, Role
    from relaymind.orchestrator.client_loop import ToolOffer, run_client_request

    gateway = build_gateway(tmp_path, [route_turn("check-service-health")])
    waiting, _ = run(gateway, "Check service health")
    record = gateway.store.get_run(waiting.run_id)
    assert record.state == "waiting_input"
    assert gateway.store.try_reserve_probe("p", "owner", model="other", after_seconds=600)
    with pytest.raises(SlotUnavailableError):
        asyncio.run(gateway.orchestrator.resume(waiting.run_id, record.version, "staging-api"))
    assert gateway.store.get_run(waiting.run_id).version == record.version
    with pytest.raises(SlotUnavailableError):
        asyncio.run(
            run_client_request(
                gateway.orchestrator, [Message(role=Role.USER, content="hi")], ToolOffer(())
            )
        )
    assert len(gateway.store.unfinished_runs(())) == 1


@pytest.mark.parametrize("policy,external", [("local_first", True), ("device_aware", False)])
def test_exempt_configuration_never_probes(tmp_path, policy, external):
    """AC21: only configured local and external paths under active policies probe."""
    from relaymind.orchestrator.warmup import IdleProbe

    gateway = build_gateway(
        tmp_path,
        [],
        external_turns=[] if external else None,
        placement=PlacementSettings(policy=policy),
    )
    asyncio.run(IdleProbe(gateway.orchestrator).tick())
    assert gateway.adapter.requests == []
    assert gateway.store.probe_reservation() is None


def test_failed_probe_enforces_persisted_cooldown(tmp_path):
    """AC21: failed refreshes must not repeatedly block foreground retries."""
    from relaymind.adapters import GenerationFailed
    from relaymind.orchestrator.warmup import IdleProbe

    gateway = build_gateway(
        tmp_path,
        [[GenerationFailed(code="failed", message="no")]],
        external_turns=[],
        placement=PlacementSettings(policy="device_aware"),
    )

    async def go():
        probe = IdleProbe(gateway.orchestrator)
        await probe.tick()
        await probe.tick()
        assert len(gateway.adapter.requests) == 1
        assert not gateway.store.model_timings()[0].succeeded
        other = RunStore(tmp_path / "runs.sqlite3", clock=gateway.store.clock)
        assert not other.try_reserve_probe("new", "owner", model="local", after_seconds=600)
        await probe.stop()

    asyncio.run(go())


def test_probe_and_foreground_slot_race_has_exactly_one_winner(tmp_path):
    """RM17/AC21: two connections racing BEGIN IMMEDIATE cannot both dispatch."""
    import threading

    from helpers import ACCEPTED_PAYLOAD

    first = RunStore(tmp_path / "race", clock=FakeClock())
    second = RunStore(tmp_path / "race", clock=first.clock)
    run = first.create_run("accepted", accepted=("run.accepted", "gateway", ACCEPTED_PAYLOAD))
    barrier = threading.Barrier(2)
    results = []

    def probe():
        barrier.wait()
        results.append(second.try_reserve_probe("p", "owner", model="local", after_seconds=600))

    worker = threading.Thread(target=probe)
    worker.start()
    barrier.wait()
    try:
        first.set_state(
            run.run_id,
            expected_version=1,
            from_state="accepted",
            to_state="routing",
            holds_slot=True,
        )
        active = True
    except SlotUnavailableError:
        active = False
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert results == [not active]


def test_successful_probe_can_reclassify_unsuitable_model(tmp_path):
    """RM26/RM27: unsuitable is a measurement, not a permanent class."""
    from relaymind.adapters import Completed, FirstToken, Usage
    from relaymind.orchestrator.local_status import LocalModelClass, LocalStatus
    from relaymind.orchestrator.warmup import IdleProbe

    gateway = build_gateway(
        tmp_path,
        [],
        external_turns=[],
        local_status=LocalStatus(LocalModelClass.UNSUITABLE, "cold routing too slow"),
        placement=PlacementSettings(policy="device_aware"),
    )

    async def measured(request):
        gateway.store.clock.advance(0.01)
        yield FirstToken()
        gateway.store.clock.advance(0.01)
        yield Usage(100, 100, 0)
        yield Completed("stop")

    gateway.adapter.generate = measured

    async def go():
        probe = IdleProbe(gateway.orchestrator)
        await probe.tick()
        await probe.stop()

    asyncio.run(go())
    assert gateway.orchestrator.local_status.status == LocalModelClass.READY


@pytest.mark.parametrize("reason", ["cooldown", "active_run", "reserved", "fresh_sample"])
def test_ineligible_probe_does_not_open_write_transaction(tmp_path, reason):
    """RM17/AC21: idle checks must leave the writer available to foreground work."""
    from helpers import ACCEPTED_PAYLOAD
    from test_placement import seed

    store = RunStore(tmp_path / "store", clock=FakeClock())
    if reason in ("cooldown", "reserved"):
        assert store.try_reserve_probe("first", "owner", model="local", after_seconds=600)
        if reason == "cooldown":
            store.finish_probe("first", "owner")
    elif reason == "active_run":
        run = store.create_run("accepted", accepted=("run.accepted", "gateway", ACCEPTED_PAYLOAD))
        store.set_state(
            run.run_id,
            expected_version=1,
            from_state="accepted",
            to_state="routing",
            holds_slot=True,
        )
    else:
        seed(store)
    statements = []
    store._conn.set_trace_callback(statements.append)
    assert not store.try_reserve_probe("new", "owner", model="local", after_seconds=600)
    assert not any(sql.startswith("BEGIN") for sql in statements)


@pytest.mark.parametrize("reason", ["cooldown", "active_run", "reserved", "fresh_sample"])
def test_probe_rechecks_eligibility_under_write_lock(tmp_path, monkeypatch, reason):
    """RM17/AC21: another connection can invalidate an optimistic idle check."""
    from contextlib import contextmanager

    from helpers import ACCEPTED_PAYLOAD
    from test_placement import seed

    store = RunStore(tmp_path / "store", clock=FakeClock())
    other = RunStore(tmp_path / "store", clock=store.clock)
    run = other.create_run("accepted", accepted=("run.accepted", "gateway", ACCEPTED_PAYLOAD))
    write = store._write

    @contextmanager
    def raced_write():
        if reason in ("cooldown", "reserved"):
            assert other.try_reserve_probe("first", "other", model="local", after_seconds=600)
            if reason == "cooldown":
                other.finish_probe("first", "other")
        elif reason == "active_run":
            other.set_state(
                run.run_id,
                expected_version=1,
                from_state="accepted",
                to_state="routing",
                holds_slot=True,
            )
        else:
            seed(other)
        with write():
            yield

    monkeypatch.setattr(store, "_write", raced_write)
    assert not store.try_reserve_probe("new", "owner", model="local", after_seconds=600)
    reservation = store.probe_reservation()
    assert reservation is None or reservation["token"] == "first"


def test_idle_scheduler_limits_wakeups_without_delaying_active_cancellation(tmp_path, monkeypatch):
    """RM27/AC21: coarse idle polling retains prompt checks while this owner is probing."""
    from relaymind.adapters import Completed
    from relaymind.orchestrator.warmup import IdleProbe

    gateway = build_gateway(
        tmp_path, [], external_turns=[], placement=PlacementSettings(policy="device_aware")
    )
    sleep = asyncio.sleep

    async def go():
        gate = asyncio.Event()

        async def generate(request):
            await gate.wait()
            yield Completed("stop")

        gateway.adapter.generate = generate
        probe = IdleProbe(gateway.orchestrator)
        await probe.tick()
        delays = []

        async def scheduled_sleep(delay):
            delays.append(delay)
            probe._closing = True
            await sleep(0)

        monkeypatch.setattr(asyncio, "sleep", scheduled_sleep)
        await probe._schedule()
        assert delays and max(delays) <= 0.05
        await probe.stop()
        assert gateway.store.probe_reservation() is None

        idle = IdleProbe(gateway.orchestrator)
        probe = idle
        delays.clear()
        await idle._schedule()
        assert delays and min(delays) >= 1
        await idle.stop()

    asyncio.run(go())


@pytest.mark.parametrize("boundary", ["tick", "stop", "scheduler_stop"])
@pytest.mark.parametrize("error_type", [TimeoutError, ValueError])
def test_probe_timing_evidence_errors_remain_visible(tmp_path, monkeypatch, boundary, error_type):
    """RM17/RM25/AC21: persistence failures are not tolerant runtime failures."""
    from relaymind.adapters import Completed
    from relaymind.orchestrator.warmup import IdleProbe

    gateway = build_gateway(
        tmp_path,
        [[Completed("stop")]],
        external_turns=[],
        placement=PlacementSettings(policy="device_aware"),
    )
    error = error_type("cannot record timing")

    def fail_write(sample):
        raise error

    monkeypatch.setattr(gateway.store, "add_model_timing", fail_write)

    async def go():
        probe = IdleProbe(gateway.orchestrator)
        await probe.tick()
        assert probe._task.done()
        assert gateway.store.probe_reservation() is None
        if boundary == "scheduler_stop":
            probe._scheduler = asyncio.create_task(probe._schedule())
            await asyncio.sleep(0)
        with pytest.raises(error_type) as caught:
            await (probe.tick() if boundary == "tick" else probe.stop())
        assert caught.value is error
        assert len(gateway.adapter.requests) == 1
        # Retrieve any task error even when the scheduler was the reporting boundary.
        if not probe._task.cancelled():
            probe._task.exception()

    asyncio.run(go())


def test_probe_with_no_retained_sample_does_not_reclassify_model(tmp_path, monkeypatch):
    """RM26/RM27/AC21: missing evidence cannot invent a prediction or readiness."""
    from relaymind.adapters import Completed, Usage
    from relaymind.orchestrator.local_status import LocalModelClass, LocalStatus
    from relaymind.orchestrator.warmup import IdleProbe

    gateway = build_gateway(
        tmp_path,
        [[Usage(100, 10, 0), Completed("stop")]],
        external_turns=[],
        local_status=LocalStatus(LocalModelClass.UNSUITABLE, "routing too slow"),
        placement=PlacementSettings(policy="device_aware"),
    )
    # Simulate evidence expiring between recording the attempt and reading it.
    monkeypatch.setattr(gateway.store, "model_timings", lambda **kwargs: [])

    async def go():
        probe = IdleProbe(gateway.orchestrator)
        await probe.tick()
        await probe._task
        await probe.stop()

    asyncio.run(go())
    assert gateway.orchestrator.local_status.status == LocalModelClass.UNSUITABLE
    assert gateway.store.probe_reservation() is None
