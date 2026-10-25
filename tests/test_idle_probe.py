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
        # A 600 s cooldown is slept in bounded steps, so settings and clocks are rechecked.
        assert delays == [60]
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
        # A reported task error is not raised again by a later check.
        if boundary == "stop":
            probe._task.exception()
        else:
            assert probe._task is None

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
    monkeypatch.setattr(gateway.store, "latest_model_timing", lambda **kwargs: None)

    async def go():
        probe = IdleProbe(gateway.orchestrator)
        await probe.tick()
        await probe._task
        await probe.stop()

    asyncio.run(go())
    assert gateway.orchestrator.local_status.status == LocalModelClass.UNSUITABLE
    assert gateway.store.probe_reservation() is None


def test_refused_admission_leaves_no_run_clock_behind(tmp_path):
    """RM17/RM25/AC21: a create rolled back by a 429 keeps no in-process run state."""
    gateway = build_gateway(tmp_path, [], placement=PlacementSettings(policy="device_aware"))
    with RunStore(tmp_path / "runs.sqlite3", clock=gateway.store.clock) as other:
        assert other.try_reserve_probe("probe1", "owner", model="local", after_seconds=600)
        for _ in range(3):
            with pytest.raises(SlotUnavailableError):
                asyncio.run(gateway.orchestrator.run_text("hello"))
    assert gateway.store.unfinished_runs(()) == []
    assert gateway.store._run_started == {}


def test_rolled_back_transaction_drops_clocks_it_started(tmp_path):
    """RM17/RM25: only committed runs keep a monotonic baseline."""
    from helpers import ACCEPTED_PAYLOAD

    with RunStore(tmp_path / "store", clock=FakeClock()) as store:
        kept = store.create_run("accepted", accepted=("run.accepted", "gateway", ACCEPTED_PAYLOAD))
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.start_run_clock(kept.run_id)
                store.create_run("accepted", accepted=("run.accepted", "gateway", ACCEPTED_PAYLOAD))
                raise RuntimeError("roll back")
        assert list(store._run_started) == [kept.run_id]
        assert [r.run_id for r in store.unfinished_runs(())] == [kept.run_id]


def test_probe_eligibility_reads_only_the_latest_successful_sample(tmp_path, monkeypatch):
    """RM27/AC21: an idle check is one indexed read, never a scan of retained samples."""
    from test_placement import seed

    with RunStore(tmp_path / "store", clock=FakeClock()) as store:
        seed(store, input_tokens=1)
        store.clock.advance(700)
        seed(store, input_tokens=2)
        seed(store, input_tokens=3)  # same timestamp: insertion order decides
        seed(store, input_tokens=4, succeeded=False)
        seed(store, input_tokens=5, purpose="skill")
        latest = store.latest_model_timing(model="local", purpose="routing", actor="local")
        assert latest is not None and latest.input_tokens == 3
        assert store.latest_model_timing(model="other", purpose="routing", actor="local") is None

        def scan(**kwargs):
            raise AssertionError("an idle check must not load every sample")

        monkeypatch.setattr(store, "model_timings", scan)
        statements = []
        store._conn.set_trace_callback(statements.append)
        assert not store.try_reserve_probe("p", "owner", model="local", after_seconds=600)
        assert any("LIMIT 1" in sql for sql in statements if "model_timings" in sql)
        store.clock.advance(601)
        assert store.try_reserve_probe("p", "owner", model="local", after_seconds=600)


def test_probe_wait_is_the_time_until_cooldown_and_freshness_allow_one(tmp_path):
    """RM27/AC21: the scheduler can sleep until the next eligible moment."""
    from test_placement import seed

    with RunStore(tmp_path / "store", clock=FakeClock()) as store:
        assert store.probe_wait_seconds(model="local", after_seconds=30) == 0
        seed(store)
        store.clock.advance(10)
        assert store.probe_wait_seconds(model="local", after_seconds=30) == 20
        seed(store, succeeded=False)  # a failed sample refreshes nothing
        assert store.probe_wait_seconds(model="local", after_seconds=30) == 20
        store.clock.advance(25)
        assert store.try_reserve_probe("p", "owner", model="local", after_seconds=30)
        store.finish_probe("p", "owner")
        store.clock.advance(5)
        assert store.probe_wait_seconds(model="local", after_seconds=30) == 25


