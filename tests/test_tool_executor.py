"""Tool executor: operation IDs, replay dedup, unknown_outcome, retries.

RM10: the executor persists a logical operation ID before a side effect.
Replays use the recorded result for that operation. A changed argument
set is a new proposal and is validated again. A timed-out write with an
unknown outcome is marked unknown_outcome and is never retried
automatically. This is controlled deduplication, not exactly-once
delivery. RM09: nothing runs before the full name and arguments are
validated, and a call is validated once. RM14: a pending operation is in
flight, never a replay. Section 11: the tool timeout is the declared one
(at most two seconds), shortened by the caller's remaining budget; one
read-only retry after a transient failure. RM12 / section 8: results and
arguments are redacted before persistence. Section 8: every outcome is a
ToolResult envelope. Issue #11; AC05 (replay part), AC12 groundwork.
"""

import asyncio
import threading
import time

import pytest

from helpers import FakeClock
from relaymind.redaction import Redactor
from relaymind.store import RunStore
from relaymind.tools import (
    SideEffect,
    ToolDeclaration,
    ToolRegistry,
    ToolRejectedError,
    ToolStatus,
    ToolUnavailableError,
    validate_tool_result,
)
from relaymind.tools.executor import OperationInFlightError, ToolExecutor

SERVICE_INPUT = {
    "type": "object",
    "properties": {
        "service": {"enum": ["staging-api", "payments-api"]},
        "password": {"type": "string"},
    },
    "required": ["service"],
    "additionalProperties": False,
}
SERVICE_OUTPUT = {
    "type": "object",
    "properties": {
        "service": {"type": "string"},
        "calls": {"type": "integer"},
        "token": {"type": "string"},
    },
    "required": ["service", "calls"],
    "additionalProperties": False,
}


class CountingHandler:
    """A handler that counts how often it really ran."""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls = 0
        self.delay = delay
        self.seen_operations: list[dict | None] = []
        self.observe = None

    def __call__(self, args):
        if self.observe is not None:
            self.seen_operations.append(self.observe(args))
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return {"service": args["service"], "calls": self.calls}


def declaration(name, handler, *, side_effect=SideEffect.READ_ONLY, timeout=1.0):
    return ToolDeclaration(
        name=name,
        description=f"Test tool {name}.",
        input_schema=SERVICE_INPUT,
        output_schema=SERVICE_OUTPUT,
        timeout_seconds=timeout,
        side_effect=side_effect,
        handler=handler,
        required_permission="demo.read",
    )


@pytest.fixture
def store():
    with RunStore(clock=FakeClock()) as run_store:
        yield run_store


@pytest.fixture
def run_id(store):
    return store.create_run("local_running").run_id


def make_executor(store, *declarations):
    registry = ToolRegistry(clock=FakeClock())
    for item in declarations:
        registry.register(item)
    return ToolExecutor(registry, store, clock=FakeClock())


def validated(executor, run_id, name="read_service", service="staging-api", **extra):
    return executor.validate(run_id, name, {"service": service, **extra}, available={name})


def call(executor, run_id, name="read_service", service="staging-api", **options):
    proposal = validated(executor, run_id, name, service)
    return asyncio.run(
        executor.execute(proposal, proposal_id="proposal_1", model="double-local", **options)
    )


def operation_id(executor, run_id, name="read_service", service="staging-api", attempt=1):
    return executor.operation_id(run_id, name, {"service": service}, attempt=attempt)


# --- RM10: operation ID persisted before the handler runs --------------------


def test_operation_id_is_persisted_before_the_handler_runs(store, run_id):
    handler = CountingHandler()
    executor = make_executor(store, declaration("read_service", handler))
    expected_id = operation_id(executor, run_id)
    handler.observe = lambda args: store.get_tool_operation(expected_id)

    execution = call(executor, run_id)

    (seen,) = handler.seen_operations
    assert seen is not None, "no operation record existed when the handler ran"
    assert seen["status"] == "pending"
    assert seen["run_id"] == run_id
    assert seen["proposal_id"] == "proposal_1"
    assert seen["model"] == "double-local"
    assert seen["arguments"] == {"service": "staging-api"}
    assert execution.result.operation_id == expected_id


def test_the_recorded_operation_holds_the_final_result(store, run_id):
    executor = make_executor(store, declaration("read_service", CountingHandler()))
    execution = call(executor, run_id)
    record = store.get_tool_operation(execution.result.operation_id)
    assert record["status"] == "succeeded"
    assert record["result"] == execution.result.to_json_object()
    validate_tool_result(record["result"])


