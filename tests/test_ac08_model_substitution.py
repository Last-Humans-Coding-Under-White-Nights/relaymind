"""AC08: the same orchestrator and skill packages across a model swap.

Spec section 13, AC08: switch to a second local model and a second
compatible external endpoint; the orchestrator and the skill packages
stay the same and only configuration changes. Requirement RM18, with
RM01, RM13, RM16, RM22 for what the traces must show. Issue #49;
section 12.3 names a working model substitution test a hard gate.

tests/test_model_substitution.py covers the adapter layer. This file
covers the other half: a managed scenario runs through the real
LocalOrchestrator, the real LlamaCppAdapter and ChatCompletionsAdapter,
and one copy of the checked-in skill packages in which only
investigate-performance may run locally first, as in the AC02 scenario.
The scenario is a local skill and two local tools, a local budget stop,
and a handoff to the external model.
It runs against configuration A, then against configuration B (a second
local endpoint and artifact, a second external endpoint and model). The
runner below takes a configuration value and nothing else, and both
runs load the same skill root. Only the endpoints are scripted: they
replay the same model turns over httpx.MockTransport.

Scope: the second local artifact is a configuration value here, a
model identifier and an endpoint. The live local swap to the second
qualified artifact (models/qwen3-0.6b-q4_k_m.manifest.json) runs the
live suite with RELAYMIND_LIVE_MANIFEST (issue #90); docs/TESTING.md
and docs/EVIDENCE.md record it.
"""

import asyncio
import dataclasses
import json
import re
from dataclasses import dataclass

import httpx
import pytest

from gateway_helpers import (
    Gateway,
    assert_needs_external,
    build_gateway,
    local_eligible_skills,
    route_turn,
    text_turn,
    tool_turn,
)
from helpers import DEMO_SKILLS_ROOT, delta_chunk, make_demo_skills, of_type, sse_response
from relaymind.adapters import Completed, TextDelta, ToolCall, Usage
from relaymind.adapters.chat_completions import ChatCompletionsAdapter, ChatCompletionsConfig
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig
from relaymind.adapters.openai_compat import StreamAccumulator
from relaymind.orchestrator.local_loop import ConfiguredModel, RunOutcome
from relaymind.orchestrator.run_state import RunState

