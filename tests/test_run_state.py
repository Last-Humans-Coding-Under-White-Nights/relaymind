"""Run state machine (spec section 5; RM17).

Section 5 lists ten run states and the allowed next behavior of each.
RM17: every transition is atomic against a run state version; resume
and cancel reject stale versions with HTTP 409; no transition from a
terminal state restarts execution, and a new attempt is a new run linked
to the previous one. Section 5: waiting releases the active execution
slot, and a resume must reacquire it and may return 429. Section 11:
one active run in the baseline. RM22: a terminal transition carries
run.completed, run.failed, or run.cancelled. Issue #6.
"""

from datetime import timedelta

import pytest

from helpers import ACCEPTED_PAYLOAD, TERMINAL_EVIDENCE
from relaymind.orchestrator.run_state import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    WAITING_STATES,
    InvalidTransitionError,
    RunState,
    RunStateMachine,
    SlotUnavailableError,
    TerminalStateError,
)
from relaymind.store import RunStore, StaleVersionError
from relaymind.store.trace_event import EventType

S = RunState

# Section 5 table, read state by state. Failure and cancellation are
# reachable from every non-terminal state: section 5 fails unfinished
# runs on restart and expired waits, section 11 fails on exhausted
# budgets, and RM21 accepts cancel for any unfinished run.
EXPECTED = {
    # "Validate configuration and request, then route or fail."
    S.ACCEPTED: {S.ROUTING},
    # "Select local, external, clarification, or an approved parallel workflow."
    S.ROUTING: {S.LOCAL_RUNNING, S.EXTERNAL_RUNNING, S.WAITING_INPUT, S.PARALLEL_RUNNING},
    # "Propose or execute permitted tools, finish, ask, or escalate."
    S.LOCAL_RUNNING: {S.COMPLETED, S.WAITING_INPUT, S.WAITING_CONFIRMATION, S.EXTERNAL_RUNNING},
    # "Continue with evidence and remaining permissions, then finish or ask."
    S.EXTERNAL_RUNNING: {S.COMPLETED, S.WAITING_INPUT, S.WAITING_CONFIRMATION},
    # "Execute approved read-only branches, join, and transfer final ownership."
    S.PARALLEL_RUNNING: {S.EXTERNAL_RUNNING},
    # "Suspend inference and tools; resume on explicit new input."
    S.WAITING_INPUT: {S.ROUTING, S.LOCAL_RUNNING, S.EXTERNAL_RUNNING},
    # "Suspend the proposed mutation; resume only with a valid approval."
    S.WAITING_CONFIRMATION: {S.LOCAL_RUNNING, S.EXTERNAL_RUNNING},
}


def test_the_ten_section_5_states_exist():
    assert [state.value for state in RunState] == [
        "accepted",
        "routing",
        "local_running",
        "external_running",
        "parallel_running",
        "waiting_input",
        "waiting_confirmation",
        "completed",
        "failed",
        "cancelled",
    ]
    assert TERMINAL_STATES == {S.COMPLETED, S.FAILED, S.CANCELLED}
    assert WAITING_STATES == {S.WAITING_INPUT, S.WAITING_CONFIRMATION}
    assert ACTIVE_STATES == {S.ROUTING, S.LOCAL_RUNNING, S.EXTERNAL_RUNNING, S.PARALLEL_RUNNING}


def test_allowed_transitions_match_the_section_5_table():
    for state in RunState:
        if state in TERMINAL_STATES:
            assert ALLOWED_TRANSITIONS[state] == frozenset(), state
        else:
            assert ALLOWED_TRANSITIONS[state] == EXPECTED[state] | {S.FAILED, S.CANCELLED}, state


@pytest.fixture
def store():
    with RunStore() as opened:
        yield opened


@pytest.fixture
def machine(store):
    return RunStateMachine(store)


def start(machine):
    """A new run with run.accepted, the first event of every run (RM22)."""
    run = machine.create()
    machine.store.append_event(run.run_id, EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD)
    return run


