"""Builders for orchestrator tests: sandbox tools, demo skills, model doubles.

Everything is deterministic: one FakeClock for the store, the tools, the
sandbox, and the loop (RM25), a fresh sandbox copy per test, and
scripted model turns (spec section 13). Nothing here touches a
production resource. Every gateway also has an external model double
that records requests, so a test measures external calls (RM01) instead
of trusting a number in the trace.
"""

import asyncio
import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path

from helpers import BASELINE, FakeClock, ScriptedAdapter, make_demo_skills, of_type
from relaymind.adapters import Completed, TextDelta, ToolCall, Usage
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.local_loop import ConfiguredModel, LocalOrchestrator
from relaymind.orchestrator.run_state import RunState
from relaymind.skills import SkillRegistry
from relaymind.store import RunStore
from relaymind.tools import CallerContext, OperatorPolicy, ToolRegistry
from relaymind.tools.sandbox import (
    DEMO_TOOL_NAMES,
    READ_PERMISSION,
    SandboxDataService,
    register_demo_tools,
)

LOCAL_MODEL_ID = "double-qwen3-0.6b"
EXTERNAL_MODEL_ID = "double-external"


@dataclass
class Gateway:
    orchestrator: LocalOrchestrator
    adapter: ScriptedAdapter
    external: ScriptedAdapter
    store: RunStore
    sandbox: SandboxDataService
    tools: ToolRegistry
    skills: SkillRegistry
    clock: FakeClock


def build_gateway(
    tmp_path,
    turns,
    *,
    budgets: LocalBudgets | None = None,
    capabilities=None,
    input_tokens: int = 800,
    on_generate=None,
    clock: FakeClock | None = None,
    sandbox_clock=None,
    caller: CallerContext | None = None,
    seed: Path = BASELINE,
    **options,
) -> Gateway:
    """A gateway over a fresh sandbox; options go to LocalOrchestrator."""
    clock = clock or FakeClock()
    tools = ToolRegistry(clock=clock)
    sandbox = SandboxDataService(
        seed, work_dir=tmp_path / "sandbox", clock=sandbox_clock or clock, reset=True
    )
    register_demo_tools(tools, sandbox)
    skills = make_demo_skills(tools)
    adapter = ScriptedAdapter(
        turns=turns, capabilities=capabilities, input_tokens=input_tokens, on_generate=on_generate
    )
    external = ScriptedAdapter(turns=[], locality="external")
    store = RunStore(tmp_path / "runs.sqlite3", clock=clock)
    orchestrator = LocalOrchestrator(
        ConfiguredModel(adapter=adapter, model_id=LOCAL_MODEL_ID),
        skills=skills,
        tools=tools,
        store=store,
        policy=OperatorPolicy(enabled_tools=DEMO_TOOL_NAMES),
        caller=caller or CallerContext(permissions=frozenset({READ_PERMISSION})),
        budgets=budgets or LocalBudgets(),
        external=ConfiguredModel(adapter=external, model_id=EXTERNAL_MODEL_ID),
        **options,
    )
    return Gateway(orchestrator, adapter, external, store, sandbox, tools, skills, clock)


def run(gateway: Gateway, prompt: str = "Is staging-api healthy right now?", **options):
    """Run one text request; return the outcome and its trace."""
    outcome = asyncio.run(gateway.orchestrator.run_text(prompt, **options))
    return outcome, gateway.store.events(outcome.run_id)


def assert_needs_external(gateway: Gateway, outcome, events, reason_code: str) -> None:
    """RM04: the run failed as needs_external with this reason, nothing published."""
    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "needs_external"
    assert outcome.reason_code == reason_code
    assert outcome.answer is None
    record = gateway.store.get_run(outcome.run_id)
    assert (record.state, record.error_code) == ("failed", "needs_external")
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["error_code"] == "needs_external"
    assert failed.payload["reason_code"] == reason_code
    assert "budget_usage" in failed.payload
    assert not of_type(events, "answer.delta")
    # No external model was called (RM01), measured on the double.
    assert gateway.external.requests == []
    assert failed.payload["external_calls"] == len(gateway.external.requests)
    # The run released the execution slot (section 11).
    assert gateway.store.active_run_id() is None


def executed_tools(events) -> list[str]:
    """Tool names that really ran: tool.started is emitted only for executions."""
    return [e.payload["tool_name"] for e in of_type(events, "tool.started")]


def replace_handler(
    gateway: Gateway, name: str, handler, *, timeout_seconds: float | None = None
) -> None:
    """Swap one registered tool's handler, and its timeout when given,
    keeping its declaration otherwise."""
    tool = gateway.tools.get(name)
    changes = {"handler": handler}
    if timeout_seconds is not None:
        changes["timeout_seconds"] = timeout_seconds
    declaration = dataclasses.replace(tool.declaration, **changes)
    gateway.tools._tools[name] = dataclasses.replace(tool, declaration=declaration)


def seed_with(tmp_path: Path, **changes) -> Path:
    """A copy of the baseline seed with top-level fields replaced."""
    data = json.loads(BASELINE.read_text("utf-8"))
    data.update(changes)
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(data), "utf-8")
    return path


def route_turn(skill: str, language: str = "en") -> list:
    """The routing generation selecting one skill function."""
    return [
        ToolCall(call_id="call_route", tool_name=skill, arguments={"language": language}),
        Usage(input_tokens=800, output_tokens=12),
        Completed(finish_reason="tool_calls"),
    ]


def tool_turn(tool: str, call_id: str = "call_tool", **arguments) -> list:
    return [
        ToolCall(call_id=call_id, tool_name=tool, arguments=arguments),
        Completed(finish_reason="tool_calls"),
    ]


def text_turn(*pieces: str) -> list:
    return [*(TextDelta(text=piece) for piece in pieces), Completed(finish_reason="stop")]
