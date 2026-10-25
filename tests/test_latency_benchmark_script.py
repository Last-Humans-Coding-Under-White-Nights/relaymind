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

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

import httpx
import pytest

from api_helpers import RunningServer
from gateway_helpers import clarify_turn, health_tail, route_turn
from helpers import ScriptedAdapter, StuckAdapter, load_script_module
from launcher_helpers import make_artifact
from relaymind.launcher import build_app, load_config
from relaymind.orchestrator.performance import PerformanceSettings

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def bench():
    return load_script_module("latency_benchmark")


def serve(bench, tmp_path, adapter, **options):
    manifest, artifact = make_artifact(tmp_path)
    path = tmp_path / "relaymind.toml"
    path.write_text(
        bench.benchmark_config(
            **options,
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


# -- the startup warm-up (#124; spec 0.31 sections 6 and 14.1) ---------------------------------


def test_the_benchmark_gateway_warms_up_unless_told_not_to(bench, tmp_path):
    """Section 6: the default launch warms the model up; --no-startup-warmup keeps it cold."""
    _, warmed = serve(bench, tmp_path, ScriptedAdapter(turns=[]))
    assert warmed.warmup is True
    (tmp_path / "cold").mkdir()
    _, cold = serve(bench, tmp_path / "cold", ScriptedAdapter(turns=[]), startup_warmup=False)
    assert cold.warmup is False
    assert cold.routing_cache is False and cold.external is None


# -- early response headers (RM32, spec 0.32 sections 7.3 and 12.1; #140) ---------------------


def test_the_baseline_config_has_early_headers_off(bench, tmp_path):
    """Section 12.1: false, the default, is the latency baseline."""
    _, config = serve(bench, tmp_path, ScriptedAdapter(turns=[]))
    assert config.performance.early_headers is False


def test_the_early_headers_config_turns_only_that_option_on(bench, tmp_path):
    """RM32: the benchmark names the option in the launch file, as every run records it."""
    _, config = serve(bench, tmp_path, ScriptedAdapter(turns=[]), early_headers=True)
    assert config.performance.early_headers is True
    assert config.performance == PerformanceSettings(early_headers=True)
    assert config.routing_cache is False and config.external is None


def test_a_simple_prompt_gives_every_metric_under_early_headers(bench, tmp_path):
    """RM20, RM32: the keep-alive comments are not an answer token."""
    adapter = ScriptedAdapter(turns=[route_turn("check-service-health"), *health_tail()])
    api, _ = serve(bench, tmp_path, adapter, early_headers=True)
    sample = measure(bench, api, "Is staging-api healthy?")
    assert sample["error"] is None and sample["state"] == "completed"
    for metric in bench.LATENCY_METRICS:
        assert isinstance(sample[metric], float), metric
    # The transport event is the header time; useful content still comes from the trace.
    assert sample["first_useful_content_ms"] >= sample["first_model_action_ms"]


def test_a_routing_prompt_under_early_headers_is_not_a_transport_error(bench, tmp_path):
    """RM32, section 12.2: the mid-stream error event and the close after it are the outcome.

    Over a real socket the gateway sends the 200 status, the needs_external error event, and
    then closes the connection on purpose. That close is not a transport error: the sample
    keeps its completion time and is checked against the expected outcome like the baseline.
    """
    adapter = ScriptedAdapter(turns=[route_turn("investigate-performance")])
    _, config = serve(bench, tmp_path, adapter, early_headers=True)
    launched = build_app(config, local_adapter=adapter)
    with RunningServer(launched.app) as server:
        api = bench.harness.Api(
            server.origin,
            launched.app.state.application_token,
            sandbox_dir=config.sandbox_dir,
        )
        sample = measure(bench, api, "Why is payments-api failing?", label="routing")
    assert sample["http_status"] == 200
    assert sample["error"] is None
    assert sample["stream_error_code"] == "needs_external"
    assert (sample["state"], sample["error_code"]) == ("failed", "needs_external")
    assert sample["completion_ms"] is None
    assert isinstance(sample["stream_duration_ms"], float)
    assert sample["first_answer_token_ms"] is None


def test_a_connection_close_without_an_error_event_is_still_a_transport_error(bench):
    """Section 12.2: only a close that follows the gateway's own error event is expected."""

    class Cut(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b": keep-alive\n\n"
            raise httpx.RemoteProtocolError("peer closed connection")

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=Cut(),
        )

    api = bench.harness.Api(
        "http://127.0.0.1", "token", sandbox_dir=Path("."), transport=httpx.MockTransport(handler)
    )
    sample = measure(bench, api, "Is staging-api healthy?")
    assert sample["error"].startswith("transport: RemoteProtocolError")
    assert sample["stream_error_code"] is None


def test_the_first_transport_event_is_labeled_by_the_option(bench):
    """Spec 0.32 section 7.3: with early headers it is the header time, never useful content."""
    assert "header time" in bench.first_transport_label(True)
    assert "not first useful content" in bench.first_transport_label(True)
    assert "header" not in bench.first_transport_label(False)
    assert bench.first_transport_label(True) != bench.first_transport_label(False)


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
    # Spec 0.31 sections 6 and 8: the counts and the first token are read from the trace;
    # a double that reports none leaves them None, never 0.
    for call in sample["model_calls"]:
        assert {"reasoning_tokens", "first_token_ms"} <= set(call)
        assert call["cached_tokens"] is None and call["reasoning_tokens"] is None


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
            "payload": {"status": "succeeded", "result": {"status": "succeeded"}},
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
            "payload": {"status": "failed", "result": {"status": "failed"}},
        },
        {
            "event_type": "answer.delta",
            "elapsed_ms": 12,
            "actor": "local",
            "payload": {"text": "x"},
        },
    ]
    assert bench.first_useful_content_ms(events) is None


