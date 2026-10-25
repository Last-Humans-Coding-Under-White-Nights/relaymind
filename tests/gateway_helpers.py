"""Builders for orchestrator tests: sandbox tools, demo skills, model doubles.

Everything is deterministic: one FakeClock for the store, the tools, the
sandbox, and the loop (RM25), a fresh sandbox copy per test, and
scripted model turns (spec section 13). Nothing here touches a
production resource. Every gateway also has an external model double
that records requests, so a test measures external calls (RM01) instead
of trusting a number in the trace.

The external model is configured only when a test passes
``external_turns``. Without it the gateway has no external model, so
every escalation ends as needs_external (RM04) and the double cannot be
called. A test of a local run that must make zero external calls (RM01)
passes ``external_turns=[]``: the external model is then configured with
nothing scripted, so any call to it fails the test.
"""

import asyncio
import dataclasses
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from helpers import (
    BASELINE,
    DEMO_SKILLS_ROOT,
    FakeClock,
    ScriptedAdapter,
    make_demo_skills,
    of_type,
)
from relaymind.adapters import Completed, ModelAdapter, TextDelta, ToolCall, Usage
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.local_loop import ConfiguredModel, LocalOrchestrator
from relaymind.orchestrator.local_status import LocalModelClass, LocalStatus
from relaymind.orchestrator.run_state import RunState
from relaymind.orchestrator.workflows import PARALLEL_WORKFLOWS
from relaymind.skills import SkillRegistry
from relaymind.skills.predicates import COMPLETION_PREDICATES
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

# Final answers that meet the health predicate for the sandbox seed.
STAGING_OK = "staging-api is healthy (checked at 2026-10-20T02:58:00Z)."
PAYMENTS_OK = "payments-api is unavailable (checked at 2026-10-20T02:59:00Z)."
PAYMENTS_ZH = "payments-api 的状态是 unavailable（不可用），检查时间为 2026-10-20T02:59:00Z。"


@dataclass
class Gateway:
    """adapter and external are ScriptedAdapter doubles unless the test
    passed configured models over real adapters (see build_gateway)."""

    orchestrator: LocalOrchestrator
    adapter: ModelAdapter
    external: ModelAdapter
    store: RunStore
    sandbox: SandboxDataService
    tools: ToolRegistry
    skills: SkillRegistry
    clock: FakeClock


@pytest.fixture(autouse=True)
def close_gateway_stores(monkeypatch):
    """Close every store build_gateway opens in a test, so no SQLite handle leaks.

    Autouse in a test module that imports it.
    """
    opened: list[RunStore] = []
    store_class = RunStore

    def open_store(*args, **kwargs) -> RunStore:
        store = store_class(*args, **kwargs)
        opened.append(store)
        return store

    monkeypatch.setattr(sys.modules[__name__], "RunStore", open_store)
    yield
    for store in opened:
        store.close()


def build_gateway(
    tmp_path,
    turns,
    *,
    budgets: LocalBudgets | None = None,
    capabilities=None,
    input_tokens: int | None = None,
    on_generate=None,
    clock: FakeClock | None = None,
    sandbox_clock=None,
    caller: CallerContext | None = None,
    seed: Path = BASELINE,
    external_turns=None,
    external_capabilities=None,
    skill_roots=None,
    local_model: ConfiguredModel | None = None,
    external_model: ConfiguredModel | None = None,
    local_status: LocalStatus | None = None,
    **options,
) -> Gateway:
    """A gateway over a fresh sandbox; options go to LocalOrchestrator.

    external_turns scripts the external model (RM13); without it none
    is configured. skill_roots replaces the checked-in demo skills.
    local_model and external_model replace the model doubles with
    configured models over real adapters (AC08); gateway.adapter and
    gateway.external are then those adapters. Scripting a double that a
    configured model replaces is an error, never silently ignored.
    local_status sets the class of the local model (RM26): absent builds
    the gateway without a local model, and gateway.adapter is then a
    double no run can reach, so a test can show it was never called;
    unavailable and unsuitable keep the configured double.
    """
    absent = local_status is not None and local_status.status == LocalModelClass.ABSENT
    if absent:
        _reject_double_options(
            "an absent local model", local_model=local_model, turns=turns or None
        )
    if local_model is not None:
        _reject_double_options(
            "local_model",
            turns=turns,
            capabilities=capabilities,
            input_tokens=input_tokens,
            on_generate=on_generate,
        )
    if external_model is not None:
        _reject_double_options(
            "external_model",
            external_turns=external_turns,
            external_capabilities=external_capabilities,
        )
    clock = clock or FakeClock()
    tools = ToolRegistry(clock=clock)
    sandbox = SandboxDataService(
        seed, work_dir=tmp_path / "sandbox", clock=sandbox_clock or clock, reset=True
    )
    register_demo_tools(tools, sandbox)
    if skill_roots is None:
        skills = make_demo_skills(tools)
    else:
        skills = SkillRegistry.load(
            skill_roots,
            tools=tools,
            predicates=COMPLETION_PREDICATES.keys(),
            workflows=PARALLEL_WORKFLOWS,
        )
    store = RunStore(tmp_path / "runs.sqlite3", clock=clock)
    local = local_model or ConfiguredModel(
        adapter=ScriptedAdapter(
            turns=turns,
            capabilities=capabilities,
            input_tokens=800 if input_tokens is None else input_tokens,
            on_generate=on_generate,
        ),
        model_id=LOCAL_MODEL_ID,
    )
    if external_model is not None:
        remote, external = external_model, external_model.adapter
    else:
        external = ScriptedAdapter(
            turns=external_turns or [], capabilities=external_capabilities, locality="external"
        )
        remote = (
            None
            if external_turns is None
            else ConfiguredModel(adapter=external, model_id=EXTERNAL_MODEL_ID)
        )
    if local_status is not None:
        options["local_status"] = local_status
    orchestrator = LocalOrchestrator(
        None if absent else local,
        skills=skills,
        tools=tools,
        store=store,
        policy=OperatorPolicy(enabled_tools=DEMO_TOOL_NAMES),
        caller=caller or CallerContext(permissions=frozenset({READ_PERMISSION})),
        budgets=budgets or LocalBudgets(),
        external=remote,
        **options,
    )
    return Gateway(orchestrator, local.adapter, external, store, sandbox, tools, skills, clock)