def test_idle_scheduler_sleeps_until_the_next_eligible_time(tmp_path, monkeypatch):
    """RM27/AC21: an idle scheduler wakes when a probe can start, within bounds."""
    from helpers import ACCEPTED_PAYLOAD
    from relaymind.adapters import Completed
    from relaymind.orchestrator.warmup import IdleProbe

    gateway = build_gateway(
        tmp_path,
        [[Completed("stop")]],
        external_turns=[],
        placement=PlacementSettings(policy="device_aware", probe_after_seconds=30),
    )
    sleep = asyncio.sleep
    delays = []

    async def go():
        probe = IdleProbe(gateway.orchestrator)
        await probe.tick()
        await probe._task

        async def scheduled_sleep(delay):
            delays.append(delay)
            probe._closing = True
            await sleep(0)

        monkeypatch.setattr(asyncio, "sleep", scheduled_sleep)

        async def once():
            probe._closing = False
            await probe._schedule()
            return delays.pop()

        assert await once() == 30  # the cooldown of the attempt just made
        gateway.store.clock.advance(29.5)
        assert await once() == 1  # never a busy loop
        gateway.store.clock.advance(1)
        run = gateway.store.create_run(
            "accepted", accepted=("run.accepted", "gateway", ACCEPTED_PAYLOAD)
        )
        gateway.store.set_state(
            run.run_id,
            expected_version=1,
            from_state="accepted",
            to_state="routing",
            holds_slot=True,
        )
        assert await once() == 1  # an active run is polled, not timed
        await probe.stop()

    asyncio.run(go())


def test_failed_scheduler_check_is_logged_backed_off_and_retried(tmp_path, monkeypatch, caplog):
    """RM17/AC21: one failed check never ends maintenance; the error stays visible."""
    import logging

    from relaymind.orchestrator.warmup import IdleProbe

    gateway = build_gateway(
        tmp_path, [], external_turns=[], placement=PlacementSettings(policy="device_aware")
    )
    sleep = asyncio.sleep
    error = OSError("disk unavailable")
    calls = []

    # Another owner's reservation keeps the real checks from starting a probe.
    assert gateway.store.try_reserve_probe("held", "other", model="local", after_seconds=600)

    async def go():
        probe = IdleProbe(gateway.orchestrator)
        tick = probe.tick

        async def failing_tick():
            calls.append(len(calls))
            if len(calls) <= 3:
                raise error
            if len(calls) == 5:
                probe._closing = True
            await tick()

        delays = []

        async def scheduled_sleep(delay):
            delays.append(delay)
            await sleep(0)

        monkeypatch.setattr(probe, "tick", failing_tick)
        monkeypatch.setattr(asyncio, "sleep", scheduled_sleep)
        with caplog.at_level(logging.ERROR, logger="relaymind.orchestrator.warmup"):
            await probe._schedule()
        assert len(calls) == 5
        # Backoff grows after each failed check and resets after a good one.
        assert delays[0] < delays[1] < delays[2] <= 30
        assert delays[3:] == [1, 1]
        assert len([r for r in caplog.records if r.exc_info and r.exc_info[1] is error]) == 3
        with pytest.raises(OSError) as caught:
            await probe.stop()
        assert caught.value is error

    asyncio.run(go())


def test_inflight_probe_is_still_cancelled_after_a_failed_check(tmp_path, monkeypatch):
    """RM17/AC21: a foreground 429 still stops the probe after a transient store error."""
    from relaymind.adapters import Completed
    from relaymind.orchestrator.warmup import IdleProbe

    gateway = build_gateway(
        tmp_path, [], external_turns=[], placement=PlacementSettings(policy="device_aware")
    )
    sleep = asyncio.sleep

    async def go():
        gate = asyncio.Event()

        async def generate(request):
            gateway.adapter.requests.append(request)
            await gate.wait()
            yield Completed("stop")

        gateway.adapter.generate = generate
        probe = IdleProbe(gateway.orchestrator)
        await probe.tick()
        task = probe._task
        reservation = gateway.store.probe_reservation
        tick = probe.tick
        failures = [OSError("busy")]

        async def flaky_tick():
            if failures:
                raise failures.pop()
            await tick()

        monkeypatch.setattr(probe, "tick", flaky_tick)

        async def fast_sleep(delay):
            await sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)
        probe._scheduler = asyncio.create_task(probe._schedule())
        with pytest.raises(SlotUnavailableError):
            await gateway.orchestrator.run_text("hello")
        for _ in range(50):
            if task.done():
                break
            await sleep(0)
        assert task.done()
        assert reservation() is None
        assert not gateway.store.model_timings()[0].succeeded
        with pytest.raises(OSError):
            await probe.stop()

    asyncio.run(go())
