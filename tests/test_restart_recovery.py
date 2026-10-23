"""AC12 restart part: interrupted runs and unknown outcomes (RM10, RM17, #8).

Spec 0.11 section 5 (restart recovery) and section 8 (run.failed). After
a process restart every unfinished run becomes failed with interrupted;
a pending write is marked unknown_outcome and never retried; pending
approvals expire; nothing is resumed or replayed. A "restart" here is a
new gateway over the same SQLite file. All model output comes from
scripted doubles (spec section 13); the sandbox is a disposable copy.
"""

import asyncio

import pytest

from gateway_helpers import (
    build_gateway,
    clarify_turn,
    health_tail,
    replace_handler,
    route_turn,
    tool_turn,
)
from helpers import ACCEPTED_PAYLOAD, FakeClock, of_type
from relaymind.orchestrator.local_loop import LocalOrchestrator
from relaymind.orchestrator.recovery import RecoveryError, recover_interrupted_runs
from relaymind.orchestrator.run_state import RunState, SlotUnavailableError, TerminalStateError
from relaymind.store.trace_event import EventType, validate_trace_event
from relaymind.tools import CallerContext
from relaymind.tools.executor import GATEWAY_OPERATION_MODEL, ToolExecutor, utc_timestamp
from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION

CALLER = CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION}))
DELETE = {"database_id": "demo-orders-db", "expected_revision": 3}
HEALTH = {"service": "staging-api"}


def restart(gateway, tmp_path, turns=(), **options):
    """A new gateway process over the same store file and clock."""
    gateway.store.close()
    return build_gateway(
        tmp_path, list(turns), clock=gateway.clock, caller=CALLER, external_turns=[], **options
    )


def count_calls(gateway, name):
    """Replace a tool handler with a counter; the list gets one item per call."""
    calls = []

    def handler(arguments):
        calls.append(arguments)
        raise AssertionError(f"{name} must not run during recovery")

    replace_handler(gateway, name, handler)
    return calls


def unfinished_run(gateway, state=RunState.LOCAL_RUNNING):
    """A run left in an active state, as a crash leaves it."""
    machine = gateway.orchestrator.machine
    record = machine.create(accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD))
    for step in (RunState.ROUTING, state):
        record = machine.transition(record.run_id, record.version, step)
    return record


def pending_operation(gateway, run_id, tool_name, arguments):
    operation_id = ToolExecutor.operation_id(run_id, tool_name, arguments)
    gateway.store.record_tool_operation(
        run_id,
        operation_id=operation_id,
        proposal_id=f"prop_{tool_name}",
        tool_name=tool_name,
        model="double-qwen3-0.6b",
        arguments=arguments,
    )
    return operation_id


def test_a_recovered_prefetch_operation_names_no_model(tmp_path):
    """RM22, RM29: an operation the gateway made itself is recovered with model null.

    The store keeps an internal label for it, because its column is not
    nullable; the recovered tool.finished is a valid trace event with
    actor gateway and no model, like the events the call wrote before the crash.
    """
    old = build_gateway(tmp_path, [], caller=CALLER, external_turns=[])
    run = unfinished_run(old)
    operation_id = ToolExecutor.operation_id(run.run_id, "get_service_health", HEALTH)
    old.store.record_tool_operation(
        run.run_id,
        operation_id=operation_id,
        proposal_id="prop_prefetch",
        tool_name="get_service_health",
        model=GATEWAY_OPERATION_MODEL,
        arguments=HEALTH,
    )
    new = restart(old, tmp_path)
    new.orchestrator.recover_after_restart()
    (finished,) = of_type(new.store.events(run.run_id), "tool.finished")
    assert finished.actor == "gateway" and finished.payload["model"] is None
    assert finished.payload["recovered"] is True and finished.payload["replayed"] is False
    # The event is valid as stored: the trace schema allows a null model for a gateway call.
    validate_trace_event(finished.to_json_object())


