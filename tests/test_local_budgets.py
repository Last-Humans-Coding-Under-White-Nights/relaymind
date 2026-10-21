"""Local execution budgets (RM03, section 11).

RM03: local execution is bounded. The defaults are one routing
generation, up to three further local generations, two executed tool
calls, one format-repair generation within those limits, and a
four-second active local budget that includes routing and tools. The
limits are configuration values and are recorded in each run. Section
11: router input is at most 2,048 local tokens and routing output at
most 128 tokens. Exceeding a budget is never silent continuation (RM04).
Issue #15.
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
    }


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
