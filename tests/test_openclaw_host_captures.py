"""The real OpenClaw host's requests against the section 7.1 subset (RM19, RM33, AC24; #135).

The host probe on macOS (``docs/evidence/2026-10-27-openclaw-mac-probe/``,
OpenClaw 2026.10.7) pointed ``openclaw agent exec`` at a capture server and
stored every request the host sent. These tests read those stored requests
and check them with the gateway's own chat request validator, so the
compatibility table in ``docs/design/2026-10-27-openclaw-integration.md``
rests on the real host, not only on the pinned transport:

- with no switches the host sends ``stream_options`` to a loopback URL, which
  the gateway rejects by name;
- with the base switches every request stays inside section 7.1; these runs
  declared the switches on a config-file provider, and the one plugin-model
  run declared none, so the switches on a plugin-supplied model are not_run;
- ``strictMessageKeys: true`` strips the tool linkage, and the gateway
  rejects the follow-up turn;
- the host rewrites the tool call ID it replays (``call_probe_1`` became
  ``callprobe1``), consistently in the call and the result, so the gateway's
  same-request check still passes.

No model or host runs here; the captures are data.
"""

import json
from pathlib import Path

import pytest

from relaymind.api.chat_request import CLIENT_TOOLS, parse_chat_request
from relaymind.api.errors import ApiError

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "docs" / "evidence" / "2026-10-27-openclaw-mac-probe"
REQUESTS = PROBE / "requests"

# Runs whose host config set no compat object (the host's loopback defaults).
NO_SWITCHES = ["a1-hello-nocompat", "a2-readfile-nocompat", "c1-write-allow"]
# Runs with the seven base switches (configs/config-b-base.json and
# config-d-nostream.json: supportsUsageInStreaming false and the others), the
# configuration compat.ts declares. All are config-file providers.
BASE_SWITCHES = [
    "c2-readfile-compat-base7",
    "d1-hello-nostream",
    "d2-readfile-nostream",
]
# Runs with all ten switches of the brief (configs/config-b.json), which adds
# strictMessageKeys true; not the plugin's configuration (REPORT.md, A.2).
ALL_TEN_SWITCHES = ["b1-hello-compat", "b2-readfile-compat"]
# The one run whose model a plugin supplied; it declared no switches, so no
# capture shows the switches on a plugin-supplied model (#136 must).
PLUGIN_MODEL = "p1-plugin-hello-storedkey"
# The capture server's tool call ID (probe-src/server/capture-server.mjs).
SERVER_CALL_ID = "call_probe_1"

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
MESSAGE_FIELDS = {
    "system": {"role", "content"},
    "user": {"role", "content"},
    "assistant": {"role", "content", "tool_calls"},
    "tool": {"role", "content", "tool_call_id"},
}


def chat_requests(run):
    paths = sorted((REQUESTS / run).glob("*-POST-_v1_chat_completions.json"))
    assert paths, run
    return [json.loads(path.read_text("utf-8")) for path in paths]


def test_the_probe_records_the_pinned_host_release():
    report = (PROBE / "REPORT.md").read_text("utf-8")
    assert "OpenClaw 2026.10.7 (157d8c2)" in report


@pytest.mark.parametrize("run", NO_SWITCHES)
def test_the_host_defaults_for_a_loopback_url_send_stream_options_and_are_rejected(run):
    """Spec 7.5: without the switch the host asks for usage on every streamed request."""
    for request in chat_requests(run):
        body = request["body"]
        assert body["stream_options"] == {"include_usage": True}
        with pytest.raises(ApiError) as error:
            parse_chat_request(body, served=(CLIENT_TOOLS,))
        assert error.value.code == "unsupported_parameter"
        assert "stream_options" in str(error.value)


@pytest.mark.parametrize("run", BASE_SWITCHES)
def test_with_the_switches_every_host_request_stays_inside_section_7_1(run):
    for request in chat_requests(run):
        body = request["body"]
        assert set(body) <= SECTION_7_1_FIELDS, sorted(set(body) - SECTION_7_1_FIELDS)
        assert body["tool_choice"] == "auto"
        assert "max_completion_tokens" in body
        roles = [message["role"] for message in body["messages"]]
        assert roles.count("system") == 1
        assert "developer" not in roles
        for message in body["messages"]:
            assert set(message) <= MESSAGE_FIELDS[message["role"]], message["role"]
        for tool in body["tools"]:
            assert "strict" not in tool["function"]
        # No assistant message is inserted after tool results.
        if "tool" in roles:
            assert roles[-1] == "tool"
        parse_chat_request(body, served=(CLIENT_TOOLS,))


@pytest.mark.parametrize("run", ALL_TEN_SWITCHES)
def test_with_all_ten_switches_a_first_turn_stays_inside_section_7_1(run):
    """Without a tool result to strip, the ten-key run's first turn passes too."""
    first = chat_requests(run)[0]["body"]
    assert "stream_options" not in first
    assert set(first) <= SECTION_7_1_FIELDS
    parse_chat_request(first, served=(CLIENT_TOOLS,))


def test_the_plugin_model_run_declared_no_switches_and_sent_stream_options():
    """The probe's plugin model ran with the host defaults: it is no evidence that
    switches on a plugin-supplied model reach the wire."""
    (request,) = chat_requests(PLUGIN_MODEL)
    assert request["body"]["stream_options"] == {"include_usage": True}
    with pytest.raises(ApiError) as error:
        parse_chat_request(request["body"], served=(CLIENT_TOOLS,))
    assert error.value.code == "unsupported_parameter"


def test_the_host_streams_unless_the_model_turns_streaming_off():
    for run in BASE_SWITCHES:
        expected = not run.startswith("d")
        assert all(r["body"]["stream"] is expected for r in chat_requests(run)), run


def test_strict_message_keys_strip_the_tool_linkage_and_the_gateway_rejects_the_turn():
    """Spec 7.5: strictMessageKeys must stay false; true orphans the tool result."""
    _, follow_up = chat_requests("b2-readfile-compat")
    assistant, tool = follow_up["body"]["messages"][2:]
    assert assistant == {"role": "assistant", "content": None}
    assert tool["role"] == "tool"
    assert "tool_call_id" not in tool
    with pytest.raises(ApiError) as error:
        parse_chat_request(follow_up["body"], served=(CLIENT_TOOLS,))
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize("run", ["c2-readfile-compat-base7", "d2-readfile-nostream"])
def test_the_host_rewrites_the_call_id_consistently_and_the_gateway_accepts_it(run):
    """Spec 7.5: the host removes underscores from the call ID it replays."""
    _, follow_up = chat_requests(run)
    assistant, tool = follow_up["body"]["messages"][2:]
    (call,) = assistant["tool_calls"]
    assert call["id"] == SERVER_CALL_ID.replace("_", "")
    assert call["id"] != SERVER_CALL_ID
    assert tool["tool_call_id"] == call["id"]
    request = parse_chat_request(follow_up["body"], served=(CLIENT_TOOLS,))
    assert request.messages[-1].tool_call_id == call["id"]


def test_the_host_decorates_the_user_message():
    """The user text the gateway receives is not the raw prompt (#139 prompt identity)."""
    (request,) = chat_requests("b1-hello-compat")
    user = request["body"]["messages"][1]["content"]
    assert user.startswith("[")
    assert "] Say hello\n\nRuntime: agent=main" in user


def test_no_capture_holds_a_credential():
    for path in REQUESTS.rglob("*.json"):
        request = json.loads(path.read_text("utf-8"))
        assert request["headers"].get("authorization") in (None, "Bearer <redacted>"), path
