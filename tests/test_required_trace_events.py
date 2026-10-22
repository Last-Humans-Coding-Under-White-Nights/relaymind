"""Required trace evidence and failure paths (RM16, RM22, RM25; issue #27)."""

import asyncio
import copy
from datetime import timedelta

import jsonschema
import pytest

from gateway_helpers import build_gateway, health_tail, route_turn, run
from helpers import ACCEPTED_PAYLOAD, FakeClock, of_type
from relaymind.store import RunStore
from relaymind.store.trace_event import TRACE_EVENT_SCHEMA, EventType, TraceEventError

TERMINAL = {
    "model_calls": [
        {"call_id": "c1", "model": "local-model", "actor": "local", "purpose": "final"}
    ],
    "external_calls": 0,
    "active_ms": 12,
    "waiting_ms": 0,
}


def accepted(store):
    return store.create_run(
        "accepted", accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD)
    ).run_id


@pytest.mark.parametrize(
    "kind,missing",
    [
        (kind, field)
        for kind in ("run.completed", "run.failed", "run.cancelled")
        for field in (
            ("active_ms", "waiting_ms")
            if kind == "run.cancelled"
            else ("active_ms", "waiting_ms", "model_calls", "external_calls")
        )
    ],
)
def test_terminal_evidence_is_required_at_write_time(kind, missing):
    """RM16/RM22/RM25: dropping required terminal evidence must reject the write."""
    payload = {**TERMINAL, "final_answer_model": "local-model", "error_code": "interrupted"}
    if kind == "run.failed":
        # Spec sections 5 and 8: an interrupted run.failed also names the
        # interrupted state and the settled operations (RM10, RM17).
        payload |= {"interrupted_state": "local_running", "settled_operations": []}
    if kind == "run.cancelled":
        # Spec 0.17 section 8: every run.cancelled names its reason.
        payload["reason"] = "cancel_requested"
    del payload[missing]
    with RunStore() as store:
        run_id = accepted(store)
        with pytest.raises(TraceEventError, match=missing):
            store.append_event(run_id, kind, "gateway", payload)
        assert len(store.events(run_id)) == 1
    event = {
        "schema_version": 1,
        "run_id": "r",
        "seq": 2,
        "timestamp": "2026-10-20T03:00:00Z",
        "elapsed_ms": 12,
        "event_type": kind,
        "actor": "gateway",
        "payload": payload,
    }
    assert not jsonschema.Draft202012Validator(TRACE_EVENT_SCHEMA).is_valid(event)


@pytest.mark.parametrize("field", ["call_id", "model", "actor", "purpose"])
@pytest.mark.parametrize("value", [None, "", " ", 3])
def test_model_call_inventory_requires_identifiable_calls(field, value):
    """RM16: an empty or malformed call inventory entry cannot prove actual calls."""
    payload = copy.deepcopy(TERMINAL)
    payload["final_answer_model"] = "local-model"
    payload["model_calls"][0][field] = value
    with RunStore() as store:
        with pytest.raises(TraceEventError, match="model_calls"):
            store.append_event(accepted(store), EventType.RUN_COMPLETED, "gateway", payload)


@pytest.mark.parametrize("error", [RuntimeError("adapter crashed"), asyncio.CancelledError()])
def test_interrupted_generation_has_a_matching_finished_event(tmp_path, error):
    """RM16/RM22: a throwing adapter must not leave a started model call unfinished."""

    def interrupt(request):
        raise error

    gateway = build_gateway(tmp_path, [route_turn("check-service-health")], on_generate=interrupt)
    with pytest.raises(type(error)):
        run(gateway)
    run_id = gateway.adapter.requests[0].generation_id.split("_gen")[0]
    events = gateway.store.events(run_id)
    (started,) = of_type(events, "model.started")
    (finished,) = of_type(events, "model.finished")
    assert finished.payload["call_id"] == started.payload["call_id"]
    assert finished.payload["model"] == started.payload["model"]
    assert finished.payload["duration_ms"] == 0
    assert finished.payload["error"]["code"] == (
        "cancelled" if isinstance(error, asyncio.CancelledError) else "interrupted"
    )
    assert events[-1].payload["model_calls"] == [
        {key: started.payload[key] for key in ("call_id", "model", "actor", "purpose")}
    ]


