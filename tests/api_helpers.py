"""Helpers for the HTTP API tests: an app over a gateway double and an in-process client.

The client calls the ASGI app directly (no socket), so every API test
is deterministic. One real loopback socket test lives in
``tests/test_openai_sdk.py``.
"""

import asyncio
import json
import threading
import time

import httpx

from gateway_helpers import build_gateway, health_tail, route_turn
from relaymind.api.app import create_app
from relaymind.api.server import build_server

RUN_ID_HEADER = "X-RelayMind-Run-ID"


def healthy_gateway(tmp_path, **options):
    """A gateway whose local model routes to check-service-health and answers."""
    turns = [route_turn("check-service-health"), *health_tail()]
    return build_gateway(tmp_path, turns, external_turns=[], **options)


def make_app(gateway, **options):
    return create_app(orchestrator=gateway.orchestrator, **options)


def token_of(app):
    """The application token the app generated at startup (section 7.1)."""
    return app.state.application_token


def call(
    app,
    method,
    path,
    *,
    json_body=None,
    content=None,
    headers=None,
    auth=True,
    raise_app_exceptions=True,
):
    """One request to the app; returns the httpx response.

    With raise_app_exceptions false, the bytes sent before the app
    failed are returned instead of the exception.
    """
    own = {"Authorization": f"Bearer {token_of(app)}"} if auth is True else {}
    merged = {**own, **(headers or {})}
    if isinstance(auth, str):
        merged["Authorization"] = auth

    async def go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.request(
                method, path, json=json_body, content=content, headers=merged
            )

    return asyncio.run(go())


def chat_body(*, model="relaymind", prompt="Is staging-api healthy right now?", **fields):
    """The body of one chat completion with a single user message."""
    return {"model": model, "messages": [{"role": "user", "content": prompt}], **fields}


def chat(
    app,
    model="relaymind",
    prompt="Is staging-api healthy right now?",
    raise_app_exceptions=True,
    **fields,
):
    headers = fields.pop("headers", None)
    body = chat_body(model=model, prompt=prompt, **fields)
    return call(
        app,
        "POST",
        "/v1/chat/completions",
        json_body=body,
        headers=headers,
        raise_app_exceptions=raise_app_exceptions,
    )


def sse_events(response):
    """The data lines of an SSE body: JSON objects, and the literal [DONE]."""
    assert response.headers["content-type"].startswith("text/event-stream")
    events = []
    for block in response.text.split("\n\n"):
        if not block.strip():
            continue
        assert block.startswith("data: "), block
        data = block[len("data: ") :]
        events.append(data if data == "[DONE]" else json.loads(data))
    return events


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
    def origin(self):
        return f"http://{self.address[0]}:{self.address[1]}"

    @property
    def base_url(self):
        return self.origin + "/v1"
