"""The pinned OpenClaw transport against the real gateway (RM33, AC24, #135).

The Node driver ``integrations/openclaw/test/host-transport/drive.mjs`` runs
the openai-completions transport of the pinned host (``@openclaw/ai``
2026.10.7, under the host transport policy that the public plugin SDK
installs) against the real gateway on a loopback socket. The gateway runs
over scripted model doubles (spec section 13), and the driver plays the
host: it owns the conversation and executes the sandbox host tool itself.

These tests need Node and ``npm ci`` in ``integrations/openclaw``. Without
them they skip with the reason; with ``RELAYMIND_OPENCLAW_NODE=1`` (CI)
a missing dependency fails instead.
"""

import asyncio
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from api_helpers import RUN_ID_HEADER, RunningServer, make_app, token_of
from gateway_helpers import build_gateway, text_turn, tool_turn
from helpers import load_script_module, make_capabilities, of_type
from native_helpers import GatedAdapter
from relaymind.api.chat import ClientToolsChat
from relaymind.orchestrator.local_loop import ConfiguredModel

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "integrations" / "openclaw"
DRIVER = PACKAGE / "test" / "host-transport" / "drive.mjs"
REQUIRED = os.environ.get("RELAYMIND_OPENCLAW_NODE") == "1"
# The harness builds the driver's environment; the tests use the same one.
qualification = load_script_module("openclaw_qualification")

# The fields section 7.1 lists, plus the API defaults it accepts.
SECTION_7_1_FIELDS = {
    "model",
    "messages",
    "stream",
    "n",
    "max_completion_tokens",
    "max_tokens",
    "temperature",
    "top_p",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
    "user",
}
ACCEPTED_DEFAULTS = {
    "store": False,
    "presence_penalty": 0,
    "frequency_penalty": 0,
    "logprobs": False,
}
SYSTEM_PROMPT = "You are a test host. Use relaymind_test_echo when asked to echo."
ECHO_PROMPT = "Echo the word lantern with the tool."
ANSWER = "The tool said: echo: lantern."


def _node() -> str | None:
    return shutil.which("node")


def _missing() -> str | None:
    if _node() is None:
        return "node is not on PATH"
    for package in ("@openclaw/ai", "openclaw"):
        if not (PACKAGE / "node_modules" / package / "package.json").exists():
            return f"run npm ci in integrations/openclaw ({package} is missing)"
    return None


def test_node_is_found_on_path_as_the_harness_finds_it(monkeypatch, tmp_path):
    """The tests run the Node the qualification harness runs (#135).

    ``node_binary()`` and the script tests look only on PATH; a fallback
    here would run a Node the harness never sees.
    """
    stray = tmp_path / ".nvm/versions/node/v24.22.1/bin/node"
    stray.parent.mkdir(parents=True)
    stray.write_text("")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert _node() is None


@pytest.fixture(autouse=True)
def node_dependencies():
    reason = _missing()
    if reason is not None:
        if REQUIRED:
            pytest.fail(f"RELAYMIND_OPENCLAW_NODE=1 but {reason}")
        pytest.skip(reason)


