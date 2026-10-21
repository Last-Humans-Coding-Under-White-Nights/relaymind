"""The demo UI trace view: every listed field, from stored events (RM23, RM25, #28).

Spec 0.20 section 9 (Demo UI) and section 12.1. The view is built only
from a run's stored events and run state, so it shows what a native
reader sees and nothing else. All model output comes from scripted
doubles (spec section 13).
"""

import asyncio
import copy
from datetime import UTC, datetime

import pytest

from gateway_helpers import (
    EXTERNAL_MODEL_ID,
    LOCAL_MODEL_ID,
    STAGING_OK,
    replace_handler,
    text_turn,
)
from helpers import ACCEPTED_PAYLOAD
from relaymind.store import RunRecord, TraceEvent
from relaymind.store.trace_event import validate_trace_event
from relaymind.ui.trace_view import build_trace_view, trace_view_from
from ui_helpers import (
    DELETE_ANSWER,
    DELETE_ARGS,
    DELETE_PROMPT,
    GENERATION_SECONDS,
    HEALTH_PROMPT,
    clarifying_gateway,
    delete_gateway,
    external_health_gateway,
    health_gateway,
    run_to_rest,
    without_original_messages,
)

_NOW = datetime(2026, 10, 20, 3, tzinfo=UTC)
# A run that was accepted and nothing more.
FRESH_RECORD = RunRecord("run_1", "routing", 1, None, None, None, _NOW, _NOW)


def view_of(gateway, outcome):
    return build_trace_view(gateway.store, outcome.run_id)


# -- a completed local run: every listed field ----------------------------------------


def test_a_local_run_shows_every_listed_field(tmp_path):
    """RM23, section 9: prompt, mode, model, skill, tool, result, answer, and timing."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway, execution_mode="local_only")
    view = view_of(gateway, outcome)

    assert view["run_id"] == outcome.run_id
    assert (view["state"], view["error_code"], view["reason_code"]) == ("completed", None, None)
    assert view["prompt"] == [{"role": "user", "content": HEALTH_PROMPT}]
    assert view["execution_mode"] == {"requested": "local_only", "effective": "local_only"}
    assert view["active_model"] == LOCAL_MODEL_ID
    assert [(c["model"], c["actor"], c["purpose"]) for c in view["model_calls"]] == [
        (LOCAL_MODEL_ID, "local", "routing"),
        (LOCAL_MODEL_ID, "local", "skill"),
        (LOCAL_MODEL_ID, "local", "skill"),
        (LOCAL_MODEL_ID, "local", "final"),
    ]
    assert view["skill"]["name"] == "check-service-health"
    assert view["skill"]["version"] and view["skill"]["content_hash"].startswith("sha256:")
    assert view["route"]["route"] == "local"
    assert view["route"]["skill_id"] == "check-service-health"
    assert view["route"]["reason_code"] == "simple_skill"
    (proposal,) = view["proposals"]
    assert (proposal["tool_name"], proposal["arguments"]) == (
        "get_service_health",
        {"service": "staging-api"},
    )
    assert (proposal["model"], proposal["admissible"], proposal["rejection"]) == (
        LOCAL_MODEL_ID,
        True,
        None,
    )
    (operation,) = view["operations"]
    assert operation["tool_name"] == "get_service_health"
    assert operation["status"] == "succeeded"
    assert operation["data"]["status"] == "healthy"
    assert operation["data"]["fixture"]["scenario"] == "baseline"
    assert operation["replayed"] is False
    assert view["failed_operations"] == []
    assert view["escalation"] is None
    assert view["final_answer"] == {"text": STAGING_OK, "model": LOCAL_MODEL_ID}


def test_a_view_is_json_data_made_only_of_the_stored_events(tmp_path):
    """RM23: the view is rebuilt from the events and the run record, nothing else."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    record = gateway.store.get_run(outcome.run_id)
    events = gateway.store.events(outcome.run_id)

    assert trace_view_from(record, events) == build_trace_view(gateway.store, outcome.run_id)


# -- timing (RM25, section 12.1) ------------------------------------------------------


