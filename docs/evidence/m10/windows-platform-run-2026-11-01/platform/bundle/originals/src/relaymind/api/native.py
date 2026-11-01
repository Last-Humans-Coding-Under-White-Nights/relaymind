"""The native run API without a web framework (RM21, AC11, spec 0.17 section 7.4).

Request parsing, the run view, the SSE event stream, and readiness.
``relaymind.api.app`` owns the routes and the error handling around
them; this module only turns requests into orchestrator calls and
stored events into responses. A view is built from the run store, so
it holds nothing the trace does not: the pending approval is the
latest ``confirmation.required`` payload, and only the token comes
from the orchestrator, once (RM11).

The event stream reads stored events. It never calls a model or a
tool, so a reconnect with Last-Event-ID cannot repeat work (AC11).
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from relaymind.adapters import AdapterHealth, Message, ModelAdapter
from relaymind.api.chat_request import parse_managed_messages
from relaymind.api.errors import ApiError
from relaymind.orchestrator.handoff import EXTERNAL_ACTOR
from relaymind.orchestrator.local_loop import LocalOrchestrator
from relaymind.orchestrator.recovery import model_calls
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.orchestrator.run_state import (
    TERMINAL_EVENTS,
    TERMINAL_STATES,
    WAITING_STATES,
    RunState,
)
from relaymind.store import EventType, RunNotFoundError, TraceEvent

__all__ = [
    "NativeSettings",
    "ResumeRequest",
    "RunRequest",
    "event_stream",
    "parse_cancel_request",
    "parse_last_event_id",
    "parse_resume_request",
    "parse_run_request",
    "readiness",
    "run_view",
    "sweep_expired_waits",
]

logger = logging.getLogger(__name__)

_RUN_FIELDS = frozenset({"messages", "execution_mode"})
_RESUME_FIELDS = frozenset({"expected_version", "input", "approval_id", "token", "approve"})
_CANCEL_FIELDS = frozenset({"expected_version"})
_APPROVAL_FIELDS = ("approval_id", "token", "approve")
# SQLite's largest integer: no event seq is above it, so a larger
# cursor is the same cursor (past every event).
_MAX_SEQ = 2**63 - 1


@dataclass(frozen=True)
class NativeSettings:
    """Timing of the native API (operator configuration).

    poll_seconds is how often a live event stream looks for new stored
    events; heartbeat_seconds is the idle time after which it sends an
    SSE comment; sweep_seconds is how often a stream of a waiting run
    ends expired waits (section 5); cancel_wait_seconds bounds how long
    a cancel request waits for running work to stop (spec 0.17 section
    7.4).
    """

    poll_seconds: float = 0.05
    heartbeat_seconds: float = 15.0
    sweep_seconds: float = 1.0
    cancel_wait_seconds: float = 5.0


@dataclass(frozen=True)
class RunRequest:
    """A validated POST /relay/v1/runs body."""

    messages: tuple[Message, ...]
    execution_mode: object = None


@dataclass(frozen=True)
class ResumeRequest:
    """A POST resume body: the version, and the fields the waiting state will check.

    Whether input or the approval fields are right depends on the run's
    state, which the orchestrator checks after the version (section 5),
    so they are carried as sent.
    """

    expected_version: int
    input: object = None
    approval_id: object = None
    token: object = None
    approve: object = None

    @property
    def has_approval_fields(self) -> bool:
        return any(getattr(self, name) is not None for name in _APPROVAL_FIELDS)


def _object(body: object) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError("invalid_request", "the request body must be a JSON object")
    return {name: value for name, value in body.items() if value is not None}


def _closed(fields: Mapping[str, Any], allowed: Collection[str]) -> None:
    for name in fields:
        if name not in allowed:
            raise ApiError("unsupported_parameter", f"{name} is not supported by this endpoint")


def parse_run_request(body: object) -> RunRequest:
    """Validate a creation body: messages, and an optional execution_mode (RM21)."""
    fields = _object(body)
    _closed(fields, _RUN_FIELDS)
    return RunRequest(
        messages=parse_managed_messages(fields.get("messages")),
        execution_mode=fields.get("execution_mode"),
    )


def _expected_version(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ApiError("invalid_request", "expected_version must be a non-negative integer")
    return value


def parse_cancel_request(body: object) -> int:
    """The expected_version of a cancel body, its only field (spec 0.17 section 7.4)."""
    fields = _object(body)
    _closed(fields, _CANCEL_FIELDS)
    return _expected_version(fields.get("expected_version"))


def parse_resume_request(body: object) -> ResumeRequest:
    """Validate the envelope of a resume body; the waiting state checks the rest."""
    fields = _object(body)
    _closed(fields, _RESUME_FIELDS)
    return ResumeRequest(
        expected_version=_expected_version(fields.get("expected_version")),
        input=fields.get("input"),
        approval_id=fields.get("approval_id"),
        token=fields.get("token"),
        approve=fields.get("approve"),
    )


def parse_last_event_id(header: str | None) -> int:
    """The cursor of a reconnecting stream: a non-negative integer, else 400.

    A cursor past the largest seq the store can hold replays nothing, so
    it is read as that largest seq rather than parsed in full.
    """
    if header is None:
        return 0
    if not (header.isascii() and header.isdigit()):
        raise ApiError("invalid_request", "Last-Event-ID must be a non-negative integer")
    digits = header.lstrip("0") or "0"
    if len(digits) > len(str(_MAX_SEQ)):
        return _MAX_SEQ
    return min(int(digits), _MAX_SEQ)


# -- the run view ------------------------------------------------------------------


def sweep_expired_waits(orchestrator: LocalOrchestrator) -> None:
    """End waits past their limit, so no view shows a run waiting too long (section 5).

    A failure is logged by type only (RM12) and never fails the read.
    """
    try:
        orchestrator.expire_waiting()
    except Exception as exc:
        logger.error("could not expire waiting runs: %s", type(exc).__name__)


def _last(orchestrator: LocalOrchestrator, run_id: str, event_type: EventType) -> TraceEvent | None:
    """The latest event of one type; the store filters, so other payloads stay undecoded."""
    events = orchestrator.store.events(run_id, event_type=event_type)
    return events[-1] if events else None


def run_view(
    orchestrator: LocalOrchestrator, run_id: str, *, reveal_token: bool = False
) -> dict[str, Any]:
    """The GET representation of a run (RM21, spec 0.17 section 7.4).

    reveal_token asks for the plaintext confirmation token; the
    orchestrator hands it out once, to the first reader that asks.
    """
    store = orchestrator.store
    record = store.get_run(run_id)
    state = RunState(record.state)
    accepted = _last(orchestrator, run_id, EventType.RUN_ACCEPTED)
    payload = {} if accepted is None else accepted.payload
    # Only run.failed carries codes; a cancelled run's reason is not an error.
    failed = _last(orchestrator, run_id, EventType.RUN_FAILED) if state is RunState.FAILED else None
    codes = {} if failed is None else failed.payload
    output = None
    if state is RunState.COMPLETED:
        deltas = store.events(run_id, event_type=EventType.ANSWER_DELTA)
        output = "".join(e.payload["text"] for e in deltas) if deltas else None
    return {
        "run_id": run_id,
        "state": state.value,
        "state_version": record.version,
        "requested_execution_mode": payload.get("requested_execution_mode"),
        "effective_execution_mode": payload.get("effective_execution_mode"),
        "output": output,
        "error_code": codes.get("error_code"),
        "reason_code": codes.get("reason_code"),
        "pending_question": _pending_question(orchestrator, run_id, state),
        "pending_approval": _pending_approval(orchestrator, run_id, state, reveal_token),
        "metrics": _metrics(orchestrator, run_id, state),
        "events_url": events_url(run_id),
    }


def events_url(run_id: str) -> str:
    return f"/relay/v1/runs/{run_id}/events"


def _pending_question(
    orchestrator: LocalOrchestrator, run_id: str, state: RunState
) -> dict[str, Any] | None:
    if state is not RunState.WAITING_INPUT:
        return None
    asked = _last(orchestrator, run_id, EventType.CLARIFICATION_REQUIRED)
    if asked is None:
        return None
    keys = ("question", "language", "reason_code", "skill_id", "missing", "waiting_seconds")
    return {key: asked.payload.get(key) for key in keys}


def _pending_approval(
    orchestrator: LocalOrchestrator, run_id: str, state: RunState, reveal: bool
) -> dict[str, Any] | None:
    if state is not RunState.WAITING_CONFIRMATION:
        return None
    issued = _last(orchestrator, run_id, EventType.CONFIRMATION_REQUIRED)
    if issued is None:
        return None
    pending = orchestrator.store.get_pending_approval(issued.run_id)
    if pending is None or pending["approval_id"] != issued.payload["approval_id"]:
        return None
    keys = ("approval_id", "tool_name", "proposal_id", "arguments", "target", "state_version")
    view = {key: issued.payload.get(key) for key in keys}
    view["expires_at"] = issued.payload.get("expires_at")
    revealed = orchestrator.reveal_confirmation(issued.run_id) if reveal else None
    view["token"] = None if revealed is None else revealed.token
    return view


def _metrics(orchestrator: LocalOrchestrator, run_id: str, state: RunState) -> dict[str, int]:
    terminal = None
    if state in TERMINAL_STATES:
        terminal = _last(orchestrator, run_id, TERMINAL_EVENTS[state])
    if terminal is not None:
        timings = {key: int(terminal.payload.get(key, 0)) for key in ("active_ms", "waiting_ms")}
    else:
        timings = orchestrator.store.state_timings(run_id)
    calls = model_calls(orchestrator.store, run_id)
    return {
        "active_ms": timings["active_ms"],
        "waiting_ms": timings["waiting_ms"],
        "model_calls": len(calls),
        "external_calls": sum(1 for call in calls if call["actor"] == EXTERNAL_ACTOR),
    }


# -- the event stream (AC11) -----------------------------------------------------


def _frame(event: TraceEvent) -> str:
    data = json.dumps(event.to_json_object())
    return f"id: {event.seq}\nevent: {event.event_type.value}\ndata: {data}\n\n"


async def event_stream(
    orchestrator: LocalOrchestrator, run_id: str, after_seq: int, settings: NativeSettings
) -> AsyncIterator[str]:
    """SSE messages for the stored events of a run after after_seq, then the live ones.

    The stream ends after the run's terminal event and stays open while
    the run waits, sending an SSE comment when it is idle. The state is
    read before the events, and a terminal transition is stored with its
    event in one transaction, so a terminal state with no event left
    means everything was sent. It only reads: replay runs no work.
    """
    store = orchestrator.store
    loop = asyncio.get_running_loop()
    last_sent = loop.time()
    next_sweep = loop.time()
    cursor = after_seq
    while True:
        try:
            state = RunState(store.get_run(run_id).state)
            if state in WAITING_STATES and loop.time() >= next_sweep:
                # Only a waiting run can expire, and not faster than the
                # sweep interval: an open stream does not keep the store busy.
                next_sweep = loop.time() + settings.sweep_seconds
                sweep_expired_waits(orchestrator)
                state = RunState(store.get_run(run_id).state)
            events = store.events(run_id, after_seq=cursor)
        except RunNotFoundError:
            return
        for event in events:
            yield _frame(event)
            cursor = event.seq
        if events:
            last_sent = loop.time()
            continue
        if state in TERMINAL_STATES:
            return
        if loop.time() - last_sent >= settings.heartbeat_seconds:
            yield ": keep-alive\n\n"
            last_sent = loop.time()
        await asyncio.sleep(settings.poll_seconds)


# -- readiness -------------------------------------------------------------------


async def _health(adapter: ModelAdapter) -> AdapterHealth:
    try:
        return await adapter.health()
    except Exception as exc:
        # The exception text may carry a URL or a key; the type is enough (RM12).
        return AdapterHealth(
            available=False, warm=False, detail=f"health check failed: {type(exc).__name__}"
        )


def _model(health: AdapterHealth | None) -> dict[str, Any] | None:
    if health is None:
        return None
    return {"available": health.available, "warm": health.warm, "detail": health.detail}


async def readiness(
    orchestrator: LocalOrchestrator, allowed_modes: Collection[ExecutionMode]
) -> tuple[bool, dict[str, Any]]:
    """Whether each permitted mode can run now, with explicit model warm state (RM21).

    A model is ready when it is available and warm. The overall answer
    is true only when every permitted mode is ready. The local model is
    reported with its class and cause (RM26, section 7.4); with detection
    on, this availability check updates the class. Without a usable local
    model auto needs the external model, and a mode the class forbids is
    not ready.
    """
    local = None if orchestrator.model is None else await _health(orchestrator.model.adapter)
    if local is not None:
        orchestrator.observe_local_health(local.available, local.warm, local.detail)
    status = orchestrator.local_status
    external = (
        None if orchestrator.external is None else await _health(orchestrator.external.adapter)
    )
    local_ready = local is not None and local.available and local.warm
    external_ready = external is not None and external.available and external.warm

    placement = orchestrator.placement.readiness(
        local_model=None if orchestrator.model is None else orchestrator.model.model_id,
        external_model=None if orchestrator.external is None else orchestrator.external.model_id,
        usable=status.usable,
        stage_seconds=orchestrator.budgets.active_seconds,
        output_cap=orchestrator.budgets.routing_output_tokens,
    )
    auto_local = status.usable and placement["routing"]["role"] == "local"

    def reason_not_ready(mode: ExecutionMode) -> str | None:
        if status.forbids(mode):
            return f"the local model is {status.status.value}"
        if mode is ExecutionMode.PARALLEL_DEMO and not orchestrator.enabled_workflows:
            return "no workflow is enabled"
        # RM26: auto without a usable local model is managed external execution.
        needs_external = mode in (ExecutionMode.EXTERNAL_ONLY, ExecutionMode.PARALLEL_DEMO) or (
            mode is ExecutionMode.AUTO and not auto_local
        )
        if needs_external and not external_ready:
            return "the external model is not ready"
        needs_local = mode is not ExecutionMode.EXTERNAL_ONLY and (
            mode is not ExecutionMode.AUTO or auto_local
        )
        if needs_local and not local_ready:
            return "the local model is not ready"
        return None

    reasons = {m: reason_not_ready(m) for m in ExecutionMode if m in allowed_modes}
    modes = {m.value: {"ready": r is None, "reason": r} for m, r in reasons.items()}
    ready = bool(modes) and all(m["ready"] for m in modes.values())
    return ready, {
        "ready": ready,
        "modes": modes,
        "models": {"local": _model(local), "external": _model(external)},
        "placement": placement,
        "local_model": {
            "model_id": None if orchestrator.model is None else orchestrator.model.model_id,
            "status": status.status.value,
            "reason": status.reason,
        },
    }