def drive(running, app, tmp_path, token=None, **scenario):
    """Run one scenario through the pinned transport; return the driver's JSON."""
    path = tmp_path / f"scenario-{len(list(tmp_path.glob('scenario-*')))}.json"
    scenario.setdefault("systemPrompt", SYSTEM_PROMPT)
    path.write_text(json.dumps(scenario), "utf-8")
    token = token or token_of(app)
    env = qualification.child_environment(running.base_url, token)
    out = path.with_suffix(".out.json")
    completed = subprocess.run(
        [_node(), str(DRIVER), str(path), str(out)],
        cwd=PACKAGE,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    text = out.read_text("utf-8")
    # The token never reaches the captured exchanges or the host log (RM12, section 7.5).
    for secret in {token_of(app), token}:
        assert secret not in text
        assert secret not in completed.stdout + completed.stderr
    output = json.loads(text)
    return output


def client_gateway(tmp_path, turns=None, **options):
    gateway = build_gateway(tmp_path, turns, **options)
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
    return gateway, app


def echo_call(call_id="call_echo_1", text="lantern"):
    return tool_turn("relaymind_test_echo", call_id=call_id, text=text)


def run_ids(output):
    return [x["response"]["headers"].get(RUN_ID_HEADER.lower()) for x in output["exchanges"]]


def body_keys_outside_7_1(body):
    return {
        key
        for key, value in body.items()
        if key not in SECTION_7_1_FIELDS
        and value is not None
        and not (key in ACCEPTED_DEFAULTS and value == ACCEPTED_DEFAULTS[key])
    }


# -- the host defaults and the declared switches ------------------------------------------


def test_the_pinned_transport_defaults_for_a_loopback_url_are_rejected(tmp_path):
    """Section 7.5: without the switches the host sends stream_options; 7.1 rejects it."""
    gateway, app = client_gateway(tmp_path, [text_turn("unused")], external_turns=[])
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, compat="host-default", prompt="Say hello.")
    (exchange,) = output["exchanges"]
    assert exchange["request"]["body"]["stream_options"] == {"include_usage": True}
    assert exchange["response"]["status"] == 400
    error = exchange["response"]["body"]["error"]
    assert error["code"] == "unsupported_parameter"
    assert "stream_options" in error["message"]
    # The host sees a failure, never a successful empty answer (section 7.5).
    (result,) = output["results"]
    assert result["stop_reason"] == "error"
    assert result["text"] == ""
    # A rejected request has no run (section 7.1).
    assert gateway.adapter.requests == []


def test_the_host_defaults_with_reasoning_on_send_reasoning_effort_and_no_developer(tmp_path):
    """Section 7.5, recorded at v2026.10.7: a loopback URL never gets the developer role.

    The host's default for a local endpoint has the developer role off, so a
    runtime context message is a user message even with reasoning on; reasoning
    on adds reasoning_effort, which 7.1 rejects like stream_options.
    """
    gateway, app = client_gateway(tmp_path, [text_turn("unused")], external_turns=[])
    history = [
        {"role": "user", "content": "First question."},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "First answer."}],
            "api": "openai-completions",
            "provider": "relaymind",
            "model": "relaymind-client-tools",
            "stopReason": "stop",
        },
        {
            "role": "user",
            "content": "Runtime context: the cwd is /tmp.",
            "runtimeContext": {"retained": True},
        },
    ]
    with RunningServer(app) as running:
        output = drive(
            running,
            app,
            tmp_path,
            compat="host-default",
            reasoning=True,
            prompt="Second question.",
            history=history,
        )
    (exchange,) = output["exchanges"]
    body = exchange["request"]["body"]
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user", "user"]
    assert body["reasoning_effort"] == "high"
    assert body["stream_options"] == {"include_usage": True}
    assert exchange["response"]["status"] == 400
    assert gateway.adapter.requests == []


def test_the_developer_switch_on_puts_a_developer_message_after_the_first_user(tmp_path):
    """Section 7.5: why the plugin declares the developer role off; 7.1 rejects the order."""
    gateway, app = client_gateway(tmp_path, [text_turn("unused")], external_turns=[])
    history = [
        {"role": "user", "content": "First question."},
        {
            "role": "user",
            "content": "Runtime context: the cwd is /tmp.",
            "runtimeContext": {"retained": True},
        },
    ]
    with RunningServer(app) as running:
        output = drive(
            running,
            app,
            tmp_path,
            compat={"supportsDeveloperRole": True},
            prompt="Second question.",
            history=history,
        )
    (exchange,) = output["exchanges"]
    roles = [m["role"] for m in exchange["request"]["body"]["messages"]]
    assert roles == ["system", "user", "developer", "user"]
    assert exchange["response"]["status"] == 400
    assert exchange["response"]["body"]["error"]["code"] == "invalid_request"
    assert output["results"][0]["stop_reason"] == "error"
    assert gateway.adapter.requests == []


