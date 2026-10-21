"""The streaming contract: append-only answers, no injected progress (RM20, AC09).

Issue #23, spec 0.15 section 7.3. The stream carries only answer content
or client-owned tool-call deltas with protocol framing. Emitted text is
append-only. A provider failure after the first chunk ends the connection
with an error and never with a success marker, and the gateway sends no
replacement answer. A skill whose evidence predicate is unmet cannot
finalize. Deterministic: the orchestrator runs over model doubles.
"""

import asyncio
import json

import httpx
import openai
import pytest

from api_helpers import (
    RUN_ID_HEADER,
    RunningServer,
    chat,
    healthy_gateway,
    make_app,
    sse_events,
    token_of,
)
from gateway_helpers import (
    STAGING_OK,
    build_gateway,
    route_turn,
    text_turn,
    tool_turn,
)
from helpers import of_type
from relaymind.adapters import ToolCall
from relaymind.api.chat import ChatResult, LiveAnswer
from relaymind.api.chat_request import CLIENT_TOOLS, MANAGED
from relaymind.orchestrator.budgets import LocalBudgets

TOOLS = [
    {
        "type": "function",
        "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {}}},
    }
]
PROVIDER_TEXT = "upstream said: key sk-live-123 is rejected"
PIECES = ("staging-api is ", "healthy ", "(checked at 2026-10-20T02:58:00Z).")


class FixedBackend:
    """A client-owned backend double that returns one prepared result."""

    def __init__(self, result):
        self.result = result

    async def complete(self, request, mode):
        return self.result


class LiveBackend:
    """A client-owned backend double that forwards provider text as it arrives.

    The provider yields the given pieces and then, when fail_with is
    set, raises. fail() stands for the backend recording run.failed.
    """

    def __init__(self, pieces, *, fail_with=None):
        self.pieces = pieces
        self.fail_with = fail_with
        self.failed: list[str] = []
        self.delivered: list[str] = []

    async def complete(self, request, mode):
        async def provider():
            for piece in self.pieces:
                self.delivered.append(piece)
                yield piece
            if self.fail_with is not None:
                raise self.fail_with

        return ChatResult(
            run_id="run_live",
            content=None,
            live=LiveAnswer(pieces=provider(), fail=lambda: self.failed.append("run_live")),
        )


def tool_call_result():
    return ChatResult(
        run_id="run_client",
        content=None,
        finish_reason="tool_calls",
        tool_calls=(
            ToolCall(call_id="call_1", tool_name="get_weather", arguments={"city": "Oslo"}),
        ),
    )


def stream_body(model, **fields):
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Is staging-api healthy right now?"}],
        "stream": True,
        **fields,
    }


def raw_stream(app, body):
    """The bytes sent for a streaming request, even when the app fails after they start."""

    async def go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.post(
                "/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {token_of(app)}"},
            )

    return asyncio.run(go())


def data_lines(response):
    return [b[len("data: ") :] for b in response.text.split("\n\n") if b.strip()]


# -- framing: both aliases, both terminal paths -------------------------------------------


def managed_stream(tmp_path):
    app = make_app(healthy_gateway(tmp_path))
    return sse_events(chat(app, stream=True)), "stop", MANAGED


def client_text_stream(tmp_path):
    result = ChatResult("run_client", "".join(PIECES), pieces=PIECES)
    app = make_app(healthy_gateway(tmp_path), client_tools=FixedBackend(result))
    return sse_events(chat(app, model=CLIENT_TOOLS, tools=TOOLS, stream=True)), "stop", CLIENT_TOOLS


def client_tool_call_stream(tmp_path):
    app = make_app(healthy_gateway(tmp_path), client_tools=FixedBackend(tool_call_result()))
    events = sse_events(chat(app, model=CLIENT_TOOLS, tools=TOOLS, stream=True))
    return events, "tool_calls", CLIENT_TOOLS


