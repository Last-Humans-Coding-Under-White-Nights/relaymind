"""One pure terminal outcome definition for live and imported evidence (RM27)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Spec 0.48 section 14.4.7: the limits whose stop means the local stage ran out of time.
TIME_BUDGETS = frozenset({"active_seconds", "run_active_seconds"})
# A failure that says nothing about the device records no stage outcome: these
# error codes, and the reason code capability_missing (RM04), which a failed
# local model gives with needs_external or with a degraded-mode handoff.
_NO_STAGE_EVIDENCE = frozenset({"interrupted", "internal_error"})


@dataclass(frozen=True)
class StageOutcome:
    """One local stage of a run: did it run out of local time (RM26, spec 0.48)?"""

    run_id: str
    ran_out_of_time: bool
    ended_at: datetime


def project_stage_outcome(
    progress: Mapping[str, Any], kind: str, payload: Mapping[str, Any]
) -> bool | None:
    """Spec 0.48 section 14.4.7: True ran out of local time, False fit, None no evidence.

    Called with the event that ends the local stage: handoff.started, or a
    terminal event of a run that never handed off. Only a run that made a
    local generation of its own, in local_only or auto, has a local stage.
    """
    if not progress["local_generation"] or progress["execution_mode"] not in (
        "auto",
        "local_only",
    ):
        return None
    if kind == "run.cancelled" or payload.get("local_active_ms") is None:
        return None
    if kind == "run.failed" and payload.get("error_code") in _NO_STAGE_EVIDENCE:
        return None
    if payload.get("reason_code") == "capability_missing":
        return None
    return payload.get("reason_code") == "local_slow" or payload.get("budget") in TIME_BUDGETS


def is_local_attempt(payload: Mapping[str, Any]) -> bool:
    """RM15/RM27: a parallel branch is not a sequential local placement attempt."""
    return payload.get("purpose") not in (None, "routing") and "branch" not in payload


def project_outcome(
    progress: Mapping[str, Any], kind: str, payload: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Project bounded observed progress, never grader success or selector claims."""
    if progress["skill_id"] is None:
        return None
    completed = kind == "run.completed"
    return {
        "local_model": progress["local_model"],
        "skill_id": progress["skill_id"],
        "local_completed": int(
            bool(
                completed
                and progress["local_attempt"]
                and not progress["handoff"]
                and not progress["external_generations"]
                and payload.get("final_answer_model") == progress["local_model"]
            )
        ),
        "local_escalated": int(
            bool(
                progress["local_attempt"]
                and (
                    progress["handoff"]
                    or (
                        kind == "run.failed"
                        and payload.get("error_code") == "needs_external"
                        and progress["execution_mode"] == "auto"
                    )
                )
            )
        ),
        "external_model": progress["external_model"],
        "external_generations": progress["external_generations"] if completed else 0,
    }
