"""Model substitution test, adapter layer (RM18, part of AC08).

Switching to a second local llama-server endpoint or a second
compatible external endpoint needs configuration changes only. The
scenario runner below takes the ModelAdapter interface and nothing
else — no branch in it knows which provider, endpoint, or model is
behind the adapter — so rerunning it against a swapped configuration,
and checking that the swapped adapter put the same scenario on the
wire, is the evidence that the adapter boundary absorbs the swap.

Scope: this covers the adapter layer only. The local configurations
are two llama-server endpoints; which artifact each one serves, and
whether that artifact is qualified, is pinned by the artifact manifest
(spec section 6) and is not tied to LlamaCppConfig, so this test does
not show a qualified-model swap. Only one artifact has a manifest so
far. AC08 also requires the same orchestrator and skill packages to
run unchanged across the swap; that half needs the orchestrator and
skill registry, which do not exist yet, and is tracked in issue #5.
"""

import json

import httpx

from helpers import (
    HEALTH_TOOL,
    delta_chunk,
    make_request,
    run_closing,
    sse_response,
)
from relaymind.adapters import Completed, Message, ModelAdapter, Role, ToolCall
from relaymind.adapters.chat_completions import (
    ChatCompletionsAdapter,
    ChatCompletionsConfig,
)
from relaymind.adapters.llamacpp import NON_THINKING, LlamaCppAdapter, LlamaCppConfig

QUESTION = "Is staging-api healthy right now?"

