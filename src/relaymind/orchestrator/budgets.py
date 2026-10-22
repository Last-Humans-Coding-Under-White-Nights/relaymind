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

The external stage (RM13, section 11). After a handoff, start_external
switches the meter to the external phase: the per-stage local limits no
longer apply, an external generation is charged to the entire-run
generation limit and to external_generations, an executed tool to the
entire-run tool limit, and time is bounded by external_seconds for each
generation and by the entire-run active time. external_room says
before a handoff whether any of that room is left, so a run that cannot
be dispatched ends as needs_external instead of failing mid-handoff.
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
    # Only the entire-run limit and external_generations (section 11).
    "external": (),
    # The checklist draft of the parallel workflow (spec 0.25, RM15): an
    # external generation made before the handoff.
    "draft": (),
}
# Purposes that run on the external model.
EXTERNAL_PURPOSES = ("external", "draft")

# The time limits, kept as floats in the recorded budgets.
_SECONDS_LIMITS = ("active_seconds", "run_active_seconds", "waiting_seconds", "external_seconds")

# Entire-run limits (section 11) and what each one sums.
_RUN_LIMITS: Mapping[str, str] = {
    "generations": "run_generations",
    "tool_calls": "run_tool_executions",
}


class BudgetExceededError(Exception):
    """A budget would be exceeded (RM03).

    Before a handoff RM04 escalates or returns needs_external; in the
    external stage the run fails with external_failed (section 11). The
    message names the limit and never calls an external limit local (RM16).
    """

    reason_code = LOCAL_BUDGET_REASON

    def __init__(self, budget: str, limit: int | float) -> None:
        super().__init__(f"budget {budget} ({limit}) is exhausted")
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
    # External stage (section 11): generations after a handoff, and the
    # time one external generation may take.
    external_generations: int = 6
    external_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in (
            "routing_generations",
            "local_generations",
            "run_generations",
            "external_generations",
        ):
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
        for name in _SECONDS_LIMITS:
            _check_limit(name, getattr(self, name), allow_zero=False, integer=False)
        if self.repair_generations > self.local_generations:
            raise ValueError("repair generations happen within the local generation limit")

    def to_json_object(self) -> dict[str, int | float]:
        """The limits as recorded in the run (RM03)."""
        data = asdict(self)
        for name in _SECONDS_LIMITS:
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
        self._external = False
        self._external_generations = 0
        # The time limit that bounded the last timed work (remaining_seconds),
        # kept for local and for external work apart: the parallel workflow
        # times a local and an external generation at once (spec 0.25).
        self._bounds: dict[bool, str | None] = {False: None, True: None}

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
        self._bounds = {False: None, True: None}

    # -- the external stage ---------------------------------------------------

    def start_external(self) -> None:
        """Switch to the external phase after a handoff (section 11)."""
        self._external = True
        self._bounds = {False: None, True: None}

    def external_room(self) -> BudgetExceededError | None:
        """The limit that stops an external stage from starting, or None.

        An external stage needs one more generation and some active time
        inside the entire-run limits.
        """
        if self._run["generations"] + 1 > self.budgets.run_generations:
            return BudgetExceededError("run_generations", self.budgets.run_generations)
        if self.active_seconds() >= self.budgets.run_active_seconds:
            return BudgetExceededError("run_active_seconds", self.budgets.run_active_seconds)
        return None

    def remaining_budgets(self) -> dict[str, int | float]:
        """What the external stage has left, as the handoff preserves it (RM13)."""
        return {
            "run_generations": max(self.budgets.run_generations - self._run["generations"], 0),
            "run_tool_executions": max(
                self.budgets.run_tool_executions - self._run["tool_calls"], 0
            ),
            "run_active_seconds": max(
                round(self.budgets.run_active_seconds - self.active_seconds(), 6), 0.0
            ),
            "external_generations": max(
                self.budgets.external_generations - self._external_generations, 0
            ),
            "external_seconds": float(self.budgets.external_seconds),
        }

    # -- time --------------------------------------------------------------

    def elapsed_seconds(self) -> float:
        """Active seconds in the current stage (0 while the run waits)."""
        if self._stage_started is None:
            return 0.0
        return self._clock.monotonic() - self._stage_started

    def active_seconds(self) -> float:
        """Active seconds over every stage of the run; waiting is excluded."""
        return self._closed_active + self.elapsed_seconds()

    def _time_bound(self, external: bool = False) -> tuple[float, str]:
        """The seconds left under the tighter time limit, and that limit's name."""
        run_left = self.budgets.run_active_seconds - self.active_seconds()
        if self._external or external:
            name, left = "external_seconds", float(self.budgets.external_seconds)
        else:
            name, left = "active_seconds", self.budgets.active_seconds - self.elapsed_seconds()
        if run_left < left:
            return run_left, "run_active_seconds"
        return left, name

    def remaining_seconds(self, external: bool = False) -> float:
        """The tighter of the stage and the entire-run time budgets.

        In the external phase: external_seconds, or the entire-run time
        left if that is less. The meter notes which limit is the bound,
        so time_exhausted names it even after the time has passed: an
        external generation's bound is fixed, while the run's time left
        keeps shrinking (RM03, RM16).
        """
        seconds, self._bounds[external or self._external] = self._time_bound(external)
        return seconds

    def time_exhausted(self, external: bool = False) -> BudgetExceededError:
        """The stop for work that ran out of time: the limit that bounded it."""
        key = external or self._external
        name = self._bounds[key] if self._bounds[key] is not None else self._time_bound(key)[1]
        return BudgetExceededError(name, getattr(self.budgets, name))

    def check_time(self, external: bool = False) -> None:
        """Raise once the stage or the entire-run active time budget is spent.

        external is work on the external model before the handoff, such as
        the draft of the parallel workflow: the per-stage local time limit
        does not bound it.
        """
        if (
            not (self._external or external)
            and self.elapsed_seconds() > self.budgets.active_seconds
        ):
            raise BudgetExceededError("active_seconds", self.budgets.active_seconds)
        if self.active_seconds() > self.budgets.run_active_seconds:
            raise BudgetExceededError("run_active_seconds", self.budgets.run_active_seconds)

    # -- counts ------------------------------------------------------------

    def _charge(self, names: tuple[str, ...], run_counter: str, external: bool = False) -> None:
        self.check_time(external)
        # The per-stage limits do not apply to the external stage, but
        # what it uses still counts in the run's usage (RM03, RM16).
        checked = () if self._external or external else names
        for name in checked:
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
        external = purpose in EXTERNAL_PURPOSES
        if external and self._external_generations + 1 > self.budgets.external_generations:
            raise BudgetExceededError("external_generations", self.budgets.external_generations)
        self._charge(names, "generations", external)
        if external:
            self._external_generations += 1

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
            "external_generations": self._external_generations,
            "active_ms": round(self.active_seconds() * 1000),
            "stages": self._stages,
        }
