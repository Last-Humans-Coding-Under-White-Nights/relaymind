"""Run state machine (spec section 5; RM17).

The allowed transitions are the section 5 table read state by state:

- accepted: validate, then route or fail.
- routing: select local, external, clarification (waiting_input), or an
  approved parallel workflow.
- local_running: finish, ask (waiting_input, or waiting_confirmation for
  a proposed mutation), or escalate to external_running.
- external_running: finish or ask.
- parallel_running: join and transfer final ownership to the external
  synthesis call (RM15).
- waiting_input / waiting_confirmation: resume, through resume() only,
  to the recorded state that suspended. A waiting_confirmation resume needs a valid approval; the
  approval check belongs to the confirmation flow (RM11), not here.
- completed, failed, cancelled: terminal, no transitions.

Every non-terminal state may also fail (exhausted budgets, expired
waits, restart) or be cancelled (RM21 cancel, a denied confirmation).

A transition into a terminal state carries its RM22 event in the same
transaction: run.completed, run.failed, or run.cancelled. A failed
run's event names the same error_code the run keeps (section 5), and a
terminal event never accompanies any other transition.

Each transition is a compare-and-set on the run state version in the
store, so a stale caller gets 409 and the run is unchanged. A retry is
a new run linked to the previous one; terminal runs never restart.

The execution slot (section 11: one active run in the baseline) is held
while a run is in an active state. Waiting and terminal states release
it; entering an active state from anything else must reacquire it, or
the transition fails with 429 and nothing changes (section 5). The slot
lives in the run store, set in the same transaction as the state
change, so every process on one store file shares it and there is no
moment where a state and its slot disagree.
"""

from collections.abc import Mapping
from enum import StrEnum

from relaymind.store import (
    IdempotencyClaim,
    RunRecord,
    RunStore,
    SlotUnavailableError,
    StaleVersionError,
)
from relaymind.store.run_store import EventDraft
from relaymind.store.trace_event import EventType

__all__ = [
    "ACTIVE_STATES",
    "ALLOWED_TRANSITIONS",
    "TERMINAL_EVENTS",
    "TERMINAL_STATES",
    "WAITING_STATES",
    "InvalidTransitionError",
    "RunState",
    "RunStateMachine",
    "SlotUnavailableError",
    "TerminalStateError",
]


class RunState(StrEnum):
    """Run states from spec section 5."""

    ACCEPTED = "accepted"
    ROUTING = "routing"
    LOCAL_RUNNING = "local_running"
    EXTERNAL_RUNNING = "external_running"
    PARALLEL_RUNNING = "parallel_running"
    WAITING_INPUT = "waiting_input"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


S = RunState

# Spec 0.17 sections 7.4 and 8: the run.cancelled reason of every cancel
# that is not a denied confirmation, such as a native cancel request or a
# compatible chat disconnect.
CANCEL_REQUESTED = "cancel_requested"

TERMINAL_STATES: frozenset[RunState] = frozenset({S.COMPLETED, S.FAILED, S.CANCELLED})
WAITING_STATES: frozenset[RunState] = frozenset({S.WAITING_INPUT, S.WAITING_CONFIRMATION})
ACTIVE_STATES: frozenset[RunState] = frozenset(
    {S.ROUTING, S.LOCAL_RUNNING, S.EXTERNAL_RUNNING, S.PARALLEL_RUNNING}
)

_END = frozenset({S.FAILED, S.CANCELLED})

# The actor of the events the gateway records itself (section 8 TraceEvent).
GATEWAY_ACTOR = "gateway"

# RM22: the event each terminal transition carries.
TERMINAL_EVENTS: Mapping[RunState, EventType] = {
    S.COMPLETED: EventType.RUN_COMPLETED,
    S.FAILED: EventType.RUN_FAILED,
    S.CANCELLED: EventType.RUN_CANCELLED,
}

ALLOWED_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = {
    S.ACCEPTED: frozenset({S.ROUTING}) | _END,
    S.ROUTING: frozenset({S.LOCAL_RUNNING, S.EXTERNAL_RUNNING, S.WAITING_INPUT, S.PARALLEL_RUNNING})
    | _END,
    S.LOCAL_RUNNING: frozenset(
        {S.COMPLETED, S.WAITING_INPUT, S.WAITING_CONFIRMATION, S.EXTERNAL_RUNNING}
    )
    | _END,
    S.EXTERNAL_RUNNING: frozenset({S.COMPLETED, S.WAITING_INPUT, S.WAITING_CONFIRMATION}) | _END,
    S.PARALLEL_RUNNING: frozenset({S.EXTERNAL_RUNNING}) | _END,
    S.WAITING_INPUT: frozenset({S.ROUTING, S.LOCAL_RUNNING, S.EXTERNAL_RUNNING}) | _END,
    S.WAITING_CONFIRMATION: frozenset({S.LOCAL_RUNNING, S.EXTERNAL_RUNNING}) | _END,
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}


class InvalidTransitionError(RuntimeError):
    """The section 5 table does not allow this transition (409)."""

    http_status = 409


class TerminalStateError(InvalidTransitionError):
    """A terminal run never changes or restarts (RM17)."""


