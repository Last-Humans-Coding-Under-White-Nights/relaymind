"""LlamaCppAdapter over HTTP, with a mock transport.

Covers RM18: the adapter implements capabilities, generate, cancel,
and health against a llama-server HTTP endpoint. Deterministic; the
live server smoke test is separate and marked. Issue #3.
"""

import asyncio
import json

import httpx

from relaymind.adapters import Completed, GenerationRequest, Message, Role, TextDelta
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig

CONFIG = LlamaCppConfig(
    base_url="http://127.0.0.1:8033",
    context_limit=8192,
    chat_template_kwargs={"enable_thinking": False},
)


def sse_body(chunks: list[dict]) -> str:
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    return "".join(lines) + "data: [DONE]\n\n"


def make_adapter(handler) -> LlamaCppAdapter:
    return LlamaCppAdapter(CONFIG, transport=httpx.MockTransport(handler))


def simple_request() -> GenerationRequest:
    return GenerationRequest(
        generation_id="gen_1",
        messages=(Message(role=Role.USER, content="hello"),),
    )


def test_capabilities_come_from_config():
    adapter = make_adapter(lambda request: httpx.Response(200))
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
            {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body(chunks),
        )

    adapter = make_adapter(handler)

    async def run():
        return [event async for event in adapter.generate(simple_request())]

    events = asyncio.run(run())
    assert seen["path"] == "/v1/chat/completions"
    # Non-thinking mode rides on every request via chat template settings.
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert events == [TextDelta(text="hi"), Completed(finish_reason="stop")]


def test_health_maps_ready_loading_and_unreachable():
    ready = make_adapter(lambda request: httpx.Response(200, json={"status": "ok"}))
    health = asyncio.run(ready.health())
    assert (health.available, health.warm) == (True, True)

    loading = make_adapter(
        lambda request: httpx.Response(503, json={"error": {"message": "Loading model"}})
    )
    health = asyncio.run(loading.health())
    assert (health.available, health.warm) == (True, False)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    down = make_adapter(unreachable)
    health = asyncio.run(down.health())
    assert health.available is False


def test_cancel_stops_an_active_generation_early():
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": f"part {i}"}, "finish_reason": None}]}
        for i in range(50)
    ] + [{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body(chunks),
        )

    adapter = make_adapter(handler)

    async def run():
        events = []
        async for event in adapter.generate(simple_request()):
            events.append(event)
            if len(events) == 2:
                await adapter.cancel("gen_1")
        return events

    events = asyncio.run(run())
    assert len(events) < 51
    assert not any(isinstance(event, Completed) for event in events)


def test_cancel_of_unknown_generation_is_a_no_op():
    adapter = make_adapter(lambda request: httpx.Response(200))
    asyncio.run(adapter.cancel("nope"))
