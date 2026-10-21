"""Local execution budgets (RM03, section 11).

RM03: local execution is bounded. The defaults are one routing
generation, up to three further local generations, two executed tool
calls, one format-repair generation within those limits, and a
four-second active local budget that includes routing and tools. The
limits are configuration values and are recorded in each run. Section
11: router input is at most 2,048 local tokens and routing output at
most 128 tokens. Exceeding a budget is never silent continuation (RM04).
Issue #15.

Spec 0.6 section 11 (#16): the Local execution row is a per-stage
limit, and a resume after a clarification opens a new local stage with
the full per-stage limits. The Entire run row (60 seconds active time,
eight model generations including routing and repair, six executed
business tools) bounds the sum of all stages. Waiting time counts toward
none of them; a wait lasts at most five minutes (waiting_seconds), and a
run asks for clarification at most twice (clarification_rounds).
"""

import math

import pytest

from helpers import FakeClock
from relaymind.orchestrator.budgets import BudgetExceededError, BudgetMeter, LocalBudgets


def test_defaults_are_the_rm03_and_section_11_values():
    budgets = LocalBudgets()
    assert budgets.to_json_object() == {
        "routing_generations": 1,
        "local_generations": 3,
        "tool_calls": 2,
        "repair_generations": 1,
        "active_seconds": 4.0,
        "routing_input_tokens": 2048,
        "routing_output_tokens": 128,
        "run_active_seconds": 60.0,
        "run_generations": 8,
        "run_tool_executions": 6,
        "waiting_seconds": 300.0,
        "clarification_rounds": 2,
    }


def test_every_budget_is_configurable():
    budgets = LocalBudgets(
        routing_generations=1,
        local_generations=5,
        tool_calls=4,
        repair_generations=2,
        active_seconds=8.0,
        routing_input_tokens=1024,
        routing_output_tokens=64,
    )
    assert budgets.to_json_object()["local_generations"] == 5
    assert budgets.to_json_object()["active_seconds"] == 8.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"routing_generations": 0},
        {"local_generations": 0},
        {"tool_calls": -1},
        {"repair_generations": -1},
        {"active_seconds": 0},
        {"routing_input_tokens": 0},
        {"routing_output_tokens": 0},
        {"run_active_seconds": 0},
        {"run_active_seconds": math.inf},
        {"run_generations": 0},
        {"run_tool_executions": -1},
        {"waiting_seconds": 0},
        {"waiting_seconds": math.nan},
        {"clarification_rounds": -1},
        {"clarification_rounds": 1.0},
        {"local_generations": True},
        {"local_generations": 2.0},
        # RM03: a non-finite limit would be no limit at all.
        {"active_seconds": math.nan},
        {"active_seconds": math.inf},
        {"routing_input_tokens": math.inf},
        # Repair happens within the generation limit, never on top of it.
        {"local_generations": 1, "repair_generations": 2},
    ],
)
def test_invalid_budgets_are_rejected(overrides):
    with pytest.raises(ValueError):
        LocalBudgets(**overrides)


def test_a_second_routing_generation_exceeds_the_budget():
    meter = BudgetMeter(LocalBudgets(), clock=FakeClock())
    meter.charge_generation("routing")
    with pytest.raises(BudgetExceededError) as exceeded:
        meter.charge_generation("routing")
    assert exceeded.value.budget == "routing_generations"
    assert exceeded.value.reason_code == "local_budget"


def test_a_fourth_local_generation_exceeds_the_budget():
    meter = BudgetMeter(LocalBudgets(), clock=FakeClock())
    meter.charge_generation("routing")
    for _ in range(3):
        meter.charge_generation("skill")
    with pytest.raises(BudgetExceededError) as exceeded:
        meter.charge_generation("skill")
    assert exceeded.value.budget == "local_generations"


def test_a_third_tool_call_exceeds_the_budget():
    meter = BudgetMeter(LocalBudgets(), clock=FakeClock())
    meter.charge_tool_call()
    meter.charge_tool_call()
    with pytest.raises(BudgetExceededError) as exceeded:
        meter.charge_tool_call()
    assert exceeded.value.budget == "tool_calls"


