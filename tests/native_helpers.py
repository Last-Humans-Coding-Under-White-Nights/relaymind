"""Helpers for the native run API tests (RM21, spec 0.17 section 7.4).

A native run continues in the background of the event loop that
accepted it, so these tests drive the app inside one event loop: one
``session`` per test, many requests. ``settled`` waits for a run to
reach a state a client would act on.
"""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx

from api_helpers import token_of
from gateway_helpers import build_gateway, health_tail, route_turn
from helpers import ScriptedAdapter
from relaymind.orchestrator.local_loop import ConfiguredModel

SETTLED = frozenset({"completed", "failed", "cancelled", "waiting_input", "waiting_confirmation"})


@asynccontextmanager
async def session(app, *, auth=True):
    """An authenticated in-process client; every request shares the running loop."""
    headers = {"Authorization": f"Bearer {token_of(app)}"} if auth else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", headers=headers
    ) as client:
        yield client


async def start_run(client, prompt="Is staging-api healthy right now?", *, headers=None, **fields):
    return await client.post(
        "/relay/v1/runs",
        json={"messages": [{"role": "user", "content": prompt}], **fields},
        headers=headers,
    )


async def settled(client, run_id, states=SETTLED, timeout=5.0):
    """Poll GET /relay/v1/runs/{id} until the run is in one of states; return the view."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await client.get(f"/relay/v1/runs/{run_id}")
        assert response.status_code == 200, response.text
        view = response.json()
        if view["state"] in states:
            return view
        assert asyncio.get_running_loop().time() < deadline, f"run still {view['state']}"
        await asyncio.sleep(0.005)


async def run_to_rest(client, prompt="Is staging-api healthy right now?", **fields):
    """Start a run and wait until it completes, fails, is cancelled, or waits."""
    accepted = await start_run(client, prompt, **fields)
    assert accepted.status_code == 202, accepted.text
    return accepted.json(), await settled(client, accepted.json()["run_id"])


def error_of(response):
    body = response.json()
    assert list(body) == ["error"]
    return body["error"]


def parse_sse(text):
    """The messages of an SSE body: dicts with id, event, data; comments are skipped."""
    messages = []
    for block in text.split("\n\n"):
        fields = {}
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            name, _, value = line.partition(": ")
            fields[name] = value
        if fields:
            messages.append(
                {
                    "id": int(fields["id"]),
                    "event": fields["event"],
                    "data": json.loads(fields["data"]),
                }
            )
    return messages


class GatedAdapter(ScriptedAdapter):
    """A scripted model whose generations wait for a gate (a run held in flight)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    async def generate(self, request):
        self.started.set()
        await self.gate.wait()
        async for event in super().generate(request):
            yield event


def gated_gateway(tmp_path):
    """A health run whose local model waits for adapter.gate, so the run stays in flight."""
    adapter = GatedAdapter(turns=[route_turn("check-service-health"), *health_tail()])
    gateway = build_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="gated"),
        external_turns=[],
    )
    return gateway, adapter
