"""The startup warm-up of the local model (#124; RM18, RM24, RM25, RM27; spec 0.31 section 6).

Section 6: when a local model is configured and placement.warmup is
true, the gateway sends one routing-shaped and one skill-shaped
generation to the local model after restart recovery and before the
server socket exists. They are not runs. A runtime that is not ready in
time is not a startup error. Section 14.1: the [placement] table; until
the placement policy exists only warmup and warmup_seconds are known.
Deterministic: scripted doubles and a fake clock, no live model.
"""

import asyncio
import dataclasses
import json
import sqlite3

import pytest

from helpers import REPO_ROOT, FakeClock, ScriptedAdapter
from launcher_helpers import write_config
from relaymind import launcher
from relaymind.adapters import (
    AdapterHealth,
    Completed,
    FirstToken,
    GenerationFailed,
    GenerationRequest,
    Message,
    Role,
    TextDelta,
    Usage,
)
from relaymind.adapters.llamacpp import LlamaCppAdapter
from relaymind.launcher import LaunchError, build_app, load_config, main
from relaymind.orchestrator.local_loop import LOCAL_OPTIONS, LocalOrchestrator, skill_stage_messages
from relaymind.orchestrator.local_status import LocalModelClass, LocalStatus
from relaymind.orchestrator.warmup import (
    WARMUP_TEXT,
    WarmupResult,
    WarmupSample,
    detect_local_model,
    warm_up,
)
from relaymind.store import RunStore


def scripted(*turns):
    return ScriptedAdapter(turns=list(turns))


def answer(cached=None, *, first=True):
    """One warm-up generation: a first token, some text, usage, and the end."""
    events = [FirstToken()] if first else []
    return [
        *events,
        TextDelta(text="ok"),
        Usage(input_tokens=700, output_tokens=2, cached_tokens=cached),
        Completed(finish_reason="stop"),
    ]


def launch(tmp_path, extra="", adapter=None):
    config = load_config(write_config(tmp_path, extra))
    return config, build_app(config, local_adapter=adapter or scripted(), external_adapter=None)


def run_main(tmp_path, adapter, extra="", **kwargs):
    """main() with doubles; returns (exit status, stdout, stderr, what serve saw)."""
    out, err, seen = [], [], {}

    def serve(app, *, host, port):
        seen["requests"] = len(adapter.requests)
        store = RunStore(tmp_path / "runs.sqlite3")
        try:
            seen["active"] = store.active_run_id()
        finally:
            store.close()

    status = main(
        ["serve", "--config", str(write_config(tmp_path, extra))],
        serve_fn=serve,
        stdout=out.append,
        stderr=err.append,
        local_adapter=adapter,
        **kwargs,
    )
    return status, out, err, seen


def run_count(tmp_path) -> int:
    with sqlite3.connect(tmp_path / "runs.sqlite3") as db:
        return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


# -- the [placement] table ---------------------------------------------------------------


def test_the_defaults_warm_up_for_thirty_seconds(tmp_path):
    """Section 14.1: warmup defaults to true and warmup_seconds to 30."""
    config = load_config(write_config(tmp_path))
    assert config.warmup is True and config.warmup_seconds == 30.0


def test_the_declared_values_are_read(tmp_path):
    config = load_config(
        write_config(tmp_path, "\n[placement]\nwarmup = false\nwarmup_seconds = 5\n")
    )
    assert config.warmup is False and config.warmup_seconds == 5.0


@pytest.mark.parametrize(
    "body",
    [
        "warmup = 'yes'",
        "warmup = 1",
        "warmup_seconds = 'ten'",
        "warmup_seconds = true",
        "warmup_seconds = 0",
        "warmup_seconds = -1",
        "warmup_seconds = nan",
        "warmup_seconds = inf",
    ],
)
def test_a_bad_value_stops_startup(tmp_path, body):
    """Section 14.1: a wrong type or a non-positive duration is a startup error."""
    with pytest.raises(LaunchError, match=r"\[placement\]"):
        load_config(write_config(tmp_path, f"\n[placement]\n{body}\n"))


@pytest.mark.parametrize("key", ["policy", "margin", "probe_after_seconds", "other"])
def test_the_placement_policy_keys_stay_unknown_until_they_exist(tmp_path, key):
    """Section 14.1: only warmup, warmup_seconds, and detect are implemented; the rest is
    refused."""
    with pytest.raises(LaunchError, match=rf"unknown key in \[placement\]: {key}"):
        load_config(write_config(tmp_path, f"\n[placement]\n{key} = 1\n"))


