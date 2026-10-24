"""RM03, RM25, RM26, RM29, RM31, RM32: labeled runner configurations (#128)."""

import argparse
import json
import tomllib
from dataclasses import replace

import pytest

from helpers import load_script_module
from launcher_helpers import make_artifact
from relaymind.launcher import load_config
from relaymind.selection.settings import SelectionSettings


@pytest.fixture(scope="module")
def settings():
    return load_script_module("evaluation_config")


def parse(settings, *values):
    parser = argparse.ArgumentParser()
    settings.add_configuration_arguments(parser)
    return settings.configuration_from_args(parser.parse_args(values))


def test_defaults_and_no_local_defaults_are_explicit(settings):
    baseline = parse(settings).to_json_object()
    assert baseline["performance"] == {
        "answer_pass": "final",
        "answer_stream": "buffered",
        "calls_per_generation": "one",
        "prefetch": False,
        "early_headers": False,
    }
    assert baseline["selection"]["chain"] == ["model_router"]
    assert baseline["local_model_configured"] is True
    absent = parse(settings, "--no-local-model").to_json_object()
    assert absent["selection"]["chain"] == ["example_match"]
    assert absent["local_model_configured"] is False


def test_repeated_selection_flags_preserve_chain_order(settings):
    """RM25, RM28: repeated selector flags must not silently discard a selector."""
    config = parse(
        settings, "--selection-chain", "example_match", "--selection-chain", "model_router"
    )
    assert config.selection.chain == ("example_match", "model_router")
    with pytest.raises(ValueError, match="more than once"):
        parse(settings, "--selection-chain", "example_match", "--selection-chain", "example_match")


def test_settings_trim_whitespace_consistently(settings):
    """RM03, RM25: whitespace around launch keys and values is insignificant."""
    config = parse(
        settings, "--performance", " prefetch = true ", "--budgets", " active_seconds = 10 "
    )
    assert config.performance.prefetch is True
    assert config.budgets.active_seconds == 10
    with pytest.raises(ValueError, match="duplicate"):
        parse(settings, "--performance", "prefetch=true", " prefetch = false ")


def test_export_and_gateway_keep_all_selection_settings(settings, tmp_path):
    """RM25, RM28: the export records the actual matcher limits and launch chain."""
    config = replace(
        parse(settings),
        selection=SelectionSettings(
            chain=("example_match", "model_router"),
            timeout_seconds=3.5,
            example_match_score=0.7,
            example_match_margin=0.2,
        ),
    )
    expected = {
        "chain": ["example_match", "model_router"],
        "timeout_seconds": 3.5,
        "example_match_score": 0.7,
        "example_match_margin": 0.2,
    }
    assert config.to_json_object()["selection"] == expected
    runner = load_script_module("run_corpus")
    manifest, artifact = make_artifact(tmp_path)
    path = tmp_path / "custom-selection.toml"
    path.write_text(
        runner.case_config(
            runner.load_frozen_corpus().cases[0],
            work=tmp_path,
            manifest=manifest,
            artifact=artifact,
            local_url="http://127.0.0.1:8081",
            port=8099,
            **config.gateway_options(),
        )
    )
    assert load_config(path).selection == config.selection


@pytest.mark.parametrize("runner_name", ["run_corpus", "latency_benchmark"])
def test_phase_two_launch_file_round_trip(settings, runner_name, tmp_path):
    runner = load_script_module(runner_name)
    manifest, artifact = make_artifact(tmp_path)
    config = parse(
        settings,
        "--performance",
        "answer_pass=draft",
        "prefetch=true",
        "calls_per_generation=many",
        "--budgets",
        "active_seconds=12.5",
        "--selection-chain",
        "example_match",
        "model_router",
    )
    options = dict(
        work=tmp_path,
        manifest=manifest,
        artifact=artifact,
        local_url="http://127.0.0.1:8081",
        port=8099,
        **config.gateway_options(),
    )
    if runner_name == "run_corpus":
        text = runner.case_config(runner.load_frozen_corpus().cases[0], **options)
    else:
        text = runner.benchmark_config(**options)
    path = tmp_path / "runner.toml"
    path.write_text(text)
    loaded = load_config(path)
    assert loaded.performance.answer_pass == "draft"
    assert loaded.performance.prefetch is True
    assert loaded.performance.calls_per_generation == "many"
    assert loaded.budgets.active_seconds == 12.5
    assert loaded.selection.chain == ("example_match", "model_router")
    assert loaded.routing_cache is False


