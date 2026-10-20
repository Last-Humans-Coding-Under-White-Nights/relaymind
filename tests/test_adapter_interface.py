"""The ModelAdapter interface: capabilities, generate, cancel, health.

Covers RM18 and spec section 6. Deterministic tests use a scripted
double; no live model is involved. Issue #1.
"""

import asyncio
import dataclasses

import pytest

from relaymind.adapters import (
    AdapterCapabilities,
    AdapterHealth,
    Completed,
    GenerationRequest,
    Message,
    ModelAdapter,
    Role,
    TextDelta,
    ToolCall,
    ToolSchema,
)

CAPS = AdapterCapabilities(
    text_streaming=True,
    tool_calling=True,
    structured_routing_output=True,
    context_limit=32768,
    reasoning_controls=(),
    usage_reporting=True,
    tokenizer_available=True,
)


class ScriptedAdapter(ModelAdapter):
    """Model double that replays a scripted event sequence."""

    def __init__(self, events):
        self._events = events
        self.cancelled: list[str] = []

    async def capabilities(self) -> AdapterCapabilities:
        return CAPS

    async def generate(self, request: GenerationRequest):
        for event in self._events:
            yield event

    async def cancel(self, generation_id: str) -> None:
        self.cancelled.append(generation_id)

    async def health(self) -> AdapterHealth:
        return AdapterHealth(available=True, warm=True, detail="scripted")


def collect(adapter: ModelAdapter, request: GenerationRequest):
    async def run():
        return [event async for event in adapter.generate(request)]

    return asyncio.run(run())


def make_request(**overrides):
    base = dict(
        generation_id="gen_1",
        messages=(Message(role=Role.USER, content="is staging-api healthy?"),),
    )
    base.update(overrides)
    return GenerationRequest(**base)


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
    events = collect(adapter, make_request())
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


def test_request_is_immutable_and_carries_tool_schemas():
    schema = ToolSchema(
        name="get_service_health",
        description="Return current health for a service.",
        input_schema={
            "type": "object",
            "properties": {"service": {"enum": ["staging-api", "payments-api"]}},
            "required": ["service"],
            "additionalProperties": False,
        },
    )
    request = make_request(tool_schemas=(schema,))
    assert request.tool_schemas[0].name == "get_service_health"
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.generation_id = "other"
