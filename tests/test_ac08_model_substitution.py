"""AC08: the same orchestrator and skill packages across a model swap.

Spec section 13, AC08: switch to a second local model and a second
compatible external endpoint; the orchestrator and the skill packages
stay the same and only configuration changes. Requirement RM18, with
RM01, RM13, RM16, RM22 for what the traces must show. Issue #49;
section 12.3 names a working model substitution test a hard gate.

tests/test_model_substitution.py covers the adapter layer. This file
covers the other half: a managed scenario runs through the real
LocalOrchestrator, the real LlamaCppAdapter and ChatCompletionsAdapter,
and the checked-in skill packages. The scenario is a local skill and two
local tools, a local budget stop, and a handoff to the external model.
It runs against configuration A, then against configuration B (a second
local endpoint and artifact, a second external endpoint and model). The
runner below takes a configuration value and nothing else, and both
runs load the same skill root. Only the endpoints are scripted: they
replay the same model turns over httpx.MockTransport.

Scope: the second local artifact is a configuration value here, a
model identifier and an endpoint, not a qualified artifact. Only one
artifact has a manifest (spec section 6), so there is no live variant;
docs/TESTING.md records that.
"""

import asyncio
import json
import re
from dataclasses import dataclass

import httpx
import pytest

from gateway_helpers import (
    Gateway,
    build_gateway,
    local_eligible_skills,
    route_turn,
    text_turn,
    tool_turn,
)
from helpers import delta_chunk, of_type, sse_response
from relaymind.adapters import Completed, TextDelta, ToolCall
from relaymind.adapters.chat_completions import ChatCompletionsAdapter, ChatCompletionsConfig
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig
from relaymind.orchestrator.local_loop import ConfiguredModel, RunOutcome
from relaymind.orchestrator.run_state import RunState

PROPOSAL_ID = re.compile(r"proposal_[0-9a-f]{16}")
PROMPT = "Why is staging-api slow right now?"
ANSWER = (
    "Observations:\n"
    "- get_latest_deployment: the latest deployment is recorded.\n"
    "- get_metrics (60 minutes): p95 latency was 175 ms and 186 ms.\n"
    "- get_recent_logs: connection pool at 85% of capacity.\n"
    "\n"
    "Hypotheses:\n"
    "- The connection pool may be close to its limit.\n"
)

# What the models say. Both configurations replay exactly these turns.
LOCAL_TURNS = [
    route_turn("investigate-performance"),
    tool_turn("get_latest_deployment", call_id="c1", service="staging-api"),
    tool_turn("get_metrics", call_id="c2", service="staging-api", window_minutes=60),
    tool_turn("get_recent_logs", call_id="c3", service="staging-api", limit=3),
]
EXTERNAL_TURNS = [
    tool_turn("get_recent_logs", call_id="e1", service="staging-api", limit=3),
    text_turn(ANSWER),
]


@dataclass(frozen=True)
class Configuration:
    """Everything that differs between two runs: configuration values only."""

    name: str
    local: LlamaCppConfig
    local_model_id: str
    external: ChatCompletionsConfig

    @property
    def external_model_id(self) -> str:
        return self.external.model


CONFIG_A = Configuration(
    name="A",
    local=LlamaCppConfig(base_url="http://127.0.0.1:8033", context_limit=8192),
    local_model_id="local-artifact-one",
    external=ChatCompletionsConfig(
        base_url="https://api.provider-one.example/v1",
        model="provider-one-large",
        context_limit=128000,
        api_key_env="AC08_KEY_A",
    ),
)
CONFIG_B = Configuration(
    name="B",
    local=LlamaCppConfig(base_url="http://127.0.0.1:9044", context_limit=32768),
    local_model_id="local-artifact-two",
    external=ChatCompletionsConfig(
        base_url="https://api.provider-two.example/v1",
        model="provider-two-medium",
        context_limit=64000,
        api_key_env="AC08_KEY_B",
    ),
)


def chunks_for(events) -> list[dict]:
    """The wire form of one scripted model turn (adapter events to SSE chunks)."""
    chunks = []
    for event in events:
        if isinstance(event, TextDelta):
            chunks.append(delta_chunk({"content": event.text}))
        elif isinstance(event, ToolCall):
            call = {
                "index": 0,
                "id": event.call_id,
                "function": {
                    "name": event.tool_name,
                    "arguments": json.dumps(dict(event.arguments)),
                },
            }
            chunks.append(delta_chunk({"tool_calls": [call]}))
        elif isinstance(event, Completed):
            chunks.append(delta_chunk({}, finish_reason=event.finish_reason))
    return chunks


