"""The evidence of one run, as the demo UI shows it (RM23, RM25, spec 0.20 section 9).

``build_trace_view`` reads a run's stored events, the tool results they
store by reference (spec 0.30), and the run record, and returns plain
JSON data. It holds nothing the stored evidence does not: a field the
events do not contain is ``None`` (or an empty list), never a guess, and
no reasoning is inferred (spec section 3). Tool results in the
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

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from relaymind.store import EventType, RunRecord, RunStore, TraceEvent, read_tool_result
from relaymind.store.trace_event import WORKFLOW_BRANCHES

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
    return trace_view_from(record, store.events(run_id), read_artifact=store.read_artifact)


def _of(events: Sequence[TraceEvent], event_type: EventType) -> list[TraceEvent]:
    return [event for event in events if event.event_type is event_type]


def _pick(payload: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {key: payload.get(key) for key in keys}


def trace_view_from(
    record: RunRecord,
    events: Sequence[TraceEvent],
    *,
    read_artifact: Callable[[str], Any | None] | None = None,
) -> dict[str, Any]:
    """The trace view of a run record and its events, in seq order.

    read_artifact reads back a tool result stored by reference (spec 0.30
    section 9); without it, or once the artifact is gone, the operation
    shows its event's status and the reference, never invented data.
    """
    accepted = next((e for e in events if e.event_type is EventType.RUN_ACCEPTED), None)
    accepted_payload = {} if accepted is None else accepted.payload
    terminal = next((e for e in reversed(events) if e.event_type in _TERMINAL_TIMING), None)
    failed = next((e for e in reversed(events) if e.event_type is EventType.RUN_FAILED), None)
    cancelled = next((e for e in reversed(events) if e.event_type is EventType.RUN_CANCELLED), None)
    operations = _operations(events, read_artifact)
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
        # None: a run stored before spec 0.22. {}: a run without options.
        "answer_options": accepted_payload.get("answer_options"),
        "execution_mode": {
            "requested": accepted_payload.get("requested_execution_mode"),
            "effective": accepted_payload.get("effective_execution_mode"),
        },
        "model_calls": model_calls,
        "workflow": _workflow(events, model_calls, terminal),
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
                "branch": event.payload.get("branch"),
                "finished": done is not None,
                "duration_ms": None if done is None else done.get("duration_ms"),
                "finish_reason": None if done is None else done.get("finish_reason"),
                "error": None if done is None else done.get("error"),
            }
        )
    return calls


_WORKFLOW_FIGURES = (
    "branch_wall_ms",
    "local_health_ms",
    "external_checklist_ms",
    "extra_call_ms",
    "abandoned_calls",
)
# The workflow figure that is each stage's duration.
_STAGE_FIGURES = {
    "local_health": "local_health_ms",
    "external_checklist": "external_checklist_ms",
    "synthesis": "extra_call_ms",
}


def _workflow(
    events: Sequence[TraceEvent], calls: Sequence[Mapping[str, Any]], terminal: TraceEvent | None
) -> dict[str, Any] | None:
    """The parallel workflow of the run as separate actors, or None (spec 0.25, RM23).

    One stage for each branch that made a model call. The figures,
    stage durations included, are the terminal event's workflow object,
    so they are known when the run ends: a branch's time covers its tool
    run, not only its model calls. Nothing is computed from the clock
    here. A run that restart recovery ended has no workflow object.
    """
    if not any(call["branch"] for call in calls):
        return None
    summary: Mapping[str, Any] = {}
    if terminal is not None and isinstance(terminal.payload.get("workflow"), Mapping):
        summary = terminal.payload["workflow"]
    stages = []
    for branch in WORKFLOW_BRANCHES:
        own = [call for call in calls if call["branch"] == branch]
        if not own:
            continue
        stages.append(
            {
                "branch": branch,
                "actor": own[0]["actor"],
                "model": own[0]["model"],
                "call_ids": [c["call_id"] for c in own],
                "finished": all(c["finished"] for c in own),
                "duration_ms": summary.get(_STAGE_FIGURES[branch]),
                "outcome": own[-1]["error"] or own[-1]["finish_reason"],
            }
        )
    handoffs = _of(events, EventType.HANDOFF_STARTED)
    workflow_id = summary.get("workflow_id") or next(
        (h.payload["workflow"] for h in handoffs if "workflow" in h.payload), None
    )
    return {
        "workflow_id": workflow_id,
        "ended": terminal is not None,
        "stages": stages,
        **{key: summary.get(key) for key in _WORKFLOW_FIGURES},
    }


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
                (
                    "proposal_id",
                    "tool_name",
                    "arguments",
                    "model",
                    "admissible",
                    "rejection",
                    "branch",
                ),
            ),
            "actor": e.actor,
        }
        for e in _of(events, EventType.TOOL_PROPOSED)
    ]


_RESULT_FIELDS = (
    "status",
    "data",
    "error",
    "duration_ms",
    "observed_at",
    "source_ref",
    "truncated",
)


def _result_of(
    payload: Mapping[str, Any], read_artifact: Callable[[str], Any | None] | None
) -> dict[str, Any]:
    """The result fields of a tool.finished event, read back when stored by reference."""
    view = read_tool_result(payload, read_artifact)
    # Not retained: the event's own status, and nothing invented (RM23).
    result = view.result if view.retained else {"status": payload.get("status")}
    return {
        **_pick(result, _RESULT_FIELDS),
        "result_ref": view.result_ref,
        "result_retained": view.retained,
    }


def _once(read_artifact: Callable[[str], Any | None]) -> Callable[[str], Any | None]:
    """read_artifact that reads each distinct artifact once."""
    seen: dict[str, Any | None] = {}

    def read(artifact_id: str) -> Any | None:
        if artifact_id not in seen:
            seen[artifact_id] = read_artifact(artifact_id)
        return seen[artifact_id]

    return read


def _operations(
    events: Sequence[TraceEvent], read_artifact: Callable[[str], Any | None] | None
) -> list[dict[str, Any]]:
    """Each finished tool operation, in order, with the model that proposed it."""
    if read_artifact is not None:
        read_artifact = _once(read_artifact)
    return [
        {
            **_pick(
                e.payload,
                (
                    "operation_id",
                    "proposal_id",
                    "tool_name",
                    "model",
                    "attempt",
                    "approval_id",
                    "branch",
                ),
            ),
            "replayed": bool(e.payload.get("replayed")),
            **_result_of(e.payload, read_artifact),
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