def test_completed_trace_accounts_for_each_actual_call_and_uses_monotonic_time(tmp_path):
    """AC01/RM01/RM16/RM22/RM25: UTC clock jumps cannot alter elapsed durations."""
    clock = FakeClock()

    def advance(request):
        clock.mono += 0.125
        clock.now -= timedelta(hours=1)

    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), *health_tail()],
        clock=clock,
        on_generate=advance,
    )
    outcome, events = run(gateway)
    assert outcome.state == "completed"
    assert {
        "run.accepted",
        "route.selected",
        "skill.loaded",
        "model.started",
        "model.finished",
        "tool.proposed",
        "tool.started",
        "tool.finished",
        "answer.delta",
        "run.completed",
    } <= {event.event_type for event in events}
    calls = events[-1].payload["model_calls"]
    assert [call["call_id"] for call in calls] == [
        r.generation_id for r in gateway.adapter.requests
    ]
    assert [call["call_id"] for call in calls] == [
        e.payload["call_id"] for e in of_type(events, "model.finished")
    ]
    assert [call["purpose"] for call in calls] == ["routing", "skill", "skill", "final"]
    assert events[-1].payload["final_answer_model"] == calls[-1]["model"]
    assert events[-1].payload["external_calls"] == len(gateway.external.requests) == 0
    assert events[-1].payload["active_ms"] == 500
    assert events[-1].payload["waiting_ms"] == 0
    assert all(e.payload["duration_ms"] == 125 for e in of_type(events, "model.finished"))
    assert all(e.timestamp.endswith("Z") for e in events)
    assert [e.elapsed_ms for e in events] == sorted(e.elapsed_ms for e in events)
    for event in events:
        jsonschema.Draft202012Validator(TRACE_EVENT_SCHEMA).validate(event.to_json_object())


def test_default_cancellation_separates_waits_without_using_wall_time(tmp_path):
    """RM22/RM25: default cancellation counts time in waiting states separately."""
    from relaymind.orchestrator.run_state import RunStateMachine

    clock = FakeClock()
    with RunStore(tmp_path / "runs.db", clock=clock) as store:
        machine = RunStateMachine(store)
        record = machine.create(accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD))
        clock.advance(0.25)
        record = machine.transition(record.run_id, record.version, "routing")
        clock.advance(0.5)
        record = machine.transition(record.run_id, record.version, "waiting_input")
        clock.advance(2)
        record = machine.resume(record.run_id, record.version)
        clock.advance(0.25)
        record = machine.transition(record.run_id, record.version, "local_running")
        record = machine.transition(record.run_id, record.version, "waiting_confirmation")
        clock.mono += 3
        clock.now -= timedelta(days=1)
        machine.cancel(record.run_id, record.version)
        payload = store.events(record.run_id)[-1].payload
        assert payload["active_ms"] == 1000
        assert payload["waiting_ms"] == 5000


def test_interrupted_tool_records_its_outcome_before_run_cancellation(tmp_path):
    """RM10/RM22: interruption must not lose the finish event of a recorded operation."""
    from gateway_helpers import replace_handler

    gateway = build_gateway(tmp_path, [route_turn("check-service-health"), *health_tail()])

    def interrupt(arguments):
        raise asyncio.CancelledError()

    replace_handler(gateway, "get_service_health", interrupt)
    with pytest.raises(asyncio.CancelledError):
        run(gateway)
    run_id = gateway.adapter.requests[0].generation_id.split("_gen")[0]
    events = gateway.store.events(run_id)
    (started,) = of_type(events, "tool.started")
    (finished,) = of_type(events, "tool.finished")
    assert finished.payload["operation_id"] == started.payload["operation_id"]
    assert finished.payload["model"] == started.payload["model"]
    assert finished.payload["result"] == gateway.store.tool_operations(run_id)[0]["result"]
    assert finished.payload["status"] == "failed"
    assert events[-1].event_type == "run.cancelled"


