"""Extra runtime arguments for the local runtime (RM18, RM25; spec 0.31 section 12.2, #117).

A constrained configuration of one machine, such as a CPU-only runtime
with fewer threads, is measured by adding llama-server arguments next to
the pinned launch settings. The default adds none, a given argument is
launched and recorded in every export, and an argument that would change
a pinned setting or the model is refused, because the manifest stays the
only source of the qualified settings (RM18).
"""

import argparse
import json

import pytest

from helpers import PINNED_MANIFEST_PATH, REPO_ROOT, load_script_module
from relaymind.adapters.manifest import launch_settings_argv, load_manifest

CONSTRAINED = "--n-gpu-layers 0 --threads 2"
CONSTRAINED_TOKENS = ("--n-gpu-layers", "0", "--threads", "2")
BANNER = "version: 0.7.0-dev (build 11785, commit 8baabd53b)"


@pytest.fixture(scope="module")
def probe_timing():
    return load_script_module("probe_timing")


@pytest.fixture(scope="module")
def harness():
    return load_script_module("live_acceptance")


@pytest.fixture()
def pinned_runtime(harness, monkeypatch):
    """check_runtime over the pinned manifest, with no artifact read and no process started."""
    monkeypatch.setattr(harness, "_runtime_banner", lambda: BANNER)
    monkeypatch.setattr(harness, "verify_artifact", lambda manifest, artifact: None)
    launched = []

    def fake_launch(artifact, server_argv, log):
        launched.append(list(server_argv))
        return object(), "http://127.0.0.1:1", 0.5

    monkeypatch.setattr(harness, "_launch_server", fake_launch)
    return launched


def test_the_default_adds_no_argument_and_a_given_one_is_launched_and_recorded(
    harness, pinned_runtime, tmp_path
):
    """RM18, #117: the default launch is the manifest's; extras follow it and are recorded."""
    pinned = launch_settings_argv(load_manifest(PINNED_MANIFEST_PATH).launch_settings)

    plain = harness.check_runtime(PINNED_MANIFEST_PATH)
    plain.launch(tmp_path / "log")
    assert plain.extra_args == ()
    assert plain.server_argv == pinned
    assert pinned_runtime[-1] == pinned
    assert plain.server_record(1.23456) == {
        "argv": pinned,
        "extra_args": [],
        "cold_start_seconds": 1.235,
    }

    constrained = harness.check_runtime(PINNED_MANIFEST_PATH, extra_args=CONSTRAINED_TOKENS)
    constrained.launch(tmp_path / "log")
    assert constrained.server_argv == [*pinned, *CONSTRAINED_TOKENS]
    assert pinned_runtime[-1] == [*pinned, *CONSTRAINED_TOKENS]
    assert constrained.server_record(2.0) == {
        "argv": [*pinned, *CONSTRAINED_TOKENS],
        "extra_args": list(CONSTRAINED_TOKENS),
        "cold_start_seconds": 2.0,
    }


def test_the_option_text_is_split_like_a_shell_would(probe_timing):
    assert probe_timing.parse_runtime_extra_args("") == ()
    assert probe_timing.parse_runtime_extra_args("   ") == ()
    assert probe_timing.parse_runtime_extra_args(CONSTRAINED) == CONSTRAINED_TOKENS
    assert probe_timing.parse_runtime_extra_args("--threads=2") == ("--threads=2",)
    with pytest.raises(argparse.ArgumentTypeError, match="quot"):
        probe_timing.parse_runtime_extra_args("--api-key 'unclosed")


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        (("--ctx-size", "4096"), "pinned"),
        (("--ctx-size=4096",), "pinned"),
        (("--no-jinja",), "pinned"),
        (("--reasoning", "on"), "pinned"),
        (("--port", "9"), "harness"),
        (("--model", "other.gguf"), "harness"),
        (("--model=other.gguf",), "harness"),
        (("--hf-repo", "x/y"), "harness"),
        (("--chat-template-kwargs", '{"enable_thinking":true}'), "runtime-only"),
        (("--reasoning-budget", "-1"), "runtime-only"),
        (("--override-kv", "tokenizer.ggml.add_bos_token=bool:false"), "runtime-only"),
        (("--parallel", "1"), "runtime-only"),
        (("--n-gpu-layers", "0", "--rope-scale", "2"), "runtime-only"),
        (("--",), "runtime-only"),
        (("-t", "2"), "long flag"),
        (("-ngl", "0"), "long flag"),
        (("0",), "positional"),
    ],
)
def test_an_argument_that_would_change_the_pinned_settings_is_refused(probe_timing, extra, reason):
    """RM18: the manifest pins the model and its launch settings; extras only add."""
    pinned = launch_settings_argv(load_manifest(PINNED_MANIFEST_PATH).launch_settings)
    with pytest.raises(ValueError, match=reason):
        probe_timing.check_runtime_extra_args(extra, pinned)