def terminal_event(state, error_code=None):
    """The RM22 event a transition into a terminal state carries."""
    if state is S.COMPLETED:
        return (
            EventType.RUN_COMPLETED,
            "local",
            {"final_answer_model": "qwen3", **TERMINAL_EVIDENCE},
        )
    if state is S.FAILED:
        return (EventType.RUN_FAILED, "gateway", {"error_code": error_code, **TERMINAL_EVIDENCE})
    return (
        EventType.RUN_CANCELLED,
        "gateway",
        {"reason": "cancel_requested", "active_ms": 0, "waiting_ms": 0},
    )


def end(machine, run, terminal, error_code=None):
    """Move a run into a terminal state with its matching event."""
    if terminal is S.FAILED:
        error_code = error_code or "x"
    return machine.transition(
        run.run_id,
        run.version,
        terminal,
        error_code=error_code,
        event=terminal_event(terminal, error_code),
    )


def walk(machine, run, *states):
    for state in states:
        if state in TERMINAL_STATES:
            run = end(machine, run, state)
        else:
            run = machine.transition(run.run_id, run.version, state)
    return run


def drive_to_terminal(machine, terminal):
    """A new run driven through local execution to a terminal state."""
    run = walk(machine, start(machine), S.ROUTING, S.LOCAL_RUNNING)
    return end(machine, run, terminal)


def test_a_new_run_is_accepted_at_version_1(machine):
    run = machine.create()
    assert (run.state, run.version) == (S.ACCEPTED, 1)


def test_each_transition_bumps_the_version(machine):
    run = walk(machine, start(machine), S.ROUTING, S.LOCAL_RUNNING, S.COMPLETED)
    assert (run.state, run.version) == (S.COMPLETED, 4)


@pytest.mark.parametrize(
    ("path", "illegal"),
    [
        ((), S.LOCAL_RUNNING),  # accepted must route first
        ((S.ROUTING,), S.COMPLETED),  # routing selects, never answers
        ((S.ROUTING,), S.WAITING_CONFIRMATION),
        ((S.ROUTING, S.EXTERNAL_RUNNING), S.LOCAL_RUNNING),  # no hand-back
        ((S.ROUTING, S.PARALLEL_RUNNING), S.COMPLETED),  # join goes through synthesis
        ((S.ROUTING, S.LOCAL_RUNNING), S.ROUTING),
    ],
)
def test_transitions_outside_the_table_are_rejected(machine, path, illegal):
    run = walk(machine, start(machine), *path)
    with pytest.raises(InvalidTransitionError) as caught:
        machine.transition(run.run_id, run.version, illegal)
    assert caught.value.http_status == 409
    assert machine.get(run.run_id) == run


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
@pytest.mark.parametrize("target", list(RunState))
def test_terminal_states_are_immutable(machine, terminal, target):
    """RM17: no transition from a terminal state restarts execution."""
    run = drive_to_terminal(machine, terminal)
    with pytest.raises(TerminalStateError) as caught:
        machine.transition(run.run_id, run.version, target)
    assert caught.value.http_status == 409
    assert machine.get(run.run_id) == run


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
def test_resume_and_cancel_never_restart_a_terminal_run(machine, terminal):
    run = drive_to_terminal(machine, terminal)
    with pytest.raises(TerminalStateError):
        machine.resume(run.run_id, run.version)
    with pytest.raises(TerminalStateError):
        machine.cancel(run.run_id, run.version)
    assert machine.get(run.run_id) == run


def test_a_new_attempt_is_a_new_run_linked_to_the_previous_one(machine):
    """RM17: a retry creates a new run instead of restarting a terminal one."""
    first = start(machine)
    first = end(machine, first, S.FAILED, "local_budget")
    retry = machine.create(previous_run_id=first.run_id)
    assert retry.run_id != first.run_id
    assert (retry.state, retry.version, retry.previous_run_id) == (S.ACCEPTED, 1, first.run_id)
    assert machine.get(first.run_id).state == S.FAILED