@pytest.mark.parametrize("scenario", ["confirmation", "handoff", "cached_route"])
def test_future_flow_event_contracts_persist_in_state_scenarios(scenario, tmp_path):
    """RM22: deterministic state/store scenarios for the #19/#18/#31 event contracts.

    These exercise the persistence boundary, not the pending confirmation,
    external execution, or routing cache implementations.
    """
    from relaymind.orchestrator.run_state import RunStateMachine

    clock = FakeClock()
    with RunStore(tmp_path / "runs.db", clock=clock) as store:
        machine = RunStateMachine(store)
        record = machine.create(accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD))
        record = machine.transition(record.run_id, record.version, "routing")
        if scenario == "cached_route":
            kind, payload = EventType.CACHE_HIT, {"originating_version": "router-v1"}
            store.append_event(record.run_id, kind, "gateway", payload)
        else:
            record = machine.transition(record.run_id, record.version, "local_running")
            if scenario == "confirmation":
                kind = EventType.CONFIRMATION_REQUIRED
                payload = {
                    "approval_id": "approval_1",
                    "proposal_id": "p1",
                    "model": "local-model",
                    "tool_name": "delete_demo_database",
                    "arguments": {"database_id": "demo-orders-db", "expected_revision": 1},
                    "target": "demo-orders-db",
                    "state_version": record.version + 1,
                    "expires_at": "2026-10-20T03:05:00.000000Z",
                }
                state = "waiting_confirmation"
            else:
                kind = EventType.HANDOFF_STARTED
                payload = {
                    "reason_code": "complex_skill",
                    "model": "external-model",
                    "skill": None,
                    "operation_ids": [],
                    "allowed_tools": ["get_metrics"],
                    "request": {"size": 900, "limit": 32768, "method": "utf8_bytes"},
                }
                state = "external_running"
            record = machine.transition(
                record.run_id, record.version, state, event=(kind, "gateway", payload)
            )
        clock.advance(0.1)
        machine.cancel(record.run_id, record.version)
        events = store.events(record.run_id)
        assert [e.event_type for e in events] == ["run.accepted", kind, "run.cancelled"]
        assert [e.seq for e in events] == [1, 2, 3]
        assert events[1].payload == payload
        for event in events:
            jsonschema.Draft202012Validator(TRACE_EVENT_SCHEMA).validate(event.to_json_object())


def test_clarification_resume_trace_includes_the_wait(tmp_path):
    """AC03/RM22/RM25: both clarification events are produced by the real local loop."""
    gateway = build_gateway(tmp_path, [route_turn("check-service-health"), *health_tail()])
    outcome, events = run(gateway, "Check service health")
    assert outcome.state == "waiting_input"
    gateway.clock.advance(2)
    resumed = asyncio.run(
        gateway.orchestrator.resume(
            outcome.run_id, gateway.store.get_run(outcome.run_id).version, "staging-api"
        )
    )
    events = gateway.store.events(outcome.run_id)
    assert resumed.state == "completed"
    (required,) = of_type(events, "clarification.required")
    (answered,) = of_type(events, "clarification.answered")
    assert required.payload["missing"] == answered.payload["missing"] == "service"
    assert answered.payload["waited_ms"] == events[-1].payload["waiting_ms"] == 2000
    assert required.seq < answered.seq


def test_cancellation_timings_survive_reopen_without_counting_downtime(tmp_path):
    """RM25: persisted intervals retain waits even when another store cancels the run."""
    from relaymind.orchestrator.run_state import RunStateMachine

    clock = FakeClock()
    path = tmp_path / "runs.db"
    with RunStore(path, clock=clock) as store:
        machine = RunStateMachine(store)
        record = machine.create(accepted=(EventType.RUN_ACCEPTED, "gateway", ACCEPTED_PAYLOAD))
        record = machine.transition(record.run_id, record.version, "routing")
        clock.advance(1)
        record = machine.transition(record.run_id, record.version, "waiting_input")
        clock.advance(2)
        record = machine.resume(record.run_id, record.version)
        clock.advance(1)
        # Record an event to pin the final measured elapsed time before close.
        store.append_event(
            record.run_id, EventType.CACHE_HIT, "gateway", {"originating_version": "v1"}
        )
    clock.advance(1000)
    with RunStore(path, clock=clock) as store:
        RunStateMachine(store).cancel(record.run_id, record.version)
        assert store.events(record.run_id)[-1].payload == {
            "reason": "cancel_requested",
            "active_ms": 2000,
            "waiting_ms": 2000,
        }
