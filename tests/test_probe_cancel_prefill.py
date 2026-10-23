"""The cancel-during-prefill probe (#117; RM14, RM26, RM27, spec 0.31 sections 12.2 and 14.1).

A long prompt goes through LlamaCppAdapter, is cancelled after a short
time, and a short routing-shaped request follows at once; the probe
records how long that request waits next to the same request on an idle
runtime. The live run is an operator action recorded in docs/EVIDENCE.md.
These tests use a mock llama-server: the order of the requests, a prompt
sized to the context, and prompts that never repeat a prefix.
"""

import asyncio
import dataclasses
import json
import time

import httpx
import pytest

from helpers import SCRIPTS_DIR, load_script_module, sse_response
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig

# The probe measures with time.monotonic, and the event loop schedules its sleeps on the
# same clock. Where that clock is coarse (about 15.6 ms on Windows), a measured delay can
# read up to two ticks short of the requested one; elsewhere the slack is negligible.
CLOCK_SLACK_MS = 2 * time.get_clock_info("monotonic").resolution * 1000

CHECKED_IN = SCRIPTS_DIR / "probes" / "m7_cancel_prefill.json"
KWARGS = {"enable_thinking": False}


@pytest.fixture(scope="module")
def probe():
    return load_script_module("probes/probe_cancel_prefill")


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


