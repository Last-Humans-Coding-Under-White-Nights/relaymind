"""AC12: cancellation during generation and in-flight writes (RM10, RM14, RM17, #29).

AC12: cancel during generation and restart during a write. Dispatch
stops; actual or unknown outcomes remain recorded; no automatic write
replay. The restart half is in test_restart_recovery.py; this file is the
cancel half.

Spec 0.19 sections 4.4 (RM14), 5 (RM17), and 7.4 (cancel). Cancellation
is best effort for in-flight work and cannot undo a committed action.
All model output comes from scripted doubles (spec section 13); the
sandbox is a disposable copy.
"""

import asyncio
import threading

from api_helpers import make_app
from gateway_helpers import (
    build_gateway,
    executed_tools,
    health_tail,
    replace_handler,
    route_turn,
    text_turn,
    tool_turn,
)
from helpers import ScriptedAdapter, of_type
from native_helpers import GatedAdapter, error_of, gated_gateway, session, settled, start_run
from relaymind.adapters import Message, Role
from relaymind.orchestrator.local_loop import ConfiguredModel
from relaymind.orchestrator.run_state import RunState, TerminalStateError
from relaymind.store.trace_event import validate_trace_event
from relaymind.tools import CallerContext
from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION

CALLER = CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION}))
PROMPT = "Delete the demo-orders-db sandbox database."
DELETE = {"database_id": "demo-orders-db", "expected_revision": 3}
ANSWER = "Deleted demo-orders-db."


def user(text):
    return (Message(role=Role.USER, content=text),)


class GatedFrom(ScriptedAdapter):
    """A scripted model whose generation number `index` (from 0) waits for a gate."""

    def __init__(self, *args, index, **kwargs):
        super().__init__(*args, **kwargs)
        self.index = index
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.released = []

    async def generate(self, request):
        if len(self.requests) == self.index:
            self.started.set()
            try:
                await self.gate.wait()
            finally:
                self.released.append(request.generation_id)
        async for event in super().generate(request):
            yield event


def delete_gateway(tmp_path, **options):
    """A delete run: route, list, propose the delete, then the answer turns."""
    turns = [
        route_turn("delete-demo-database"),
        tool_turn("list_demo_databases", call_id="call_list"),
        tool_turn("delete_demo_database", call_id="call_delete", **DELETE),
        text_turn(ANSWER),
        text_turn(ANSWER),
    ]
    return build_gateway(tmp_path, turns, caller=CALLER, external_turns=[], **options)


def block_handler(gateway, name):
    """Make a tool block on a worker thread until released; count its calls."""
    original = gateway.tools.get(name).declaration.handler
    entered, release, calls = threading.Event(), threading.Event(), []

    def blocked(arguments):
        calls.append(arguments)
        entered.set()
        release.wait(5)
        return original(arguments)

    replace_handler(gateway, name, blocked, timeout_seconds=30.0)
    return entered, release, calls


async def wait_for(flag: threading.Event):
    assert await asyncio.to_thread(flag.wait, 5), "the handler never started"


async def approved_write_in_flight(gateway):
    """Run to the confirmation, approve it, and return when the write is in flight."""
    waiting = await gateway.orchestrator.run_text(PROMPT)
    assert waiting.state is RunState.WAITING_CONFIRMATION
    request = waiting.confirmation
    version = gateway.store.get_run(waiting.run_id).version
    started = gateway.orchestrator.start_confirmation(
        waiting.run_id,
        version,
        approval_id=request.approval_id,
        token=request.token,
        approve=True,
    )
    return started


# -- cancel during generation ------------------------------------------------------


def test_cancel_during_local_generation_stops_dispatch_and_cancels_the_model(tmp_path):
    """RM14, RM17, AC12: a cancelled local generation is asked to stop; nothing dispatches."""
    gateway, adapter = gated_gateway(tmp_path)

    async def go():
        started = gateway.orchestrator.start(user("Is staging-api healthy right now?"))
        await adapter.started.wait()
        version = gateway.store.get_run(started.run_id).version
        record = await gateway.orchestrator.cancel(started.run_id, version)
        # The model would now answer: a late result must change nothing.
        adapter.gate.set()
        await asyncio.sleep(0.05)
        return started.run_id, record

    run_id, record = asyncio.run(go())

    assert record.state == "cancelled"
    events = gateway.store.events(run_id)
    (cancelled,) = of_type(events, "run.cancelled")
    assert cancelled.payload["reason"] == "cancel_requested"
    validate_trace_event(cancelled.to_json_object())
    (started_call,) = of_type(events, "model.started")
    # The adapter was told to stop the very generation that was in flight.
    assert adapter.cancelled == [started_call.payload["call_id"]]
    assert executed_tools(events) == []
    assert of_type(events, "answer.delta") == []
    assert len(of_type(events, "model.started")) == 1
    assert gateway.store.active_run_id() is None


