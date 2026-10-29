"""OpenClaw cancellation, errors, retries, and run trace correlation (#138).

RM12, RM14, RM16, RM17, RM20, RM21, RM22, RM25, RM33; AC11, AC12, AC24;
spec sections 7.3 to 7.5, 8, and 11.

The Node driver ``integrations/openclaw/test/plugin-transport/drive-plugin.mjs``
plays the host around the plugin, through the pinned openai-completions
transport wrapped by the provider's own ``wrapStreamFn``, against the real
gateway over scripted model doubles on a loopback socket (spec section 13).
The driver's abort options stand in for the host's abort signal; its
``repeat`` option stands in for a host retry of an unchanged request. The real
host's SIGINT path is the host CLI tier (``scripts/openclaw_qualification.py
host --scenario abort``), run by an operator; it is not run here.

Like the other OpenClaw tests these skip without Node and ``npm ci`` in
``integrations/openclaw``; with ``RELAYMIND_OPENCLAW_NODE=1`` they fail instead.
"""

import asyncio
import threading
import time

import httpx
import pytest

from api_helpers import RUN_ID_HEADER, RunningServer, make_app, token_of
from gateway_helpers import build_gateway, text_turn, tool_turn
from helpers import ScriptedAdapter, make_capabilities, of_type
from native_helpers import GatedAdapter
from relaymind.adapters import GenerationFailed
from relaymind.api.app import ChatSettings, NativeSettings
from relaymind.api.chat import ClientToolsChat
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.local_loop import ConfiguredModel
from relaymind.orchestrator.local_status import LocalStatus
from relaymind.orchestrator.performance import PerformanceSettings
from relaymind.orchestrator.route_decision import ExecutionMode
from test_openclaw_host_transport import REQUIRED, _missing
from test_openclaw_plugin import drive_plugin, inference

SYSTEM_PROMPT = "You are a test host. Use the sandbox tools when asked."
ECHO = "relaymind_test_echo"
WRITE = "relaymind_test_write"
LOGIN = "relaymind_test_login"
KEY_HEADER = "idempotency-key"


@pytest.fixture(autouse=True)
def node_dependencies():
    reason = _missing()
    if reason is not None:
        if REQUIRED:
            pytest.fail(f"RELAYMIND_OPENCLAW_NODE=1 but {reason}")
        pytest.skip(reason)


def client_gateway(tmp_path, turns=None, *, settings=None, native=None, **options):
    gateway = build_gateway(tmp_path, turns, **options)
    app = make_app(
        gateway,
        client_tools=ClientToolsChat(gateway.orchestrator),
        settings=settings,
        native=native,
    )
    return gateway, app


def drive(running, app, tmp_path, *, token=None, **scenario):
    scenario.setdefault("systemPrompt", SYSTEM_PROMPT)
    return drive_plugin(running.base_url, token or token_of(app), tmp_path, **scenario)


def all_run_ids(gateway):
    return [row[0] for row in gateway.store._conn.execute("SELECT run_id FROM runs ORDER BY rowid")]


