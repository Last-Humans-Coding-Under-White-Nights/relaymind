"""The latency benchmark runner, over model doubles (spec 0.28 section 12.2, #33; RM25).

scripts/latency_benchmark.py starts the gateway with ``relaymind serve
--config`` (routing cache off, no external model), sends the twelve
prompts through the compatible chat stream, and takes the section 12.1
metrics: first transport event and first answer token on the client's
clock, first model action and first useful content from the trace.
Three warm-ups, then ten runs per prompt in a seeded random order;
errors and timeouts are recorded, never dropped. These tests drive the
runner against the launcher's app over scripted model doubles.
"""

import asyncio
import json
from collections import Counter
from pathlib import Path

import httpx
import pytest

from gateway_helpers import clarify_turn, health_tail, route_turn
from helpers import ScriptedAdapter, StuckAdapter, load_script_module
from launcher_helpers import make_artifact
from relaymind.launcher import build_app, load_config

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def bench():
    return load_script_module("latency_benchmark")


def serve(bench, tmp_path, adapter):
    manifest, artifact = make_artifact(tmp_path)
    path = tmp_path / "relaymind.toml"
    path.write_text(
        bench.benchmark_config(
            work=tmp_path / "work",
            manifest=manifest,
            artifact=artifact,
            local_url="http://127.0.0.1:8033",
            port=8099,
        ),
        "utf-8",
    )
    config = load_config(path)
    launched = build_app(config, local_adapter=adapter)
    api = bench.harness.Api(
        "http://127.0.0.1",
        launched.app.state.application_token,
        sandbox_dir=config.sandbox_dir,
        transport=httpx.ASGITransport(app=launched.app),
    )
    return api, config


def measure(bench, api, prompt, label="simple", timeout=30.0):
    async def go():
        try:
            return await bench.measure(
                api, {"id": "lat-x", "label": label, "prompt": prompt}, timeout=timeout
            )
        finally:
            await api.aclose()

    return asyncio.run(go())


def test_the_prompts_are_twelve_short_english_prompts(bench):
    prompts = bench.load_prompts()
    assert len(prompts) == 12
    assert len({p["id"] for p in prompts}) == 12
    labels = Counter(p["label"] for p in prompts)
    assert set(labels) == {"simple", "clarification", "routing"}
    assert all(len(p["prompt"].split()) <= 8 and p["prompt"].isascii() for p in prompts)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["prompts"].pop(),
        lambda d: d["prompts"][0].update(label="other"),
        lambda d: d["prompts"][1].update(id="lat-01"),
        lambda d: d["prompts"][0].update(extra=1),
    ],
)
def test_malformed_prompt_files_are_refused(bench, tmp_path, change):
    document = json.loads((REPO / "evaluation" / "latency_prompts.v1.json").read_text("utf-8"))
    change(document)
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps(document), "utf-8")
    with pytest.raises(ValueError):
        bench.load_prompts(path)


def test_the_schedule_runs_each_prompt_ten_times_in_a_seeded_random_order(bench):
    ids = [f"lat-{i:02}" for i in range(1, 13)]
    order = bench.schedule(ids, runs=10, seed=1234)
    assert len(order) == 120
    assert Counter(order) == {i: 10 for i in ids}
    assert order == bench.schedule(ids, runs=10, seed=1234)
    assert order != bench.schedule(ids, runs=10, seed=1235)
    assert order != sorted(order) and order != ids * 10


def test_the_warmups_cover_each_label_once(bench):
    warmups = bench.warmup_prompts(bench.load_prompts())
    assert len(warmups) == 3
    assert {p["label"] for p in warmups} == {"simple", "clarification", "routing"}


def test_the_config_has_the_cache_off_and_no_external_model(bench, tmp_path):
    """Section 12.2: exact-route caching stays off; the local path only."""
    _, config = serve(bench, tmp_path, ScriptedAdapter(turns=[]))
    assert config.routing_cache is False
    assert config.external is None


def test_a_simple_prompt_gives_every_metric(bench, tmp_path):
    adapter = ScriptedAdapter(turns=[route_turn("check-service-health"), *health_tail()])
    api, _ = serve(bench, tmp_path, adapter)
    sample = measure(bench, api, "Is staging-api healthy?")
    assert sample["error"] is None and sample["timed_out"] is False
    assert sample["state"] == "completed" and sample["http_status"] == 200
    for metric in bench.LATENCY_METRICS:
        assert isinstance(sample[metric], float), metric
    # The answer comes after the tool result, so useful content is not before the action.
    assert sample["first_useful_content_ms"] >= sample["first_model_action_ms"]
    assert sample["model_calls"] and all("cached_tokens" in c for c in sample["model_calls"])


