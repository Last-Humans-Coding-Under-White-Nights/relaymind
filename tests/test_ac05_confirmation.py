"""AC05: confirmation for a mutating demo action (RM11, RM10, RM17, #19).

AC05: delete a sandbox database, then approve, deny, replay, expire, or
alter arguments. Only a matching fresh approval executes, once; every
other case leaves the fixture untouched.

Spec 0.10 sections 4.3 (confirmation flow), 5 (confirmation and resume),
7.4 (resume with approval_id, token, and approve), and 8
(confirmation.required, run.cancelled). The approval token is returned
once to the caller and the store keeps only its hash. A textual yes in
chat is not an approval. All model output comes from scripted doubles
(spec section 13); the sandbox is a disposable copy.
"""

import asyncio
import dataclasses
import json

import pytest

from gateway_helpers import (
    EXTERNAL_MODEL_ID,
    LOCAL_MODEL_ID,
    build_gateway,
    executed_tools,
    route_turn,
    text_turn,
    tool_turn,
)
from helpers import of_type
from relaymind.adapters import Message, Role
from relaymind.orchestrator.local_loop import ApprovalRejectedError, ResumeInputError
from relaymind.orchestrator.run_state import (
    RunState,
    SlotUnavailableError,
    TerminalStateError,
)
from relaymind.store import StaleVersionError
from relaymind.store.trace_event import validate_trace_event
from relaymind.tools import CallerContext
from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION

CALLER = CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION}))
PROMPT = "Delete the demo-orders-db sandbox database."
DELETE = {"database_id": "demo-orders-db", "expected_revision": 3}
ANSWER = "Deleted demo-orders-db."


def delete_turns(*tail):
    """Route, list the databases, propose the delete, then the scripted tail."""
    return [
        route_turn("delete-demo-database"),
        tool_turn("list_demo_databases", call_id="call_list"),
        tool_turn("delete_demo_database", call_id="call_delete", **DELETE),
        *tail,
    ]


def gateway_for(tmp_path, *tail, **options):
    return build_gateway(
        tmp_path, delete_turns(*tail), caller=options.pop("caller", CALLER), **options
    )


def start(gateway, prompt=PROMPT, **options):
    outcome = asyncio.run(gateway.orchestrator.run_text(prompt, **options))
    return outcome, gateway.store.events(outcome.run_id)


def decide(gateway, waiting, *, approve=True, **overrides):
    """Resume a confirmation; overrides replace approval_id, token, or version."""
    request = waiting.confirmation
    version = overrides.pop("version", None)
    if version is None:
        version = gateway.store.get_run(waiting.run_id).version
    body = {"approval_id": request.approval_id, "token": request.token, "approve": approve}
    body.update(overrides)
    return asyncio.run(gateway.orchestrator.resume_confirmation(waiting.run_id, version, **body))


def database_ids(gateway):
    return [db["database_id"] for db in gateway.sandbox.list_databases()]


def waiting_gateway(tmp_path, *tail, **options):
    gateway = gateway_for(tmp_path, *tail, **options)
    waiting, events = start(gateway)
    assert waiting.state is RunState.WAITING_CONFIRMATION
    return gateway, waiting, events


APPROVED_TAIL = (text_turn(ANSWER), text_turn(ANSWER))


# --- the proposal suspends; nothing is mutated ----------------------------------


def test_a_mutating_proposal_waits_for_confirmation_and_mutates_nothing(tmp_path):
    """RM11, section 5: the run waits in waiting_confirmation, the fixture is intact."""
    gateway, waiting, events = waiting_gateway(tmp_path)

    record = gateway.store.get_run(waiting.run_id)
    assert record.state == "waiting_confirmation"
    assert waiting.answer is None
    assert executed_tools(events) == ["list_demo_databases"]
    assert "demo-orders-db" in database_ids(gateway)
    # Waiting releases the execution slot (section 5).
    assert gateway.store.active_run_id() is None
    proposal = [
        e
        for e in of_type(events, "tool.proposed")
        if e.payload["tool_name"] == "delete_demo_database"
    ]
    assert proposal[0].payload["admissible"] is True


