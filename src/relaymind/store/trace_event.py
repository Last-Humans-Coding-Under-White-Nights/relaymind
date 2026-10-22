"""TraceEvent: one ordered event in a run trace (spec section 8; RM22).

The JSON Schema is published next to this module as
``schemas/trace_event.v1.json``; the validator below enforces the same
contract, and the run store calls it before every write, so an event
that does not match the schema never reaches SQLite.

Payload rules per event type come from RM22 and RM16: every tool event
names its operation or proposal ID and its originating model, model
events name the call and the model, run.completed names the
final-answer model, run.failed keeps an error code (section 5) and, when restart recovery
ends the run, the interrupted state and the settled operations,
tool.finished from recovery is recovered and never replayed,
clarification.required names its question, language, reason code,
missing input, the clarify decision's skill_id (non-blank, or null when
no skill was chosen), and the positive waiting_seconds limit in force
(section 11), clarification.answered names the redacted resume input (non-blank),
the missing input it answers, and the non-negative integer waited_ms
from the monotonic clock (spec 0.6 section 8; RM12, RM25), run.completed
and run.failed give model_calls as an array of identified calls and
external_calls, active_ms, and waiting_ms as non-negative
integers (section 8, 12.1), handoff.started names the reason code, the
external model, the selected skill (or null), the completed operation
IDs, the allowed tools, and the measured external request (RM13),
confirmation.required is exactly the approval, tool, proposal,
canonical redacted arguments, target, bound state_version, UTC
expires_at, and proposing model, never the token or its hash (RM11),
run.cancelled names its reason, user_denied for a denied confirmation
(with the approval_id; RM11) or cancel_requested for any other cancel
(with the approval_id of an approval it voided),
cache.hit names the originating version,
and skill.loaded names the skill, its version, and its content hash
(RM06). Timestamps are UTC; elapsed_ms
comes from a monotonic clock (RM25).

run.accepted is the first event of a run (RM22): it has seq 1, and seq
1 is always run.accepted. Its payload is the section 8 run.accepted
payload: the requested execution mode (or null), the effective mode,
the model_alias (relaymind or relaymind-client-tools for a chat run,
null for a native run; spec 0.6 section 8), the budgets in force
(RM03), the skill roots in order, the skills offered with name,
version, and content_hash, and the original messages as role and text
(spec 0.20 section 8; the demo UI shows the prompt from them).
"""

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from relaymind.schema_util import check_closed_fields, check_schema_version, load_packaged_schema

SCHEMA_VERSION = 1

TRACE_EVENT_SCHEMA: dict[str, Any] = load_packaged_schema("relaymind.store", "trace_event.v1.json")


class EventType(StrEnum):
    """Required trace event types (RM22)."""

    RUN_ACCEPTED = "run.accepted"
    ROUTE_SELECTED = "route.selected"
    SKILL_LOADED = "skill.loaded"
    MODEL_STARTED = "model.started"
    MODEL_FINISHED = "model.finished"
    TOOL_PROPOSED = "tool.proposed"
    TOOL_STARTED = "tool.started"
    TOOL_FINISHED = "tool.finished"
    CLARIFICATION_REQUIRED = "clarification.required"
    CLARIFICATION_ANSWERED = "clarification.answered"
    CONFIRMATION_REQUIRED = "confirmation.required"
    HANDOFF_STARTED = "handoff.started"
    ANSWER_DELTA = "answer.delta"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    CACHE_HIT = "cache.hit"


_FIELDS = (
    "schema_version",
    "run_id",
    "seq",
    "timestamp",
    "elapsed_ms",
    "event_type",
    "actor",
    "payload",
)

_TOOL_EVENTS = (EventType.TOOL_PROPOSED, EventType.TOOL_STARTED, EventType.TOOL_FINISHED)

