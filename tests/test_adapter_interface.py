"""The ModelAdapter interface: capabilities, generate, cancel, health.

Covers RM18 and spec section 6. Deterministic tests use a scripted
double; no live model is involved. Issue #1.
"""

import asyncio
import dataclasses

import pytest

from helpers import HEALTH_TOOL, ScriptedAdapter, collect_events, make_capabilities, make_request
from relaymind.adapters import (
    Completed,
    GenerationOptions,
    GenerationRequest,
    Message,
    ModelAdapter,
    Role,
    TextDelta,
    ToolCall,
    ToolSchema,
)

CAPS = make_capabilities()


def test_adapter_with_missing_methods_cannot_be_instantiated():
    class Incomplete(ModelAdapter):
        async def capabilities(self):
            return CAPS

    with pytest.raises(TypeError):
        Incomplete()


def test_double_streams_normalized_events_in_order():
    scripted = [
        ToolCall(
            call_id="c1",
            tool_name="get_service_health",
            arguments={"service": "staging-api"},
        ),
        TextDelta(text="staging-api is healthy"),
        Completed(finish_reason="stop"),
    ]
    adapter = ScriptedAdapter(scripted)
    events = collect_events(adapter, make_request())
    assert events == scripted


def test_cancel_targets_a_generation_id():
    adapter = ScriptedAdapter([])
    asyncio.run(adapter.cancel("gen_1"))
    assert adapter.cancelled == ["gen_1"]


def test_health_reports_availability_and_warm_state():
    adapter = ScriptedAdapter([])
    health = asyncio.run(adapter.health())
    assert health.available is True
    assert health.warm is True


def test_request_messages_use_supported_roles_only():
    with pytest.raises(ValueError):
        Message(role="moderator", content="hi")


def test_tool_message_carries_its_originating_call_id():
    message = Message(role=Role.TOOL, content='{"status": "healthy"}', tool_call_id="call_1")
    assert message.tool_call_id == "call_1"
    with pytest.raises(ValueError):
        Message(role=Role.TOOL, content='{"status": "healthy"}')
    with pytest.raises(ValueError):
        Message(role=Role.USER, content="hi", tool_call_id="call_1")


def test_assistant_message_can_record_prior_tool_calls_without_text():
    call = ToolCall(call_id="call_1", tool_name="get_service_health", arguments={"service": "s"})
    message = Message(role=Role.ASSISTANT, content=None, tool_calls=(call,))
    assert message.tool_calls == (call,)
    with pytest.raises(ValueError):
        Message(role=Role.ASSISTANT, content=None)  # no text and no tool calls
    with pytest.raises(ValueError):
        Message(role=Role.USER, content="hi", tool_calls=(call,))


def test_generation_options_reject_invalid_values():
    with pytest.raises(ValueError):
        GenerationOptions(max_output_tokens=0)
    with pytest.raises(ValueError):
        GenerationOptions(temperature=-0.1)
    with pytest.raises(ValueError):
        GenerationOptions(top_p=1.5)


def test_request_sequences_are_stored_as_tuples():
    request = GenerationRequest(
        generation_id="gen_1",
        messages=[Message(role=Role.USER, content="hi")],
        tool_schemas=[],
    )
    assert isinstance(request.messages, tuple)
    assert isinstance(request.tool_schemas, tuple)


def test_tool_schema_input_schema_is_a_frozen_mapping():
    schema = ToolSchema(name="t", description="d", input_schema={"type": "object"})
    with pytest.raises(TypeError):
        schema.input_schema["type"] = "array"
    with pytest.raises(TypeError):
        ToolSchema(name="t", description="d", input_schema="not-a-mapping")


def test_request_is_immutable_and_carries_tool_schemas():
    request = make_request(tool_schemas=(HEALTH_TOOL,))
    assert request.tool_schemas[0].name == "get_service_health"
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.generation_id = "other"


def test_sequence_fields_reject_strings():
    # A string is iterable; tuple("abc") silently decomposes it.
    with pytest.raises(TypeError):
        GenerationRequest(generation_id="gen_1", messages="not a sequence")


def test_assistant_tool_call_message_needs_no_explicit_content():
    call = ToolCall(call_id="call_1", tool_name="get_service_health", arguments={"service": "s"})
    message = Message(role=Role.ASSISTANT, tool_calls=(call,))
    assert message.content is None


def test_the_double_counts_tokens_when_it_claims_a_tokenizer():
    # RM16: a double that reports tokenizer_available must count, not
    # inherit the contract default that refuses.
    adapter = ScriptedAdapter([])
    assert asyncio.run(adapter.capabilities()).tokenizer_available is True
    count = asyncio.run(adapter.count_tokens(make_request().messages, (HEALTH_TOOL,)))
    assert isinstance(count, int) and count > 0
