"""Local generation loop: bounded execution and no tool on invalid output.

RM03: one routing generation, up to three further local generations, two
executed tool calls, one format-repair generation within those limits,
and four seconds of active local time including routing and tools; all
recorded in the run. RM04: exceeding a budget, unrepaired output,
unavailable local capability, or oversized context escalates if external
dispatch is permitted and otherwise returns needs_external, with a
reason code; never silent continuation. These tests configure no
external model (the handoff itself is covered in test_ac02_handoff.py,
#18), so every escalation here ends as needs_external. RM02 / RM09:
invalid routing output or an invalid tool proposal never executes a tool.
RM05: an identifier the request, context, or tool results never named is
not used. RM10: the same call in one run replays the recorded result.
Section 4.1: oversized router input bypasses the fast path without a
routing generation.
Issues #14 and #15.
"""

import asyncio
import dataclasses
import json
import threading

import pytest

from gateway_helpers import (
    LOCAL_MODEL_ID,
    STAGING_OK,
    assert_needs_external,
    assert_workflow_unavailable,
    build_gateway,
    executed_tools,
    replace_handler,
    route_turn,
    run,
    seed_with,
    text_turn,
    tool_turn,
)
from helpers import FakeClock, ScriptedAdapter, make_capabilities, of_type
from relaymind.adapters import Completed, GenerationFailed, Message, Role, TextDelta, ToolCall
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.local_loop import ConfiguredModel
from relaymind.orchestrator.run_state import RunState, SlotUnavailableError
from relaymind.redaction import Redactor
from relaymind.skills import SkillVersionMissingError
from relaymind.store import TraceClock
from relaymind.store.trace_event import TraceEventError, validate_trace_event
from relaymind.tools import CallerContext, ToolUnavailableError
from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION

HEALTH_RUN = [
    route_turn("check-service-health"),
    tool_turn("get_service_health", service="staging-api"),
    text_turn(STAGING_OK),
    text_turn(STAGING_OK),
]


# --- routing: invalid output, capability, bypass, clarify ----------------------


@pytest.mark.parametrize(
    "routing_turn",
    [
        text_turn("check-service-health"),
        tool_turn("get_service_health", service="staging-api"),
        tool_turn("no-such-skill", language="en"),
    ],
    ids=["text-only", "application-tool", "unknown-skill"],
)
def test_invalid_routing_output_never_executes_a_tool(tmp_path, routing_turn):
    gateway = build_gateway(tmp_path, [routing_turn])
    before = gateway.sandbox.state_bytes()
    outcome, events = run(gateway)

    assert_needs_external(gateway, outcome, events, "invalid_output")
    assert executed_tools(events) == []
    assert not of_type(events, "route.selected")
    assert not of_type(events, "skill.loaded")
    assert len(gateway.adapter.requests) == 1
    assert gateway.sandbox.state_bytes() == before
    assert gateway.store.get_tool_operation("any") is None


def test_a_generation_error_is_an_unavailable_local_capability(tmp_path):
    gateway = build_gateway(
        tmp_path, [[GenerationFailed(code="timeout", message="no response", received=False)]]
    )
    outcome, events = run(gateway)
    assert_needs_external(gateway, outcome, events, "capability_missing")


@pytest.mark.parametrize(
    "missing", [{"tool_calling": False}, {"tokenizer_available": False}], ids=str
)
def test_a_missing_local_capability_makes_no_generation(tmp_path, missing):
    gateway = build_gateway(tmp_path, [], capabilities=make_capabilities(**missing))
    outcome, events = run(gateway)
    assert_needs_external(gateway, outcome, events, "capability_missing")
    assert gateway.adapter.requests == []


def test_oversized_router_input_bypasses_the_fast_path(tmp_path):
    gateway = build_gateway(tmp_path, [], input_tokens=2049)
    outcome, events = run(gateway)

    assert_needs_external(gateway, outcome, events, "context_limit")
    assert gateway.adapter.requests == [], "an oversized input still ran the router"
    (selected,) = of_type(events, "route.selected")
    assert selected.payload["decision"]["route"] == "external"
    assert selected.payload["decision"]["reason_code"] == "context_limit"
    assert selected.payload["input_tokens"] == 2049
    assert selected.payload["mechanism"] == "bypass"
    assert selected.actor == "gateway"
    # The measured input was the full router request, never truncated.
    (counted_messages, counted_tools) = gateway.adapter.counted[0]
    assert counted_messages[-1].content == "Is staging-api healthy right now?"
    assert len(counted_tools) == len(gateway.skills.names()) + 1


def test_input_at_the_cap_takes_the_fast_path(tmp_path):
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
            text_turn(STAGING_OK),
        ],
        input_tokens=2048,
    )
    outcome, events = run(gateway)
    assert outcome.state is RunState.COMPLETED
    (selected,) = of_type(events, "route.selected")
    assert selected.payload["input_tokens"] == 2048


@pytest.mark.parametrize(
    ("prompt", "fragment"),
    [("Is the service healthy?", "Which service"), ("这个服务健康吗？", "哪个服务")],
)
def test_a_missing_service_waits_for_input_without_a_tool(tmp_path, prompt, fragment):
    gateway = build_gateway(tmp_path, [route_turn("check-service-health")])
    outcome, events = run(gateway, prompt)

    assert outcome.state is RunState.WAITING_INPUT
    assert fragment in outcome.clarification
    (asked,) = of_type(events, "clarification.required")
    assert asked.payload["reason_code"] == "missing_input"
    assert asked.payload["question"] == outcome.clarification
    # Spec 0.6 section 8: the clarify names the skill and the missing input.
    assert asked.payload["skill_id"] == "check-service-health"
    assert asked.payload["missing"] == "service"
    assert gateway.store.active_run_id() is None, "a waiting run keeps the slot"
    assert executed_tools(events) == []
    assert not of_type(events, "skill.loaded")
    assert gateway.store.get_run(outcome.run_id).state == "waiting_input"