def test_operation_id_is_stable_for_the_same_logical_call(store, run_id):
    executor = make_executor(store, declaration("read_service", CountingHandler()))
    first = operation_id(executor, run_id)
    again = operation_id(executor, run_id)
    other_run = operation_id(executor, "run_other")
    retry = operation_id(executor, run_id, attempt=2)
    assert first == again
    assert len({first, other_run, retry}) == 3
    assert first.startswith("op_")


# --- RM10: replay returns the recorded result -------------------------------


def test_replaying_the_same_operation_returns_the_recorded_result(store, run_id):
    handler = CountingHandler()
    executor = make_executor(store, declaration("read_service", handler))

    first = call(executor, run_id)
    replay = call(executor, run_id)

    assert handler.calls == 1, "the replay ran the handler again"
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.result == first.result


def test_a_replay_across_executor_instances_uses_the_store(store, run_id):
    # The record lives in SQLite, not in executor memory.
    handler = CountingHandler()
    first = call(make_executor(store, declaration("read_service", handler)), run_id)
    replay = call(make_executor(store, declaration("read_service", handler)), run_id)
    assert handler.calls == 1
    assert replay.replayed is True
    assert replay.result == first.result


def test_a_failed_operation_replays_as_failed_without_rerunning(store, run_id):
    calls = []

    def broken(args):
        calls.append(args)
        raise RuntimeError("boom")

    executor = make_executor(store, declaration("read_service", broken))
    first = call(executor, run_id)
    replay = call(executor, run_id)
    assert first.result.status is ToolStatus.FAILED
    assert replay.replayed is True
    assert replay.result == first.result
    assert len(calls) == 1


def test_before_run_is_called_only_for_a_real_execution(store, run_id):
    # RM03: the caller charges its tool budget here, so a replay is free.
    executor = make_executor(store, declaration("read_service", CountingHandler()))
    seen = []
    call(executor, run_id, before_run=seen.append)
    call(executor, run_id, before_run=seen.append)
    assert seen == [operation_id(executor, run_id)]


def test_a_refusing_before_run_records_and_runs_nothing(store, run_id):
    handler = CountingHandler()
    executor = make_executor(store, declaration("read_service", handler))

    def refuse(_operation_id):
        raise RuntimeError("tool budget exhausted")

    with pytest.raises(RuntimeError):
        call(executor, run_id, before_run=refuse)
    assert store.get_tool_operation(operation_id(executor, run_id)) is None
    assert handler.calls == 0


# --- RM09: validation happens once, before anything runs --------------------


def test_execute_does_not_validate_again(store, run_id):
    executor = make_executor(store, declaration("read_service", CountingHandler()))
    proposal = validated(executor, run_id)
    checks = []
    original = executor.registry.validate
    executor.registry.validate = lambda *a, **k: checks.append(a) or original(*a, **k)
    asyncio.run(executor.execute(proposal, proposal_id="p", model="m"))
    assert checks == []


def test_changed_arguments_create_a_new_operation(store, run_id):
    handler = CountingHandler()
    executor = make_executor(store, declaration("read_service", handler))

    first = call(executor, run_id, service="staging-api")
    changed = call(executor, run_id, service="payments-api")

    assert changed.replayed is False
    assert changed.result.operation_id != first.result.operation_id
    assert handler.calls == 2
    assert changed.result.data["service"] == "payments-api"


def test_changed_arguments_are_validated_again_before_execution(store, run_id):
    handler = CountingHandler()
    executor = make_executor(store, declaration("read_service", handler))
    call(executor, run_id, service="staging-api")

    with pytest.raises(ToolRejectedError) as rejected:
        validated(executor, run_id, region="eu")

    assert rejected.value.code == "invalid_arguments"
    assert handler.calls == 1


def test_a_rejected_call_records_no_operation(store, run_id):
    handler = CountingHandler()
    executor = make_executor(store, declaration("read_service", handler))
    with pytest.raises(ToolRejectedError):
        validated(executor, run_id, service="prod-db")
    assert store.get_tool_operation(operation_id(executor, run_id, service="prod-db")) is None
    assert handler.calls == 0


def test_a_tool_outside_the_available_set_never_runs(store, run_id):
    handler = CountingHandler()
    executor = make_executor(store, declaration("read_service", handler))
    with pytest.raises(ToolRejectedError) as rejected:
        executor.validate(run_id, "read_service", {"service": "staging-api"}, available=set())
    assert rejected.value.code == "tool_not_permitted"
    assert handler.calls == 0