def test_unfinished_run_fails_as_interrupted_and_releases_the_slot(tmp_path):
    """RM17, section 5: an unfinished run becomes failed with interrupted."""
    old = build_gateway(tmp_path, [], caller=CALLER, external_turns=[])
    run = unfinished_run(old)
    assert old.store.active_run_id() == run.run_id
    new = restart(old, tmp_path)

    report = new.orchestrator.recover_after_restart()

    record = new.store.get_run(run.run_id)
    assert (record.state, record.error_code) == ("failed", "interrupted")
    assert [r.run_id for r in report.runs] == [run.run_id]
    assert report.runs[0].interrupted_state == "local_running"
    assert new.store.active_run_id() is None
    (failed,) = of_type(new.store.events(run.run_id), "run.failed")
    assert failed.payload["error_code"] == "interrupted"
    assert failed.payload["interrupted_state"] == "local_running"
    assert failed.payload["model_calls"] == []
    assert failed.payload["external_calls"] == 0
    assert failed.payload["settled_operations"] == []
    # Recovery calls no model and resumes no loop (RM01, section 5).
    assert new.adapter.requests == [] and new.external.requests == []


@pytest.mark.parametrize(
    "state",
    [RunState.ACCEPTED, RunState.ROUTING, RunState.EXTERNAL_RUNNING],
)
def test_every_unfinished_state_is_recovered(tmp_path, state):
    """RM17, section 5: any non-terminal state fails as interrupted."""
    old = build_gateway(tmp_path, [], caller=CALLER, external_turns=[])
    machine = old.orchestrator.machine
    record = machine.create(accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD))
    if state is not RunState.ACCEPTED:
        record = machine.transition(record.run_id, record.version, RunState.ROUTING)
    if state is RunState.EXTERNAL_RUNNING:
        record = machine.transition(record.run_id, record.version, state)
    new = restart(old, tmp_path)

    new.orchestrator.recover_after_restart()

    record = new.store.get_run(record.run_id)
    assert (record.state, record.error_code) == ("failed", "interrupted")


def test_pending_write_is_unknown_outcome_and_a_pending_read_is_failed(tmp_path):
    """RM10, section 5: a pending write is unknown_outcome, never retried."""
    old = build_gateway(tmp_path, [], caller=CALLER, external_turns=[])
    run = unfinished_run(old)
    write = pending_operation(old, run.run_id, "delete_demo_database", DELETE)
    read = pending_operation(old, run.run_id, "get_service_health", HEALTH)
    new = restart(old, tmp_path)
    delete_calls = count_calls(new, "delete_demo_database")
    health_calls = count_calls(new, "get_service_health")

    report = new.orchestrator.recover_after_restart()

    write_record = new.store.get_tool_operation(write)
    read_record = new.store.get_tool_operation(read)
    assert write_record["status"] == "unknown_outcome"
    assert read_record["status"] == "failed"
    assert write_record["result"]["error"]["code"] == "tool_interrupted"
    assert write_record["finished_at"] is not None
    assert {(op.operation_id, op.status) for op in report.runs[0].operations} == {
        (write, "unknown_outcome"),
        (read, "failed"),
    }
    events = new.store.events(run.run_id)
    finished = of_type(events, "tool.finished")
    assert {e.payload["operation_id"] for e in finished} == {write, read}
    assert all(e.payload["recovered"] is True and e.payload["replayed"] is False for e in finished)
    assert all(e.payload["model"] == "double-qwen3-0.6b" for e in finished)
    # The tool.finished events come before the terminal event.
    assert events[-1].event_type is EventType.RUN_FAILED
    assert events[-1].payload["settled_operations"] == [
        {"operation_id": write, "status": "unknown_outcome"},
        {"operation_id": read, "status": "failed"},
    ]
    # No handler ran (RM10, RM14).
    assert delete_calls == [] and health_calls == []


def test_a_settled_write_is_replayed_never_retried(tmp_path):
    """RM10: the same call after recovery gets the recorded unknown_outcome."""
    old = build_gateway(tmp_path, [], caller=CALLER, external_turns=[])
    run = unfinished_run(old)
    pending_operation(old, run.run_id, "delete_demo_database", DELETE)
    new = restart(old, tmp_path)
    calls = count_calls(new, "delete_demo_database")
    new.orchestrator.recover_after_restart()

    executor = new.orchestrator.executor
    call = executor.validate(
        run.run_id,
        "delete_demo_database",
        DELETE,
        available={"delete_demo_database"},
    )
    execution = asyncio.run(executor.execute(call, proposal_id="again", model="m"))

    assert execution.replayed is True
    assert execution.result.status.value == "unknown_outcome"
    assert calls == []