def test_repair_counts_against_the_local_generation_limit():
    meter = BudgetMeter(LocalBudgets(), clock=FakeClock())
    meter.charge_generation("skill")
    meter.charge_generation("repair")
    meter.charge_generation("skill")
    with pytest.raises(BudgetExceededError) as exceeded:
        meter.charge_generation("skill")
    assert exceeded.value.budget == "local_generations"


def test_a_second_repair_exceeds_the_repair_budget():
    meter = BudgetMeter(LocalBudgets(), clock=FakeClock())
    meter.charge_generation("repair")
    with pytest.raises(BudgetExceededError) as exceeded:
        meter.charge_generation("repair")
    assert exceeded.value.budget == "repair_generations"


def test_a_refused_charge_does_not_count():
    meter = BudgetMeter(LocalBudgets(tool_calls=1), clock=FakeClock())
    meter.charge_tool_call()
    with pytest.raises(BudgetExceededError):
        meter.charge_tool_call()
    assert meter.usage()["tool_calls"] == 1


def test_active_time_covers_routing_and_tool_time():
    clock = FakeClock()
    meter = BudgetMeter(LocalBudgets(active_seconds=4.0), clock=clock)
    meter.charge_generation("routing")
    clock.advance(1.5)  # routing generation
    meter.check_time()
    meter.charge_tool_call()
    clock.advance(2.0)  # tool execution
    meter.check_time()
    assert meter.remaining_seconds() == pytest.approx(0.5)
    clock.advance(0.6)
    with pytest.raises(BudgetExceededError) as exceeded:
        meter.check_time()
    assert exceeded.value.budget == "active_seconds"


def test_no_charge_is_accepted_after_the_time_budget_is_spent():
    clock = FakeClock()
    meter = BudgetMeter(LocalBudgets(active_seconds=1.0), clock=clock)
    clock.advance(1.01)
    with pytest.raises(BudgetExceededError):
        meter.charge_generation("skill")
    with pytest.raises(BudgetExceededError):
        meter.charge_tool_call()


def test_usage_reports_every_counter_and_elapsed_time():
    clock = FakeClock()
    meter = BudgetMeter(LocalBudgets(), clock=clock)
    meter.charge_generation("routing")
    meter.charge_generation("skill")
    meter.charge_generation("repair")
    meter.charge_tool_call()
    clock.advance(0.25)
    assert meter.usage() == {
        "routing_generations": 1,
        "local_generations": 2,
        "repair_generations": 1,
        "tool_calls": 1,
        "active_ms": 250,
        "stages": 1,
    }


# --- spec 0.6 section 11: per-stage limits, entire-run limits -------------------------


def test_a_new_stage_has_the_full_per_stage_limits():
    """Section 11: a resume opens a new local stage with the full per-stage
    limits, so a resumed run that routes again has its routing generation."""
    meter = BudgetMeter(LocalBudgets(), clock=FakeClock())
    meter.charge_generation("routing")
    meter.charge_generation("repair")
    meter.charge_tool_call()
    meter.end_stage()
    meter.new_stage()
    meter.charge_generation("routing")
    assert meter.may_repair() is True
    meter.charge_generation("repair")
    meter.charge_tool_call()
    meter.charge_tool_call()
    usage = meter.usage()
    assert usage["routing_generations"] == 2
    assert usage["repair_generations"] == 2
    assert usage["tool_calls"] == 3
    assert usage["stages"] == 2


def test_the_entire_run_bounds_generations_across_stages():
    """Section 11: at most eight model generations in the whole run."""
    meter = BudgetMeter(LocalBudgets(), clock=FakeClock())
    for _ in range(2):
        meter.charge_generation("routing")
        for _ in range(3):
            meter.charge_generation("skill")
        meter.end_stage()
        meter.new_stage()
    with pytest.raises(BudgetExceededError) as exceeded:
        meter.charge_generation("routing")
    assert exceeded.value.budget == "run_generations"
    assert exceeded.value.reason_code == "local_budget"
    assert meter.usage()["routing_generations"] == 2