class Endpoint:
    """A scripted llama-server or Chat Completions endpoint.

    Replays its turns in order, one per generation, and records every
    request, so a test can see which endpoint and model identifier
    the orchestrator reached.
    """

    def __init__(self, turns):
        self.turns = [chunks_for(turn) for turn in turns]
        self.requests: list[httpx.Request] = []
        self.generations: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/apply-template":
            return httpx.Response(200, json={"prompt": "templated"})
        if path == "/tokenize":
            return httpx.Response(200, json={"tokens": list(range(100))})
        assert path.endswith("/chat/completions"), path
        self.generations.append(json.loads(request.content))
        assert self.turns, "unscripted generation"
        return sse_response(self.turns.pop(0))

    def hosts(self) -> set[tuple[str, int | None]]:
        return {(r.url.host, r.url.port) for r in self.requests}


@dataclass
class Observed:
    """One run of the scenario: the configuration it ran under and what it left behind."""

    config: Configuration
    outcome: RunOutcome
    events: list
    operations: list
    local: Endpoint
    external: Endpoint
    gateway: Gateway


def run_scenario(tmp_path, skill_roots, config: Configuration, local_turns=LOCAL_TURNS) -> Observed:
    """Run the managed scenario under one configuration.

    The orchestrator is built the same way for every configuration: the
    only inputs that change are the adapter configs and model identifiers.
    """
    local, external = Endpoint(local_turns), Endpoint(EXTERNAL_TURNS)
    local_adapter = LlamaCppAdapter(config.local, transport=httpx.MockTransport(local))
    external_adapter = ChatCompletionsAdapter(
        config.external, transport=httpx.MockTransport(external)
    )
    work = tmp_path / config.name
    work.mkdir()
    gateway = build_gateway(
        work,
        [],
        skill_roots=skill_roots,
        local_model=ConfiguredModel(adapter=local_adapter, model_id=config.local_model_id),
        external_model=ConfiguredModel(adapter=external_adapter, model_id=config.external_model_id),
    )

    async def go():
        async with local_adapter, external_adapter:
            return await gateway.orchestrator.run_text(PROMPT)

    outcome = asyncio.run(go())
    return Observed(
        config,
        outcome,
        gateway.store.events(outcome.run_id),
        gateway.store.tool_operations(outcome.run_id),
        local,
        external,
        gateway,
    )