@pytest.mark.parametrize(
    "build", [managed_stream, client_text_stream, client_tool_call_stream], ids=lambda f: f.__name__
)
def test_every_stream_has_the_same_framing_and_one_terminal_path(tmp_path, build):
    """RM20, AC09: role chunk, content or tool-call deltas, one finish chunk, then [DONE]."""
    events, finish, alias = build(tmp_path)
    assert events[-1] == "[DONE]" and events.count("[DONE]") == 1
    chunks = events[:-1]
    assert len({c["id"] for c in chunks}) == 1 and chunks[0]["id"].startswith("chatcmpl-")
    assert {c["object"] for c in chunks} == {"chat.completion.chunk"}
    assert {c["model"] for c in chunks} == {alias}
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    finishes = [c["choices"][0]["finish_reason"] for c in chunks]
    assert finishes[-1] == finish and finishes[:-1] == [None] * (len(chunks) - 1)
    assert chunks[-1]["choices"][0]["delta"] == {}
    # Nothing but content or tool_calls (and the role on the first chunk) between them.
    for c in chunks[1:-1]:
        assert set(c["choices"][0]["delta"]) in ({"content"}, {"tool_calls"})


# -- append-only answers, nothing injected ---------------------------------------------------


def test_managed_content_chunks_are_exactly_the_published_answer_deltas(tmp_path):
    """RM20: no progress text, no acknowledgment, no empty heartbeat chunks."""
    turns = [
        route_turn("check-service-health"),
        tool_turn("get_service_health", service="staging-api"),
        text_turn(STAGING_OK),
        text_turn(*PIECES),
    ]
    gateway = build_gateway(tmp_path, turns, external_turns=[])
    response = chat(make_app(gateway), stream=True)
    run_id = response.headers[RUN_ID_HEADER]
    chunks = sse_events(response)[:-1]
    sent = [c["choices"][0]["delta"].get("content") for c in chunks[1:-1]]
    published = [e.payload["text"] for e in of_type(gateway.store.events(run_id), "answer.delta")]
    assert published == list(PIECES)
    assert sent == published
    assert "".join(sent) == STAGING_OK


def test_the_stream_never_shortens_or_rewrites_what_a_plain_response_says(tmp_path):
    plain = chat(make_app(healthy_gateway(tmp_path / "a"))).json()
    streamed = chat(make_app(healthy_gateway(tmp_path / "b")), stream=True)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in sse_events(streamed)[:-1])
    assert text == plain["choices"][0]["message"]["content"] == STAGING_OK


def test_the_final_generation_runs_with_tools_disabled(tmp_path):
    """Section 7.3: planning is buffered; the generation whose text is streamed offers no tools."""
    gateway = healthy_gateway(tmp_path)
    response = chat(make_app(gateway), stream=True)
    assert response.status_code == 200
    requests = gateway.adapter.requests
    assert len(requests) == 4
    assert all(r.tool_schemas for r in requests[:3])
    assert requests[-1].tool_schemas == ()
    events = gateway.store.events(response.headers[RUN_ID_HEADER])
    assert [e.payload["purpose"] for e in of_type(events, "model.started")][-1] == "final"


def test_a_live_stream_forwards_pieces_in_order_and_finishes_once(tmp_path):
    backend = LiveBackend(PIECES)
    app = make_app(healthy_gateway(tmp_path), client_tools=backend)
    events = sse_events(raw_stream(app, stream_body(CLIENT_TOOLS)))
    chunks = events[:-1]
    assert events[-1] == "[DONE]"
    assert [c["choices"][0]["delta"].get("content") for c in chunks[1:-1]] == list(PIECES)
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert backend.failed == []


def test_a_live_stream_skips_empty_pieces(tmp_path):
    backend = LiveBackend(("a", "", "b"))
    app = make_app(healthy_gateway(tmp_path), client_tools=backend)
    chunks = sse_events(raw_stream(app, stream_body(CLIENT_TOOLS)))[:-1]
    assert [c["choices"][0]["delta"].get("content") for c in chunks[1:-1]] == ["a", "b"]


# -- a provider failure after the stream started ------------------------------------------------


def test_a_mid_stream_failure_ends_without_a_finish_chunk_or_done(tmp_path):
    """RM20, section 11: an error event, run recorded as failed, no success marker."""
    backend = LiveBackend(PIECES[:2], fail_with=RuntimeError(PROVIDER_TEXT))
    app = make_app(healthy_gateway(tmp_path), client_tools=backend)
    response = raw_stream(app, stream_body(CLIENT_TOOLS))
    lines = data_lines(response)
    assert "[DONE]" not in lines
    events = [json.loads(line) for line in lines]
    chunks, error = events[:-1], events[-1]
    assert [c["choices"][0]["delta"].get("content") for c in chunks[1:]] == list(PIECES[:2])
    assert all(c["choices"][0]["finish_reason"] is None for c in chunks)
    assert error == {
        "error": {
            "code": "external_failed",
            "message": error["error"]["message"],
            "run_id": "run_live",
        }
    }
    # The message is fixed text: nothing the provider said reaches the client (RM12).
    assert PROVIDER_TEXT not in response.text and "sk-live" not in response.text
    assert backend.failed == ["run_live"]