@pytest.mark.parametrize(
    ("extra", "token"),
    [
        # llama-server takes [model_path] as a positional argument (RM18: the model is pinned).
        (("--threads", "2", "unauthorized_model.gguf"), "unauthorized_model.gguf"),
        (("unauthorized_model.gguf", "--threads", "2"), "unauthorized_model.gguf"),
        (("--threads", "2", "3"), "3"),
        (("--n-gpu-layers", "0", "-1"), "-1"),
    ],
)
def test_a_positional_argument_is_refused(probe_timing, extra, token):
    """RM18, #117: a positional token could name a model; only flags and their values pass."""
    pinned = launch_settings_argv(load_manifest(PINNED_MANIFEST_PATH).launch_settings)
    with pytest.raises(ValueError, match="positional") as refused:
        probe_timing.check_runtime_extra_args(extra, pinned)
    assert repr(token) in str(refused.value)


@pytest.mark.parametrize(
    ("extra", "token", "reason"),
    [
        (("--threads",), "--threads", "needs a value"),
        (("--threads", "--batch-size", "256"), "--threads", "needs a value"),
        (("--batch-size", "256", "--ubatch-size"), "--ubatch-size", "needs a value"),
        (("--threads=",), "--threads=", "needs a value"),
        (("--n-gpu-layers", "-t"), "--n-gpu-layers", "needs a value"),
        (("--threads", "high"), "--threads", "integer"),
        (("--threads=high",), "--threads=high", "integer"),
        (("--n-gpu-layers=foo",), "--n-gpu-layers=foo", "integer"),
        (("--batch-size", "2.5"), "--batch-size", "integer"),
        (("--threads", "1e3"), "--threads", "integer"),
        (("--threads", " 2"), "--threads", "integer"),
        (("--threads", "0"), "--threads", "at least 1"),
        (("--threads", "-1"), "--threads", "at least 1"),
        (("--threads-batch=0",), "--threads-batch=0", "at least 1"),
        (("--ubatch-size", "0"), "--ubatch-size", "at least 1"),
        (("--n-gpu-layers", "-2"), "--n-gpu-layers", "at least -1"),
        (("--gpu-layers", "none"), "--gpu-layers", "integer"),
    ],
)
def test_a_missing_or_invalid_value_is_refused(probe_timing, extra, token, reason):
    """#117: every allowed setting takes an integer value; the runtime would refuse to start."""
    pinned = launch_settings_argv(load_manifest(PINNED_MANIFEST_PATH).launch_settings)
    with pytest.raises(ValueError, match=reason) as refused:
        probe_timing.check_runtime_extra_args(extra, pinned)
    assert repr(token) in str(refused.value)


@pytest.mark.parametrize(
    "name", ["n-gpu-layers", "gpu-layers", "threads", "threads-batch", "batch-size", "ubatch-size"]
)
def test_a_no_form_of_a_value_setting_is_refused(probe_timing, name):
    """#117: llama-server b11785 has no --no- form of a setting that takes a value."""
    pinned = launch_settings_argv(load_manifest(PINNED_MANIFEST_PATH).launch_settings)
    with pytest.raises(ValueError, match="no --no- form") as refused:
        probe_timing.check_runtime_extra_args((f"--no-{name}",), pinned)
    assert repr(f"--no-{name}") in str(refused.value)


@pytest.mark.parametrize(
    "extra", [("--mlock",), ("--mlock", "foo"), ("--mmap",), ("--no-mmap",), ("--no-mlock",)]
)
def test_the_memory_switches_b11785_does_not_have_are_refused(probe_timing, extra):
    """#117: b11785 sets the loading mode with --load-mode; it has no --mlock or --mmap switch."""
    pinned = launch_settings_argv(load_manifest(PINNED_MANIFEST_PATH).launch_settings)
    with pytest.raises(ValueError, match="runtime-only") as refused:
        probe_timing.check_runtime_extra_args(extra, pinned)
    assert repr(extra[0]) in str(refused.value)