def test_an_operation_of_a_removed_tool_is_unknown_outcome(tmp_path):
    """RM10: a tool that is no longer registered may have written; it is not failed."""
    old = build_gateway(tmp_path, [], caller=CALLER, external_turns=[])
    run = unfinished_run(old)
    old.store.record_tool_operation(
        run.run_id,
        operation_id="op_removed",
        proposal_id="p",
        tool_name="retired_tool",
        model="m",
        arguments={},
    )
    new = restart(old, tmp_path)

    new.orchestrator.recover_after_restart()

    assert new.store.get_tool_operation("op_removed")["status"] == "unknown_outcome"


def test_pending_approval_expires_on_restart(tmp_path):
    """RM11, section 5: a waiting confirmation is failed and its approval is dead."""
    old = build_gateway(
        tmp_path,
        [
            route_turn("delete-demo-database"),
            tool_turn("list_demo_databases", call_id="call_list"),
            tool_turn("delete_demo_database", call_id="call_delete", **DELETE),
        ],
        caller=CALLER,
        external_turns=[],
    )
    waiting = asyncio.run(old.orchestrator.run_text("Delete the demo-orders-db sandbox database."))
    assert waiting.state is RunState.WAITING_CONFIRMATION
    request = waiting.confirmation
    version = old.store.get_run(waiting.run_id).version
    assert old.store.get_pending_approval(waiting.run_id) is not None
    new = restart(old, tmp_path)
    delete_calls = count_calls(new, "delete_demo_database")

    report = new.orchestrator.recover_after_restart()

    assert report.runs[0].interrupted_state == "waiting_confirmation"
    assert report.runs[0].expired_approvals == 1
    record = new.store.get_run(waiting.run_id)
    assert (record.state, record.error_code) == ("failed", "interrupted")
    assert new.store.get_pending_approval(waiting.run_id) is None
    # The token no longer works for the version it was bound to (RM11).
    assert (
        new.store.consume_approval(
            waiting.run_id,
            token=request.token,
            tool_name="delete_demo_database",
            arguments=DELETE,
            target="demo-orders-db",
            state_version=version,
        )
        is False
    )
    with pytest.raises(TerminalStateError):
        asyncio.run(
            new.orchestrator.resume_confirmation(
                waiting.run_id,
                record.version,
                approval_id=request.approval_id,
                token=request.token,
                approve=True,
            )
        )
    assert delete_calls == []
    (failed,) = of_type(new.store.events(waiting.run_id), "run.failed")
    assert failed.payload["interrupted_state"] == "waiting_confirmation"
    assert failed.payload["model_calls"]  # rebuilt from the run's model.started events
    assert failed.payload["external_calls"] == 0


def test_approval_expiry_is_recorded_at_the_restart_time(tmp_path):
    """RM11: the approval record shows it ended at recovery, not at its old expiry."""
    old = build_gateway(
        tmp_path,
        [
            route_turn("delete-demo-database"),
            tool_turn("list_demo_databases", call_id="call_list"),
            tool_turn("delete_demo_database", call_id="call_delete", **DELETE),
        ],
        caller=CALLER,
        external_turns=[],
    )
    waiting = asyncio.run(old.orchestrator.run_text("Delete the demo-orders-db sandbox database."))
    old.clock.advance(30)
    new = restart(old, tmp_path)
    recovered_at = new.clock.utc_now()

    new.orchestrator.recover_after_restart()

    row = new.store._conn.execute(
        "SELECT expires_at, consumed_at FROM approvals WHERE run_id = ?", (waiting.run_id,)
    ).fetchone()
    assert row["consumed_at"] is None
    assert row["expires_at"] == recovered_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_waiting_input_run_is_failed_as_interrupted(tmp_path):
    """RM17, section 5: a clarification wait does not survive a restart."""
    old = build_gateway(tmp_path, [clarify_turn()], caller=CALLER, external_turns=[])
    waiting = asyncio.run(old.orchestrator.run_text("Is it healthy?"))
    assert waiting.state is RunState.WAITING_INPUT
    new = restart(old, tmp_path)

    new.orchestrator.recover_after_restart()

    record = new.store.get_run(waiting.run_id)
    assert (record.state, record.error_code) == ("failed", "interrupted")
    with pytest.raises(TerminalStateError):
        asyncio.run(new.orchestrator.resume(waiting.run_id, record.version, "staging-api"))


