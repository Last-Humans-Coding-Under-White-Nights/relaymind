"""Shared test helpers for the adapter test suite."""

import asyncio
import json

from relaymind.adapters import (
    AdapterCapabilities,
    GenerationRequest,
    Message,
    ModelAdapter,
    Role,
    ToolSchema,
)

# The canonical demo tool (spec section 9), shared across test files.
HEALTH_TOOL = ToolSchema(
    name="get_service_health",
    description="Return current health for a service.",
    input_schema={
        "type": "object",
        "properties": {"service": {"enum": ["staging-api", "payments-api"]}},
        "required": ["service"],
        "additionalProperties": False,
    },
)


def make_capabilities(**overrides) -> AdapterCapabilities:
    """A full capability record with sensible defaults."""
    base = dict(
        text_streaming=True,
        tool_calling=True,
        structured_routing_output=True,
        context_limit=32768,
        reasoning_controls=("enable_thinking",),
        usage_reporting=True,
        tokenizer_available=True,
    )
    base.update(overrides)
    return AdapterCapabilities(**base)


def make_request(**overrides) -> GenerationRequest:
    """A minimal generation request with sensible defaults."""
    base = dict(
        generation_id="gen_1",
        messages=(Message(role=Role.USER, content="is staging-api healthy?"),),
    )
    base.update(overrides)
    return GenerationRequest(**base)


def collect_events(adapter: ModelAdapter, request: GenerationRequest) -> list:
    """Run a generation to completion and return its events."""

    async def run():
        return [event async for event in adapter.generate(request)]

    return asyncio.run(run())


def delta_chunk(delta: dict, finish_reason: str | None = None) -> dict:
    """One streamed chat-completion chunk in the shape llama-server emits."""
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def sse_body(chunks: list[dict]) -> str:
    """Encode chunks as a server-sent-events response body."""
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    return "".join(lines) + "data: [DONE]\n\n"