def test_failed_runs_keep_an_error_code(machine):
    """Section 5: failed retains an error code."""
    run = start(machine)
    with pytest.raises(ValueError, match="error_code"):
        machine.transition(run.run_id, run.version, S.FAILED)
    failed = end(machine, run, S.FAILED, "invalid_request")
    assert failed.error_code == "invalid_request"


def test_a_stale_transition_is_rejected_with_409(machine):
    run = start(machine)
    machine.transition(run.run_id, run.version, S.ROUTING)
    with pytest.raises(StaleVersionError) as caught:
        machine.transition(run.run_id, run.version, S.ROUTING)
    assert caught.value.http_status == 409


@pytest.mark.parametrize(
    ("action", "path"),
    [
        ("resume", (S.ROUTING, S.LOCAL_RUNNING, S.WAITING_INPUT)),
        ("cancel", (S.ROUTING, S.LOCAL_RUNNING)),
    ],
    ids=["resume", "cancel"],
)
def test_resume_and_cancel_with_a_stale_version_are_rejected_with_409(machine, action, path):
    """RM17: resume and cancel reject stale versions with 409."""
    run = walk(machine, start(machine), *path)
    with pytest.raises(StaleVersionError) as caught:
        getattr(machine, action)(run.run_id, run.version - 1)
    assert caught.value.http_status == 409
    assert machine.get(run.run_id) == run


def test_two_resumes_with_the_same_version_resume_once(machine):
    """RM17: atomic against the version; the second caller gets 409."""
    run = walk(machine, start(machine), S.ROUTING, S.LOCAL_RUNNING, S.WAITING_INPUT)
    resumed = machine.resume(run.run_id, run.version)
    assert resumed.state == S.LOCAL_RUNNING
    with pytest.raises(StaleVersionError):
        machine.resume(run.run_id, run.version)


@pytest.mark.parametrize(
    ("path", "waiting"),
    [
        ((S.ROUTING,), S.WAITING_INPUT),
        ((S.ROUTING, S.LOCAL_RUNNING), S.WAITING_INPUT),
        ((S.ROUTING, S.EXTERNAL_RUNNING), S.WAITING_INPUT),
        ((S.ROUTING, S.LOCAL_RUNNING), S.WAITING_CONFIRMATION),
        ((S.ROUTING, S.EXTERNAL_RUNNING), S.WAITING_CONFIRMATION),
    ],
)
def test_resume_returns_to_the_state_that_suspended(machine, path, waiting):
    run = walk(machine, start(machine), *path)
    suspended_from = run.state
    run = machine.transition(run.run_id, run.version, waiting)
    assert machine.resume(run.run_id, run.version).state == suspended_from


def test_resume_outside_a_waiting_state_is_rejected(machine):
    run = walk(machine, start(machine), S.ROUTING, S.LOCAL_RUNNING)
    with pytest.raises(InvalidTransitionError, match="waiting"):
        machine.resume(run.run_id, run.version)


@pytest.mark.parametrize(
    "path",
    [
        (),
        (S.ROUTING,),
        (S.ROUTING, S.LOCAL_RUNNING),
        (S.ROUTING, S.EXTERNAL_RUNNING),
        (S.ROUTING, S.PARALLEL_RUNNING),
        (S.ROUTING, S.WAITING_INPUT),
        (S.ROUTING, S.LOCAL_RUNNING, S.WAITING_CONFIRMATION),
    ],
)
def test_cancel_ends_any_unfinished_run(machine, path):
    run = walk(machine, start(machine), *path)
    cancelled = machine.cancel(run.run_id, run.version, event=terminal_event(S.CANCELLED))
    assert (cancelled.state, cancelled.version) == (S.CANCELLED, run.version + 1)