def test_the_entire_run_bounds_tool_executions_across_stages():
    meter = BudgetMeter(LocalBudgets(run_tool_executions=3), clock=FakeClock())
    meter.charge_tool_call()
    meter.charge_tool_call()
    meter.end_stage()
    meter.new_stage()
    meter.charge_tool_call()
    with pytest.raises(BudgetExceededError) as exceeded:
        meter.charge_tool_call()
    assert exceeded.value.budget == "run_tool_executions"


def test_the_entire_run_bounds_active_time_across_stages():
    clock = FakeClock()
    meter = BudgetMeter(LocalBudgets(active_seconds=4.0, run_active_seconds=6.0), clock=clock)
    clock.advance(3.5)
    meter.end_stage()
    clock.advance(500)  # waiting: no budget counts it
    meter.new_stage()
    clock.advance(2.0)
    meter.check_time()  # 2.0 s in this stage, 5.5 s in the run
    assert meter.remaining_seconds() == pytest.approx(0.5)
    clock.advance(0.6)
    with pytest.raises(BudgetExceededError) as exceeded:
        meter.check_time()
    assert exceeded.value.budget == "run_active_seconds"


def test_waiting_time_is_excluded_from_active_time():
    """Section 11: the waiting state is excluded from active execution time."""
    clock = FakeClock()
    meter = BudgetMeter(LocalBudgets(), clock=clock)
    clock.advance(1.5)
    meter.end_stage()
    clock.advance(200.0)
    assert meter.usage()["active_ms"] == 1500
    meter.new_stage()
    clock.advance(0.5)
    assert meter.active_seconds() == pytest.approx(2.0)
    assert meter.usage()["active_ms"] == 2000
    meter.check_time()


def test_every_generation_purpose_is_charged_in_one_place():
    """RM03: routing, skill, repair, and final generations share one charge path."""
    meter = BudgetMeter(LocalBudgets(), clock=FakeClock())
    for purpose in ("routing", "skill", "repair", "final"):
        meter.charge_generation(purpose)
    usage = meter.usage()
    assert (usage["routing_generations"], usage["local_generations"]) == (1, 3)
    assert usage["repair_generations"] == 1
    assert meter.may_repair() is False
    with pytest.raises(ValueError):
        meter.charge_generation("bonus")


def test_no_repair_is_offered_when_no_local_generation_is_left():
    """RM03, RM04: a repair is charged to both the repair and the local
    generation limits, so may_repair needs both. Without a local
    generation left, the loop stops with invalid_output instead of
    failing the repair with local_budget."""
    meter = BudgetMeter(LocalBudgets(local_generations=1), clock=FakeClock())
    meter.charge_generation("skill")
    assert meter.may_repair() is False


@pytest.mark.parametrize(
    ("first_stage", "second_stage", "expected"),
    [(0.0, 1.0, "active_seconds"), (3.5, 1.0, "run_active_seconds")],
)
def test_time_exhaustion_names_the_tighter_time_budget(first_stage, second_stage, expected):
    """RM04 / section 11: a timeout at the remaining time names the budget
    that bound it, the stage one or the entire-run one."""
    clock = FakeClock()
    meter = BudgetMeter(LocalBudgets(active_seconds=4.0, run_active_seconds=5.0), clock=clock)
    clock.advance(first_stage)
    meter.end_stage()
    meter.new_stage()
    clock.advance(second_stage)
    exceeded = meter.time_exhausted()
    assert isinstance(exceeded, BudgetExceededError)
    assert exceeded.budget == expected


def test_no_repair_is_offered_when_the_entire_run_has_no_generation_left():
    """RM03, section 11 (spec 0.6): a repair also counts toward the
    entire-run generation limit, so may_repair needs room there too."""
    meter = BudgetMeter(LocalBudgets(local_generations=3, run_generations=2), clock=FakeClock())
    meter.charge_generation("routing")
    meter.charge_generation("skill")
    assert meter.may_repair() is False
