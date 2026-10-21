"""The evidence of one run, as the demo UI shows it (RM23, RM25, spec 0.20 section 9).

``build_trace_view`` reads a run's stored events and run record and
returns plain JSON data. It holds nothing the events do not: a field
the events do not contain is ``None`` (or an empty list), never a guess,
and no reasoning is inferred (spec section 3). Tool results in the
events are already redacted (RM12); the renderer escapes them.

Latency is model action latency only: the elapsed time of the first
validated model-produced route, clarification, or tool proposal
(spec section 12.1). A route the gateway made itself (actor gateway) is
not a model action, and neither is a proposal the gateway rejected. A
clarification is a route decision (route=clarify), so its route.selected
is the action; clarification.required is always the gateway's record.
The run's active and waiting time come from its terminal event, as
separate fields, and are not answer latency.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from relaymind.store import EventType, RunRecord, RunStore, TraceEvent

__all__ = ["build_trace_view", "trace_view_from"]

_GATEWAY_ACTOR = "gateway"
_TERMINAL_TIMING = (
    EventType.RUN_COMPLETED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLED,
)
_FAILED = ("failed", "unknown_outcome")


def build_trace_view(store: RunStore, run_id: str) -> dict[str, Any]:
    """The trace view of a stored run (RunNotFoundError for an unknown one)."""
    record = store.get_run(run_id)
    return trace_view_from(record, store.events(run_id))


def _of(events: Sequence[TraceEvent], event_type: EventType) -> list[TraceEvent]:
    return [event for event in events if event.event_type is event_type]


def _pick(payload: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {key: payload.get(key) for key in keys}


def trace_view_from(record: RunRecord, events: Sequence[TraceEvent]) -> dict[str, Any]:
    """The trace view of a run record and its events, in seq order."""
    accepted = next((e for e in events if e.event_type is EventType.RUN_ACCEPTED), None)
    accepted_payload = {} if accepted is None else accepted.payload
    terminal = next((e for e in reversed(events) if e.event_type in _TERMINAL_TIMING), None)
    failed = next((e for e in reversed(events) if e.event_type is EventType.RUN_FAILED), None)
    cancelled = next((e for e in reversed(events) if e.event_type is EventType.RUN_CANCELLED), None)
    operations = _operations(events)
    model_calls = _model_calls(events)
    answer = _final_answer(events)
    return {
        "run_id": record.run_id,
        "state": record.state,
        "state_version": record.version,
        "error_code": None if failed is None else failed.payload.get("error_code"),
        "reason_code": None if failed is None else failed.payload.get("reason_code"),
        "cancel_reason": None if cancelled is None else cancelled.payload.get("reason"),
        "accepted_at": None if accepted is None else accepted.timestamp,
        "prompt": _prompt(accepted_payload),
        "execution_mode": {
            "requested": accepted_payload.get("requested_execution_mode"),
            "effective": accepted_payload.get("effective_execution_mode"),
        },
        "model_calls": model_calls,
        "active_model": _active_model(events, model_calls, terminal),
        "skill": _skill(events),
        "route": _route(events),
        "proposals": _proposals(events),
        "operations": operations,
        "failed_operations": [op for op in operations if op["status"] in _FAILED],
        "escalation": _escalation(events),
        "clarifications": _clarifications(events),
        "confirmation": _confirmation(events, terminal),
        "final_answer": answer,
        "timing": _timing(events, terminal),
    }


def _prompt(accepted: Mapping[str, Any]) -> list[dict[str, str]] | None:
    messages = accepted.get("original_messages")
    if messages is None:
        return None
    return [{"role": m["role"], "content": m["content"]} for m in messages]


def _model_calls(events: Sequence[TraceEvent]) -> list[dict[str, Any]]:
    """One entry for each model.started, with its model.finished facts when there is one."""
    finished = {e.payload["call_id"]: e.payload for e in _of(events, EventType.MODEL_FINISHED)}
    calls = []
    for event in _of(events, EventType.MODEL_STARTED):
        done = finished.get(event.payload["call_id"])
        calls.append(
            {
                "call_id": event.payload["call_id"],
                "model": event.payload["model"],
                "actor": event.payload.get("actor", event.actor),
                "purpose": event.payload.get("purpose"),
                "finished": done is not None,
                "duration_ms": None if done is None else done.get("duration_ms"),
                "finish_reason": None if done is None else done.get("finish_reason"),
                "error": None if done is None else done.get("error"),
            }
        )
    return calls


def _active_model(
    events: Sequence[TraceEvent], calls: Sequence[Mapping[str, Any]], terminal: TraceEvent | None
) -> str | None:
    """The model of the latest call while active; the answer's model once it ended (RM16)."""
    if terminal is not None and terminal.event_type is EventType.RUN_COMPLETED:
        return terminal.payload["final_answer_model"]
    return calls[-1]["model"] if calls else None


def _skill(events: Sequence[TraceEvent]) -> dict[str, Any] | None:
    loaded = _of(events, EventType.SKILL_LOADED)
    return _pick(loaded[-1].payload, ("name", "version", "content_hash")) if loaded else None