def test_frozen_adapter_arguments_are_one_decoded_object(store, run_id):
    # A ToolCall freezes its arguments (RM11 binding); RM09 still treats
    # that mapping as the decoded argument object.
    from relaymind.adapters import ToolCall

    handler = CountingHandler()
    executor = make_executor(store, declaration("read_service", handler))
    proposal = ToolCall(
        call_id="c1", tool_name="read_service", arguments={"service": "staging-api"}
    )
    checked = executor.validate(
        run_id, proposal.tool_name, proposal.arguments, available={"read_service"}
    )
    execution = asyncio.run(executor.execute(checked, proposal_id="p", model="m"))
    assert execution.result.status is ToolStatus.SUCCEEDED
    assert handler.calls == 1


# --- RM10 / section 11: timeouts, interruptions, worker errors ----------------


def test_a_write_timeout_is_marked_unknown_outcome(store, run_id):
    handler = CountingHandler(delay=0.5)
    executor = make_executor(
        store,
        declaration("write_service", handler, side_effect=SideEffect.MUTATING, timeout=0.05),
    )

    execution = call(executor, run_id, name="write_service")

    result = execution.result
    assert result.status is ToolStatus.UNKNOWN_OUTCOME
    assert result.data is None
    assert result.error["code"] == "tool_timeout"
    validate_tool_result(result.to_json_object())
    assert store.get_tool_operation(result.operation_id)["status"] == "unknown_outcome"


def test_an_unknown_outcome_is_never_retried_automatically(store, run_id):
    handler = CountingHandler(delay=0.5)
    executor = make_executor(
        store,
        declaration("write_service", handler, side_effect=SideEffect.MUTATING, timeout=0.05),
    )
    first = call(executor, run_id, name="write_service")
    retry = call(executor, run_id, name="write_service")

    assert retry.replayed is True
    assert retry.result == first.result
    assert retry.result.status is ToolStatus.UNKNOWN_OUTCOME
    proposal = validated(executor, run_id, "write_service")
    assert executor.may_retry(proposal, first.result, attempt=1) is False
    time.sleep(0.6)  # let the timed-out handler finish in the background
    assert handler.calls == 1, "the unknown-outcome write was retried"
    # A late completion never rewrites the recorded unknown outcome.
    assert store.get_tool_operation(first.result.operation_id)["status"] == "unknown_outcome"


def test_a_read_timeout_is_a_failed_result(store, run_id):
    # A read has no side effect to be uncertain about: its timeout is a
    # failed observation, not an unknown outcome.
    executor = make_executor(
        store, declaration("read_service", CountingHandler(delay=0.5), timeout=0.05)
    )
    result = call(executor, run_id).result
    assert result.status is ToolStatus.FAILED
    assert result.error["code"] == "tool_timeout"
    validate_tool_result(result.to_json_object())


def test_the_remaining_budget_shortens_the_tool_timeout(store, run_id):
    # Section 11: a tool is bounded by its timeout and the remaining budget.
    executor = make_executor(
        store, declaration("read_service", CountingHandler(delay=1.0), timeout=2.0)
    )
    started = time.monotonic()
    result = call(executor, run_id, time_limit=lambda: 0.1).result
    assert time.monotonic() - started < 0.6
    assert result.error["code"] == "tool_timeout"
    assert result.duration_ms == 100


def test_a_worker_error_is_recorded_as_an_error_not_a_timeout(store, run_id):
    # An exception outside the handler (here, from the registry itself)
    # is captured as tool_error.
    executor = make_executor(store, declaration("read_service", CountingHandler()))

    def explode(*args, **kwargs):
        raise MemoryError("worker died")

    executor.registry.run = explode
    result = call(executor, run_id).result
    assert result.status is ToolStatus.FAILED
    assert result.error["code"] == "tool_error"
    assert "MemoryError" in result.error["message"]
    assert store.get_tool_operation(result.operation_id)["status"] == "failed"


