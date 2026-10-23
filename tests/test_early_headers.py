"""Early response headers and keep-alives for streamed chat requests (RM32, #140).

Spec 0.32 section 7.3: under performance.early_headers a streamed chat
request gets its status line, X-RelayMind-Run-ID, and
X-RelayMind-Events-URL as soon as the run is accepted, then an SSE
comment keep-alive while the run works, and no content before the
answer (RM20). Every check that can fail a request (401, 400, 403, a
stored idempotency key, the 429 slot) still runs before a header is
sent. A run that ends without an answer ends the stream with the
mid-stream error event, because the status line is gone. A disconnect
cancels the run as before (RM14, AC12). Deterministic with model
doubles; the app is driven as raw ASGI so the test decides when the
client leaves and what it has received so far.
"""

import asyncio
import json
import threading

import httpx
import pytest

from api_helpers import (
    RUN_ID_HEADER,
    RunningServer,
    call,
    chat,
    chat_body,
    make_app,
    sse_events,
    token_of,
)
from gateway_helpers import STAGING_OK, build_gateway, health_tail, route_turn
from helpers import ScriptedAdapter, of_type
from native_helpers import GatedAdapter
from relaymind.adapters import GenerationFailed, Message, Role
from relaymind.api.chat import StreamAborted
from relaymind.api.idempotency import REPLAYED_HEADER
from relaymind.api.native import NativeSettings
from relaymind.orchestrator.local_loop import ConfiguredModel
from relaymind.orchestrator.performance import PerformanceSettings
from test_chat_disconnect_cancel import (
    PROMPT,
    cancelled_run,
    chat_client,
    run_ended,
    until,
)

EARLY = PerformanceSettings(early_headers=True)
EVENTS_URL_HEADER = "X-RelayMind-Events-URL"
FAST = NativeSettings(heartbeat_seconds=0.01)
HEALTH_TURNS = [route_turn("check-service-health"), *health_tail()]


def gated_early(tmp_path, performance=EARLY, **options):
    """A health run whose local model waits for adapter.gate, with early headers by default."""
    adapter = GatedAdapter(turns=HEALTH_TURNS)
    gateway = build_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="gated"),
        external_turns=[],
        performance=performance,
        **options,
    )
    return gateway, adapter


def head(client):
    """The response start of a raw client: status and headers by lower-case name."""
    for message in client.sent:
        if message["type"] == "http.response.start":
            headers = {k.decode().lower(): v.decode() for k, v in message["headers"]}
            return message["status"], headers
    return None


def body_text(client):
    return b"".join(m.get("body", b"") for m in client.sent if m["type"] == "http.response.body")


def blocks(client):
    """The SSE blocks received so far: comment lines and data events, in order."""
    return [b for b in body_text(client).decode().split("\n\n") if b.strip()]


def comments(client):
    return [b for b in blocks(client) if b.startswith(":")]


def data_events(client):
    out = []
    for block in blocks(client):
        if block.startswith("data: "):
            payload = block[len("data: ") :]
            out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


def shape(events):
    """Stream events without the run-specific id and the clock."""
    return [e if isinstance(e, str) else {**e, "id": "chatcmpl-X", "created": 0} for e in events]


def buffered_stream(tmp_path, turns=HEALTH_TURNS, **options):
    """The stream the default configuration sends for the same script."""
    gateway = build_gateway(tmp_path / "base", turns, external_turns=[], **options)
    response = chat(make_app(gateway), stream=True)
    assert response.status_code == 200
    return shape(sse_events(response))


# -- the headers come first ------------------------------------------------------------------


