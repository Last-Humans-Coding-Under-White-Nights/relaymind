"""The prefix-cache probe (#114, #117; RM27, RM25, spec 0.31 sections 6 and 10).

The probe sends the gateway's own routing and skill-stage requests to
llama-server over raw HTTP, with cache_prompt on and then off, and reads
the whole timings and usage objects back. The live run is an operator
action recorded in docs/EVIDENCE.md. These tests use a mock transport:
what is sent is what the adapter's request builder encodes, and a count
the runtime did not return is recorded as absent, never as 0 (RM16).
"""

import json

import httpx
import pytest

from helpers import SCRIPTS_DIR, load_script_module, sse_response
from relaymind.adapters.llamacpp import build_chat_request

CHECKED_IN = SCRIPTS_DIR / "probes" / "m7_prefix_cache.json"
KWARGS = {"enable_thinking": False}


@pytest.fixture(scope="module")
def probe():
    return load_script_module("probes/probe_prefix_cache")


@pytest.fixture(scope="module")
def config(probe):
    return probe.load_probe_config(CHECKED_IN)


@pytest.fixture()
def surfaces(probe, config, tmp_path):
    return probe.build_surfaces(
        skills_root=config.skills_root,
        sandbox_seed=config.sandbox_seed,
        skill=config.skill,
        work_dir=tmp_path,
    )


def chat_chunks(*, timings, usage, text="ok"):
    chunks = [{"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    last = {"choices": []}
    if usage is not None:
        last["usage"] = usage
    if timings is not None:
        last["timings"] = timings
    chunks.append(last)
    return chunks


class FakeLlamaServer:
    """A llama-server stand-in over a mock transport; records the chat bodies it receives."""

    def __init__(self, *, timings=True, usage=True):
        self.chat_bodies: list[dict] = []
        self.with_timings = timings
        self.with_usage = usage

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/apply-template":
            body = json.loads(request.content)
            return httpx.Response(200, json={"prompt": json.dumps(body, sort_keys=True)})
        if path == "/tokenize":
            content = json.loads(request.content)["content"]
            return httpx.Response(200, json={"tokens": [ord(c) for c in content]})
        assert path == "/v1/chat/completions"
        body = json.loads(request.content)
        self.chat_bodies.append(body)
        reused = body["cache_prompt"]
        timings = (
            {
                "cache_n": 90 if reused else 0,
                "prompt_n": 10 if reused else 100,
                "prompt_ms": 5.0 if reused else 40.0,
                "predicted_n": 3,
                "predicted_ms": 6.0,
            }
            if self.with_timings
            else None
        )
        usage = (
            {
                "prompt_tokens": 100,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 90 if reused else 0},
            }
            if self.with_usage
            else None
        )
        return sse_response(chat_chunks(timings=timings, usage=usage))

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://llama.test", transport=httpx.MockTransport(self.handler)
        )


def test_the_body_sent_is_the_chat_request_with_the_arm_flag(probe, config, surfaces):
    """RM27, #114: the sent body is what build_chat_request encodes, with cache_prompt = the arm.

    Written as {**build_chat_request(...), "cache_prompt": arm}, so it still holds when the
    adapter's own body carries cache_prompt (spec 0.31 section 6).
    """
    server = FakeLlamaServer()
    expected = []
    with server.client() as client:
        for layout in config.layouts:
            for arm in (True, False):
                probe.run_group(client, surfaces, config, KWARGS, layout, arm)
                for step in probe.plan_group(config, layout):
                    request = surfaces.request(step.purpose, step.text, "g")
                    expected.append(
                        {
                            **build_chat_request(request, chat_template_kwargs=KWARGS),
                            "cache_prompt": arm,
                        }
                    )
    assert server.chat_bodies == expected
    assert {body["cache_prompt"] for body in server.chat_bodies} == {True, False}


def test_a_group_has_a_cold_pair_warmups_and_ten_distinct_requests_per_purpose(probe, config):
    for layout in ("blocked", "interleaved"):
        steps = probe.plan_group(config, layout)
        phases = [s.phase for s in steps]
        assert phases == ["cold"] * 2 + ["warmup"] * 6 + ["measured"] * 20
        assert [s.purpose for s in steps[:2]] == ["routing", "skill"]
        measured = [s for s in steps if s.phase == "measured"]
        for purpose in ("routing", "skill"):
            texts = [s.text for s in measured if s.purpose == purpose]
            assert len(texts) == len(set(texts)) == 10
            assert config.warmup_prompts[purpose] not in texts
        order = [s.purpose for s in measured]
        if layout == "blocked":
            assert order == ["routing"] * 10 + ["skill"] * 10
        else:
            assert order == ["routing", "skill"] * 10


