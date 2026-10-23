"""The external latency probe (#116; RM25, RM18, spec 0.31 sections 6 and 12.1).

The probe sends two request shapes built by the gateway's own code (an
answer generation after a health tool exchange, and a tool-offering
generation of investigate-performance, both through ``external_messages``)
to a Chat Completions endpoint over raw HTTP. It records the time to the
response headers, to the first reasoning delta, to the first content or
tool-call delta, and to the end, with the usage object. The live run is an
operator action recorded in docs/EVIDENCE.md. These tests use a mock
transport: the arm bodies, the timing extraction, the reasoning-token
count (absent is recorded as null, never as 0, RM16), the call cap, and the
rule that no credential reaches the export (RM12, spec section 3).
"""

import json

import httpx
import pytest

from helpers import SCRIPTS_DIR, load_script_module, sse_body
from relaymind.orchestrator.handoff import RULES

CHECKED_IN = SCRIPTS_DIR / "probes" / "m7_external_latency.json"
KEY = "sk-test-0123456789abcdef-secret"
MODEL = "provider-model-x"
OFF_FIELDS = {"thinking": {"type": "disabled"}}
TOOLS = {"get_latest_deployment", "get_metrics", "get_recent_logs"}


@pytest.fixture(scope="module")
def probe():
    return load_script_module("probes/probe_external_latency")


@pytest.fixture(scope="module")
def config(probe):
    return probe.load_probe_config(CHECKED_IN)


@pytest.fixture()
def shapes(probe, config, tmp_path):
    return probe.build_shapes(config, work_dir=tmp_path)


def chunk(delta, finish_reason=None, **extra):
    return {
        "model": "provider-model-x-0101",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        **extra,
    }


def stream(*, reasoning=True, tool_call=False, usage=True, reasoning_tokens=True):
    chunks = []
    if reasoning:
        chunks += [chunk({"reasoning_content": "Let me think."})]
    if tool_call:
        call = {"index": 0, "id": "c1", "function": {"name": "get_metrics", "arguments": "{}"}}
        chunks += [chunk({"tool_calls": [call]})]
    else:
        chunks += [chunk({"content": "The service "}), chunk({"content": "is healthy."})]
    chunks += [chunk({}, "tool_calls" if tool_call else "stop")]
    if usage:
        details = {"reasoning_tokens": 7} if reasoning_tokens else {}
        chunks += [
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 900,
                    "completion_tokens": 20,
                    "total_tokens": 920,
                    "prompt_tokens_details": {"cached_tokens": 128},
                    "completion_tokens_details": details,
                },
            }
        ]
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=sse_body(chunks)
    )


def run(probe, config, shapes, handler, **overrides):
    options = {
        "base_url": "https://provider.example/api/v4",
        "model": MODEL,
        "api_key": KEY,
        "reasoning_off": OFF_FIELDS,
        "transport": httpx.MockTransport(handler),
    }
    options.update(overrides)
    return probe.run_probe(config, shapes, **options)


class FakeClock:
    """A monotonic clock that moves by a fixed step at every read."""

    def __init__(self, step=0.010):
        self.now = 100.0
        self.step = step
        self.reads = 0

    def __call__(self):
        self.now += self.step
        self.reads += 1
        return self.now


def arm_body(probe, shapes, arm, index=0, run_id=None):
    request = shapes.request(arm.shape, index, run_id or f"run_{arm.name}_{index}")
    return probe.request_body(request, model=MODEL, reasoning_off=OFF_FIELDS, arm=arm)


# -- the checked-in definition --------------------------------------------------------------------


def test_the_checked_in_definition_is_four_arms_of_five(probe, config):
    """#116: tools or none, times the provider's reasoning default or off."""
    arms = probe.ARMS
    assert [(a.shape, a.reasoning) for a in arms] == [
        ("answer", "default"),
        ("answer", "off"),
        ("tools", "default"),
        ("tools", "off"),
    ]
    assert config.repetitions == 5
    assert len(config.requests("answer")) == len(config.requests("tools")) == 5
    assert config.reasoning_off["fields"] == OFF_FIELDS
    # The provider documentation checked on the day is recorded with the definition.
    assert all(url.startswith("https://") for url in config.reasoning_off["documentation"])