def test_placement_must_be_a_table(tmp_path):
    path = write_config(tmp_path)
    path.write_text("placement = 5\n" + path.read_text())
    with pytest.raises(LaunchError, match="placement must be a table"):
        load_config(path)


def test_the_example_file_documents_the_placement_table(tmp_path):
    """Section 14.1: the commented [placement] block loads when uncommented, with the defaults."""
    lines = (REPO_ROOT / "relaymind.example.toml").read_text(encoding="utf-8").splitlines()
    start = lines.index("# [placement]")
    block = []
    for line in lines[start:]:
        if not line.startswith("# "):
            break
        block.append(line[2:])
    assert len(block) == 4
    config = load_config(write_config(tmp_path, "\n" + "\n".join(block) + "\n"))
    assert (config.warmup, config.warmup_seconds, config.detect) == (True, 30.0, True)


# -- build_app sends nothing; the warm-up is its own step ----------------------------------


def test_build_app_sends_no_generation(tmp_path):
    """The warm-up is a step main() runs, so building the app keeps every scripted turn."""
    adapter = scripted(answer(), answer())
    _, built = launch(tmp_path, adapter=adapter)
    try:
        assert adapter.requests == []
        assert built.warmup is None
    finally:
        built.orchestrator.store.close()


def test_the_launched_gateway_offers_a_fresh_adapter_for_the_warm_up(tmp_path):
    """One httpx client must not serve two event loops: the warm-up gets its own adapter."""
    config = load_config(write_config(tmp_path))
    built = build_app(config)
    try:
        first, second = built.new_local_adapter(), built.new_local_adapter()
        assert isinstance(first, LlamaCppAdapter) and first is not second
        assert first is not built.orchestrator.model.adapter
        assert first._config == built.orchestrator.model.adapter._config
    finally:
        built.orchestrator.store.close()


# -- the warm-up requests ---------------------------------------------------------------


def test_the_warm_up_sends_a_routing_shaped_and_a_skill_shaped_request(tmp_path):
    """Section 6, RM18: the requests are built by the gateway's own builders."""
    adapter = scripted(answer(), answer())
    _, built = launch(tmp_path, adapter=adapter)
    try:
        result = asyncio.run(warm_up(built.orchestrator, adapter, seconds=30))
        routing, skill = adapter.requests
        message = Message(role=Role.USER, content=WARMUP_TEXT)
        expected = built.orchestrator.router.build_request([message], routing.generation_id)
        assert routing == expected
        # The skill stage: the same builder as a run, over a skill whose tools are permitted.
        names = [t.name for t in skill.tool_schemas]
        assert names and skill.options.temperature == LOCAL_OPTIONS.temperature
        chosen = next(
            s
            for s in map(built.orchestrator.skills.get, built.orchestrator.router.offered)
            if s.description in skill.messages[0].content
        )
        assert skill.messages == tuple(skill_stage_messages(chosen, [message], names))
        assert [s.purpose for s in result.samples] == ["routing", "skill"]
        assert result.ready is True and result.detail is None
    finally:
        built.orchestrator.store.close()


def test_the_warm_up_text_is_not_a_measured_prompt():
    """Section 10: a repeated text would be served whole from the cache and inflate the figure."""
    corpus = (REPO_ROOT / "evaluation" / "corpus.v1.json").read_text(encoding="utf-8")
    latency = (REPO_ROOT / "evaluation" / "latency_prompts.v1.json").read_text(encoding="utf-8")
    assert json.dumps(WARMUP_TEXT)[1:-1] not in corpus + latency


def test_timings_are_recorded_per_request(tmp_path):
    """RM25, section 6: duration, first token, and cached tokens, from the monotonic clock."""
    clock = FakeClock()

    def tick(_request):
        clock.advance(0.25)

    adapter = ScriptedAdapter(turns=[answer(cached=0), answer(cached=512)], on_generate=tick)
    _, built = launch(tmp_path, adapter=adapter)
    try:
        # The orchestrator's clock is the store's; the warm-up takes it from the orchestrator.
        built.orchestrator.clock = clock
        result = asyncio.run(warm_up(built.orchestrator, adapter, seconds=30))
    finally:
        built.orchestrator.store.close()

    routing, skill = result.samples
    # The double advances the clock 0.25 s as each generation starts.
    assert (routing.duration_ms, skill.duration_ms) == (250, 250)
    assert (routing.cached_tokens, skill.cached_tokens) == (0, 512)
    assert (routing.input_tokens, routing.output_tokens) == (700, 2)
    assert routing.first_token_ms == 250
    assert result.to_json_object()["samples"][1]["cached_tokens"] == 512