def test_the_host_refuses_a_request_over_the_advertised_window_before_sending(tmp_path):
    """Section 7.5: the advertised window is enforced by the host, not the gateway.

    The host estimates the input and refuses before any request when it does not
    fit the advertised context window, so a window must describe the mode the
    gateway serves (in auto, a local-only window would refuse work the gateway
    would send to the external model).
    """
    gateway, app = client_gateway(tmp_path, [text_turn("unused")], external_turns=[])
    with RunningServer(app) as running:
        output = drive(
            running, app, tmp_path, prompt="Say hello. " * 100, contextWindow=100, maxTokens=50
        )
    assert output["exchanges"] == []
    (result,) = output["results"]
    assert result["stop_reason"] == "error"
    assert "Context window exceeded" in result["error_message"]
    assert gateway.adapter.requests == []


def test_a_stale_token_is_401_and_a_failure(tmp_path):
    """Section 7.5: after a gateway restart the old token fails with 401, never empty text."""
    gateway, app = client_gateway(tmp_path, [text_turn("unused")], external_turns=[])
    with RunningServer(app) as running:
        output = drive(
            running, app, tmp_path, prompt="Say hello.", token="stale-token-0000000000000000000000"
        )
    (exchange,) = output["exchanges"]
    assert exchange["response"]["status"] == 401
    assert exchange["response"]["body"]["error"]["code"] == "unauthenticated"
    assert output["results"][0]["stop_reason"] == "error"
    assert gateway.adapter.requests == []


def test_the_declared_switches_keep_the_streamed_request_inside_section_7_1(tmp_path):
    """RM19, RM33: the plugin compat yields a 7.1 request and a text answer."""
    gateway, app = client_gateway(tmp_path, [text_turn("Hello ", "there.")], external_turns=[])
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Say hello.")
    (exchange,) = output["exchanges"]
    body = exchange["request"]["body"]
    assert body_keys_outside_7_1(body) == set()
    assert body["model"] == "relaymind-client-tools"
    assert body["stream"] is True
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert exchange["request"]["headers"]["authorization"] == "<redacted>"
    assert exchange["response"]["status"] == 200
    run_id = exchange["response"]["headers"][RUN_ID_HEADER.lower()]
    sse = exchange["response"]["body"]["sse"]
    assert sse[-1] == "[DONE]"
    chunks = [event for event in sse if isinstance(event, dict) and "choices" in event]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert all("usage" not in chunk for chunk in chunks)
    (result,) = output["results"]
    assert result["stop_reason"] == "stop"
    assert result["text"] == "Hello there."
    # The host tolerates a stream without usage; the counts are zeros it did not get.
    events = gateway.store.events(run_id)
    assert of_type(events, "run.completed")
    assert gateway.external.requests == []


def test_a_non_streamed_request_is_inside_section_7_1(tmp_path):
    """RM19: the buffered form also passes, with no usage in the response."""
    _, app = client_gateway(tmp_path, [text_turn("Hello.")], external_turns=[])
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Say hello.", stream=False)
    (exchange,) = output["exchanges"]
    assert exchange["request"]["body"]["stream"] is False
    assert body_keys_outside_7_1(exchange["request"]["body"]) == set()
    assert exchange["response"]["status"] == 200
    assert "usage" not in exchange["response"]["body"]
    assert output["results"][0]["text"] == "Hello."


def test_the_token_field_is_max_completion_tokens(tmp_path):
    """Section 7.5: maxTokensField; the configured output limit is sent as is."""
    _, app = client_gateway(tmp_path, [text_turn("Hello.")], external_turns=[])
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Say hello.", maxTokens=1024)
    body = output["exchanges"][0]["request"]["body"]
    assert body["max_completion_tokens"] == 1024
    assert "max_tokens" not in body


