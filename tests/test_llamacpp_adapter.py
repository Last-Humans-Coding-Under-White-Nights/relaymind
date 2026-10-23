"""LlamaCppAdapter over HTTP, with a mock transport.

Covers RM18: the adapter implements capabilities, generate, cancel,
and health against a llama-server HTTP endpoint. Deterministic; the
live server smoke test is separate and marked. Issue #3.
"""

import asyncio
import json

import httpx
import pytest

from helpers import collect_events, delta_chunk, make_llamacpp_adapter, make_request, sse_response
from relaymind.adapters import Completed, FirstToken, GenerationFailed, TextDelta, ToolCall, Usage
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig


def test_capabilities_come_from_config():
    adapter = make_llamacpp_adapter(lambda request: httpx.Response(200))
    caps = asyncio.run(adapter.capabilities())
    assert caps.tool_calling is True
    assert caps.text_streaming is True
    assert caps.context_limit == 8192


def test_generate_streams_normalized_events():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        chunks = [
            delta_chunk({"content": "hi"}),
            delta_chunk({}, finish_reason="stop"),
        ]
        return sse_response(chunks)

    adapter = make_llamacpp_adapter(handler)
    events = collect_events(adapter, make_request())
    assert seen["path"] == "/v1/chat/completions"
    # Non-thinking mode rides on every request via chat template settings.
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert events == [FirstToken(), TextDelta(text="hi"), Completed(finish_reason="stop")]


def test_config_defaults_to_non_thinking_mode():
    # Spec section 6: non-thinking is the default; an operator may
    # enable thinking through the same chat-template settings.
    config = LlamaCppConfig(base_url="http://127.0.0.1:8033", context_limit=8192)
    assert dict(config.chat_template_kwargs) == {"enable_thinking": False}


def test_generate_streams_tool_call_over_http():
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

    events = collect_events(make_llamacpp_adapter(handler), make_request())
    assert events == [
        FirstToken(),
        ToolCall(call_id="call_1", tool_name="get_service_health", arguments={"service": "s"}),
        Completed(finish_reason="tool_calls"),
    ]


def test_stream_ending_without_finish_reason_still_completes():
    # Finding: EOF / [DONE] without a finish_reason chunk dropped
    # buffered tool calls and never yielded Completed.
    chunks = [
        delta_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "get_service_health", "arguments": "{}"},
                    }
                ]
            }
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(chunks)

    events = collect_events(make_llamacpp_adapter(handler), make_request())
    assert events == [
        FirstToken(),
        ToolCall(call_id="call_1", tool_name="get_service_health", arguments={}),
        Completed(finish_reason="end_of_stream"),
    ]


def test_non_200_response_fails_with_provider_error():
    adapter = make_llamacpp_adapter(lambda request: httpx.Response(500, text="boom"))
    events = collect_events(adapter, make_request())
    assert len(events) == 1
    assert isinstance(events[0], GenerationFailed)
    assert events[0].code == "provider_error"
    # RM25 / spec 12.1: the error status came over the wire, so it is a
    # transport event; a synthesized failure is not.
    assert events[0].received is True


def test_unreachable_server_fails_with_provider_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    events = collect_events(make_llamacpp_adapter(handler), make_request())
    assert len(events) == 1
    assert isinstance(events[0], GenerationFailed)
    assert events[0].code == "provider_unreachable"
    # Nothing came over the wire (RM25, spec 12.1).
    assert events[0].received is False


def test_timeout_fails_with_provider_timeout():
    # Finding: timeouts were reported as provider_unreachable; they
    # carry the stable provider_timeout code instead.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    events = collect_events(make_llamacpp_adapter(handler), make_request())
    assert len(events) == 1
    assert isinstance(events[0], GenerationFailed)
    assert events[0].code == "provider_timeout"


def test_adapter_is_an_async_context_manager():
    adapter = make_llamacpp_adapter(lambda request: httpx.Response(200))

    async def run():
        async with adapter as entered:
            assert entered is adapter

    asyncio.run(run())
    assert adapter._client.is_closed


def test_health_maps_ready_loading_and_unreachable():
    ready = make_llamacpp_adapter(lambda request: httpx.Response(200, json={"status": "ok"}))
    health = asyncio.run(ready.health())
    assert (health.available, health.warm) == (True, True)

    loading = make_llamacpp_adapter(
        lambda request: httpx.Response(503, json={"error": {"message": "Loading model"}})
    )
    health = asyncio.run(loading.health())
    assert (health.available, health.warm) == (True, False)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    down = make_llamacpp_adapter(unreachable)
    health = asyncio.run(down.health())
    assert health.available is False


def test_cancel_stops_an_active_generation_early():
    chunks = [delta_chunk({"content": f"part {i}"}) for i in range(50)]
    chunks.append(delta_chunk({}, finish_reason="stop"))

    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(chunks)

    adapter = make_llamacpp_adapter(handler)

    async def run():
        events = []
        async for event in adapter.generate(make_request()):
            events.append(event)
            if len(events) == 2:
                await adapter.cancel("gen_1")
        return events

    events = asyncio.run(run())
    assert len(events) < 51
    assert not any(isinstance(event, Completed) for event in events)


def test_cancel_of_unknown_generation_is_a_no_op():
    adapter = make_llamacpp_adapter(lambda request: httpx.Response(200))
    asyncio.run(adapter.cancel("nope"))


