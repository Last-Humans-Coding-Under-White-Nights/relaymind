"""Extra runtime arguments for the local runtime (RM18, RM25; spec 0.31 section 12.2, #117).

A constrained configuration of one machine, such as a CPU-only runtime
with fewer threads, is measured by adding llama-server arguments next to
the pinned launch settings. The default adds none, a given argument is
launched and recorded in every export, and an argument that would change
a pinned setting or the model is refused, because the manifest stays the
only source of the qualified settings (RM18).
"""

import argparse

import pytest

from helpers import PINNED_MANIFEST_PATH, load_script_module
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
        (("--hf-repo", "x/y"), "harness"),
        (("-t", "2"), "long flag"),
        (("-ngl", "0"), "long flag"),
        (("0",), "start with a flag"),
    ],
)
def test_an_argument_that_would_change_the_pinned_settings_is_refused(probe_timing, extra, reason):
    """RM18: the manifest pins the model and its launch settings; extras only add."""
    pinned = launch_settings_argv(load_manifest(PINNED_MANIFEST_PATH).launch_settings)
    with pytest.raises(ValueError, match=reason):
        probe_timing.check_runtime_extra_args(extra, pinned)


def test_a_negative_value_is_a_value_and_not_a_flag(probe_timing):
    pinned = launch_settings_argv(load_manifest(PINNED_MANIFEST_PATH).launch_settings)
    given = ("--n-gpu-layers", "-1", "--threads", "2")
    assert probe_timing.check_runtime_extra_args(given, pinned) == given


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