def wait_for(predicate, timeout=10.0, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {message}")


def terminal(gateway, run_id, timeout=10.0):
    def state():
        record = gateway.store.get_run(run_id)
        return record if record.state in {"completed", "failed", "cancelled"} else None

    return wait_for(state, timeout, f"run {run_id} to end")


class ThreadGate(ScriptedAdapter):
    """A scripted model whose generations wait until the test thread opens the gate.

    The wait polls a threading.Event, so the test thread (outside the
    server's loop) can open it. The gateway cancels a generation by
    cancelling its task, so a cancel still stops the wait at the next
    poll: asyncio.sleep raises CancelledError there.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gate = threading.Event()
        self.started = threading.Event()
        self.cancelled_ids: list[str] = []

    async def generate(self, request):
        self.started.set()
        while not self.gate.is_set():
            await asyncio.sleep(0.01)
        async for event in super().generate(request):
            yield event

    async def cancel(self, generation_id):
        self.cancelled_ids.append(generation_id)
        await super().cancel(generation_id)


def in_background(fn):
    """Run fn in a thread; the returned function joins it and gives fn's result."""
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as error:  # re-raised in the test thread
            box["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    def join(timeout=60):
        thread.join(timeout)
        assert not thread.is_alive(), "the driver did not finish"
        if "error" in box:
            raise box["error"]
        return box["value"]

    return join


# The classes the pinned host retries on the same model (maybeRetryTransient,
# openclaw@2026.10.7; pinned by integrations/openclaw/test/errors.test.ts).
HOST_SAME_MODEL_RETRIED = {"rate_limit", "overloaded", "server_error", "timeout", "output_limit"}


def host_class(output):
    """The class the pinned host's own classifier gives the first failed message (section 7.5)."""
    return output["results"][0]["host_failover_reason"]


def failure_of(output, index=0):
    result = output["results"][index]
    assert result["stop_reason"] == "error"
    # Never an empty success: a failure has a message and no answer text.
    assert result["text"] == ""
    return result["error_message"]


# -- cancellation (RM14, AC12; sections 7.4 and 7.5) -------------------------------------


def test_an_abort_before_the_request_is_sent_creates_no_run(tmp_path):
    """Before admission: nothing reaches the gateway, so no run exists and none is invented."""
    gateway, app = client_gateway(tmp_path, [text_turn("unused")], external_turns=[])
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Say hello.", abort="before")
    assert output["results"][0]["stop_reason"] == "aborted"
    assert all_run_ids(gateway) == []
    assert gateway.adapter.requests == []
    (attempt,) = output["attempts"]
    assert attempt["runId"] == "unavailable"
    assert attempt["outcome"] == "aborted"


def test_an_abort_during_a_buffered_generation_cancels_the_run_and_frees_the_slot(tmp_path):
    """During inference: the adapter is asked to cancel, the run settles, the slot is free."""
    adapter = GatedAdapter(turns=[text_turn("never"), text_turn("never either")])
    gateway, app = client_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="gated"),
        external_turns=[],
    )
    with RunningServer(app) as running:
        first = drive(running, app, tmp_path, prompt="Say hello.", abort={"afterMs": 1500})
        (run_id,) = all_run_ids(gateway)
        record = terminal(gateway, run_id)
        # The slot is free: the next request is admitted (a run, not 429).
        second = drive(running, app, tmp_path, prompt="Say more.", abort={"afterMs": 1500})
    assert first["results"][0]["stop_reason"] == "aborted"
    assert record.state == "cancelled"
    (cancelled,) = of_type(gateway.store.events(run_id), "run.cancelled")
    assert cancelled.payload["reason"] == "cancel_requested"
    assert adapter.cancelled or adapter.interrupted
    # The cancelled run keeps its model call (RM25): started, never invented as finished.
    assert of_type(gateway.store.events(run_id), "model.started")
    # Buffered: no headers were sent, so the run ID is unavailable to the host (7.5).
    (attempt,) = first["attempts"]
    assert attempt["runId"] == "unavailable"
    assert attempt["outcome"] == "aborted"
    (exchange,) = inference(second)
    assert exchange["response"] is None or exchange["response"]["status"] != 429
    assert len(all_run_ids(gateway)) == 2
    assert second["attempts"][0]["code"] != "overloaded"


def test_an_abort_under_early_headers_records_the_run_id_and_cancels_it(tmp_path):
    """RM32 early headers: the run ID is known before the abort, so it is recorded."""
    adapter = GatedAdapter(turns=[text_turn("never")])
    gateway, app = client_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="gated"),
        external_turns=[],
        performance=PerformanceSettings(early_headers=True),
        native=NativeSettings(heartbeat_seconds=0.05),
    )
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Say hello.", abort={"afterMs": 1500})
        (run_id,) = all_run_ids(gateway)
        record = terminal(gateway, run_id)
    assert record.state == "cancelled"
    (exchange,) = inference(output)
    assert exchange["response"]["headers"][RUN_ID_HEADER.lower()] == run_id
    (attempt,) = output["attempts"]
    assert (attempt["runId"], attempt["runIdSource"], attempt["outcome"]) == (
        run_id,
        "header",
        "aborted",
    )
    assert output["results"][0]["text"] == ""


