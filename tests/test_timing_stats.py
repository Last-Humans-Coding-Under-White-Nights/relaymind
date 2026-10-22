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

from helpers import HEALTH_TOOL, ScriptedAdapter, collect_events, make_capabilities, make_request
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


def test_a_cancel_after_its_generation_ended_is_not_kept():
    """RM25, spec 0.19 section 7.4: a late cancel leaves nothing behind.

    The orchestrator may ask to cancel a generation that already ended
    (best effort). That request must not mark a later generation with
    the same ID as cancelled, and must not be held forever.
    """
    adapter = ScriptedAdapter([TextDelta(text="hi")])  # no terminal event
    collector = TimingCollector(adapter, clock=scripted_clock(0.0, 0.5, 1.0, 1.5))
    collect_events(collector, make_request())
    asyncio.run(collector.cancel("gen_1"))
    collect_events(collector, make_request())

    assert [r.failure_code for r in collector.records] == [
        "no_terminal_event",
        "no_terminal_event",
    ]
    assert adapter.cancelled == ["gen_1"]


def test_collector_delegates_count_tokens_to_the_wrapped_adapter():
    # RM16, spec section 6: the collector reports the wrapped adapter's
    # tokenizer_available, so it must also hand count_tokens through
    # instead of refusing with the base-class default.
    class Counting(ScriptedAdapter):
        def __init__(self):
            super().__init__([])
            self.counted = []

        async def count_tokens(self, messages, tool_schemas=()):
            self.counted.append((tuple(messages), tuple(tool_schemas)))
            return 42

    adapter = Counting()
    collector = TimingCollector(adapter, clock=scripted_clock(0.0))
    messages = (Message(role=Role.USER, content="is staging-api healthy?"),)
    assert asyncio.run(collector.count_tokens(messages, (HEALTH_TOOL,))) == 42
    assert adapter.counted == [(messages, (HEALTH_TOOL,))]


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


def test_a_provider_sent_failure_is_a_transport_event():
    # Spec 12.1: the first transport event is the first received byte
    # or status event. An HTTP error status or an error chunk came over
    # the wire, so it counts; only a synthesized failure does not.
    adapter = ScriptedAdapter(
        [GenerationFailed(code="provider_error", message="returned 500", received=True)]
    )
    collector, _ = collect_with_stats(adapter, scripted_clock(0.0, 0.3))
    record = collector.records[0]
    assert record.first_transport_event_seconds == 0.3
    assert record.failure_code == "provider_error"


class FinalizingAdapter(ScriptedAdapter):
    """Records when its stream's cleanup runs."""

    def __init__(self, events):
        super().__init__(events)
        self.stream_closed = False

    async def generate(self, request):
        try:
            for event in self._events:
                yield event
        finally:
            self.stream_closed = True


def test_abandoning_the_collector_stream_closes_the_inner_stream_at_once():
    # The inner stream holds the provider connection; it must be closed
    # when the consumer walks away, not whenever the GC gets to it.
    adapter = FinalizingAdapter([TextDelta(text="a"), TextDelta(text="b"), Completed("stop")])
    collector = TimingCollector(adapter, clock=scripted_clock(0.0, 1.0))

    async def take_one():
        stream = collector.generate(make_request())
        await anext(stream)
        await stream.aclose()
        return adapter.stream_closed

    assert asyncio.run(take_one()) is True


def test_a_stream_that_ends_without_a_terminal_event_is_recorded():
    # Spec 12.2: errors are recorded, never dropped. A stream that just
    # stops has no finish reason, so the record must say so.
    adapter = ScriptedAdapter([TextDelta(text="partial")])
    collector, _ = collect_with_stats(adapter, scripted_clock(0.0, 1.0))
    record = collector.records[0]
    assert record.finish_reason is None
    assert record.failure_code == "no_terminal_event"
    assert record.completion_seconds is None


class CancellableAdapter(ScriptedAdapter):
    """Ends its stream without Completed once cancelled, like the HTTP pump."""

    async def generate(self, request):
        yield TextDelta(text="partial")
        if request.generation_id in self.cancelled:
            return
        yield Completed(finish_reason="stop")


def test_a_cancelled_generation_is_recorded_as_cancelled():
    adapter = CancellableAdapter([])
    collector = TimingCollector(adapter, clock=scripted_clock(0.0, 1.0, 2.0))

    async def cancel_after_first():
        request = make_request()
        async for _event in collector.generate(request):
            await collector.cancel(request.generation_id)

    asyncio.run(cancel_after_first())
    assert collector.records[0].failure_code == "cancelled"
    assert collector.records[0].finish_reason is None


