"""One pure terminal outcome definition for live and imported evidence (RM27)."""

from collections.abc import Mapping
from typing import Any


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