def _reject_double_options(model: str, **options) -> None:
    """Raise when a test scripts a double that the configured model replaces."""
    given = sorted(name for name, value in options.items() if value is not None)
    if given:
        raise ValueError(f"{model} replaces the model double; drop {', '.join(given)}")


def run(gateway: Gateway, prompt: str = "Is staging-api healthy right now?", **options):
    """Run one text request; return the outcome and its trace."""
    outcome = asyncio.run(gateway.orchestrator.run_text(prompt, **options))
    return outcome, gateway.store.events(outcome.run_id)


def assert_needs_external(gateway: Gateway, outcome, events, reason_code: str) -> None:
    """RM04: the run failed as needs_external with this reason, nothing published."""
    assert isinstance(gateway.external, ScriptedAdapter), (
        "assert_needs_external measures external calls on the double; "
        "this gateway has a configured external adapter"
    )
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


def assert_workflow_unavailable(gateway: Gateway, outcome, events) -> None:
    """RM21, section 4.4: parallel_demo failed explicitly with workflow_unavailable."""
    assert outcome.state is RunState.FAILED
    assert outcome.error_code == "workflow_unavailable"
    assert outcome.reason_code == "parallel_workflow"
    assert outcome.answer is None
    record = gateway.store.get_run(outcome.run_id)
    assert (record.state, record.error_code) == ("failed", "workflow_unavailable")
    (failed,) = of_type(events, "run.failed")
    assert failed.payload["error_code"] == "workflow_unavailable"
    assert failed.payload["reason_code"] == "parallel_workflow"
    assert not of_type(events, "answer.delta")
    # No external call was made, measured on the double when there is one.
    if isinstance(gateway.external, ScriptedAdapter):
        assert gateway.external.requests == []
    assert gateway.store.active_run_id() is None


def executed_tools(events) -> list[str]:
    """Tool names that really ran: tool.started is emitted only for executions."""
    return [e.payload["tool_name"] for e in of_type(events, "tool.started")]


def purposes(events) -> list[str]:
    """The purpose of every model call, in order (RM03)."""
    return [e.payload["purpose"] for e in of_type(events, "model.started")]


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


def local_eligible_skills(tmp_path: Path) -> list[Path]:
    """Skill roots: a copy of the demo skills with investigate-performance allowed locally."""
    root = tmp_path / "skills"
    shutil.copytree(DEMO_SKILLS_ROOT, root)
    manifest = root / "investigate-performance" / "skill.manifest.json"
    data = json.loads(manifest.read_text("utf-8"))
    data["local_eligible"] = True
    manifest.write_text(json.dumps(data), "utf-8")
    return [root]


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


def clarify_turn(question: str = "Which service do you mean?", language: str = "en") -> list:
    """The routing generation calling request_clarification (no skill chosen)."""
    return [
        ToolCall(
            call_id="call_route",
            tool_name="request_clarification",
            arguments={"language": language, "question": question},
        ),
        Completed(finish_reason="tool_calls"),
    ]


def health_tail(service: str = "staging-api", answer: str = STAGING_OK) -> list:
    """A health check after routing: tool call, finalize proposal, final answer."""
    return [tool_turn("get_service_health", service=service), text_turn(answer), text_turn(answer)]