def test_the_headers_and_keep_alives_arrive_before_the_run_finishes(tmp_path):
    """RM32, RM20: status, run ID, and events URL at acceptance, comments while it works."""
    gateway, adapter = gated_early(tmp_path)
    app = make_app(gateway, native=FAST)

    async def go():
        client = chat_client(app, stream=True).start()
        await asyncio.wait_for(adapter.started.wait(), 10)
        await until(lambda: head(client) is not None and len(comments(client)) >= 2)
        run_id = gateway.store.active_run_id()
        early = (head(client), data_events(client), list(comments(client)))
        state_while_waiting = gateway.store.get_run(run_id).state
        adapter.gate.set()
        await asyncio.wait_for(client.task, 10)
        return client, run_id, early, state_while_waiting

    client, run_id, early, state = asyncio.run(go())
    (status, headers), seen_data, seen_comments = early
    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert headers[RUN_ID_HEADER.lower()] == run_id
    assert headers[EVENTS_URL_HEADER.lower()] == f"/relay/v1/runs/{run_id}/events"
    assert headers["cache-control"] == "no-cache"
    # The run was still working, and nothing but comments had been sent.
    assert state in ("routing", "local_running")
    assert seen_data == []
    assert all(c.startswith(":") for c in seen_comments)
    # After the run: exactly the chunks of a buffered response, in the same order (RM20).
    assert shape(data_events(client)) == buffered_stream(tmp_path)
    assert gateway.store.get_run(run_id).state == "completed"
    assert gateway.external.requests == []


def test_a_progress_client_can_open_the_events_url_the_header_names(tmp_path):
    """RM21, RM32: the header names the native stream of the run, with the same token."""
    gateway, adapter = gated_early(tmp_path)
    app = make_app(gateway, native=FAST)

    async def go():
        client = chat_client(app, stream=True).start()
        await asyncio.wait_for(adapter.started.wait(), 10)
        await until(lambda: head(client) is not None)
        url = head(client)[1][EVENTS_URL_HEADER.lower()]
        adapter.gate.set()
        await asyncio.wait_for(client.task, 10)
        return url

    url = asyncio.run(go())
    response = call(app, "GET", url)
    assert response.status_code == 200
    assert "run.completed" in response.text


def test_the_default_sends_nothing_until_the_run_finishes(tmp_path):
    """RM32: false, the default, is the latency baseline; no early header, no keep-alive."""
    gateway, adapter = gated_early(tmp_path, performance=PerformanceSettings())
    app = make_app(gateway, native=FAST)

    async def go():
        client = chat_client(app, stream=True).start()
        await asyncio.wait_for(adapter.started.wait(), 10)
        await asyncio.sleep(0.1)
        before = list(client.sent)
        adapter.gate.set()
        await asyncio.wait_for(client.task, 10)
        return client, before

    client, before = asyncio.run(go())
    assert before == []
    status, headers = head(client)
    assert status == 200
    assert EVENTS_URL_HEADER.lower() not in headers
    assert RUN_ID_HEADER.lower() in headers
    assert comments(client) == []
    assert shape(data_events(client)) == buffered_stream(tmp_path)


def test_a_request_without_stream_is_unchanged(tmp_path):
    """RM32: early headers apply to a streamed request only."""
    gateway = build_gateway(tmp_path, HEALTH_TURNS, external_turns=[], performance=EARLY)
    response = chat(make_app(gateway), stream=False)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == STAGING_OK
    assert EVENTS_URL_HEADER not in response.headers
    assert RUN_ID_HEADER in response.headers


# -- every check still runs before a header is sent -----------------------------------------


def early_app(tmp_path, **options):
    gateway = build_gateway(tmp_path, HEALTH_TURNS, external_turns=[], performance=EARLY, **options)
    return gateway, make_app(gateway)