@pytest.mark.parametrize(
    ("skill", "reason"),
    [("health-and-diagnostic-checklist", "parallel_workflow")],
)
def test_a_non_local_route_returns_needs_external(tmp_path, skill, reason):
    gateway = build_gateway(tmp_path, [route_turn(skill)])
    outcome, events = run(gateway, "Run the health checklist for staging-api.")
    assert_needs_external(gateway, outcome, events, reason)
    assert executed_tools(events) == []
    # Section 4.4: the workflow is not enabled, so the route is external.
    assert of_type(events, "route.selected")[0].payload["decision"]["route"] == "external"


# --- budgets ---------------------------------------------------------------------


def test_a_third_generation_over_budget_returns_needs_external(tmp_path):
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
        ],
        budgets=LocalBudgets(local_generations=2, repair_generations=1),
    )
    outcome, events = run(gateway)

    assert_needs_external(gateway, outcome, events, "local_budget")
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["budget"] == "local_generations"
    assert len(gateway.adapter.requests) == 3
    # The actual tool outcome stays recorded (section 5).
    assert executed_tools(events) == ["get_service_health"]


def test_a_tool_call_over_budget_is_never_executed(tmp_path):
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            tool_turn("get_service_health", call_id="call_2", service="payments-api"),
        ],
        budgets=LocalBudgets(tool_calls=1),
    )
    outcome, events = run(gateway, "Are staging-api and payments-api healthy?")

    assert_needs_external(gateway, outcome, events, "local_budget")
    assert of_type(events, "run.failed")[0].payload["budget"] == "tool_calls"
    assert executed_tools(events) == ["get_service_health"]
    assert len(of_type(events, "tool.proposed")) == 2


def test_routing_time_counts_against_the_active_budget(tmp_path):
    clock = FakeClock()

    def slow_router(request):
        if request.generation_id.endswith("routing"):
            clock.advance(4.5)

    gateway = build_gateway(
        tmp_path, [route_turn("check-service-health")], clock=clock, on_generate=slow_router
    )
    outcome, events = run(gateway)

    assert_needs_external(gateway, outcome, events, "local_budget")
    assert of_type(events, "run.failed")[0].payload["budget"] == "active_seconds"
    assert executed_tools(events) == []
    assert len(gateway.adapter.requests) == 1


def test_tool_time_counts_against_the_active_budget(tmp_path):
    clock = FakeClock()

    class SlowSandboxClock(TraceClock):
        def utc_now(self):
            clock.advance(3.8)  # the sandbox read takes 3.8 s of active time
            return clock.utc_now()

    slow_sandbox = SlowSandboxClock()
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
        ],
        clock=clock,
        sandbox_clock=slow_sandbox,
        on_generate=lambda request: clock.advance(0.2),
    )
    outcome, events = run(gateway)

    assert_needs_external(gateway, outcome, events, "local_budget")
    assert of_type(events, "run.failed")[0].payload["budget"] == "active_seconds"
    (finished,) = of_type(events, "tool.finished")
    assert finished.payload["result"]["status"] == "succeeded"
    assert len(gateway.adapter.requests) == 2


def test_budget_usage_counts_routing_repair_and_tools(tmp_path):
    clock = FakeClock()
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("drop_everything", service="staging-api"),
            tool_turn("get_service_health", call_id="call_2", service="staging-api"),
            text_turn(STAGING_OK),
            text_turn(STAGING_OK),
        ],
        budgets=LocalBudgets(local_generations=4),
        clock=clock,
        on_generate=lambda request: clock.advance(0.5),
    )
    outcome, events = run(gateway)

    assert outcome.state is RunState.COMPLETED
    usage = of_type(events, "run.completed")[0].payload["budget_usage"]
    assert usage == {
        "routing_generations": 1,
        "local_generations": 4,
        "repair_generations": 1,
        "tool_calls": 1,
        "external_generations": 0,
        "active_ms": 2500,
        "stages": 1,
    }


# --- tool proposals: validation, repair, provenance, replay ---------------------


@pytest.mark.parametrize(
    "bad_turn",
    [
        tool_turn("drop_everything", service="staging-api"),
        tool_turn("get_service_health", service="prod-db"),
        tool_turn("get_service_health", service="staging-api", region="eu"),
        tool_turn("delete_demo_database", database_id="demo-orders", expected_revision=1),
        tool_turn("request_clarification", language="en", question="Which?"),
    ],
    ids=["unknown-tool", "bad-enum", "undeclared-field", "not-allowed", "control-action"],
)
def test_an_invalid_proposal_is_repaired_once_and_never_executed(tmp_path, bad_turn):
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            bad_turn,
            tool_turn("get_service_health", call_id="call_2", service="staging-api"),
            text_turn(STAGING_OK),
            text_turn(STAGING_OK),
        ],
        budgets=LocalBudgets(local_generations=4),
    )
    before = gateway.sandbox.state_bytes()
    outcome, events = run(gateway)

    assert outcome.state is RunState.COMPLETED
    assert executed_tools(events) == ["get_service_health"]
    assert len(of_type(events, "tool.proposed")) == 2
    rejected = of_type(events, "tool.proposed")[0]
    assert rejected.payload["admissible"] is False
    assert rejected.payload["rejection"]["code"]
    purposes = [e.payload["purpose"] for e in of_type(events, "model.started")]
    assert purposes == ["routing", "skill", "repair", "skill", "final"]
    assert gateway.sandbox.state_bytes() == before


def test_a_second_invalid_proposal_returns_needs_external(tmp_path):
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("drop_everything", service="staging-api"),
            tool_turn("drop_everything", call_id="call_2", service="staging-api"),
        ],
    )
    outcome, events = run(gateway)
    assert_needs_external(gateway, outcome, events, "invalid_output")
    assert executed_tools(events) == []