@pytest.mark.parametrize(
    "arguments",
    [
        ["--performance", "prefetch=1"],
        ["--performance", "prefetch=TRUE"],
        ["--performance", "answer_stream=live"],
        ["--performance", "unknown=true"],
        ["--performance", "prefetch=true", "prefetch=false"],
        ["--performance", "prefetch"],
        ["--budgets", "active_seconds=nan"],
        ["--budgets", "tool_calls=1.5"],
        ["--budgets", "unknown=1"],
        ["--selection-chain", "unknown"],
        ["--no-local-model", "--selection-chain", "model_router"],
    ],
)
def test_invalid_options_fail_before_launch(settings, arguments):
    with pytest.raises(ValueError):
        parse(settings, *arguments)


def test_no_local_config_omits_model_and_forbidden_modes(settings, tmp_path):
    runner = load_script_module("run_corpus")
    config = parse(settings, "--no-local-model")
    endpoint = runner.harness.ExternalEndpoint("https://example.invalid/v1", "model", 8192)
    text = runner.case_config(
        runner.load_frozen_corpus().cases[0],
        work=tmp_path,
        manifest=None,
        artifact=None,
        local_url=None,
        port=8099,
        external=endpoint,
        **config.gateway_options(),
    )
    raw = tomllib.loads(text)
    assert "local_model" not in raw
    assert raw["policy"]["allowed_modes"] == ["auto", "external_only"]
    path = tmp_path / "absent.toml"
    path.write_text(text)
    assert load_config(path).manifest is None


def test_report_labels_configuration_without_inventing_legacy_defaults(settings):
    config = parse(settings, "--performance", "answer_pass=draft").to_json_object()
    report = settings.label_report("# Test\n\nResults\n", config)
    assert report.index("Configuration") < report.index("Results")
    assert json.loads(report.split("```json\n")[1].split("```")[0]) == config
    assert "not recorded" in settings.label_report("# Test\n", None)
    assert "\n\n\n" not in report


@pytest.mark.parametrize("missing", ["manifest", "artifact", "local_url"])
def test_local_launch_rejects_missing_prerequisites(missing, tmp_path):
    """RM18, RM25: incomplete local runtime inputs fail before producing invalid TOML."""
    harness = load_script_module("live_acceptance")
    options = dict(
        work=tmp_path,
        manifest=tmp_path / "model.json",
        artifact=tmp_path / "model.gguf",
        local_url="http://127.0.0.1:8081",
        port=8099,
    )
    options[missing] = None
    with pytest.raises(ValueError, match="local model requires.*manifest.*artifact.*local_url"):
        harness.gateway_config(**options)


@pytest.mark.parametrize("runner_name", ["run_corpus", "latency_benchmark"])
def test_actual_cli_accepts_configuration_options(runner_name, monkeypatch, tmp_path):
    """RM25: both command-line entry points accept the published option spellings."""
    runner = load_script_module(runner_name)
    captured = []

    def capture(args):
        captured.append(args)
        raise InterruptedError("captured before launch")

    monkeypatch.setattr(
        runner, "run_corpus" if runner_name == "run_corpus" else "run_benchmark", capture
    )
    with pytest.raises(InterruptedError):
        runner.main(
            [
                "--out",
                str(tmp_path / "out.json"),
                "--performance",
                "answer_pass=draft",
                "--budgets",
                "active_seconds=15",
                "--selection-chain",
                "example_match",
                "--no-local-model",
                "--external-base-url",
                "https://example.invalid/v1",
                "--external-model",
                "model",
                "--external-context-limit",
                "8192",
            ]
        )
    assert captured[0].no_local_model
    assert captured[0].performance == ["answer_pass=draft"]


def test_absent_local_modes_are_recorded_as_not_run(monkeypatch):
    runner = load_script_module("run_corpus")
    monkeypatch.setenv(runner.harness.EXTERNAL_KEY_ENV, "test-token")
    modes, not_run = runner.select_modes(
        ["auto", "external_only", "local_only"],
        external=runner.harness.ExternalEndpoint("https://example.invalid/v1", "model", 8192),
        local_model=False,
    )
    assert modes == ("auto", "external_only")
    assert "no local model" in not_run["local_only"]


