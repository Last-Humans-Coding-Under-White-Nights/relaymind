"""ChatCompletionsAdapter over HTTP, with a recorded mock transport.

Covers RM18 and issue #4: capabilities, generate, cancel, and health
against an operator-configured external Chat Completions endpoint.
Credentials come from an environment variable, stay server-side, and
never appear in error messages. Deterministic; no live provider calls.
"""

import asyncio
import json

import httpx

from helpers import collect_events, delta_chunk, make_request, sse_response
from relaymind.adapters import Completed, GenerationFailed, TextDelta, ToolCall, Usage
from relaymind.adapters.chat_completions import (
    ChatCompletionsAdapter,
    ChatCompletionsConfig,
)

KEY_ENV = "RELAYMIND_TEST_EXTERNAL_KEY"

CONFIG = ChatCompletionsConfig(
    base_url="https://api.provider.example/v1",
    model="provider-large-1",
    context_limit=128000,
    api_key_env=KEY_ENV,
)


def make_adapter(handler) -> ChatCompletionsAdapter:
    return ChatCompletionsAdapter(CONFIG, transport=httpx.MockTransport(handler))


def run_closing(adapter, action):
    """Run one adapter call and close the underlying client."""

    async def go():
        async with adapter:
            return await action(adapter)

    return asyncio.run(go())


def test_config_defaults_to_the_spec_external_budget():
    # Spec section 11.2: external generation is at most 30 seconds.
    config = ChatCompletionsConfig(base_url="https://x.example/v1", model="m", context_limit=1024)
    assert config.request_timeout_seconds == 30.0


def test_capabilities_come_from_config():
    adapter = make_adapter(lambda request: httpx.Response(200))
    caps = run_closing(adapter, lambda a: a.capabilities())
    assert caps.text_streaming is True
    assert caps.tool_calling is True
    assert caps.context_limit == 128000
    # No local tokenizer and no qualified reasoning controls for an
    # external provider; claiming either would be dishonest (RM16).
    assert caps.tokenizer_available is False
    assert caps.reasoning_controls == ()


def test_generate_authenticates_from_environment(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-test-123")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        chunks = [
            delta_chunk({"content": "hello"}),
            delta_chunk({}, finish_reason="stop"),
        ]
        return sse_response(chunks)

    events = collect_events(make_adapter(handler), make_request())
    assert seen["path"] == "/v1/chat/completions"
    assert seen["authorization"] == "Bearer sk-test-123"
    assert seen["body"]["model"] == "provider-large-1"
    assert events == [TextDelta(text="hello"), Completed(finish_reason="stop")]


def test_provider_error_detail_never_contains_the_credential(monkeypatch):
    # Issue #4: credentials are never echoed in errors — even when the
    # provider or a proxy reflects the Authorization header back.
    monkeypatch.setenv(KEY_ENV, "sk-leak-me")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, text=f"bad request; got header {request.headers['authorization']}"
        )

    events = collect_events(make_adapter(handler), make_request())
    failure = events[0]
    assert isinstance(failure, GenerationFailed)
    assert failure.code == "provider_error"
    assert "sk-leak-me" not in failure.message
    assert "[redacted]" in failure.message


def test_credential_straddling_the_truncation_boundary_never_leaks(monkeypatch):
    # Redaction must run before the error detail is truncated: a key
    # crossing the cut would otherwise leave an unmatchable fragment.
    monkeypatch.setenv(KEY_ENV, "sk-secret-123456")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="e" * 195 + "sk-secret-123456 rejected")

    events = collect_events(make_adapter(handler), make_request())
    failure = events[0]
    assert isinstance(failure, GenerationFailed)
    # No fragment of the key may survive, from either end of the cut.
    assert "sk-sec" not in failure.message
    assert "123456" not in failure.message


def test_whitespace_credential_counts_as_missing(monkeypatch):
    # A blank value would send "Bearer   " and make redaction replace
    # every run of spaces; it must be treated as no credential at all.
    monkeypatch.setenv(KEY_ENV, "   \n")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may leave the adapter without credentials")

    events = collect_events(make_adapter(handler), make_request())
    assert len(events) == 1
    assert isinstance(events[0], GenerationFailed)
    assert events[0].code == "missing_credentials"


