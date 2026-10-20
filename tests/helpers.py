"""Shared test helpers for the adapter test suite."""

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import httpx

from relaymind.adapters import (
    AdapterCapabilities,
    AdapterHealth,
    GenerationRequest,
    Message,
    ModelAdapter,
    Role,
    ToolSchema,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / "models"
SCRIPTS_DIR = REPO_ROOT / "scripts"

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


def run_closing(adapter, action):
    """Run one async action on an adapter in one event loop, then close it.

    The adapter is closed even when the action raises, so a failing
    assertion inside the action never leaks the HTTP client.
    """

    async def go():
        async with adapter:
            return await action(adapter)

    return asyncio.run(go())


def delta_chunk(delta: dict, finish_reason: str | None = None) -> dict:
    """One streamed chat-completion chunk in the shape llama-server emits."""
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def sse_body(chunks: list[dict]) -> str:
    """Encode chunks as a server-sent-events response body."""
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    return "".join(lines) + "data: [DONE]\n\n"


def sse_response(chunks: list[dict]) -> httpx.Response:
    """A streamed chat-completion response for a mock transport."""
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=sse_body(chunks),
    )


class ScriptedAdapter(ModelAdapter):
    """Model double that replays a scripted event sequence."""

    def __init__(self, events):
        self._events = events
        self.cancelled: list[str] = []

    async def capabilities(self) -> AdapterCapabilities:
        return make_capabilities()

    async def generate(self, request: GenerationRequest):
        for event in self._events:
            yield event

    async def cancel(self, generation_id: str) -> None:
        self.cancelled.append(generation_id)

    async def health(self) -> AdapterHealth:
        return AdapterHealth(available=True, warm=True, detail="scripted")


def load_script_module(name: str):
    """Import scripts/<name>.py as a module so its functions can be tested."""
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(name: str, *args) -> subprocess.CompletedProcess:
    """Run scripts/<name>.py as a subprocess and capture its output."""
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / f"{name}.py"), *map(str, args)],
        capture_output=True,
        text=True,
    )