def test_terminal_runs_are_untouched_and_recovery_is_idempotent(tmp_path):
    """RM17: terminal runs never change; a second recovery finds nothing."""
    old = build_gateway(tmp_path, [clarify_turn()], caller=CALLER, external_turns=[])
    waiting = asyncio.run(old.orchestrator.run_text("Is it healthy?"))
    cancelled = old.orchestrator.machine.cancel(
        waiting.run_id, old.store.get_run(waiting.run_id).version, reason="user_denied"
    )
    done = unfinished_run(old)
    new = restart(old, tmp_path)
    before = new.store.events(cancelled.run_id)

    first = new.orchestrator.recover_after_restart()
    second = new.orchestrator.recover_after_restart()

    assert [r.run_id for r in first.runs] == [done.run_id]
    assert second.runs == ()
    assert new.store.get_run(cancelled.run_id).state == "cancelled"
    assert new.store.events(cancelled.run_id) == before
    assert len(of_type(new.store.events(done.run_id), "run.failed")) == 1


def test_a_new_run_can_start_after_recovery(tmp_path):
    """Section 11: the interrupted run no longer holds the one execution slot."""
    old = build_gateway(tmp_path, [], caller=CALLER, external_turns=[])
    unfinished_run(old)
    new = restart(
        old,
        tmp_path,
        [route_turn("check-service-health"), *health_tail()],
    )
    with pytest.raises(SlotUnavailableError):
        asyncio.run(new.orchestrator.run_text("Is staging-api healthy right now?"))

    new.orchestrator.recover_after_restart()

    outcome = asyncio.run(new.orchestrator.run_text("Is staging-api healthy right now?"))
    assert outcome.state is RunState.COMPLETED


def test_recovery_refuses_while_this_process_tracks_waiting_runs(tmp_path):
    """Section 5: recovery is a startup step, never run beside live runs."""
    gateway = build_gateway(tmp_path, [clarify_turn()], caller=CALLER, external_turns=[])
    asyncio.run(gateway.orchestrator.run_text("Is it healthy?"))

    with pytest.raises(RuntimeError, match="startup"):
        gateway.orchestrator.recover_after_restart()


def test_a_run_that_cannot_be_recovered_does_not_stop_the_others(tmp_path):
    """Section 5: one failing run is raised after the others are recovered."""
    old = build_gateway(tmp_path, [], caller=CALLER, external_turns=[])
    first = unfinished_run(old)
    # The slot allows one active run; a waiting run is a second unfinished one.
    machine = old.orchestrator.machine
    second = machine.create(accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD))
    new = restart(old, tmp_path)
    original = new.store.set_state

    def flaky(run_id, **kwargs):
        if run_id == first.run_id:
            raise RuntimeError("disk full")
        return original(run_id, **kwargs)

    new.store.set_state = flaky

    with pytest.raises(ExceptionGroup):
        recover_interrupted_runs(new.store, new.orchestrator.machine, new.orchestrator.executor)

    assert new.store.get_run(second.run_id).state == "failed"
    # The failed run is rolled back whole: still unfinished, so a later recovery retries it.
    assert new.store.get_run(first.run_id).state == "local_running"


def test_recovery_refuses_after_this_process_accepted_a_run(tmp_path):
    """Section 5: recovery runs before the gateway accepts any run, not after."""
    gateway = build_gateway(
        tmp_path, [route_turn("check-service-health"), *health_tail()], caller=CALLER
    )
    outcome = asyncio.run(gateway.orchestrator.run_text("Is staging-api healthy right now?"))
    assert outcome.state is RunState.COMPLETED

    with pytest.raises(RuntimeError, match="startup"):
        gateway.orchestrator.recover_after_restart()