def test_cancel_during_external_generation_stops_dispatch_and_cancels_the_model(tmp_path):
    """RM14, RM17, AC12: an external generation is cancelled; a late result is dropped."""
    external = GatedAdapter(turns=[tool_turn("get_service_health", service="staging-api")])
    external.locality = "external"
    gateway = build_gateway(
        tmp_path,
        [],
        external_model=ConfiguredModel(adapter=external, model_id="gated-external"),
    )

    async def go():
        started = gateway.orchestrator.start(
            user("Is staging-api healthy right now?"), execution_mode="external_only"
        )
        await external.started.wait()
        version = gateway.store.get_run(started.run_id).version
        record = await gateway.orchestrator.cancel(started.run_id, version)
        # The provider answers late with a tool call: it must not dispatch.
        external.gate.set()
        await asyncio.sleep(0.05)
        return started.run_id, record

    run_id, record = asyncio.run(go())

    assert record.state == "cancelled"
    events = gateway.store.events(run_id)
    (cancelled,) = of_type(events, "run.cancelled")
    (call,) = of_type(events, "model.started")
    assert call.actor == "external"
    assert external.cancelled == [call.payload["call_id"]]
    assert cancelled.payload["external_calls"] == 1
    assert executed_tools(events) == []
    assert of_type(events, "answer.delta") == []
    assert gateway.store.active_run_id() is None


# -- cancel during a write -----------------------------------------------------------


def test_cancel_during_a_write_records_unknown_outcome_and_never_replays_it(tmp_path):
    """RM10, RM14, AC12: an in-flight write keeps a recorded outcome; nothing runs it again."""
    gateway = delete_gateway(tmp_path)
    entered, release, calls = block_handler(gateway, "delete_demo_database")

    async def go():
        started = await approved_write_in_flight(gateway)
        await wait_for(entered)
        version = gateway.store.get_run(started.run_id).version
        record = await gateway.orchestrator.cancel(started.run_id, version)
        before = gateway.store.events(started.run_id)
        # The abandoned handler finishes late: it may leave evidence, never a new effect.
        release.set()
        await asyncio.sleep(0.1)
        return started.run_id, record, before

    run_id, record, before = asyncio.run(go())

    assert record.state == "cancelled"
    events = gateway.store.events(run_id)
    (finished,) = [
        e
        for e in of_type(events, "tool.finished")
        if e.payload["tool_name"] == "delete_demo_database"
    ]
    result = finished.payload["result"]
    assert result["status"] == "unknown_outcome"
    assert result["error"]["code"] == "tool_interrupted"
    operation = gateway.store.get_tool_operation(finished.payload["operation_id"])
    assert operation["status"] == "unknown_outcome"
    # The late handler result did not rewrite the record or the trace.
    assert gateway.store.events(run_id) == before
    assert len(calls) == 1
    (cancelled,) = of_type(events, "run.cancelled")
    assert cancelled.payload["reason"] == "cancel_requested"
    assert of_type(events, "run.completed") == []
    assert len(of_type(events, "model.started")) == 3
    # The outcome is on the trace before the run ends (section 7.4).
    assert finished.seq < cancelled.seq
    assert gateway.store.active_run_id() is None


def test_cancel_during_a_read_records_it_as_failed(tmp_path):
    """RM10, AC12: a read has no side effect to be unsure of, so an interrupted read is failed."""
    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), *health_tail()],
        external_turns=[],
    )
    entered, release, calls = block_handler(gateway, "get_service_health")

    async def go():
        started = gateway.orchestrator.start(user("Is staging-api healthy right now?"))
        await wait_for(entered)
        version = gateway.store.get_run(started.run_id).version
        record = await gateway.orchestrator.cancel(started.run_id, version)
        release.set()
        await asyncio.sleep(0.05)
        return started.run_id, record

    run_id, record = asyncio.run(go())

    assert record.state == "cancelled"
    events = gateway.store.events(run_id)
    (finished,) = of_type(events, "tool.finished")
    assert finished.payload["result"]["status"] == "failed"
    assert finished.payload["result"]["error"]["code"] == "tool_interrupted"
    assert len(calls) == 1
    assert len(of_type(events, "model.started")) == 2