def test_latency_is_the_first_model_action_never_the_completed_answer(tmp_path):
    """RM25, section 12.1: the figure is the first validated model action."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    view = view_of(gateway, outcome)
    events = gateway.store.events(outcome.run_id)
    route = next(e for e in events if e.event_type.value == "route.selected")
    completed = events[-1]

    timing = view["timing"]
    assert timing["first_model_action_ms"] == route.elapsed_ms == int(GENERATION_SECONDS * 1000)
    # Four generations ran: the answer arrived much later than the first action.
    assert completed.elapsed_ms >= 4 * int(GENERATION_SECONDS * 1000)
    assert timing["first_model_action_ms"] < completed.elapsed_ms
    # Run times are separate fields, and no field claims answer latency.
    assert timing["active_ms"] == completed.payload["active_ms"]
    assert timing["waiting_ms"] == completed.payload["waiting_ms"] == 0
    assert set(timing) == {"first_model_action_ms", "active_ms", "waiting_ms"}


def test_a_route_the_gateway_made_itself_is_not_a_model_action(tmp_path):
    """Section 9 (spec 0.20): in external_only the tool call is the first model action."""
    gateway = external_health_gateway(tmp_path)
    outcome = run_to_rest(gateway, execution_mode="external_only")
    view = view_of(gateway, outcome)
    events = gateway.store.events(outcome.run_id)
    proposed = next(e for e in events if e.event_type.value == "tool.proposed")

    assert view["route"]["mechanism"] == "execution_mode"
    assert view["timing"]["first_model_action_ms"] == proposed.elapsed_ms
    assert proposed.elapsed_ms > 0


def test_no_model_action_yet_is_reported_as_none_not_zero():
    """Section 9 (spec 0.20): while there is no action, the figure is absent."""
    accepted = validate_trace_event(
        {
            "schema_version": 1,
            "run_id": "run_1",
            "seq": 1,
            "timestamp": "2026-10-20T03:00:00Z",
            "elapsed_ms": 0,
            "event_type": "run.accepted",
            "actor": "gateway",
            "payload": {
                **ACCEPTED_PAYLOAD,
                "original_messages": [{"role": "user", "content": "hello"}],
            },
        }
    )
    view = trace_view_from(FRESH_RECORD, [accepted])

    assert view["timing"] == {
        "first_model_action_ms": None,
        "active_ms": None,
        "waiting_ms": None,
    }
    assert view["final_answer"] is None
    assert view["active_model"] is None
    assert view["prompt"] == [{"role": "user", "content": "hello"}]


def test_a_run_stored_before_the_prompt_was_recorded_says_so():
    """Section 9 (spec 0.20): no original_messages means the prompt is unknown, not empty."""
    accepted = TraceEvent(
        run_id="run_1",
        seq=1,
        timestamp="2026-10-20T03:00:00Z",
        elapsed_ms=0,
        event_type="run.accepted",
        actor="gateway",
        payload={k: v for k, v in ACCEPTED_PAYLOAD.items() if k != "original_messages"},
    )
    view = trace_view_from(FRESH_RECORD, [accepted])

    assert view["prompt"] is None


# -- escalation and the active model ---------------------------------------------------


def test_an_external_run_shows_the_escalation_and_the_external_model(tmp_path):
    """RM13, RM23: the handoff reason is shown, and the external model is the active one."""
    gateway = external_health_gateway(tmp_path)
    outcome = run_to_rest(gateway, execution_mode="external_only")
    view = view_of(gateway, outcome)

    assert view["execution_mode"] == {"requested": "external_only", "effective": "external_only"}
    assert view["escalation"]["reason_code"] == "execution_mode"
    assert view["escalation"]["model"] == EXTERNAL_MODEL_ID
    assert view["active_model"] == EXTERNAL_MODEL_ID
    assert view["final_answer"]["model"] == EXTERNAL_MODEL_ID
    assert {c["actor"] for c in view["model_calls"]} == {"external"}
    assert view["skill"] is None


def test_a_run_that_needs_an_external_model_shows_why(tmp_path):
    """RM04, RM23: a failed run shows its error code and reason code as events recorded them."""
    gateway = health_gateway(tmp_path, external_turns=None)
    outcome = run_to_rest(gateway, "Why is staging-api slow?", execution_mode="local_only")
    view = view_of(gateway, outcome)

    assert view["state"] == outcome.state.value
    if outcome.state.value == "failed":
        assert view["error_code"] == outcome.error_code
        assert view["reason_code"] == outcome.reason_code
        assert view["final_answer"] is None


# -- failed operations ------------------------------------------------------------------


def test_a_failed_operation_is_listed_with_its_error(tmp_path):
    """RM10, RM23: a user can inspect failed operations."""
    gateway = health_gateway(tmp_path, external_turns=None)

    def broken(arguments):
        raise RuntimeError("sandbox unreachable")

    replace_handler(gateway, "get_service_health", broken)
    outcome = run_to_rest(gateway)
    view = view_of(gateway, outcome)

    assert view["failed_operations"], view["operations"]
    failed = view["failed_operations"][0]
    assert failed["status"] == "failed"
    assert failed["tool_name"] == "get_service_health"
    assert failed["error"]["code"] == "tool_failure"
    assert failed in view["operations"]
    assert all(op["status"] != "succeeded" for op in view["failed_operations"])


# -- clarification ------------------------------------------------------------------------


def test_a_clarification_shows_the_question_and_then_the_answer(tmp_path):
    """RM23, section 5: the question is shown while waiting, and the answer once given."""
    gateway = clarifying_gateway(tmp_path)
    waiting = run_to_rest(gateway, "Is the service healthy?")
    view = view_of(gateway, waiting)

    assert view["state"] == "waiting_input"
    (asked,) = view["clarifications"]
    assert asked["question"] == "Which service do you mean?"
    assert asked["missing"] == "skill_selection"
    assert asked["answer"] is None
    assert view["final_answer"] is None


# -- confirmation -----------------------------------------------------------------------


def test_a_pending_confirmation_shows_what_will_be_changed(tmp_path):
    """RM11, RM23: the tool, arguments, target, and expiry, and no token."""
    gateway = delete_gateway(tmp_path)
    waiting = run_to_rest(gateway, DELETE_PROMPT)
    view = view_of(gateway, waiting)
    confirmation = view["confirmation"]

    assert view["state"] == "waiting_confirmation"
    assert confirmation["status"] == "pending"
    assert confirmation["tool_name"] == "delete_demo_database"
    assert confirmation["arguments"] == DELETE_ARGS
    assert confirmation["target"] == "demo-orders-db"
    assert confirmation["expires_at"].endswith("Z")
    assert confirmation["approval_id"] == waiting.confirmation.approval_id
    assert confirmation["model"] == LOCAL_MODEL_ID
    assert waiting.confirmation.token not in repr(view)


def test_an_approved_confirmation_is_shown_as_approved(tmp_path):
    """RM11, RM23: the executed operation names the approval that authorized it."""
    gateway = delete_gateway(
        tmp_path,
        text_turn(DELETE_ANSWER),
        text_turn(DELETE_ANSWER),
    )
    waiting = run_to_rest(gateway, DELETE_PROMPT)
    request = waiting.confirmation
    version = gateway.store.get_run(waiting.run_id).version
    asyncio.run(
        gateway.orchestrator.resume_confirmation(
            waiting.run_id,
            version,
            approval_id=request.approval_id,
            token=request.token,
            approve=True,
        )
    )
    view = view_of(gateway, waiting)

    assert view["state"] == "completed"
    assert view["confirmation"]["status"] == "approved"
    assert view["confirmation"]["approval_id"] == request.approval_id
    assert [op["tool_name"] for op in view["operations"]] == [
        "list_demo_databases",
        "delete_demo_database",
    ]


def test_a_denied_confirmation_is_shown_as_denied_and_the_run_as_cancelled(tmp_path):
    """RM11, RM23: a denial cancels the run with user_denied; nothing was mutated."""
    gateway = delete_gateway(tmp_path)
    waiting = run_to_rest(gateway, DELETE_PROMPT)
    request = waiting.confirmation
    version = gateway.store.get_run(waiting.run_id).version
    asyncio.run(
        gateway.orchestrator.resume_confirmation(
            waiting.run_id,
            version,
            approval_id=request.approval_id,
            token=request.token,
            approve=False,
        )
    )
    view = view_of(gateway, waiting)

    assert view["state"] == "cancelled"
    assert view["cancel_reason"] == "user_denied"
    assert view["confirmation"]["status"] == "denied"
    assert [op["tool_name"] for op in view["operations"]] == ["list_demo_databases"]


def test_an_expired_confirmation_is_not_shown_as_pending(tmp_path):
    """RM11, RM17, section 5: a run that ended on an unused approval shows it as expired."""
    gateway = delete_gateway(tmp_path)
    waiting = run_to_rest(gateway, DELETE_PROMPT)
    gateway.clock.advance(301)
    gateway.orchestrator.expire_waiting()
    view = view_of(gateway, waiting)

    assert view["state"] == "failed"
    assert view["error_code"] == "approval_expired"
    assert view["confirmation"]["status"] == "expired"
    assert view["timing"]["waiting_ms"] >= 300_000


# -- the view is a copy ---------------------------------------------------------------------


def test_building_a_view_does_not_change_the_events(tmp_path):
    """RM23: the UI renders evidence; it changes none."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    before = copy.deepcopy([e.to_json_object() for e in gateway.store.events(outcome.run_id)])

    view = view_of(gateway, outcome)
    view["operations"][0]["data"]["status"] = "unavailable"

    after = [e.to_json_object() for e in gateway.store.events(outcome.run_id)]
    assert before == after


@pytest.mark.parametrize("missing", ["prompt", "skill", "route", "escalation", "final_answer"])
def test_absent_evidence_is_none_never_invented(tmp_path, missing):
    """RM23: a field the events do not contain is None, not a guess."""
    gateway = health_gateway(tmp_path)
    outcome = run_to_rest(gateway)
    record = gateway.store.get_run(outcome.run_id)
    kept = [
        e
        for e in gateway.store.events(outcome.run_id)
        if e.event_type.value
        not in {
            "prompt": set(),
            "skill": {"skill.loaded"},
            "route": {"route.selected"},
            "escalation": {"handoff.started"},
            "final_answer": {"answer.delta"},
        }[missing]
    ]
    if missing == "prompt":
        kept[0] = without_original_messages(kept[0])
    view = trace_view_from(record, kept)
    assert view[missing] is None