def test_confirmation_required_binds_run_tool_arguments_target_and_version(tmp_path):
    """RM11, section 8: the event and the pending approval carry the binding."""
    gateway, waiting, events = waiting_gateway(tmp_path)

    (required,) = of_type(events, "confirmation.required")
    request = waiting.confirmation
    record = gateway.store.get_run(waiting.run_id)
    assert required.payload["approval_id"] == request.approval_id
    assert required.payload["tool_name"] == "delete_demo_database"
    assert required.payload["arguments"] == DELETE
    assert required.payload["target"] == "demo-orders-db"
    assert required.payload["state_version"] == record.version == request.state_version
    assert required.payload["model"] == LOCAL_MODEL_ID
    assert required.payload["expires_at"].endswith("Z")
    pending = gateway.store.get_pending_approval(waiting.run_id)
    assert pending["approval_id"] == request.approval_id
    assert pending["arguments"] == DELETE
    assert pending["target"] == "demo-orders-db"
    assert (pending["expires_at"] - pending["created_at"]).total_seconds() == 300
    validate_trace_event(required.to_json_object())


def test_the_token_is_never_traced_or_stored_in_plaintext(tmp_path):
    """RM11, RM12, section 5: the caller gets the token once; the store keeps a hash."""
    gateway, waiting, events = waiting_gateway(tmp_path)

    token = waiting.confirmation.token
    assert token and token not in repr(waiting)
    assert token not in json.dumps([e.to_json_object() for e in events])
    dump = "\n".join(gateway.store._conn.iterdump())
    assert token not in dump


def test_each_confirmation_gets_its_own_token(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _, first, _ = waiting_gateway(tmp_path / "a")
    _, second, _ = waiting_gateway(tmp_path / "b")
    assert first.confirmation.token != second.confirmation.token


# --- approve ----------------------------------------------------------------------


def test_a_matching_fresh_approval_executes_the_delete_once(tmp_path):
    """RM11, AC05: approve runs the delete once and the run completes."""
    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)

    outcome = decide(gateway, waiting)

    events = gateway.store.events(waiting.run_id)
    assert outcome.state is RunState.COMPLETED
    assert outcome.answer == ANSWER
    assert executed_tools(events) == ["list_demo_databases", "delete_demo_database"]
    assert "demo-orders-db" not in database_ids(gateway)
    started = [
        e
        for e in of_type(events, "tool.started")
        if e.payload["tool_name"] == "delete_demo_database"
    ]
    assert started[0].payload["approval_id"] == waiting.confirmation.approval_id
    assert gateway.store.get_run(waiting.run_id).state == "completed"
    assert gateway.store.active_run_id() is None


def test_the_resumed_run_opens_a_new_stage_inside_the_run_limits(tmp_path):
    """Spec 0.10 section 5: like a clarification resume, a new stage within the run limits."""
    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)
    decide(gateway, waiting)
    (completed,) = of_type(gateway.store.events(waiting.run_id), "run.completed")
    assert completed.payload["budget_usage"]["stages"] == 2
    assert completed.payload["external_calls"] == 0
    assert completed.payload["final_answer_model"] == LOCAL_MODEL_ID


def test_the_wait_counts_toward_waiting_time_not_active_time(tmp_path):
    """Section 12.1, RM22: a confirmation wait is waiting_ms, excluded from active_ms."""
    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)
    gateway.clock.advance(7)
    decide(gateway, waiting)
    (completed,) = of_type(gateway.store.events(waiting.run_id), "run.completed")
    assert completed.payload["waiting_ms"] == 7000
    assert completed.payload["active_ms"] == 0


