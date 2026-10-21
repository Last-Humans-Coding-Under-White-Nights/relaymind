"""Idempotency keys for execution-creating requests (RM17, RM21, AC11, spec 0.18 section 7.4).

Two POSTs create work: POST /relay/v1/runs and POST /v1/chat/completions.
A key is stored with the run it creates (``RunStore.create_run``), so
this module only reads it: it checks the header, hashes the request,
and decides whether a stored key is a conflict or a duplicate. A
duplicate never reaches the orchestrator, so it makes no model call and
runs no tool; a completed side effect is never repeated.

``stored_chat_result`` rebuilds the chat response of a finished run from
its events, so a replay says what the run recorded and nothing a model
is asked for again.
"""

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from starlette.datastructures import Headers

from relaymind.adapters import ToolCall
from relaymind.api.chat import TOOL_CALLS, ChatResult, answer_pieces
from relaymind.api.errors import ApiError
from relaymind.canonical import canonical_json
from relaymind.orchestrator.run_state import TERMINAL_STATES, RunState
from relaymind.redaction import REDACTED
from relaymind.store import EventType, IdempotencyClaim, IdempotencyRecord, RunStore

__all__ = [
    "APPLICATION_CALLER",
    "IDEMPOTENCY_HEADER",
    "MAX_KEY_LENGTH",
    "REPLAYED_HEADER",
    "check_stored_key",
    "claim_for",
    "idempotency_key",
    "request_hash",
    "stored_chat_result",
]

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
MAX_KEY_LENGTH = 255
# The prototype authenticates one caller, the holder of the application
# token (section 3). The token changes at every start; the caller, and
# so its keys, does not.
APPLICATION_CALLER = "application"


def idempotency_key(headers: Headers) -> str | None:
    """The request's key, None when absent; a malformed key is 400 invalid_request."""
    values = headers.getlist(IDEMPOTENCY_HEADER)
    if not values:
        return None
    if len(values) > 1:
        raise ApiError("invalid_request", f"{IDEMPOTENCY_HEADER} must be sent once")
    key = values[0]
    if not 1 <= len(key) <= MAX_KEY_LENGTH or not (key.isascii() and key.isprintable()):
        raise ApiError(
            "invalid_request",
            f"{IDEMPOTENCY_HEADER} must be 1 to {MAX_KEY_LENGTH} printable ASCII characters",
        )
    return key


def request_hash(endpoint: str, body: object, mode_header: str | None = None) -> str:
    """The SHA-256 of the canonical JSON of the endpoint, the decoded body, and the mode header."""
    try:
        text = canonical_json({"endpoint": endpoint, "body": body, "mode_header": mode_header})
    except ValueError:
        raise ApiError("invalid_request", "the request body must be valid JSON") from None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def claim_for(key: str | None, digest: str) -> IdempotencyClaim | None:
    return None if key is None else IdempotencyClaim(APPLICATION_CALLER, key, digest)


def check_stored_key(store: RunStore, claim: IdempotencyClaim | None) -> IdempotencyRecord | None:
    """The stored record of a claim when it is a duplicate; 409 when the payload changed."""
    if claim is None:
        return None
    stored = store.find_idempotency_key(claim.caller, claim.key)
    if stored is None:
        return None
    if stored.request_hash != claim.request_hash:
        raise ApiError(
            "idempotency_conflict",
            f"{IDEMPOTENCY_HEADER} was used with a different request; send a new key",
        )
    return stored


def _redacted(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_redacted(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, str):
        return any(_redacted(item) for item in value)
    return isinstance(value, str) and REDACTED in value


def stored_chat_result(
    store: RunStore, run_id: str, *, stream: bool
) -> tuple[ChatResult, datetime]:
    """The chat response a finished run recorded, and when the run ended (spec 0.18 section 7.4).

    A run that has not ended is 409 idempotency_conflict, and one that
    ended failed or cancelled is 409 run_terminal: a retry after a
    failure is a new attempt under a new key (RM17). Client-owned
    calls come from tool.proposed; arguments that were redacted would
    differ from the first response, so they are refused (RM12).
    """
    record = store.get_run(run_id)
    state = RunState(record.state)
    if state not in TERMINAL_STATES:
        raise ApiError("idempotency_conflict", f"run {run_id} has not finished", run_id=run_id)
    if state is not RunState.COMPLETED:
        raise ApiError(
            "run_terminal",
            f"run {run_id} ended {state.value}; send a new {IDEMPOTENCY_HEADER} to try again",
            run_id=run_id,
        )
    proposed = [
        e.payload
        for e in store.events(run_id, event_type=EventType.TOOL_PROPOSED)
        if e.payload.get("owner") == "client"
    ]
    if proposed:
        if any(_redacted(p["arguments"]) for p in proposed):
            raise ApiError(
                "idempotency_conflict",
                f"the stored tool calls of run {run_id} were redacted and cannot be replayed",
                run_id=run_id,
            )
        calls = tuple(ToolCall(p["proposal_id"], p["tool_name"], p["arguments"]) for p in proposed)
        return ChatResult(run_id, None, TOOL_CALLS, tool_calls=calls), record.updated_at
    completed = store.events(run_id, event_type=EventType.RUN_COMPLETED)[-1].payload
    answer: Any = completed.get("answer")
    if not isinstance(answer, str):
        raise ApiError("internal_error", "an internal error occurred", run_id=run_id)
    pieces = answer_pieces(store, run_id, answer) if stream else ()
    return ChatResult(run_id, answer, pieces=pieces), record.updated_at
