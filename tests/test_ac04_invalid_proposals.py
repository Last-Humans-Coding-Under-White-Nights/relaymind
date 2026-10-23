"""AC04: invalid proposals never execute; bounded repair or explicit stop (#17).

AC04: when the model proposes an unknown tool or invalid arguments,
nothing executes, and the run gets a bounded repair or an explicit
escalation with a recorded reason. RM09: the full tool name and the
arguments are validated before execution and undeclared fields are
rejected. RM08: only tools in the intersection of the skill allowlist,
operator policy, and caller permissions have a handler. RM02: unknown
skills or invalid routing fields never trigger tool execution. RM03:
one format-repair generation, counted within the local generation
limit. RM04: unrepaired output escalates if external dispatch is
permitted, otherwise needs_external; the trace uses a reason code from
the section 8 list. External dispatch is not configured before #18, so
every escalation here is needs_external. Model output comes from
scripted doubles (spec section 13).
"""

import asyncio

import pytest

from gateway_helpers import (
    STAGING_OK,
    build_gateway,
    executed_tools,
    purposes,
    route_turn,
    run,
    text_turn,
    tool_turn,
)
from helpers import of_type
from relaymind.adapters import Completed, GenerationFailed, ToolCall
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.local_loop import Escalation
from relaymind.orchestrator.route_decision import REASON_CODES
from relaymind.orchestrator.run_state import RunState

# Section 8 reason codes (spec 0.6), copied from the spec so a schema change is noticed.
SECTION_8_REASON_CODES = {
    "simple_skill",
    "complex_skill",
    "missing_input",
    "local_budget",
    "invalid_output",
    "tool_failure",
    "context_limit",
    "capability_missing",
    "parallel_workflow",
    "execution_mode",
    # Spec 0.31 (RM26, RM27, RM28).
    "local_slow",
    "no_local_model",
    "selection_failed",
}

INVALID_PROPOSALS = {
    "unknown-tool": (tool_turn("drop_everything", service="staging-api"), "unknown_tool"),
    "bad-enum": (tool_turn("get_service_health", service="prod-db"), None),
    "undeclared-field": (
        tool_turn("get_service_health", service="staging-api", region="eu"),
        None,
    ),
    "missing-field": (tool_turn("get_service_health"), None),
    "wrong-type": (tool_turn("get_service_health", service=7), None),
    "not-allowed": (
        tool_turn("delete_demo_database", database_id="demo-orders-db", expected_revision=3),
        None,
    ),
}


def good_tail(call_id="call_2"):
    return [
        tool_turn("get_service_health", call_id=call_id, service="staging-api"),
        text_turn(STAGING_OK),
        text_turn(STAGING_OK),
    ]


# --- no execution, bounded repair ------------------------------------------------------


@pytest.mark.parametrize("case", INVALID_PROPOSALS)
def test_an_invalid_proposal_is_never_executed(tmp_path, case):
    bad_turn, code = INVALID_PROPOSALS[case]
    gateway = build_gateway(tmp_path, [route_turn("check-service-health"), bad_turn, bad_turn])
    before = gateway.sandbox.state_bytes()
    outcome, events = run(gateway)

    assert executed_tools(events) == []
    # The trace keeps the evidence: each rejected proposal and the model
    # output it came from (section 5: partial evidence stays in the trace).
    finished = of_type(events, "model.finished")
    assert [f.payload["purpose"] for f in finished] == ["routing", "skill", "repair"]
    assert all(f.payload["tool_calls"] == 1 for f in finished)
    proposals = of_type(events, "tool.proposed")
    assert len(proposals) == 2
    for proposal in proposals:
        assert proposal.payload["admissible"] is False
        assert proposal.payload["rejection"]["code"]
        if code is not None:
            assert proposal.payload["rejection"]["code"] == code
    assert gateway.sandbox.state_bytes() == before