def test_a_runtime_context_message_after_the_first_user_message_is_sent_as_user(tmp_path):
    """Section 7.5: with the developer switch off, the host never sends a late developer."""
    _, app = client_gateway(tmp_path, [text_turn("Done.")], external_turns=[])
    history = [
        {"role": "user", "content": "First question."},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "First answer."}],
            "api": "openai-completions",
            "provider": "relaymind",
            "model": "relaymind-client-tools",
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": "stop",
        },
        {
            "role": "user",
            "content": "Runtime context: the cwd is /tmp.",
            "runtimeContext": {"retained": True},
        },
    ]
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Second question.", history=history)
    body = output["exchanges"][0]["request"]["body"]
    roles = [m["role"] for m in body["messages"]]
    assert "developer" not in roles
    assert roles == ["system", "user", "assistant", "user", "user"]
    assert output["exchanges"][0]["response"]["status"] == 200


# -- text and a host tool round trip (AC24) ----------------------------------------------


def test_a_tool_round_trip_through_the_pinned_transport(tmp_path):
    """AC24, RM08, RM09, RM33: the host executes the call once; IDs are preserved."""
    gateway, app = client_gateway(
        tmp_path, [echo_call("call_echo_1"), text_turn(ANSWER)], external_turns=[]
    )
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt=ECHO_PROMPT)
    first, second = output["exchanges"]
    assert [r["stop_reason"] for r in output["results"]] == ["toolUse", "stop"]
    (call,) = output["results"][0]["tool_calls"]
    assert call == {
        "id": "call_echo_1",
        "name": "relaymind_test_echo",
        "arguments": {"text": "lantern"},
    }
    # The host executed the tool exactly once (AC24).
    assert output["host_tool_executions"] == [call]
    assert output["results"][1]["text"] == ANSWER
    # The tool catalog the host sent is the one the model was offered, unchanged.
    (tool,) = first["request"]["body"]["tools"]
    assert tool["function"]["name"] == "relaymind_test_echo"
    assert "strict" not in tool["function"]
    # The second request carries the call and its result with the same ID.
    messages = second["request"]["body"]["messages"]
    assert body_keys_outside_7_1(second["request"]["body"]) == set()
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool"]
    assert messages[2]["tool_calls"][0]["id"] == "call_echo_1"
    assert set(messages[3]) == {"role", "content", "tool_call_id"}
    assert messages[3]["tool_call_id"] == "call_echo_1"
    assert messages[3]["content"] == "echo: lantern"
    # Two separate runs; RelayMind proposed the call and executed nothing (RM08).
    first_run, second_run = run_ids(output)
    assert first_run != second_run
    first_events = gateway.store.events(first_run)
    (proposed,) = of_type(first_events, "tool.proposed")
    assert proposed.payload["tool_name"] == "relaymind_test_echo"
    for run_id in (first_run, second_run):
        events = gateway.store.events(run_id)
        assert not of_type(events, "tool.started")
        assert not of_type(events, "tool.finished")
        assert of_type(events, "run.completed")
    assert gateway.external.requests == []


def test_parallel_tool_calls_true_from_operator_params_stays_refused(tmp_path):
    """Section 7.5: an operator setting the gateway refuses stays refused, by name."""
    gateway, app = client_gateway(tmp_path, [text_turn("unused")], external_turns=[])
    with RunningServer(app) as running:
        output = drive(
            running, app, tmp_path, prompt="Say hello.", extraBody={"parallel_tool_calls": True}
        )
    (exchange,) = output["exchanges"]
    assert exchange["request"]["body"]["parallel_tool_calls"] is True
    assert exchange["response"]["status"] == 400
    assert exchange["response"]["body"]["error"]["code"] == "unsupported_parameter"
    assert "parallel_tool_calls" in exchange["response"]["body"]["error"]["message"]
    assert output["results"][0]["stop_reason"] == "error"
    assert gateway.adapter.requests == []


# -- modes and context limits (section 7.5) -----------------------------------------------


def test_an_oversized_request_goes_external_in_auto(tmp_path):
    """RM13, RM26: over the local limit, auto hands off before any output."""
    gateway, app = client_gateway(
        tmp_path,
        [],
        capabilities=make_capabilities(context_limit=50),
        external_turns=[text_turn("External hello.")],
    )
    gateway.adapter.input_tokens = None  # count characters
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Say hello. " * 20)
    assert output["results"][0]["text"] == "External hello."
    (run_id,) = run_ids(output)
    (handoff,) = of_type(gateway.store.events(run_id), "handoff.started")
    assert handoff.payload["reason_code"] == "context_limit"
    assert gateway.adapter.requests == []
    assert len(gateway.external.requests) == 1


