"""RM18, RM22, RM25: reproducible harness commands and bounded resource lifetimes."""

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import load_script_module


@pytest.fixture
def harness():
    return load_script_module("live_acceptance")


@pytest.fixture
def lifecycle(harness, monkeypatch, tmp_path):
    """Replace inference and network boundaries; retain config and export file writes."""
    state = SimpleNamespace(
        fail=None,
        work=tmp_path / "private-work",
        llama=False,
        socket=False,
        gateway=False,
        exported=False,
    )

    def checkpoint(stage):
        if state.fail == stage:
            raise RuntimeError(stage)

    def make_work(**kwargs):
        state.work.mkdir()
        return str(state.work)

    def launch(*args):
        checkpoint("launch")
        state.llama = True
        return state, "http://127.0.0.1:8001", 0.1

    def stop(process):
        state.llama = False

    class Socket:
        def __init__(self):
            checkpoint("socket")
            state.socket = True

        def bind(self, address):
            checkpoint("bind")

        def getsockname(self):
            return "127.0.0.1", 8002

        def close(self):
            state.socket = False

    class Gateway:
        def __init__(self, config, log, env):
            self.restarts = []

        def start(self):
            state.gateway = True
            checkpoint("gateway_start")
            return SimpleNamespace(recorded_lines=[])

        def readiness(self):
            checkpoint("readiness")
            return {"status": 200, "body": {}}

        def stop(self):
            state.gateway = False

    async def run_plan(*args):
        checkpoint("scenario")
        return [harness.Result("routing_cache", ("RM25",))]

    def traces(*args):
        checkpoint("trace")
        assert state.work.is_dir()
        assert not state.gateway
        return {}

    def stats(*args):
        checkpoint("statistics")
        return "stats.json"

    write_json = harness.write_json

    def export(path, document, *, work):
        assert work.is_dir(), "work files must survive until evidence is exported"
        checkpoint("export")
        write_json(path, document, work=work)
        state.exported = True

    manifest = harness.load_manifest(harness.DEFAULT_MANIFEST)
    monkeypatch.setattr(harness, "_runtime_banner", lambda: f"version: {manifest.runtime_version}")
    monkeypatch.setattr(harness, "verify_artifact", lambda *args: None)
    monkeypatch.setattr(harness, "_git", lambda *args: "")
    monkeypatch.setattr(harness.tempfile, "mkdtemp", make_work)
    monkeypatch.setattr(harness, "_launch_server", launch)
    monkeypatch.setattr(harness, "_stop", stop)
    # Patch the harness binding, not Python's socket module used by asyncio.
    monkeypatch.setattr(harness, "socket", SimpleNamespace(socket=Socket))
    monkeypatch.setattr(harness, "_free_port", lambda: 8003)
    monkeypatch.setattr(harness, "GatewayProcess", Gateway)
    monkeypatch.setattr(harness, "_run_plan", run_plan)
    monkeypatch.setattr(harness, "_traces", traces)
    monkeypatch.setattr(harness, "_cache_stats", stats)
    monkeypatch.setattr(harness, "write_json", export)
    monkeypatch.setattr(
        harness,
        "launch_plans",
        lambda *args, **kwargs: [harness.LaunchPlan("local", (), "Lifecycle fixture")],
    )
    state.args = SimpleNamespace(
        manifest=str(harness.DEFAULT_MANIFEST),
        out=str(tmp_path / "evidence.json"),
        external_base_url=None,
        second_external_base_url=None,
        second_external_model=None,
        second_external_context_limit=None,
    )
    return state


@pytest.mark.parametrize(
    "failure",
    [
        "launch",
        "socket",
        "bind",
        "gateway_start",
        "readiness",
        "scenario",
        "trace",
        "statistics",
        "export",
    ],
)
def test_live_failure_releases_every_acquired_resource(harness, lifecycle, failure):
    """RM18/RM22/RM25, sections 13/14.1: failures leave no child or private workspace."""
    lifecycle.fail = failure
    with pytest.raises(RuntimeError, match=f"^{failure}$"):
        harness.run_live(lifecycle.args)
    assert not lifecycle.gateway, "gateway remains running"
    assert not lifecycle.socket, "reserved socket remains open"
    assert not lifecycle.llama, "local inference process remains running"
    assert not lifecycle.work.exists(), "private work directory was not removed"