@pytest.mark.parametrize("case", INVALID_PROPOSALS)
def test_an_unrepaired_proposal_stops_with_invalid_output(tmp_path, case):
    bad_turn, _ = INVALID_PROPOSALS[case]
    gateway = build_gateway(tmp_path, [route_turn("check-service-health"), bad_turn, bad_turn])
    outcome, events = run(gateway)

    assert outcome.state is RunState.FAILED
    assert (outcome.error_code, outcome.reason_code) == ("needs_external", "invalid_output")
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["reason_code"] == "invalid_output"
    assert not of_type(events, "answer.delta")
    assert purposes(events) == ["routing", "skill", "repair"]


@pytest.mark.parametrize("case", INVALID_PROPOSALS)
def test_one_repair_lets_a_valid_proposal_run(tmp_path, case):
    bad_turn, _ = INVALID_PROPOSALS[case]
    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), bad_turn, *good_tail()],
        budgets=LocalBudgets(local_generations=4),
    )
    outcome, events = run(gateway)

    assert outcome.state is RunState.COMPLETED
    assert executed_tools(events) == ["get_service_health"]
    assert purposes(events) == ["routing", "skill", "repair", "skill", "final"]


def test_exactly_one_repair_is_counted_against_the_generation_budget(tmp_path):
    # RM03: the repair is one of the local generations, never on top of them.
    bad_turn, _ = INVALID_PROPOSALS["unknown-tool"]
    gateway = build_gateway(tmp_path, [route_turn("check-service-health"), bad_turn, bad_turn])
    _, events = run(gateway)
    (failed,) = of_type(events, "run.failed")
    usage = failed.payload["budget_usage"]
    assert usage["repair_generations"] == 1
    assert usage["local_generations"] == 2
    assert usage["routing_generations"] == 1
    assert usage["tool_calls"] == 0


def test_the_repair_uses_up_a_generation_the_answer_then_lacks(tmp_path):
    # With the default three local generations, a repaired tool call leaves
    # only one generation: the finalize proposal fits, the final one does
    # not, so the run stops with local_budget instead of running a fourth.
    bad_turn, _ = INVALID_PROPOSALS["unknown-tool"]
    gateway = build_gateway(tmp_path, [route_turn("check-service-health"), bad_turn, *good_tail()])
    outcome, events = run(gateway)

    assert (outcome.error_code, outcome.reason_code) == ("needs_external", "local_budget")
    assert purposes(events) == ["routing", "skill", "repair", "skill"]
    assert not of_type(events, "answer.delta")


def test_without_a_repair_budget_an_invalid_proposal_stops_at_once(tmp_path):
    bad_turn, _ = INVALID_PROPOSALS["unknown-tool"]
    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), bad_turn, *good_tail()],
        budgets=LocalBudgets(repair_generations=0),
    )
    outcome, events = run(gateway)

    assert (outcome.error_code, outcome.reason_code) == ("needs_external", "invalid_output")
    assert purposes(events) == ["routing", "skill"]
    assert executed_tools(events) == []


def test_the_repair_generation_sees_the_rejection(tmp_path):
    bad_turn, _ = INVALID_PROPOSALS["unknown-tool"]
    gateway = build_gateway(
        tmp_path,
        [route_turn("check-service-health"), bad_turn, *good_tail()],
        budgets=LocalBudgets(local_generations=4),
    )
    run(gateway)
    repair_request = gateway.adapter.requests[2]
    assert repair_request.generation_id.endswith("_repair")
    tool_messages = [m for m in repair_request.messages if m.role.value == "tool"]
    assert tool_messages and "unknown_tool" in tool_messages[-1].content


# --- RM02: invalid routing output -----------------------------------------------------------


@pytest.mark.parametrize(
    "routing_turn",
    [
        tool_turn("no-such-skill", language="en"),
        tool_turn("check-service-health", language="fr"),
        tool_turn("check-service-health", language="en", service="staging-api"),
        tool_turn("get_service_health", service="staging-api"),
    ],
    ids=["unknown-skill", "bad-language", "undeclared-field", "application-tool"],
)
def test_invalid_routing_output_never_executes_and_records_a_reason(tmp_path, routing_turn):
    gateway = build_gateway(tmp_path, [routing_turn])
    outcome, events = run(gateway)
    assert executed_tools(events) == []
    assert not of_type(events, "skill.loaded")
    assert (outcome.error_code, outcome.reason_code) == ("needs_external", "invalid_output")