def test_an_offloaded_tool_result_is_read_by_its_status(bench):
    """A large result is stored as result_ref (section 8); the event status still counts."""
    events = [
        {
            "event_type": "tool.finished",
            "elapsed_ms": 10,
            "actor": "gateway",
            "payload": {
                "status": "succeeded",
                "result_ref": {
                    "artifact_id": "artifact_x",
                    "uri": "sqlite:artifact/artifact_x",
                    "size_bytes": 70000,
                    "truncated": False,
                },
            },
        },
        {
            "event_type": "answer.delta",
            "elapsed_ms": 12,
            "actor": "local",
            "payload": {"text": "x"},
        },
    ]
    assert bench.first_useful_content_ms(events) == 12.0


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
    """RM25, spec 0.31 sections 6 and 12.2: cache_prompt is sent; None is not measured."""
    record = bench.prefix_cache_record(
        [{"cached_tokens": 0}, {"cached_tokens": 512}, {"cached_tokens": None}, {}]
    )
    assert record["adapter_sets_cache_prompt"] is True
    assert "sends cache_prompt" in record["note"]
    assert "not measured" in record["note"]
    assert record["calls"] == 4
    assert record["calls_with_reported_count"] == 2
    assert record["calls_not_measured"] == 2
    assert record["calls_with_cached_tokens"] == 1
    assert record["cached_tokens_total"] == 512


def test_external_cache_tokens_are_not_attributed_to_the_local_runtime(bench):
    """RM25: mixed configurations retain call ownership and isolate local cache counts."""
    events = [
        {
            "event_type": "model.finished",
            "actor": actor,
            "payload": {
                "call_id": actor,
                "model": model,
                "purpose": "skill",
                "first_token_ms": 10,
                "usage": {"cached_tokens": cached},
            },
        }
        for actor, model, cached in (("local", "small", 7), ("external", "large", 500))
    ]
    calls = bench._model_calls(events)
    assert [(c["actor"], c["model"]) for c in calls] == [("local", "small"), ("external", "large")]
    assert bench.prefix_cache_record(calls)["cached_tokens_total"] == 7
    assert bench.prefix_cache_record(calls)["calls"] == 1


def test_a_run_without_any_reported_count_has_no_cached_total(bench):
    """A total over nothing measured is null, not 0."""
    record = bench.prefix_cache_record([{"cached_tokens": None}, {"cached_tokens": None}])
    assert record["calls_with_reported_count"] == 0
    assert record["cached_tokens_total"] is None
    assert record["calls_with_cached_tokens"] is None


def test_an_error_status_without_a_run_is_an_error(bench):
    """Section 12.2: an error status the label does not expect is recorded as an error."""

    def handler(request):
        return httpx.Response(503, json={"error": {"code": "unavailable"}})

    api = bench.harness.Api(
        "http://127.0.0.1", "token", sandbox_dir=Path("."), transport=httpx.MockTransport(handler)
    )
    sample = measure(bench, api, "Is staging-api healthy?")
    assert sample["http_status"] == 503
    assert sample["error"] == "no run ID in the response (status 503)"
    assert sample["first_model_action_ms"] is None