def test_a_clarification_is_the_useful_content_and_the_run_is_cancelled(bench, tmp_path):
    adapter = ScriptedAdapter(
        turns=[
            clarify_turn("Which service do you mean?"),
            route_turn("check-service-health"),
            *health_tail(),
        ]
    )
    api, _ = serve(bench, tmp_path, adapter)

    async def go():
        try:
            first = await bench.measure(
                api,
                {"id": "lat-07", "label": "clarification", "prompt": "Is it healthy?"},
                timeout=30,
            )
            second = await bench.measure(
                api,
                {"id": "lat-01", "label": "simple", "prompt": "Is staging-api healthy?"},
                timeout=30,
            )
            return first, second
        finally:
            await api.aclose()

    first, second = asyncio.run(go())
    assert first["error"] is None and first["state"] == "waiting_input"
    assert first["first_useful_content_ms"] is not None
    assert first["cancelled_after"] is True
    # The waiting run was cancelled, so the next run could start.
    assert second["error"] is None and second["state"] == "completed"


def test_a_routing_prompt_ends_as_needs_external(bench, tmp_path):
    api, _ = serve(bench, tmp_path, ScriptedAdapter(turns=[route_turn("investigate-performance")]))
    sample = measure(bench, api, "Why is payments-api failing?", label="routing")
    assert sample["error"] is None
    assert (sample["state"], sample["error_code"]) == ("failed", "needs_external")
    assert sample["first_model_action_ms"] is not None
    assert sample["first_answer_token_ms"] is None


def test_an_unexpected_outcome_is_recorded_as_an_error(bench, tmp_path):
    api, _ = serve(bench, tmp_path, ScriptedAdapter(turns=[route_turn("investigate-performance")]))
    sample = measure(bench, api, "Is staging-api healthy?", label="simple")
    assert sample["error"] is not None and "failed" in sample["error"]
    assert sample["timed_out"] is False


def test_a_request_without_a_response_in_time_is_a_timeout(bench, tmp_path):
    api, _ = serve(bench, tmp_path, StuckAdapter())
    sample = measure(bench, api, "Is staging-api healthy?", timeout=0.5)
    assert sample["timed_out"] is True and sample["error"] == "timeout"
    assert sample["first_answer_token_ms"] is None


def test_first_useful_content_needs_evidence_or_a_question(bench):
    events = [
        {"event_type": "run.accepted", "elapsed_ms": 0, "actor": "gateway", "payload": {}},
        {
            "event_type": "answer.delta",
            "elapsed_ms": 5,
            "actor": "local",
            "payload": {"text": "Ok"},
        },
    ]
    assert bench.first_useful_content_ms(events) is None
    events += [
        {
            "event_type": "tool.finished",
            "elapsed_ms": 10,
            "actor": "gateway",
            "payload": {"result": {"status": "succeeded"}},
        },
        {
            "event_type": "answer.delta",
            "elapsed_ms": 12,
            "actor": "local",
            "payload": {"text": "x"},
        },
    ]
    assert bench.first_useful_content_ms(events) == 12.0
    question = [
        {
            "event_type": "clarification.required",
            "elapsed_ms": 7,
            "actor": "gateway",
            "payload": {"question": "Which?"},
        }
    ]
    assert bench.first_useful_content_ms(question) == 7.0


def test_a_failed_tool_result_is_not_evidence(bench):
    events = [
        {
            "event_type": "tool.finished",
            "elapsed_ms": 10,
            "actor": "gateway",
            "payload": {"result": {"status": "failed"}},
        },
        {
            "event_type": "answer.delta",
            "elapsed_ms": 12,
            "actor": "local",
            "payload": {"text": "x"},
        },
    ]
    assert bench.first_useful_content_ms(events) is None


def test_the_run_keeps_cold_warmup_and_measured_samples_apart(bench):
    prompts = bench.load_prompts()
    seen = []

    async def fake_measure(api, prompt, *, timeout):
        seen.append(prompt["id"])
        return {
            "prompt_id": prompt["id"],
            "label": prompt["label"],
            "timed_out": False,
            "error": None,
        }

    result = asyncio.run(
        bench.run_samples(None, prompts, seed=9, runs=10, measure_one=fake_measure)
    )
    assert result["cold"]["prompt_id"] == prompts[0]["id"]
    assert len(result["warmups"]) == 3
    assert len(result["measured"]) == 120
    assert [s["prompt_id"] for s in result["measured"]] == bench.schedule(
        [p["id"] for p in prompts], runs=10, seed=9
    )
    assert [s["order"] for s in result["measured"]] == list(range(120))
    assert len(seen) == 124


def test_the_prefix_cache_setting_is_read_from_the_adapter(bench):
    record = bench.prefix_cache_record([{"cached_tokens": 0}, {"cached_tokens": 512}])
    assert record["adapter_sets_cache_prompt"] is False
    assert record["calls"] == 2 and record["calls_with_cached_tokens"] == 1