def test_an_abort_while_the_host_reads_the_replay_leaves_the_completed_run(tmp_path):
    """During replay: the buffered run already completed; the abort changes nothing."""
    gateway, app = client_gateway(tmp_path, [text_turn("Hello ", "there.")], external_turns=[])
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Say hello.", abort={"onEvent": "text_delta"})
        (run_id,) = all_run_ids(gateway)
        record = terminal(gateway, run_id)
    assert record.state == "completed"
    events = gateway.store.events(run_id)
    assert not of_type(events, "run.cancelled")
    assert "".join(e.payload["text"] for e in of_type(events, "answer.delta")) == "Hello there."
    # What the host had read is a prefix of the run's answer, never other text.
    assert "Hello there.".startswith(output["results"][0]["text_deltas"])


def test_an_abort_after_completion_changes_nothing(tmp_path):
    """After completion: the run stays completed and the host keeps the answer."""
    gateway, app = client_gateway(tmp_path, [text_turn("Hello there.")], external_turns=[])
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Say hello.", abort="afterResult")
        (run_id,) = all_run_ids(gateway)
        record = terminal(gateway, run_id)
    assert record.state == "completed"
    assert output["results"][0]["text"] == "Hello there."
    assert not of_type(gateway.store.events(run_id), "run.cancelled")
    (attempt,) = output["attempts"]
    assert (attempt["runId"], attempt["outcome"]) == (run_id, "answer")


def test_a_native_events_disconnect_alone_does_not_cancel(tmp_path):
    """Section 7.4: dropping /relay/v1/runs/{id}/events never cancels the chat's run."""
    adapter = ThreadGate(turns=[text_turn("Still here.")])
    gateway, app = client_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="thread-gated"),
        external_turns=[],
    )
    with RunningServer(app) as running:
        join = in_background(lambda: drive(running, app, tmp_path, prompt="Say hello."))
        assert adapter.started.wait(10)
        (run_id,) = all_run_ids(gateway)
        headers = {"Authorization": f"Bearer {token_of(app)}"}
        with (
            httpx.Client(base_url=running.origin, headers=headers, timeout=10) as client,
            client.stream("GET", f"/relay/v1/runs/{run_id}/events") as events,
        ):
            assert events.status_code == 200
            next(events.iter_lines())
        # The events client is gone; the run is still running.
        time.sleep(0.2)
        assert gateway.store.get_run(run_id).state not in {"completed", "failed", "cancelled"}
        adapter.gate.set()
        output = join()
        record = terminal(gateway, run_id)
    assert record.state == "completed"
    assert output["results"][0]["text"] == "Still here."
    assert adapter.cancelled_ids == []


# -- errors as failures, never empty successes (RM16, RM20; sections 7.5 and 11) ---------


def test_a_stale_token_on_the_request_is_401_with_the_reconnect_instruction(tmp_path):
    """The headless agent exec path (no catalog run): the request itself is refused."""
    gateway, app = client_gateway(tmp_path, [text_turn("unused")], external_turns=[])
    with RunningServer(app) as running:
        output = drive(
            running,
            app,
            tmp_path,
            token="rm-stale-token-000000000000000000000000",
            secrets=(token_of(app),),
            prompt="Say hello.",
            resolve="dynamic",
        )
    message = failure_of(output)
    assert message.startswith("RelayMind unauthenticated (HTTP 401): ")
    assert "openclaw models auth paste-api-key --provider relaymind" in message
    assert all_run_ids(gateway) == []
    assert output["attempts"][0]["runId"] == "unavailable"


