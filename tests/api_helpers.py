"""Helpers for the HTTP API tests: an app over a gateway double and an in-process client.

The client calls the ASGI app directly (no socket), so every API test
is deterministic. One real loopback socket test lives in
``tests/test_openai_sdk.py``.
"""

import asyncio
import json

import httpx

from gateway_helpers import build_gateway, health_tail, route_turn
from relaymind.api.app import create_app

TOKEN = "test-token-" + "x" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RUN_ID_HEADER = "X-RelayMind-Run-ID"


def healthy_gateway(tmp_path, **options):
    """A gateway whose local model routes to check-service-health and answers."""
    turns = [route_turn("check-service-health"), *health_tail()]
    return build_gateway(tmp_path, turns, external_turns=[], **options)


def make_app(gateway, **options):
    options.setdefault("token", TOKEN)
    return create_app(orchestrator=gateway.orchestrator, **options)


def call(app, method, path, *, json_body=None, content=None, headers=None, auth=True):
    """One request to the app; returns the httpx response."""
    merged = {**(AUTH if auth is True else {}), **(headers or {})}
    if isinstance(auth, str):
        merged["Authorization"] = auth

    async def go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.request(
                method, path, json=json_body, content=content, headers=merged
            )

    return asyncio.run(go())


def chat(app, model="relaymind", prompt="Is staging-api healthy right now?", **fields):
    headers = fields.pop("headers", None)
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], **fields}
    return call(app, "POST", "/v1/chat/completions", json_body=body, headers=headers)


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
