"""AC01: a simple health request completes locally, in each language.

Spec section 13, AC01: a simple health request with a valid service in
each language gives local skill selection, a real sandbox tool call, the
correct final status, and zero external calls. Requirements RM01 to
RM03, RM06, RM16, RM23. Deterministic with model doubles; the live
check-service-health trace is recorded in docs/EVIDENCE.md and runs as
tests/test_local_loop_live.py (pytest -m live).

RM01: zero external inference calls, measured on a recording external
model double that the gateway is configured with, and the trace agrees. RM02: the
local model selects the skill; the gateway builds the RouteDecision.
RM03: budgets are recorded in the run. RM06: skill.loaded carries name,
version, and hash. RM16: the completed run names the final-answer model
and every actual model call; the answer keeps the returned status.
RM23: the tool call runs against the labeled sandbox data service.
Section 7.3: the final generation runs with tools disabled.
"""

import pytest

from gateway_helpers import LOCAL_MODEL_ID, build_gateway, route_turn, run, text_turn, tool_turn
from helpers import of_type
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.run_state import RunState
from relaymind.skills.predicates import health_status_preserved
from relaymind.tools import ToolResult

CASES = {
    "en-staging": (
        "Is staging-api healthy right now?",
        "en",
        "staging-api",
        "healthy",
        "2026-10-20T02:58:00Z",
        "staging-api is healthy (checked at 2026-10-20T02:58:00Z).",
    ),
    "zh-staging": (
        "staging-api 现在健康吗？",
        "zh",
        "staging-api",
        "healthy",
        "2026-10-20T02:58:00Z",
        "staging-api 的状态是 healthy（健康），检查时间为 2026-10-20T02:58:00Z。",
    ),
    "en-payments": (
        "Check the health of payments-api.",
        "en",
        "payments-api",
        "unavailable",
        "2026-10-20T02:59:00Z",
        "payments-api is unavailable (checked at 2026-10-20T02:59:00Z).",
    ),
    "zh-payments": (
        "帮我检查一下 payments-api 的健康状态。",
        "zh",
        "payments-api",
        "unavailable",
        "2026-10-20T02:59:00Z",
        "payments-api 的状态是 unavailable（不可用），检查时间为 2026-10-20T02:59:00Z。",
    ),
}


def run_case(tmp_path, case):
    prompt, language, service, _, _, answer = CASES[case]
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health", language),
            tool_turn("get_service_health", service=service),
            text_turn(answer),  # finalize proposal (section 7.3)
            text_turn(answer[:12], answer[12:]),  # final generation, tools disabled
        ],
    )
    outcome, events = run(gateway, prompt)
    return gateway, outcome, events


@pytest.mark.parametrize("case", CASES)
def test_the_run_completes_locally_with_the_returned_status(tmp_path, case):
    _, language, _, status, checked_at, answer = CASES[case]
    gateway, outcome, events = run_case(tmp_path, case)

    assert outcome.state is RunState.COMPLETED
    assert outcome.answer == answer
    assert status in outcome.answer and checked_at in outcome.answer
    assert gateway.store.get_run(outcome.run_id).state == "completed"


@pytest.mark.parametrize("case", CASES)
def test_the_local_model_selected_the_skill(tmp_path, case):
    _, language, _, _, _, _ = CASES[case]
    gateway, outcome, events = run_case(tmp_path, case)

    (selected,) = of_type(events, "route.selected")
    assert selected.actor == "local"
    assert selected.payload["decision"] == {
        "schema_version": 1,
        "route": "local",
        "skill_id": "check-service-health",
        "language": language,
        "reason_code": "simple_skill",
        "clarification": None,
    }
    assert selected.payload["mechanism"] == "native_function_calling"
    assert selected.payload["model"] == LOCAL_MODEL_ID
    skill = gateway.skills.get("check-service-health")
    (loaded,) = of_type(events, "skill.loaded")
    assert loaded.payload == {
        "name": "check-service-health",
        "version": skill.version,
        "content_hash": skill.content_hash,
    }
    # RM07: the loaded snapshot is the one run.accepted offered.
    (accepted,) = of_type(events, "run.accepted")
    assert loaded.payload in accepted.payload["skills_offered"]


@pytest.mark.parametrize("case", CASES)
def test_the_tool_ran_against_the_sandbox(tmp_path, case):
    _, _, service, status, checked_at, _ = CASES[case]
    gateway, outcome, events = run_case(tmp_path, case)

    (proposed,) = of_type(events, "tool.proposed")
    (started,) = of_type(events, "tool.started")
    (finished,) = of_type(events, "tool.finished")
    assert proposed.payload["tool_name"] == "get_service_health"
    assert proposed.payload["arguments"] == {"service": service}
    assert proposed.payload["skill_id"] == "check-service-health"
    assert started.payload["proposal_id"] == proposed.payload["proposal_id"]
    for event in (proposed, started, finished):
        assert event.payload["model"] == LOCAL_MODEL_ID

    result = finished.payload["result"]
    assert result["status"] == "succeeded"
    assert result["data"]["status"] == status
    assert result["data"]["checked_at"] == checked_at
    assert result["data"]["fixture"]["scenario"] == "baseline"
    record = gateway.store.get_tool_operation(started.payload["operation_id"])
    assert record["status"] == "succeeded"
    assert record["result"] == result


