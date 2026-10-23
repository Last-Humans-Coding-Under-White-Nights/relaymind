"""Restart recovery (spec section 5; RM10, RM17, AC12 restart part).

After a process restart the gateway runs ``recover_interrupted_runs``
once, before it accepts any run. Every run that is not terminal becomes
failed with ``interrupted``; the model loop is never resumed and no
write is ever replayed. Each run is recovered in one store transaction:

1. Its pending tool operations are settled by the executor, which owns
   operation outcomes (a write is ``unknown_outcome``, a read is
   ``failed``) and each gets a ``tool.finished`` event.
2. Its pending approvals expire (RM11).
3. It moves to failed with ``run.failed`` (RM22), which releases the
   execution slot.

One run that cannot be recovered is rolled back whole and does not stop
the others; the errors are raised together at the end as a
``RecoveryError`` that still carries the report of the recovered runs.
Running it again is safe: terminal runs are never touched.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from relaymind.orchestrator.handoff import EXTERNAL_ACTOR
from relaymind.orchestrator.run_state import (
    GATEWAY_ACTOR,
    TERMINAL_STATES,
    RunState,
    RunStateMachine,
)
from relaymind.store import RunStore
from relaymind.store.trace_event import EventType
from relaymind.tools.executor import GATEWAY_OPERATION_MODEL, ToolExecutor

INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class SettledOperation:
    """A pending tool operation that recovery finished."""

    operation_id: str
    status: str


@dataclass(frozen=True)
class RecoveredRun:
    """One run recovery ended, and what it settled on the way."""

    run_id: str
    interrupted_state: str
    operations: tuple[SettledOperation, ...]
    expired_approvals: int


@dataclass(frozen=True)
class RecoveryReport:
    runs: tuple[RecoveredRun, ...]


class RecoveryError(ExceptionGroup):
    """Some runs could not be recovered; ``report`` lists the ones that were."""

    report: RecoveryReport

    def __new__(
        cls, message: str, exceptions: Sequence[Exception], report: RecoveryReport
    ) -> "RecoveryError":
        error = super().__new__(cls, message, exceptions)
        error.report = report
        return error

    def __init__(
        self, message: str, exceptions: Sequence[Exception], report: RecoveryReport
    ) -> None:
        super().__init__(message, exceptions)

    def derive(self, excs: Sequence[Exception]) -> "RecoveryError":
        return RecoveryError(self.message, excs, self.report)


def recover_interrupted_runs(
    store: RunStore, machine: RunStateMachine, executor: ToolExecutor
) -> RecoveryReport:
    """Fail every unfinished run as interrupted and settle what it left open.

    The orchestrator passes its own state machine and executor, so
    recovered results use the clock and redaction it was configured with.
    """
    terminal = [state.value for state in TERMINAL_STATES]
    recovered: list[RecoveredRun] = []
    errors: list[Exception] = []
    for listed in store.unfinished_runs(terminal):
        try:
            run = _recover(store, machine, executor, listed.run_id, terminal)
        except Exception as exc:
            errors.append(exc)
        else:
            if run is not None:
                recovered.append(run)
    report = RecoveryReport(tuple(recovered))
    if errors:
        raise RecoveryError("some interrupted runs could not be recovered", errors, report)
    return report


def _recover(
    store: RunStore,
    machine: RunStateMachine,
    executor: ToolExecutor,
    run_id: str,
    terminal: Collection[str],
) -> RecoveredRun | None:
    # RM25: this process has no baseline for the run yet; downtime is not counted.
    store.start_run_clock(run_id)
    with store.transaction():
        # Read the run again inside the transaction: the listing may be stale.
        record = store.get_run(run_id)
        if record.state in terminal:
            return None
        settled = tuple(
            _settle(store, executor, operation)
            for operation in store.tool_operations(run_id, status="pending")
        )
        expired = store.expire_approvals(run_id)
        timings = store.state_timings(run_id)
        calls = model_calls(store, run_id)
        machine.transition(
            run_id,
            record.version,
            RunState.FAILED,
            error_code=INTERRUPTED,
            event=(
                EventType.RUN_FAILED,
                GATEWAY_ACTOR,
                {
                    "error_code": INTERRUPTED,
                    "detail": f"the gateway restarted while the run was {record.state}",
                    "interrupted_state": record.state,
                    "settled_operations": [asdict(op) for op in settled],
                    "model_calls": calls,
                    "external_calls": sum(1 for call in calls if call["actor"] == EXTERNAL_ACTOR),
                    **timings,
                },
            ),
        )
    return RecoveredRun(run_id, record.state, settled, expired)


def _settle(
    store: RunStore, executor: ToolExecutor, operation: Mapping[str, Any]
) -> SettledOperation:
    result = executor.settle_after_restart(operation)
    payload: dict[str, Any] = {
        "operation_id": operation["operation_id"],
        "tool_name": operation["tool_name"],
        # An operation the gateway made itself (a prefetch) names no model (RM22).
        "model": None if operation["model"] == GATEWAY_OPERATION_MODEL else operation["model"],
        "status": result.status.value,
        "replayed": False,
        "recovered": True,
        "result": result.to_json_object(),
    }
    if operation["proposal_id"] is not None:
        payload["proposal_id"] = operation["proposal_id"]
    branch = _branch_of(store, operation)
    if branch is not None:
        payload["branch"] = branch
    store.append_event(operation["run_id"], EventType.TOOL_FINISHED, GATEWAY_ACTOR, payload)
    return SettledOperation(operation["operation_id"], result.status.value)


def _branch_of(store: RunStore, operation: Mapping[str, Any]) -> str | None:
    """The workflow branch the operation's tool.started named, if any (spec 0.25 section 8)."""
    for event in store.events(operation["run_id"], event_type=EventType.TOOL_STARTED):
        if event.payload.get("operation_id") == operation["operation_id"]:
            return event.payload.get("branch")
    return None


def model_calls(store: RunStore, run_id: str) -> list[dict[str, Any]]:
    """The run's model calls, rebuilt from its model.started events (RM16)."""
    return [
        {key: event.payload[key] for key in ("call_id", "model", "actor", "purpose")}
        for event in store.events(run_id, event_type=EventType.MODEL_STARTED)
    ]