def test_an_identifier_the_request_never_named_is_not_used(tmp_path):
    # RM05: the user asked about staging-api; payments-api is a guess.
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="payments-api"),
            tool_turn("get_service_health", call_id="call_2", service="payments-api"),
        ],
    )
    outcome, events = run(gateway)

    assert_needs_external(gateway, outcome, events, "invalid_output")
    assert executed_tools(events) == []
    rejection = of_type(events, "tool.proposed")[0].payload["rejection"]
    assert rejection["code"] == "unsupported_identifier"


def test_the_same_call_twice_replays_the_recorded_result(tmp_path):
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            tool_turn("get_service_health", call_id="call_2", service="staging-api"),
            text_turn(STAGING_OK),
        ],
    )
    handler_runs = []
    original = gateway.tools.get("get_service_health").declaration.handler
    replace_handler(
        gateway, "get_service_health", lambda args: handler_runs.append(args) or original(args)
    )
    outcome, events = run(gateway)

    finished = of_type(events, "tool.finished")
    assert [e.payload["replayed"] for e in finished] == [False, True]
    # A replay is not an execution (RM03 counts executed tools): the
    # handler ran once, tool.started marks only that run, and only it
    # was charged.
    assert len(handler_runs) == 1
    assert executed_tools(events) == ["get_service_health"]
    assert of_type(events, "run.failed")[0].payload["budget_usage"]["tool_calls"] == 1
    assert finished[0].payload["operation_id"] == finished[1].payload["operation_id"]
    assert finished[0].payload["result"] == finished[1].payload["result"]


# --- finalize: predicate gate and final generation --------------------------------


def test_a_finalize_proposal_that_misstates_the_status_cannot_finalize(tmp_path):
    # RM16 / section 7.3: payments-api is unavailable; "healthy" is not evidence.
    wrong = "payments-api is healthy (checked at 2026-10-20T02:59:00Z)."
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="payments-api"),
            text_turn(wrong),
        ],
        budgets=LocalBudgets(local_generations=3, repair_generations=0),
    )
    outcome, events = run(gateway, "Check the health of payments-api.")

    assert_needs_external(gateway, outcome, events, "invalid_output")
    assert len(gateway.adapter.requests) == 3, "a final generation ran on an unmet predicate"


def test_a_final_answer_that_fails_the_predicate_is_never_published(tmp_path):
    right = "payments-api is unavailable (checked at 2026-10-20T02:59:00Z)."
    wrong = "payments-api is healthy (checked at 2026-10-20T02:59:00Z)."
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="payments-api"),
            text_turn(right),
            text_turn(wrong),
        ],
    )
    outcome, events = run(gateway, "Check the health of payments-api.")
    assert_needs_external(gateway, outcome, events, "invalid_output")


def test_a_failed_predicate_is_repaired_within_the_budget(tmp_path):
    right = "payments-api is unavailable (checked at 2026-10-20T02:59:00Z)."
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="payments-api"),
            text_turn("payments-api looks fine."),
            text_turn(right),
            text_turn(right),
        ],
        budgets=LocalBudgets(local_generations=4),
    )
    outcome, events = run(gateway, "Check the health of payments-api.")
    assert outcome.state is RunState.COMPLETED
    assert outcome.answer == right
    purposes = [e.payload["purpose"] for e in of_type(events, "model.started")]
    assert purposes == ["routing", "skill", "skill", "repair", "final"]


def test_a_tool_call_in_the_final_generation_is_invalid_output(tmp_path):
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
            tool_turn("get_service_health", call_id="call_2", service="staging-api"),
        ],
    )
    outcome, events = run(gateway)
    assert_needs_external(gateway, outcome, events, "invalid_output")
    assert executed_tools(events) == ["get_service_health"]


def test_the_skill_generation_sees_the_skill_and_its_permitted_tools(tmp_path):
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
            text_turn(STAGING_OK),
        ],
    )
    run(gateway)
    skill_request = gateway.adapter.requests[1]
    assert [t.name for t in skill_request.tool_schemas] == ["get_service_health"]
    skill = gateway.skills.get("check-service-health")
    # Layout chosen in development runs (see local_loop): the skill description
    # and the tool to call come first, the full skill body follows the
    # request as a system message (RM06: the body is in the model context).
    first, request_message, body_message = skill_request.messages[:3]
    assert first.role is Role.SYSTEM
    assert skill.description in first.content and "get_service_health" in first.content
    assert request_message.content == "Is staging-api healthy right now?"
    assert body_message.role is Role.SYSTEM and skill.body in body_message.content
    # Local generations are deterministic (temperature 0), like routing.
    assert all(r.options.temperature == 0 for r in gateway.adapter.requests)
    # The tool result reaches the next generation as data in a tool message.
    after_tool = gateway.adapter.requests[2]
    tool_messages = [m for m in after_tool.messages if m.role is Role.TOOL]
    assert len(tool_messages) == 1 and "2026-10-20T02:58:00Z" in tool_messages[0].content


def test_run_accepts_a_message_list(tmp_path):
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="payments-api"),
            text_turn("payments-api is unavailable (checked at 2026-10-20T02:59:00Z)."),
            [
                TextDelta(text="payments-api is unavailable (checked at 2026-10-20T02:59:00Z)."),
                Completed(finish_reason="stop"),
            ],
        ],
    )
    messages = (
        Message(role=Role.USER, content="I am looking at payments-api."),
        Message(role=Role.ASSISTANT, content="OK."),
        Message(role=Role.USER, content="Is it healthy?"),
    )
    outcome = asyncio.run(gateway.orchestrator.run(messages))
    assert outcome.state is RunState.COMPLETED
    events = gateway.store.events(outcome.run_id)
    assert of_type(events, "run.completed")[0].payload["final_answer_model"] == LOCAL_MODEL_ID