class FakeLlamaServer:
    """A llama-server stand-in: four chars per token, a slow long prompt, a log of the requests."""

    def __init__(self, *, long_seconds=0.25):
        self.log: list[tuple[str, str]] = []
        self.long_prompts: list[str] = []
        self.long_seconds = long_seconds

    @staticmethod
    def _is_long(body) -> bool:
        return body["messages"][0]["content"].startswith("Capture ")

    def _answer(self, request: httpx.Request):
        path = request.url.path
        if path == "/apply-template":
            return httpx.Response(200, json={"prompt": json.dumps(json.loads(request.content))})
        if path == "/tokenize":
            content = json.loads(request.content)["content"]
            return httpx.Response(200, json={"tokens": list(range(len(content) // 4))})
        if path == "/props":
            return httpx.Response(
                200,
                json={"default_generation_settings": {"n_ctx": 8192}, "total_slots": 4},
            )
        if path == "/slots":
            return httpx.Response(200, json=[{"id": 0, "is_processing": False}])
        assert path == "/v1/chat/completions"
        return None

    def _chat(self, body):
        timings = {"cache_n": 5, "prompt_n": 7, "prompt_ms": 3.0, "predicted_n": 2}
        usage = {"prompt_tokens": 12, "completion_tokens": 2}
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": usage, "timings": timings},
        ]
        return sse_response(chunks)

    def sync_handler(self, request: httpx.Request) -> httpx.Response:
        answer = self._answer(request)
        if answer is not None:
            return answer
        body = json.loads(request.content)
        self.log.append(("short", body["messages"][-1]["content"]))
        return self._chat(body)

    async def async_handler(self, request: httpx.Request) -> httpx.Response:
        answer = self._answer(request)
        if answer is not None:
            return answer
        body = json.loads(request.content)
        if self._is_long(body):
            self.log.append(("long", body["messages"][0]["content"]))
            self.long_prompts.append(body["messages"][0]["content"])
            await asyncio.sleep(self.long_seconds)
        else:
            self.log.append(("short", body["messages"][-1]["content"]))
        return self._chat(body)

    def adapter(self) -> LlamaCppAdapter:
        return LlamaCppAdapter(
            LlamaCppConfig(
                base_url="http://llama.test", context_limit=8192, chat_template_kwargs=KWARGS
            ),
            transport=httpx.MockTransport(self.async_handler),
        )

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://llama.test", transport=httpx.MockTransport(self.sync_handler)
        )


def small(config, probe, **changes):
    values = {"target_tokens": 400, "cancel_after_ms": 20, "repetitions": 2, "warmups": 1}
    return dataclasses.replace(config, **{**values, **changes})


def test_the_prompt_is_sized_to_the_target_and_fits_the_context(probe, config):
    server = FakeLlamaServer()

    async def go():
        async with server.adapter() as adapter:
            return await probe.size_long_prompt(adapter, config, target=400)

    lines, tokens = asyncio.run(go())
    assert lines > 0 and abs(tokens - 400) <= 8

    # The target never exceeds the context a slot has, less room for the answer.
    assert probe.fit_target(6000, n_ctx=8192, output_tokens=8) == 6000
    assert probe.fit_target(6000, n_ctx=4096, output_tokens=8) == 4096 - 8 - probe.CONTEXT_MARGIN
    assert probe.fit_target(6000, n_ctx=None, output_tokens=8) == 6000


def test_a_repetition_sends_idle_then_long_then_short_and_cancels_the_long_one(
    probe, config, surfaces
):
    """RM14: the run is cancelled; the probe measures what the cancel left running."""
    config = small(config, probe)
    server = FakeLlamaServer()

    async def go():
        async with server.adapter() as adapter:
            with server.client() as client:
                return await probe.run_repetition(
                    adapter, client, surfaces, config, KWARGS, repetition=0, lines=20
                )

    row = asyncio.run(go())
    assert [kind for kind, _ in server.log] == ["short", "long", "short"]
    # Different short texts, so the second one is not served whole from the cache.
    assert server.log[0][1] != server.log[2][1]
    assert row["repetition"] == 0 and row["long_prompt_tokens"] > 0
    assert row["cancel"]["requested_after_ms"] == 20
    assert row["cancel"]["called_after_ms"] >= 20 - CLOCK_SLACK_MS
    assert row["cancel"]["events_before_cancel"] == 0
    assert row["idle"]["http_status"] == 200 and row["after_cancel"]["http_status"] == 200
    assert row["after_cancel"]["wall_ms"] >= row["after_cancel"]["first_delta_ms"] >= 0
    # The short request went out while the cancelled generation was still running.
    assert row["after_cancel"]["sent_after_cancel_ms"] < 150
    assert row["long_generation"]["ended_after_cancel_ms"] is not None
    assert row["long_generation"]["ended_by"] in ("completed", "cancelled", "failed")


def test_called_after_ms_is_measured_and_not_the_requested_delay(
    probe, config, surfaces, monkeypatch
):
    """RM16: a late cancel call is recorded as late (the field is a measurement)."""
    config = small(config, probe, cancel_after_ms=20)
    server = FakeLlamaServer()
    real_sleep = asyncio.sleep

    async def oversleep(delay, *args, **kwargs):
        # The probe asks for 20 ms; the loop is late and gives it back after 90 ms.
        return await real_sleep(0.09 if delay == 0.02 else delay, *args, **kwargs)

    monkeypatch.setattr(probe.asyncio, "sleep", oversleep)

    async def go():
        async with server.adapter() as adapter:
            with server.client() as client:
                return await probe.run_repetition(
                    adapter, client, surfaces, config, KWARGS, repetition=0, lines=20
                )

    row = asyncio.run(go())
    assert row["cancel"]["requested_after_ms"] == 20
    # Still well above the requested 20 ms, so the field is the measured delay.
    assert row["cancel"]["called_after_ms"] >= 85 - CLOCK_SLACK_MS


def test_the_sizing_and_reference_prompts_tokenise_like_the_repetition_prompts(probe, config):
    """RM16: the within-1% sizing and the reference prefill describe the measured prompt size."""
    server = FakeLlamaServer()

    async def go():
        async with server.adapter() as adapter:
            lines, _ = await probe.size_long_prompt(adapter, config, target=400)
            sized = probe.long_request(
                config, repetition=probe.reference_label(config), lines=lines, generation_id="s"
            )
            measured = probe.long_request(config, repetition=0, lines=lines, generation_id="m")
            return (
                await adapter.count_tokens(sized.messages, ()),
                await adapter.count_tokens(measured.messages, ()),
            )

    sized_tokens, measured_tokens = asyncio.run(go())
    assert sized_tokens == measured_tokens


def test_the_reference_prompt_shares_no_prefix_with_a_repetition(probe, config):
    label = probe.reference_label(config)
    assert label >= config.repetitions
    reference = probe.long_request(config, repetition=label, lines=10, generation_id="r")
    first = probe.long_request(config, repetition=0, lines=10, generation_id="a")
    one, two = reference.messages[0].content, first.messages[0].content
    common = 0
    while common < min(len(one), len(two)) and one[common] == two[common]:
        common += 1
    assert common <= len("Capture 0")


def test_no_two_long_prompts_share_a_prefix(probe, config):
    first = probe.long_request(config, repetition=0, lines=10, generation_id="a")
    second = probe.long_request(config, repetition=1, lines=10, generation_id="b")
    one, two = first.messages[0].content, second.messages[0].content
    common = 0
    while common < min(len(one), len(two)) and one[common] == two[common]:
        common += 1
    # Only the header word and the first digit of the number can match.
    assert common <= len("Capture 0")
    assert first.options.max_output_tokens == config.long_output_tokens


def test_the_definition_needs_a_short_prompt_pair_for_every_repetition(probe, tmp_path):
    raw = json.loads(CHECKED_IN.read_text("utf-8"))
    raw["repetitions"] = 8
    for key in ("manifest", "skills_root", "sandbox_seed"):
        raw[key] = str((CHECKED_IN.parent / raw[key]).resolve())
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(raw), "utf-8")
    with pytest.raises(ValueError, match="short_prompts"):
        probe.load_probe_config(bad)