def test_the_summary_line_shows_a_missing_count_as_not_reported():
    """RM16: the printed line never turns an absent count into 0."""
    sample = WarmupSample("routing", 412, 380, 700, 2, None)
    skill = WarmupSample("skill", 388, None, 700, 2, 612)
    line = WarmupResult((sample, skill), True).summary()
    assert line == (
        "Local warm-up finished: routing 412 ms (first token 380 ms, cached n/a), "
        "skill 388 ms (first token n/a, cached 612)."
    )


def test_a_count_the_runtime_did_not_report_stays_none(tmp_path):
    """RM16, spec 0.33: an absent cached count is None, never 0; no first token is None."""
    adapter = scripted(
        [Usage(input_tokens=5, output_tokens=1), Completed(finish_reason="stop")],
        answer(first=False),
    )
    _, built = launch(tmp_path, adapter=adapter)
    try:
        result = asyncio.run(warm_up(built.orchestrator, adapter, seconds=30))
    finally:
        built.orchestrator.store.close()
    routing, skill = result.samples
    assert routing.cached_tokens is None and routing.first_token_ms is None
    # The text delta dates the first token when an adapter sends no marker.
    assert skill.first_token_ms is not None


def test_a_failed_generation_is_reported_not_raised(tmp_path):
    adapter = scripted([GenerationFailed(code="unreachable", message="connection refused")])
    _, built = launch(tmp_path, adapter=adapter)
    try:
        result = asyncio.run(warm_up(built.orchestrator, adapter, seconds=30))
    finally:
        built.orchestrator.store.close()
    assert result.ready is False and "unreachable" in result.detail
    assert result.samples == ()


def test_a_runtime_that_does_not_answer_in_time_is_reported(tmp_path):
    """Section 6: warmup_seconds bounds the whole warm-up; the generation is closed."""
    closed = []

    class Silent(ScriptedAdapter):
        async def generate(self, request: GenerationRequest):
            self.requests.append(request)
            try:
                await asyncio.Event().wait()
                yield FirstToken()  # never reached
            finally:
                closed.append(request.generation_id)

    adapter = Silent(turns=[])
    _, built = launch(tmp_path, adapter=adapter)
    try:
        result = asyncio.run(warm_up(built.orchestrator, adapter, seconds=0.05))
    finally:
        built.orchestrator.store.close()
    assert result.ready is False and "within" in result.detail
    assert len(adapter.requests) == 1  # the skill-shaped request was never sent
    assert closed == [adapter.requests[0].generation_id]


# -- main() runs it after recovery, before serving ----------------------------------------


def test_main_warms_up_after_recovery_and_before_serving_without_a_run(tmp_path, monkeypatch):
    """Section 14.1 startup order: recovery, the warm-up, then the socket; no run is created."""
    order = []
    original = LocalOrchestrator.recover_after_restart

    def spy(self):
        order.append("recovery")
        return original(self)

    monkeypatch.setattr(LocalOrchestrator, "recover_after_restart", spy)
    adapter = ScriptedAdapter(
        turns=[answer(cached=0), answer(cached=0)], on_generate=lambda r: order.append("generate")
    )
    status, out, err, seen = run_main(tmp_path, adapter)

    assert status == 0 and err == []
    assert order == ["recovery", "generate", "generate"]
    (line,) = [line for line in out if line.startswith("Local warm-up finished")]
    assert out.index(line) < out.index(next(x for x in out if x.startswith("Serving on")))
    assert seen["requests"] == 2 and seen["active"] is None
    assert run_count(tmp_path) == 0
    # The address and the token are printed after the warm-up, not before it.
    assert any(line.startswith("Application token: ") for line in out)


def test_main_with_warmup_false_sends_nothing(tmp_path):
    adapter = scripted()
    status, _, err, seen = run_main(tmp_path, adapter, "\n[placement]\nwarmup = false\n")
    assert status == 0 and err == [] and adapter.requests == [] and seen["requests"] == 0


