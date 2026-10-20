"""Model substitution test (AC08, RM18, spec section 12.3 gate).

Switching to a second qualified local model or a second compatible
external endpoint needs configuration changes only. The scenario
runner below takes the ModelAdapter interface and nothing else — no
branch in it knows which provider, endpoint, or model is behind the
adapter — so rerunning it against a swapped configuration IS the
evidence that no routing-code change is needed. Issue #5.
"""

import json

import httpx

from helpers import HEALTH_TOOL, collect_events, delta_chunk, make_request, sse_body
from relaymind.adapters import Completed, Message, ModelAdapter, Role, ToolCall
from relaymind.adapters.chat_completions import (
    ChatCompletionsAdapter,
    ChatCompletionsConfig,
)
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig

# The one scenario, reused verbatim across every configuration: a
# health question that the model must answer with a tool proposal.
SCENARIO_REQUEST = make_request(
    messages=(Message(role=Role.USER, content="Is staging-api healthy right now?"),),
    tool_schemas=(HEALTH_TOOL,),
)

EXPECTED_EVENTS = [
    ToolCall(
        call_id="call_1",
        tool_name="get_service_health",
        arguments={"service": "staging-api"},
    ),
    Completed(finish_reason="tool_calls"),
]


def run_scenario(adapter: ModelAdapter) -> list:
    """Drive one adapter through the scenario.

    Takes the adapter contract only; it must never need to know what
    is behind it (AC08: same orchestration code for every supported
    configuration).
    """
    return collect_events(adapter, SCENARIO_REQUEST)


def scripted_endpoint(served: dict):
    """A mock endpoint that replays the scenario's tool-call stream.

    Records which host was called and with which model identifier, so
    a test can prove the swap reached the swapped configuration.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        served["host"] = request.url.host
        served["port"] = request.url.port
        served["model"] = json.loads(request.content).get("model")
        chunks = [
            delta_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {
                                "name": "get_service_health",
                                "arguments": '{"service": "staging-api"}',
                            },
                        }
                    ]
                }
            ),
            delta_chunk({}, finish_reason="tool_calls"),
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body(chunks),
        )

    return handler


def test_local_model_swap_is_configuration_only():
    # First qualified local model, then a second one on its own
    # llama-server endpoint: only LlamaCppConfig values change.
    results = {}
    for name, config in {
        "first": LlamaCppConfig(base_url="http://127.0.0.1:8033", context_limit=8192),
        "second": LlamaCppConfig(base_url="http://127.0.0.1:9044", context_limit=32768),
    }.items():
        served: dict = {}
        adapter = LlamaCppAdapter(config, transport=httpx.MockTransport(scripted_endpoint(served)))
        events = run_scenario(adapter)
        results[name] = (served, events)

    assert results["first"][0]["port"] == 8033
    assert results["second"][0]["port"] == 9044
    # Same scenario, same normalized outcome, from both configurations.
    assert results["first"][1] == EXPECTED_EVENTS
    assert results["second"][1] == EXPECTED_EVENTS


def test_external_endpoint_swap_is_configuration_only(monkeypatch):
    # First compatible external endpoint, then a second provider with
    # its own model identifier and credential variable: only
    # ChatCompletionsConfig values change.
    monkeypatch.setenv("SUBST_KEY_A", "sk-a")
    monkeypatch.setenv("SUBST_KEY_B", "sk-b")
    results = {}
    for name, config in {
        "first": ChatCompletionsConfig(
            base_url="https://api.provider-one.example/v1",
            model="provider-one-large",
            context_limit=128000,
            api_key_env="SUBST_KEY_A",
        ),
        "second": ChatCompletionsConfig(
            base_url="https://api.provider-two.example/v1",
            model="provider-two-medium",
            context_limit=64000,
            api_key_env="SUBST_KEY_B",
        ),
    }.items():
        served: dict = {}
        adapter = ChatCompletionsAdapter(
            config, transport=httpx.MockTransport(scripted_endpoint(served))
        )
        events = run_scenario(adapter)
        results[name] = (served, events)

    assert results["first"][0]["host"] == "api.provider-one.example"
    assert results["first"][0]["model"] == "provider-one-large"
    assert results["second"][0]["host"] == "api.provider-two.example"
    assert results["second"][0]["model"] == "provider-two-medium"
    assert results["first"][1] == EXPECTED_EVENTS
    assert results["second"][1] == EXPECTED_EVENTS


def test_local_to_external_swap_keeps_the_same_scenario_code(monkeypatch):
    # RM18: swapping between two supported adapters is a configuration
    # and construction concern; the scenario runner never changes.
    monkeypatch.setenv("SUBST_KEY_A", "sk-a")
    local_served: dict = {}
    external_served: dict = {}
    local = LlamaCppAdapter(
        LlamaCppConfig(base_url="http://127.0.0.1:8033", context_limit=8192),
        transport=httpx.MockTransport(scripted_endpoint(local_served)),
    )
    external = ChatCompletionsAdapter(
        ChatCompletionsConfig(
            base_url="https://api.provider-one.example/v1",
            model="provider-one-large",
            context_limit=128000,
            api_key_env="SUBST_KEY_A",
        ),
        transport=httpx.MockTransport(scripted_endpoint(external_served)),
    )
    assert run_scenario(local) == EXPECTED_EVENTS
    assert run_scenario(external) == EXPECTED_EVENTS
    assert local_served["port"] == 8033
    assert external_served["host"] == "api.provider-one.example"