@pytest.mark.parametrize(
    ("side_effect", "status"),
    [(SideEffect.READ_ONLY, "failed"), (SideEffect.MUTATING, "unknown_outcome")],
)
def test_an_interrupted_execution_never_leaves_a_pending_operation(
    store, run_id, side_effect, status
):
    # RM10 / section 5: a cancellation after the pending record still
    # finishes it: failed for a read, unknown_outcome for a write.
    release = threading.Event()

    def slow(args):
        release.wait(2)
        return {"service": args["service"], "calls": 1}

    executor = make_executor(
        store, declaration("svc_tool", slow, side_effect=side_effect, timeout=2.0)
    )
    proposal = validated(executor, run_id, "svc_tool")

    async def cancel_midway():
        task = asyncio.create_task(executor.execute(proposal, proposal_id="p", model="m"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(cancel_midway())
    finally:
        release.set()
    record = store.get_tool_operation(operation_id(executor, run_id, "svc_tool"))
    assert record["status"] == status
    assert record["result"]["error"]["code"] == "tool_interrupted"


def test_a_running_tool_does_not_block_the_event_loop(store, run_id):
    executor = make_executor(
        store, declaration("read_service", CountingHandler(delay=0.3), timeout=2.0)
    )
    proposal = validated(executor, run_id)
    ticks = []

    async def both():
        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.02)

        tick = asyncio.create_task(ticker())
        await executor.execute(proposal, proposal_id="p", model="m")
        tick.cancel()

    asyncio.run(both())
    assert len(ticks) >= 5, "the handler blocked the event loop"


def test_a_pending_operation_is_in_flight_and_does_not_run_twice(store, run_id):
    release = threading.Event()
    calls = []

    def slow(args):
        calls.append(args)
        release.wait(2)
        return {"service": args["service"], "calls": len(calls)}

    executor = make_executor(store, declaration("read_service", slow, timeout=2.0))
    worker = threading.Thread(target=call, args=(executor, run_id))
    worker.start()
    try:
        pending = operation_id(executor, run_id)
        deadline = time.monotonic() + 2
        while store.get_tool_operation(pending) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        charged = []
        with pytest.raises(OperationInFlightError):
            call(executor, run_id, before_run=charged.append)
        assert charged == [], "an in-flight operation was charged as a new execution"
    finally:
        release.set()
        worker.join()
    assert len(calls) == 1


# --- section 11: one read-only retry after a transient failure -----------------


def test_a_transient_read_failure_may_be_retried_once(store, run_id):
    attempts = []

    def flaky(args):
        attempts.append(args)
        if len(attempts) == 1:
            raise ToolUnavailableError("unreachable")
        return {"service": args["service"], "calls": len(attempts)}

    executor = make_executor(store, declaration("read_service", flaky))
    proposal = validated(executor, run_id)
    first = call(executor, run_id)
    assert first.result.error["code"] == "tool_unavailable"
    assert executor.may_retry(proposal, first.result, attempt=1) is True
    second = call(executor, run_id, attempt=2)
    assert second.result.status is ToolStatus.SUCCEEDED
    assert second.result.operation_id == operation_id(executor, run_id, attempt=2)
    assert executor.may_retry(proposal, second.result, attempt=2) is False
    # Both attempts replay from the store; neither runs again.
    assert call(executor, run_id).replayed and call(executor, run_id, attempt=2).replayed
    assert len(attempts) == 2


def test_a_non_transient_failure_or_a_write_is_not_retried(store, run_id):
    def broken(args):
        raise RuntimeError("bug")

    def down(args):
        raise ToolUnavailableError("unreachable")

    executor = make_executor(
        store,
        declaration("read_service", broken),
        declaration("write_service", down, side_effect=SideEffect.MUTATING),
    )
    read = call(executor, run_id)
    write = call(executor, run_id, name="write_service")
    assert executor.may_retry(validated(executor, run_id), read.result, attempt=1) is False
    proposal = validated(executor, run_id, "write_service")
    assert executor.may_retry(proposal, write.result, attempt=1) is False


# --- RM12 / section 8: redaction before persistence ----------------------------


def test_secret_fields_are_redacted_before_persistence(store, run_id):
    def leaky(args):
        return {"service": args["service"], "calls": 1, "token": "s3cr3t-token"}

    executor = make_executor(store, declaration("read_service", leaky))
    executor.redactor = Redactor(secret_fields={"token", "password"})
    proposal = executor.validate(
        run_id,
        "read_service",
        {"service": "staging-api", "password": "hunter2"},
        available={"read_service"},
    )
    execution = asyncio.run(executor.execute(proposal, proposal_id="p", model="m"))
    record = store.get_tool_operation(execution.result.operation_id)
    assert record["arguments"] == {"service": "staging-api", "password": "[redacted]"}
    assert record["result"]["data"]["token"] == "[redacted]"
    assert execution.result.data["token"] == "[redacted]"
    assert execution.secrets == frozenset({"s3cr3t-token"})
    assert "s3cr3t" not in str(record) and "hunter2" not in str(record)


def test_a_time_limit_error_after_the_pending_record_still_finishes_it(store, run_id):
    """RM10, RM14: once the operation is recorded pending, any exception
    finishes it, so a later call is never stuck as in flight."""
    executor = make_executor(store, declaration("read_service", CountingHandler()))

    def broken_limit():
        raise RuntimeError("meter failed")

    with pytest.raises(RuntimeError, match="meter failed"):
        call(executor, run_id, time_limit=broken_limit)
    recorded = store.get_tool_operation(operation_id(executor, run_id))
    assert recorded is not None and recorded["status"] != "pending"