def test_a_runtime_that_fails_the_warm_up_is_one_printed_line_and_not_a_startup_error(tmp_path):
    adapter = scripted([GenerationFailed(code="unreachable", message="refused")])
    status, out, err, seen = run_main(tmp_path, adapter)

    assert status == 0 and "requests" in seen  # the gateway still serves
    (line,) = err
    assert line.startswith("relaymind: local warm-up did not finish: ") and "\n" not in line
    assert "unreachable" in line
    assert any(line.startswith("Application token: ") for line in out)


def test_any_adapter_failure_is_that_one_line(tmp_path):
    """The scripted double raises when it has no turn left; that is an adapter failure too."""
    status, _, err, _ = run_main(tmp_path, scripted())
    assert status == 0 and len(err) == 1 and err[0].startswith("relaymind: local warm-up")


def test_a_slow_runtime_is_one_printed_line(tmp_path):
    class Silent(ScriptedAdapter):
        async def generate(self, request):
            await asyncio.Event().wait()
            yield FirstToken()

    status, _, err, _ = run_main(
        tmp_path, Silent(turns=[]), "\n[placement]\nwarmup_seconds = 0.05\n"
    )
    assert status == 0
    (line,) = err
    assert "within 0.05 s" in line


def test_the_launcher_keeps_the_timings_in_launched_warmup(tmp_path):
    """#124: Launched.warmup holds the samples; #126 and #129 read them."""
    adapter = scripted(answer(cached=0), answer(cached=7))
    config, built = launch(tmp_path, adapter=adapter)
    try:
        warmed = launcher.warm_up_gateway(config, built)
        assert isinstance(warmed.warmup, WarmupResult) and warmed.warmup.ready
        assert [s.cached_tokens for s in warmed.warmup.samples] == [0, 7]
        assert warmed.app is built.app and warmed.orchestrator is built.orchestrator
    finally:
        built.orchestrator.store.close()


def test_a_real_adapter_for_the_warm_up_is_closed_and_not_the_serving_one(tmp_path, monkeypatch):
    """A short-lived adapter: it is built for the warm-up and closed in the same event loop."""
    closed = []
    made = []

    class Recording(ScriptedAdapter):
        async def aclose(self):
            closed.append(self)

    def make():
        adapter = Recording(turns=[answer(), answer()])
        made.append(adapter)
        return adapter

    config = load_config(write_config(tmp_path))
    built = build_app(config, local_adapter=scripted())
    try:
        built = dataclasses.replace(built, new_local_adapter=make)
        warmed = launcher.warm_up_gateway(config, built)
        assert warmed.warmup.ready
        assert closed == made and len(made) == 1
    finally:
        built.orchestrator.store.close()


# -- detection of the local model (spec 0.31 RM26, section 14.1; #126) -------------------------


