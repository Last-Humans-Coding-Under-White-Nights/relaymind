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
