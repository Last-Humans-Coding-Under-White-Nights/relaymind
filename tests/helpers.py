"""Shared test helpers for the adapter test suite."""

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

from relaymind.adapters import (
    AdapterCapabilities,
    GenerationRequest,
    Message,
    ModelAdapter,
    Role,
    ToolSchema,
)
from relaymind.adapters.manifest import ManifestError

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

# The checked-in candidate manifest (RM18, spec section 6).
PINNED_MANIFEST_PATH = MODELS_DIR / "qwen3-0.6b-q8_0.manifest.json"

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
    """Run a generation to completion, close the adapter, return events."""

    async def run():
        try:
            return [event async for event in adapter.generate(request)]
        finally:
            aclose = getattr(adapter, "aclose", None)
            if aclose is not None:
                await aclose()

    return asyncio.run(run())


def delta_chunk(delta: dict, finish_reason: str | None = None) -> dict:
    """One streamed chat-completion chunk in the shape llama-server emits."""
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def sse_body(chunks: list[dict]) -> str:
    """Encode chunks as a server-sent-events response body."""
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    return "".join(lines) + "data: [DONE]\n\n"


def smoke_adapter_settings(launch_settings: Mapping[str, object]) -> tuple[int, dict]:
    """Adapter context limit and base template kwargs from launch settings.

    The live smoke test configures its adapter from the manifest (spec
    section 6). Settings it cannot use fail here with one clear error.
    """
    ctx_size = launch_settings.get("ctx_size")
    if type(ctx_size) is not int or ctx_size <= 0:
        raise ManifestError(
            f"launch_settings.ctx_size must be a positive integer, got {ctx_size!r}"
        )
    template_kwargs = launch_settings.get("chat_template_kwargs", {})
    if not isinstance(template_kwargs, Mapping):
        raise ManifestError(
            "launch_settings.chat_template_kwargs must be an object, "
            f"got {type(template_kwargs).__name__}"
        )
    return ctx_size, dict(template_kwargs)