def test_a_mode_the_policy_forbids_is_403_named(tmp_path):
    settings = ChatSettings(allowed_modes=frozenset({ExecutionMode.AUTO}))
    gateway, app = client_gateway(
        tmp_path, [text_turn("unused")], settings=settings, external_turns=[]
    )
    with RunningServer(app) as running:
        output = drive(
            running,
            app,
            tmp_path,
            prompt="Say hello.",
            resolve="dynamic",
            pluginConfig={"executionMode": "external_only"},
        )
    message = failure_of(output)
    assert message.startswith("RelayMind execution_mode_forbidden (HTTP 403): ")
    assert all_run_ids(gateway) == []
    # Spec 0.49 section 7.5: a refusal by policy is format, never retried on the same model.
    assert host_class(output) == "format"


def test_needs_external_is_422_a_refusal_the_host_does_not_retry(tmp_path):
    """local_only: the local model cannot complete, and RelayMind never goes external.

    Spec 0.49 section 7.5: needs_external is the host's format class, so the
    pinned host does not retry it on the same model (no retry storm of new runs).
    """
    gateway, app = client_gateway(
        tmp_path, [text_turn("x"), text_turn("x")], external_turns=[text_turn("unused")]
    )
    with RunningServer(app) as running:
        output = drive(
            running,
            app,
            tmp_path,
            prompt="Echo the word lantern.",
            hostTools=[ECHO],
            toolChoice="required",
            maxRequests=1,
            pluginConfig={"executionMode": "local_only"},
        )
    message = failure_of(output)
    assert message.startswith("RelayMind needs_external (HTTP 422): ")
    (run_id,) = all_run_ids(gateway)
    assert f"Gateway run {run_id}" in message
    assert gateway.external.requests == []
    (attempt,) = output["attempts"]
    assert (attempt["runId"], attempt["runIdSource"], attempt["code"]) == (
        run_id,
        "error_body",
        "needs_external",
    )
    assert host_class(output) == "format"
    assert host_class(output) not in HOST_SAME_MODEL_RETRIED


def test_needs_external_after_a_failed_local_generation_is_a_provider_failure(tmp_path):
    """local_only: the local runtime fails the generation (capability_missing).

    Spec 0.49 section 7.5: that reason depends on the local runtime's health, not
    on the configuration and the request, so it is server_error, which the pinned
    host retries on the same model, and the message never calls it a refusal.
    """
    gateway, app = client_gateway(
        tmp_path,
        [[GenerationFailed(code="connection_error", message="x")]],
        external_turns=[text_turn("unused")],
    )
    with RunningServer(app) as running:
        output = drive(
            running,
            app,
            tmp_path,
            prompt="Say hello.",
            maxRequests=1,
            pluginConfig={"executionMode": "local_only"},
        )
    message = failure_of(output)
    assert message.startswith("RelayMind needs_external (HTTP 422): ")
    assert "reason capability_missing." in message
    assert "refused this request" not in message
    assert gateway.external.requests == []
    assert host_class(output) == "server_error"
    assert host_class(output) in HOST_SAME_MODEL_RETRIED


@pytest.mark.parametrize("early_headers", [False, True], ids=["http_503", "stream_event"])
def test_an_external_failure_is_a_failure_with_its_run_id(tmp_path, early_headers):
    """503 external_failed, or the error event after early headers; usage stays unknown."""
    gateway, app = client_gateway(
        tmp_path,
        [text_turn("x"), text_turn("x")],
        external_turns=[text_turn("still text"), text_turn("still text")],
        performance=PerformanceSettings(early_headers=early_headers),
        native=NativeSettings(heartbeat_seconds=0.05),
    )
    with RunningServer(app) as running:
        output = drive(
            running,
            app,
            tmp_path,
            prompt="Echo the word lantern.",
            hostTools=[ECHO],
            toolChoice="required",
            maxRequests=1,
        )
    message = failure_of(output)
    where = "stream error event" if early_headers else "HTTP 503"
    assert message.startswith(f"RelayMind external_failed ({where}): ")
    (run_id,) = all_run_ids(gateway)
    record = terminal(gateway, run_id)
    assert record.state == "failed"
    events = gateway.store.events(run_id)
    # The failed run keeps its actual model calls; the doubles report no usage (RM25).
    started = of_type(events, "model.started")
    assert len(started) == len(gateway.adapter.requests) + len(gateway.external.requests)
    for finished in of_type(events, "model.finished"):
        assert finished.payload.get("usage") in (None, {})
    (attempt,) = output["attempts"]
    assert attempt["runId"] == run_id
    assert attempt["usage"] == "unknown"
    # Never mixed owners: no answer text from any actor.
    assert not of_type(events, "answer.delta")
    # Spec 0.49 section 7.5: still a failure of this provider, which the pinned
    # host retries on the same model (its reason_code cannot show a permanent refusal).
    assert host_class(output) == "server_error"


