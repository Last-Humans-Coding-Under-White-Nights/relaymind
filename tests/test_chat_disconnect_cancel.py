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
from gateway_helpers import build_gateway, health_tail, route_turn, tool_turn
from helpers import of_type
from native_helpers import GatedAdapter, gated_gateway
from relaymind.adapters import Message, Role
from relaymind.api.chat import ClientToolsChat
from relaymind.api.chat_request import CLIENT_TOOLS
from relaymind.api.native import NativeSettings
from relaymind.orchestrator.local_loop import ConfiguredModel
from test_ac12_cancellation import Handler
from test_client_owned_tools import WEATHER
from test_streaming_contract import LiveBackend

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


def chat_client(app, *, stream, key=None, **fields):
    body = json.dumps(
        {
            "model": "relaymind",
            "messages": [{"role": "user", "content": PROMPT}],
            "stream": stream,
            **fields,
        }
    ).encode()
    client = Client(app, "POST", "/v1/chat/completions", body)
    if key is not None:
        client._scope["headers"].append((b"idempotency-key", key.encode()))
    return client


def response_of(client):
    """The status and the decoded body of a response that was not a stream."""
    status = next(m["status"] for m in client.sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in client.sent if m["type"] == "http.response.body")
    return status, json.loads(body)


async def until(predicate, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never held"
        await asyncio.sleep(0.005)


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
        await until(lambda: any(m["type"] == "http.response.body" for m in events.sent))
        await events.leave()
        adapter.gate.set()
        await run_ended(gateway, run.run_id)
        return run.run_id

    run_id = asyncio.run(go())
    assert gateway.store.get_run(run_id).state == "completed"
    assert adapter.cancelled == []
    assert of_type(gateway.store.events(run_id), "run.cancelled") == []


def test_a_chat_disconnect_names_the_cancelled_run(tmp_path):
    """RM16, RM19, section 11: the error of a cancelled request carries its run ID."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        client = chat_client(app, stream=False).start()
        await asyncio.wait_for(adapter.started.wait(), 10)
        run_id = gateway.store.active_run_id()
        await client.leave()
        return client, run_id

    client, run_id = asyncio.run(go())
    status, body = response_of(client)
    assert status == 409
    assert body["error"]["code"] == "run_terminal"
    assert body["error"]["run_id"] == run_id


def test_a_chat_disconnect_cancels_a_client_owned_run(tmp_path):
    """RM08, RM14, RM19, AC12: the client-owned alias run is cancelled the same way."""
    adapter = GatedAdapter(turns=[tool_turn("get_weather", call_id="call_w1", city="Oslo")])
    gateway = build_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="gated"),
        external_turns=[],
    )
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))

    async def go():
        client = chat_client(app, stream=False, model=CLIENT_TOOLS, tools=[WEATHER]).start()
        await asyncio.wait_for(adapter.started.wait(), 10)
        run_id = gateway.store.active_run_id()
        await client.leave()
        await run_ended(gateway, run_id)
        return run_id

    run_id = asyncio.run(go())
    cancelled_run(gateway, run_id)
    assert len(adapter.cancelled) == 1
    assert adapter.cancelled[0].startswith(f"{run_id}_gen1")
    assert adapter.late_cancels == []


class Hanging:
    """A backend double that owns no orchestrator run and waits until cancelled."""

    def __init__(self, *, ignore_cancels=0):
        self.cancels = 0
        self.ignore_cancels = ignore_cancels
        self.release = asyncio.Event()

    async def complete(self, request, mode):
        while True:
            try:
                await self.release.wait()
                raise RuntimeError("released")
            except asyncio.CancelledError:
                self.cancels += 1
                if self.cancels > self.ignore_cancels:
                    raise


def test_a_chat_disconnect_cancels_a_backend_that_owns_no_run(tmp_path):
    """RM14, RM19, section 7.4: the request task is cancelled even when no run is registered."""
    gateway, _ = gated_gateway(tmp_path)
    backend = Hanging()
    app = make_app(gateway, client_tools=backend)

    async def go():
        client = chat_client(app, stream=False, model=CLIENT_TOOLS, tools=[WEATHER]).start()
        await asyncio.sleep(0.01)
        await client.leave()
        return client

    client = asyncio.run(go())
    assert backend.cancels == 1
    assert response_of(client)[0] == 409


def test_a_chat_disconnect_waits_for_the_cancel_only_so_long(tmp_path):
    """RM03, RM14, spec 0.17 section 7.4: the cancel wait is bounded, as for the native cancel."""
    gateway, _ = gated_gateway(tmp_path)
    backend = Hanging(ignore_cancels=1)
    app = make_app(gateway, client_tools=backend, native=NativeSettings(cancel_wait_seconds=0.05))

    async def go():
        client = chat_client(app, stream=False, model=CLIENT_TOOLS, tools=[WEATHER]).start()
        await asyncio.sleep(0.01)
        await client.leave()
        return client

    client = asyncio.run(go())
    assert backend.cancels == 2
    assert response_of(client)[0] == 409


def test_a_chat_request_cancelled_by_the_server_asks_the_adapter_to_stop(tmp_path):
    """RM14, AC12, spec 0.19 section 7.4: a request cancelled from outside (a server
    shutdown) still asks the adapter first, and the run ends as cancelled."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)

    async def go():
        client = chat_client(app, stream=False).start()
        await asyncio.wait_for(adapter.started.wait(), 10)
        run_id = gateway.store.active_run_id()
        client.task.cancel()
        await asyncio.wait({client.task}, timeout=10)
        await run_ended(gateway, run_id)
        return run_id

    run_id = asyncio.run(go())
    cancelled_run(gateway, run_id)
    assert adapter.cancelled == [f"{run_id}_gen1_routing"]


def test_a_chat_disconnect_while_a_live_answer_is_collected_closes_it(tmp_path):
    """RM14, RM20, spec 0.24 section 7.4: stream false still watches the client while
    a live answer is collected; leaving closes the provider stream."""
    gateway, _ = gated_gateway(tmp_path)
    backend = LiveBackend(["Hel"], hang=True)
    app = make_app(gateway, client_tools=backend)

    async def go():
        client = chat_client(app, stream=False, model=CLIENT_TOOLS, tools=[WEATHER]).start()
        await until(lambda: backend.delivered == ["Hel"])
        await client.leave()
        return client

    client = asyncio.run(go())
    assert backend.closed
    assert backend.failed == []
    assert response_of(client)[0] == 409


def test_a_failing_receive_is_not_taken_as_a_disconnect(tmp_path):
    """RM19, section 7.4: only http.disconnect cancels; a receive that fails leaves the run."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)

    class Broken(Client):
        async def _receive(self):
            if not self._requested:
                return await super()._receive()
            raise RuntimeError("receive failed")

    async def go():
        body = json.dumps(
            {"model": "relaymind", "messages": [{"role": "user", "content": PROMPT}]}
        ).encode()
        client = Broken(app, "POST", "/v1/chat/completions", body).start()
        await asyncio.wait_for(adapter.started.wait(), 10)
        run_id = gateway.store.active_run_id()
        await asyncio.sleep(0.01)
        adapter.gate.set()
        await asyncio.wait_for(client.task, 10)
        return client, run_id

    client, run_id = asyncio.run(go())
    assert response_of(client)[0] == 200
    assert gateway.store.get_run(run_id).state == "completed"
    assert adapter.cancelled == []


def test_a_disconnect_after_a_stored_reply_changes_nothing(tmp_path):
    """RM17, RM21, AC11, section 7.4: a request answered from a stored Idempotency-Key,
    and a run that already ended, are left as they are when the client leaves."""
    gateway, adapter = gated_gateway(tmp_path)
    app = make_app(gateway)
    adapter.gate.set()

    async def go():
        first = chat_client(app, stream=False, key="k-92").start()
        await asyncio.wait_for(first.task, 10)
        await first.leave()
        replay = chat_client(app, stream=False, key="k-92").start()
        await asyncio.wait_for(replay.task, 10)
        await replay.leave()
        return first, replay

    first, replay = asyncio.run(go())
    (status, body), (replay_status, replay_body) = response_of(first), response_of(replay)
    assert status == replay_status == 200
    assert replay_body["choices"] == body["choices"]
    run_id = body["id"].removeprefix("chatcmpl-")
    assert gateway.store.get_run(run_id).state == "completed"
    assert of_type(gateway.store.events(run_id), "run.cancelled") == []
    assert adapter.cancelled == []
