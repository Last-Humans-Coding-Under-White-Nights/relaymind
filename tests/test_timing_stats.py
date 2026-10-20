"""Per-generation timing and statistics collection (issue #37).

Covers RM25 / spec section 12.1: metric definitions measured with a
monotonic clock for durations and UTC for timestamps. Engineering
instrumentation for adapter comparisons, not the formal M6 benchmark.
Deterministic: scripted adapter doubles and a scripted clock.
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from helpers import ScriptedAdapter, collect_events, make_capabilities, make_request
from relaymind.adapters import (
    Completed,
    GenerationFailed,
    Message,
    Role,
    TextDelta,
    ToolCall,
    Usage,
)
from relaymind.stats import AdapterIdentity, TimingCollector, export_stats, run_comparison


def scripted_clock(*ticks):
    """A fake monotonic clock returning the given values in order."""
    values = iter(ticks)
    return lambda: next(values)


def collect_with_stats(adapter, clock, request=None):
    collector = TimingCollector(adapter, clock=clock)
    events = collect_events(collector, request or make_request())
    return collector, events


TOOL_CALL = ToolCall(call_id="call_1", tool_name="get_service_health", arguments={"service": "s"})


def test_first_model_action_is_distinct_from_first_transport_event():
    # Spec 12.1: the first transport event is any first received event;
    # the first model action is the first validated proposal (a tool
    # call), which can arrive later.
    adapter = ScriptedAdapter(
        [
            Usage(input_tokens=10, output_tokens=0),  # transport only
            TOOL_CALL,
            Completed(finish_reason="tool_calls"),
        ]
    )
    # clock: start, then one tick per event.
    collector, _ = collect_with_stats(adapter, scripted_clock(0.0, 0.1, 0.4, 0.5))
    record = collector.records[0]
    assert record.first_transport_event_seconds == 0.1
    assert record.first_model_action_seconds == 0.4


def test_first_answer_token_is_the_first_nonempty_text_delta():
    adapter = ScriptedAdapter(
        [
            TextDelta(text=""),  # empty delta: transport, not an answer
            TextDelta(text="pong"),
            Completed(finish_reason="stop"),
        ]
    )
    collector, _ = collect_with_stats(adapter, scripted_clock(0.0, 0.2, 0.3, 0.9))
    record = collector.records[0]
    assert record.first_transport_event_seconds == 0.2
    assert record.first_answer_token_seconds == 0.3
    assert record.first_model_action_seconds is None


def test_completion_latency_and_finish_reason_are_recorded():
    adapter = ScriptedAdapter([TextDelta(text="hi"), Completed(finish_reason="stop")])
    collector, _ = collect_with_stats(adapter, scripted_clock(10.0, 10.5, 12.0))
    record = collector.records[0]
    assert record.completion_seconds == 2.0
    assert record.finish_reason == "stop"
    assert record.failure_code is None


def test_usage_tokens_are_recorded_per_call():
    adapter = ScriptedAdapter(
        [
            TextDelta(text="hi"),
            Usage(input_tokens=21, output_tokens=4),
            Completed(finish_reason="stop"),
        ]
    )
    collector, _ = collect_with_stats(adapter, scripted_clock(0.0, 1.0, 2.0, 3.0))
    record = collector.records[0]
    assert record.input_tokens == 21
    assert record.output_tokens == 4
    assert record.generation_id == "gen_1"


def test_failed_generation_records_the_code_not_a_completion():
    # Spec 12.1: completion latency is request to successful final
    # answer; a failure must never masquerade as one.
    adapter = ScriptedAdapter([GenerationFailed(code="provider_timeout", message="slow")])
    collector, _ = collect_with_stats(adapter, scripted_clock(0.0, 5.0))
    record = collector.records[0]
    assert record.failure_code == "provider_timeout"
    assert record.completion_seconds is None
    assert record.finish_reason is None
    # Nothing came over the wire: the adapter synthesized the failure,
    # so there is no transport latency to report (spec 12.1, RM16).
    assert record.first_transport_event_seconds is None


def test_prompt_is_the_current_turn_in_a_multi_turn_request():
    adapter = ScriptedAdapter([TextDelta(text="hi"), Completed(finish_reason="stop")])
    request = make_request(
        messages=(
            Message(role=Role.USER, content="first question"),
            Message(role=Role.ASSISTANT, content="first answer"),
            Message(role=Role.USER, content="follow-up"),
        )
    )
    collector, _ = collect_with_stats(adapter, scripted_clock(0.0, 1.0, 2.0), request)
    assert collector.records[0].prompt == "follow-up"


class ClosableAdapter(ScriptedAdapter):
    def __init__(self, events):
        super().__init__(events)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_collector_closes_the_wrapped_adapter():
    adapter = ClosableAdapter([])
    collector = TimingCollector(adapter)
    asyncio.run(collector.aclose())
    assert adapter.closed


def test_collector_close_tolerates_an_adapter_without_aclose():
    asyncio.run(TimingCollector(ScriptedAdapter([])).aclose())


def test_events_pass_through_unchanged_and_records_accumulate():
    events_in = [TextDelta(text="hi"), Completed(finish_reason="stop")]
    adapter = ScriptedAdapter(events_in)
    collector = TimingCollector(adapter, clock=scripted_clock(0.0, 1.0, 2.0, 3.0, 4.0, 5.0))

    assert collect_events(collector, make_request(generation_id="gen_a")) == events_in
    assert collect_events(collector, make_request(generation_id="gen_b")) == events_in
    assert [record.generation_id for record in collector.records] == ["gen_a", "gen_b"]


def test_export_writes_identity_metrics_and_per_call_usage(tmp_path):
    adapter = ScriptedAdapter(
        [
            TextDelta(text="hi"),
            Usage(input_tokens=21, output_tokens=4),
            Completed(finish_reason="stop"),
        ]
    )
    collector, _ = collect_with_stats(adapter, scripted_clock(0.0, 0.5, 1.0, 2.0))
    identity = AdapterIdentity(
        adapter="llamacpp",
        model_id="qwen3-0.6b",
        quantization="Q8_0",
        runtime_version="build 11669 commit 620d2225c",
    )
    out = tmp_path / "local.json"
    export_stats(out, identity=identity, records=collector.records)

    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["adapter"] == {
        "adapter": "llamacpp",
        "model_id": "qwen3-0.6b",
        "quantization": "Q8_0",
        "runtime_version": "build 11669 commit 620d2225c",
    }
    # Working data, never published as measured results (spec 12.3).
    assert document["status"] == "engineering_instrumentation"
    # RM25: timestamps are UTC.
    exported_at = datetime.fromisoformat(document["exported_at_utc"])
    assert exported_at.utcoffset() == timedelta(0)
    (generation,) = document["generations"]
    assert generation["generation_id"] == "gen_1"
    assert generation["first_answer_token_seconds"] == 0.5
    assert generation["completion_seconds"] == 2.0
    assert generation["finish_reason"] == "stop"
    # Tokens stay per call; nothing is summed across tokenizers (12.1).
    assert generation["input_tokens"] == 21
    assert generation["output_tokens"] == 4
    assert "total" not in json.dumps(document)


def test_export_creates_missing_parent_directories(tmp_path):
    out = tmp_path / "nested" / "exports" / "local.json"
    export_stats(
        str(out),
        identity=AdapterIdentity(adapter="llamacpp", model_id="qwen3-0.6b"),
        records=[],
    )
    assert json.loads(out.read_text(encoding="utf-8"))["generations"] == []


def test_run_comparison_writes_one_export_per_adapter_config(tmp_path):
    # Issue #37: the same prompt list runs against every configured
    # adapter, and each config gets its own machine-readable export.
    adapters = {
        "local": (
            ScriptedAdapter([TextDelta(text="a"), Completed(finish_reason="stop")]),
            AdapterIdentity(adapter="llamacpp", model_id="qwen3-0.6b", quantization="Q8_0"),
        ),
        "external": (
            ScriptedAdapter([TextDelta(text="b"), Completed(finish_reason="stop")]),
            AdapterIdentity(adapter="chat_completions", model_id="provider-large-1"),
        ),
    }
    prompts = ["Reply with exactly one word: pong", "Is staging-api healthy?"]
    out_dir = tmp_path / "exports"

    paths = asyncio.run(run_comparison(adapters, prompts, out_dir))

    assert set(paths) == {"local", "external"}
    for name, identity_model in (("local", "qwen3-0.6b"), ("external", "provider-large-1")):
        document = json.loads(paths[name].read_text(encoding="utf-8"))
        assert document["adapter"]["model_id"] == identity_model
        generations = document["generations"]
        # One record per prompt, in prompt order, aligned across
        # configs by the shared generation id scheme.
        assert [g["generation_id"] for g in generations] == ["gen_000", "gen_001"]
        assert [g["prompt"] for g in generations] == prompts
        assert all(g["finish_reason"] == "stop" for g in generations)


def test_run_comparison_uses_the_injected_clock(tmp_path):
    adapters = {
        "local": (
            ScriptedAdapter([TextDelta(text="a"), Completed(finish_reason="stop")]),
            AdapterIdentity(adapter="llamacpp", model_id="qwen3-0.6b"),
        ),
    }
    # One generation: start, then one tick per event.
    clock = scripted_clock(0.0, 0.25, 1.5)
    paths = asyncio.run(run_comparison(adapters, ["ping"], tmp_path, clock=clock))

    (generation,) = json.loads(paths["local"].read_text(encoding="utf-8"))["generations"]
    assert generation["first_transport_event_seconds"] == 0.25
    assert generation["first_answer_token_seconds"] == 0.25
    assert generation["completion_seconds"] == 1.5


def test_collector_delegates_the_rest_of_the_adapter_contract():
    adapter = ScriptedAdapter([])
    collector = TimingCollector(adapter, clock=scripted_clock(0.0, 1.0))
    assert asyncio.run(collector.capabilities()) == make_capabilities()
    assert asyncio.run(collector.health()).detail == "scripted"
    asyncio.run(collector.cancel("gen_9"))
    assert adapter.cancelled == ["gen_9"]


@pytest.mark.parametrize("finish_reason", ["tool_calls", "length", "end_of_stream"])
def test_completion_latency_only_counts_a_successful_final_answer(finish_reason):
    # Spec 12.1: completion latency is request to successful final
    # answer. A tool proposal, a truncation, or a finish-less end is
    # recorded as the finish reason but is not a completion.
    adapter = ScriptedAdapter([TextDelta(text="hi"), Completed(finish_reason=finish_reason)])
    collector, _ = collect_with_stats(adapter, scripted_clock(0.0, 0.5, 1.0))
    record = collector.records[0]
    assert record.finish_reason == finish_reason
    assert record.completion_seconds is None


def test_cached_tokens_are_recorded_per_call():
    # Spec 7.3: provider cache counts are retained per call.
    adapter = ScriptedAdapter(
        [Usage(input_tokens=1000, output_tokens=4, cached_tokens=900), Completed("stop")]
    )
    collector, _ = collect_with_stats(adapter, scripted_clock(0.0, 1.0, 2.0))
    assert collector.records[0].cached_tokens == 900


def test_each_generation_records_a_utc_start_timestamp():
    # RM25: event timestamps are UTC.
    adapter = ScriptedAdapter([Completed(finish_reason="stop")])
    collector, _ = collect_with_stats(adapter, scripted_clock(0.0, 1.0))
    started_at = datetime.fromisoformat(collector.records[0].started_at_utc)
    assert started_at.utcoffset() == timedelta(0)


class RaisingAdapter(ScriptedAdapter):
    async def generate(self, request):
        yield TextDelta(text="partial")
        raise RuntimeError("boom")


def test_an_adapter_exception_is_recorded_as_a_failure():
    collector = TimingCollector(RaisingAdapter([]), clock=scripted_clock(0.0, 1.0, 2.0))
    with pytest.raises(RuntimeError):
        collect_events(collector, make_request())
    record = collector.records[0]
    assert record.failure_code == "adapter_error"
    assert record.completion_seconds is None


def test_an_abandoned_generation_is_recorded_as_such():
    adapter = ScriptedAdapter([TextDelta(text="a"), TextDelta(text="b"), Completed("stop")])
    collector = TimingCollector(adapter, clock=scripted_clock(0.0, 1.0, 2.0))

    async def take_one():
        stream = collector.generate(make_request())
        await anext(stream)
        await stream.aclose()

    asyncio.run(take_one())
    assert collector.records[0].failure_code == "abandoned"


def test_run_comparison_keeps_going_after_an_adapter_exception(tmp_path):
    # One broken generation must not lose the other configs' exports.
    adapters = {
        "broken": (RaisingAdapter([]), AdapterIdentity(adapter="x", model_id="x")),
        "local": (
            ScriptedAdapter([TextDelta(text="a"), Completed(finish_reason="stop")]),
            AdapterIdentity(adapter="llamacpp", model_id="qwen3-0.6b"),
        ),
    }
    paths = asyncio.run(run_comparison(adapters, ["p1", "p2"], tmp_path))
    broken = json.loads(paths["broken"].read_text(encoding="utf-8"))["generations"]
    assert [g["failure_code"] for g in broken] == ["adapter_error", "adapter_error"]
    local = json.loads(paths["local"].read_text(encoding="utf-8"))["generations"]
    assert [g["finish_reason"] for g in local] == ["stop", "stop"]


@pytest.mark.parametrize(
    "names",
    [["../results", "local"], ["a/b", "local"], ["", "local"], ["Local", "local"]],
)
def test_run_comparison_rejects_unsafe_or_colliding_config_names(tmp_path, names):
    adapters = {
        name: (ScriptedAdapter([]), AdapterIdentity(adapter="x", model_id="x")) for name in names
    }
    with pytest.raises(ValueError):
        asyncio.run(run_comparison(adapters, ["p"], tmp_path / "out"))
    assert not (tmp_path / "out").exists()