# Payload keys that must be non-blank strings, per event type.
_REQUIRED_PAYLOAD: dict[EventType, tuple[str, ...]] = {
    **{event_type: ("model",) for event_type in _TOOL_EVENTS},
    EventType.MODEL_STARTED: ("call_id", "model"),
    EventType.MODEL_FINISHED: ("call_id", "model"),
    EventType.RUN_COMPLETED: ("final_answer_model",),
    EventType.RUN_FAILED: ("error_code",),
    EventType.CACHE_HIT: ("originating_version",),
    # RM06: every load names the skill, its version, and its hash.
    EventType.SKILL_LOADED: ("name", "version", "content_hash"),
    # Spec 0.6 section 8: the input the run waits for is never blank.
    EventType.CLARIFICATION_REQUIRED: ("question", "language", "reason_code", "missing"),
    EventType.CLARIFICATION_ANSWERED: ("input", "missing"),
    # Spec 0.10 section 8 (RM11): the approval, the tool, the proposal,
    # the target, and the proposing model.
    EventType.CONFIRMATION_REQUIRED: ("approval_id", "tool_name", "proposal_id", "target", "model"),
    # Spec 0.9 section 8 (RM13): the escalation reason and the external model.
    EventType.HANDOFF_STARTED: ("reason_code", "model"),
}

# Native execution_mode values, shared by the chat header (sections 7.1, 7.2).
EXECUTION_MODES: tuple[str, ...] = ("auto", "local_only", "external_only", "parallel_demo")

# The chat virtual aliases (section 7.1); a native run records null (section 8).
MODEL_ALIASES: tuple[str, ...] = ("relaymind", "relaymind-client-tools")

_SKILL_OFFERED_FIELDS = ("name", "version", "content_hash")

# The roles whose text is kept as the original request (spec 0.20 section 8).
_MESSAGE_ROLES = ("system", "developer", "user", "assistant")

# The same patterns as the published schema: a UTC RFC 3339 time, and
# non-blank text (at least one non-space character).
_UTC_TIMESTAMP = re.compile(
    r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])"
    r"T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d{1,6})?Z"
)
_NON_BLANK = re.compile(r"\S")


class TraceEventError(ValueError):
    """A trace event failed schema validation (spec section 8)."""