class HangingAdapter(ScriptedAdapter):
    def __init__(self):
        super().__init__([])
        self.started = asyncio.Event()

    async def generate(self, request):
        self.started.set()
        await asyncio.Event().wait()
        yield Completed(finish_reason="stop")  # pragma: no cover - never reached


def test_an_interrupted_generation_is_recorded_as_cancelled():
    # asyncio.CancelledError (Ctrl-C under asyncio.run) is not an
    # Exception; the interruption must still reach the record.
    async def interrupt():
        adapter = HangingAdapter()
        collector = TimingCollector(adapter, clock=scripted_clock(0.0))
        task = asyncio.create_task(collect(collector))
        await adapter.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return collector.records[0]

    async def collect(collector):
        async for _event in collector.generate(make_request()):
            pass

    assert asyncio.run(interrupt()).failure_code == "cancelled"


class FailThenRaiseAdapter(ScriptedAdapter):
    async def generate(self, request):
        yield GenerationFailed(code="invalid_tool_arguments", message="bad", received=True)
        raise RuntimeError("cleanup failed")


def test_an_exception_after_a_failure_keeps_the_provider_failure_code():
    collector = TimingCollector(FailThenRaiseAdapter([]), clock=scripted_clock(0.0, 1.0))
    with pytest.raises(RuntimeError):
        collect_events(collector, make_request())
    assert collector.records[0].failure_code == "invalid_tool_arguments"


def test_a_collector_bookkeeping_error_is_not_blamed_on_the_adapter():
    def broken_clock():
        if broken_clock.calls:
            raise ArithmeticError("clock broke")
        broken_clock.calls += 1
        return 0.0

    broken_clock.calls = 0
    adapter = ScriptedAdapter([TextDelta(text="hi"), Completed("stop")])
    collector = TimingCollector(adapter, clock=broken_clock)
    with pytest.raises(ArithmeticError):
        collect_events(collector, make_request())
    assert collector.records[0].failure_code != "adapter_error"


def test_drain_hands_over_the_records_and_releases_them():
    # A long-lived collector must not hold every record forever.
    adapter = ScriptedAdapter([Completed(finish_reason="stop")])
    collector = TimingCollector(adapter, clock=scripted_clock(0.0, 1.0, 2.0, 3.0))
    collect_events(collector, make_request(generation_id="gen_a"))
    collect_events(collector, make_request(generation_id="gen_b"))

    drained = collector.drain()

    assert [record.generation_id for record in drained] == ["gen_a", "gen_b"]
    assert collector.records == []


class HangOnSecondPromptAdapter(ScriptedAdapter):
    def __init__(self):
        super().__init__([])
        self.calls = 0
        self.hanging = asyncio.Event()

    async def generate(self, request):
        self.calls += 1
        if self.calls == 1:
            yield Completed(finish_reason="stop")
            return
        self.hanging.set()
        await asyncio.Event().wait()
        yield Completed(finish_reason="stop")  # pragma: no cover - never reached


def test_an_interrupted_comparison_still_exports_what_it_measured(tmp_path):
    # An interrupt midway through a long prompt list must not lose the
    # records already gathered for that config (spec 12.2: record
    # errors, never drop them).
    async def interrupt():
        adapter = HangOnSecondPromptAdapter()
        adapters = {"local": (adapter, AdapterIdentity(adapter="llamacpp", model_id="m"))}
        task = asyncio.create_task(run_comparison(adapters, ["p1", "p2", "p3"], tmp_path))
        await adapter.hanging.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(interrupt())
    generations = json.loads((tmp_path / "local.json").read_text(encoding="utf-8"))["generations"]
    assert [g["prompt"] for g in generations] == ["p1", "p2"]
    assert generations[0]["finish_reason"] == "stop"
    assert generations[1]["failure_code"] == "cancelled"


def test_export_records_run_info_under_its_own_key(tmp_path):
    # A probe export must carry what produced it (the command, cold
    # start, prompts), so an EVIDENCE.md entry can point at one file
    # instead of a description of a script (RM16, section 12).
    out = tmp_path / "probe.json"
    run_info = {"command": "python scripts/probe_timing.py probe.json", "cold_start_seconds": 1.2}
    export_stats(
        out,
        identity=AdapterIdentity(adapter="llamacpp", model_id="qwen3-0.6b"),
        records=[],
        run_info=run_info,
    )
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["run"] == run_info
    assert document["status"] == "engineering_instrumentation"


def test_export_without_run_info_has_no_run_key(tmp_path):
    out = tmp_path / "plain.json"
    export_stats(
        out, identity=AdapterIdentity(adapter="llamacpp", model_id="qwen3-0.6b"), records=[]
    )
    assert "run" not in json.loads(out.read_text(encoding="utf-8"))