def test_success_exports_before_removing_private_work(harness, lifecycle):
    """RM22/RM25: retain the work files through export, then release all resources."""
    document = harness.run_live(lifecycle.args)
    assert lifecycle.exported
    assert json.loads(Path(lifecycle.args.out).read_text())["kind"] == document["kind"]
    assert not lifecycle.gateway
    assert not lifecycle.socket
    assert not lifecycle.llama
    assert not lifecycle.work.exists()


def test_cli_default_manifest_works_outside_checkout(harness, monkeypatch, tmp_path):
    """RM18/RM25: an absolute invocation can resolve the pinned default from any cwd."""
    monkeypatch.chdir(tmp_path)

    def run(args):
        manifest = harness.load_manifest(Path(args.manifest))
        assert manifest.model_id
        return {"summary": {}}

    monkeypatch.setattr(harness, "run_live", run)
    harness.main(["--out", "evidence.json"])


def test_recorded_command_preserves_spaced_and_quoted_arguments(harness, monkeypatch):
    """RM25, section 12.3: the recorded command must reproduce argument boundaries."""
    arguments = ["--out", "my evidence/run.json", "--external-model", "model 'quoted' $name"]
    monkeypatch.setattr(harness.sys, "argv", ["scripts/live_acceptance.py", *arguments])
    monkeypatch.setattr(harness, "os", SimpleNamespace(name="posix"))
    assert shlex.split(harness._command())[2:] == arguments


def test_recorded_command_uses_windows_quoting(harness, monkeypatch):
    """RM18/RM25: native Windows command records use Windows argument escaping."""
    monkeypatch.setattr(
        harness.sys,
        "argv",
        [
            "scripts/live_acceptance.py",
            "--out",
            "my evidence/run.json",
            "--external-model",
            'model "quoted"',
        ],
    )
    monkeypatch.setattr(harness, "os", SimpleNamespace(name="nt"))
    assert harness._command() == (
        'python scripts/live_acceptance.py --out "my evidence/run.json" '
        '--external-model "model \\"quoted\\""'
    )


@pytest.mark.parametrize("missing", [True, False])
def test_planned_swap_is_reported_when_a_prerequisite_scenario_fails(
    harness, lifecycle, monkeypatch, missing
):
    """AC08/RM18: a planned swap is never omitted when an external-only run is missing."""
    endpoint = harness.ExternalEndpoint("https://example.invalid/v1", "model-a", 8192)
    lifecycle.args.external_base_url = endpoint.base_url
    lifecycle.args.external_model = endpoint.model
    lifecycle.args.external_context_limit = endpoint.context_limit
    second = harness.ExternalEndpoint(
        "https://second.invalid/v1", "model-b", 8192, harness.SECOND_EXTERNAL_KEY_ENV
    )
    lifecycle.args.second_external_base_url = second.base_url
    lifecycle.args.second_external_model = second.model
    lifecycle.args.second_external_context_limit = second.context_limit
    monkeypatch.setenv(harness.EXTERNAL_KEY_ENV, "fixture-key")
    monkeypatch.setenv(harness.SECOND_EXTERNAL_KEY_ENV, "fixture-key")
    monkeypatch.setattr(
        harness,
        "launch_plans",
        lambda *args, **kwargs: [
            harness.LaunchPlan("external", ("external_only",), "First", external=endpoint),
            harness.LaunchPlan("external-second", ("external_only",), "Second", external=second),
        ],
    )

    async def run_plan(*args):
        return (
            []
            if missing
            else [harness.Result("external_only", ("AC08",), error="admission failed")]
        )

    monkeypatch.setattr(harness, "_run_plan", run_plan)
    document = harness.run_live(lifecycle.args)
    swaps = [s for s in document["scenarios"] if s["name"] == "ac08_external_swap"]
    assert len(swaps) == 1
    assert swaps[0]["status"] == "error"
    assert swaps[0]["error"]