def test_no_replacement_answer_follows_a_mid_stream_failure(tmp_path):
    """RM20: the managed model is never asked to continue or replace the answer."""
    gateway = healthy_gateway(tmp_path)
    backend = LiveBackend(PIECES[:1], fail_with=RuntimeError("down"))
    app = make_app(gateway, client_tools=backend)
    response = raw_stream(app, stream_body(CLIENT_TOOLS))
    text = "".join(
        c["choices"][0]["delta"].get("content", "")
        for c in map(json.loads, data_lines(response)[:-1])
    )
    assert text == PIECES[0]
    assert gateway.adapter.requests == [] and gateway.external.requests == []


def test_a_failing_recorder_still_ends_the_stream_without_a_success_marker(tmp_path):
    backend = LiveBackend(PIECES[:1], fail_with=RuntimeError("down"))

    async def complete(request, mode):
        result = await LiveBackend.complete(backend, request, mode)

        def broken():
            raise RuntimeError("store is closed")

        return ChatResult(result.run_id, None, live=LiveAnswer(result.live.pieces, broken))

    backend.complete = complete
    app = make_app(healthy_gateway(tmp_path), client_tools=backend)
    lines = data_lines(raw_stream(app, stream_body(CLIENT_TOOLS)))
    assert "[DONE]" not in lines
    assert all(
        json.loads(line).get("choices", [{}])[0].get("finish_reason") is None for line in lines
    )


def test_a_failure_before_the_first_chunk_is_a_normal_error_response(tmp_path):
    """Section 11: the status can still change before streaming starts."""

    class Failing:
        async def complete(self, request, mode):
            raise RuntimeError(PROVIDER_TEXT)

    app = make_app(healthy_gateway(tmp_path), client_tools=Failing())
    response = raw_stream(app, stream_body(CLIENT_TOOLS))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert PROVIDER_TEXT not in response.text


def test_the_sdk_sees_a_mid_stream_failure_as_an_error_over_a_socket(tmp_path):
    """AC09: a standard client gets chunks, then an error, and never a finish_reason."""
    backend = LiveBackend(PIECES[:2], fail_with=RuntimeError(PROVIDER_TEXT))
    app = make_app(healthy_gateway(tmp_path), client_tools=backend)
    received = []
    with RunningServer(app) as running:
        client = openai.OpenAI(
            api_key=token_of(app),
            base_url=running.base_url,
            http_client=httpx.Client(trust_env=False),
            max_retries=0,
        )
        stream = client.chat.completions.create(
            model=CLIENT_TOOLS, messages=stream_body(CLIENT_TOOLS)["messages"], stream=True
        )
        with pytest.raises(openai.APIError) as caught:
            for chunk in stream:
                received.append(chunk)
    assert PROVIDER_TEXT not in str(caught.value)
    assert [c.choices[0].delta.content for c in received[1:]] == list(PIECES[:2])
    assert all(c.choices[0].finish_reason is None for c in received)
    assert backend.failed == ["run_live"]


# -- a skill with an unmet evidence predicate cannot finalize -------------------------------


@pytest.mark.parametrize("stream", [False, True])
def test_an_unmet_predicate_never_reaches_the_client_as_an_answer(tmp_path, stream):
    """Section 7.3, RM16: the model's finalize request alone does not finalize."""
    wrong = "payments-api is healthy (checked at 2026-10-20T02:59:00Z)."
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("check-service-health"),
            tool_turn("get_service_health", service="payments-api"),
            text_turn(wrong),
        ],
        budgets=LocalBudgets(local_generations=3, repair_generations=0),
    )
    response = chat(make_app(gateway), prompt="Check the health of payments-api.", stream=stream)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "needs_external"
    assert "healthy" not in response.text
    events = gateway.store.events(response.headers[RUN_ID_HEADER])
    assert not of_type(events, "answer.delta")
    assert len(gateway.adapter.requests) == 3, "a final generation ran on an unmet predicate"