def test_cancel_records_its_reason_and_event(machine, store):
    """Section 5: a denied confirmation ends cancelled with user_denied."""
    run = start(machine)
    run = walk(machine, run, S.ROUTING, S.LOCAL_RUNNING, S.WAITING_CONFIRMATION)
    cancelled = machine.cancel(
        run.run_id,
        run.version,
        reason="user_denied",
        event=(
            EventType.RUN_CANCELLED,
            "gateway",
            {"reason": "user_denied", "active_ms": 0, "waiting_ms": 0},
        ),
    )
    assert cancelled.error_code == "user_denied"
    assert [e.event_type for e in store.events(run.run_id)] == [
        EventType.RUN_ACCEPTED,
        EventType.RUN_CANCELLED,
    ]


def test_cancel_without_an_event_records_a_default_run_cancelled(machine, store):
    """RM21, RM22, spec 0.17 sections 7.4 and 8: cancel(run_id, version)
    records run.cancelled by itself, with reason cancel_requested unless
    another reason (user_denied) is given."""
    run = walk(machine, start(machine), S.ROUTING)
    cancelled = machine.cancel(run.run_id, run.version)
    assert (cancelled.state, cancelled.error_code) == (S.CANCELLED, "cancel_requested")
    last = store.events(run.run_id)[-1]
    assert (last.event_type, last.actor) == (EventType.RUN_CANCELLED, "gateway")
    assert last.payload["reason"] == "cancel_requested"
    assert last.payload["waiting_ms"] == 0
    assert last.payload["active_ms"] >= 0
    other = walk(machine, start(machine), S.ROUTING, S.LOCAL_RUNNING, S.WAITING_CONFIRMATION)
    denied = machine.cancel(other.run_id, other.version, reason="user_denied")
    assert denied.error_code == "user_denied"
    payload = store.events(other.run_id)[-1].payload
    assert payload["reason"] == "user_denied"
    assert payload["active_ms"] >= 0
    assert payload["waiting_ms"] >= 0


ACCEPTED_EVENT = (EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD)


@pytest.mark.parametrize("terminal", [S.FAILED, S.CANCELLED])
def test_a_run_created_with_run_accepted_can_end_straight_from_accepted(machine, store, terminal):
    """Section 5: accepted validates, then routes or fails; RM21 cancels any
    unfinished run. create(accepted=...) writes run.accepted with the run
    (RM22), so the terminal event takes seq 2 and the run can end at once."""
    run = machine.create(accepted=ACCEPTED_EVENT)
    ended = end(machine, run, terminal)
    assert ended.state == terminal
    assert [(e.seq, e.event_type) for e in store.events(run.run_id)] == [
        (1, EventType.RUN_ACCEPTED),
        (2, terminal_event(terminal)[0]),
    ]


# -- execution slot (section 5 and section 11) ------------------------------


def test_an_active_run_holds_the_execution_slot(machine):
    run = walk(machine, start(machine), S.ROUTING)
    assert machine.active_run_id() == run.run_id
    walk(machine, run, S.LOCAL_RUNNING)
    assert machine.active_run_id() == run.run_id


def test_a_second_active_run_is_rejected_with_429(machine):
    """Section 11: one active run in the baseline; reject with 429."""
    first = walk(machine, start(machine), S.ROUTING)
    second = start(machine)
    with pytest.raises(SlotUnavailableError) as caught:
        machine.transition(second.run_id, second.version, S.ROUTING)
    assert caught.value.http_status == 429
    assert machine.get(second.run_id) == second
    assert machine.active_run_id() == first.run_id


@pytest.mark.parametrize("waiting", sorted(WAITING_STATES))
def test_waiting_states_release_the_execution_slot(machine, waiting):
    """Section 5: waiting releases the active execution slot."""
    walk(machine, start(machine), S.ROUTING, S.LOCAL_RUNNING, waiting)
    assert machine.active_run_id() is None
    other = walk(machine, start(machine), S.ROUTING)
    assert machine.active_run_id() == other.run_id