# --- RM04: every escalation carries a section 8 reason code --------------------------------


def test_the_published_reason_codes_are_the_section_8_list():
    assert REASON_CODES == SECTION_8_REASON_CODES


ESCALATIONS = {
    "unrepaired-output": (
        [route_turn("check-service-health"), *([tool_turn("drop_everything")] * 2)],
        {},
        "invalid_output",
    ),
    "generation-budget": (
        [
            route_turn("check-service-health"),
            tool_turn("drop_everything"),
            *good_tail(),
        ],
        {},
        "local_budget",
    ),
    "generation-error": (
        [[GenerationFailed(code="server_error", message="boom")]],
        {},
        "capability_missing",
    ),
    "oversized-input": ([], {"input_tokens": 4096}, "context_limit"),
    "parallel-workflow": (
        [route_turn("health-and-diagnostic-checklist")],
        {},
        "parallel_workflow",
    ),
    "two-routing-calls": (
        [
            [
                ToolCall(
                    call_id="a", tool_name="check-service-health", arguments={"language": "en"}
                ),
                ToolCall(
                    call_id="b", tool_name="check-service-health", arguments={"language": "en"}
                ),
                Completed(finish_reason="tool_calls"),
            ]
        ],
        {},
        "invalid_output",
    ),
}


@pytest.mark.parametrize("case", ESCALATIONS)
def test_every_escalation_carries_a_section_8_reason_code(tmp_path, case):
    turns, options, expected = ESCALATIONS[case]
    gateway = build_gateway(tmp_path, turns, **options)
    outcome, events = run(gateway)

    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "needs_external"
    assert outcome.reason_code == expected
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["reason_code"] in SECTION_8_REASON_CODES
    assert failed.payload["reason_code"] == expected
    assert failed.payload["external_calls"] == 0


def test_an_escalation_without_a_section_8_reason_code_is_refused():
    """RM04: the trace must use a reason code. An unlisted label is a bug in
    the gateway, not something to write into a trace."""
    assert Escalation("invalid_output", "detail").reason_code == "invalid_output"
    with pytest.raises(ValueError):
        Escalation("model_was_unsure", "detail")


def test_an_unlisted_reason_inside_a_run_fails_it_and_frees_the_slot(tmp_path):
    """RM17 / section 5: the refusal is an unexpected exception, so the run
    ends as failed with internal_error and never stays active (review 61.2)."""

    def unlisted(request):
        raise Escalation("model_was_unsure", "detail")

    gateway = build_gateway(tmp_path, [route_turn("check-service-health")], on_generate=unlisted)
    with pytest.raises(ValueError):
        run(gateway)
    run_id = gateway.adapter.requests[0].generation_id.removesuffix("_gen1_routing")
    record = gateway.store.get_run(run_id)
    assert (record.state, record.error_code) == ("failed", "internal_error")
    assert gateway.store.active_run_id() is None


def test_an_unlisted_reason_in_a_resumed_stage_fails_it_and_frees_the_slot(tmp_path):
    """RM17 / section 5: the same holds after a clarification resume: the
    resumed stage is guarded too, so the run ends as failed with
    internal_error and never stays active (review 61.2, #4335042351)."""
    gateway = build_gateway(tmp_path, [route_turn("check-service-health")])
    waiting, _ = run(gateway, "Is the service healthy?")
    assert waiting.state is RunState.WAITING_INPUT

    def unlisted(request):
        raise Escalation("model_was_unsure", "detail")

    gateway.adapter.on_generate = unlisted
    version = gateway.store.get_run(waiting.run_id).version
    with pytest.raises(ValueError):
        asyncio.run(gateway.orchestrator.resume(waiting.run_id, version, "staging-api"))
    record = gateway.store.get_run(waiting.run_id)
    assert (record.state, record.error_code) == ("failed", "internal_error")
    assert gateway.store.active_run_id() is None
    assert waiting.run_id not in gateway.orchestrator._waiting
    (failed,) = of_type(gateway.store.events(waiting.run_id), "run.failed")
    assert failed.payload["error_code"] == "internal_error"