def test_an_approved_replay_asks_for_no_second_confirmation(tmp_path):
    """RM10, RM11: the same call again gets the recorded outcome; no second delete."""
    again = tool_turn("delete_demo_database", call_id="call_again", **DELETE)
    gateway, waiting, _ = waiting_gateway(tmp_path, again, *APPROVED_TAIL)

    outcome = decide(gateway, waiting)

    events = gateway.store.events(waiting.run_id)
    assert outcome.state is RunState.COMPLETED
    assert len(of_type(events, "confirmation.required")) == 1
    assert executed_tools(events).count("delete_demo_database") == 1
    replayed = [e for e in of_type(events, "tool.finished") if e.payload["replayed"]]
    assert len(replayed) == 1


# --- deny ---------------------------------------------------------------------------


def test_a_denial_cancels_the_run_with_user_denied_and_mutates_nothing(tmp_path):
    """Section 5, AC05: deny ends cancelled with user_denied."""
    gateway, waiting, _ = waiting_gateway(tmp_path)
    gateway.clock.advance(3)

    outcome = decide(gateway, waiting, approve=False)

    events = gateway.store.events(waiting.run_id)
    record = gateway.store.get_run(waiting.run_id)
    assert outcome.state is RunState.CANCELLED
    assert outcome.answer is None
    assert (record.state, record.error_code) == ("cancelled", "user_denied")
    assert "demo-orders-db" in database_ids(gateway)
    assert "delete_demo_database" not in executed_tools(events)
    (cancelled,) = of_type(events, "run.cancelled")
    assert cancelled.payload["reason"] == "user_denied"
    assert cancelled.payload["approval_id"] == waiting.confirmation.approval_id
    assert cancelled.payload["waiting_ms"] == 3000
    assert gateway.store.active_run_id() is None
    validate_trace_event(cancelled.to_json_object())


def test_a_denied_run_never_restarts(tmp_path):
    gateway, waiting, _ = waiting_gateway(tmp_path)
    decide(gateway, waiting, approve=False)
    with pytest.raises(TerminalStateError):
        decide(gateway, waiting, approve=True)
    assert "demo-orders-db" in database_ids(gateway)


# --- replay -------------------------------------------------------------------------


def test_a_used_token_cannot_execute_again(tmp_path):
    """RM11, RM17, AC05: a replayed approval is 409 and runs nothing."""
    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)
    version = gateway.store.get_run(waiting.run_id).version
    decide(gateway, waiting)

    # Either version: the run is terminal and never restarts (409).
    with pytest.raises(TerminalStateError):
        decide(gateway, waiting, version=version)
    with pytest.raises(TerminalStateError):
        decide(gateway, waiting)
    events = gateway.store.events(waiting.run_id)
    assert executed_tools(events).count("delete_demo_database") == 1


def test_a_consumed_approval_is_single_use_in_the_store(tmp_path):
    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)
    decide(gateway, waiting)
    assert gateway.store.get_pending_approval(waiting.run_id) is None


# --- expire ---------------------------------------------------------------------------


@pytest.mark.parametrize("approve", [True, False], ids=["approve", "deny"])
def test_an_approval_older_than_five_minutes_fails_the_run(tmp_path, approve):
    """RM11, section 5, AC05: an expired approval executes nothing."""
    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)
    gateway.clock.advance(301)

    outcome = decide(gateway, waiting, approve=approve)

    events = gateway.store.events(waiting.run_id)
    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "approval_expired"
    assert gateway.store.get_run(waiting.run_id).error_code == "approval_expired"
    assert "demo-orders-db" in database_ids(gateway)
    assert "delete_demo_database" not in executed_tools(events)
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["error_code"] == "approval_expired"
    assert failed.payload["waiting_ms"] == 301000
    assert gateway.store.active_run_id() is None


def test_an_approval_at_exactly_five_minutes_is_expired(tmp_path):
    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)
    gateway.clock.advance(300)
    assert decide(gateway, waiting).error_code == "approval_expired"


def test_a_wait_inside_five_minutes_still_approves(tmp_path):
    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)
    gateway.clock.advance(299)
    assert decide(gateway, waiting).state is RunState.COMPLETED


