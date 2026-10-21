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
run's TraceClock monotonic clock (RM25) and covers routing, model
generations, and tool execution.

Stages (spec 0.6 sections 5 and 11). The Local execution limits above
are per stage. A run's first stage starts at routing; when the run waits
for input the stage ends (end_stage), and a resume opens a new stage
(new_stage) with the full per-stage limits. The Entire run limits,
run_active_seconds, run_generations (routing and repair included), and
run_tool_executions, bound the sum of all stages. Waiting time counts
toward none of them. waiting_seconds is how long one wait may last
before the run fails with input_expired, and clarification_rounds is
how many clarifications a run may ask for; a further one fails the run
with missing_input (section 11).
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

# Entire-run limits (section 11) and what each one sums.
_RUN_LIMITS: Mapping[str, str] = {
    "generations": "run_generations",
    "tool_calls": "run_tool_executions",
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
    # Entire run (section 11): the sum of all local stages.
    run_active_seconds: float = 60.0
    run_generations: int = 8
    run_tool_executions: int = 6
    # Waiting state and clarification rounds (sections 5 and 11).
    waiting_seconds: float = 300.0
    clarification_rounds: int = 2

    def __post_init__(self) -> None:
        for name in ("routing_generations", "local_generations", "run_generations"):
            _check_limit(name, getattr(self, name), allow_zero=False, integer=True)
        for name in (
            "tool_calls",
            "repair_generations",
            "run_tool_executions",
            "clarification_rounds",
        ):
            _check_limit(name, getattr(self, name), allow_zero=True, integer=True)
        for name in ("routing_input_tokens", "routing_output_tokens"):
            _check_limit(name, getattr(self, name), allow_zero=False, integer=True)
        for name in ("active_seconds", "run_active_seconds", "waiting_seconds"):
            _check_limit(name, getattr(self, name), allow_zero=False, integer=False)
        if self.repair_generations > self.local_generations:
            raise ValueError("repair generations happen within the local generation limit")

    def to_json_object(self) -> dict[str, int | float]:
        """The limits as recorded in the run (RM03)."""
        data = asdict(self)
        for name in ("active_seconds", "run_active_seconds", "waiting_seconds"):
            data[name] = float(data[name])
        return data


_STAGE_COUNTERS = ("routing_generations", "local_generations", "repair_generations", "tool_calls")


class BudgetMeter:
    """Counts one run's local usage against its LocalBudgets.

    The meter starts with the run's first stage open. end_stage closes
    it when the run waits; new_stage opens the next one on resume.
    """

    def __init__(self, budgets: LocalBudgets, *, clock: TraceClock) -> None:
        self.budgets = budgets
        self._clock = clock
        self._stages = 1
        self._stage_started: float | None = clock.monotonic()
        self._closed_active = 0.0  # active seconds of the stages that ended
        self._stage = dict.fromkeys(_STAGE_COUNTERS, 0)
        self._total = dict.fromkeys(_STAGE_COUNTERS, 0)
        self._run = dict.fromkeys(_RUN_LIMITS, 0)

    # -- stages ------------------------------------------------------------

    def end_stage(self) -> None:
        """Close the current stage: the run waits, and waiting is not active."""
        if self._stage_started is not None:
            self._closed_active += self.elapsed_seconds()
            self._stage_started = None

    def new_stage(self) -> None:
        """Open a new stage with the full per-stage limits (section 11)."""
        self.end_stage()
        self._stages += 1
        self._stage_started = self._clock.monotonic()
        self._stage = dict.fromkeys(_STAGE_COUNTERS, 0)

    # -- time --------------------------------------------------------------

    def elapsed_seconds(self) -> float:
        """Active seconds in the current stage (0 while the run waits)."""
        if self._stage_started is None:
            return 0.0
        return self._clock.monotonic() - self._stage_started

    def active_seconds(self) -> float:
        """Active seconds over every stage of the run; waiting is excluded."""
        return self._closed_active + self.elapsed_seconds()

    def remaining_seconds(self) -> float:
        """The tighter of the stage and the entire-run time budgets."""
        return min(
            self.budgets.active_seconds - self.elapsed_seconds(),
            self.budgets.run_active_seconds - self.active_seconds(),
        )

    def time_exhausted(self) -> BudgetExceededError:
        """The stop for work that ran out of time: the tighter time budget."""
        stage_left = self.budgets.active_seconds - self.elapsed_seconds()
        run_left = self.budgets.run_active_seconds - self.active_seconds()
        if run_left < stage_left:
            return BudgetExceededError("run_active_seconds", self.budgets.run_active_seconds)
        return BudgetExceededError("active_seconds", self.budgets.active_seconds)

    def check_time(self) -> None:
        """Raise once the stage or the entire-run active time budget is spent."""
        if self.elapsed_seconds() > self.budgets.active_seconds:
            raise BudgetExceededError("active_seconds", self.budgets.active_seconds)
        if self.active_seconds() > self.budgets.run_active_seconds:
            raise BudgetExceededError("run_active_seconds", self.budgets.run_active_seconds)

    # -- counts ------------------------------------------------------------

    def _charge(self, names: tuple[str, ...], run_counter: str) -> None:
        self.check_time()
        for name in names:
            limit = getattr(self.budgets, name)
            if self._stage[name] + 1 > limit:
                raise BudgetExceededError(name, limit)
        run_limit_name = _RUN_LIMITS[run_counter]
        run_limit = getattr(self.budgets, run_limit_name)
        if self._run[run_counter] + 1 > run_limit:
            raise BudgetExceededError(run_limit_name, run_limit)
        for name in names:
            self._stage[name] += 1
            self._total[name] += 1
        self._run[run_counter] += 1

    def charge_generation(self, purpose: str) -> None:
        """Charge one generation by its purpose (GENERATION_CHARGES)."""
        try:
            names = GENERATION_CHARGES[purpose]
        except KeyError:
            raise ValueError(f"unknown generation purpose {purpose!r}") from None
        self._charge(names, "generations")

    def charge_tool_call(self) -> None:
        """One executed tool call; replays are not executions (RM03)."""
        self._charge(("tool_calls",), "tool_calls")

    def may_repair(self) -> bool:
        """Whether a format-repair generation is still within this stage's limits.

        A repair is charged to both the repair and the local generation
        limits of the stage (GENERATION_CHARGES) and to the entire-run
        generation limit, so all of them must have room (RM03).
        """
        return (
            all(
                self._stage[counter] < getattr(self.budgets, counter)
                for counter in GENERATION_CHARGES["repair"]
            )
            and self._run["generations"] < self.budgets.run_generations
        )

    def usage(self) -> dict[str, int]:
        """What the run used over all stages, as recorded in its trace."""
        return {
            **self._total,
            "active_ms": round(self.active_seconds() * 1000),
            "stages": self._stages,
        }