class RunStateMachine:
    """Applies section 5 transitions to runs in a RunStore."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def create(
        self,
        *,
        previous_run_id: str | None = None,
        accepted: EventDraft | None = None,
        idempotency: IdempotencyClaim | None = None,
    ) -> RunRecord:
        """A new run in accepted. A retry links to the run it replaces (RM17).

        idempotency stores the caller's key with the run (spec 0.18
        section 7.4).

        accepted is the run.accepted event (RM22), written with the run in
        one transaction. Pass it so the run can fail or be cancelled
        straight from accepted (section 5): only run.accepted may be seq
        1, so without it the run's first event could not be a terminal one.
        """
        return self.store.create_run(
            S.ACCEPTED,
            previous_run_id=previous_run_id,
            accepted=accepted,
            idempotency=idempotency,
        )

    def get(self, run_id: str) -> RunRecord:
        return self.store.get_run(run_id)

    def active_run_id(self) -> str | None:
        """The run holding the one active execution slot, if any (section 11)."""
        return self.store.active_run_id()

    def delete_run(self, run_id: str) -> None:
        """Explicitly delete a run (section 5); a slot it held goes with it."""
        self.store.delete_run(run_id)

    def purge_expired(self) -> list[str]:
        """Purge runs past retention (section 5); slots they held go with them."""
        return self.store.purge_expired()

    def _current(self, run_id: str, expected_version: int) -> tuple[RunRecord, RunState]:
        run = self.store.get_run(run_id)
        state = RunState(run.state)
        # RM17: a stale version is 409 StaleVersionError before any other
        # check, so a caller holding an old version always learns that.
        if run.version != expected_version:
            raise StaleVersionError(run_id, expected_version, run.version)
        if state in TERMINAL_STATES:
            raise TerminalStateError(f"run {run_id} is {state}; terminal runs never restart")
        return run, state

    def transition(
        self,
        run_id: str,
        expected_version: int,
        to_state: RunState,
        *,
        error_code: str | None = None,
        event: EventDraft | None = None,
    ) -> RunRecord:
        """Move a run to to_state if the table allows it and the version is current.

        A waiting run only fails or is cancelled here; it continues
        through resume(), to the state that suspended it (section 5).
        A terminal to_state needs its RM22 event (see TERMINAL_EVENTS).
        """
        _run, state = self._current(run_id, expected_version)
        to_state = RunState(to_state)
        if state in WAITING_STATES and to_state not in _END:
            raise InvalidTransitionError(
                f"run {run_id} is {state}; it continues only through resume (section 5)"
            )
        return self._apply(run_id, expected_version, state, to_state, error_code, event)

    def _apply(
        self,
        run_id: str,
        expected_version: int,
        state: RunState,
        to_state: RunState,
        error_code: str | None = None,
        event: EventDraft | None = None,
    ) -> RunRecord:
        """Check the table and the event, then compare-and-set from state.

        The store sets the slot with the state in one transaction: held
        in active states, released otherwise (429 if another run holds it).
        """
        if to_state not in ALLOWED_TRANSITIONS[state]:
            raise InvalidTransitionError(f"run {run_id} cannot go from {state} to {to_state}")
        if to_state is S.FAILED and not error_code:
            raise ValueError("a failed run needs an error_code (section 5)")
        _check_terminal_event(to_state, error_code, event)
        return self.store.set_state(
            run_id,
            expected_version=expected_version,
            from_state=state,
            to_state=to_state,
            resume_state=state if to_state in WAITING_STATES else None,
            error_code=error_code,
            event=event,
            holds_slot=to_state in ACTIVE_STATES,
        )

    def resume(self, run_id: str, expected_version: int) -> RunRecord:
        """Resume a waiting run to the state that suspended it (RM17: stale is 409).

        The run is read once; the compare-and-set on that version decides
        a race, so a stale resume always gets StaleVersionError. The
        resume starts this store's run clock (RM25), so the events written
        after it get elapsed_ms.
        """
        run, state = self._current(run_id, expected_version)
        if state not in WAITING_STATES or run.resume_state is None:
            raise InvalidTransitionError(f"run {run_id} is {state}, not a waiting state")
        # RM25: this store may not have created the run (another process,
        # or a reopened file); its events need a monotonic baseline here.
        # A clock the store already runs keeps its baseline.
        self.store.start_run_clock(run_id)
        return self._apply(run_id, expected_version, state, RunState(run.resume_state))

    def cancel(
        self,
        run_id: str,
        expected_version: int,
        *,
        reason: str = CANCEL_REQUESTED,
        event: EventDraft | None = None,
    ) -> RunRecord:
        """Cancel an unfinished run (RM17: stale is 409, terminal never changes).

        event is the run.cancelled event (RM22). Without one, the gateway
        records run.cancelled with measured active/waiting time and the
        reason: user_denied for a denied confirmation, otherwise
        cancel_requested (spec 0.17 sections 7.4 and 8).
        """
        if event is None:
            # Check the caller's version before doing any timing work (RM17).
            self._current(run_id, expected_version)
            payload = {"reason": reason, **self.store.state_timings(run_id)}
            event = (EventType.RUN_CANCELLED, GATEWAY_ACTOR, payload)
        return self.transition(
            run_id, expected_version, S.CANCELLED, error_code=reason, event=event
        )


def _check_terminal_event(
    to_state: RunState, error_code: str | None, event: EventDraft | None
) -> None:
    """RM22: a terminal transition carries its own event, and only it does."""
    event_type = None if event is None else EventType(event[0])
    expected = TERMINAL_EVENTS.get(to_state)
    if expected is None:
        if event_type in TERMINAL_EVENTS.values():
            raise ValueError(f"{event_type} belongs only to the transition into its state (RM22)")
        return
    if event_type is not expected:
        raise ValueError(f"a transition to {to_state} needs a {expected} event (RM22)")
    if to_state is S.FAILED and event is not None and event[2].get("error_code") != error_code:
        raise ValueError(
            f"the run.failed event error_code must be the run's error_code {error_code!r}"
        )