@pytest.mark.parametrize(
    "runner_name,startup_failure",
    [("run_corpus", False), ("run_corpus", True), ("latency_benchmark", False)],
)
def test_absent_local_run_does_not_touch_runtime(
    settings, runner_name, startup_failure, monkeypatch, tmp_path
):
    """RM26: a complete runner export needs no local manifest, process, devices or probes."""
    from types import SimpleNamespace

    runner = load_script_module(runner_name)

    def forbidden(*args, **kwargs):
        pytest.fail("an absent local model must not access the local runtime")

    monkeypatch.setenv(runner.harness.EXTERNAL_KEY_ENV, "test-token")
    monkeypatch.setattr(runner.harness, "check_runtime", forbidden)
    monkeypatch.setattr(runner, "runtime_devices", forbidden)
    monkeypatch.setattr(runner.harness, "application_revision", lambda: {})
    monkeypatch.setattr(runner, "machine_manifest", lambda: {})
    args = argparse.Namespace(
        no_local_model=True,
        performance=["answer_pass=draft"],
        budgets=[],
        selection_chain=None,
        external_base_url="https://example.invalid/v1",
        external_model="model",
        external_context_limit=8192,
        external_region="test-region",
        cases=["simple-en-01"],
        languages=None,
        modes=["auto", "external_only", "local_only"],
        seed=1,
        runs=1,
        timeout=1,
        out=str(tmp_path / "out.json"),
    )
    if runner_name == "run_corpus":
        args.cases = [runner.load_frozen_corpus().cases[0].case_id]

        def run_one(case, mode, **options):
            assert options["runtime"] is None
            assert options["configuration"].local_model_configured is False
            if startup_failure:
                raise RuntimeError("gateway startup failed")
            record = dict(
                run_id=None, state="error", error_code=None, reason_code=None, timed_out=False
            )
            return (
                runner.grade_record(
                    case,
                    mode,
                    record,
                    [],
                    None,
                    local_model_configured=options["configuration"].local_model_configured,
                ),
                record,
                [],
            )

        monkeypatch.setattr(runner, "_run_one", run_one)
        document = runner.run_corpus(args)
        assert document["selection"]["modes"] == ["auto", "external_only"]
        assert document["llama_server"] is None
        assert document["grades"][0]["local_completion"] is None
        if startup_failure:
            assert document["records"][f"{args.cases[0]}/auto"]["runner_error"] == (
                "RuntimeError: gateway startup failed"
            )
        assert document["summary"]["modes"]["auto"]["en"]["all"]["local_completion"]["rate"] is None
    else:
        monkeypatch.setattr(runner, "_props", forbidden)

        class Gateway:
            def __init__(self, *args):
                pass

            def start(self):
                return SimpleNamespace(url="http://127.0.0.1:1", token="token", recorded_lines=[])

            def stop(self):
                pass

        monkeypatch.setattr(runner.harness, "GatewayProcess", Gateway)

        async def samples(*args, **kwargs):
            assert kwargs["external"] is True
            return {"cold": {}, "warmups": [], "measured": []}

        monkeypatch.setattr(runner, "run_samples", samples)
        document = runner.run_benchmark(args)
        assert document["cold_start"]["llama_server_start_to_healthy_seconds"] is None
        assert document["environment"]["runtime_devices"] is None
        assert document["design"]["startup_warmup"] is False
        assert document["prefix_cache"] is None
    assert document["configuration"]["local_model_configured"] is False
    environment = document if runner_name == "run_corpus" else document["environment"]
    for key in ("model_id", "manifest", "model_file", "model_sha256", "quantization"):
        assert key in environment and environment[key] is None
    assert document["configuration"]["performance"]["answer_pass"] == "draft"
    assert '"answer_pass": "draft"' in (tmp_path / "out.md").read_text()
    assert "test-token" not in (tmp_path / "out.json").read_text()


def test_early_header_alias_rejects_conflicts(settings):
    with pytest.raises(ValueError, match="conflicts"):
        settings.configuration_from_args(
            argparse.Namespace(performance=["early_headers=false"], early_headers=True)
        )


@pytest.mark.parametrize("runner_name", ["run_corpus", "latency_benchmark"])
def test_no_local_requires_external_credentials_before_runtime(runner_name, monkeypatch):
    """RM26: an unconfigured external path cannot produce a no-local measurement."""
    runner = load_script_module(runner_name)
    monkeypatch.delenv(runner.harness.EXTERNAL_KEY_ENV, raising=False)
    args = argparse.Namespace(
        no_local_model=True, external_base_url=None, cases=None, languages=None
    )
    with pytest.raises(ValueError, match="external endpoint"):
        (runner.run_corpus if runner_name == "run_corpus" else runner.run_benchmark)(args)


def test_external_latency_routing_prompt_expects_a_completed_answer(monkeypatch):
    """RM25, RM26: a configured external path can complete the routing prompt."""
    import asyncio

    runner = load_script_module("latency_benchmark")

    async def stream(api, prompt, sample):
        sample.update(run_id="run-1", http_status=200)

    async def trace(api, run_id, sample):
        sample.update(state="completed", error_code=None)

    monkeypatch.setattr(runner, "_stream", stream)
    monkeypatch.setattr(runner, "_read_trace", trace)
    sample = asyncio.run(
        runner.measure(
            None, {"id": "routing", "label": "routing", "prompt": "Why?"}, timeout=1, external=True
        )
    )
    assert sample["error"] is None