@pytest.mark.parametrize(
    ("prompt", "language_line"),
    [
        ("Is staging-api healthy right now?", "Answer in English."),
        ("staging-api 现在健康吗？", "请用中文回答。"),
    ],
)
def test_the_final_generation_is_told_the_request_language(tmp_path, prompt, language_line):
    # Section 9 skill rules: answer in the language of the request. The
    # gateway's RouteDecision language comes from the request text.
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
            text_turn(STAGING_OK),
        ],
    )
    run(gateway, prompt)
    final = gateway.adapter.requests[-1]
    assert final.messages[-1].role is Role.SYSTEM
    assert language_line in final.messages[-1].content


def test_a_skill_without_permitted_tools_returns_needs_external(tmp_path):
    # RM08: availability is the intersection with caller permissions; a
    # skill with nothing left cannot run locally (RM04 capability_missing).
    gateway = build_gateway(tmp_path, [route_turn("check-service-health")])
    gateway.orchestrator.caller = CallerContext(permissions=frozenset())
    outcome, events = run(gateway)
    assert_needs_external(gateway, outcome, events, "capability_missing")
    assert len(gateway.adapter.requests) == 1


@pytest.mark.parametrize(
    "bad_turn",
    [
        [
            ToolCall(
                call_id="a", tool_name="get_service_health", arguments={"service": "staging-api"}
            ),
            ToolCall(
                call_id="b", tool_name="get_service_health", arguments={"service": "staging-api"}
            ),
            Completed(finish_reason="tool_calls"),
        ],
        text_turn(),
    ],
    ids=["two-calls-at-once", "empty-output"],
)
def test_other_invalid_output_is_repaired_and_never_executed(tmp_path, bad_turn):
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            bad_turn,
            tool_turn("get_service_health", call_id="call_2", service="staging-api"),
            text_turn(STAGING_OK),
            text_turn(STAGING_OK),
        ],
        budgets=LocalBudgets(local_generations=4),
    )
    outcome, events = run(gateway)
    assert outcome.state is RunState.COMPLETED
    assert executed_tools(events) == ["get_service_health"]
    purposes = [e.payload["purpose"] for e in of_type(events, "model.started")]
    assert purposes == ["routing", "skill", "repair", "skill", "final"]


def test_a_mutating_tool_never_runs_without_confirmation(tmp_path):
    # RM11 / AGENTS.md invariant 5: a mutating demo action needs a fresh,
    # single-use confirmation (#19, tests/test_ac05_confirmation.py). Even
    # a caller with the mutate permission gets no execution on the
    # proposal: the run waits in waiting_confirmation, the handler has
    # not run, and the sandbox database is still there.
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("delete-demo-database"),
            tool_turn("list_demo_databases", call_id="call_list"),
            tool_turn(
                "delete_demo_database",
                call_id="call_delete",
                database_id="demo-orders-db",
                expected_revision=3,
            ),
            text_turn("Deleted demo-orders-db."),
            text_turn("Deleted demo-orders-db."),
        ],
        caller=CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION})),
    )
    outcome, events = run(gateway, "Delete the demo-orders-db sandbox database.")

    assert outcome.state is RunState.WAITING_CONFIRMATION
    assert "delete_demo_database" not in executed_tools(events)
    ids = [db["database_id"] for db in gateway.sandbox.databases_output()["databases"]]
    assert "demo-orders-db" in ids
    proposed = [
        e
        for e in of_type(events, "tool.proposed")
        if e.payload["tool_name"] == "delete_demo_database"
    ]
    assert proposed and proposed[-1].payload["admissible"] is True
    assert len(of_type(events, "confirmation.required")) == 1


# --- run.accepted (section 8) and execution modes (sections 4.4, 7.4) -----------


def test_run_accepted_carries_the_section_8_payload(tmp_path):
    """Section 8 run.accepted: modes, model alias, budgets, roots, offered skills."""
    gateway = build_gateway(tmp_path, HEALTH_RUN)
    outcome, events = run(gateway, execution_mode="local_only", model_alias="relaymind")

    (accepted,) = of_type(events, "run.accepted")
    validate_trace_event(accepted.to_json_object())
    snapshot = gateway.skills.offered_snapshot(gateway.skills.names())
    assert accepted.payload == {
        "requested_execution_mode": "local_only",
        "effective_execution_mode": "local_only",
        "model_alias": "relaymind",
        "budgets": LocalBudgets().to_json_object(),
        "skill_roots": snapshot["skill_roots"],
        "skills_offered": snapshot["skills_offered"],
    }
    assert outcome.state is RunState.COMPLETED


def test_an_absent_mode_is_recorded_as_null_and_runs_as_auto(tmp_path):
    """Section 8: requested_execution_mode is null when absent; auto applies."""
    gateway = build_gateway(tmp_path, HEALTH_RUN)
    _, events = run(gateway)
    accepted = of_type(events, "run.accepted")[0].payload
    assert accepted["requested_execution_mode"] is None
    assert accepted["effective_execution_mode"] == "auto"
    assert accepted["model_alias"] is None


def test_external_only_records_a_gateway_decision_without_a_generation(tmp_path):
    """Section 7.4 (spec 0.6): external_only runs no routing generation.

    The gateway records route external, skill_id null, reason
    execution_mode; external dispatch is not wired, so needs_external.
    """
    gateway = build_gateway(tmp_path, [])
    outcome, events = run(gateway, execution_mode="external_only")

    assert_needs_external(gateway, outcome, events, "execution_mode")
    assert gateway.adapter.requests == [] and gateway.adapter.counted == []
    (selected,) = of_type(events, "route.selected")
    assert selected.actor == "gateway"
    assert selected.payload["decision"] == {
        "schema_version": 1,
        "route": "external",
        "skill_id": None,
        "language": "en",
        "reason_code": "execution_mode",
        "clarification": None,
    }
    accepted = of_type(events, "run.accepted")[0].payload
    assert [s["name"] for s in accepted["skills_offered"]] == list(gateway.skills.names())