def test_cancel_interrupts_an_idle_stream():
    # Finding: a flag checked at the next chunk never fires when the
    # stream is idle; cancel must interrupt the wait itself.
    first_chunk = (
        b'data: {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": null}]}\n\n'
    )

    class HangingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first_chunk
            await asyncio.Event().wait()  # never set: stream goes idle

        async def aclose(self):
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=HangingStream()
        )

    adapter = make_llamacpp_adapter(handler)

    async def run():
        events = []

        async def consume():
            async for event in adapter.generate(make_request()):
                events.append(event)

        task = asyncio.create_task(consume())
        while not events and not task.done():
            await asyncio.sleep(0.01)
        await adapter.cancel("gen_1")
        await asyncio.wait_for(task, timeout=2)
        return events

    events = asyncio.run(run())
    assert events == [FirstToken(), TextDelta(text="hi")]


def test_done_marker_ends_the_stream_without_waiting_for_eof():
    # data: [DONE] is the protocol's termination marker; on a kept-open
    # connection the generator must not wait for EOF or a read timeout.
    body = (
        b'data: {"choices": [{"index": 0, "delta": {"content": "hi"},'
        b' "finish_reason": "stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    class KeptOpenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield body
            await asyncio.Event().wait()  # connection stays open

        async def aclose(self):
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=KeptOpenStream()
        )

    adapter = make_llamacpp_adapter(handler)

    async def run():
        async with adapter:
            return [event async for event in adapter.generate(make_request())]

    events = asyncio.run(asyncio.wait_for(run(), timeout=2))
    assert events == [FirstToken(), TextDelta(text="hi"), Completed(finish_reason="stop")]


@pytest.mark.parametrize("thinking", [None, False, True])
def test_every_request_extracts_reasoning_and_never_streams_it_as_answer(thinking):
    # RM18 / section 6: default and operator-enabled thinking both isolate thoughts.
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return sse_response(
            [
                delta_chunk({"reasoning_content": "private thought"}),
                delta_chunk({"content": "pong"}),
                delta_chunk({}, finish_reason="stop"),
            ]
        )

    options = {} if thinking is None else {"chat_template_kwargs": {"enable_thinking": thinking}}
    config = LlamaCppConfig(base_url="http://localhost:8033", context_limit=8192, **options)
    adapter = LlamaCppAdapter(config, transport=httpx.MockTransport(handler))
    events = collect_events(adapter, make_request())
    assert seen["reasoning_format"] == "deepseek"
    assert seen["chat_template_kwargs"] == {"enable_thinking": thinking is True}
    assert events == [FirstToken(), TextDelta(text="pong"), Completed(finish_reason="stop")]


# -- prompt caching, counts, and the first-token marker (#120; RM18, RM27) -----------------


def test_every_request_asks_for_prompt_caching():
    """Spec 0.31 section 6: the adapter sends cache_prompt true, not the server default."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return sse_response([delta_chunk({}, finish_reason="stop")])

    collect_events(make_llamacpp_adapter(handler), make_request())
    assert seen["body"]["cache_prompt"] is True


def test_the_runtime_counts_reach_the_usage_event_over_http():
    """Spec 0.31 section 6: cache_n from timings and the usage details are read live."""
    chunks = [
        delta_chunk({"content": "ok"}),
        {**delta_chunk({}, finish_reason="stop"), "timings": {"cache_n": 40, "prompt_n": 10}},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 2,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
            "timings": {"cache_n": 40, "prompt_n": 10},
        },
    ]
    events = collect_events(make_llamacpp_adapter(lambda r: sse_response(chunks)), make_request())
    assert Usage(input_tokens=50, output_tokens=2, cached_tokens=40, reasoning_tokens=0) in events


def test_first_token_marker_comes_before_the_first_text_and_is_sent_once():
    """Spec 0.31 section 6: the marker precedes the first fragment's events."""
    chunks = [
        delta_chunk({"role": "assistant", "content": ""}),
        delta_chunk({"content": "hi"}),
        delta_chunk({"content": " there"}),
        delta_chunk({}, finish_reason="stop"),
    ]
    events = collect_events(make_llamacpp_adapter(lambda r: sse_response(chunks)), make_request())
    assert events == [
        FirstToken(),
        TextDelta(text="hi"),
        TextDelta(text=" there"),
        Completed(finish_reason="stop"),
    ]


def test_first_token_marker_comes_at_the_first_tool_fragment_not_at_the_call():
    """RM09: the ToolCall exists only after the finish chunk; the marker does not wait for it."""
    fragment = {
        "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "t", "arguments": ""}}]
    }
    rest = {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}
    chunks = [
        delta_chunk(fragment),
        delta_chunk(rest),
        delta_chunk({}, finish_reason="tool_calls"),
    ]
    events = collect_events(make_llamacpp_adapter(lambda r: sse_response(chunks)), make_request())
    assert [type(e) for e in events] == [FirstToken, ToolCall, Completed]


def test_a_stream_with_no_fragment_has_no_marker():
    events = collect_events(
        make_llamacpp_adapter(lambda r: sse_response([delta_chunk({}, finish_reason="stop")])),
        make_request(),
    )
    assert events == [Completed(finish_reason="stop")]