def test_a_missing_token_is_401_before_any_run(tmp_path):
    """RM19: authentication comes first."""
    gateway, app = early_app(tmp_path)
    response = chat(app, stream=True, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert EVENTS_URL_HEADER not in response.headers
    assert gateway.store.active_run_id() is None
    assert gateway.adapter.requests == []


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"model": "relaymind", "messages": [], "stream": True}, 400),
        ({"model": "nope", "messages": [{"role": "user", "content": "x"}], "stream": True}, 400),
    ],
)
def test_an_invalid_body_is_400_before_any_run(tmp_path, body, status):
    gateway, app = early_app(tmp_path)
    response = call(app, "POST", "/v1/chat/completions", json_body=body)
    assert response.status_code == status
    assert EVENTS_URL_HEADER not in response.headers
    assert RUN_ID_HEADER not in response.headers
    assert gateway.store.active_run_id() is None


def test_a_forbidden_mode_is_403_before_any_run(tmp_path):
    """RM19: the operator policy is checked before the run is accepted."""
    from relaymind.api.app import ChatSettings
    from relaymind.orchestrator.route_decision import ExecutionMode

    gateway = build_gateway(tmp_path, HEALTH_TURNS, external_turns=[], performance=EARLY)
    app = make_app(gateway, settings=ChatSettings(allowed_modes=frozenset({ExecutionMode.AUTO})))
    response = chat(app, stream=True, headers={"X-RelayMind-Execution-Mode": "external_only"})
    assert response.status_code == 403
    assert EVENTS_URL_HEADER not in response.headers
    assert gateway.store.active_run_id() is None


def test_a_busy_slot_is_429_before_a_header_is_sent(tmp_path):
    """RM17, section 11: the slot is taken at acceptance, so a second request is 429, not queued."""
    gateway, adapter = gated_early(tmp_path)
    app = make_app(gateway, native=FAST)

    async def go():
        first = chat_client(app, stream=True).start()
        await asyncio.wait_for(adapter.started.wait(), 10)
        await until(lambda: head(first) is not None)
        second = chat_client(app, stream=True).start()
        await asyncio.wait_for(second.task, 10)
        adapter.gate.set()
        await asyncio.wait_for(first.task, 10)
        return first, second

    first, second = asyncio.run(go())
    status, headers = head(second)
    assert status == 429
    assert EVENTS_URL_HEADER.lower() not in headers
    assert json.loads(body_text(second))["error"]["code"] == "overloaded"
    assert head(first)[0] == 200


def test_an_idempotent_replay_is_answered_from_the_stored_run(tmp_path):
    """RM17, spec 0.18: a duplicate key runs nothing and sends no early header."""
    gateway = build_gateway(tmp_path, HEALTH_TURNS, external_turns=[], performance=EARLY)
    app = make_app(gateway, native=FAST)

    async def go():
        first = chat_client(app, stream=True, key="key-early-1").start()
        await asyncio.wait_for(first.task, 10)
        second = chat_client(app, stream=True, key="key-early-1").start()
        await asyncio.wait_for(second.task, 10)
        return first, second

    first, second = asyncio.run(go())
    assert EVENTS_URL_HEADER.lower() in head(first)[1]
    status, replay_headers = head(second)
    assert status == 200
    assert replay_headers[RUN_ID_HEADER.lower()] == head(first)[1][RUN_ID_HEADER.lower()]
    assert replay_headers[REPLAYED_HEADER.lower()] == "true"
    assert EVENTS_URL_HEADER.lower() not in replay_headers
    assert len(gateway.adapter.requests) == 4
    assert shape(data_events(second)) == shape(data_events(first))


# -- a run that ends without an answer ----------------------------------------------------------


