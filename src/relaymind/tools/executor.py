"""Tool executor: operation IDs, replay, retries, and timeouts (RM10, section 11).

The executor owns side effects (spec section 3), and it is the only
owner of operation IDs and replay: callers never read tool_operations.

1. ``validate`` checks the full tool name and arguments once (RM09,
   through the registry) and returns a ValidatedCall; a rejected call
   records nothing and runs nothing. ``execute`` takes only a
   ValidatedCall, so a call is never validated twice.
2. The logical operation ID comes from the run, the tool name, the
   canonical arguments, and the attempt number, so the same call in the
   same run is the same operation and a changed argument set is a new
   one.
3. If the operation has a recorded outcome, ``execute`` replays it and
   never runs the handler again; an ``unknown_outcome`` replays as
   ``unknown_outcome``, so it is never retried automatically. A pending
   record is an operation in flight, never a replay:
   OperationInFlightError (RM14).
4. Otherwise it calls ``before_run`` with the operation ID (the caller
   charges its tool budget there and may refuse), persists the
   operation as ``pending`` before the handler runs, runs the handler
   on a worker thread so the event loop is never blocked, and always
   finishes the record, whatever happens.

The handler is bounded by the tool's declared timeout, or by the
caller's remaining active budget when that is shorter (section 11). A
timed-out mutating tool may have changed something, so its outcome is
``unknown_outcome``; a timed-out read-only tool is a failed result. A
handler still running after its bound is left to finish on its daemon
thread; its late result never rewrites the recorded outcome (the store
only finishes pending operations). An error from the worker itself is
recorded as such, never as a timeout. If execution is interrupted after
the pending record (an exception or a cancellation), the record is
finished as ``unknown_outcome`` for a mutating tool and ``failed`` for
a read-only one before the interruption propagates.

Section 11 allows one retry of a read-only call after a transient
failure (an unreachable service or a timeout). ``may_retry`` says when;
the retry is attempt 2, a new operation with its own ID, charged like
any execution.

Tool results and arguments are redacted (RM12, section 8) before they
are persisted, and the redacted result is what callers get back, so a
replay and a first run look the same.

This is controlled deduplication within one gateway store. It does not
claim exactly-once delivery to external services (RM10).
"""

import asyncio
import hashlib
import sqlite3
import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from relaymind.canonical import canonical_json
from relaymind.redaction import Redactor
from relaymind.store import RunStore, TraceClock
from relaymind.tools.registry import (
    RegisteredTool,
    SideEffect,
    ToolRegistry,
    ToolResult,
    ToolStatus,
    utc_timestamp,
)

PENDING = "pending"

# Section 11: the failures a read-only call may be retried once after.
TRANSIENT_ERRORS: frozenset[str] = frozenset({"tool_unavailable", "tool_timeout"})
MAX_ATTEMPTS = 2


class OperationInFlightError(RuntimeError):
    """The same logical operation is already running (RM14: never dispatch twice)."""

    def __init__(self, operation_id: str) -> None:
        super().__init__(f"operation {operation_id} is already in flight")
        self.operation_id = operation_id


@dataclass(frozen=True)
class ValidatedCall:
    """A tool call that passed RM09 validation for one run."""

    run_id: str
    tool: RegisteredTool
    arguments: Mapping[str, Any]

    @property
    def tool_name(self) -> str:
        return self.tool.name


@dataclass(frozen=True)
class Execution:
    """One executor outcome.

    result is the redacted, recorded ToolResult. replayed says whether it
    came from the store without running the handler. secrets holds the
    secret-field values the handler returned, so the caller can scrub
    them from model text (RM12); it is empty for a replay.
    time_limit_exhausted identifies a timeout at the caller's limit,
    even when the event-loop timer fires before the next clock tick.
    """

    result: ToolResult
    replayed: bool
    secrets: frozenset[str] = frozenset()
    time_limit_exhausted: bool = False