# The one scenario, reused verbatim across every configuration: a
# health question that the model must answer with a tool proposal.
SCENARIO_REQUEST = make_request(
    messages=(Message(role=Role.USER, content=QUESTION),),
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

# Body keys that legitimately differ by configuration. Everything else
# in a generation body is the scenario and must be identical across a
# swap.
MODEL_KEYS = ("model",)
LOCAL_ONLY_KEYS = ("chat_template_kwargs",)

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


async def run_scenario(adapter: ModelAdapter) -> list:
    """Drive one adapter through the scenario.

    Takes the adapter contract only; it must never need to know what
    is behind it (RM18: the caller is the same code for every supported
    configuration).
    """
    return [event async for event in adapter.generate(SCENARIO_REQUEST)]


def exercise(adapter: ModelAdapter):
    """Capabilities, health, and the scenario, in one loop, then close.

    Uses the adapter contract only, so health() and capabilities() are
    swapped along with generate().
    """

    async def probe(a: ModelAdapter):
        return await a.capabilities(), await a.health(), await run_scenario(a)

    return run_closing(adapter, probe)


def scripted_endpoint(served: list[dict]):
    """A mock endpoint that answers health checks and replays the scenario.

    Appends one record per request (method, host, port, path, model
    identifier, Authorization header, and the parsed body), so a test
    can prove the swap reached the swapped configuration and that the
    swapped adapter sent the scenario itself, not just any request.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        served.append(
            {
                "method": request.method,
                "host": request.url.host,
                "port": request.url.port,
                "path": request.url.path,
                "model": body.get("model") if body else None,
                "authorization": request.headers.get("authorization"),
                "body": body,
            }
        )
        if request.method == "GET":
            return httpx.Response(200, json={"status": "ok"})
        return sse_response(SCENARIO_CHUNKS)

    return handler


def scripted_adapter(adapter_cls, config):
    """Build an adapter on a scripted endpoint; return it and its record list."""
    served: list[dict] = []
    adapter = adapter_cls(config, transport=httpx.MockTransport(scripted_endpoint(served)))
    return adapter, served


def requests_by_method(served: list[dict], method: str) -> list[dict]:
    return [record for record in served if record["method"] == method]


def scenario_body(record: dict, *, drop: tuple[str, ...] = MODEL_KEYS) -> dict:
    """The generation body minus the configuration-specific keys."""
    return {key: value for key, value in record["body"].items() if key not in drop}


def assert_carries_scenario(body: dict) -> None:
    assert [m["content"] for m in body["messages"]] == [QUESTION]
    assert [t["function"]["name"] for t in body["tools"]] == ["get_service_health"]


def test_local_model_swap_is_configuration_only():
    # First local llama-server endpoint, then a second one: only
    # LlamaCppConfig values change.
    first, first_served = scripted_adapter(LlamaCppAdapter, LOCAL_ONE)
    second, second_served = scripted_adapter(LlamaCppAdapter, LOCAL_TWO)

    first_caps, first_health, first_events = exercise(first)
    second_caps, second_health, second_events = exercise(second)

    assert first_caps.context_limit == 8192
    assert second_caps.context_limit == 32768
    # Same scenario, same normalized outcome, from both configurations.
    assert first_events == EXPECTED_EVENTS
    assert second_events == EXPECTED_EVENTS
    assert first_health.available and second_health.available

    first_post = requests_by_method(first_served, "POST")
    second_post = requests_by_method(second_served, "POST")
    assert len(first_post) == 1
    assert len(second_post) == 1
    assert first_post[0]["port"] == 8033
    assert second_post[0]["port"] == 9044
    assert first_post[0]["path"] == "/v1/chat/completions"
    assert second_post[0]["path"] == "/v1/chat/completions"
    # The swapped adapter sent the same scenario, including non-thinking
    # mode (spec section 6), not just any request.
    assert scenario_body(first_post[0]) == scenario_body(second_post[0])
    assert_carries_scenario(first_post[0]["body"])
    assert first_post[0]["body"]["chat_template_kwargs"] == dict(NON_THINKING)
    assert second_post[0]["body"]["chat_template_kwargs"] == dict(NON_THINKING)

    # Health follows the swap too: llama-server /health, no credential.
    for served, port in ((first_served, 8033), (second_served, 9044)):
        (check,) = requests_by_method(served, "GET")
        assert (check["port"], check["path"]) == (port, "/health")
        assert check["authorization"] is None


def test_external_endpoint_swap_is_configuration_only(monkeypatch):
    # First compatible external endpoint, then a second provider with
    # its own model identifier and credential variable: only
    # ChatCompletionsConfig values change.
    monkeypatch.setenv("SUBST_KEY_A", "sk-a")
    monkeypatch.setenv("SUBST_KEY_B", "sk-b")
    first, first_served = scripted_adapter(ChatCompletionsAdapter, PROVIDER_ONE)
    second, second_served = scripted_adapter(ChatCompletionsAdapter, PROVIDER_TWO)

    first_caps, first_health, first_events = exercise(first)
    second_caps, second_health, second_events = exercise(second)

    assert first_caps.context_limit == 128000
    assert second_caps.context_limit == 64000
    assert first_events == EXPECTED_EVENTS
    assert second_events == EXPECTED_EVENTS
    assert first_health.available and second_health.available

    first_post = requests_by_method(first_served, "POST")
    second_post = requests_by_method(second_served, "POST")
    assert len(first_post) == 1
    assert len(second_post) == 1
    assert first_post[0]["host"] == "api.provider-one.example"
    assert first_post[0]["path"] == "/v1/chat/completions"
    assert first_post[0]["model"] == "provider-one-large"
    assert first_post[0]["authorization"] == "Bearer sk-a"
    assert second_post[0]["host"] == "api.provider-two.example"
    assert second_post[0]["path"] == "/v1/chat/completions"
    assert second_post[0]["model"] == "provider-two-medium"
    assert second_post[0]["authorization"] == "Bearer sk-b"
    # Same scenario on the wire; only the model identifier differs.
    assert scenario_body(first_post[0]) == scenario_body(second_post[0])
    assert_carries_scenario(first_post[0]["body"])

    # Health follows the swap: each provider's /models with its own key.
    for served, host, auth in (
        (first_served, "api.provider-one.example", "Bearer sk-a"),
        (second_served, "api.provider-two.example", "Bearer sk-b"),
    ):
        (check,) = requests_by_method(served, "GET")
        assert (check["host"], check["path"]) == (host, "/v1/models")
        assert check["authorization"] == auth


def test_local_to_external_swap_keeps_the_same_scenario_code(monkeypatch):
    # RM18: swapping between two supported adapters is a configuration
    # and construction concern; the scenario runner never changes. What
    # is specific to crossing the boundary: the local request stays on
    # loopback with no model identifier and no credential, even while
    # external credentials are configured in the environment (the
    # custom variable and the default one), and the external request
    # carries both.
    monkeypatch.setenv("SUBST_KEY_A", "sk-a")
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "sk-default")
    local, local_served = scripted_adapter(LlamaCppAdapter, LOCAL_ONE)
    external, external_served = scripted_adapter(ChatCompletionsAdapter, PROVIDER_ONE)

    local_caps, local_health, local_events = exercise(local)
    external_caps, external_health, external_events = exercise(external)

    assert local_events == EXPECTED_EVENTS
    assert external_events == EXPECTED_EVENTS
    assert local_health.available and external_health.available

    local_post = requests_by_method(local_served, "POST")
    external_post = requests_by_method(external_served, "POST")
    assert len(local_post) == 1
    assert len(external_post) == 1
    assert local_post[0]["host"] == "127.0.0.1"
    assert local_post[0]["port"] == 8033
    assert local_post[0]["model"] is None
    assert local_post[0]["authorization"] is None
    assert external_post[0]["host"] == "api.provider-one.example"
    assert external_post[0]["model"] == "provider-one-large"
    assert external_post[0]["authorization"] == "Bearer sk-a"
    # Same scenario on both sides; only the model identifier and the
    # local chat-template settings differ.
    drop = MODEL_KEYS + LOCAL_ONLY_KEYS
    assert scenario_body(local_post[0], drop=drop) == scenario_body(external_post[0], drop=drop)
    assert_carries_scenario(local_post[0]["body"])
    assert local_post[0]["body"]["chat_template_kwargs"] == dict(NON_THINKING)
    assert "chat_template_kwargs" not in external_post[0]["body"]
    (local_check,) = requests_by_method(local_served, "GET")
    assert local_check["authorization"] is None

    # The contract surface that does change across the boundary is
    # reported through capabilities, not hidden: callers read it, the
    # scenario runner does not branch on it.
    assert local_caps.context_limit == 8192
    assert external_caps.context_limit == 128000
    assert local_caps.reasoning_controls == ("enable_thinking",)
    assert external_caps.reasoning_controls == ()
