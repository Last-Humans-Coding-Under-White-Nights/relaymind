"""A compatible chat disconnect cancels the run (RM14, RM17, RM19, RM20, AC12, #92).

Spec 0.24 section 7.4: when the client of POST /v1/chat/completions goes
away before the response is sent, the run the request owns is cancelled
by the same path as the native cancel. A native events disconnect never
cancels a run. The app is driven as raw ASGI so the test decides when
the client leaves; all model output is scripted (spec section 13).
"""

import asyncio
import json

import pytest

from api_helpers import make_app, token_of
from gateway_helpers import build_gateway, health_tail, route_turn
from helpers import of_type
from native_helpers import gated_gateway
from relaymind.adapters import Message, Role
from test_ac12_cancellation import Handler

PROMPT = "Is staging-api healthy right now?"


class Client:
    """One raw ASGI request whose client leaves when the test says so."""

    def __init__(self, app, method, path, body=b""):
        self.gone = asyncio.Event()
        self.sent: list[dict] = []
        self.task: asyncio.Task | None = None
        self._requested = False
        self._scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "server": ("127.0.0.1", 80),
            "client": ("127.0.0.1", 50000),
            "headers": [
                (b"authorization", f"Bearer {token_of(app)}".encode()),
                (b"content-type", b"application/json"),
            ],
        }
        self._app = app
        self._body = body

    async def _receive(self):
        if not self._requested:
            self._requested = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self.gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message):
        self.sent.append(message)

    def start(self):
        self.task = asyncio.ensure_future(self._app(self._scope, self._receive, self._send))
        return self

    async def leave(self):
        self.gone.set()
        await asyncio.wait_for(self.task, timeout=10)


def chat_client(app, *, stream):
    body = json.dumps(
        {"model": "relaymind", "messages": [{"role": "user", "content": PROMPT}], "stream": stream}
    ).encode()
    return Client(app, "POST", "/v1/chat/completions", body)


async def run_ended(gateway, run_id, timeout=10.0):
    """Wait until the run is terminal."""
    deadline = asyncio.get_running_loop().time() + timeout
    while gateway.store.get_run(run_id).state not in ("completed", "failed", "cancelled"):
        assert asyncio.get_running_loop().time() < deadline, "run never ended"
        await asyncio.sleep(0.005)


def cancelled_run(gateway, run_id):
    events = gateway.store.events(run_id)
    assert gateway.store.get_run(run_id).state == "cancelled"
    (cancelled,) = of_type(events, "run.cancelled")
    assert cancelled.payload["reason"] == "cancel_requested"
    assert of_type(events, "run.completed") == []
    assert of_type(events, "answer.delta") == []
    assert gateway.store.active_run_id() is None
    return events


@pytest.mark.parametrize("stream", [False, True])
def test_a_chat_disconnect_cancels_the_run(tmp_path, stream):
    """RM14, RM17, RM19, RM20, AC12: with or without stream, a run that has not
    sent a byte is cancelled; the generation is asked to stop, nothing dispatches."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        client = chat_client(app, stream=stream).start()
        await asyncio.wait_for(adapter.started.wait(), 10)
        run_id = gateway.store.active_run_id()
        await client.leave()
        await run_ended(gateway, run_id)
        return run_id

    run_id = asyncio.run(go())
    cancelled_run(gateway, run_id)
    assert adapter.cancelled == [f"{run_id}_gen1_routing"]
    assert adapter.late_cancels == []


def test_a_chat_disconnect_during_a_read_settles_it_like_a_cancel(tmp_path):
    """RM10, RM14, AC12: an in-flight tool operation is recorded as interrupted, not replayed."""
    gateway = build_gateway(
        tmp_path, [route_turn("check-service-health"), *health_tail()], external_turns=[]
    )
    handler = Handler(gateway, "get_service_health")
    app = make_app(gateway)

    async def go():
        client = chat_client(app, stream=False).start()
        await handler.started()
        run_id = gateway.store.active_run_id()
        await client.leave()
        await run_ended(gateway, run_id)
        await handler.finish()
        return run_id

    run_id = asyncio.run(go())
    events = gateway.store.events(run_id)
    (finished,) = of_type(events, "tool.finished")
    assert finished.payload["result"]["status"] == "failed"
    assert finished.payload["result"]["error"]["code"] == "tool_interrupted"
    assert len(handler.calls) == 1
    assert events[-1].event_type == "run.cancelled"
    assert gateway.store.get_run(run_id).state == "cancelled"


def test_a_chat_request_that_stays_connected_completes(tmp_path):
    """RM19: watching for a disconnect changes nothing for a client that stays."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        client = chat_client(app, stream=False).start()
        await asyncio.wait_for(adapter.started.wait(), 10)
        run_id = gateway.store.active_run_id()
        adapter.gate.set()
        await asyncio.wait_for(client.task, 10)
        return client, run_id

    client, run_id = asyncio.run(go())
    status = next(m["status"] for m in client.sent if m["type"] == "http.response.start")
    assert status == 200
    assert gateway.store.active_run_id() is None
    assert of_type(gateway.store.events(run_id), "run.completed")


def test_a_native_events_disconnect_leaves_the_run_alone(tmp_path):
    """RM21, AC12, section 7.4: only a chat disconnect cancels; an SSE client may leave."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        run = gateway.orchestrator.start((Message(role=Role.USER, content=PROMPT),))
        await asyncio.wait_for(adapter.started.wait(), 10)
        events = Client(app, "GET", f"/relay/v1/runs/{run.run_id}/events").start()
        await asyncio.sleep(0.05)
        await events.leave()
        adapter.gate.set()
        await run_ended(gateway, run.run_id)
        return run.run_id

    run_id = asyncio.run(go())
    assert gateway.store.get_run(run_id).state == "completed"
    assert adapter.cancelled == []
    assert of_type(gateway.store.events(run_id), "run.cancelled") == []