@dataclass(frozen=True)
class TraceEvent:
    """A validated trace event."""

    run_id: str
    seq: int
    timestamp: str
    elapsed_ms: int
    event_type: EventType
    actor: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        # A plain string such as "run.accepted" becomes its EventType;
        # an unknown one raises ValueError.
        object.__setattr__(self, "event_type", EventType(self.event_type))

    def to_json_object(self) -> dict[str, Any]:
        """The event in its published schema shape."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "elapsed_ms": self.elapsed_ms,
            "event_type": self.event_type.value,
            "actor": self.actor,
            "payload": dict(self.payload),
        }


def _is_text(value: object) -> bool:
    return isinstance(value, str) and _NON_BLANK.search(value) is not None


def _is_count(value: object, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def _check_timestamp(value: object, name: str = "timestamp") -> str:
    error = f"'{name}' must be a UTC time such as 2026-10-20T03:00:00Z"
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise TraceEventError(error)
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise TraceEventError(error) from None
    return value


def _is_array(value: object) -> bool:
    """A JSON array from Python: a list or tuple, or another non-text sequence."""
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)


def _is_finite_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


_ANSWER_OPTION_CHECKS: dict[str, Callable[[Any], bool]] = {
    "max_completion_tokens": lambda v: _is_count(v, 1),
    "temperature": lambda v: _is_finite_number(v) and 0 <= v <= 2,
    "top_p": lambda v: _is_finite_number(v) and 0 < v <= 1,
}


def _check_accepted_payload(payload: Mapping[str, Any]) -> None:
    """The section 8 run.accepted payload."""
    for key in (
        "requested_execution_mode",
        "effective_execution_mode",
        "model_alias",
        "budgets",
        "skill_roots",
        "skills_offered",
        "original_messages",
    ):
        if key not in payload:
            raise TraceEventError(f"run.accepted payload needs '{key}'")
    modes = ", ".join(EXECUTION_MODES)
    requested = payload["requested_execution_mode"]
    if requested is not None and requested not in EXECUTION_MODES:
        raise TraceEventError(f"'requested_execution_mode' must be one of {modes}, or null")
    if payload["effective_execution_mode"] not in EXECUTION_MODES:
        raise TraceEventError(f"'effective_execution_mode' must be one of {modes}")
    alias = payload["model_alias"]
    if alias is not None and alias not in MODEL_ALIASES:
        raise TraceEventError(f"'model_alias' must be one of {', '.join(MODEL_ALIASES)}, or null")
    if not isinstance(payload["budgets"], Mapping):
        raise TraceEventError("'budgets' must be a JSON object")
    roots = payload["skill_roots"]
    if not _is_array(roots) or not all(_is_text(root) for root in roots):
        raise TraceEventError("'skill_roots' must be an array of non-blank strings")
    offered = payload["skills_offered"]
    if not _is_array(offered) or not all(
        isinstance(skill, Mapping) and all(_is_text(skill.get(k)) for k in _SKILL_OFFERED_FIELDS)
        for skill in offered
    ):
        raise TraceEventError(
            "'skills_offered' must be an array of objects with a non-blank"
            f" {', '.join(_SKILL_OFFERED_FIELDS)}"
        )
    _check_original_messages(payload["original_messages"])
    # Spec 0.22: optional, because a run stored before it has no field.
    if "answer_options" in payload:
        _check_answer_options(payload["answer_options"])
    # Spec 0.26: optional label of a run with the routing cache enabled.
    if "routing_cache" in payload and type(payload["routing_cache"]) is not bool:
        raise TraceEventError("'routing_cache' must be a boolean")


def _check_cache_hit_payload(payload: Mapping[str, Any]) -> None:
    """The optional provenance fields of cache.hit (spec 0.26 section 8)."""
    for key in ("originating_run_id", "key"):
        if key in payload and not _is_text(payload[key]):
            raise TraceEventError(f"cache.hit '{key}' must be non-blank text")
    if "age_ms" in payload and not _is_count(payload["age_ms"], 0):
        raise TraceEventError("cache.hit 'age_ms' must be a non-negative integer")
    skill = payload.get("skill")
    if "skill" in payload and not (
        isinstance(skill, Mapping)
        and set(skill) == set(_SKILL_OFFERED_FIELDS)
        and all(_is_text(skill[key]) for key in _SKILL_OFFERED_FIELDS)
    ):
        raise TraceEventError(
            f"cache.hit 'skill' must have exactly a non-blank {', '.join(_SKILL_OFFERED_FIELDS)}"
        )


def _check_answer_options(options: object) -> None:
    """The answer options a run applied, only the keys that apply (spec 0.22 section 8)."""
    if not isinstance(options, Mapping) or not set(options) <= set(_ANSWER_OPTION_CHECKS):
        raise TraceEventError(
            f"'answer_options' must be an object with only {', '.join(_ANSWER_OPTION_CHECKS)}"
        )
    for name, value in options.items():
        if not _ANSWER_OPTION_CHECKS[name](value):
            raise TraceEventError(f"'answer_options' has an invalid '{name}'")


def _check_original_messages(messages: object) -> None:
    """The conversation roles of the request, as role and text (spec 0.20 section 8)."""
    if not _is_array(messages) or not all(
        isinstance(message, Mapping)
        and set(message) == {"role", "content"}
        and message["role"] in _MESSAGE_ROLES
        and isinstance(message["content"], str)
        for message in messages
    ):
        raise TraceEventError(
            "'original_messages' must be an array of objects with only a role"
            f" ({', '.join(_MESSAGE_ROLES)}) and a text content"
        )


def _check_clarification_payload(payload: Mapping[str, Any]) -> None:
    """The section 8 clarification.required fields that are not plain text (spec 0.6)."""
    for key in ("skill_id", "waiting_seconds"):
        if key not in payload:
            raise TraceEventError(f"clarification.required payload needs '{key}'")
    skill_id = payload["skill_id"]
    if skill_id is not None and not _is_text(skill_id):
        raise TraceEventError("'skill_id' must be a non-blank string, or null")
    seconds = payload["waiting_seconds"]
    # RM03: a limit is finite; infinity or NaN would be no limit.
    if type(seconds) not in (int, float) or not seconds > 0 or not math.isfinite(seconds):
        raise TraceEventError("'waiting_seconds' must be a positive finite number")


# Spec 0.6 section 8 / 12.1 (RM01, RM25): run.completed and run.failed
# count calls and time as non-negative integers.
_TERMINAL_COUNTS = ("external_calls", "active_ms", "waiting_ms")


def _check_terminal_payload(event_type: EventType, payload: Mapping[str, Any]) -> None:
    """Required terminal evidence from section 8 (RM16, RM22, RM25)."""
    required = ("active_ms", "waiting_ms")
    if event_type is not EventType.RUN_CANCELLED:
        required += ("model_calls", "external_calls")
    for key in required:
        if key not in payload:
            raise TraceEventError(f"{event_type} payload needs '{key}'")
    for key in _TERMINAL_COUNTS:
        if key in payload and not _is_count(payload[key], 0):
            raise TraceEventError(f"'{key}' must be a non-negative integer")
    if "model_calls" in payload:
        calls = payload["model_calls"]
        if not _is_array(calls) or not all(
            isinstance(call, Mapping)
            and all(_is_text(call.get(key)) for key in ("call_id", "model", "actor", "purpose"))
            for call in calls
        ):
            raise TraceEventError(
                "'model_calls' must identify each call_id, model, actor, and purpose"
            )


# Spec sections 5 and 8 (RM10, RM17): a run that restart recovery ends
# has error_code interrupted, the non-terminal state it was in, and the
# operations it settled, each unknown_outcome or failed.
_INTERRUPTED = "interrupted"
_RECOVERY_FIELDS = ("interrupted_state", "settled_operations")
_INTERRUPTED_STATES = (
    "accepted",
    "routing",
    "local_running",
    "external_running",
    "parallel_running",
    "waiting_input",
    "waiting_confirmation",
)
_SETTLED_STATUSES = ("unknown_outcome", "failed")


def _check_failed_payload(payload: Mapping[str, Any]) -> None:
    """The section 8 run.failed recovery fields (RM10, RM17, AC12).

    They are required when error_code is interrupted, and allowed only then.
    """
    interrupted = payload.get("error_code") == _INTERRUPTED
    for key in _RECOVERY_FIELDS:
        if interrupted and key not in payload:
            raise TraceEventError(f"an interrupted run.failed payload needs '{key}'")
        if not interrupted and key in payload:
            raise TraceEventError(f"'{key}' needs error_code {_INTERRUPTED}")
    if not interrupted:
        return
    if payload["interrupted_state"] not in _INTERRUPTED_STATES:
        raise TraceEventError(
            f"'interrupted_state' must be one of {', '.join(_INTERRUPTED_STATES)}"
        )
    operations = payload["settled_operations"]
    if not _is_array(operations) or not all(
        isinstance(op, Mapping)
        and set(op) == {"operation_id", "status"}
        and _is_text(op["operation_id"])
        and op["status"] in _SETTLED_STATUSES
        for op in operations
    ):
        raise TraceEventError(
            "'settled_operations' must be an array of objects with exactly a non-blank"
            f" operation_id and a status of {' or '.join(_SETTLED_STATUSES)}"
        )


def _check_recovered_result(payload: Mapping[str, Any]) -> None:
    """A tool.finished from restart recovery is never a replay (section 5; RM10)."""
    if "recovered" not in payload:
        return
    if type(payload["recovered"]) is not bool:
        raise TraceEventError("'recovered' must be a boolean")
    if payload["recovered"] and payload.get("replayed") is not False:
        raise TraceEventError("a recovered tool.finished needs 'replayed' false")


# Spec 0.10 section 8 (RM11, RM12): the confirmation.required payload is
# exactly these fields, so the token and its hash never reach the trace.
_CONFIRMATION_FIELDS = (
    "approval_id",
    "tool_name",
    "proposal_id",
    "arguments",
    "target",
    "state_version",
    "expires_at",
    "model",
)


def _check_confirmation_payload(payload: Mapping[str, Any]) -> None:
    """The section 8 confirmation.required fields that are not plain text (RM11).

    The canonical redacted arguments, the run version the approval is
    bound to, and the UTC expiry; no other field, never the token or
    its hash.
    """
    check_closed_fields(payload, _CONFIRMATION_FIELDS, TraceEventError, kind="payload")
    if not isinstance(payload["arguments"], Mapping):
        raise TraceEventError("'arguments' must be a JSON object")
    if not _is_count(payload["state_version"], 1):
        raise TraceEventError("'state_version' must be an integer of at least 1")
    _check_timestamp(payload["expires_at"], "expires_at")


# Spec 0.10 section 8: a denied confirmation ends the run as cancelled
# with user_denied and the approval ID (RM11). Spec 0.17 sections 7.4 and
# 8: every other cancellation is a cancel request, so every run.cancelled
# names one of these reasons, with or without a voided approval.
_CANCEL_REASONS = ("user_denied", "cancel_requested")


def _check_cancelled_payload(payload: Mapping[str, Any]) -> None:
    """The section 8 run.cancelled reason and approval_id (RM11, RM17, RM22)."""
    if payload.get("reason") not in _CANCEL_REASONS:
        raise TraceEventError(
            f"run.cancelled payload needs 'reason' {' or '.join(_CANCEL_REASONS)}"
        )
    if "approval_id" in payload and not _is_text(payload["approval_id"]):
        raise TraceEventError("'approval_id' must be a non-blank string")


def _check_answered_payload(payload: Mapping[str, Any]) -> None:
    """The section 8 clarification.answered waiting time (spec 0.6; RM25)."""
    if "waited_ms" not in payload:
        raise TraceEventError("clarification.answered payload needs 'waited_ms'")
    if not _is_count(payload["waited_ms"], 0):
        raise TraceEventError("'waited_ms' must be a non-negative integer")


# Spec section 8: how the external request was measured (RM13).
_REQUEST_METHODS = ("tokenizer", "utf8_bytes")
_REQUEST_FIELDS = ("size", "limit", "method")


def _check_handoff_payload(payload: Mapping[str, Any]) -> None:
    """The section 8 handoff.started fields that are not plain text (RM13, RM14).

    The selected skill (name, version, content_hash) or null, the
    completed operations the envelope carries, the allowed tools, and
    the measured external request. In degraded mode (spec section 11)
    the flag and its local cause go together.
    """
    for key in ("skill", "operation_ids", "allowed_tools", "request"):
        if key not in payload:
            raise TraceEventError(f"handoff.started payload needs '{key}'")
    skill = payload["skill"]
    if skill is not None and not (
        isinstance(skill, Mapping)
        and set(skill) == set(_SKILL_OFFERED_FIELDS)
        and all(_is_text(skill[key]) for key in _SKILL_OFFERED_FIELDS)
    ):
        raise TraceEventError(
            f"'skill' must be null or an object with exactly a non-blank"
            f" {', '.join(_SKILL_OFFERED_FIELDS)}"
        )
    for key in ("operation_ids", "allowed_tools"):
        values = payload[key]
        if not _is_array(values) or not all(_is_text(value) for value in values):
            raise TraceEventError(f"'{key}' must be an array of non-blank strings")
    request = payload["request"]
    if not (
        isinstance(request, Mapping)
        and set(request) == set(_REQUEST_FIELDS)
        and _is_count(request["size"], 0)
        and _is_count(request["limit"], 1)
        and request["method"] in _REQUEST_METHODS
    ):
        raise TraceEventError(
            "'request' must be an object with exactly a non-negative integer size,"
            f" a positive integer limit, and a method of {' or '.join(_REQUEST_METHODS)}"
        )
    # Spec 0.13 section 11: degraded mode and its cause go together.
    if ("degraded_mode" in payload) != ("local_failure" in payload):
        raise TraceEventError("'degraded_mode' and 'local_failure' go together")
    if "degraded_mode" in payload:
        if payload["degraded_mode"] is not True:
            raise TraceEventError("'degraded_mode' must be true when present")
        if not _is_text(payload["local_failure"]):
            raise TraceEventError("'local_failure' must be non-blank text")


def _check_result_reference(payload: Mapping[str, Any]) -> None:
    """A result or its retained artifact, with explicit truncation (RM22)."""
    if "result" in payload:
        if "result_ref" in payload:
            raise TraceEventError("a tool event cannot carry both result and result_ref")
        result = payload["result"]
        if not isinstance(result, Mapping) or type(result.get("truncated")) is not bool:
            raise TraceEventError("a tool result needs a boolean 'truncated'")
    if "result_ref" not in payload:
        return
    ref = check_closed_fields(
        payload["result_ref"],
        ("artifact_id", "uri", "size_bytes", "truncated"),
        TraceEventError,
        kind="result_ref",
    )
    for key in ("artifact_id", "uri"):
        if not _is_text(ref[key]):
            raise TraceEventError(f"result_ref needs a non-blank '{key}'")
    if not _is_count(ref["size_bytes"], 0):
        raise TraceEventError("result_ref 'size_bytes' must be a non-negative integer")
    if type(ref["truncated"]) is not bool:
        raise TraceEventError("result_ref 'truncated' must be a boolean")


# Spec 0.25 section 8 (RM15, RM22): the branches of the parallel workflow
# and the summary its terminal events carry.
WORKFLOW_BRANCHES: tuple[str, ...] = ("local_health", "external_checklist", "synthesis")
_BRANCH_EVENTS = (EventType.MODEL_STARTED, EventType.MODEL_FINISHED, *_TOOL_EVENTS)
_WORKFLOW_MS_FIELDS = ("branch_wall_ms", "local_health_ms", "external_checklist_ms")
_WORKFLOW_FIELDS = ("workflow_id", *_WORKFLOW_MS_FIELDS, "extra_call_ms", "abandoned_calls")
_TERMINAL_EVENTS = (EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_CANCELLED)


def _check_workflow_fields(event_type: EventType, payload: Mapping[str, Any]) -> None:
    """The optional branch of a model or tool event, and the workflow of a terminal event."""
    if event_type in _BRANCH_EVENTS and "branch" in payload:
        if payload["branch"] not in WORKFLOW_BRANCHES:
            raise TraceEventError(f"'branch' must be one of {', '.join(WORKFLOW_BRANCHES)}")
    if event_type is EventType.HANDOFF_STARTED and "workflow" in payload:
        if not _is_text(payload["workflow"]):
            raise TraceEventError("'workflow' must be a non-blank string")
    if event_type in _TERMINAL_EVENTS and "workflow" in payload:
        summary = check_closed_fields(
            payload["workflow"], _WORKFLOW_FIELDS, TraceEventError, kind="workflow"
        )
        if not _is_text(summary["workflow_id"]):
            raise TraceEventError("workflow 'workflow_id' must be a non-blank string")
        for key in _WORKFLOW_MS_FIELDS:
            if not _is_count(summary[key], 0):
                raise TraceEventError(f"workflow '{key}' must be a non-negative integer")
        if summary["extra_call_ms"] is not None and not _is_count(summary["extra_call_ms"], 0):
            raise TraceEventError("workflow 'extra_call_ms' must be a non-negative integer or null")
        abandoned = summary["abandoned_calls"]
        if not _is_array(abandoned) or not all(_is_text(call) for call in abandoned):
            raise TraceEventError(
                "workflow 'abandoned_calls' must be an array of non-blank strings"
            )


def _check_payload(event_type: EventType, payload: Mapping[str, Any]) -> None:
    _check_workflow_fields(event_type, payload)
    if event_type is EventType.RUN_ACCEPTED:
        _check_accepted_payload(payload)
    if event_type is EventType.CACHE_HIT:
        _check_cache_hit_payload(payload)
    if event_type is EventType.CLARIFICATION_REQUIRED:
        _check_clarification_payload(payload)
    if event_type is EventType.CLARIFICATION_ANSWERED:
        _check_answered_payload(payload)
    if event_type is EventType.HANDOFF_STARTED:
        _check_handoff_payload(payload)
    if event_type is EventType.CONFIRMATION_REQUIRED:
        _check_confirmation_payload(payload)
    if event_type is EventType.RUN_CANCELLED:
        _check_cancelled_payload(payload)
    if event_type is EventType.RUN_FAILED:
        _check_failed_payload(payload)
    if event_type is EventType.TOOL_FINISHED:
        _check_recovered_result(payload)
    if event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_CANCELLED):
        _check_terminal_payload(event_type, payload)
    if event_type in _TOOL_EVENTS:
        _check_result_reference(payload)
        ids = [key for key in ("operation_id", "proposal_id") if key in payload]
        if not ids:
            raise TraceEventError(f"{event_type} payload needs operation_id or proposal_id")
        for key in ids:
            # An ID that is present is checked even when the other one is valid.
            if not _is_text(payload[key]):
                raise TraceEventError(f"{event_type} payload needs a non-blank '{key}'")
    for key in _REQUIRED_PAYLOAD.get(event_type, ()):
        if not _is_text(payload.get(key)):
            raise TraceEventError(f"{event_type} payload needs a non-blank '{key}'")


def validate_trace_event(data: object) -> TraceEvent:
    """Validate a decoded TraceEvent against the published schema."""
    data = check_closed_fields(data, _FIELDS, TraceEventError, kind="TraceEvent")
    check_schema_version(data["schema_version"], SCHEMA_VERSION, TraceEventError)
    for name in ("run_id", "actor"):
        if not _is_text(data[name]):
            raise TraceEventError(f"'{name}' must be a non-blank string")
    if not _is_count(data["seq"], 1):
        raise TraceEventError("'seq' must be an integer of at least 1")
    if not _is_count(data["elapsed_ms"], 0):
        raise TraceEventError("'elapsed_ms' must be a non-negative integer")
    timestamp = _check_timestamp(data["timestamp"])
    event_type = data["event_type"]
    if not isinstance(event_type, str) or event_type not in EventType:
        raise TraceEventError(f"'event_type' must be one of {', '.join(EventType)}")
    event_type = EventType(event_type)
    # RM22: run.accepted is the first event of a run, and only it is.
    if (event_type is EventType.RUN_ACCEPTED) != (data["seq"] == 1):
        raise TraceEventError(
            "run.accepted must be the first event of a run ('seq' 1), and only it"
        )
    payload = data["payload"]
    if not isinstance(payload, Mapping):
        raise TraceEventError("'payload' must be a JSON object")
    _check_payload(event_type, payload)
    payload = dict(payload)
    if event_type is EventType.RUN_ACCEPTED:
        # Any sequence is accepted; the event holds JSON-shaped lists.
        payload["skill_roots"] = list(payload["skill_roots"])
        payload["skills_offered"] = [dict(skill) for skill in payload["skills_offered"]]
    return TraceEvent(
        run_id=data["run_id"],
        seq=data["seq"],
        timestamp=timestamp,
        elapsed_ms=data["elapsed_ms"],
        event_type=event_type,
        actor=data["actor"],
        payload=payload,
    )
