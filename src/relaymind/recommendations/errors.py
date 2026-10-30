"""The recommendation error taxonomy (spec 11.1; RM36).

Shared codes keep their section 11 status from api/errors.py. The three
recommendation-only codes are listed here, because the section 11 table that
STATUS_BY_CODE mirrors does not include them.
"""

from relaymind.api.errors import INTERNAL_MESSAGE, STATUS_BY_CODE

MESSAGES = {
    "unauthenticated": "a valid application token is required",
    "invalid_request": "the recommendation request is invalid",
    "recommendation_not_found": "the recommendation was not found",
    "stale_version": "the recommendation version changed",
    "invalid_state": "the recommendation state does not permit this operation",
    "idempotency_conflict": "the idempotency key was used with a different request",
    "recommendation_in_progress": "the recommendation is still computing",
    "overloaded": "the recommendation service is busy",
    "recommendation_unavailable": "the recommendation service is unavailable",
    "deadline_exceeded": "the recommendation deadline was exceeded",
    "internal_error": INTERNAL_MESSAGE,
}
_OWN_STATUSES = {
    "recommendation_not_found": 404,
    "recommendation_in_progress": 409,
    "recommendation_unavailable": 503,
}
STATUSES = {code: _OWN_STATUSES.get(code) or STATUS_BY_CODE[code] for code in MESSAGES}


class RecommendationError(Exception):
    def __init__(self, code: str, recommendation_id: str | None = None):
        super().__init__(code)
        self.code, self.recommendation_id = code, recommendation_id

    @property
    def status(self) -> int:
        return STATUSES[self.code]

    def inner(self) -> dict:
        return dict(
            code=self.code,
            message=MESSAGES[self.code],
            run_id=None,
            recommendation_id=self.recommendation_id,
        )
