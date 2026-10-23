"""AC17: the draft answer pass (RM31, RM16, RM19, spec 0.32 sections 7.3, 8, 14.1).

Under answer_pass draft, the skill-stage draft that meets the completion
predicate is published without a second generation, when it is in the
run's language, holds no tool-call JSON, and the caller sent no answer
option. Otherwise the final generation runs as before and the recorded
options are what shaped the published answer. Under the default every
earlier test passes unchanged (tests/test_ac01_local_health.py is the
gate). Deterministic with model doubles (spec section 13).
"""

import asyncio

import pytest

from gateway_helpers import (
    PAYMENTS_OK,
    PAYMENTS_ZH,
    STAGING_OK,
    build_gateway,
    health_tail,
    local_eligible_skills,
    purposes,
    route_turn,
    run,
    text_turn,
    tool_turn,
)
from helpers import of_type
from relaymind.adapters import GenerationOptions, Message, Role
from relaymind.orchestrator.performance import PerformanceSettings
from relaymind.orchestrator.run_state import RunState
from test_ac02_handoff import ANSWER, local_collection

DRAFT = PerformanceSettings(answer_pass="draft")
PROMPT = (Message(role=Role.USER, content="Is staging-api healthy right now?"),)
# A passing draft that also holds a tool call written as text.
CALL_AS_TEXT = (
    STAGING_OK + ' {"name": "get_service_health", "arguments": {"service": "staging-api"}}'
)


def draft_gateway(tmp_path, turns, **options):
    return build_gateway(
        tmp_path, turns, external_turns=[], performance=options.pop("performance", DRAFT), **options
    )


def completed(events):
    (event,) = of_type(events, "run.completed")
    return event.payload


def accepted(events):
    (event,) = of_type(events, "run.accepted")
    return event.payload


def test_a_passing_draft_is_published(tmp_path):
    """AC17, RM31: purposes are routing, skill, skill; the draft is the answer."""
    gateway = draft_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
        ],
    )
    outcome, events = run(gateway)
    assert outcome.state is RunState.COMPLETED
    assert outcome.answer == STAGING_OK
    assert purposes(events) == ["routing", "skill", "skill"]
    assert len(gateway.adapter.requests) == 3
    assert completed(events)["answer_pass_applied"] == "draft"
    assert completed(events)["answer"] == STAGING_OK
    deltas = of_type(events, "answer.delta")
    assert "".join(e.payload["text"] for e in deltas) == STAGING_OK
    # Local-first (RM01): the draft pass makes no external call, and the trace agrees.
    assert gateway.external.requests == []
    assert completed(events)["external_calls"] == 0
    assert gateway.store.active_run_id() is None


def test_the_draft_is_published_by_the_local_actor(tmp_path):
    """RM14, RM20: the local model owns the answer, as the final pass would have."""
    gateway = draft_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
        ],
    )
    _, events = run(gateway)
    deltas = of_type(events, "answer.delta")
    assert {e.actor for e in deltas} == {"local"}
    assert completed(events)["final_answer_model"] == gateway.orchestrator.model.model_id


@pytest.mark.parametrize(
    "options",
    [
        GenerationOptions(temperature=0.2),
        GenerationOptions(max_output_tokens=64),
        GenerationOptions(top_p=0.9),
    ],
)
def test_a_caller_answer_option_runs_the_final_pass(tmp_path, options):
    """AC17, RM19, RM31: the options shape the published answer, so the final pass runs."""
    gateway = draft_gateway(tmp_path, [route_turn("check-service-health"), *health_tail()])
    outcome = asyncio.run(gateway.orchestrator.run(PROMPT, final_options=options))
    events = gateway.store.events(outcome.run_id)
    assert outcome.answer == STAGING_OK
    assert purposes(events) == ["routing", "skill", "skill", "final"]
    assert completed(events)["answer_pass_applied"] == "final"
    final = gateway.adapter.requests[-1]
    assert final.generation_id.endswith("_final")
    requested = {k: v for k, v in vars(options).items() if v is not None}
    assert {k: getattr(final.options, k) for k in requested} == requested
    # The recorded options are what shaped the published answer.
    recorded = accepted(events)["answer_options"]
    assert recorded == {
        {"max_output_tokens": "max_completion_tokens"}.get(k, k): v for k, v in requested.items()
    }


def test_a_chinese_run_with_an_english_draft_takes_the_final_pass(tmp_path):
    """AC17, RM31: the draft must be in the run's language; the final pass answers in Chinese."""
    gateway = draft_gateway(
        tmp_path,
        [
            route_turn("check-service-health", "zh"),
            tool_turn("get_service_health", service="payments-api"),
            text_turn(PAYMENTS_OK),
            text_turn(PAYMENTS_ZH),
        ],
    )
    outcome, events = run(gateway, "帮我检查一下 payments-api 的健康状态。")
    assert outcome.answer == PAYMENTS_ZH
    assert purposes(events) == ["routing", "skill", "skill", "final"]
    assert completed(events)["answer_pass_applied"] == "final"