def test_a_needs_external_run_ends_with_the_error_event(tmp_path):
    """RM04, RM32, section 7.3: no status change is possible; the event carries code and reason."""
    gateway = build_gateway(
        tmp_path,
        [route_turn("investigate-performance")],
        external_turns=None,
        performance=EARLY,
    )
    app = make_app(gateway, native=FAST)

    async def go():
        client = chat_client(app, stream=True).start()
        with pytest.raises(StreamAborted):
            await asyncio.wait_for(client.task, 10)
        return client

    client = asyncio.run(go())
    assert head(client)[0] == 200
    events = data_events(client)
    assert len(events) == 1
    error = events[0]["error"]
    run_id = head(client)[1][RUN_ID_HEADER.lower()]
    assert error["code"] == "needs_external"
    assert error["reason_code"] == "complex_skill"
    assert error["run_id"] == run_id
    assert isinstance(error["message"], str) and error["message"]
    # No finish chunk and no [DONE]: the connection ends (RM20).
    assert "[DONE]" not in body_text(client).decode()
    assert "finish_reason" not in body_text(client).decode()
    record = gateway.store.get_run(run_id)
    assert (record.state, record.error_code) == ("failed", "needs_external")
    assert gateway.store.active_run_id() is None
    assert of_type(gateway.store.events(run_id), "answer.delta") == []


def test_an_external_failure_ends_with_the_error_event(tmp_path):
    """RM20: a failed external stage is the same error event, never a replacement answer."""
    external = ScriptedAdapter(
        turns=[[GenerationFailed(code="unreachable", message="no route to host")]],
        locality="external",
    )
    gateway = build_gateway(
        tmp_path,
        [route_turn("investigate-performance")],
        external_model=ConfiguredModel(adapter=external, model_id="ext"),
        performance=EARLY,
    )
    app = make_app(gateway, native=FAST)

    async def go():
        client = chat_client(app, stream=True).start()
        with pytest.raises(StreamAborted):
            await asyncio.wait_for(client.task, 10)
        return client

    client = asyncio.run(go())
    (event,) = data_events(client)
    assert event["error"]["code"] == "external_failed"
    assert event["error"]["run_id"] == head(client)[1][RUN_ID_HEADER.lower()]


def test_a_clarification_is_an_answer_and_streams_normally(tmp_path):
    """Section 5: a clarification is assistant content with finish_reason stop, not an error."""
    from gateway_helpers import clarify_turn

    gateway = build_gateway(
        tmp_path, [clarify_turn("Which service do you mean?")], external_turns=[], performance=EARLY
    )
    app = make_app(gateway, native=FAST)

    async def go():
        client = chat_client(app, stream=True).start()
        await asyncio.wait_for(client.task, 10)
        return client

    client = asyncio.run(go())
    events = data_events(client)
    assert events[-1] == "[DONE]"
    text = "".join(
        e["choices"][0]["delta"].get("content") or "" for e in events if isinstance(e, dict)
    )
    assert text == "Which service do you mean?"


# -- a disconnect cancels the run as before ------------------------------------------------------


def test_a_disconnect_after_the_headers_cancels_the_run(tmp_path):
    """RM14, RM17, AC12: the client leaves while only comments were sent; the run is cancelled."""
    gateway, adapter = gated_early(tmp_path)
    app = make_app(gateway, native=FAST)

    async def go():
        client = chat_client(app, stream=True).start()
        await asyncio.wait_for(adapter.started.wait(), 10)
        await until(lambda: head(client) is not None)
        run_id = gateway.store.active_run_id()
        await client.leave()
        await run_ended(gateway, run_id)
        return run_id, client

    run_id, client = asyncio.run(go())
    cancelled_run(gateway, run_id)
    assert adapter.cancelled == [f"{run_id}_gen1_routing"]
    assert adapter.late_cancels == []
    assert data_events(client) == []