def _route(events: Sequence[TraceEvent]) -> dict[str, Any] | None:
    selected = _of(events, EventType.ROUTE_SELECTED)
    if not selected:
        return None
    payload = selected[-1].payload
    decision = payload["decision"]
    return {
        **_pick(decision, ("route", "skill_id", "reason_code", "language")),
        "mechanism": payload.get("mechanism"),
        "model": payload.get("model"),
    }


def _proposals(events: Sequence[TraceEvent]) -> list[dict[str, Any]]:
    return [
        {
            **_pick(
                e.payload,
                ("proposal_id", "tool_name", "arguments", "model", "admissible", "rejection"),
            ),
            "actor": e.actor,
        }
        for e in _of(events, EventType.TOOL_PROPOSED)
    ]


def _operations(events: Sequence[TraceEvent]) -> list[dict[str, Any]]:
    """Each finished tool operation, in order, with the model that proposed it."""
    return [
        {
            **_pick(
                e.payload,
                ("operation_id", "proposal_id", "tool_name", "model", "attempt", "approval_id"),
            ),
            "replayed": bool(e.payload.get("replayed")),
            **_pick(
                e.payload["result"],
                (
                    "status",
                    "data",
                    "error",
                    "duration_ms",
                    "observed_at",
                    "source_ref",
                    "truncated",
                ),
            ),
        }
        for e in _of(events, EventType.TOOL_FINISHED)
    ]


def _escalation(events: Sequence[TraceEvent]) -> dict[str, Any] | None:
    handoffs = _of(events, EventType.HANDOFF_STARTED)
    if not handoffs:
        return None
    payload = handoffs[-1].payload
    return {
        **_pick(payload, ("reason_code", "model", "operation_ids")),
        "degraded_mode": bool(payload.get("degraded_mode")),
        "local_failure": payload.get("local_failure"),
    }


def _clarifications(events: Sequence[TraceEvent]) -> list[dict[str, Any]]:
    answers = [e.payload for e in _of(events, EventType.CLARIFICATION_ANSWERED)]
    rounds = []
    for index, event in enumerate(_of(events, EventType.CLARIFICATION_REQUIRED)):
        asked = event.payload
        rounds.append(
            {
                **_pick(asked, ("question", "language", "reason_code", "skill_id", "missing")),
                "answer": answers[index]["input"] if index < len(answers) else None,
            }
        )
    return rounds


def _confirmation(
    events: Sequence[TraceEvent], terminal: TraceEvent | None
) -> dict[str, Any] | None:
    """The latest confirmation: pending, approved (an operation names it), denied, or void.

    An operation answered from its record emits no tool.started, so its
    tool.finished, which also names the approval, counts as well (RM10).
    """
    required = _of(events, EventType.CONFIRMATION_REQUIRED)
    if not required:
        return None
    payload = required[-1].payload
    approval_id = payload["approval_id"]
    status = "pending"
    used = (EventType.TOOL_STARTED, EventType.TOOL_FINISHED)
    if any(e.event_type in used and e.payload.get("approval_id") == approval_id for e in events):
        status = "approved"
    for event in _of(events, EventType.RUN_CANCELLED):
        if event.payload.get("approval_id") == approval_id:
            status = "denied" if event.payload.get("reason") == "user_denied" else "voided"
    if status == "pending" and terminal is not None:
        # The run ended without the approval being used.
        expired = terminal.payload.get("error_code") == "approval_expired"
        status = "expired" if expired else "voided"
    return {
        **_pick(
            payload,
            ("approval_id", "tool_name", "arguments", "target", "expires_at", "state_version"),
        ),
        "model": payload.get("model"),
        "status": status,
    }


def _final_answer(events: Sequence[TraceEvent]) -> dict[str, str] | None:
    deltas = _of(events, EventType.ANSWER_DELTA)
    if not deltas:
        return None
    return {
        "text": "".join(e.payload["text"] for e in deltas),
        "model": deltas[0].payload["model"],
    }


def _timing(events: Sequence[TraceEvent], terminal: TraceEvent | None) -> dict[str, int | None]:
    first_action: int | None = None
    for event in events:
        if _is_model_action(event):
            first_action = event.elapsed_ms
            break
    return {
        "first_model_action_ms": first_action,
        "active_ms": None if terminal is None else int(terminal.payload.get("active_ms", 0)),
        "waiting_ms": None if terminal is None else int(terminal.payload.get("waiting_ms", 0)),
    }


def _is_model_action(event: TraceEvent) -> bool:
    """A validated route (clarify included) or tool proposal that a model produced.

    A client-owned proposal carries no admissible field: it is exposed
    only after its validation (section 7.2).
    """
    if event.event_type is EventType.ROUTE_SELECTED:
        return event.actor != _GATEWAY_ACTOR
    if event.event_type is EventType.TOOL_PROPOSED:
        return event.payload.get("admissible", True) is True
    return False