class FakeSleep:
    """An injected sleep that advances the fake clock instead of waiting."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


def health_sequence(adapter, *states):
    """Make adapter.health return the given (available, warm) pairs, the last one repeated."""
    replies = list(states)

    async def health():
        available, warm = replies.pop(0) if len(replies) > 1 else replies[0]
        return AdapterHealth(available=available, warm=warm, detail="probe")

    adapter.health = health


def slow(clock: FakeClock, seconds: float):
    """An on_generate hook: each generation takes seconds on the fake clock."""

    def tick(_request):
        clock.advance(seconds)

    return tick


def detect_with(tmp_path, adapter, *, warmup=True, seconds=30.0, clock=None, extra=""):
    """Run detect_local_model over a launched gateway with a fake clock and sleep."""
    clock = clock or FakeClock()
    _, built = launch(tmp_path, extra, adapter=adapter)
    try:
        built.orchestrator.clock = clock
        sleep = FakeSleep(clock)
        status, result = asyncio.run(
            detect_local_model(
                built.orchestrator, adapter, warmup=warmup, seconds=seconds, sleep=sleep
            )
        )
    finally:
        built.orchestrator.store.close()
    return status, result, sleep


def test_detect_is_a_placement_key_that_defaults_to_true(tmp_path):
    """Section 14.1: placement.detect, default true."""
    assert load_config(write_config(tmp_path)).detect is True
    config = load_config(write_config(tmp_path, "\n[placement]\ndetect = false\n"))
    assert config.detect is False


@pytest.mark.parametrize("value", ["1", "'yes'"])
def test_a_detect_value_that_is_not_a_boolean_stops_startup(tmp_path, value):
    with pytest.raises(LaunchError, match=r"\[placement\] detect"):
        load_config(write_config(tmp_path, f"\n[placement]\ndetect = {value}\n"))


def test_a_model_that_answers_and_fits_the_stage_budget_is_ready(tmp_path):
    """RM26: available, and the routing-shaped warm-up within active_seconds."""
    clock = FakeClock()
    adapter = ScriptedAdapter(turns=[answer(), answer()], on_generate=slow(clock, 1.0))
    status, result, _ = detect_with(tmp_path, adapter, clock=clock)

    assert status == LocalStatus.ready()
    assert result.ready and [s.purpose for s in result.samples] == ["routing", "skill"]


def test_a_routing_warm_up_over_the_stage_budget_is_unsuitable(tmp_path):
    """RM26: the routing-shaped warm-up took longer than active_seconds (4 s by default)."""
    clock = FakeClock()
    adapter = ScriptedAdapter(turns=[answer(), answer()], on_generate=slow(clock, 4.5))
    status, result, _ = detect_with(tmp_path, adapter, clock=clock)

    assert status.status == LocalModelClass.UNSUITABLE
    assert status.reason == (
        "the routing warm-up took 4500 ms; the local stage limit (active_seconds) is 4 s"
    )
    # The timings are kept, as for any warm-up (#124).
    assert result.samples[0].duration_ms == 4500


def test_a_warm_up_exactly_at_the_budget_is_ready(tmp_path):
    """RM26: within the budget means not longer than it."""
    clock = FakeClock()
    adapter = ScriptedAdapter(turns=[answer(), answer()], on_generate=slow(clock, 4.0))
    status, _, _ = detect_with(tmp_path, adapter, clock=clock)
    assert status == LocalStatus.ready()


def test_the_stage_budget_is_the_configured_one(tmp_path):
    """RM03: a raised active_seconds is the limit the warm-up is held to."""
    clock = FakeClock()
    adapter = ScriptedAdapter(turns=[answer(), answer()], on_generate=slow(clock, 4.5))
    status, _, _ = detect_with(
        tmp_path, adapter, clock=clock, extra="\n[budgets]\nactive_seconds = 8\n"
    )
    assert status == LocalStatus.ready()


def test_a_runtime_that_does_not_answer_is_unavailable_after_warmup_seconds(tmp_path):
    """RM26: the availability check polls until placement.warmup_seconds, then gives up."""
    adapter = scripted()
    health_sequence(adapter, (False, False))
    status, result, sleep = detect_with(tmp_path, adapter, seconds=3)

    assert status.status == LocalModelClass.UNAVAILABLE
    assert status.reason == "the runtime did not answer with the model loaded within 3 s (probe)"
    assert result is None and adapter.requests == []
    assert sum(sleep.calls) == pytest.approx(3)


def test_a_model_that_finishes_loading_in_time_is_checked_further(tmp_path):
    """RM26: warm false while loading; the check waits for the model, not only the runtime."""
    clock = FakeClock()
    adapter = ScriptedAdapter(turns=[answer(), answer()], on_generate=slow(clock, 0.5))
    health_sequence(adapter, (True, False), (True, False), (True, True))
    status, _, sleep = detect_with(tmp_path, adapter, clock=clock)

    assert status == LocalStatus.ready()
    assert len(sleep.calls) == 2
    assert len(adapter.requests) == 2


def test_a_health_check_that_raises_is_unavailable_without_its_text(tmp_path):
    """RM12, RM26: the exception text may carry a URL or a key; its type is the cause."""
    adapter = scripted()

    async def broken():
        raise ConnectionError("http://user:secret@127.0.0.1:8033 refused")

    adapter.health = broken
    status, _, _ = detect_with(tmp_path, adapter, seconds=1)

    assert status.status == LocalModelClass.UNAVAILABLE
    assert "ConnectionError" in status.reason and "secret" not in status.reason


def test_with_warmup_false_only_availability_is_checked(tmp_path):
    """Section 14.1: the unsuitable check needs the warm-up."""
    adapter = scripted()
    status, result, _ = detect_with(tmp_path, adapter, warmup=False)
    assert status == LocalStatus.ready() and result is None and adapter.requests == []


def test_a_failed_routing_warm_up_is_unavailable(tmp_path):
    """RM26: the runtime answers, but the model cannot generate on this machine."""
    adapter = scripted([GenerationFailed(code="unreachable", message="refused")])
    status, result, _ = detect_with(tmp_path, adapter)

    assert status.status == LocalModelClass.UNAVAILABLE
    assert "routing warm-up did not finish" in status.reason and "unreachable" in status.reason
    assert result is not None and not result.ready


def silent_adapter():
    class Silent(ScriptedAdapter):
        async def generate(self, request):
            self.requests.append(request)
            await asyncio.Event().wait()
            yield FirstToken()

    return Silent(turns=[])


def test_a_warm_up_cut_off_before_the_stage_budget_is_unavailable(tmp_path):
    """Spec 0.37: shorter than the stage budget, a cut-off warm-up says nothing about speed."""
    status, _, _ = detect_with(tmp_path, silent_adapter(), seconds=0.05)
    assert status.status == LocalModelClass.UNAVAILABLE
    assert "within 0.05 s" in status.reason


def test_a_warm_up_cut_off_at_or_after_the_stage_budget_is_unsuitable(tmp_path):
    """Spec 0.37: it did not finish within the stage budget, so it does not fit it."""
    status, _, _ = detect_with(
        tmp_path, silent_adapter(), seconds=0.05, extra="\n[budgets]\nactive_seconds = 0.05\n"
    )
    assert status.status == LocalModelClass.UNSUITABLE
    assert "did not finish within 0.05 s" in status.reason


# -- main prints the class (RM26) --------------------------------------------------------------


def test_main_prints_unavailable_and_serves(tmp_path):
    """#126: a local double that does not answer is printed as unavailable; startup goes on."""
    adapter = scripted()
    health_sequence(adapter, (False, False))
    status, out, err, seen = run_main(tmp_path, adapter, "\n[placement]\nwarmup_seconds = 0.05\n")

    assert status == 0 and err == [] and "requests" in seen
    (line,) = [line for line in out if line.startswith("Local model:")]
    assert line.startswith("Local model: unavailable (")
    assert line.endswith("auto runs as managed external execution.")
    assert adapter.requests == []