def test_a_cancelled_run_is_terminal_and_cannot_continue_the_write(tmp_path):
    """RM17, RM14, AC12: no transition from cancelled; a second cancel is a terminal error."""
    gateway = delete_gateway(tmp_path)
    entered, release, calls = block_handler(gateway, "delete_demo_database")

    async def go():
        started = await approved_write_in_flight(gateway)
        await wait_for(entered)
        version = gateway.store.get_run(started.run_id).version
        await gateway.orchestrator.cancel(started.run_id, version)
        final = gateway.store.get_run(started.run_id)
        try:
            await gateway.orchestrator.cancel(started.run_id, final.version)
        except TerminalStateError:
            terminal = True
        else:
            terminal = False
        release.set()
        await asyncio.sleep(0.05)
        return terminal

    assert asyncio.run(go()) is True
    assert len(calls) == 1


def test_cancel_after_a_write_succeeded_keeps_the_actual_outcome(tmp_path):
    """RM10, RM14, AC12: a cancel during the next generation keeps the write as succeeded."""
    adapter = GatedFrom(
        turns=[
            route_turn("delete-demo-database"),
            tool_turn("list_demo_databases", call_id="call_list"),
            tool_turn("delete_demo_database", call_id="call_delete", **DELETE),
            text_turn(ANSWER),
        ],
        index=3,
    )
    gateway = build_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="gated"),
        caller=CALLER,
        external_turns=[],
    )

    async def go():
        waiting = await gateway.orchestrator.run_text(PROMPT)
        request = waiting.confirmation
        version = gateway.store.get_run(waiting.run_id).version
        started = gateway.orchestrator.start_confirmation(
            waiting.run_id,
            version,
            approval_id=request.approval_id,
            token=request.token,
            approve=True,
        )
        await adapter.started.wait()
        version = gateway.store.get_run(started.run_id).version
        record = await gateway.orchestrator.cancel(started.run_id, version)
        adapter.gate.set()
        await asyncio.sleep(0.05)
        return started.run_id, record

    run_id, record = asyncio.run(go())

    assert record.state == "cancelled"
    events = gateway.store.events(run_id)
    (finished,) = [
        e
        for e in of_type(events, "tool.finished")
        if e.payload["tool_name"] == "delete_demo_database"
    ]
    assert finished.payload["result"]["status"] == "succeeded"
    operation = gateway.store.get_tool_operation(finished.payload["operation_id"])
    assert operation["status"] == "succeeded"
    assert "demo-orders-db" not in [db["database_id"] for db in gateway.sandbox.list_databases()]
    assert of_type(events, "answer.delta") == []


def test_native_cancel_during_a_write_answers_cancelled_and_keeps_the_outcome(tmp_path):
    """RM21, RM10, AC12: the native cancel endpoint reports cancelled; the write stays unknown."""
    gateway = delete_gateway(tmp_path)
    entered, release, calls = block_handler(gateway, "delete_demo_database")
    app = make_app(gateway)

    async def go():
        async with session(app) as client:
            accepted = (await start_run(client, PROMPT)).json()
            view = await settled(client, accepted["run_id"])
            assert view["state"] == "waiting_confirmation"
            approval = view["pending_approval"]
            resumed = await client.post(
                f"/relay/v1/runs/{accepted['run_id']}/resume",
                json={
                    "expected_version": view["state_version"],
                    "approval_id": approval["approval_id"],
                    "token": approval["token"],
                    "approve": True,
                },
            )
            assert resumed.status_code == 202, resumed.text
            await wait_for(entered)
            current = (await client.get(f"/relay/v1/runs/{accepted['run_id']}")).json()
            cancelled = await client.post(
                f"/relay/v1/runs/{accepted['run_id']}/cancel",
                json={"expected_version": current["state_version"]},
            )
            again = await client.post(
                f"/relay/v1/runs/{accepted['run_id']}/cancel",
                json={"expected_version": cancelled.json()["state_version"]},
            )
            release.set()
            await asyncio.sleep(0.05)
            return accepted, cancelled, again

    accepted, cancelled, again = asyncio.run(go())

    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert (again.status_code, error_of(again)["code"]) == (409, "run_terminal")
    events = gateway.store.events(accepted["run_id"])
    (finished,) = [
        e
        for e in of_type(events, "tool.finished")
        if e.payload["tool_name"] == "delete_demo_database"
    ]
    assert finished.payload["result"]["status"] == "unknown_outcome"
    assert len(calls) == 1