def test_a_held_slot_is_429_overloaded_with_nothing_created(tmp_path):
    adapter = ThreadGate(turns=[text_turn("First."), text_turn("Second.")])
    gateway, app = client_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="thread-gated"),
        external_turns=[],
    )
    with RunningServer(app) as running:
        join = in_background(lambda: drive(running, app, tmp_path, prompt="Hold the slot."))
        assert adapter.started.wait(10)
        busy = drive(running, app, tmp_path, prompt="Say hello.")
        adapter.gate.set()
        held = join()
    message = failure_of(busy)
    assert message.startswith("RelayMind overloaded (HTTP 429): ")
    assert "not queued" in message
    assert held["results"][0]["text"] == "First."
    # The refused request created nothing: one run, the one holding the slot.
    assert len(all_run_ids(gateway)) == 1
    assert busy["attempts"][0]["runId"] == "unavailable"


class OpenOn429:
    """ASGI wrapper: opens the gate when the gateway answers 429, so a retry finds the slot free."""

    def __init__(self, app, gate):
        self.app = app
        self.gate = gate

    async def __call__(self, scope, receive, send):
        async def watched(message):
            if message["type"] == "http.response.start" and message["status"] == 429:
                self.gate.set()
            await send(message)

        await self.app(scope, receive, watched)


def test_a_held_slot_retried_with_max_retries_uses_the_same_key_and_succeeds(tmp_path):
    """Bounded retry (7.5): a 429 created nothing, so the retry reuses the key."""
    adapter = ThreadGate(turns=[text_turn("First."), text_turn("Second.")])
    gateway, app = client_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="thread-gated"),
        external_turns=[],
    )
    with RunningServer(OpenOn429(app, adapter.gate)) as running:
        join = in_background(lambda: drive(running, app, tmp_path, prompt="Hold the slot."))
        assert adapter.started.wait(10)
        # The 429 opens the gate; the plugin waits 500 ms before its one retry.
        retried = drive(running, app, tmp_path, prompt="Say hello.", pluginConfig={"maxRetries": 1})
        join()
    assert retried["results"][0]["text"] == "Second."
    first, second = inference(retried)
    assert first["response"]["status"] == 429
    assert first["request"]["headers"][KEY_HEADER] == second["request"]["headers"][KEY_HEADER]
    assert [a["retried"] for a in retried["attempts"]] == [True, False]
    assert len(all_run_ids(gateway)) == 2


class Stalled(ScriptedAdapter):
    """An external model that never answers in time."""

    async def generate(self, request):
        self.requests.append(request)
        await asyncio.sleep(5)
        yield  # pragma: no cover


def test_an_exhausted_time_limit_is_504_a_timeout(tmp_path):
    stalled = Stalled(locality="external", capabilities=make_capabilities())
    gateway, app = client_gateway(
        tmp_path,
        [],
        local_status=LocalStatus.absent(),
        external_model=ConfiguredModel(adapter=stalled, model_id="stalled"),
        budgets=LocalBudgets(external_seconds=0.2),
    )
    with RunningServer(app) as running:
        output = drive(
            running,
            app,
            tmp_path,
            prompt="Say hello.",
            pluginConfig={"executionMode": "external_only"},
        )
    message = failure_of(output)
    assert message.startswith("RelayMind deadline_exceeded (HTTP 504): ")
    assert "external_seconds" in message
    (run_id,) = all_run_ids(gateway)
    assert output["attempts"][0]["runId"] == run_id


# -- idempotency through the real gateway (RM17, RM21, AC11; section 7.4) ----------------