def test_main_prints_unsuitable_with_a_fake_clock(tmp_path, monkeypatch):
    """#126: a warm-up over active_seconds is printed as unsuitable; the timings line stays."""
    clock = FakeClock()
    monkeypatch.setattr(launcher, "RunStore", lambda path: RunStore(path, clock=clock))
    adapter = ScriptedAdapter(turns=[answer(), answer()], on_generate=slow(clock, 6.0))
    status, out, err, _ = run_main(tmp_path, adapter)

    assert status == 0 and err == []
    assert any(line.startswith("Local warm-up finished: routing 6000 ms") for line in out)
    (line,) = [line for line in out if line.startswith("Local model:")]
    assert line.startswith("Local model: unsuitable (the routing warm-up took 6000 ms;")


def test_main_prints_no_class_line_for_a_ready_model(tmp_path):
    adapter = scripted(answer(), answer())
    status, out, err, _ = run_main(tmp_path, adapter)
    assert status == 0 and err == []
    assert not any(line.startswith("Local model:") for line in out)


def test_detect_false_keeps_the_earlier_behavior(tmp_path):
    """Section 14.1: with detect false nothing is classified, as before spec 0.31."""
    adapter = scripted(answer(), answer())
    health_sequence(adapter, (False, False))
    status, out, err, _ = run_main(tmp_path, adapter, "\n[placement]\ndetect = false\n")

    assert status == 0 and err == []
    assert not any(line.startswith("Local model:") for line in out)
    # The warm-up still runs, without the availability check.
    assert len(adapter.requests) == 2


def test_the_launcher_sets_the_detected_class_on_the_orchestrator(tmp_path):
    adapter = scripted()
    health_sequence(adapter, (False, False))
    config, built = launch(tmp_path, "\n[placement]\nwarmup_seconds = 0.05\n", adapter=adapter)
    try:
        warmed = launcher.warm_up_gateway(config, built)
        assert warmed.orchestrator.local_status.status == LocalModelClass.UNAVAILABLE
        assert warmed.orchestrator.detect is True
    finally:
        built.orchestrator.store.close()