def test_a_malformed_stream_line_is_recorded_as_an_error(bench):
    """Section 12.2: errors are recorded with their sample; one bad chunk ends no benchmark."""

    def handler(request):
        return httpx.Response(
            200, text="data: {not json\n\n", headers={"content-type": "text/event-stream"}
        )

    api = bench.harness.Api(
        "http://127.0.0.1", "token", sandbox_dir=Path("."), transport=httpx.MockTransport(handler)
    )
    sample = measure(bench, api, "Is staging-api healthy?")
    assert sample["http_status"] == 200 and sample["timed_out"] is False
    assert sample["error"].startswith("stream: JSONDecodeError")


class _FakeRuntime:
    """A runtime whose llama-server launch is a stand-in (no process)."""

    def launch(self, log):
        return object(), "http://127.0.0.1:1", 0.0


def test_an_interrupted_benchmark_removes_its_work_dir(bench, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    stopped = []

    def interrupt(url):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        bench.harness, "check_runtime", lambda manifest, extra_args=(): _FakeRuntime()
    )
    monkeypatch.setattr(bench.harness, "application_revision", lambda: {})
    monkeypatch.setattr(bench.session, "prior_configuration", lambda *args: {})
    monkeypatch.setattr(bench.harness, "_stop", stopped.append)
    monkeypatch.setattr(bench.tempfile, "mkdtemp", lambda prefix: str(work))
    monkeypatch.setattr(bench, "_props", interrupt)
    args = argparse.Namespace(
        manifest="m.json", runtime_extra_args=(), seed=1, runs=1, timeout=1.0, out="x.json"
    )
    with pytest.raises(KeyboardInterrupt):
        bench.run_benchmark(args)
    assert not work.exists()
    assert len(stopped) == 1


# -- the warm-up samples in the export (#124; spec 0.31 section 12.2) --------------------------

WARMUP_LINE = (
    "Local warm-up finished: routing 364 ms (first token 120 ms, cached 946), "
    "skill 210 ms (first token n/a, cached n/a)."
)


def test_the_warmup_samples_are_read_from_the_launcher_output(bench):
    """Section 12.2: with warm-up on, the export holds the warm-up samples; n/a stays None."""
    lines = ["Serving on http://127.0.0.1:8099", WARMUP_LINE, "Application token: <not recorded>"]
    record = bench.startup_warmup_record(lines, startup_warmup=True)
    assert record["line"] == WARMUP_LINE
    assert record["samples"] == [
        {"purpose": "routing", "duration_ms": 364, "first_token_ms": 120, "cached_tokens": 946},
        {"purpose": "skill", "duration_ms": 210, "first_token_ms": None, "cached_tokens": None},
    ]


def test_no_warmup_samples_are_invented(bench):
    """Honest evidence: warm-up off, or a launcher that printed no warm-up line, has none."""
    off = bench.startup_warmup_record([WARMUP_LINE], startup_warmup=False)
    assert off == {"enabled": False, "line": None, "samples": None}
    silent = bench.startup_warmup_record(["Serving on http://127.0.0.1:8099"], startup_warmup=True)
    assert silent == {"enabled": True, "line": None, "samples": None}


def test_latency_trace_collection_retains_http_fallback_claims(bench):
    """RM16/RM25/RM28: HTTP attempts remain separate from observed generation samples."""
    attempt = dict(
        name="remote",
        version="1",
        transport="http",
        outcome="none",
        elapsed_ms=1,
        model_calls_claim=[],
    )

    class Api:
        async def view(self, run_id):
            return {"state": "failed", "error_code": "needs_external"}

        async def events(self, run_id):
            return [{"event_type": "run.failed", "payload": {"selector": {"attempts": [attempt]}}}]

    sample = {"run_id": "r", "state": "failed", "error_code": None}
    asyncio.run(bench._read_trace(Api(), "r", sample))
    assert sample["selector_provenance"]["scope"] == "http_attempted"
    assert sample["selector_provenance"]["attempts"] == [attempt]
    assert sample["model_calls"] == []


@pytest.mark.parametrize(
    "local,warmup_disabled,expected",
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_effective_configuration_keeps_checkpoint_settings_raw(
    bench, local, warmup_disabled, expected
):
    """RM25: effective warm-up is shared without mutating raw checkpoint identity."""
    argv = ["--out", "fixture.json", "--placement", "policy=expected_wait"]
    if not local:
        argv.append("--no-local-model")
    if warmup_disabled:
        argv.append("--no-startup-warmup")
    args = bench.argument_parser().parse_args(argv)
    configuration = bench.configuration_from_args(args)
    raw = configuration.to_json_object()
    effective = bench.effective_configuration(
        configuration, no_startup_warmup=args.no_startup_warmup
    )
    assert effective["placement"]["warmup"] is expected
    assert effective["placement"]["policy"] == "expected_wait"
    assert configuration.to_json_object() == raw
    assert "warmup" not in raw["placement"]
