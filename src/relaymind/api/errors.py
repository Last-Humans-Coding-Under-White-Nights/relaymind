"""The error contract of the public API (spec section 11; RM04, RM17, AC07, AC16).

Every error response has the body ``{"error": {"code", "message",
"run_id"}}``, plus ``reason_code`` when the failure has an RM04 reason
code. The code is stable and the status follows from it, through the
one table ``STATUS_BY_CODE``. This module holds the contract without a
web framework: the HTTP layer turns an ``ApiError`` into a response.

``error_from_exception`` maps what the orchestrator and the store
raise. ``error_from_outcome`` maps a run that ended failed. Internal
errors get a fixed message: exception text, credentials, and secret
values never reach a response (RM12).
"""

from relaymind.orchestrator.local_loop import (
    APPROVAL_EXPIRED,
    CONTEXT_LIMIT_EXCEEDED,
    EXTERNAL_FAILED,
    INPUT_EXPIRED,
    INTERNAL_ERROR,
    MISSING_INPUT,
    NEEDS_EXTERNAL,
    ApprovalRejectedError,
    ResumeInputError,
    RunOutcome,
    aborted_run_id,
)
from relaymind.orchestrator.recovery import INTERRUPTED
from relaymind.orchestrator.run_state import (
    InvalidTransitionError,
    RunState,
    TerminalStateError,
)
from relaymind.store import RunNotFoundError, SlotUnavailableError, StaleVersionError

__all__ = ["STATUS_BY_CODE", "ApiError", "error_from_exception", "error_from_outcome"]

STATUS_BY_CODE: dict[str, int] = {
    "invalid_request": 400,
    "unsupported_parameter": 400,
    "invalid_execution_mode": 400,
    "invalid_resume_input": 400,
    "context_limit_exceeded": 400,
    "unauthenticated": 401,
    "execution_mode_forbidden": 403,
    "approval_invalid": 403,
    "run_not_found": 404,
    "stale_version": 409,
    "run_terminal": 409,
    "invalid_state": 409,
    "idempotency_conflict": 409,
    "needs_external": 422,
    "overloaded": 429,
    "internal_error": 500,
    "workflow_unavailable": 503,
    "external_failed": 503,
    "deadline_exceeded": 504,
}

# Time limits: a run stopped by one of these is a deadline, not an outage.
DEADLINE_BUDGETS = frozenset({"run_active_seconds", "external_seconds"})
# Failed runs that are run state, not an error of the request (section 11).
NOT_AN_ERROR = frozenset({MISSING_INPUT, INPUT_EXPIRED, APPROVAL_EXPIRED, INTERRUPTED})
INTERNAL_MESSAGE = "an internal error occurred"
WORKFLOW_UNAVAILABLE = "workflow_unavailable"


class ApiError(Exception):
    """One documented error response."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        run_id: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        if code not in STATUS_BY_CODE:
            raise ValueError(f"{code!r} is not a documented error code (spec section 11)")
        super().__init__(message)
        self.code = code
        self.status = STATUS_BY_CODE[code]
        self.message = message
        self.run_id = run_id
        self.reason_code = reason_code

    def body(self) -> dict[str, dict[str, str | None]]:
        error: dict[str, str | None] = {
            "code": self.code,
            "message": self.message,
            "run_id": self.run_id,
        }
        if self.reason_code is not None:
            error["reason_code"] = self.reason_code
        return {"error": error}


# Most specific first: TerminalStateError is an InvalidTransitionError.
# Each exception also declares its http_status (and code, when it has
# one); a test keeps them equal to this mapping.
_KNOWN: tuple[tuple[type[Exception], str], ...] = (
    (ResumeInputError, "invalid_resume_input"),
    (ApprovalRejectedError, "approval_invalid"),
    (RunNotFoundError, "run_not_found"),
    (StaleVersionError, "stale_version"),
    (TerminalStateError, "run_terminal"),
    (InvalidTransitionError, "invalid_state"),
    (SlotUnavailableError, "overloaded"),
)


def error_from_exception(exc: BaseException, *, run_id: str | None = None) -> ApiError:
    """The documented error for an exception; anything unknown is internal_error.

    An exception raised by a run's own work is internal_error whatever
    its type: the run already ended as failed with internal_error, and
    the request was not at fault (sections 5, 11).
    """
    if isinstance(exc, ApiError):
        return exc
    aborted = aborted_run_id(exc)
    if aborted is not None:
        return ApiError("internal_error", INTERNAL_MESSAGE, run_id=aborted)
    for kind, code in _KNOWN:
        if isinstance(exc, kind):
            return ApiError(code, str(exc), run_id=getattr(exc, "run_id", None) or run_id)
    return ApiError("internal_error", INTERNAL_MESSAGE, run_id=run_id)


_OUTCOME_MESSAGES = {
    NEEDS_EXTERNAL: "the task cannot complete under the effective execution mode",
    CONTEXT_LIMIT_EXCEEDED: "the evidence does not fit the external context limit",
    EXTERNAL_FAILED: "the external model could not finish the run",
    WORKFLOW_UNAVAILABLE: "the configured workflow is unavailable",
}


def error_from_outcome(outcome: RunOutcome) -> ApiError | None:
    """The error response of a run that ended failed, or None when it is not one.

    A run that completed, waits, was cancelled, or failed with an error
    code that section 11 reports as run state is not an error response.
    An external_failed run stopped by a time limit is a deadline (504).
    """
    if outcome.state is not RunState.FAILED or outcome.error_code in NOT_AN_ERROR:
        return None
    code = outcome.error_code
    if code == INTERNAL_ERROR:
        return ApiError("internal_error", INTERNAL_MESSAGE, run_id=outcome.run_id)
    if code == EXTERNAL_FAILED and outcome.budget in DEADLINE_BUDGETS:
        return ApiError(
            "deadline_exceeded",
            f"the time limit {outcome.budget} was exhausted before an answer",
            run_id=outcome.run_id,
            reason_code=outcome.reason_code,
        )
    if code not in _OUTCOME_MESSAGES:
        raise ValueError(f"run {outcome.run_id} failed with undocumented error code {code!r}")
    return ApiError(
        code, _OUTCOME_MESSAGES[code], run_id=outcome.run_id, reason_code=outcome.reason_code
    )