def test_a_chinese_draft_in_a_chinese_run_is_published(tmp_path):
    gateway = draft_gateway(
        tmp_path,
        [
            route_turn("check-service-health", "zh"),
            tool_turn("get_service_health", service="payments-api"),
            text_turn(PAYMENTS_ZH),
        ],
    )
    outcome, events = run(gateway, "帮我检查一下 payments-api 的健康状态。")
    assert outcome.answer == PAYMENTS_ZH
    assert purposes(events) == ["routing", "skill", "skill"]
    assert completed(events)["answer_pass_applied"] == "draft"


def test_a_chinese_draft_in_an_english_run_takes_the_final_pass(tmp_path):
    gateway = draft_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="payments-api"),
            text_turn(PAYMENTS_ZH),
            text_turn(PAYMENTS_OK),
        ],
    )
    outcome, events = run(gateway, "Check the health of payments-api.")
    assert outcome.answer == PAYMENTS_OK
    assert completed(events)["answer_pass_applied"] == "final"


def test_a_draft_that_holds_tool_call_json_takes_the_final_pass(tmp_path):
    """AC17, RM09, RM31: a tool call written as text is never published as the answer."""
    gateway = draft_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn(CALL_AS_TEXT),
            text_turn(STAGING_OK),
        ],
    )
    outcome, events = run(gateway)
    assert outcome.answer == STAGING_OK
    assert CALL_AS_TEXT not in [e.payload["text"] for e in of_type(events, "answer.delta")]
    assert purposes(events) == ["routing", "skill", "skill", "final"]
    assert completed(events)["answer_pass_applied"] == "final"


def test_a_draft_that_misses_the_predicate_is_never_published(tmp_path):
    """RM16, RM31: the draft is published only after the completion predicate passed."""
    gateway = draft_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn("staging-api looks fine."),
            text_turn(STAGING_OK),
        ],
    )
    outcome, events = run(gateway)
    assert outcome.answer == STAGING_OK
    # The repair generation produced the draft that passed.
    assert purposes(events) == ["routing", "skill", "skill", "repair"]
    assert completed(events)["answer_pass_applied"] == "draft"


# -- the default, and what the pass does not touch --------------------------------------


def test_the_default_runs_the_final_pass_and_records_it(tmp_path):
    """AC17, AC01: the default is the baseline; the trace says which pass produced the answer."""
    gateway = build_gateway(
        tmp_path, [route_turn("check-service-health"), *health_tail()], external_turns=[]
    )
    outcome, events = run(gateway)
    assert outcome.answer == STAGING_OK
    assert purposes(events) == ["routing", "skill", "skill", "final"]
    assert completed(events)["answer_pass_applied"] == "final"
    assert accepted(events)["answer_pass"] == "final"


def test_run_accepted_records_the_performance_options_in_force(tmp_path):
    """RM22, spec 0.32 section 8: the five options are recorded with the run."""
    settings = PerformanceSettings(answer_pass="draft", calls_per_generation="many", prefetch=True)
    gateway = draft_gateway(
        tmp_path, [route_turn("check-service-health"), *health_tail()], performance=settings
    )
    _, events = run(gateway)
    recorded = accepted(events)
    assert {
        name: recorded[name]
        for name in (
            "answer_pass",
            "answer_stream",
            "calls_per_generation",
            "prefetch",
            "early_headers",
        )
    } == settings.to_json_object()


def test_the_default_records_the_baseline_options(tmp_path):
    gateway = build_gateway(
        tmp_path, [route_turn("check-service-health"), *health_tail()], external_turns=[]
    )
    _, events = run(gateway)
    assert accepted(events)["answer_pass"] == "final"
    assert accepted(events)["answer_stream"] == "buffered"
    assert accepted(events)["calls_per_generation"] == "one"
    assert accepted(events)["prefetch"] is False
    assert accepted(events)["early_headers"] is False


def test_an_external_answer_records_no_answer_pass(tmp_path):
    """Spec 0.32: absent when no RM31 pass applies; an external answer is its own generation."""
    gateway = build_gateway(
        tmp_path,
        local_collection(),
        skill_roots=local_eligible_skills(tmp_path),
        external_turns=[
            tool_turn("get_recent_logs", call_id="e1", service="staging-api", limit=3),
            text_turn(ANSWER),
        ],
        performance=DRAFT,
    )
    outcome, events = run(gateway, "Why is staging-api slow right now?")
    assert outcome.state is RunState.COMPLETED
    assert "answer_pass_applied" not in completed(events)


def test_the_options_do_not_change_an_external_only_run(tmp_path):
    gateway = build_gateway(
        tmp_path,
        [],
        external_turns=[text_turn("A plain answer.")],
        performance=DRAFT,
    )
    outcome, events = run(gateway, "Say something.", execution_mode="external_only")
    assert outcome.state is RunState.COMPLETED
    assert "answer_pass_applied" not in completed(events)
    assert gateway.adapter.requests == []