def test_a_resume_reacquires_the_slot_or_fails_with_429_unchanged(machine):
    """Section 5: a resume must reacquire the slot and may return 429."""
    waiting = walk(machine, start(machine), S.ROUTING, S.LOCAL_RUNNING, S.WAITING_CONFIRMATION)
    other = walk(machine, start(machine), S.ROUTING)
    with pytest.raises(SlotUnavailableError):
        machine.resume(waiting.run_id, waiting.version)
    assert machine.get(waiting.run_id) == waiting
    end(machine, other, S.FAILED)
    resumed = machine.resume(waiting.run_id, waiting.version)
    assert resumed.state == S.LOCAL_RUNNING
    assert machine.active_run_id() == waiting.run_id


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
def test_terminal_states_release_the_slot(machine, terminal):
    drive_to_terminal(machine, terminal)
    assert machine.active_run_id() is None


def test_cancelling_a_waiting_run_does_not_touch_another_runs_slot(machine):
    waiting = walk(machine, start(machine), S.ROUTING, S.WAITING_INPUT)
    other = walk(machine, start(machine), S.ROUTING)
    machine.cancel(waiting.run_id, waiting.version, event=terminal_event(S.CANCELLED))
    assert machine.active_run_id() == other.run_id


def test_a_stale_transition_into_an_active_state_does_not_keep_the_slot(machine):
    run = walk(machine, start(machine), S.ROUTING, S.WAITING_INPUT)
    machine.resume(run.run_id, run.version)
    machine.transition(run.run_id, run.version + 1, S.WAITING_INPUT)
    with pytest.raises(StaleVersionError):
        machine.resume(run.run_id, run.version)
    assert machine.active_run_id() is None


def test_one_active_run_is_shared_by_machines_on_one_store_file(tmp_path):
    """Section 11: one active run in the baseline, across stores and processes.

    Two machines, each with its own store on the same SQLite file (as two
    worker processes would have), still admit one active run.
    """
    path = tmp_path / "runs.sqlite"
    with RunStore(path) as store_a, RunStore(path) as store_b:
        a, b = RunStateMachine(store_a), RunStateMachine(store_b)
        first = walk(a, start(a), S.ROUTING)
        second = start(b)
        with pytest.raises(SlotUnavailableError) as caught:
            b.transition(second.run_id, second.version, S.ROUTING)
        assert caught.value.http_status == 429
        assert b.get(second.run_id) == second
        assert b.active_run_id() == first.run_id
        walk(a, first, S.WAITING_INPUT)
        walk(b, second, S.ROUTING)
        assert a.active_run_id() == second.run_id


class AfterSetStore(RunStore):
    """Runs a hook once, right after the next set_state has committed."""

    after_set = None

    def set_state(self, run_id, **kwargs):
        run = super().set_state(run_id, **kwargs)
        hook, self.after_set = self.after_set, None
        if hook:
            hook(run)
        return run


def test_a_resume_right_after_suspension_holds_the_slot():
    """Section 5, section 11: the slot changes with the state, in one transaction.

    Another caller resumes the run the moment its suspension commits.
    The resumed run holds the slot, so a second run cannot also become
    active.
    """
    with AfterSetStore() as store:
        machine = RunStateMachine(store)
        run = walk(machine, start(machine), S.ROUTING, S.LOCAL_RUNNING)
        store.after_set = lambda waiting: machine.resume(waiting.run_id, waiting.version)
        machine.transition(run.run_id, run.version, S.WAITING_INPUT)
        assert machine.get(run.run_id).state == S.LOCAL_RUNNING
        assert machine.active_run_id() == run.run_id
        other = start(machine)
        with pytest.raises(SlotUnavailableError):
            machine.transition(other.run_id, other.version, S.ROUTING)


# -- terminal events (RM22, section 5) ------------------------------------------


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
def test_a_terminal_transition_needs_its_event(machine, store, terminal):
    """RM22: entering completed, failed, or cancelled records run.completed/failed/cancelled."""
    run = walk(machine, start(machine), S.ROUTING, S.LOCAL_RUNNING)
    error_code = "x" if terminal is S.FAILED else None
    with pytest.raises(ValueError, match="event"):
        machine.transition(run.run_id, run.version, terminal, error_code=error_code)
    others = [state for state in TERMINAL_STATES if state is not terminal]
    for other in others:
        with pytest.raises(ValueError, match="event"):
            machine.transition(
                run.run_id,
                run.version,
                terminal,
                error_code=error_code,
                event=terminal_event(other, error_code),
            )
    assert machine.get(run.run_id) == run
    done = end(machine, run, terminal)
    assert store.events(run.run_id)[-1].event_type == terminal_event(terminal)[0]
    assert done.state == terminal