def test_an_oversized_request_in_local_only_is_needs_external(tmp_path):
    """RM04, RM26: local_only never falls back; the host sees a failure."""
    gateway, app = client_gateway(
        tmp_path,
        [],
        capabilities=make_capabilities(context_limit=50),
        external_turns=[],
    )
    gateway.adapter.input_tokens = None
    with RunningServer(app) as running:
        output = drive(
            running, app, tmp_path, prompt="Say hello. " * 20, executionMode="local_only"
        )
    (exchange,) = output["exchanges"]
    assert exchange["request"]["headers"]["x-relaymind-execution-mode"] == "local_only"
    assert exchange["response"]["status"] == 422
    assert exchange["response"]["body"]["error"]["code"] == "needs_external"
    assert output["results"][0]["stop_reason"] == "error"
    assert gateway.external.requests == []


def test_a_request_over_both_limits_is_context_limit_exceeded(tmp_path):
    """RM13: over the external budget too, the run ends as context_limit_exceeded."""
    gateway, app = client_gateway(
        tmp_path,
        [],
        capabilities=make_capabilities(context_limit=50),
        external_turns=[],
        external_capabilities=make_capabilities(context_limit=60),
    )
    gateway.adapter.input_tokens = None
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Say hello. " * 20)
    (exchange,) = output["exchanges"]
    assert exchange["response"]["status"] == 400
    assert exchange["response"]["body"]["error"]["code"] == "context_limit_exceeded"
    assert output["results"][0]["stop_reason"] == "error"
    assert gateway.external.requests == []


def test_external_only_works_without_a_local_model_call(tmp_path):
    """Section 7.5: the mode header reaches the gateway; the local model is not called."""
    gateway, app = client_gateway(tmp_path, [], external_turns=[text_turn("External.")])
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Say hello.", executionMode="external_only")
    assert output["results"][0]["text"] == "External."
    assert gateway.adapter.requests == []
    assert len(gateway.external.requests) == 1


# -- cancellation (section 7.4) -----------------------------------------------------------


def test_an_abort_during_a_buffered_generation_cancels_the_owned_run(tmp_path):
    """RM14, section 7.5: the host abort signal reaches the gateway's owned run."""
    adapter = GatedAdapter(turns=[text_turn("never")])
    gateway = build_gateway(
        tmp_path,
        None,
        local_model=ConfiguredModel(adapter=adapter, model_id="gated"),
        external_turns=[],
    )
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
    with RunningServer(app) as running:
        output = drive(running, app, tmp_path, prompt="Say hello.", abortAfterMs=1500)
        (exchange,) = output["exchanges"]
        assert exchange["client_closed_early"] is True
        # Buffered: no response headers were sent, so the host has no run ID;
        # it is unavailable, never invented (section 7.5).
        assert exchange["response"] is None or exchange["response"]["status"] is None
        (run_id,) = only_run_ids(gateway)
        record = _wait_for_terminal(gateway, run_id)
    assert output["results"][0]["stop_reason"] == "aborted"
    assert record.state == "cancelled"
    assert adapter.cancelled or adapter.interrupted
    (cancelled,) = of_type(gateway.store.events(run_id), "run.cancelled")
    assert cancelled.payload["reason"] == "cancel_requested"


def only_run_ids(gateway):
    return qualification._run_ids(gateway)


def _wait_for_terminal(gateway, run_id, timeout=10.0):
    done = threading.Event()
    record = None

    async def poll():
        nonlocal record
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            record = gateway.store.get_run(run_id)
            if record.state in {"completed", "failed", "cancelled"}:
                done.set()
                return
            await asyncio.sleep(0.02)

    asyncio.run(poll())
    assert done.is_set(), f"run {run_id} still {record.state}"
    return record