@pytest.mark.parametrize(
    "change",
    [
        lambda raw: raw.update(repetitions=0),
        lambda raw: raw["answer"].update(requests=[]),
        lambda raw: raw["tools"]["requests"].append(raw["tools"]["requests"][0]),
        lambda raw: raw["answer"]["requests"][0].update(service="no-such-service"),
        lambda raw: raw["reasoning_off"].update(fields={}),
        lambda raw: raw.update(skills_root="../../nowhere"),
    ],
)
def test_a_bad_definition_is_refused(probe, tmp_path, change):
    raw = json.loads(CHECKED_IN.read_text(encoding="utf-8"))
    change(raw)
    path = tmp_path / "bad.json"
    # Relative paths in the definition resolve against its folder, so point them at the checkout.
    path.write_text(
        json.dumps(raw).replace("../../", str(SCRIPTS_DIR.parent) + "/"), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        probe.load_probe_config(path)


# -- the request shapes ---------------------------------------------------------------------------


def test_the_answer_arms_offer_no_tools_and_carry_the_health_exchange(probe, shapes):
    """#116: an answer generation, a health tool exchange in the conversation, no tools."""
    for arm in probe.ARMS:
        if arm.shape != "answer":
            continue
        body = arm_body(probe, shapes, arm)
        assert "tools" not in body
        messages = body["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"].startswith(RULES[0])
        assert "Answer in English." in messages[0]["content"]
        assert messages[1]["role"] == "user"
        call = messages[2]["tool_calls"][0]
        assert call["function"]["name"] == "get_service_health"
        assert messages[3]["role"] == "tool"
        assert messages[3]["tool_call_id"] == call["id"]
        result = json.loads(messages[3]["content"])
        assert result["status"] == "succeeded"
        assert result["data"]["status"] in {"healthy", "unavailable"}


def test_the_tool_arms_offer_the_tools_of_investigate_performance(probe, shapes):
    """#116: a tool-offering generation with the skill's allowed tools, no exchange yet."""
    for arm in probe.ARMS:
        if arm.shape != "tools":
            continue
        body = arm_body(probe, shapes, arm)
        assert {tool["function"]["name"] for tool in body["tools"]} == TOOLS
        roles = [message["role"] for message in body["messages"]]
        assert roles == ["system", "user"]
        assert "Skill instructions:" in body["messages"][0]["content"]


def test_reasoning_off_is_the_only_difference_between_the_arms_of_a_shape(probe, shapes):
    """#116: default sends no reasoning field; off sends the provider's documented field."""
    by_name = {arm.name: arm for arm in probe.ARMS}
    for shape in ("answer", "tools"):
        default = arm_body(probe, shapes, by_name[f"{shape}_default"], run_id="run_same")
        off = arm_body(probe, shapes, by_name[f"{shape}_off"], run_id="run_same")
        assert "thinking" not in default
        assert off["thinking"] == {"type": "disabled"}
        assert {**off, "thinking": None} == {**default, "thinking": None}
        for body in (default, off):
            assert body["model"] == MODEL
            assert body["stream"] is True
            assert body["stream_options"] == {"include_usage": True}
            # The gateway sets no sampling option on the external stage (RM18).
            assert not {"temperature", "top_p", "max_tokens"} & set(body)


def test_every_request_has_its_own_user_text_and_run_id(probe, shapes, config):
    """Five requests of an arm differ, and the arms of a shape use the same five texts."""
    for arm in probe.ARMS:
        users = [
            next(
                m["content"]
                for m in arm_body(probe, shapes, arm, i)["messages"]
                if m["role"] == "user"
            )
            for i in range(config.repetitions)
        ]
        assert users == [r["text"] for r in config.requests(arm.shape)]
    first = arm_body(probe, shapes, probe.ARMS[0], 0)["messages"][0]["content"]
    second = arm_body(probe, shapes, probe.ARMS[0], 1)["messages"][0]["content"]
    assert first != second


# -- timing and usage -----------------------------------------------------------------------------


def test_timings_come_from_the_clock_in_the_order_of_the_stream(probe):
    """#116: headers, first reasoning delta, first content delta, end; none read twice."""
    clock = FakeClock(step=0.010)
    transport = httpx.MockTransport(lambda request: stream())
    with httpx.Client(base_url="https://provider.example", transport=transport) as client:
        row = probe.observe_call(client, "/chat/completions", {"stream": True}, {}, clock=clock)
    # started=10, headers=20, first reasoning=30, first content=40, end=50 (ms after the zero read).
    assert row["headers_ms"] == pytest.approx(10.0)
    assert row["first_reasoning_ms"] == pytest.approx(20.0)
    assert row["first_output_ms"] == pytest.approx(30.0)
    assert row["end_ms"] == pytest.approx(40.0)
    assert row["http_status"] == 200
    assert row["finish_reason"] == "stop"
    assert row["output"]["text"] == "The service is healthy."
    assert row["output"]["reasoning_chars"] == len("Let me think.")
    assert row["response_model"] == "provider-model-x-0101"


def test_a_tool_call_delta_is_the_first_output(probe):
    transport = httpx.MockTransport(lambda request: stream(reasoning=False, tool_call=True))
    with httpx.Client(base_url="https://provider.example", transport=transport) as client:
        row = probe.observe_call(
            client, "/chat/completions", {"stream": True}, {}, clock=FakeClock()
        )
    assert row["first_reasoning_ms"] is None
    assert row["first_output_ms"] is not None
    assert row["output"]["tool_calls"] == [{"name": "get_metrics", "arguments": "{}"}]
    assert row["finish_reason"] == "tool_calls"


def test_usage_is_recorded_whole_and_a_missing_count_is_null(probe):
    """RM16: a count the provider did not return is null, never 0."""
    transport = httpx.MockTransport(lambda request: stream())
    with httpx.Client(base_url="https://provider.example", transport=transport) as client:
        row = probe.observe_call(client, "/chat/completions", {}, {}, clock=FakeClock())
    assert row["usage"]["prompt_tokens"] == 900
    assert row["prompt_tokens"] == 900
    assert row["completion_tokens"] == 20
    assert row["cached_tokens"] == 128
    assert row["reasoning_tokens"] == 7

    transport = httpx.MockTransport(lambda request: stream(reasoning_tokens=False))
    with httpx.Client(base_url="https://provider.example", transport=transport) as client:
        row = probe.observe_call(client, "/chat/completions", {}, {}, clock=FakeClock())
    assert row["reasoning_tokens"] is None

    transport = httpx.MockTransport(lambda request: stream(usage=False))
    with httpx.Client(base_url="https://provider.example", transport=transport) as client:
        row = probe.observe_call(client, "/chat/completions", {}, {}, clock=FakeClock())
    assert row["usage"] is None
    assert row["prompt_tokens"] is None
    assert row["cached_tokens"] is None


def test_an_error_status_is_a_row_with_its_error(probe):
    transport = httpx.MockTransport(lambda request: httpx.Response(429, text="slow down"))
    with httpx.Client(base_url="https://provider.example", transport=transport) as client:
        row = probe.observe_call(client, "/chat/completions", {}, {}, clock=FakeClock())
    assert row["http_status"] == 429
    assert "429" in row["error"] and "slow down" in row["error"]
    assert row["first_output_ms"] is None


# -- the run --------------------------------------------------------------------------------------


def test_the_run_sends_twenty_calls_in_rounds_of_the_four_arms(probe, config, shapes):
    """#116: four arms of five requests, interleaved so drift in the provider is spread."""
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return stream()

    result = run(probe, config, shapes, handler)
    assert result["calls_made"] == len(seen) == 20
    rows = result["rows"]
    assert [row["position"] for row in rows] == list(range(20))
    for arm in probe.ARMS:
        arm_rows = [row for row in rows if row["arm"] == arm.name]
        assert [row["index"] for row in arm_rows] == [0, 1, 2, 3, 4]
    for round_number in range(5):
        assert {row["arm"] for row in rows[4 * round_number : 4 * round_number + 4]} == {
            arm.name for arm in probe.ARMS
        }
    # The first call of the run also pays for the connection; the row says so.
    assert [row["first_call_of_run"] for row in rows] == [True] + [False] * 19


def test_the_run_never_makes_more_calls_than_its_cap(probe, config, shapes):
    """The owner's hard cap: a plan over the cap is refused before any call."""
    sent = []

    def handler(request):
        sent.append(request)
        return stream()

    with pytest.raises(ValueError, match="cap"):
        run(probe, config, shapes, handler, max_calls=19)
    assert sent == []


def test_a_failed_call_is_not_retried(probe, config, shapes):
    """Every retry would be another provider call; an error is a row."""
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(500, text="boom")

    result = run(probe, config, shapes, handler)
    assert len(sent) == 20
    assert all(row["error"] for row in result["rows"])


def test_the_credential_is_sent_but_never_recorded(probe, config, shapes):
    """RM12, spec section 3: the key is in the header only, and a provider echo is scrubbed."""
    headers_seen = []

    def handler(request):
        headers_seen.append(request.headers["authorization"])
        return httpx.Response(
            401,
            headers={"x-echo": f"Bearer {KEY}", "content-type": "text/plain"},
            text=f"bad credential {KEY}",
        )

    result = run(probe, config, shapes, handler)
    assert set(headers_seen) == {f"Bearer {KEY}"}
    document = probe.build_export(
        result,
        config=config,
        command=["probe", "--api-key-env", "K"],
        provider={"api_key_env": "K"},
    )
    text = json.dumps(document, ensure_ascii=False)
    assert KEY not in text
    assert "authorization" not in text.lower()
    assert "[redacted]" in text
    assert "api_key_env" in text


def test_the_export_says_what_it_is(probe, config, shapes):
    result = run(probe, config, shapes, lambda request: stream())
    document = probe.build_export(result, config=config, command=["probe"])
    assert document["kind"] == "relaymind.probe.external_latency"
    assert "single-pass" in document["label"]
    assert document["provider"]["base_url"] == "https://provider.example/api/v4"
    assert document["provider"]["model"] == MODEL
    assert document["calls_made"] == 20
    assert document["reasoning_off"]["fields"] == OFF_FIELDS
    # One body per arm, so the options used are in the record.
    assert set(document["example_bodies"]) == {arm.name for arm in probe.ARMS}
    # Observations, never percentiles (spec 12.3).
    text = json.dumps(document["summary"])
    assert "p50" not in text and "p95" not in text
    assert {entry["arm"] for entry in document["summary"]} == {arm.name for arm in probe.ARMS}


def test_main_writes_an_export_and_prints_no_credential(probe, monkeypatch, tmp_path, capsys):
    """The command reads the key from the named variable and writes and prints none of it."""
    monkeypatch.setenv("TEST_PROBE_KEY", KEY)
    real = probe.run_probe

    def with_mock_transport(*args, **kwargs):
        return real(*args, **kwargs, transport=httpx.MockTransport(lambda request: stream()))

    monkeypatch.setattr(probe, "run_probe", with_mock_transport)
    out = tmp_path / "export.json"
    probe.main(
        [
            str(CHECKED_IN),
            "--out",
            str(out),
            "--base-url",
            "https://provider.example/api/v4",
            "--model",
            MODEL,
            "--api-key-env",
            "TEST_PROBE_KEY",
        ]
    )
    text = out.read_text(encoding="utf-8")
    document = json.loads(text)
    assert KEY not in text
    assert document["calls_made"] == 20
    assert document["provider"]["api_key_env"] == "TEST_PROBE_KEY"
    assert document["provider"]["region_known"] is False
    assert document["application"]["head"]
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err


def test_main_refuses_to_start_without_the_key(probe, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("TEST_PROBE_KEY", raising=False)
    with pytest.raises(SystemExit) as stopped:
        probe.main(
            [
                str(CHECKED_IN),
                "--out",
                str(tmp_path / "export.json"),
                "--base-url",
                "https://provider.example/api/v4",
                "--model",
                MODEL,
                "--api-key-env",
                "TEST_PROBE_KEY",
            ]
        )
    assert stopped.value.code == 2
    assert "TEST_PROBE_KEY is not set" in capsys.readouterr().err
    assert not (tmp_path / "export.json").exists()
