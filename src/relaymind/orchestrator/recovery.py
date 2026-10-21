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
the others; the errors are raised together at the end. Running it again
is safe: terminal runs are never touched.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from relaymind.orchestrator.handoff import EXTERNAL_ACTOR
from relaymind.orchestrator.run_state import (
    GATEWAY_ACTOR,
    TERMINAL_STATES,
    RunState,
    RunStateMachine,
)
from relaymind.redaction import Redactor
from relaymind.store import RunRecord, RunStore
from relaymind.store.trace_event import EventType
from relaymind.tools import ToolRegistry
from relaymind.tools.executor import ToolExecutor

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


def recover_interrupted_runs(
    store: RunStore, tools: ToolRegistry, *, redactor: Redactor | None = None
) -> RecoveryReport:
    """Fail every unfinished run as interrupted and settle what it left open."""
    machine = RunStateMachine(store)
    executor = ToolExecutor(tools, store, clock=store.clock, redactor=redactor)
    recovered: list[RecoveredRun] = []
    errors: list[Exception] = []
    for record in store.unfinished_runs([state.value for state in TERMINAL_STATES]):
        try:
            recovered.append(_recover(store, machine, executor, record))
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise ExceptionGroup("some interrupted runs could not be recovered", errors)
    return RecoveryReport(tuple(recovered))


def _recover(
    store: RunStore, machine: RunStateMachine, executor: ToolExecutor, record: RunRecord
) -> RecoveredRun:
    # RM25: this process has no baseline for the run yet; downtime is not counted.
    store.start_run_clock(record.run_id)
    with store.transaction():
        settled = tuple(
            _settle(store, executor, operation)
            for operation in store.tool_operations(record.run_id)
            if operation["status"] == "pending"
        )
        expired = store.expire_approvals(record.run_id)
        timings = store.state_timings(record.run_id)
        calls = _model_calls(store, record.run_id)
        machine.transition(
            record.run_id,
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
    return RecoveredRun(record.run_id, record.state, settled, expired)


def _settle(
    store: RunStore, executor: ToolExecutor, operation: Mapping[str, Any]
) -> SettledOperation:
    result = executor.settle_after_restart(operation)
    payload: dict[str, Any] = {
        "operation_id": operation["operation_id"],
        "tool_name": operation["tool_name"],
        "model": operation["model"],
        "status": result.status.value,
        "replayed": False,
        "recovered": True,
        "result": result.to_json_object(),
    }
    if operation["proposal_id"] is not None:
        payload["proposal_id"] = operation["proposal_id"]
    store.append_event(operation["run_id"], EventType.TOOL_FINISHED, GATEWAY_ACTOR, payload)
    return SettledOperation(operation["operation_id"], result.status.value)


def _model_calls(store: RunStore, run_id: str) -> list[dict[str, Any]]:
    """The run's model calls, rebuilt from its model.started events (RM16)."""
    return [
        {key: event.payload[key] for key in ("call_id", "model", "actor", "purpose")}
        for event in store.events(run_id)
        if event.event_type is EventType.MODEL_STARTED
    ]