@pytest.mark.parametrize("case", CASES)
def test_zero_external_calls_are_measured_and_the_trace_agrees(tmp_path, case):
    gateway, outcome, events = run_case(tmp_path, case)

    # Measured: the configured external model double was never called,
    # and every call the trace lists reached the local double.
    assert gateway.external.requests == []
    started = of_type(events, "model.started")
    assert [r.generation_id for r in gateway.adapter.requests] == [
        e.payload["call_id"] for e in started
    ]
    assert [e.payload["purpose"] for e in started] == ["routing", "skill", "skill", "final"]
    # The trace labels each call with the adapter's declared locality.
    assert gateway.adapter.locality == "local"
    assert all(e.actor == "local" and e.payload["model"] == LOCAL_MODEL_ID for e in started)
    (completed,) = of_type(events, "run.completed")
    assert completed.payload["final_answer_model"] == LOCAL_MODEL_ID
    assert completed.payload["external_calls"] == len(gateway.external.requests)
    assert [call["call_id"] for call in completed.payload["model_calls"]] == [
        e.payload["call_id"] for e in started
    ]


@pytest.mark.parametrize("case", CASES)
def test_the_budgets_and_their_use_are_recorded(tmp_path, case):
    gateway, outcome, events = run_case(tmp_path, case)

    (accepted,) = of_type(events, "run.accepted")
    assert (
        accepted.payload["budgets"]
        == LocalBudgets().to_json_object()
        == {
            "routing_generations": 1,
            "local_generations": 3,
            "tool_calls": 2,
            "repair_generations": 1,
            "active_seconds": 4.0,
            "routing_input_tokens": 2048,
            "routing_output_tokens": 128,
            # Spec 0.6 section 11: entire-run limits, waiting, and rounds.
            "run_active_seconds": 60.0,
            "run_generations": 8,
            "run_tool_executions": 6,
            "waiting_seconds": 300.0,
            "clarification_rounds": 2,
            # Spec 0.9 section 11: the external stage limits.
            "external_generations": 6,
            "external_seconds": 30.0,
        }
    )
    offered = accepted.payload["skills_offered"]
    assert [s["name"] for s in offered] == list(gateway.skills.names())
    (completed,) = of_type(events, "run.completed")
    usage = completed.payload["budget_usage"]
    assert usage["routing_generations"] == 1
    assert usage["local_generations"] == 3
    assert usage["tool_calls"] == 1
    assert usage["repair_generations"] == 0


@pytest.mark.parametrize("case", CASES)
def test_the_answer_is_the_final_generation_with_tools_disabled(tmp_path, case):
    gateway, outcome, events = run_case(tmp_path, case)

    final_request = gateway.adapter.requests[-1]
    assert final_request.tool_schemas == ()
    deltas = of_type(events, "answer.delta")
    assert "".join(e.payload["text"] for e in deltas) == outcome.answer
    assert all(e.actor == "local" for e in deltas)
    finished = of_type(events, "tool.finished")
    result = ToolResult.from_json_object(finished[0].payload["result"])
    assert health_status_preserved(outcome.answer, [result])


def test_the_event_order_follows_the_run(tmp_path):
    gateway, outcome, events = run_case(tmp_path, "en-staging")
    assert [e.event_type.value for e in events] == [
        "run.accepted",
        "model.started",
        "model.finished",
        "route.selected",
        "skill.loaded",
        "model.started",
        "model.finished",
        "tool.proposed",
        "tool.started",
        "tool.finished",
        "model.started",
        "model.finished",
        "model.started",
        "model.finished",
        # The final answer is checked by the completion predicate before
        # any of it is published (RM16, section 7.3).
        "answer.delta",
        "answer.delta",
        "run.completed",
    ]
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


def test_model_finished_keeps_what_the_model_proposed(tmp_path):
    # RM16 / RM23: the trace shows the model's actual output for each call.
    gateway, outcome, events = run_case(tmp_path, "en-staging")
    finished = of_type(events, "model.finished")
    assert [e.payload["text"] for e in finished] == [
        None,
        None,
        outcome.answer,  # the finalize proposal
        outcome.answer,  # the final generation
    ]
    assert finished[1].payload["proposed_calls"] == [
        {"tool_name": "get_service_health", "arguments": {"service": "staging-api"}}
    ]
