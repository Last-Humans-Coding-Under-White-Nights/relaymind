"""AC09: a standard SDK sends text with stream true and false (RM19, RM20).

Issue #22, spec 0.14 section 7.1. The official OpenAI Python SDK is the
client: the same conversation runs with stream false and true, over
the in-process app and over a real loopback socket. The responses are
valid chat completions, the request ID is stable across chunks, the
final markers are right, and no internal progress appears in the
content. A rejected request surfaces as the SDK's own error type, with
the documented code. Deterministic: the orchestrator runs over model
doubles.
"""

import asyncio
import threading
import time

import httpx
import openai
import pytest

from api_helpers import TOKEN, healthy_gateway, make_app
from gateway_helpers import STAGING_OK
from helpers import of_type
from relaymind.api.server import build_server, serve

PROMPT = "Is staging-api healthy right now?"
MESSAGES = [{"role": "user", "content": PROMPT}]


def in_process_client(app, key=TOKEN):
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1/v1")
    return openai.AsyncOpenAI(
        api_key=key, base_url="http://127.0.0.1/v1", http_client=http, max_retries=0
    )


def test_models_list_through_the_sdk(tmp_path):
    async def go():
        client = in_process_client(make_app(healthy_gateway(tmp_path)))
        return [m.id async for m in client.models.list()]

    assert asyncio.run(go()) == ["relaymind"]


def test_a_plain_completion_through_the_sdk(tmp_path):
    gateway = healthy_gateway(tmp_path)

    async def go():
        client = in_process_client(make_app(gateway))
        return await client.chat.completions.create(model="relaymind", messages=MESSAGES)

    completion = asyncio.run(go())
    assert completion.model == "relaymind"
    assert completion.object == "chat.completion"
    (choice,) = completion.choices
    assert (choice.index, choice.finish_reason) == (0, "stop")
    assert choice.message.role == "assistant" and choice.message.content == STAGING_OK
    assert completion.id.startswith("chatcmpl-run_")
    assert completion.usage is None
    # The ID is the run that served the request (RM16).
    run_id = completion.id.removeprefix("chatcmpl-")
    assert gateway.store.get_run(run_id).state == "completed"


def test_a_streamed_completion_through_the_sdk(tmp_path):
    gateway = healthy_gateway(tmp_path)

    async def go():
        client = in_process_client(make_app(gateway))
        stream = await client.chat.completions.create(
            model="relaymind", messages=MESSAGES, stream=True
        )
        return [chunk async for chunk in stream]

    chunks = asyncio.run(go())
    assert {c.id for c in chunks} == {chunks[0].id}
    assert all(c.object == "chat.completion.chunk" and c.model == "relaymind" for c in chunks)
    assert chunks[0].choices[0].delta.role == "assistant"
    text = "".join(c.choices[0].delta.content or "" for c in chunks)
    assert text == STAGING_OK
    finishes = [c.choices[0].finish_reason for c in chunks]
    assert finishes[-1] == "stop" and finishes[:-1] == [None] * (len(chunks) - 1)
    # Nothing but the answer was streamed (RM20): the deltas are the run's answer.delta events.
    run_id = chunks[0].id.removeprefix("chatcmpl-")
    published = [e.payload["text"] for e in of_type(gateway.store.events(run_id), "answer.delta")]
    streamed = [c.choices[0].delta.content for c in chunks if c.choices[0].delta.content]
    assert streamed == published


def test_an_unsupported_parameter_is_the_sdks_bad_request(tmp_path):
    async def go():
        client = in_process_client(make_app(healthy_gateway(tmp_path)))
        await client.chat.completions.create(model="relaymind", messages=MESSAGES, seed=7)

    with pytest.raises(openai.BadRequestError) as caught:
        asyncio.run(go())
    assert caught.value.status_code == 400
    assert caught.value.body["code"] == "unsupported_parameter"
    assert "seed" in caught.value.message


def test_a_wrong_key_is_the_sdks_authentication_error(tmp_path):
    async def go():
        client = in_process_client(make_app(healthy_gateway(tmp_path)), key="not-the-token")
        await client.chat.completions.create(model="relaymind", messages=MESSAGES)

    with pytest.raises(openai.AuthenticationError) as caught:
        asyncio.run(go())
    assert caught.value.body["code"] == "unauthenticated"


def test_the_execution_mode_header_reaches_the_run_through_the_sdk(tmp_path):
    gateway = healthy_gateway(tmp_path)

    async def go():
        client = in_process_client(make_app(gateway))
        return await client.chat.completions.create(
            model="relaymind",
            messages=MESSAGES,
            extra_headers={"X-RelayMind-Execution-Mode": "local_only"},
        )

    completion = asyncio.run(go())
    run_id = completion.id.removeprefix("chatcmpl-")
    (accepted,) = of_type(gateway.store.events(run_id), "run.accepted")
    assert accepted.payload["effective_execution_mode"] == "local_only"
    assert gateway.external.requests == []


# -- a real loopback socket ------------------------------------------------------------


class RunningServer:
    def __init__(self, app):
        self.server = build_server(app, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "the server did not start"
            time.sleep(0.01)
        (socket,) = self.server.servers[0].sockets
        self.address = socket.getsockname()
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def base_url(self):
        return f"http://{self.address[0]}:{self.address[1]}/v1"


@pytest.mark.parametrize("stream", [False, True])
def test_the_sdk_round_trip_over_a_loopback_socket(tmp_path, stream):
    """AC09: stream true and false against a bound server."""
    gateway = healthy_gateway(tmp_path)
    with RunningServer(make_app(gateway)) as running:
        assert running.address[0] == "127.0.0.1"
        client = openai.OpenAI(
            api_key=TOKEN,
            base_url=running.base_url,
            http_client=httpx.Client(trust_env=False),
            max_retries=0,
        )
        if stream:
            chunks = list(
                client.chat.completions.create(model="relaymind", messages=MESSAGES, stream=True)
            )
            text = "".join(c.choices[0].delta.content or "" for c in chunks)
            assert chunks[-1].choices[0].finish_reason == "stop"
        else:
            completion = client.chat.completions.create(model="relaymind", messages=MESSAGES)
            text = completion.choices[0].message.content
        assert text == STAGING_OK
        with pytest.raises(openai.AuthenticationError):
            openai.OpenAI(
                api_key="nope",
                base_url=running.base_url,
                http_client=httpx.Client(trust_env=False),
                max_retries=0,
            ).models.list()


def test_serve_refuses_a_public_address(tmp_path):
    app = make_app(healthy_gateway(tmp_path))
    with pytest.raises(ValueError, match="loopback"):
        serve(app, host="0.0.0.0", port=0)
