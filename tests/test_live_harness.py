"""Deterministic checks of the live harness (RM18, RM22, AC08, spec 6/13).

These test configuration and evidence formatting, not model capability.
"""

import json
import os
import subprocess
import sys
from pathlib import PureWindowsPath

import pytest

from helpers import (
    LIVE_MANIFEST_ENV,
    PINNED_MANIFEST_PATH,
    REPO_ROOT,
    SECOND_MANIFEST_PATH,
    live_manifest_path,
)
from relaymind.adapters.manifest import load_manifest


def test_relative_manifest_is_repo_relative_from_another_directory(tmp_path, monkeypatch):
    """RM18, AC08: changing cwd must not select a different artifact."""
    monkeypatch.chdir(tmp_path)
    selected = live_manifest_path(
        {LIVE_MANIFEST_ENV: "models/../models/" + SECOND_MANIFEST_PATH.name}
    )
    assert selected == SECOND_MANIFEST_PATH
    assert load_manifest(selected) == load_manifest(SECOND_MANIFEST_PATH)


def test_absolute_manifest_outside_repo_is_preserved(tmp_path, monkeypatch):
    """RM18: operators can select a manifest outside the checkout."""
    path = tmp_path / "custom.manifest.json"
    path.write_bytes(SECOND_MANIFEST_PATH.read_bytes())
    monkeypatch.chdir(REPO_ROOT / "tests")
    assert live_manifest_path({LIVE_MANIFEST_ENV: str(path)}) == path
    assert load_manifest(path) == load_manifest(SECOND_MANIFEST_PATH)


@pytest.mark.parametrize("selection", [None, "relative", "absolute"])
def test_export_uses_one_repo_path_format(tmp_path, monkeypatch, selection):
    """RM22, AC08: default and selected traces use the committed <repo> format."""
    import test_local_loop_live as live

    monkeypatch.delenv(LIVE_MANIFEST_ENV, raising=False)
    manifest_path = PINNED_MANIFEST_PATH
    if selection is not None:
        manifest_path = SECOND_MANIFEST_PATH
        value = manifest_path.relative_to(REPO_ROOT) if selection == "relative" else manifest_path
        monkeypatch.setenv(LIVE_MANIFEST_ENV, str(value))
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "trace.json"
    monkeypatch.setenv(live.TRACE_ENV, str(output))
    monkeypatch.setattr(live, "_runtime_banner", lambda: "test runtime")
    monkeypatch.setattr(live, "_records", [])
    live._write_export(load_manifest(manifest_path))
    exported = json.loads(output.read_text())
    assert exported["manifest"] == f"<repo>/models/{manifest_path.name}"
    assert exported["model_sha256"] == load_manifest(manifest_path).file_sha256


@pytest.mark.parametrize("module", ["test_llamacpp_live_smoke", "test_local_loop_live"])
@pytest.mark.parametrize(
    ("case", "status", "message"),
    [
        ("default_missing", 0, "model artifact not found"),
        ("selected_missing", 1, "names a manifest whose artifact is missing"),
        ("override_missing", 1, "RELAYMIND_SMOKE_MODEL points to a missing file"),
        ("broken_manifest", 1, "cannot read manifest"),
        ("runtime_missing", 0, "llama-server not on PATH"),
    ],
)
def test_live_fixture_checks_artifact_before_runtime(tmp_path, module, case, status, message):
    """RM18, AC08: explicit broken selections fail even without a runtime.

    Run real pytest fixture resolution in both live modules, with no
    runtime on PATH and disposable model paths. No inference is run.
    """
    models = tmp_path / "models"
    models.mkdir()
    if case == "runtime_missing":
        (models / load_manifest(PINNED_MANIFEST_PATH).file_name).write_bytes(b"unused")
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "from pathlib import Path\n"
        "import test_llamacpp_live_smoke as smoke\n"
        f"smoke.MODELS_DIR = Path({str(models)!r})\n"
        f"pytest_plugins = [{module!r}]\n"
        "def test_server(server):\n    pass\n"
    )
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\nmarkers = live: live model tests\n")
    environ = dict(os.environ)
    for name in (LIVE_MANIFEST_ENV, "RELAYMIND_SMOKE_MODEL", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        environ.pop(name, None)
    environ["PATH"] = ""
    environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environ["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT / "tests"), str(REPO_ROOT / "src")))
    if case == "selected_missing":
        environ[LIVE_MANIFEST_ENV] = str(SECOND_MANIFEST_PATH)
    elif case == "override_missing":
        environ["RELAYMIND_SMOKE_MODEL"] = str(tmp_path / "missing.gguf")
    elif case == "broken_manifest":
        environ[LIVE_MANIFEST_ENV] = str(tmp_path / "missing.manifest.json")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(config),
            "-q",
            "-ra",
            "-p",
            "no:cacheprovider",
            str(probe),
        ],
        cwd=tmp_path,
        env=environ,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == status, output
    assert message in output, output


@pytest.mark.parametrize("username", ["operator", "用户"])
def test_export_redacts_native_windows_skill_paths(tmp_path, monkeypatch, username):
    """RM22: trace exports omit the checkout even with a Unicode Windows username."""
    import test_local_loop_live as live

    checkout = PureWindowsPath("C:/Users") / username / "relaymind"
    monkeypatch.setattr(live, "REPO_ROOT", checkout)
    monkeypatch.setattr(live, "_records", [{"skill_root": str(checkout / "skills")}])
    monkeypatch.setattr(live, "_runtime_banner", lambda: "test runtime")
    monkeypatch.delenv(LIVE_MANIFEST_ENV, raising=False)
    output = tmp_path / "trace.json"
    monkeypatch.setenv(live.TRACE_ENV, str(output))
    live._write_export(load_manifest(PINNED_MANIFEST_PATH))
    exported = json.loads(output.read_text())
    assert exported["runs"][0]["skill_root"] == str(PureWindowsPath("<repo>/skills"))