def test_the_run_failed_event_names_the_runs_error_code(machine):
    """Section 5, RM22: a failed run and its run.failed event keep the same error code."""
    run = start(machine)
    with pytest.raises(ValueError, match="error_code"):
        machine.transition(
            run.run_id,
            run.version,
            S.FAILED,
            error_code="invalid_request",
            event=terminal_event(S.FAILED, "local_budget"),
        )
    assert machine.get(run.run_id) == run


def test_a_terminal_event_never_accompanies_another_transition(machine):
    """RM22, section 12 honest evidence: run.completed only when the run completes."""
    run = start(machine)
    with pytest.raises(ValueError, match="RM22"):
        machine.transition(run.run_id, run.version, S.ROUTING, event=terminal_event(S.COMPLETED))
    assert machine.get(run.run_id) == run


# -- interleavings, without threads ------------------------------------------


class InterleavingStore(RunStore):
    """Runs a hook once, right after the next get_run or before the next set_state.

    This replays a concurrent caller at an exact point in another
    caller's transition, deterministically.
    """

    after_get = None
    before_set = None

    def get_run(self, run_id):
        run = super().get_run(run_id)
        hook, self.after_get = self.after_get, None
        if hook:
            hook()
        return run

    def set_state(self, run_id, **kwargs):
        hook, self.before_set = self.before_set, None
        if hook:
            hook()
        return super().set_state(run_id, **kwargs)


@pytest.fixture
def interleaved():
    with InterleavingStore() as opened:
        yield opened, RunStateMachine(opened)


def waiting_run(machine):
    return walk(machine, start(machine), S.ROUTING, S.LOCAL_RUNNING, S.WAITING_INPUT)


@pytest.mark.parametrize(
    "hook",
    ["after_get", "before_set"],
    ids=["found-the-slot-held", "took-the-slot"],
)
def test_a_losing_resume_keeps_the_winners_slot(interleaved, hook):
    """RM17, section 5: the loser gets 409 and the winner keeps the execution slot.

    after_get: the loser reads the run, the winner resumes it, then the
    loser finds the slot already held by the run and loses the
    compare-and-set. before_set: the loser took the slot first, but the
    winner's run is active.
    """
    store, machine = interleaved
    run = waiting_run(machine)
    setattr(store, hook, lambda: machine.resume(run.run_id, run.version))
    with pytest.raises(StaleVersionError):
        machine.resume(run.run_id, run.version)
    assert machine.get(run.run_id).state == S.LOCAL_RUNNING
    assert machine.active_run_id() == run.run_id


def test_a_resume_that_races_a_cancel_is_stale_not_invalid(interleaved):
    """RM17: a stale resume always gets StaleVersionError (409).

    The run is cancelled after the resume has read it; the resume must
    report the stale version, not a terminal-state or table error.
    """
    store, machine = interleaved
    run = waiting_run(machine)
    store.after_get = lambda: machine.cancel(
        run.run_id, run.version, event=terminal_event(S.CANCELLED)
    )
    with pytest.raises(StaleVersionError) as caught:
        machine.resume(run.run_id, run.version)
    assert type(caught.value) is StaleVersionError
    assert machine.get(run.run_id).state == S.CANCELLED
    assert machine.active_run_id() is None


# -- leaving a waiting state (section 5) ---------------------------------------


