"""RM03/RM25/RM27: serial local phase time is measured independently of budgets."""

from relaymind.orchestrator.budgets import BudgetMeter, LocalBudgets


class Clock:
    value = 0.0

    def monotonic(self):
        return self.value


def test_waits_resume_and_successful_handoff_freeze_one_boundary():
    clock = Clock()
    meter = BudgetMeter(LocalBudgets(), clock=clock)
    clock.value = 1.25
    meter.end_stage()
    clock.value = 101.25
    meter.new_stage()
    clock.value = 102.5
    boundary = clock.monotonic()
    assert meter.local_active_ms(boundary) == 2500
    clock.value = 103
    meter.close_local(boundary)
    meter.start_external()
    clock.value = 105
    meter.end_stage()
    clock.value = 110
    meter.new_stage()
    clock.value = 111
    assert meter.usage()["local_active_ms"] == 2500
    assert meter.usage()["active_ms"] == 6000


def test_external_origin_zero_and_parallel_unknown():
    clock = Clock()
    meter = BudgetMeter(LocalBudgets(), clock=clock, local_active=False)
    clock.value = 1
    assert meter.usage()["local_active_ms"] == 0
    meter.mark_local_unsupported()
    assert meter.usage()["local_active_ms"] is None


def test_snapshot_uses_one_clock_read_for_both_totals():
    """RM25: clock movement between field reads cannot make local exceed total."""

    class MovingClock:
        value = 0

        def monotonic(self):
            self.value += 0.001
            return self.value

    meter = BudgetMeter(LocalBudgets(), clock=MovingClock())
    usage = meter.usage()
    assert usage["local_active_ms"] == usage["active_ms"]


def test_gateway_terminal_and_handoff_share_measured_boundary(tmp_path):
    """RM27: real orchestrator handoff freezes local work, external work stays excluded."""
    from gateway_helpers import STAGING_OK, build_gateway, route_turn, run, text_turn, tool_turn
    from helpers import FakeClock
    from relaymind.adapters import GenerationFailed

    clock = FakeClock()
    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), [GenerationFailed(code="offline", message="offline")]],
        external_turns=[
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
        ],
        clock=clock,
        degraded_mode=True,
    )
    _, events = run(gateway)
    handoffs = [e for e in events if e.event_type.value == "handoff.started"]
    assert handoffs
    assert events[-1].payload["local_active_ms"] == handoffs[0].payload["local_active_ms"]