def test_the_sweep_fails_an_expired_confirmation(tmp_path):
    """Section 5: expired confirmations are swept like expired clarifications."""
    gateway, waiting, _ = waiting_gateway(tmp_path)
    assert gateway.orchestrator.expire_waiting() == []
    gateway.clock.advance(301)

    (expired,) = gateway.orchestrator.expire_waiting()

    assert (expired.run_id, expired.error_code) == (waiting.run_id, "approval_expired")
    assert gateway.store.get_run(waiting.run_id).state == "failed"
    assert "demo-orders-db" in database_ids(gateway)
    with pytest.raises(TerminalStateError):
        decide(gateway, waiting)


# --- altered arguments or target ----------------------------------------------------


def test_an_approval_for_one_database_does_not_authorize_another(tmp_path):
    """RM11, AC05: a changed target is a new proposal with its own confirmation."""
    other = {"database_id": "demo-analytics-db", "expected_revision": 4}
    gateway, waiting, _ = waiting_gateway(
        tmp_path, tool_turn("delete_demo_database", call_id="call_other", **other), *APPROVED_TAIL
    )

    second = decide(gateway, waiting)

    assert second.state is RunState.WAITING_CONFIRMATION
    assert second.confirmation.approval_id != waiting.confirmation.approval_id
    assert second.confirmation.target == "demo-analytics-db"
    # The first delete ran; the second has not, and the first token is no use for it.
    assert database_ids(gateway) == ["demo-analytics-db"]
    with pytest.raises(ApprovalRejectedError):
        decide(gateway, second, token=waiting.confirmation.token)
    with pytest.raises(ApprovalRejectedError):
        decide(gateway, second, approval_id=waiting.confirmation.approval_id)
    assert database_ids(gateway) == ["demo-analytics-db"]


def test_changed_arguments_in_the_pending_call_void_the_approval(tmp_path):
    """RM11: the gateway presents the arguments it will run; a mismatch consumes nothing."""
    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)
    pending = gateway.orchestrator._waiting[waiting.run_id].confirmation
    altered = dataclasses.replace(
        pending.admitted, arguments={"database_id": "demo-analytics-db", "expected_revision": 3}
    )
    gateway.orchestrator._waiting[waiting.run_id].confirmation = dataclasses.replace(
        pending, admitted=altered
    )

    with pytest.raises(ApprovalRejectedError):
        decide(gateway, waiting)

    assert database_ids(gateway) == ["demo-orders-db", "demo-analytics-db"]
    assert gateway.store.get_run(waiting.run_id).state == "waiting_confirmation"


def test_a_fixture_that_changed_after_the_proposal_is_a_conflict_with_no_mutation(tmp_path):
    """RM11, section 9: the fixture revision is part of the binding; a stale one deletes nothing."""
    gateway, waiting, _ = waiting_gateway(tmp_path)
    gateway.sandbox.delete_database("demo-analytics-db", expected_revision=3)

    outcome = decide(gateway, waiting)

    events = gateway.store.events(waiting.run_id)
    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "needs_external"
    assert outcome.reason_code == "tool_failure"
    assert database_ids(gateway) == ["demo-orders-db"]
    (finished,) = [
        e
        for e in of_type(events, "tool.finished")
        if e.payload["tool_name"] == "delete_demo_database"
    ]
    assert finished.payload["result"]["error"]["code"] == "revision_conflict"


# --- wrong approvals and bad bodies -------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [{"token": "not-the-token"}, {"approval_id": "approval_other"}],
    ids=["wrong-token", "wrong-approval-id"],
)
def test_a_wrong_token_or_approval_id_is_403_and_the_run_keeps_waiting(tmp_path, overrides):
    """RM11: nothing runs, nothing is consumed; the right token still works."""
    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)
    version = gateway.store.get_run(waiting.run_id).version

    with pytest.raises(ApprovalRejectedError) as caught:
        decide(gateway, waiting, **overrides)

    assert caught.value.http_status == 403
    assert caught.value.code == "approval_invalid"
    record = gateway.store.get_run(waiting.run_id)
    assert (record.state, record.version) == ("waiting_confirmation", version)
    assert "demo-orders-db" in database_ids(gateway)
    assert decide(gateway, waiting).state is RunState.COMPLETED