def test_an_admitted_parallel_workflow_routes_parallel_in_auto(tmp_path):
    """Section 4.4 (spec 0.6): enabled workflow, declaring skill, admitting mode.

    The parallel workflow itself (#30) is not wired into this loop, so the
    run ends as needs_external with reason parallel_workflow.
    """
    gateway = build_gateway(
        tmp_path,
        [route_turn("health-and-diagnostic-checklist")],
        enabled_workflows={"health-and-diagnostic-checklist"},
        parallel_in_auto=True,
    )
    outcome, events = run(
        gateway, "Run the health checklist for staging-api.", execution_mode="auto"
    )
    assert of_type(events, "route.selected")[0].payload["decision"]["route"] == "parallel"
    assert_needs_external(gateway, outcome, events, "parallel_workflow")
    assert executed_tools(events) == []


def test_an_admitted_parallel_route_in_parallel_demo_fails_as_workflow_unavailable(tmp_path):
    """RM15, RM21, section 4.4 (spec 0.17): parallel_demo fails explicitly.

    The workflow (#30) is not implemented, so a parallel route in
    parallel_demo ends as failed with workflow_unavailable, never as a
    quiet needs_external or a single-actor handoff.
    """
    gateway = build_gateway(
        tmp_path,
        [route_turn("health-and-diagnostic-checklist")],
        enabled_workflows={"health-and-diagnostic-checklist"},
        external_turns=[],
    )
    outcome, events = run(
        gateway, "Run the health checklist for staging-api.", execution_mode="parallel_demo"
    )
    assert of_type(events, "route.selected")[0].payload["decision"]["route"] == "parallel"
    assert_workflow_unavailable(gateway, outcome, events)
    assert executed_tools(events) == []
    assert not of_type(events, "handoff.started")


@pytest.mark.parametrize(
    ("enabled", "external"),
    [(False, True), (True, False)],
    ids=["no-enabled-workflow", "no-external-endpoint"],
)
def test_parallel_demo_fails_before_routing_when_its_workflow_cannot_run(
    tmp_path, enabled, external
):
    """RM21, section 4.4 (spec 0.17): configuration without the workflow's needs.

    No model is called: a run that cannot succeed does not spend a
    routing generation.
    """
    gateway = build_gateway(
        tmp_path,
        [],
        enabled_workflows={"health-and-diagnostic-checklist"} if enabled else (),
        external_turns=[] if external else None,
    )
    outcome, events = run(
        gateway, "Run the health checklist for staging-api.", execution_mode="parallel_demo"
    )
    assert_workflow_unavailable(gateway, outcome, events)
    assert not of_type(events, "route.selected")
    assert gateway.adapter.requests == []


@pytest.mark.parametrize(
    ("mode", "enabled", "in_auto"),
    [
        ("auto", True, False),
        ("local_only", True, True),
    ],
    ids=["auto-without-policy", "local-only"],
)
def test_a_parallel_skill_the_run_does_not_admit_routes_external(tmp_path, mode, enabled, in_auto):
    """Section 4.4 (spec 0.6): otherwise external with reason parallel_workflow."""
    gateway = build_gateway(
        tmp_path,
        [route_turn("health-and-diagnostic-checklist")],
        enabled_workflows={"health-and-diagnostic-checklist"} if enabled else (),
        parallel_in_auto=in_auto,
    )
    outcome, events = run(gateway, "Run the health checklist for staging-api.", execution_mode=mode)
    decision = of_type(events, "route.selected")[0].payload["decision"]
    assert (decision["route"], decision["reason_code"]) == ("external", "parallel_workflow")
    assert_needs_external(gateway, outcome, events, "parallel_workflow")


def test_parallel_demo_does_not_hand_a_parallel_skill_off_when_it_is_not_enabled(tmp_path):
    """RM21, section 4.4 (spec 0.17): a skill declaring a workflow that is not enabled.

    Another workflow is enabled, so the precondition holds and the skill
    routes external with parallel_workflow; in parallel_demo that is the
    unavailable workflow, not a handoff.
    """
    gateway = build_gateway(
        tmp_path,
        [route_turn("health-and-diagnostic-checklist")],
        enabled_workflows={"some-other-workflow"},
        external_turns=[],
    )
    outcome, events = run(
        gateway, "Run the health checklist for staging-api.", execution_mode="parallel_demo"
    )
    assert_workflow_unavailable(gateway, outcome, events)
    assert not of_type(events, "handoff.started")


# --- RM05 provenance and the run language (section 5, spec 0.6) ---------------------


def test_a_gateway_question_listing_ids_is_not_provenance(tmp_path):
    """RM05 / section 5: only user turns and tool results count.

    The gateway's own clarification lists the valid services; a user who
    answers without naming one is asked again, and nothing runs.
    """
    gateway = build_gateway(tmp_path, [route_turn("check-service-health")])
    messages = (
        Message(role=Role.USER, content="Is my service healthy?"),
        Message(
            role=Role.ASSISTANT,
            content="Which service do you mean? Options: staging-api, payments-api.",
        ),
        Message(role=Role.USER, content="I don't know, you pick."),
    )
    outcome = asyncio.run(gateway.orchestrator.run(messages))
    events = gateway.store.events(outcome.run_id)
    assert outcome.state is RunState.WAITING_INPUT
    assert executed_tools(events) == []