def test_the_whole_timings_and_usage_objects_are_read(probe, config, surfaces):
    server = FakeLlamaServer()
    with server.client() as client:
        rows = probe.run_group(client, surfaces, config, KWARGS, "blocked", True)
    measured = [r for r in rows if r["phase"] == "measured"]
    assert len(measured) == 20
    row = measured[0]
    assert row["timings"]["cache_n"] == 90 and row["usage"]["prompt_tokens"] == 100
    assert (row["cache_n"], row["prompt_n"], row["predicted_n"]) == (90, 10, 3)
    assert (row["prompt_ms"], row["predicted_ms"]) == (5.0, 6.0)
    assert row["cached_tokens"] == 90
    assert row["cache_prompt"] is True and row["http_status"] == 200 and row["error"] is None
    assert row["wall_ms"] >= row["first_delta_ms"] >= 0


def test_a_count_the_runtime_did_not_return_is_absent_and_not_zero(probe, config, surfaces):
    """RM16, spec 0.31 section 6: no timings and no usage are recorded as null."""
    server = FakeLlamaServer(timings=False, usage=False)
    with server.client() as client:
        rows = probe.run_group(client, surfaces, config, KWARGS, "blocked", False)
    for row in rows:
        assert row["timings"] is None and row["usage"] is None
        for key in ("cache_n", "prompt_n", "prompt_ms", "predicted_n", "cached_tokens"):
            assert row[key] is None, key
    summary = probe.summarize(rows)
    assert all(entry["reused_share"] is None for entry in summary)
    assert all(entry["cached_tokens_reported"] == 0 for entry in summary)


def test_cached_tokens_is_null_when_usage_has_no_prompt_details(probe):
    row = probe.row_fields(
        timings={"cache_n": 4, "prompt_n": 6},
        usage={"prompt_tokens": 10, "completion_tokens": 1},
    )
    assert row["cache_n"] == 4 and row["cached_tokens"] is None
    assert row["prompt_tokens"] == 10 and row["completion_tokens"] == 1


def test_the_summary_gives_the_reused_share_per_group_and_purpose(probe, config, surfaces):
    server = FakeLlamaServer()
    rows = []
    with server.client() as client:
        for arm in (True, False):
            rows += probe.run_group(client, surfaces, config, KWARGS, "blocked", arm)
    summary = {(e["cache_prompt"], e["purpose"]): e for e in probe.summarize(rows)}
    assert set(summary) == {
        (True, "routing"),
        (True, "skill"),
        (False, "routing"),
        (False, "skill"),
    }
    on, off = summary[(True, "routing")], summary[(False, "routing")]
    assert on["requests"] == off["requests"] == 10
    assert on["reused_share"] == 0.9 and off["reused_share"] == 0.0
    assert on["cache_n"] == {"min": 90, "max": 90} and off["cache_n"] == {"min": 0, "max": 0}
    assert on["cached_tokens_reported"] == 10


def test_a_failed_request_is_recorded_and_the_pass_goes_on(probe, config, surfaces):
    def handler(request):
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(503, json={"error": {"message": "loading model"}})
        return FakeLlamaServer().handler(request)

    with httpx.Client(
        base_url="http://llama.test", transport=httpx.MockTransport(handler)
    ) as client:
        rows = probe.run_group(client, surfaces, config, KWARGS, "blocked", True)
    assert len(rows) == 28
    assert all(r["http_status"] == 503 and r["error"] for r in rows)
    assert all(r["cache_n"] is None for r in rows)


def test_the_common_prefix_of_token_lists(probe):
    assert probe.common_prefix_length([[1, 2, 3, 4], [1, 2, 9], [1, 2, 3]]) == 2
    assert probe.common_prefix_length([[1, 2], [3]]) == 0
    assert probe.common_prefix_length([[5, 6]]) == 2
    assert probe.common_prefix_length([]) == 0