def model_names(value) -> set[str]:
    """Every string found under a "model" or "final_answer_model" key, at any depth."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("model", "final_answer_model") and isinstance(item, str):
                found.add(item)
            found |= model_names(item)
    elif isinstance(value, list):
        for item in value:
            found |= model_names(item)
    return found


def renamer(observed: Observed):
    """A function that rewrites the values that differ per run into names.

    Renamed: the model identifiers (to local-model and external-model),
    the run id, the operation IDs, and the proposal IDs (random per
    run, so numbered in order of first appearance).
    """
    names = {
        observed.config.local_model_id: "local-model",
        observed.config.external_model_id: "external-model",
        observed.outcome.run_id: "run",
    }
    for index, operation in enumerate(observed.operations):
        names[operation["operation_id"]] = f"op{index}"
    proposals: dict[str, str] = {}

    def proposal_name(proposal_id: str) -> str:
        return proposals.setdefault(proposal_id, f"proposal{len(proposals)}")

    def rename(value):
        if isinstance(value, str):
            for old, new in names.items():
                value = value.replace(old, new)
            return PROPOSAL_ID.sub(lambda m: proposal_name(m.group()), value)
        if isinstance(value, dict):
            return {key: rename(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rename(item) for item in value]
        return value

    return rename


def normalized(observed: Observed) -> list[dict]:
    """The trace without the values that are configuration or per-run.

    Dropped: run id and wall-clock timestamps. One more field is
    configuration by definition and is checked against the
    configuration, then dropped: the external context limit the
    handoff was measured against. What remains is routing, tool
    proposals, arguments, results, handoff content, and the order of
    events.
    """
    rename = renamer(observed)
    limit = observed.config.external.context_limit
    out = []
    for event in observed.events:
        record = event.to_json_object()
        for key in ("run_id", "timestamp"):
            record.pop(key)
        if record["event_type"] == "handoff.started":
            assert record["payload"]["request"].pop("limit") == limit
        out.append(rename(record))
    return out


def external_requests(observed: Observed) -> list[dict]:
    """What the external model received, as sent on the wire, with the same renames."""
    rename = renamer(observed)
    return [
        {"messages": rename(body["messages"]), "tools": body["tools"]}
        for body in observed.external.generations
    ]


@pytest.fixture
def skill_roots(tmp_path):
    """One skill root, used unchanged by both runs: investigate-performance
    is allowed to run locally first, as in the AC02 scenario."""
    return local_eligible_skills(tmp_path)


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("AC08_KEY_A", "sk-a")
    monkeypatch.setenv("AC08_KEY_B", "sk-b")


@pytest.fixture
def swapped(tmp_path, skill_roots) -> tuple[Observed, Observed]:
    """The scenario under configuration A, then under configuration B."""
    first = run_scenario(tmp_path, skill_roots, CONFIG_A)
    second = run_scenario(tmp_path, skill_roots, CONFIG_B)
    return first, second


def test_the_scenario_completes_through_a_handoff_under_each_configuration(swapped):
    """AC08, RM18: the managed scenario works under A and under B."""
    for observed in swapped:
        assert observed.outcome.state is RunState.COMPLETED, observed.config.name
        assert observed.outcome.answer == ANSWER
        assert [op["tool_name"] for op in observed.operations] == [
            "get_latest_deployment",
            "get_metrics",
            "get_recent_logs",
        ]
        (handoff,) = of_type(observed.events, "handoff.started")
        assert handoff.payload["reason_code"] == "local_budget"
        # Every scripted turn was used, on the endpoint of this configuration.
        assert observed.local.turns == []
        assert observed.external.turns == []


def test_the_swap_reaches_the_swapped_endpoints(swapped):
    """AC08, RM01, RM18: each run talks to its own endpoints, local ones only on loopback."""
    first, second = swapped

    assert first.local.hosts() == {("127.0.0.1", 8033)}
    assert second.local.hosts() == {("127.0.0.1", 9044)}
    assert first.external.hosts() == {("api.provider-one.example", None)}
    assert second.external.hosts() == {("api.provider-two.example", None)}
    for observed, key in ((first, "sk-a"), (second, "sk-b")):
        # Local generations carry no model identifier and no credential.
        assert len(observed.local.generations) == 4
        assert all("model" not in body for body in observed.local.generations)
        assert all(r.headers.get("authorization") is None for r in observed.local.requests)
        # External generations name the configured model and use its credential.
        assert len(observed.external.generations) == 2
        assert {body["model"] for body in observed.external.generations} == {
            observed.config.external_model_id
        }
        assert {r.headers["authorization"] for r in observed.external.requests} == {f"Bearer {key}"}


def test_both_runs_use_the_same_skill_packages(swapped, skill_roots):
    """AC08, RM07, RM18: one skill root, the same versions and content hashes."""
    first, second = swapped

    def offered(observed):
        (accepted,) = of_type(observed.events, "run.accepted")
        return accepted.payload["skills_offered"], accepted.payload["skill_roots"]

    assert offered(first) == offered(second)
    assert [str(root) for root in skill_roots] == offered(first)[1]
    for name in ("investigate-performance", "check-service-health"):
        assert first.gateway.skills.get(name).content_hash == (
            second.gateway.skills.get(name).content_hash
        )


def test_the_traces_name_the_actual_models(swapped):
    """AC08, RM16, RM22: run A names A's models only, run B names B's models only."""
    for observed in swapped:
        config = observed.config
        payloads = [event.to_json_object()["payload"] for event in observed.events]
        assert model_names(payloads) == {config.local_model_id, config.external_model_id}
        # The local generations are attributed to the local model and the
        # external ones to the external model, by actor.
        started = of_type(observed.events, "model.started")
        assert {(e.actor, e.payload["model"]) for e in started} == {
            ("local", config.local_model_id),
            ("external", config.external_model_id),
        }
        (completed,) = of_type(observed.events, "run.completed")
        assert completed.payload["final_answer_model"] == config.external_model_id


def test_routing_tool_proposals_and_handoff_are_the_same_across_the_swap(swapped):
    """AC08, RM13, RM18: same routing, proposals, and handoff content in both traces."""
    first, second = swapped

    trace_a = normalized(first)
    trace_b = normalized(second)

    assert trace_a == trace_b
    # The comparison is not vacuous: it covers the facts AC08 names.
    types = [record["event_type"] for record in trace_a]
    for needed in ("route.selected", "tool.proposed", "handoff.started", "run.completed"):
        assert needed in types
    # The handoff content the external model received is identical too:
    # the same task, evidence, proposals, and tool schemas on the wire.
    sent_a = external_requests(first)
    sent_b = external_requests(second)
    assert sent_a == sent_b
    assert "Handoff (JSON):" in sent_a[0]["messages"][0]["content"]


def test_the_comparison_notices_a_difference_in_tool_proposals(tmp_path, skill_roots):
    """AC08: control. A local model that proposes other arguments changes the trace."""
    different = [
        *LOCAL_TURNS[:2],
        tool_turn("get_metrics", call_id="c2", service="staging-api", window_minutes=30),
        LOCAL_TURNS[3],
    ]
    first = run_scenario(tmp_path, skill_roots, CONFIG_A)
    second = run_scenario(tmp_path, skill_roots, CONFIG_B, local_turns=different)

    assert normalized(first) != normalized(second)