def test_a_refused_flag_is_named_without_its_value(probe_timing):
    """RM12: the value of a refused --flag=value may be a secret; the message names the flag."""
    pinned = launch_settings_argv(load_manifest(PINNED_MANIFEST_PATH).launch_settings)
    for extra in (("--api-key=sk-not-a-real-key",), ("--model=sk-not-a-real-key",)):
        with pytest.raises(ValueError) as refused:
            probe_timing.check_runtime_extra_args(extra, pinned)
        assert "sk-not-a-real-key" not in str(refused.value)
        assert repr(extra[0].split("=", 1)[0]) in str(refused.value)


def test_a_negative_value_is_a_value_and_not_a_flag(probe_timing):
    pinned = launch_settings_argv(load_manifest(PINNED_MANIFEST_PATH).launch_settings)
    given = ("--n-gpu-layers", "-1", "--threads", "2")
    assert probe_timing.check_runtime_extra_args(given, pinned) == given


def test_every_runtime_only_setting_is_accepted_with_its_value_in_both_forms(probe_timing):
    """#117: "--flag value" and "--flag=value"; the GPU layers also take auto and all."""
    pinned = launch_settings_argv(load_manifest(PINNED_MANIFEST_PATH).launch_settings)
    assert probe_timing.check_runtime_extra_args(CONSTRAINED_TOKENS, pinned) == CONSTRAINED_TOKENS
    given = (
        "--gpu-layers=0",
        "--threads-batch",
        "2",
        "--batch-size",
        "256",
        "--ubatch-size=128",
        "--threads=4",
    )
    assert probe_timing.check_runtime_extra_args(given, pinned) == given
    for words in (("--n-gpu-layers", "auto"), ("--gpu-layers=all",), ("--n-gpu-layers", "99")):
        assert probe_timing.check_runtime_extra_args(words, pinned) == words


def test_the_recorded_extra_arguments_pass_the_check(probe_timing):
    """RM16: every export that recorded extra arguments still passes the check unchanged."""
    pinned = launch_settings_argv(load_manifest(PINNED_MANIFEST_PATH).launch_settings)
    recorded = []
    for path in sorted((REPO_ROOT / "docs" / "evidence").glob("*.json")):
        document = json.loads(path.read_text("utf-8"))
        if not isinstance(document, dict):
            continue
        server = document.get("llama_server")
        extras = server.get("extra_args") if isinstance(server, dict) else None
        extras = extras or document.get("llama_server_extra_args")
        if extras:
            recorded.append(tuple(extras))
            assert probe_timing.check_runtime_extra_args(extras, pinned) == tuple(extras)
    assert CONSTRAINED_TOKENS in recorded


def test_check_runtime_refuses_a_pinned_argument(harness, pinned_runtime):
    with pytest.raises(SystemExit, match="pinned"):
        harness.check_runtime(PINNED_MANIFEST_PATH, extra_args=("--ctx-size", "4096"))


class _Stop(Exception):
    pass


@pytest.mark.parametrize("script", ["run_corpus", "latency_benchmark"])
def test_the_runners_pass_the_option_to_the_runtime_check(script, tmp_path, monkeypatch):
    """#117: both runners take --runtime-extra-args; without it they pass none."""
    runner = load_script_module(script)
    seen = []

    def check_runtime(manifest, extra_args=()):
        seen.append(tuple(extra_args))
        raise _Stop

    monkeypatch.setattr(runner.harness, "check_runtime", check_runtime)
    out = str(tmp_path / "x.json")
    for argv in (["--out", out], ["--out", out, f"--runtime-extra-args={CONSTRAINED}"]):
        with pytest.raises(_Stop):
            runner.main(argv)
    assert seen == [(), CONSTRAINED_TOKENS]


def test_the_live_harness_takes_the_option_too(harness, tmp_path, monkeypatch):
    seen = []

    def check_runtime(manifest, extra_args=()):
        seen.append(tuple(extra_args))
        raise _Stop

    monkeypatch.setattr(harness, "check_runtime", check_runtime)
    out = str(tmp_path / "x.json")
    for argv in (["--out", out], ["--out", out, f"--runtime-extra-args={CONSTRAINED}"]):
        with pytest.raises(_Stop):
            harness.main(argv)
    assert seen == [(), CONSTRAINED_TOKENS]
