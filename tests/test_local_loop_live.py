"""Live check-service-health trace through the bounded local loop (AC01).

Spec section 13: model doubles verify orchestration; real models verify
capability separately, and passing doubles alone does not satisfy the
live-demo requirements. Section 12.3 hard gate: live local
skill-and-tool traces in both languages. This runs the real router,
skill loop, executor, and sandbox against the pinned Qwen3-0.6B on
llama-server for an English and a Chinese health request, and checks
AC01: local skill selection, a real sandbox tool call, the correct final
status (the completion predicate), and zero external calls in the trace
(RM01, RM02, RM03, RM06, RM16, RM23). Issues #14 and #15.

Run explicitly with: pytest -m live tests/test_local_loop_live.py
Set RELAYMIND_TRACE_OUT=<file.json> to write the full traces, the
warm-up run included, for docs/EVIDENCE.md. Nothing is written
otherwise.
"""

import asyncio
import json
import os
import platform
import subprocess
from pathlib import Path

import pytest

from helpers import BASELINE, ScriptedAdapter, make_demo_skills
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig
from relaymind.orchestrator.local_loop import ConfiguredModel, LocalOrchestrator
from relaymind.orchestrator.run_state import RunState
from relaymind.skills.predicates import health_status_preserved
from relaymind.store import RunStore
from relaymind.tools import CallerContext, OperatorPolicy, ToolRegistry, ToolResult, ToolStatus
from relaymind.tools.sandbox import (
    DEMO_TOOL_NAMES,
    READ_PERMISSION,
    SandboxDataService,
    register_demo_tools,
)
from test_llamacpp_live_smoke import (  # noqa: F401 - shared live fixtures
    adapter_settings,
    manifest,
    require_llama_server,
    server,
)

pytestmark = pytest.mark.live

TRACE_ENV = "RELAYMIND_TRACE_OUT"
# The paired requests of skills/check-service-health/examples.json, both
# services in both languages; none is chosen for the model's benefit.
PROMPTS = {
    "en-staging": ("Is staging-api healthy right now?", "en", "staging-api"),
    "zh-staging": ("staging-api 现在健康吗？", "zh", "staging-api"),
    "en-payments": ("Check the health of payments-api.", "en", "payments-api"),
    "zh-payments": ("帮我检查一下 payments-api 的健康状态。", "zh", "payments-api"),
}
# Warm up with the first prompt, as the M1 and M2 probes do.
WARMUP_PROMPT = PROMPTS["en-staging"][0]

_records: list[dict] = []


@pytest.fixture(scope="module")
def gateway(server, manifest, adapter_settings, tmp_path_factory):  # noqa: F811
    ctx_size, template_kwargs = adapter_settings
    work = tmp_path_factory.mktemp("live-loop")
    tools = ToolRegistry()
    register_demo_tools(tools, SandboxDataService(BASELINE, work_dir=work / "sandbox", reset=True))
    skills = make_demo_skills(tools)
    store = RunStore(work / "runs.sqlite3")
    adapter = LlamaCppAdapter(
        LlamaCppConfig(
            base_url=server, context_limit=ctx_size, chat_template_kwargs=template_kwargs
        )
    )
    model_id = manifest.file_name
    # A recording external double that is not configured on the gateway:
    # with an external model configured, an escalation would dispatch to it
    # (RM13), and these live runs measure the local path. The trace count
    # of external calls is what proves RM01 here.
    external = ScriptedAdapter(turns=[], locality="external")
    orchestrator = LocalOrchestrator(
        ConfiguredModel(adapter=adapter, model_id=model_id),
        skills=skills,
        tools=tools,
        store=store,
        policy=OperatorPolicy(enabled_tools=DEMO_TOOL_NAMES),
        caller=CallerContext(permissions=frozenset({READ_PERMISSION})),
    )
    loop = asyncio.new_event_loop()
    # One warm-up run so the first measured request does not carry the
    # first-request cost seen in the M1 and M2 probes. It is recorded,
    # never graded.
    warmup = loop.run_until_complete(orchestrator.run_text(WARMUP_PROMPT))
    _record(store, warmup, WARMUP_PROMPT, warmup=True)
    yield orchestrator, store, loop, model_id, external
    loop.run_until_complete(adapter.aclose())
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()
    _write_export(manifest)
    store.close()


def _record(store, outcome, prompt, *, warmup):
    _records.append(
        {
            "prompt": prompt,
            "warmup": warmup,
            "outcome": {
                "run_id": outcome.run_id,
                "state": outcome.state.value,
                "answer": outcome.answer,
                "error_code": outcome.error_code,
                "reason_code": outcome.reason_code,
            },
            "events": [event.to_json_object() for event in store.events(outcome.run_id)],
        }
    )


def _runtime_banner() -> str | None:
    try:
        result = subprocess.run(
            ["llama-server", "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout + result.stderr).strip() or None


def _write_export(artifact) -> None:
    target = os.environ.get(TRACE_ENV)
    if not target:
        return
    export = {
        "kind": "relaymind.live_local_trace",
        "note": "Live single-pass traces; observations, not benchmark results (spec 12.3).",
        "command": f"{TRACE_ENV}={target} pytest -m live tests/test_local_loop_live.py",
        "machine": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "runtime_banner": _runtime_banner(),
        "model_file": artifact.file_name,
        "runs": _records,
    }
    # Skill roots are recorded as absolute paths; keep the checkout's own
    # location out of a file that is committed.
    text = json.dumps(export, indent=2, ensure_ascii=False).replace(
        str(Path(__file__).resolve().parent.parent), "<repo>"
    )
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


@pytest.mark.parametrize("case", PROMPTS)
def test_check_service_health_completes_locally(gateway, case):
    orchestrator, store, loop, model_id, external = gateway
    prompt, language, service = PROMPTS[case]
    outcome = loop.run_until_complete(orchestrator.run_text(prompt))
    _record(store, outcome, prompt, warmup=False)
    events = store.events(outcome.run_id)
    by_type = {}
    for event in events:
        by_type.setdefault(event.event_type.value, []).append(event)

    failure = by_type.get("run.failed", [None])[0]
    assert outcome.state is RunState.COMPLETED, (
        f"run did not complete: {failure and failure.payload}"
    )

    # Local skill selection (RM02).
    (selected,) = by_type["route.selected"]
    assert selected.payload["decision"]["route"] == "local"
    assert selected.payload["decision"]["skill_id"] == "check-service-health"
    assert selected.payload["decision"]["language"] == language

    # A real sandbox tool call (RM23).
    finished = by_type["tool.finished"]
    results = [ToolResult.from_json_object(e.payload["result"]) for e in finished]
    assert results[-1].status is ToolStatus.SUCCEEDED
    assert results[-1].data["service"] == service

    # The correct final status (RM16): the registered predicate holds.
    assert health_status_preserved(outcome.answer, results)

    # Zero external calls (RM01): the gateway has no external model here,
    # so the trace is the proof: every call is labelled with the adapter's
    # locality and run.completed counts no external call.
    assert external.requests == []
    started = by_type["model.started"]
    assert all(e.actor == "local" and e.payload["model"] == model_id for e in started)
    (completed,) = by_type["run.completed"]
    assert completed.payload["external_calls"] == len(external.requests)
    assert completed.payload["final_answer_model"] == model_id
    assert len(completed.payload["model_calls"]) == len(started)