def test_an_id_only_an_assistant_turn_named_is_not_used(tmp_path):
    """RM05 / section 5: an assistant turn is not provenance for a tool call."""
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="payments-api"),
            tool_turn("get_service_health", call_id="call_2", service="payments-api"),
        ],
    )
    messages = (
        Message(role=Role.USER, content="Is staging-api healthy?"),
        Message(role=Role.ASSISTANT, content="I can also check payments-api."),
        Message(role=Role.USER, content="Please do the check."),
    )
    outcome = asyncio.run(gateway.orchestrator.run(messages))
    events = gateway.store.events(outcome.run_id)
    assert_needs_external(gateway, outcome, events, "invalid_output")
    rejection = of_type(events, "tool.proposed")[0].payload["rejection"]
    assert rejection["code"] == "unsupported_identifier"
    assert executed_tools(events) == []


def test_a_pattern_constrained_id_needs_provenance(tmp_path):
    """RM05: every identifier-like argument is checked, not only enums.

    database_id is pattern-constrained (section 9); demo-billing-db was
    never listed or named by the user, so it is rejected before the
    confirmation check and nothing is mutated.
    """
    delete = {"database_id": "demo-billing-db", "expected_revision": 3}
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("delete-demo-database"),
            tool_turn("delete_demo_database", **delete),
            tool_turn("delete_demo_database", call_id="call_2", **delete),
        ],
        caller=CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION})),
    )
    before = gateway.sandbox.state_bytes()
    outcome, events = run(gateway, "Delete the old billing sandbox database.")
    rejections = [e.payload["rejection"]["code"] for e in of_type(events, "tool.proposed")]
    assert rejections == ["unsupported_identifier", "unsupported_identifier"]
    assert_needs_external(gateway, outcome, events, "invalid_output")
    assert gateway.sandbox.state_bytes() == before


def test_an_id_only_follow_up_keeps_the_conversation_language(tmp_path):
    """Section 5 (spec 0.6): an identifier-only reply does not change the language."""
    gateway = build_gateway(tmp_path, HEALTH_RUN)
    messages = (
        Message(role=Role.USER, content="帮我检查服务健康"),
        Message(role=Role.ASSISTANT, content="请问是哪个服务？可选：staging-api、payments-api。"),
        Message(role=Role.USER, content="staging-api"),
    )
    outcome = asyncio.run(gateway.orchestrator.run(messages))
    assert outcome.decision.language.value == "zh"
    assert "请用中文回答。" in gateway.adapter.requests[-1].messages[-1].content


# --- RM07: the run.accepted snapshot pins the skill ----------------------------------


def only_run_id(gateway) -> str:
    (run_id,) = [row[0] for row in gateway.store._conn.execute("SELECT run_id FROM runs")]
    return run_id


def test_the_skill_is_loaded_at_the_version_run_accepted_recorded(tmp_path):
    """RM07: a skill changed after run.accepted is an explicit failure, never swapped.

    The registry snapshot changes during routing; the run must not load
    the new content under the old run.
    """
    gateway = build_gateway(tmp_path, HEALTH_RUN)

    def change_skill(request):
        if request.generation_id.endswith("routing"):
            skill = gateway.skills.get("check-service-health")
            gateway.skills._skills[skill.name] = dataclasses.replace(
                skill, content_hash="sha256:" + "0" * 64
            )

    gateway.adapter.on_generate = change_skill
    with pytest.raises(SkillVersionMissingError):
        run(gateway)
    run_id = only_run_id(gateway)
    events = gateway.store.events(run_id)
    assert not of_type(events, "skill.loaded")
    assert executed_tools(events) == []
    assert gateway.store.get_run(run_id).error_code == "internal_error"


# --- RM01 / RM16: model calls are labelled from the adapter ---------------------------


def test_an_adapter_without_a_locality_is_refused():
    """RM16: the trace cannot prove where an undeclared adapter ran."""
    adapter = ScriptedAdapter([])
    adapter.locality = None
    with pytest.raises(ValueError, match="locality"):
        ConfiguredModel(adapter=adapter, model_id="m")


def test_external_calls_are_counted_from_the_adapter_locality(tmp_path):
    """RM16: every actual model call is labelled; external calls are counted."""
    gateway = build_gateway(tmp_path, HEALTH_RUN)
    gateway.adapter.locality = "external"
    outcome, events = run(gateway)
    assert {e.actor for e in of_type(events, "model.started")} == {"external"}
    completed = of_type(events, "run.completed")[0].payload
    assert completed["external_calls"] == len(gateway.adapter.requests) == 4


# --- unexpected errors (section 5, RM17) ----------------------------------------------


def test_an_unexpected_exception_fails_the_run_and_frees_the_slot(tmp_path):
    """Section 5 / RM17: an unexpected error is a recorded failure, not a stuck run.

    The run ends failed with internal_error and its run.failed event,
    the slot is released, and the next run can start.
    """

    def crash(request):
        if request.generation_id.endswith("skill"):
            raise RuntimeError("adapter crashed")

    gateway = build_gateway(tmp_path, HEALTH_RUN, on_generate=crash)
    with pytest.raises(RuntimeError, match="adapter crashed"):
        run(gateway)
    run_id = only_run_id(gateway)
    record = gateway.store.get_run(run_id)
    assert (record.state, record.error_code) == ("failed", "internal_error")
    (failed,) = of_type(gateway.store.events(run_id), "run.failed")
    assert failed.payload["error_code"] == "internal_error"
    assert failed.payload["detail"] == "RuntimeError"
    assert gateway.store.active_run_id() is None

    gateway.adapter.on_generate = None
    gateway.adapter._turns = list(HEALTH_RUN)
    outcome, _ = run(gateway)
    assert outcome.state is RunState.COMPLETED