@pytest.mark.parametrize(
    ("waiting", "target"),
    [
        (S.WAITING_INPUT, S.ROUTING),
        (S.WAITING_INPUT, S.LOCAL_RUNNING),
        (S.WAITING_INPUT, S.EXTERNAL_RUNNING),
        (S.WAITING_CONFIRMATION, S.LOCAL_RUNNING),
        (S.WAITING_CONFIRMATION, S.EXTERNAL_RUNNING),
    ],
)
def test_a_waiting_run_continues_only_through_resume(machine, waiting, target):
    """Section 5: waiting resumes on new input or a valid approval, to the state that suspended.

    transition() cannot move a waiting run to another running state, even
    one the table lists for some waiting run; only resume() continues it.
    """
    run = walk(machine, start(machine), S.ROUTING, S.LOCAL_RUNNING, waiting)
    with pytest.raises(InvalidTransitionError, match="resume"):
        machine.transition(run.run_id, run.version, target)
    assert machine.get(run.run_id) == run
    assert machine.active_run_id() is None


@pytest.mark.parametrize("waiting", sorted(WAITING_STATES))
def test_a_waiting_run_may_still_fail_or_be_cancelled(machine, waiting):
    """Section 5: expired waits fail; a denied confirmation or RM21 cancel cancels."""
    run = walk(machine, start(machine), S.ROUTING, S.LOCAL_RUNNING, waiting)
    failed = end(machine, run, S.FAILED, "input_expired")
    assert failed.state == S.FAILED
    other = walk(machine, start(machine), S.ROUTING, S.LOCAL_RUNNING, waiting)
    assert end(machine, other, S.CANCELLED).state == S.CANCELLED


# -- deletion and retention release the slot (section 5, section 11) -----------


def test_deleting_an_active_run_releases_its_slot(machine):
    """Section 5 explicit deletion; section 11: the next run can start."""
    run = walk(machine, start(machine), S.ROUTING)
    machine.delete_run(run.run_id)
    assert machine.active_run_id() is None
    other = walk(machine, start(machine), S.ROUTING)
    assert machine.active_run_id() == other.run_id


def test_deleting_another_run_keeps_the_slot(machine):
    active = walk(machine, start(machine), S.ROUTING)
    done = start(machine)
    machine.delete_run(done.run_id)
    assert machine.active_run_id() == active.run_id


def test_purging_an_active_run_releases_its_slot():
    """Section 5 retention: purged runs give up the execution slot."""
    with RunStore(retention=timedelta(0)) as store:
        machine = RunStateMachine(store)
        run = walk(machine, start(machine), S.ROUTING)
        assert machine.purge_expired() == [run.run_id]
        assert machine.active_run_id() is None


# -- Review fixes: stale before terminal, the run clock on resume -------------


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
def test_a_stale_version_on_a_terminal_run_is_stale_first(machine, terminal):
    """RM17: resume and cancel reject stale versions with 409
    (StaleVersionError) before any other check, so a caller holding an
    old version learns it is stale even when the run has since ended.
    With the current version, a terminal run still gives TerminalStateError."""
    run = drive_to_terminal(machine, terminal)
    stale = run.version - 1
    for attempt in (
        lambda: machine.resume(run.run_id, stale),
        lambda: machine.cancel(run.run_id, stale),
        lambda: machine.transition(run.run_id, stale, S.ROUTING),
    ):
        with pytest.raises(StaleVersionError):
            attempt()
    with pytest.raises(TerminalStateError):
        machine.resume(run.run_id, run.version)
    assert machine.get(run.run_id) == run


def test_a_resume_in_another_store_starts_its_run_clock(tmp_path):
    """RM25: a store that resumes a run it did not create measures
    elapsed_ms from the run's last persisted value, so the events it
    writes after the resume are accepted."""
    path = tmp_path / "runs.sqlite"
    with RunStore(path) as first, RunStore(path) as second:
        machine = RunStateMachine(first)
        run = walk(machine, start(machine), S.ROUTING, S.WAITING_INPUT)
        resumed = RunStateMachine(second).resume(run.run_id, run.version)
        assert resumed.state == S.ROUTING
        written = second.append_event(run.run_id, EventType.ANSWER_DELTA, "local", {"text": "x"})
        assert written.elapsed_ms >= max(e.elapsed_ms for e in first.events(run.run_id)[:-1])
