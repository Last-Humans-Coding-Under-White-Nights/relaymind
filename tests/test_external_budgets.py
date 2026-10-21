"""External-stage budgets (RM03, RM13, section 11).

Section 11: external generation is at most 30 seconds and is also
bounded by the remaining run budget. The per-stage local limits do not
constrain the external stage; the entire-run limits apply to every mode.
The external limits are configuration values recorded in each run
(RM03). Issue #18.
"""

import pytest

from helpers import FakeClock
from relaymind.orchestrator.budgets import BudgetExceededError, BudgetMeter, LocalBudgets


def external_meter(clock=None, **limits):
    clock = clock or FakeClock()
    meter = BudgetMeter(LocalBudgets(**limits), clock=clock)
    meter.start_external()
    return meter, clock


def test_the_external_limits_are_recorded_with_the_other_budgets():
    """RM03: the external limits are configuration values and appear in the run."""
    recorded = LocalBudgets().to_json_object()
    assert recorded["external_generations"] == 6
    assert recorded["external_seconds"] == 30.0
    custom = LocalBudgets(external_generations=2, external_seconds=12).to_json_object()
    assert (custom["external_generations"], custom["external_seconds"]) == (2, 12.0)


@pytest.mark.parametrize(
    "bad",
    [{"external_generations": 0}, {"external_seconds": 0}, {"external_seconds": float("inf")}],
)
def test_a_non_finite_or_zero_external_limit_is_refused(bad):
    """RM03: a limit is finite; infinity or NaN would be no limit."""
    with pytest.raises(ValueError):
        LocalBudgets(**bad)


def test_external_generations_ignore_the_stage_limits_but_count_in_the_run():
    """Section 11: the per-stage local limits do not constrain the external stage."""
    meter, _ = external_meter(local_generations=1, routing_generations=1, run_generations=3)
    meter.charge_generation("external")
    meter.charge_generation("external")
    usage = meter.usage()
    assert usage["external_generations"] == 2
    assert usage["local_generations"] == 0
    with pytest.raises(BudgetExceededError) as stop:
        meter.charge_generation("external")
        meter.charge_generation("external")
    assert stop.value.budget in ("run_generations", "external_generations")


def test_the_external_generation_limit_is_enforced():
    """RM03: exceeding a budget is an explicit stop."""
    meter, _ = external_meter(external_generations=1)
    meter.charge_generation("external")
    with pytest.raises(BudgetExceededError) as stop:
        meter.charge_generation("external")
    assert stop.value.budget == "external_generations"


def test_the_entire_run_generation_limit_still_applies():
    """Section 11: the total run limits apply to every mode."""
    meter, _ = external_meter(run_generations=2, external_generations=5)
    meter.charge_generation("external")
    meter.charge_generation("external")
    with pytest.raises(BudgetExceededError) as stop:
        meter.charge_generation("external")
    assert stop.value.budget == "run_generations"


def test_external_tool_calls_count_against_the_run_only():
    """Section 11: six executed business tools bound the whole run."""
    meter, _ = external_meter(tool_calls=0, run_tool_executions=2)
    meter.charge_tool_call()
    meter.charge_tool_call()
    with pytest.raises(BudgetExceededError) as stop:
        meter.charge_tool_call()
    assert stop.value.budget == "run_tool_executions"


def test_external_time_is_30_seconds_a_generation_inside_the_run_budget():
    """Section 11: at most 30 seconds, also bounded by the remaining run budget."""
    meter, clock = external_meter(active_seconds=4.0, run_active_seconds=60.0)
    clock.advance(10)  # the 4-second stage limit is long spent
    meter.check_time()  # but it no longer applies
    assert meter.remaining_seconds() == 30.0
    clock.advance(25)
    assert meter.remaining_seconds() == 25.0  # 60 - 35
    clock.advance(30)
    with pytest.raises(BudgetExceededError) as stop:
        meter.check_time()
    assert stop.value.budget == "run_active_seconds"


def test_the_stage_time_limit_still_applies_before_the_external_stage():
    """RM03: nothing changes for local work."""
    clock = FakeClock()
    meter = BudgetMeter(LocalBudgets(active_seconds=4.0), clock=clock)
    clock.advance(5)
    with pytest.raises(BudgetExceededError):
        meter.check_time()


def test_external_room_says_why_dispatch_is_impossible():
    """RM04: with no run budget left the run ends needs_external, not mid-handoff."""
    clock = FakeClock()
    meter = BudgetMeter(LocalBudgets(run_generations=1, routing_generations=1), clock=clock)
    assert meter.external_room() is None
    meter.charge_generation("routing")
    stop = meter.external_room()
    assert stop is not None and stop.budget == "run_generations"

    meter = BudgetMeter(LocalBudgets(run_active_seconds=5.0), clock=clock)
    clock.advance(6)
    stop = meter.external_room()
    assert stop is not None and stop.budget == "run_active_seconds"


def test_remaining_budgets_report_what_the_external_model_has_left():
    """RM13: the handoff preserves the remaining budget."""
    clock = FakeClock()
    meter = BudgetMeter(LocalBudgets(), clock=clock)
    meter.charge_generation("routing")
    meter.charge_generation("skill")
    meter.charge_tool_call()
    clock.advance(1.5)
    assert meter.remaining_budgets() == {
        "run_generations": 6,
        "run_tool_executions": 5,
        "run_active_seconds": 58.5,
        "external_generations": 6,
        "external_seconds": 30.0,
    }


def test_an_external_timeout_names_the_limit_that_bounded_the_generation():
    """RM03, RM16: the trace names the limit that stopped the work, not a later one.

    external_seconds is fixed for each generation while the run's time
    left shrinks, so the limit must be the one in force when the
    generation started (review of #18).
    """
    meter, clock = external_meter(run_active_seconds=60.0, external_seconds=30.0)
    clock.advance(4)  # the local stage used 4 seconds
    assert meter.remaining_seconds() == 30.0
    clock.advance(30)  # the generation timed out at external_seconds
    assert meter.time_exhausted().budget == "external_seconds"

    clock.advance(10)  # 44 seconds used, 16 left in the run
    assert meter.remaining_seconds() == 16.0
    clock.advance(16)
    assert meter.time_exhausted().budget == "run_active_seconds"


def test_external_tool_executions_are_reported_in_the_usage():
    """RM03, RM16: budget_usage counts every executed tool of the run."""
    clock = FakeClock()
    meter = BudgetMeter(LocalBudgets(), clock=clock)
    meter.charge_tool_call()
    meter.start_external()
    meter.charge_tool_call()
    meter.charge_tool_call()
    assert meter.usage()["tool_calls"] == 3
