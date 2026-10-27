"""The class of the local model: ready, unavailable, unsuitable, or absent (RM26).

Spec 0.31 RM26 and sections 7.4, 8, 11, and 14.1. The gateway runs
without a usable local model in three classes:

- absent: the launch file has no local_model table;
- unavailable: the configured model cannot run on this machine (the
  runtime does not answer, or the model did not load in time);
- unsuitable: it runs, but the routing-shaped and skill-shaped warm-ups
  did not fit the local per-stage active budget together, or (spec 0.48)
  its latest local stages kept running out of local time.

The class is a measurement, never a permanent label: startup detection
sets it, and readiness repeats the availability check (section 7.4).
In every class but ready, auto is managed external execution. local_only
and parallel_demo are not permitted while the model is absent or
unavailable; while it is unsuitable local_only stays permitted (the
operator asked for it) and parallel_demo does not. None of this is
degraded mode, which stays for a usable model that failed mid-run
(section 11).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.store.outcomes import StageOutcome

__all__ = [
    "ABSENT_REASON",
    "STAGE_STREAK",
    "LocalModelClass",
    "LocalStatus",
    "stage_status",
]

# The cause recorded for a gateway whose launch file has no local_model table.
ABSENT_REASON = "no local model is configured"

# Spec 0.48 section 14.4.7: this many latest local stages that all ran out of local
# time make a ready model unsuitable.
STAGE_STREAK = 3


class LocalModelClass(StrEnum):
    """The class of the local model (section 8, local_model_status)."""

    READY = "ready"
    UNAVAILABLE = "unavailable"
    UNSUITABLE = "unsuitable"
    ABSENT = "absent"


# The modes each class does not permit (section 7.4); a request for one is 403.
_FORBIDDEN: dict[LocalModelClass, frozenset[ExecutionMode]] = {
    LocalModelClass.READY: frozenset(),
    LocalModelClass.UNSUITABLE: frozenset({ExecutionMode.PARALLEL_DEMO}),
    LocalModelClass.UNAVAILABLE: frozenset({ExecutionMode.LOCAL_ONLY, ExecutionMode.PARALLEL_DEMO}),
    LocalModelClass.ABSENT: frozenset({ExecutionMode.LOCAL_ONLY, ExecutionMode.PARALLEL_DEMO}),
}


@dataclass(frozen=True)
class LocalStatus:
    """The class and its measured cause; the cause is None only for a ready model."""

    status: LocalModelClass
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", LocalModelClass(self.status))
        if self.reason is not None and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise ValueError("reason must be None or non-blank text")
        if self.status != LocalModelClass.READY and self.reason is None:
            raise ValueError(f"a local model that is {self.status} needs a cause")

    @classmethod
    def ready(cls) -> "LocalStatus":
        return cls(LocalModelClass.READY)

    @classmethod
    def absent(cls) -> "LocalStatus":
        return cls(LocalModelClass.ABSENT, ABSENT_REASON)

    @property
    def usable(self) -> bool:
        """Whether auto uses the local model (RM01); otherwise it is managed external execution."""
        return self.status == LocalModelClass.READY

    def forbids(self, mode: ExecutionMode) -> bool:
        """Whether this class does not permit the mode (section 7.4)."""
        return ExecutionMode(mode) in _FORBIDDEN[self.status]

    def refusal(self, mode: ExecutionMode) -> str | None:
        """The 403 message for a mode this class does not permit, or None."""
        if not self.forbids(mode):
            return None
        return (
            f"the execution mode {ExecutionMode(mode).value} is not permitted while the local"
            f" model is {self.status.value} ({self.reason})"
        )

    def line(self) -> str:
        """The one line the launcher prints for a class other than ready (RM26)."""
        if self.usable:
            raise ValueError("a ready local model has no launcher class line")
        return (
            f"Local model: {self.status.value} ({self.reason});"
            " auto runs as managed external execution."
        )

    def to_json_object(self, model_id: str | None) -> dict[str, Any]:
        """The run.accepted fields (section 8): model_id is null only when absent."""
        return {
            "local_model": model_id,
            "local_model_status": self.status.value,
            "local_model_reason": self.reason,
        }


def stage_status(
    outcomes: Sequence[StageOutcome],
    *,
    now: datetime,
    probe_after_seconds: float,
    active_seconds: float,
) -> LocalStatus | None:
    """The class the latest local stages measured, or None when they say nothing (RM26).

    outcomes are the model's latest stage outcomes, newest first (spec 0.48
    section 14.4.7). Unsuitable needs STAGE_STREAK of them that all ran out of
    local time, the latest ended less than probe_after_seconds ago; a stale
    streak expires, so the next local stage measures the model again.
    """
    latest = list(outcomes[:STAGE_STREAK])
    if len(latest) < STAGE_STREAK or not all(o.ran_out_of_time for o in latest):
        return None
    if (now - latest[0].ended_at).total_seconds() >= probe_after_seconds:
        return None
    return LocalStatus(
        LocalModelClass.UNSUITABLE,
        f"the last {STAGE_STREAK} local stages ran out of local time"
        f" (active_seconds {active_seconds:g} s); the latest ended at"
        f" {latest[0].ended_at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
    )
