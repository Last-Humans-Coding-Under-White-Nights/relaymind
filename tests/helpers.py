"""Shared test helpers for the test suite."""

import asyncio
import importlib.util
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
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
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig
from relaymind.orchestrator.workflows import PARALLEL_WORKFLOWS
from relaymind.skills import SkillRegistry
from relaymind.skills.predicates import COMPLETION_PREDICATES
from relaymind.store import TraceClock
from relaymind.tools import SideEffect, ToolDeclaration, ToolRegistry
from relaymind.tools.sandbox import SandboxDataService, register_demo_tools

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / "models"
SCRIPTS_DIR = REPO_ROOT / "scripts"

# The checked-in candidate manifest (RM18, spec section 6).
PINNED_MANIFEST_PATH = MODELS_DIR / "qwen3-0.6b-q8_0.manifest.json"

# The start time of FakeClock, a fixed UTC moment.
T0 = datetime(2026, 10, 20, 3, 0, 0, tzinfo=UTC)


class FakeClock(TraceClock):
    """A controllable UTC clock and monotonic clock (RM25) for store tests."""

    def __init__(self) -> None:
        self.now = T0
        self.mono = 1000.0

    def utc_now(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.mono += seconds


# A valid run.accepted payload (spec section 8). run.accepted is the
# first event of every run (RM22), so store tests write it before others.
ACCEPTED_PAYLOAD = {
    "requested_execution_mode": None,
    "effective_execution_mode": "auto",
    "model_alias": "relaymind",
    "budgets": {"run_generations": 8, "run_tool_executions": 6, "run_active_seconds": 60},
    "skill_roots": ["/srv/relaymind/skills"],
    "skills_offered": [
        {"name": "check-service-health", "version": "1.0.0", "content_hash": "sha256:ab12"}
    ],
}

# The section 9 service-ID input object, shared across test files.
SERVICE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"service": {"enum": ["staging-api", "payments-api"]}},
    "required": ["service"],
    "additionalProperties": False,
}

# The canonical demo tool (spec section 9), shared across test files.
HEALTH_TOOL = ToolSchema(
    name="get_service_health",
    description="Return current health for a service.",
    input_schema=SERVICE_INPUT_SCHEMA,
)

# Sandbox demo data (RM23, section 9) and a fixed test time.
BASELINE = REPO_ROOT / "tests" / "fixtures" / "sandbox" / "baseline.json"
# The checked-in demo skills: the default root, skills/, of this checkout.
DEMO_SKILLS_ROOT = REPO_ROOT / "skills"


def make_tool_declaration(**overrides) -> ToolDeclaration:
    """A valid read-only tool declaration; overrides replace fields."""
    fields = {
        "name": "echo_service",
        "description": "Return the requested service ID.",
        "input_schema": SERVICE_INPUT_SCHEMA,
        "output_schema": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
            "additionalProperties": False,
        },
        "timeout_seconds": 1.0,
        "side_effect": SideEffect.READ_ONLY,
        "handler": lambda args: {"service": args["service"]},
        "required_permission": "demo.read",
    }
    fields.update(overrides)
    return ToolDeclaration(**fields)


def make_sandbox(tmp_path: Path, seed: Path = BASELINE) -> SandboxDataService:
    """A fresh, reset sandbox working copy under tmp_path (fixtures rule)."""
    return SandboxDataService(seed, work_dir=tmp_path / "sandbox", clock=FakeClock(), reset=True)


def make_demo_tools(tmp_path: Path, service: SandboxDataService | None = None) -> ToolRegistry:
    """The seven section 9 tools over a sandbox (a fresh one by default)."""
    registry = ToolRegistry(clock=FakeClock())
    register_demo_tools(registry, service or make_sandbox(tmp_path))
    return registry


def make_demo_skills(tools: ToolRegistry) -> SkillRegistry:
    """The checked-in demo skills, loaded from this checkout's skills/."""
    return SkillRegistry.load(
        [DEMO_SKILLS_ROOT],
        tools=tools,
        predicates=COMPLETION_PREDICATES.keys(),
        workflows=PARALLEL_WORKFLOWS,
    )


# A llama-server endpoint for adapter tests over a mock transport.
LLAMACPP_CONFIG = LlamaCppConfig(
    base_url="http://127.0.0.1:8033",
    context_limit=8192,
    chat_template_kwargs={"enable_thinking": False},
)


def make_llamacpp_adapter(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
    config: LlamaCppConfig = LLAMACPP_CONFIG,
) -> LlamaCppAdapter:
    """A LlamaCppAdapter whose HTTP requests go to handler."""
    return LlamaCppAdapter(config, transport=httpx.MockTransport(handler))


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

    async def count_tokens(
        self,
        messages: Sequence[Message],
        tool_schemas: Sequence[ToolSchema] = (),
    ) -> int:
        # The double claims a tokenizer (make_capabilities), so it counts
        # instead of inheriting the refusing default: one unit per
        # content character and per offered tool. A fixed rule for
        # tests, not an estimate of any real tokenizer.
        return sum(len(message.content or "") for message in messages) + len(tool_schemas)


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