class _Stubborn(GatedAdapter):
    """A local model that, once cancelled, stops only when release is set."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = asyncio.Event()

    async def generate(self, request):
        self.started.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            await self.release.wait()
            raise
        async for event in super().generate(request):
            yield event


def stubborn_early(tmp_path, native):
    adapter = _Stubborn(turns=HEALTH_TURNS)
    gateway = build_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="stubborn"),
        external_turns=[],
        performance=EARLY,
    )
    return gateway, adapter, make_app(gateway, native=native)


def test_a_disconnect_releases_the_slot_before_the_connection_is_freed(tmp_path):
    """RM14, RM17, AC12, section 7.4: the early stream waits for the run to stop, as the
    buffered disconnect does, so the next request gets the single slot, not 429."""
    gateway, adapter, app = stubborn_early(tmp_path, FAST)

    async def go():
        first = chat_client(app, stream=True).start()
        await asyncio.wait_for(adapter.started.wait(), 10)
        await until(lambda: head(first) is not None)
        run_id = gateway.store.active_run_id()
        # The model takes a moment to stop, well inside the cancel wait.
        asyncio.get_running_loop().call_later(0.05, adapter.release.set)
        await first.leave()
        # No yield between the freed connection and these checks.
        assert gateway.store.get_run(run_id).state == "cancelled"
        assert gateway.store.active_run_id() is None
        second = chat_client(app, stream=True).start()
        await until(lambda: head(second) is not None)
        status = head(second)[0]
        adapter.gate.set()
        await asyncio.wait_for(second.task, 10)
        return run_id, status

    run_id, status = asyncio.run(go())
    cancelled_run(gateway, run_id)
    assert status == 200


def test_the_disconnect_wait_is_bounded_by_the_native_cancel_wait(tmp_path):
    """RM03, RM14, section 7.4: a run that does not stop holds the connection only
    native.cancel_wait_seconds, as the buffered disconnect and the native cancel do."""
    native = NativeSettings(heartbeat_seconds=0.01, cancel_wait_seconds=0.2)
    gateway, adapter, app = stubborn_early(tmp_path, native)

    async def go():
        loop = asyncio.get_running_loop()
        client = chat_client(app, stream=True).start()
        await asyncio.wait_for(adapter.started.wait(), 10)
        await until(lambda: head(client) is not None)
        run_id = gateway.store.active_run_id()
        left = loop.time()
        await client.leave()
        held = loop.time() - left
        still_active = gateway.store.active_run_id() == run_id
        adapter.release.set()
        await run_ended(gateway, run_id)
        return run_id, held, still_active

    run_id, held, still_active = asyncio.run(go())
    assert still_active, "the run stopped before the wait ran out"
    assert 0.15 <= held < 5
    cancelled_run(gateway, run_id)


def test_a_task_cancelled_before_it_starts_releases_the_slot(tmp_path):
    """RM17: a run that was accepted and never driven still ends, cancelled."""
    gateway = build_gateway(tmp_path, HEALTH_TURNS, external_turns=[], performance=EARLY)
    orchestrator = gateway.orchestrator

    async def go():
        accepted = orchestrator.accept((Message(role=Role.USER, content=PROMPT),))
        assert gateway.store.get_run(accepted.run_id).state == "routing"
        assert gateway.store.active_run_id() == accepted.run_id
        task = accepted.start(lambda outcome: outcome)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return accepted.run_id

    run_id = asyncio.run(go())
    assert gateway.store.get_run(run_id).state == "cancelled"
    assert gateway.store.active_run_id() is None
    assert gateway.adapter.requests == []


def test_a_cancel_before_the_task_first_runs_maps_to_the_run(tmp_path):
    """RM14, RM17, AC12: start() registers the task as the run's owner at once.

    A disconnect before the task first ran still names the run and ends
    it as cancelled with cancel_requested; the mapping is gone afterwards.
    """
    gateway = build_gateway(tmp_path, HEALTH_TURNS, external_turns=[], performance=EARLY)
    orchestrator = gateway.orchestrator

    async def go():
        accepted = orchestrator.accept((Message(role=Role.USER, content=PROMPT),))
        task = accepted.start(lambda outcome: outcome)
        # No yield between start() and the cancel: the task has not run yet.
        assert orchestrator.request_cancel(task) == accepted.run_id
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task not in orchestrator._owned
        return accepted.run_id

    run_id = asyncio.run(go())
    cancelled_run(gateway, run_id)
    assert gateway.store.active_run_id() is None
    assert gateway.adapter.requests == []
    assert orchestrator._owned == {}


def test_a_finished_task_leaves_no_ownership_behind(tmp_path):
    """RM14: the task-to-run mapping ends with the task that drove the run."""
    gateway = build_gateway(tmp_path, HEALTH_TURNS, external_turns=[], performance=EARLY)
    orchestrator = gateway.orchestrator

    async def go():
        accepted = orchestrator.accept((Message(role=Role.USER, content=PROMPT),))
        task = accepted.start(lambda outcome: outcome)
        assert orchestrator._owned.get(task) is not None
        await task
        return accepted.run_id

    run_id = asyncio.run(go())
    assert gateway.store.get_run(run_id).state == "completed"
    assert orchestrator._owned == {}


# -- the client-owned alias ---------------------------------------------------------------------


def test_the_client_owned_alias_streams_with_early_headers_too(tmp_path):
    """RM08, RM32: the same headers; the function call arrives in the closing chunks."""
    from relaymind.api.chat import ClientToolsChat
    from test_client_owned_tools import WEATHER, weather_call

    gateway = build_gateway(tmp_path, [weather_call()], external_turns=[], performance=EARLY)
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator), native=FAST)

    async def go():
        client = chat_client(app, stream=True, model="relaymind-client-tools", tools=[WEATHER])
        client.start()
        await asyncio.wait_for(client.task, 10)
        return client

    client = asyncio.run(go())
    status, headers = head(client)
    assert status == 200
    run_id = headers[RUN_ID_HEADER.lower()]
    assert headers[EVENTS_URL_HEADER.lower()] == f"/relay/v1/runs/{run_id}/events"
    events = data_events(client)
    assert events[-1] == "[DONE]"
    finish = events[-2]["choices"][0]["finish_reason"]
    assert finish == "tool_calls"
    calls = [
        c
        for e in events
        if isinstance(e, dict)
        for c in e["choices"][0]["delta"].get("tool_calls", [])
    ]
    assert [c["function"]["name"] for c in calls] == ["get_weather"]


# -- over a real socket -------------------------------------------------------------------------


class ThreadGatedAdapter(ScriptedAdapter):
    """A model double held by a thread event, so a test outside the server's loop can open it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gate = threading.Event()
        self.started = threading.Event()

    async def generate(self, request):
        self.started.set()
        await asyncio.get_running_loop().run_in_executor(None, self.gate.wait, 10)
        async for event in super().generate(request):
            yield event