def test_a_wrong_token_cannot_deny_either(tmp_path):
    gateway, waiting, _ = waiting_gateway(tmp_path)
    with pytest.raises(ApprovalRejectedError):
        decide(gateway, waiting, approve=False, token="guess")
    assert gateway.store.get_run(waiting.run_id).state == "waiting_confirmation"


@pytest.mark.parametrize(
    "overrides",
    [{"approve": "yes"}, {"approve": None}, {"token": ""}, {"token": 7}, {"approval_id": "  "}],
    ids=["str-approve", "null-approve", "blank-token", "int-token", "blank-id"],
)
def test_a_malformed_resume_body_is_400_and_the_run_keeps_waiting(tmp_path, overrides):
    """Sections 7.4, 11: invalid_resume_input leaves the run and its version unchanged."""
    gateway, waiting, _ = waiting_gateway(tmp_path)
    version = gateway.store.get_run(waiting.run_id).version

    with pytest.raises(ResumeInputError) as caught:
        decide(gateway, waiting, **overrides)

    assert caught.value.http_status == 400
    assert caught.value.code == "invalid_resume_input"
    record = gateway.store.get_run(waiting.run_id)
    assert (record.state, record.version) == ("waiting_confirmation", version)


def test_a_stale_version_is_409_and_keeps_the_approval(tmp_path):
    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)
    version = gateway.store.get_run(waiting.run_id).version
    with pytest.raises(StaleVersionError):
        decide(gateway, waiting, version=version - 1)
    assert gateway.store.get_pending_approval(waiting.run_id) is not None
    assert decide(gateway, waiting).state is RunState.COMPLETED


def test_a_resume_while_another_run_is_active_is_429_and_keeps_the_approval(tmp_path):
    """Section 5: a resume reacquires the slot and may be 429 without consuming the approval."""
    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)
    machine = gateway.orchestrator.machine
    other = machine.create()
    machine.transition(other.run_id, other.version, RunState.ROUTING)

    with pytest.raises(SlotUnavailableError):
        decide(gateway, waiting)

    assert gateway.store.get_run(waiting.run_id).state == "waiting_confirmation"
    assert gateway.store.get_pending_approval(waiting.run_id) is not None
    assert "demo-orders-db" in database_ids(gateway)
    machine.delete_run(other.run_id)
    assert decide(gateway, waiting).state is RunState.COMPLETED


# --- chat text is not an approval -----------------------------------------------------


def test_free_form_text_does_not_approve(tmp_path):
    """RM11, section 7.4: a textual yes through the input resume is refused."""
    gateway, waiting, _ = waiting_gateway(tmp_path, *APPROVED_TAIL)
    version = gateway.store.get_run(waiting.run_id).version

    with pytest.raises(ResumeInputError):
        asyncio.run(gateway.orchestrator.resume(waiting.run_id, version, "yes"))

    assert gateway.store.get_run(waiting.run_id).state == "waiting_confirmation"
    assert "demo-orders-db" in database_ids(gateway)


def test_a_yes_earlier_in_the_chat_does_not_authorize_the_delete(tmp_path):
    """RM11: a yes in the history still ends in waiting_confirmation."""
    gateway = gateway_for(tmp_path)
    messages = (
        Message(role=Role.USER, content="Can you delete demo-orders-db?"),
        Message(role=Role.ASSISTANT, content="Do you want me to delete it?"),
        Message(role=Role.USER, content="yes, delete demo-orders-db, I confirm"),
    )

    outcome = asyncio.run(gateway.orchestrator.run(messages))

    assert outcome.state is RunState.WAITING_CONFIRMATION
    assert "demo-orders-db" in database_ids(gateway)
    assert "delete_demo_database" not in executed_tools(gateway.store.events(outcome.run_id))