class ToolExecutor:
    """Runs validated tool calls once per logical operation (RM10)."""

    def __init__(
        self,
        registry: ToolRegistry,
        store: RunStore,
        *,
        clock: TraceClock | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self._clock = clock or TraceClock()
        self.redactor = redactor or Redactor()

    @staticmethod
    def operation_id(
        run_id: str, tool_name: str, arguments: Mapping[str, Any], *, attempt: int = 1
    ) -> str:
        """The logical operation ID: run, tool, canonical arguments, and attempt."""
        identity: dict[str, Any] = {
            "run_id": run_id,
            "tool_name": tool_name,
            "arguments": dict(arguments),
        }
        if attempt != 1:
            identity["attempt"] = attempt
        digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
        return f"op_{digest[:32]}"

    def validate(
        self, run_id: str, tool_name: object, arguments: object, *, available: Collection[str]
    ) -> ValidatedCall:
        """RM09 validation, once; raises ToolRejectedError and runs nothing."""
        tool = self.registry.validate(tool_name, arguments, available=available)
        assert isinstance(arguments, Mapping)  # validate() guarantees a decoded object
        return ValidatedCall(run_id, tool, MappingProxyType(dict(arguments)))

    @staticmethod
    def may_retry(call: ValidatedCall, result: ToolResult, *, attempt: int) -> bool:
        """Section 11: one retry of a read-only call after a transient failure."""
        return (
            attempt < MAX_ATTEMPTS
            and call.tool.side_effect is SideEffect.READ_ONLY
            and result.status is ToolStatus.FAILED
            and result.error is not None
            and result.error.get("code") in TRANSIENT_ERRORS
        )

    async def execute(
        self,
        call: ValidatedCall,
        *,
        proposal_id: str,
        model: str,
        attempt: int = 1,
        before_run: Callable[[str], None] | None = None,
        time_limit: Callable[[], float] | None = None,
        on_finished: Callable[[Execution], None] | None = None,
    ) -> Execution:
        """Replay or run one attempt of a validated call (RM10).

        before_run(operation_id) runs only when the handler is about to
        run; an exception from it stops here with nothing recorded.
        time_limit() gives the caller's remaining active seconds; the
        handler is bounded by the shorter of it and the tool timeout.
        on_finished receives every recorded outcome, including a replay
        or interruption, so the caller can close its trace (RM22).
        """

        def finished(execution: Execution) -> Execution:
            if on_finished is not None:
                on_finished(execution)
            return execution

        if not 1 <= attempt <= MAX_ATTEMPTS:
            raise ValueError(f"attempt must be 1 to {MAX_ATTEMPTS}")
        operation_id = self.operation_id(
            call.run_id, call.tool_name, call.arguments, attempt=attempt
        )
        recorded = self.store.get_tool_operation(operation_id)
        if recorded is not None:
            if recorded["status"] == PENDING:
                raise OperationInFlightError(operation_id)
            return finished(
                Execution(ToolResult.from_json_object(recorded["result"]), replayed=True)
            )
        if before_run is not None:
            before_run(operation_id)
        try:
            self.store.record_tool_operation(
                call.run_id,
                operation_id=operation_id,
                proposal_id=proposal_id,
                tool_name=call.tool_name,
                model=model,
                arguments=self.redactor.redact(dict(call.arguments)),
            )
        except sqlite3.IntegrityError:
            # Another caller recorded the same operation first.
            raise OperationInFlightError(operation_id) from None

        # RM10, RM14: from here on, every exit finishes the pending record,
        # so a failure before the handler runs never leaves it in flight.
        try:
            bound = call.tool.timeout_seconds
            limited_by_caller = False
            if time_limit is not None:
                remaining = time_limit()
                # RM03 / AC07: at equal bounds the active budget is spent
                # when the tool times out, so a retry would have no time
                # left; the timeout counts as the caller's limit.
                limited_by_caller = remaining <= bound
                bound = max(0.0, min(bound, remaining))
            raw = await self._run_bounded(call, operation_id, bound)
        except BaseException:
            # Interrupted after the pending record: never leave it pending.
            finished(self._finish(operation_id, self._interrupted_result(call, operation_id)))
            raise
        return finished(
            replace(
                self._finish(operation_id, raw),
                time_limit_exhausted=(
                    limited_by_caller
                    and raw.error is not None
                    and raw.error.get("code") == "tool_timeout"
                ),
            )
        )

    def _finish(self, operation_id: str, raw: ToolResult) -> Execution:
        """Redact, record the outcome once, and return what was recorded."""
        secrets = self.redactor.secret_values(raw.data)
        recorded = self.redactor.redact(raw.to_json_object())
        result = ToolResult.from_json_object(recorded)
        self.store.finish_tool_operation(operation_id, status=result.status.value, result=recorded)
        return Execution(result, replayed=False, secrets=secrets)

    async def _run_bounded(
        self, call: ValidatedCall, operation_id: str, bound: float
    ) -> ToolResult:
        """The handler's ToolResult, run on a daemon thread within bound seconds."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ToolResult] = loop.create_future()

        def settle(outcome: ToolResult | None, error: BaseException | None) -> None:
            if future.done():
                return  # the bound passed; a late outcome is not recorded
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(outcome)  # type: ignore[arg-type]

        def work() -> None:
            outcome: ToolResult | None = None
            error: BaseException | None = None
            try:
                outcome = self.registry.run(call.tool, call.arguments, operation_id=operation_id)
            except BaseException as exc:  # handed to the event loop, never lost
                error = exc
            try:
                loop.call_soon_threadsafe(settle, outcome, error)
            except RuntimeError:
                pass  # the event loop is closed: the run has already moved on

        threading.Thread(target=work, name=f"tool-{operation_id}", daemon=True).start()
        try:
            return await asyncio.wait_for(future, bound)
        except TimeoutError:
            return self._timeout_result(call, operation_id, bound)
        except Exception as exc:
            return self._worker_error_result(call, operation_id, exc)

    def _result(
        self,
        call: ValidatedCall,
        operation_id: str,
        *,
        status: ToolStatus,
        error: dict[str, str],
        duration_ms: int = 0,
    ) -> ToolResult:
        return ToolResult(
            operation_id=operation_id,
            tool_name=call.tool_name,
            status=status,
            data=None,
            error=error,
            observed_at=utc_timestamp(self._clock.utc_now()),
            duration_ms=duration_ms,
            source_ref=f"tool:{call.tool_name}/{operation_id}",
            truncated=False,
        )

    def _uncertain(self, call: ValidatedCall) -> ToolStatus:
        mutating = call.tool.side_effect is SideEffect.MUTATING
        return ToolStatus.UNKNOWN_OUTCOME if mutating else ToolStatus.FAILED

    def _timeout_result(self, call: ValidatedCall, operation_id: str, bound: float) -> ToolResult:
        status = self._uncertain(call)
        message = f"tool {call.tool_name!r} did not finish within {bound:.3f} s; " + (
            "its outcome is unknown and it is not retried"
            if status is ToolStatus.UNKNOWN_OUTCOME
            else "no result"
        )
        return self._result(
            call,
            operation_id,
            status=status,
            error={"code": "tool_timeout", "message": message},
            duration_ms=round(bound * 1000),
        )

    def _worker_error_result(
        self, call: ValidatedCall, operation_id: str, exc: Exception
    ) -> ToolResult:
        # The type only: an exception message may repeat argument values (RM12).
        return self._result(
            call,
            operation_id,
            status=self._uncertain(call),
            error={
                "code": "tool_error",
                "message": f"tool {call.tool_name!r} worker raised {type(exc).__name__}",
            },
        )

    def _interrupted_result(self, call: ValidatedCall, operation_id: str) -> ToolResult:
        return self._result(
            call,
            operation_id,
            status=self._uncertain(call),
            error={
                "code": "tool_interrupted",
                "message": f"execution of {call.tool_name!r} was interrupted before an outcome",
            },
        )