def test_an_invalid_run_accepted_leaves_no_stuck_run(tmp_path, monkeypatch):
    """RM22, section 5: run.accepted is written with the run, so a payload
    that fails validation creates no run, never one stuck in accepted
    without its first event, and the next run can start."""
    gateway = build_gateway(tmp_path, HEALTH_RUN)
    monkeypatch.setattr(LocalBudgets, "to_json_object", lambda self: "not an object")
    with pytest.raises(TraceEventError, match="budgets"):
        run(gateway)
    assert gateway.store._conn.execute("SELECT run_id FROM runs").fetchall() == []
    assert gateway.store.active_run_id() is None


def test_a_cancelled_run_ends_cancelled_and_frees_the_slot(tmp_path):
    """RM17 / section 5: a cancelled task leaves a terminal cancelled run."""

    def cancel(request):
        if request.generation_id.endswith("skill"):
            raise asyncio.CancelledError

    gateway = build_gateway(tmp_path, HEALTH_RUN, on_generate=cancel)
    with pytest.raises(asyncio.CancelledError):
        run(gateway)
    run_id = only_run_id(gateway)
    assert gateway.store.get_run(run_id).state == "cancelled"
    assert of_type(gateway.store.events(run_id), "run.cancelled")
    assert gateway.store.active_run_id() is None


def test_a_busy_slot_rejects_the_run_without_creating_one(tmp_path):
    """Section 11: one active run; additional work is rejected (429)."""
    gateway = build_gateway(tmp_path, HEALTH_RUN)
    machine = gateway.orchestrator.machine
    holder = machine.create()
    machine.transition(holder.run_id, holder.version, RunState.ROUTING)
    with pytest.raises(SlotUnavailableError):
        run(gateway)
    assert only_run_id(gateway) == holder.run_id


# --- tools: failures, retries, time bound, redaction ------------------------------------


def test_a_failed_tool_ends_the_run_with_tool_failure(tmp_path):
    """RM04 / RM16: a failed tool outcome is tool_failure, never reported as health.

    The sandbox cannot reach the health service; section 11 allows one
    read-only retry for that transient failure, then the run stops.
    """
    gateway = build_gateway(
        tmp_path,
        HEALTH_RUN,
        seed=seed_with(tmp_path, unreachable_tools=["get_service_health"]),
    )
    outcome, events = run(gateway)
    assert_needs_external(gateway, outcome, events, "tool_failure")
    started = of_type(events, "tool.started")
    assert [e.payload["attempt"] for e in started] == [1, 2]
    finished = of_type(events, "tool.finished")
    assert [e.payload["status"] for e in finished] == ["failed", "failed"]
    assert len({e.payload["operation_id"] for e in finished}) == 2
    for event in finished:
        record = gateway.store.get_tool_operation(event.payload["operation_id"])
        assert record["status"] == "failed"
    # The failure never reached a finalize or final generation.
    purposes = [e.payload["purpose"] for e in of_type(events, "model.started")]
    assert purposes == ["routing", "skill"]


def test_a_transient_read_failure_gets_one_retry(tmp_path):
    """Section 11: one read-only retry for a transient failure, within the budgets."""
    gateway = build_gateway(tmp_path, HEALTH_RUN)
    original = gateway.tools.get("get_service_health").declaration.handler
    calls = []

    def flaky(args):
        calls.append(args)
        if len(calls) == 1:
            raise ToolUnavailableError("sandbox briefly unreachable")
        return original(args)

    replace_handler(gateway, "get_service_health", flaky)
    outcome, events = run(gateway)
    assert outcome.state is RunState.COMPLETED
    statuses = [e.payload["status"] for e in of_type(events, "tool.finished")]
    assert statuses == ["failed", "succeeded"]
    usage = of_type(events, "run.completed")[0].payload["budget_usage"]
    assert usage["tool_calls"] == 2


def test_a_retry_is_charged_and_refused_over_the_tool_budget(tmp_path):
    """Section 11: the retry stays within all budgets (RM03)."""
    gateway = build_gateway(
        tmp_path,
        HEALTH_RUN,
        budgets=LocalBudgets(tool_calls=1),
        seed=seed_with(tmp_path, unreachable_tools=["get_service_health"]),
    )
    outcome, events = run(gateway)
    assert_needs_external(gateway, outcome, events, "local_budget")
    assert of_type(events, "run.failed")[0].payload["budget"] == "tool_calls"
    assert len(of_type(events, "tool.started")) == 1


def test_a_non_transient_read_failure_is_not_retried(tmp_path):
    """Section 11: only a transient failure is retried."""
    gateway = build_gateway(tmp_path, HEALTH_RUN)

    def broken(args):
        raise ValueError("bug in the handler")

    replace_handler(gateway, "get_service_health", broken)
    outcome, events = run(gateway)
    assert_needs_external(gateway, outcome, events, "tool_failure")
    assert len(of_type(events, "tool.started")) == 1


def test_a_tool_is_bounded_by_the_remaining_active_budget(tmp_path):
    """Section 11 / RM03 / AC07: a tool uses only the remaining active budget.

    Routing and the tool proposal spend 3.75 s of the 4 s budget. Control
    that time explicitly so slow CI setup cannot exhaust it before the
    tool starts; the executor still enforces its real asyncio timeout.
    """
    clock = FakeClock()
    gateway = build_gateway(
        tmp_path,
        HEALTH_RUN,
        clock=clock,
        budgets=LocalBudgets(active_seconds=4.0),
        on_generate=lambda request: clock.advance(1.875),
    )
    original = gateway.tools.get("get_service_health").declaration.handler
    release = threading.Event()

    def blocked(args):
        release.wait(2)
        return original(args)

    replace_handler(gateway, "get_service_health", blocked)
    try:
        outcome, events = run(gateway)
    finally:
        release.set()

    assert_needs_external(gateway, outcome, events, "local_budget")
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["budget"] == "active_seconds"
    assert failed.payload["budget_usage"]["tool_calls"] == 1
    assert executed_tools(events) == ["get_service_health"]
    (finished,) = of_type(events, "tool.finished")
    assert finished.payload["result"]["error"]["code"] == "tool_timeout"
    assert finished.payload["result"]["duration_ms"] == 250
    record = gateway.store.get_tool_operation(finished.payload["operation_id"])
    assert record["status"] == "failed"