PROPOSAL_ID = re.compile(r"proposal_[0-9a-f]{16}")
# The trace fields that hold a model identifier; run.accepted names the local one (RM26).
MODEL_FIELDS = ("model", "final_answer_model", "local_model")
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
    """The wire form of one scripted model turn (adapter events to SSE chunks).

    Each tool call has its own index, so a turn with several calls stays
    several calls. Usage goes in its own chunk after the finish chunk,
    where a stream with include_usage puts it.
    """
    chunks, usage = [], []
    calls = 0
    for event in events:
        if isinstance(event, TextDelta):
            chunks.append(delta_chunk({"content": event.text}))
        elif isinstance(event, ToolCall):
            call = {
                "index": calls,
                "id": event.call_id,
                "function": {
                    "name": event.tool_name,
                    "arguments": json.dumps(dict(event.arguments)),
                },
            }
            calls += 1
            chunks.append(delta_chunk({"tool_calls": [call]}))
        elif isinstance(event, Usage):
            usage.append(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": event.input_tokens,
                        "completion_tokens": event.output_tokens,
                    },
                }
            )
        elif isinstance(event, Completed):
            chunks.append(delta_chunk({}, finish_reason=event.finish_reason))
        else:
            raise AssertionError(f"no wire form for {event!r}")
    return chunks + usage


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
        None,
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
    """Every string found under a model field (MODEL_FIELDS), at any depth."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in MODEL_FIELDS and isinstance(item, str):
                found.add(item)
            found |= model_names(item)
    elif isinstance(value, list):
        for item in value:
            found |= model_names(item)
    return found


def renamer(observed: Observed):
    """A function that rewrites the values that differ per run into names.

    Renamed: the model identifiers (to local-model and external-model),
    only as the whole value of a model field, so a model identifier that
    is also ordinary text (a service name, a word of the answer) never
    rewrites that text. The run id, the operation IDs, and the proposal
    IDs are random tokens that other values embed (call ids, source
    references, the handoff JSON), so they are renamed inside any
    string; proposal IDs are numbered in order of first appearance.
    """
    models = {
        observed.config.local_model_id: "local-model",
        observed.config.external_model_id: "external-model",
    }
    tokens = {observed.outcome.run_id: "run"}
    for index, operation in enumerate(observed.operations):
        tokens[operation["operation_id"]] = f"op{index}"
    proposals: dict[str, str] = {}

    def proposal_name(proposal_id: str) -> str:
        return proposals.setdefault(proposal_id, f"proposal{len(proposals)}")

    def rename(value, key=None):
        if isinstance(value, str):
            if key in MODEL_FIELDS:
                return models.get(value, value)
            for old, new in tokens.items():
                value = value.replace(old, new)
            return PROPOSAL_ID.sub(lambda m: proposal_name(m.group()), value)
        if isinstance(value, dict):
            return {name: rename(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [rename(item, key) for item in value]
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
    """AC08, RM07, RM18: one skill root, the same versions and content hashes,
    and those are the checked-in packages but for one recorded change."""
    first, second = swapped

    def offered(observed):
        (accepted,) = of_type(observed.events, "run.accepted")
        return accepted.payload["skills_offered"], accepted.payload["skill_roots"]

    assert offered(first) == offered(second)
    assert [str(root) for root in skill_roots] == offered(first)[1]
    # The hashes the traces record are those of the checked-in packages,
    # except investigate-performance, the one package the scenario changes.
    checked_in = make_demo_skills(first.gateway.tools)
    recorded = {skill["name"]: skill for skill in offered(first)[0]}
    assert "check-service-health" in recorded
    for name, skill in recorded.items():
        package = checked_in.get(name)
        assert skill["version"] == package.version, name
        if name != CHANGED_SKILL:
            assert skill["content_hash"] == package.content_hash, name
    assert recorded[CHANGED_SKILL]["content_hash"] != checked_in.get(CHANGED_SKILL).content_hash
    assert changed_files(skill_roots[0] / CHANGED_SKILL, DEMO_SKILLS_ROOT / CHANGED_SKILL) == {
        "skill.manifest.json": {"local_eligible"}
    }


CHANGED_SKILL = "investigate-performance"


def changed_files(copy, original) -> dict[str, set[str]]:
    """The files that differ between two package directories; for a JSON
    manifest, the top-level fields that differ."""
    names = {p.relative_to(copy).as_posix() for p in copy.rglob("*") if p.is_file()}
    assert names == {p.relative_to(original).as_posix() for p in original.rglob("*") if p.is_file()}
    changed: dict[str, set[str]] = {}
    for name in sorted(names):
        new, old = (copy / name).read_bytes(), (original / name).read_bytes()
        if new == old:
            continue
        if name.endswith(".json"):
            a, b = json.loads(new), json.loads(old)
            changed[name] = {k for k in a.keys() | b.keys() if a.get(k) != b.get(k)}
        else:
            changed[name] = set()
    return changed


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
    """AC08: control. A local model that proposes other arguments changes the
    trace, and the first difference is that tool proposal."""
    different = [
        *LOCAL_TURNS[:2],
        tool_turn("get_metrics", call_id="c2", service="staging-api", window_minutes=30),
        LOCAL_TURNS[3],
    ]
    first = run_scenario(tmp_path, skill_roots, CONFIG_A)
    second = run_scenario(tmp_path, skill_roots, CONFIG_B, local_turns=different)

    trace_a, trace_b = normalized(first), normalized(second)
    first_difference = next(
        index for index, (a, b) in enumerate(zip(trace_a, trace_b, strict=False)) if a != b
    )
    finished_a, proposed_a = trace_a[first_difference : first_difference + 2]
    finished_b, proposed_b = trace_b[first_difference : first_difference + 2]
    assert finished_a["event_type"] == finished_b["event_type"] == "model.finished"
    assert proposed_a["event_type"] == proposed_b["event_type"] == "tool.proposed"
    for finished, proposed, window in ((finished_a, proposed_a, 60), (finished_b, proposed_b, 30)):
        arguments = {"service": "staging-api", "window_minutes": window}
        assert finished["payload"]["proposed_calls"] == [
            {"tool_name": "get_metrics", "arguments": arguments}
        ]
        assert proposed["payload"]["arguments"] == arguments


def test_routing_generation_reports_the_scripted_usage(swapped):
    """AC08, RM03, RM25: the usage each endpoint streams reaches the trace
    under both configurations, so token accounting is part of the swap."""
    for observed in swapped:
        routing = [
            e
            for e in of_type(observed.events, "model.finished")
            if e.payload["purpose"] == "routing"
        ]
        assert [e.payload["usage"] for e in routing] == [
            {
                "input_tokens": 800,
                "output_tokens": 12,
                # The double reports no counts: absent, never a defaulted 0 (RM16).
                "cached_tokens": None,
                "reasoning_tokens": None,
            }
        ], observed.config.name


def test_a_model_identifier_that_is_also_trace_text_is_renamed_only_as_a_model(
    tmp_path, skill_roots
):
    """AC08: the comparison renames a model identifier only where the trace
    names a model, so an identifier equal to a service name in tool
    arguments and results leaves them alone."""
    clashing = dataclasses.replace(CONFIG_B, local_model_id="staging-api")
    first = run_scenario(tmp_path, skill_roots, CONFIG_A)
    second = run_scenario(tmp_path, skill_roots, clashing)

    assert normalized(first) == normalized(second)
    assert external_requests(first) == external_requests(second)


def test_a_turn_with_two_tool_calls_stays_two_calls_on_the_wire():
    """AC08, RM09: the scripted wire form keeps each tool call separate."""
    turn = [
        ToolCall(call_id="p1", tool_name="get_metrics", arguments={"service": "staging-api"}),
        ToolCall(call_id="p2", tool_name="get_recent_logs", arguments={"service": "staging-api"}),
        Usage(input_tokens=5, output_tokens=2),
        Completed(finish_reason="tool_calls"),
    ]
    accumulator = StreamAccumulator()
    events = [e for chunk in chunks_for(turn) for e in accumulator.feed(chunk)]
    events += accumulator.flush()

    assert events == [turn[0], turn[1], turn[2], turn[3]]


def test_build_gateway_rejects_a_double_option_beside_a_configured_model(tmp_path):
    """A configured model replaces the double; scripting the double too is an error."""
    adapter = ChatCompletionsAdapter(CONFIG_A.external)
    external = ConfiguredModel(adapter=adapter, model_id=CONFIG_A.external_model_id)
    with pytest.raises(ValueError, match="external_turns"):
        build_gateway(tmp_path, [], external_model=external, external_turns=[text_turn("x")])
    local = ConfiguredModel(adapter=LlamaCppAdapter(CONFIG_A.local), model_id="m")
    with pytest.raises(ValueError, match="turns"):
        build_gateway(tmp_path, [route_turn("check-service-health")], local_model=local)


def test_assert_needs_external_explains_a_configured_external_adapter(tmp_path):
    """assert_needs_external measures the double; with a real adapter it says so."""
    adapter = ChatCompletionsAdapter(CONFIG_A.external)
    gateway = build_gateway(
        tmp_path,
        [],
        external_model=ConfiguredModel(adapter=adapter, model_id=CONFIG_A.external_model_id),
    )
    assert gateway.external is adapter
    with pytest.raises(AssertionError, match="configured external adapter"):
        assert_needs_external(gateway, None, [], "local_budget")