def test_regrading_rejects_new_configuration_flags(tmp_path):
    """RM25: offline regrading uses the source configuration, never a supplied new one."""
    runner = load_script_module("run_corpus")
    with pytest.raises(SystemExit) as exc:
        runner.main(
            ["--out", str(tmp_path / "missing.json"), "--regrade", "--performance", "prefetch=true"]
        )
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "events,configured,expected",
    [
        ([], False, None),
        ([], True, False),
        ([], None, False),
        (
            [{"event_type": "run.accepted", "payload": {"local_model_status": "ready"}}],
            False,
            False,
        ),
        ([{"event_type": "run.accepted", "payload": {"local_model_status": "absent"}}], True, None),
        ([{"event_type": "run.accepted", "payload": {"local_model": None}}], True, None),
        ([{"event_type": "run.accepted", "payload": {}}], False, False),
        (
            [
                {
                    "event_type": "model.started",
                    "actor": "local",
                    "payload": {"model": "local-model", "call_id": "call-1"},
                }
            ],
            False,
            False,
        ),
    ],
)
def test_grade_uses_configuration_only_without_recorded_local_evidence(
    events, configured, expected
):
    """RM25, RM26: startup failures in the none class have no local-completion denominator."""
    from relaymind.evaluation.grading import grade_run

    runner = load_script_module("run_corpus")
    case = runner.load_frozen_corpus().case("simple-01")
    grade = grade_run(
        case, "auto", events, state="error", databases=None, local_model_configured=configured
    )
    assert grade.local_completion is expected
    # Existing callers without a configuration retain their historical grading.
    if not events:
        assert (
            grade_run(case, "auto", events, state="error", databases=None).local_completion is False
        )


def test_regrade_empty_trace_uses_stored_configuration(monkeypatch):
    """RM25, RM26: offline grading excludes no-local startup failures from the local rate."""
    runner = load_script_module("run_corpus")
    corpus = runner.load_frozen_corpus()
    case = corpus.case("simple-01")
    record = dict(run_id=None, state="error", error_code=None, reason_code=None, timed_out=False)
    old = runner.grade_record(case, "auto", record, [], None).to_json_object()
    document = dict(
        corpus={"sha256": corpus.digest},
        grades=[old],
        records={"simple-01/auto": record},
        traces={},
        configuration={"local_model_configured": False},
    )
    monkeypatch.setattr(runner.harness, "application_revision", lambda: {})
    assert runner.regrade(document, corpus)["grades"][0]["local_completion"] is None


def test_local_latency_export_has_no_dangling_external_region(monkeypatch, tmp_path):
    """RM25: an unused external region must not imply a configured endpoint."""
    from types import SimpleNamespace

    runner = load_script_module("latency_benchmark")
    manifest, artifact = make_artifact(tmp_path)
    runtime = runner.harness.Runtime(
        manifest, runner.harness.load_manifest(manifest), artifact, "test runtime"
    )

    def launch(self, log):
        log.write_text("")
        return None, "http://127.0.0.1:8081", 0.0

    class Gateway:
        def __init__(self, *args):
            pass

        def start(self):
            return SimpleNamespace(url="http://127.0.0.1:1", token="token", recorded_lines=[])

        def stop(self):
            pass

    async def samples(*args, **kwargs):
        return {"cold": {}, "warmups": [], "measured": []}

    monkeypatch.setattr(runner.harness.Runtime, "launch", launch)
    monkeypatch.setattr(runner.harness, "check_runtime", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(runner.harness, "application_revision", lambda: {})
    monkeypatch.setattr(runner.harness, "GatewayProcess", Gateway)
    monkeypatch.setattr(runner, "machine_manifest", lambda: {})
    monkeypatch.setattr(runner, "runtime_devices", lambda: None)
    monkeypatch.setattr(runner, "_props", lambda url: None)
    monkeypatch.setattr(runner, "run_samples", samples)
    args = argparse.Namespace(
        manifest=str(manifest),
        runtime_extra_args=(),
        external_base_url=None,
        external_region="unused-region",
        seed=1,
        runs=1,
        timeout=1,
        out=str(tmp_path / "local-latency.json"),
    )
    environment = runner.run_benchmark(args)["environment"]
    assert environment["external_endpoint"] is None
    assert environment["external_endpoint_region"] is None