@pytest.mark.parametrize(
    ("active_seconds", "tool_timeout", "reason", "attempts"),
    [
        (0.02, 2.0, "local_budget", 1),
        (0.02, 0.02, "local_budget", 1),
        (4.0, 0.02, "tool_failure", 2),
    ],
    ids=["budget-shorter", "equal-limits", "tool-shorter"],
)
def test_tool_timeout_uses_its_bound_before_the_clock_deadline(
    tmp_path, active_seconds, tool_timeout, reason, attempts
):
    """RM03 / AC07: an early timer cannot turn budget exhaustion into a retry.

    A frozen trace clock models a platform timer firing before the next
    monotonic tick. The executor still reaches its real asyncio timeout.
    """
    gateway = build_gateway(
        tmp_path, HEALTH_RUN, budgets=LocalBudgets(active_seconds=active_seconds)
    )
    release = threading.Event()
    tool = gateway.tools.get("get_service_health")

    def blocked(args):
        release.wait(2)
        return tool.declaration.handler(args)

    replace_handler(gateway, tool.name, blocked, timeout_seconds=tool_timeout)
    try:
        outcome, events = run(gateway)
    finally:
        release.set()

    assert_needs_external(gateway, outcome, events, reason)
    (failed,) = of_type(events, "run.failed")
    if reason == "local_budget":
        assert failed.payload["budget"] == "active_seconds"
    assert failed.payload["budget_usage"]["tool_calls"] == attempts
    finished = of_type(events, "tool.finished")
    assert len(finished) == attempts
    for event in finished:
        assert event.payload["result"]["error"]["code"] == "tool_timeout"
        record = gateway.store.get_tool_operation(event.payload["operation_id"])
        assert record["status"] == "failed"


def test_configured_secret_fields_never_reach_the_store(tmp_path):
    """RM12 / section 8: tool results, arguments, and model text are redacted first.

    checked_at and service are configured as secret fields here, so
    their values must not appear in any persisted event or tool
    operation, even where the model repeats them.
    """
    gateway = build_gateway(
        tmp_path, HEALTH_RUN, redactor=Redactor(secret_fields={"checked_at", "service"})
    )
    outcome, events = run(gateway)
    persisted = json.dumps([e.to_json_object() for e in events], ensure_ascii=False)
    assert "2026-10-20T02:58:00Z" not in persisted
    proposed = [e.payload["arguments"] for e in of_type(events, "tool.proposed")]
    assert proposed == [{"service": "[redacted]"}]
    (finished,) = of_type(events, "tool.finished")
    record = gateway.store.get_tool_operation(finished.payload["operation_id"])
    assert record["result"]["data"]["checked_at"] == "[redacted]"
    assert record["arguments"] == {"service": "[redacted]"}
    # Redacted evidence cannot satisfy the predicate: no answer is published.
    assert not of_type(events, "answer.delta")
    assert outcome.state is RunState.FAILED


# --- one validation, one batch of answer deltas ------------------------------------------


def test_a_proposal_is_validated_once(tmp_path):
    """RM09: the full name and arguments are validated once, before execution."""
    gateway = build_gateway(tmp_path, HEALTH_RUN)
    calls = []
    validate = gateway.tools.validate

    def counting(*args, **kwargs):
        calls.append(args[0])
        return validate(*args, **kwargs)

    gateway.tools.validate = counting
    outcome, _ = run(gateway)
    assert outcome.state is RunState.COMPLETED
    assert calls == ["get_service_health"]


def test_answer_deltas_are_appended_in_one_batch(tmp_path):
    """Section 7.3: the checked answer is published at once, in order."""
    final = text_turn("staging-api is healthy ", "(checked at 2026-10-20T02:58:00Z).")
    gateway = build_gateway(tmp_path, [*HEALTH_RUN[:3], final])
    batches = []
    append_events = gateway.store.append_events

    def recording(run_id, drafts):
        batches.append([str(d[0]) for d in drafts])
        return append_events(run_id, drafts)

    gateway.store.append_events = recording
    outcome, events = run(gateway)
    assert outcome.state is RunState.COMPLETED
    assert ["answer.delta", "answer.delta"] in batches
    assert "".join(e.payload["text"] for e in of_type(events, "answer.delta")) == outcome.answer


def test_every_call_of_a_multi_call_turn_is_recorded_as_a_proposal(tmp_path):
    """RM16, RM23: the trace keeps every proposal that led to a rejection,
    so a turn with two tool calls records both as tool.proposed, each
    rejected with multiple_tool_calls."""
    two_calls = [
        ToolCall(call_id="a", tool_name="get_service_health", arguments={"service": "staging-api"}),
        ToolCall(
            call_id="b", tool_name="get_service_health", arguments={"service": "payments-api"}
        ),
        Completed(finish_reason="tool_calls"),
    ]
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            two_calls,
            tool_turn("get_service_health", call_id="call_2", service="staging-api"),
            text_turn(STAGING_OK),
            text_turn(STAGING_OK),
        ],
        budgets=LocalBudgets(local_generations=4),
    )
    outcome, events = run(gateway)
    assert outcome.state is RunState.COMPLETED
    proposed = of_type(events, "tool.proposed")
    rejected = [e.payload for e in proposed if e.payload["admissible"] is False]
    assert [p["call_id"] for p in rejected] == ["a", "b"]
    assert [p["arguments"]["service"] for p in rejected] == ["staging-api", "payments-api"]
    assert {p["rejection"]["code"] for p in rejected} == {"multiple_tool_calls"}
    assert len({p["proposal_id"] for p in rejected}) == 2
    assert executed_tools(events) == ["get_service_health"]
