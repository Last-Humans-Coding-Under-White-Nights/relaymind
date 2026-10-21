"""Local execution budgets (RM03, RM04, section 11).

LocalBudgets holds the configured limits; the orchestrator records them
in each run. Every limit is a finite number: NaN or infinity would turn
a limit into no limit (RM03). BudgetMeter counts what a run used and
refuses a charge that would go over a limit, so exceeding a budget is
always an explicit BudgetExceededError (reason code local_budget) and
never silent continuation (RM04). A refused charge is not counted.

Generation counts follow RM03: one routing generation, then up to
local_generations further generations. A format-repair generation is
one of those further generations, never extra, and at most
repair_generations of them happen. Every generation is charged by its
purpose through charge_generation. Active time is measured with the
run's TraceClock monotonic clock (RM25) from the start of the meter and
covers routing, model generations, and tool execution.
"""

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from relaymind.store import TraceClock

LOCAL_BUDGET_REASON = "local_budget"

# What each generation purpose is charged against (RM03).
GENERATION_CHARGES: Mapping[str, tuple[str, ...]] = {
    "routing": ("routing_generations",),
    "skill": ("local_generations",),
    "repair": ("repair_generations", "local_generations"),
    "final": ("local_generations",),
}


class BudgetExceededError(Exception):
    """A local budget would be exceeded (RM04: escalate or needs_external)."""

    reason_code = LOCAL_BUDGET_REASON

    def __init__(self, budget: str, limit: int | float) -> None:
        super().__init__(f"local budget {budget} ({limit}) is exhausted")
        self.budget = budget
        self.limit = limit


def _check_limit(name: str, value: object, *, allow_zero: bool, integer: bool) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"budget {name} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"budget {name} must be finite")
    if integer and not isinstance(value, int):
        raise ValueError(f"budget {name} must be an integer")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"budget {name} must be {'non-negative' if allow_zero else 'positive'}")


@dataclass(frozen=True)
class LocalBudgets:
    """Configured local limits (RM03 and section 11 defaults)."""

    routing_generations: int = 1
    local_generations: int = 3
    tool_calls: int = 2
    repair_generations: int = 1
    active_seconds: float = 4.0
    routing_input_tokens: int = 2048
    routing_output_tokens: int = 128

    def __post_init__(self) -> None:
        for name in ("routing_generations", "local_generations"):
            _check_limit(name, getattr(self, name), allow_zero=False, integer=True)
        for name in ("tool_calls", "repair_generations"):
            _check_limit(name, getattr(self, name), allow_zero=True, integer=True)
        for name in ("routing_input_tokens", "routing_output_tokens"):
            _check_limit(name, getattr(self, name), allow_zero=False, integer=True)
        _check_limit("active_seconds", self.active_seconds, allow_zero=False, integer=False)
        if self.repair_generations > self.local_generations:
            raise ValueError("repair generations happen within the local generation limit")

    def to_json_object(self) -> dict[str, int | float]:
        """The limits as recorded in the run (RM03)."""
        data = asdict(self)
        data["active_seconds"] = float(self.active_seconds)
        return data


class BudgetMeter:
    """Counts one run's local usage against its LocalBudgets."""

    def __init__(self, budgets: LocalBudgets, *, clock: TraceClock) -> None:
        self.budgets = budgets
        self._clock = clock
        self._started = clock.monotonic()
        self._counts = {
            "routing_generations": 0,
            "local_generations": 0,
            "repair_generations": 0,
            "tool_calls": 0,
        }

    def elapsed_seconds(self) -> float:
        return self._clock.monotonic() - self._started

    def remaining_seconds(self) -> float:
        return self.budgets.active_seconds - self.elapsed_seconds()

    def check_time(self) -> None:
        """Raise once the active local time budget is spent."""
        if self.elapsed_seconds() > self.budgets.active_seconds:
            raise BudgetExceededError("active_seconds", self.budgets.active_seconds)

    def _charge(self, *names: str) -> None:
        self.check_time()
        for name in names:
            limit = getattr(self.budgets, name)
            if self._counts[name] + 1 > limit:
                raise BudgetExceededError(name, limit)
        for name in names:
            self._counts[name] += 1

    def charge_generation(self, purpose: str) -> None:
        """Charge one generation by its purpose (GENERATION_CHARGES)."""
        try:
            names = GENERATION_CHARGES[purpose]
        except KeyError:
            raise ValueError(f"unknown generation purpose {purpose!r}") from None
        self._charge(*names)

    def charge_tool_call(self) -> None:
        """One executed tool call; replays are not executions (RM03)."""
        self._charge("tool_calls")

    def may_repair(self) -> bool:
        """Whether a format-repair generation is still within its limits.

        A repair is charged to both the repair and the local generation
        limits (GENERATION_CHARGES), so both must have room (RM03).
        """
        return all(
            self._counts[counter] < getattr(self.budgets, counter)
            for counter in GENERATION_CHARGES["repair"]
        )

    def usage(self) -> dict[str, int]:
        """What the run used, as recorded in its trace."""
        return {**self._counts, "active_ms": round(self.elapsed_seconds() * 1000)}
