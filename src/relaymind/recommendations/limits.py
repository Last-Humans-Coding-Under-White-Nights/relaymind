"""Fixed recommendation limits (spec 14.5; RM03, RM36).

Each limit is defined once. The API, service and store enforce these values,
and recommendation_budgets() records the same values in the accepted event.
"""

from relaymind.assets.catalog import MAX_OFFER_BYTES, MAX_OFFER_RECORDS
from relaymind.assets.settings import RecommendationSettings
from relaymind.selection.asset_http import MAX_RESPONSE_BYTES

CANDIDATE_LIMIT = 10
DISPLAY_LIMIT = 3
MAX_CREATE_BYTES = 512 * 1024
MAX_TASK_BYTES = 8192
MAX_CONVERSATION_ENTRIES = 32
MAX_CONVERSATION_BYTES = 32768
CHOICE_SECONDS = 600
RETENTION_DAYS = 30
MAX_EVENTS_PAGE = 1000
DEFAULT_EVENTS_PAGE = 100

_DEFAULTS = RecommendationSettings()


def recommendation_budgets(
    total_ms: int = _DEFAULTS.total_ms,
    primary_ms: int = _DEFAULTS.primary_ms,
    fallback: bool = _DEFAULTS.fallback,
) -> dict:
    return dict(
        total_ms=total_ms,
        primary_ms=primary_ms,
        fallback_enabled=fallback,
        candidate_limit=CANDIDATE_LIMIT,
        display_limit=DISPLAY_LIMIT,
        max_catalog_records=MAX_OFFER_RECORDS,
        max_catalog_bytes=MAX_OFFER_BYTES,
        max_result_bytes=MAX_RESPONSE_BYTES,
        max_create_bytes=MAX_CREATE_BYTES,
        max_task_bytes=MAX_TASK_BYTES,
        max_conversation_entries=MAX_CONVERSATION_ENTRIES,
        max_conversation_bytes=MAX_CONVERSATION_BYTES,
        choice_seconds=CHOICE_SECONDS,
        retention_days=RETENTION_DAYS,
    )