def test_usage_events_stream_over_http(monkeypatch):
    # usage_reporting is claimed and include_usage requested; the usage
    # chunk trails finish_reason on the wire, and Completed stays the
    # terminal event.
    monkeypatch.setenv(KEY_ENV, "sk-test-123")
    chunks = [
        delta_chunk({"content": "hi"}),
        delta_chunk({}, finish_reason="stop"),
        {"choices": [], "usage": {"prompt_tokens": 21, "completion_tokens": 4}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(chunks)

    events = collect_events(make_adapter(handler), make_request())
    assert events == [
        TextDelta(text="hi"),
        Usage(input_tokens=21, output_tokens=4),
        Completed(finish_reason="stop"),
    ]


def test_transport_error_message_never_contains_the_credential(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-leak-me")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"proxy rejected Bearer sk-leak-me for {request.url}")

    events = collect_events(make_adapter(handler), make_request())
    failure = events[0]
    assert isinstance(failure, GenerationFailed)
    assert failure.code == "provider_unreachable"
    assert "sk-leak-me" not in failure.message


def test_health_detail_never_contains_the_credential(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-leak-me")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"proxy rejected Bearer sk-leak-me for {request.url}")

    health = run_closing(make_adapter(handler), lambda a: a.health())
    assert health.available is False
    assert "sk-leak-me" not in health.detail


def test_streamed_error_chunk_never_contains_the_credential(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-leak-me")
    error_chunk = {"error": {"message": "quota exceeded for Bearer sk-leak-me", "code": 429}}

    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response([error_chunk])

    events = collect_events(make_adapter(handler), make_request())
    failure = events[0]
    assert isinstance(failure, GenerationFailed)
    assert failure.code == "provider_error"
    assert "sk-leak-me" not in failure.message


def test_missing_credential_fails_as_event_naming_the_variable(monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may leave the adapter without credentials")

    events = collect_events(make_adapter(handler), make_request())
    assert len(events) == 1
    failure = events[0]
    assert isinstance(failure, GenerationFailed)
    assert failure.code == "missing_credentials"
    assert KEY_ENV in failure.message


def test_generate_streams_tool_call_over_http(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-test-123")
    chunks = [
        delta_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "get_service_health", "arguments": '{"serv'},
                    }
                ]
            }
        ),
        delta_chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ice": "s"}'}}]}),
        delta_chunk({}, finish_reason="tool_calls"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(chunks)

    events = collect_events(make_adapter(handler), make_request())
    assert events == [
        ToolCall(call_id="call_1", tool_name="get_service_health", arguments={"service": "s"}),
        Completed(finish_reason="tool_calls"),
    ]


def test_timeout_fails_with_provider_timeout(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-test-123")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    events = collect_events(make_adapter(handler), make_request())
    assert len(events) == 1
    assert isinstance(events[0], GenerationFailed)
    assert events[0].code == "provider_timeout"


def test_cancel_stops_an_active_generation_early(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-test-123")
    chunks = [delta_chunk({"content": f"part {i}"}) for i in range(50)]
    chunks.append(delta_chunk({}, finish_reason="stop"))

    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(chunks)

    adapter = make_adapter(handler)

    async def run():
        events = []
        async with adapter:
            async for event in adapter.generate(make_request()):
                events.append(event)
                if len(events) == 2:
                    await adapter.cancel("gen_1")
        return events

    events = asyncio.run(run())
    assert len(events) < 51
    assert not any(isinstance(event, Completed) for event in events)


def test_health_maps_ready_auth_rejected_and_missing_credentials(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "sk-test-123")
    seen = {}

    def ready(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": []})

    health = run_closing(make_adapter(ready), lambda a: a.health())
    assert (health.available, health.warm) == (True, True)
    assert seen["path"] == "/v1/models"
    assert seen["authorization"] == "Bearer sk-test-123"

    rejected = make_adapter(lambda request: httpx.Response(401, text="bad key"))
    health = run_closing(rejected, lambda a: a.health())
    assert health.available is False
    assert "sk-test-123" not in health.detail

    monkeypatch.delenv(KEY_ENV, raising=False)
    no_key = make_adapter(lambda request: httpx.Response(200))
    health = run_closing(no_key, lambda a: a.health())
    assert health.available is False
    assert KEY_ENV in health.detail