def test_the_headers_reach_a_real_client_before_the_run_finishes(tmp_path):
    """RM32: over a loopback socket the status line and headers are flushed at acceptance."""
    adapter = ThreadGatedAdapter(turns=HEALTH_TURNS)
    gateway = build_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="gated"),
        external_turns=[],
        performance=EARLY,
    )
    app = make_app(gateway, native=FAST)
    with RunningServer(app) as server:
        with httpx.Client(
            base_url=server.origin,
            headers={"Authorization": f"Bearer {token_of(app)}"},
            timeout=10,
        ) as client:
            body = chat_body(stream=True)
            with client.stream("POST", "/v1/chat/completions", json=body) as response:
                # The call returned with the run held by the gate: headers came first.
                assert response.status_code == 200
                assert not adapter.gate.is_set()
                run_id = response.headers[RUN_ID_HEADER]
                assert response.headers[EVENTS_URL_HEADER] == f"/relay/v1/runs/{run_id}/events"
                assert gateway.store.get_run(run_id).state in ("routing", "local_running")
                adapter.gate.set()
                lines = [line for line in response.iter_lines() if line]
    assert lines[-1] == "data: [DONE]"
    text = "".join(
        json.loads(line[len("data: ") :])["choices"][0]["delta"].get("content") or ""
        for line in lines
        if line.startswith("data: {")
    )
    assert text == STAGING_OK