def test_recovery_never_fails_a_run_this_process_is_executing(tmp_path):
    """Section 5, RM17: a live local_running run is not interrupted by recovery."""
    gateway = build_gateway(
        tmp_path, [route_turn("check-service-health"), *health_tail()], caller=CALLER
    )
    original = gateway.tools.get("get_service_health").declaration.handler
    refused = []

    def handler(arguments):
        try:
            gateway.orchestrator.recover_after_restart()
        except RuntimeError as exc:
            refused.append(exc)
        return original(arguments)

    replace_handler(gateway, "get_service_health", handler)

    outcome = asyncio.run(gateway.orchestrator.run_text("Is staging-api healthy right now?"))

    assert len(refused) == 1
    assert outcome.state is RunState.COMPLETED
    assert of_type(gateway.store.events(outcome.run_id), "run.failed") == []


def test_recovery_report_survives_a_run_that_cannot_be_recovered(tmp_path):
    """Section 5: the runs that were recovered are still reported with the errors."""
    old = build_gateway(tmp_path, [], caller=CALLER, external_turns=[])
    first = unfinished_run(old)
    second = old.orchestrator.machine.create(
        accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD)
    )
    new = restart(old, tmp_path)
    original = new.store.set_state

    def flaky(run_id, **kwargs):
        if run_id == first.run_id:
            raise RuntimeError("disk full")
        return original(run_id, **kwargs)

    new.store.set_state = flaky

    with pytest.raises(RecoveryError) as caught:
        new.orchestrator.recover_after_restart()

    assert [r.run_id for r in caught.value.report.runs] == [second.run_id]
    assert [str(e) for e in caught.value.exceptions] == ["disk full"]


def test_recovery_uses_the_orchestrator_clock(tmp_path):
    """RM25: recovered tool results use the clock the orchestrator was given."""
    old = build_gateway(tmp_path, [], caller=CALLER, external_turns=[])
    run = unfinished_run(old)
    write = pending_operation(old, run.run_id, "delete_demo_database", DELETE)
    new = restart(old, tmp_path)
    other = FakeClock()
    other.advance(3600)
    orchestrator = LocalOrchestrator(
        new.orchestrator.model,
        skills=new.skills,
        tools=new.tools,
        store=new.store,
        policy=new.orchestrator.policy,
        caller=CALLER,
        clock=other,
    )

    orchestrator.recover_after_restart()

    observed = new.store.get_tool_operation(write)["result"]["observed_at"]
    assert observed == utc_timestamp(other.utc_now())


def test_a_run_that_moved_after_the_listing_is_read_again(tmp_path):
    """RM17: recovery reads each run inside its transaction, not from a stale listing."""
    old = build_gateway(tmp_path, [], caller=CALLER, external_turns=[])
    moved = unfinished_run(old)
    ended = old.orchestrator.machine.create(
        accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD)
    )
    new = restart(old, tmp_path)
    original = new.store.unfinished_runs

    def stale_listing(terminal_states):
        records = original(terminal_states)
        machine = new.orchestrator.machine
        machine.transition(moved.run_id, moved.version, RunState.EXTERNAL_RUNNING)
        record = new.store.get_run(ended.run_id)
        machine.cancel(ended.run_id, record.version, reason="user_denied")
        return records

    new.store.unfinished_runs = stale_listing

    report = new.orchestrator.recover_after_restart()

    assert [(r.run_id, r.interrupted_state) for r in report.runs] == [
        (moved.run_id, "external_running")
    ]
    assert new.store.get_run(ended.run_id).state == "cancelled"
    assert new.store.get_run(moved.run_id).state == "failed"


def test_a_void_approval_is_not_counted_as_expired(tmp_path):
    """RM11: an approval whose run moved past its state version is not pending."""
    old = build_gateway(tmp_path, [], caller=CALLER, external_turns=[])
    run = unfinished_run(old)
    old.store.create_approval(
        run.run_id,
        token="old",
        tool_name="delete_demo_database",
        arguments=DELETE,
        target="demo-orders-db",
        state_version=run.version,
    )
    record = old.orchestrator.machine.transition(
        run.run_id, run.version, RunState.WAITING_CONFIRMATION
    )
    old.store.create_approval(
        run.run_id,
        token="live",
        tool_name="delete_demo_database",
        arguments=DELETE,
        target="demo-orders-db",
        state_version=record.version,
    )
    new = restart(old, tmp_path)

    report = new.orchestrator.recover_after_restart()

    assert report.runs[0].expired_approvals == 1
