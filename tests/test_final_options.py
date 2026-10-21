"""A chat request's token budget and sampling apply to the answer only (RM19, RM03).

Spec 0.14 section 7.1, issue #22: max_completion_tokens, temperature,
and top_p are applied to the final-response generation, and to the
external generations that produce the answer. Routing, skill, and
repair generations keep their controlled settings, so a client cannot
loosen the routing budget (RM03) by sending a large value.
"""

import asyncio

from gateway_helpers import (
    STAGING_OK,
    build_gateway,
    route_turn,
    text_turn,
    tool_turn,
)
from relaymind.adapters import GenerationOptions, Message, Role
from relaymind.orchestrator.local_loop import EXTERNAL_OPTIONS, LOCAL_OPTIONS

REQUESTED = GenerationOptions(max_output_tokens=50, temperature=0.4, top_p=0.9)
PROMPT = (Message(role=Role.USER, content="Is staging-api healthy right now?"),)


def local_gateway(tmp_path):
    return build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="staging-api"),
            text_turn(STAGING_OK),
            text_turn(STAGING_OK),
        ],
        external_turns=[],
    )


def test_only_the_final_generation_takes_the_requested_options(tmp_path):
    gateway = local_gateway(tmp_path)
    outcome = asyncio.run(gateway.orchestrator.run(PROMPT, final_options=REQUESTED))
    assert outcome.answer == STAGING_OK
    final = gateway.adapter.requests[-1]
    assert final.generation_id.endswith("_final")
    assert final.options == GenerationOptions(max_output_tokens=50, temperature=0.4, top_p=0.9)
    # Every earlier generation keeps the controlled settings.
    assert len(gateway.adapter.requests) == 4
    for request in gateway.adapter.requests[:-1]:
        assert request.options.max_output_tokens != 50
        assert request.options.top_p is None
        assert request.options.temperature == 0


def test_the_final_generation_keeps_its_default_when_nothing_is_requested(tmp_path):
    gateway = local_gateway(tmp_path)
    asyncio.run(gateway.orchestrator.run(PROMPT))
    assert gateway.adapter.requests[-1].options == LOCAL_OPTIONS


def test_a_partial_request_keeps_the_controlled_temperature(tmp_path):
    gateway = local_gateway(tmp_path)
    asyncio.run(
        gateway.orchestrator.run(PROMPT, final_options=GenerationOptions(max_output_tokens=7))
    )
    assert gateway.adapter.requests[-1].options == GenerationOptions(
        max_output_tokens=7, temperature=LOCAL_OPTIONS.temperature
    )


def test_external_generations_that_write_the_answer_take_the_requested_options(tmp_path):
    gateway = build_gateway(tmp_path, [], external_turns=[text_turn("staging-api answer")])
    asyncio.run(
        gateway.orchestrator.run(PROMPT, execution_mode="external_only", final_options=REQUESTED)
    )
    (request,) = gateway.external.requests
    assert request.options == GenerationOptions(max_output_tokens=50, temperature=0.4, top_p=0.9)
    assert gateway.adapter.requests == []


def test_external_defaults_hold_when_nothing_is_requested(tmp_path):
    gateway = build_gateway(tmp_path, [], external_turns=[text_turn("staging-api answer")])
    asyncio.run(gateway.orchestrator.run(PROMPT, execution_mode="external_only"))
    assert gateway.external.requests[0].options == EXTERNAL_OPTIONS