def test_a_host_retry_of_the_same_request_replays_the_completed_run(tmp_path):
    gateway, app = client_gateway(tmp_path, [text_turn("Hello ", "there.")], external_turns=[])
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Say hello.", repeat="last")
    first, again = inference(output)
    assert first["request"]["headers"][KEY_HEADER] == again["request"]["headers"][KEY_HEADER]
    run_id = first["response"]["headers"][RUN_ID_HEADER.lower()]
    assert again["response"]["headers"][RUN_ID_HEADER.lower()] == run_id
    assert again["response"]["headers"]["idempotent-replayed"] == "true"
    assert output["repeated"]["text"] == output["results"][0]["text"] == "Hello there."
    # No new work: one run, one model call.
    assert all_run_ids(gateway) == [run_id]
    assert len(gateway.adapter.requests) == 1
    assert [a["replayed"] for a in output["attempts"]] == [False, True]


def test_the_tool_result_turn_gets_a_new_key_and_a_new_run_in_the_same_turn(tmp_path):
    gateway, app = client_gateway(
        tmp_path,
        [tool_turn(ECHO, call_id="call_echo_1", text="lantern"), text_turn("Echoed.")],
        external_turns=[],
    )
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Echo lantern.", hostTools=[ECHO])
    first, second = inference(output)
    assert first["request"]["headers"][KEY_HEADER] != second["request"]["headers"][KEY_HEADER]
    runs = [a["runId"] for a in output["attempts"]]
    assert runs == all_run_ids(gateway) and len(set(runs)) == 2
    # One OpenClaw turn, two RelayMind runs (section 7.5).
    assert len({a["turn"] for a in output["attempts"]}) == 1


def test_an_inference_retry_never_asks_the_host_to_repeat_a_completed_tool(tmp_path):
    """After an allowed write ran once, a retry of the follow-up replays the answer."""
    sandbox = tmp_path / "host-sandbox"
    sandbox.mkdir()
    gateway, app = client_gateway(
        tmp_path,
        [
            tool_turn(WRITE, call_id="call_write_1", name="note.txt", line="written by the host"),
            text_turn("The note was written."),
        ],
        external_turns=[],
    )
    with RunningServer(app) as running:
        output = drive(
            running,
            app,
            tmp_path,
            prompt="Write the note.",
            hostTools=[WRITE],
            approval="allow",
            sandboxDir=str(sandbox),
            repeat="last",
        )
    assert output["repeated"]["text"] == "The note was written."
    assert output["repeated"]["tool_calls"] == []
    assert len(output["host_tool_executions"]) == 1
    assert (sandbox / "note.txt").read_text("utf-8").splitlines() == ["written by the host"]
    proposed = [
        e for r in all_run_ids(gateway) for e in of_type(gateway.store.events(r), "tool.proposed")
    ]
    assert len(proposed) == 1
    assert len(all_run_ids(gateway)) == 2
    assert len(gateway.adapter.requests) == 2


def test_a_changed_payload_under_the_host_s_own_key_is_409_idempotency_conflict(tmp_path):
    gateway, app = client_gateway(
        tmp_path, [text_turn("First."), text_turn("unused")], external_turns=[]
    )
    with RunningServer(app) as running:
        first = drive(running, app, tmp_path, prompt="Say hello.", hostIdempotencyKey="host-key-1")
        second = drive(running, app, tmp_path, prompt="Say more.", hostIdempotencyKey="host-key-1")
    assert first["results"][0]["text"] == "First."
    message = failure_of(second)
    assert message.startswith("RelayMind idempotency_conflict (HTTP 409): ")
    assert len(all_run_ids(gateway)) == 1
    assert host_class(second) == "format"


