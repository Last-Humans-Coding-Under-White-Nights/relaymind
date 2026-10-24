"""AC20, RM05, RM26, RM28: Chinese requests use managed external execution.

The real example matcher selects from the bilingual demo packages; only
the external generations are scripted (spec section 13).
"""

import asyncio

import pytest

from gateway_helpers import PAYMENTS_ZH, executed_tools, text_turn, tool_turn
from helpers import of_type
from relaymind.adapters import Role
from relaymind.orchestrator.run_state import RunState
from test_ac20_no_local import CLASSES, assert_no_local_call, no_local, run

STAGING_ZH = "staging-api 的状态是 healthy（健康），检查时间为 2026-10-20T02:58:00Z。"


@pytest.mark.parametrize("status", CLASSES.values(), ids=CLASSES)
@pytest.mark.parametrize(
    ("prompt", "service", "answer"),
    [
        ("staging-api 现在健康吗？", "staging-api", STAGING_ZH),
        ("帮我检查一下 payments-api 的健康状态。", "payments-api", PAYMENTS_ZH),
    ],
    ids=["staging", "payments"],
)
def test_chinese_examples_select_the_skill_without_local_inference(
    tmp_path, status, prompt, service, answer
):
    """AC20, RM26, RM28: the actual Chinese examples select and run the health skill."""
    gateway = no_local(
        tmp_path,
        status,
        [tool_turn("get_service_health", service=service), text_turn(answer)],
    )
    outcome, events = run(gateway, prompt)

    assert outcome.state is RunState.COMPLETED and outcome.answer == answer
    (route,) = of_type(events, "route.selected")
    assert route.actor == "gateway"
    assert route.payload["mechanism"] == "example_match"
    assert route.payload["selector"]["attempts"] == [
        {"name": "example_match", "outcome": "selected"}
    ]
    decision = route.payload["decision"]
    assert decision["language"] == "zh"
    assert decision["skill_id"] == "check-service-health"
    assert (decision["route"], decision["reason_code"]) == ("external", "no_local_model")
    assert route.payload["local_model_status"] == status.status.value
    (loaded,) = of_type(events, "skill.loaded")
    assert loaded.payload["name"] == "check-service-health"
    assert executed_tools(events) == ["get_service_health"]
    assert len(gateway.external.requests) == 2
    assert_no_local_call(gateway, events)


@pytest.mark.parametrize("status", CLASSES.values(), ids=CLASSES)
def test_chinese_missing_identifier_clarifies_and_keeps_language_after_resume(tmp_path, status):
    """AC20, RM05, RM26, RM28: gateway clarification and resume retain Chinese."""
    gateway = no_local(
        tmp_path,
        status,
        [tool_turn("get_service_health", service="payments-api"), text_turn(PAYMENTS_ZH)],
    )
    # The first Chinese health example with the service identifier omitted.
    waiting, events = run(gateway, "现在健康吗？")

    assert waiting.state is RunState.WAITING_INPUT
    assert waiting.clarification.startswith("请问是哪个服务？")
    (route,) = of_type(events, "route.selected")
    assert route.actor == "gateway"
    assert route.payload["mechanism"] == "example_match"
    decision = route.payload["decision"]
    assert decision["skill_id"] == "check-service-health"
    assert (decision["route"], decision["reason_code"]) == ("clarify", "missing_input")
    (asked,) = of_type(events, "clarification.required")
    assert asked.payload["question"] == waiting.clarification
    assert asked.payload["language"] == "zh"
    assert asked.payload["missing"] == "service"
    assert gateway.external.requests == []
    assert not of_type(events, "tool.proposed")
    assert executed_tools(events) == []
    assert_no_local_call(gateway, events)

    version = gateway.store.get_run(waiting.run_id).version
    outcome = asyncio.run(gateway.orchestrator.resume(waiting.run_id, version, "payments-api"))
    events = gateway.store.events(waiting.run_id)

    assert outcome.state is RunState.COMPLETED and outcome.answer == PAYMENTS_ZH
    selected = of_type(events, "route.selected")
    assert [event.payload["decision"]["language"] for event in selected] == ["zh", "zh"]
    resumed = selected[1]
    assert resumed.payload["mechanism"] == "clarification_resume"
    assert resumed.payload["decision"]["skill_id"] == "check-service-health"
    assert resumed.payload["decision"]["route"] == "external"
    assert resumed.payload["decision"]["reason_code"] == "no_local_model"
    assert resumed.payload["local_model_status"] == status.status.value
    assert executed_tools(events) == ["get_service_health"]
    for request in gateway.external.requests:
        system = "\n".join(m.content for m in request.messages if m.role is Role.SYSTEM)
        assert "请用中文回答。" in system
    assert len(gateway.external.requests) == 2
    assert_no_local_call(gateway, events)