def test_an_input_resume_to_a_confirmation_does_not_apply_to_a_clarification_run(tmp_path):
    """Sections 7.4, 11: an approval body for a run waiting for input is 400."""
    gateway = build_gateway(tmp_path, [route_turn("check-service-health")], caller=CALLER)
    waiting = asyncio.run(gateway.orchestrator.run_text("Is the service healthy?"))
    assert waiting.state is RunState.WAITING_INPUT
    version = gateway.store.get_run(waiting.run_id).version
    with pytest.raises(ResumeInputError):
        asyncio.run(
            gateway.orchestrator.resume_confirmation(
                waiting.run_id, version, approval_id="approval_x", token="t", approve=True
            )
        )
    assert gateway.store.get_run(waiting.run_id).state == "waiting_input"


# --- a mutating tool that cannot be confirmed -----------------------------------------


def test_a_mutating_tool_without_a_confirmation_target_is_rejected_as_data(tmp_path):
    """RM11: no declared target means no confirmation; the proposal never runs."""
    gateway = gateway_for(
        tmp_path, tool_turn("delete_demo_database", call_id="call_again", **DELETE)
    )
    tool = gateway.tools.get("delete_demo_database")
    declaration = dataclasses.replace(tool.declaration, confirmation_target=None)
    gateway.tools._tools["delete_demo_database"] = dataclasses.replace(
        tool, declaration=declaration
    )

    outcome, events = start(gateway)

    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "needs_external"
    rejections = [
        e.payload["rejection"]["code"]
        for e in of_type(events, "tool.proposed")
        if e.payload["tool_name"] == "delete_demo_database"
    ]
    assert rejections == ["confirmation_unsupported", "confirmation_unsupported"]
    assert not of_type(events, "confirmation.required")
    assert "demo-orders-db" in database_ids(gateway)


# --- the external stage --------------------------------------------------------------


def external_gateway(tmp_path, *external_turns):
    return build_gateway(
        tmp_path,
        [],
        caller=CALLER,
        external_turns=[
            tool_turn("list_demo_databases", call_id="e_list"),
            tool_turn("delete_demo_database", call_id="e_delete", **DELETE),
            *external_turns,
        ],
    )


def test_the_external_stage_asks_for_the_same_confirmation(tmp_path):
    """RM11, section 4.4: external_running suspends and returns to external_running."""
    gateway = external_gateway(tmp_path, text_turn(ANSWER))
    waiting, events = start(gateway, execution_mode="external_only")

    assert waiting.state is RunState.WAITING_CONFIRMATION
    (required,) = of_type(events, "confirmation.required")
    assert required.payload["model"] == EXTERNAL_MODEL_ID
    assert "demo-orders-db" in database_ids(gateway)
    assert gateway.store.get_run(waiting.run_id).resume_state == "external_running"

    outcome = decide(gateway, waiting)

    events = gateway.store.events(waiting.run_id)
    assert outcome.state is RunState.COMPLETED
    assert outcome.answer == ANSWER
    assert "demo-orders-db" not in database_ids(gateway)
    assert executed_tools(events) == ["list_demo_databases", "delete_demo_database"]
    (completed,) = of_type(events, "run.completed")
    assert completed.payload["final_answer_model"] == EXTERNAL_MODEL_ID
    assert [m["actor"] for m in completed.payload["model_calls"]] == ["external"] * 3


def test_a_denial_in_the_external_stage_cancels_the_run(tmp_path):
    gateway = external_gateway(tmp_path)
    waiting, _ = start(gateway, execution_mode="external_only")

    outcome = decide(gateway, waiting, approve=False)

    assert outcome.state is RunState.CANCELLED
    assert gateway.store.get_run(waiting.run_id).error_code == "user_denied"
    assert "demo-orders-db" in database_ids(gateway)