def test_a_redacted_tool_call_is_not_replayed(tmp_path):
    """RM12: the stored arguments were redacted, so the replay is refused, not altered."""
    gateway, app = client_gateway(
        tmp_path,
        [tool_turn(LOGIN, call_id="call_login_1", user="ops", password="hunter2-sandbox")],
        external_turns=[],
    )
    with RunningServer(app) as running:
        output = drive(
            running,
            app,
            tmp_path,
            prompt="Log in.",
            hostTools=[LOGIN],
            maxRequests=1,
            repeat="first",
        )
    assert output["results"][0]["stop_reason"] == "toolUse"
    assert output["repeated"]["stop_reason"] == "error"
    assert output["repeated"]["error_message"].startswith(
        "RelayMind idempotency_conflict (HTTP 409): "
    )
    assert "redacted" in output["repeated"]["error_message"]
    assert len(all_run_ids(gateway)) == 1


def test_a_cancelled_runs_key_sent_again_is_409_run_terminal(tmp_path):
    """Section 7.4: a cancelled run's key is never run again.

    A new plugin process starts with an empty key ledger, so it sends the
    cancelled run's key again; the gateway answers 409 run_terminal, naming
    the run, and creates nothing.
    """
    adapter = GatedAdapter(turns=[text_turn("never")])
    gateway, app = client_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="gated"),
        external_turns=[],
    )
    scenario = {"prompt": "Say hello.", "fixedTimestamps": True}
    with RunningServer(app) as running:
        drive(running, app, tmp_path, abort={"afterMs": 1500}, **scenario)
        (run_id,) = all_run_ids(gateway)
        terminal(gateway, run_id)
        again = drive(running, app, tmp_path, **scenario)
    assert gateway.store.get_run(run_id).state == "cancelled"
    assert failure_of(again).startswith("RelayMind run_terminal (HTTP 409): ")
    assert f"Gateway run {run_id}" in failure_of(again)
    assert all_run_ids(gateway) == [run_id]
    # A key the gateway never runs again: format, not a same-model retry.
    assert host_class(again) == "format"


def test_a_host_retry_of_a_cancelled_request_is_a_new_run_under_a_new_key(tmp_path):
    """Section 7.4: a retry after a failure uses a new key and is a new run.

    In the same plugin process the key ledger knows the cancelled run spent
    its key, so the host's retry of the unchanged request sends <key>-retry1
    and gets an answer, never 409 run_terminal (#138 review).
    """
    adapter = ThreadGate(turns=[text_turn("Hello again.")])
    gateway, app = client_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="thread-gated"),
        external_turns=[],
    )

    def cancelled_first():
        ids = all_run_ids(gateway)
        return bool(ids) and gateway.store.get_run(ids[0]).state == "cancelled"

    def open_after_cancel():
        wait_for(cancelled_first, 30, "the first run to be cancelled")
        adapter.gate.set()

    opener = threading.Thread(target=open_after_cancel, daemon=True)
    opener.start()
    with RunningServer(app) as running:
        output = drive(
            running, app, tmp_path, prompt="Say hello.", abort={"afterMs": 1500}, repeat="first"
        )
    opener.join(5)
    first, second = all_run_ids(gateway)
    assert gateway.store.get_run(first).state == "cancelled"
    assert gateway.store.get_run(second).state == "completed"
    one, two = inference(output)
    assert (
        two["request"]["headers"][KEY_HEADER] == f"{one['request']['headers'][KEY_HEADER]}-retry1"
    )
    assert output["repeated"]["text"] == "Hello again."


def test_a_key_survives_a_gateway_token_change(tmp_path):
    """Section 7.4: keys belong to the one caller, not to the token of one start."""
    gateway = build_gateway(tmp_path, [text_turn("Hello there.")], external_turns=[])
    before = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
    after = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
    assert token_of(before) != token_of(after)
    scenario = {"prompt": "Say hello.", "fixedTimestamps": True}
    with RunningServer(before) as running:
        first = drive(running, before, tmp_path, secrets=(token_of(after),), **scenario)
    with RunningServer(after) as running:
        second = drive(running, after, tmp_path, secrets=(token_of(before),), **scenario)
    (one,) = inference(first)
    (two,) = inference(second)
    assert one["request"]["headers"][KEY_HEADER] == two["request"]["headers"][KEY_HEADER]
    assert two["response"]["headers"]["idempotent-replayed"] == "true"
    assert second["results"][0]["text"] == "Hello there."
    assert len(gateway.adapter.requests) == 1
