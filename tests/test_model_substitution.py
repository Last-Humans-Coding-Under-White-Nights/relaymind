"""Model substitution test, adapter layer (RM18, part of AC08).

Switching to a second qualified local model or a second compatible
external endpoint needs configuration changes only. The scenario
runner below takes the ModelAdapter interface and nothing else — no
branch in it knows which provider, endpoint, or model is behind the
adapter — so rerunning it against a swapped configuration is the
evidence that the adapter boundary absorbs the swap.

Scope: this covers the adapter layer only. AC08 also requires the same
orchestrator and skill packages to run unchanged across the swap; that
half needs the orchestrator and skill registry, which do not exist
yet, and is tracked in issue #5.
"""

import asyncio
import json

import httpx

from helpers import HEALTH_TOOL, collect_events, delta_chunk, make_request, sse_response
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

SCENARIO_CHUNKS = [
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

# The configurations under test. Each pair differs in configuration
# values only.
LOCAL_ONE = LlamaCppConfig(base_url="http://127.0.0.1:8033", context_limit=8192)
LOCAL_TWO = LlamaCppConfig(base_url="http://127.0.0.1:9044", context_limit=32768)
PROVIDER_ONE = ChatCompletionsConfig(
    base_url="https://api.provider-one.example/v1",
    model="provider-one-large",
    context_limit=128000,
    api_key_env="SUBST_KEY_A",
)
PROVIDER_TWO = ChatCompletionsConfig(
    base_url="https://api.provider-two.example/v1",
    model="provider-two-medium",
    context_limit=64000,
    api_key_env="SUBST_KEY_B",
)


def run_scenario(adapter: ModelAdapter) -> list:
    """Drive one adapter through the scenario.

    Takes the adapter contract only; it must never need to know what
    is behind it (RM18: the caller is the same code for every supported
    configuration).
    """
    return collect_events(adapter, SCENARIO_REQUEST)


def context_limit(adapter: ModelAdapter) -> int:
    return asyncio.run(adapter.capabilities()).context_limit


def scripted_endpoint(served: list[dict]):
    """A mock endpoint that replays the scenario's tool-call stream.

    Appends one record per request (host, port, path, model identifier,
    Authorization header), so a test can prove the swap reached the
    swapped configuration and that exactly one call was made.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        served.append(
            {
                "host": request.url.host,
                "port": request.url.port,
                "path": request.url.path,
                "model": json.loads(request.content).get("model"),
                "authorization": request.headers.get("authorization"),
            }
        )
        return sse_response(SCENARIO_CHUNKS)

    return handler


def test_local_model_swap_is_configuration_only():
    # First qualified local model, then a second one on its own
    # llama-server endpoint: only LlamaCppConfig values change.
    first_served: list[dict] = []
    first = LlamaCppAdapter(
        LOCAL_ONE, transport=httpx.MockTransport(scripted_endpoint(first_served))
    )
    second_served: list[dict] = []
    second = LlamaCppAdapter(
        LOCAL_TWO, transport=httpx.MockTransport(scripted_endpoint(second_served))
    )

    assert context_limit(first) == 8192
    assert context_limit(second) == 32768
    # Same scenario, same normalized outcome, from both configurations.
    assert run_scenario(first) == EXPECTED_EVENTS
    assert run_scenario(second) == EXPECTED_EVENTS
    assert len(first_served) == 1
    assert len(second_served) == 1
    assert first_served[0]["port"] == 8033
    assert second_served[0]["port"] == 9044
    assert first_served[0]["path"] == "/v1/chat/completions"
    assert second_served[0]["path"] == "/v1/chat/completions"


def test_external_endpoint_swap_is_configuration_only(monkeypatch):
    # First compatible external endpoint, then a second provider with
    # its own model identifier and credential variable: only
    # ChatCompletionsConfig values change.
    monkeypatch.setenv("SUBST_KEY_A", "sk-a")
    monkeypatch.setenv("SUBST_KEY_B", "sk-b")
    first_served: list[dict] = []
    first = ChatCompletionsAdapter(
        PROVIDER_ONE, transport=httpx.MockTransport(scripted_endpoint(first_served))
    )
    second_served: list[dict] = []
    second = ChatCompletionsAdapter(
        PROVIDER_TWO, transport=httpx.MockTransport(scripted_endpoint(second_served))
    )

    assert context_limit(first) == 128000
    assert context_limit(second) == 64000
    assert run_scenario(first) == EXPECTED_EVENTS
    assert run_scenario(second) == EXPECTED_EVENTS
    assert len(first_served) == 1
    assert len(second_served) == 1
    assert first_served[0]["host"] == "api.provider-one.example"
    assert first_served[0]["path"] == "/v1/chat/completions"
    assert first_served[0]["model"] == "provider-one-large"
    assert first_served[0]["authorization"] == "Bearer sk-a"
    assert second_served[0]["host"] == "api.provider-two.example"
    assert second_served[0]["path"] == "/v1/chat/completions"
    assert second_served[0]["model"] == "provider-two-medium"
    assert second_served[0]["authorization"] == "Bearer sk-b"


def test_local_to_external_swap_keeps_the_same_scenario_code(monkeypatch):
    # RM18: swapping between two supported adapters is a configuration
    # and construction concern; the scenario runner never changes. What
    # is specific to crossing the boundary: the local request stays on
    # loopback with no model identifier and no credential, even while an
    # external credential is configured in the environment, and the
    # external request carries both.
    monkeypatch.setenv("SUBST_KEY_A", "sk-a")
    local_served: list[dict] = []
    external_served: list[dict] = []
    local = LlamaCppAdapter(
        LOCAL_ONE, transport=httpx.MockTransport(scripted_endpoint(local_served))
    )
    external = ChatCompletionsAdapter(
        PROVIDER_ONE, transport=httpx.MockTransport(scripted_endpoint(external_served))
    )

    assert run_scenario(local) == EXPECTED_EVENTS
    assert run_scenario(external) == EXPECTED_EVENTS
    assert len(local_served) == 1
    assert len(external_served) == 1
    assert local_served[0]["host"] == "127.0.0.1"
    assert local_served[0]["port"] == 8033
    assert local_served[0]["model"] is None
    assert local_served[0]["authorization"] is None
    assert external_served[0]["host"] == "api.provider-one.example"
    assert external_served[0]["model"] == "provider-one-large"
    assert external_served[0]["authorization"] == "Bearer sk-a"
