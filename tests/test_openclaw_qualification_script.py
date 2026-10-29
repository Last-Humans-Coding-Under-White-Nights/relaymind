"""The OpenClaw qualification harness (RM33, AC24, #135; RM12, RM19).

``scripts/openclaw_qualification.py`` serves the real gateway over model
doubles. Its ``host`` mode runs an operator command (a real OpenClaw host on
the operator's machine; a stub here) with the token in the child's
environment only. Its ``fixtures`` mode captures the deterministic transport
fixtures that ``integrations/openclaw/test/fixtures/`` commits; the committed
set must be what a fresh capture produces.
"""

import json
import sys
from pathlib import Path

import pytest

from helpers import load_script_module

qualification = load_script_module("openclaw_qualification")

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "integrations" / "openclaw" / "test" / "fixtures" / "v2026.10.7"

# A stand-in for the host: one client-owned chat request with the token from the
# environment, then it prints the token, which the export must not keep.
STUB_HOST = """
import json, os, urllib.request
base = os.environ["RELAYMIND_BASE_URL"]
token = os.environ["RELAYMIND_APP_TOKEN"]
body = {"model": "relaymind-client-tools", "messages": [{"role": "user", "content": "Hi"}]}
request = urllib.request.Request(
    base + "/chat/completions", data=json.dumps(body).encode(), method="POST",
    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(request) as response:
    print(json.loads(response.read())["choices"][0]["message"]["content"])
print("token was", token)
"""


def test_host_mode_runs_the_command_and_exports_the_runs_without_the_token(tmp_path):
    """RM12, RM19: the token reaches only the child's environment, never the export."""
    out = tmp_path / "export.json"
    export = qualification.run_host([sys.executable, "-c", STUB_HOST], out)
    text = out.read_text("utf-8")
    assert export["status"] == "completed"
    assert export["token"] == "<not recorded>"
    assert "token was <not recorded>" in export["stdout"]
    assert "Hello from RelayMind." in export["stdout"]
    (run,) = export["runs"]
    assert run["state"] == "completed"
    assert any(e["type"] == "run.completed" for e in run["events"])
    # The gateway executed no host tool (RM08, section 12.5).
    assert export["host_tool_executions_by_gateway"] == 0
    assert "deterministic" in export["evidence"]
    assert json.loads(text)["status"] == "completed"
    assert export["versions"]["relaymind_revision"]["head"]


def test_a_failing_host_command_is_recorded_as_failed(tmp_path):
    export = qualification.run_host(
        [sys.executable, "-c", "raise SystemExit(3)"], tmp_path / "export.json"
    )
    assert export["status"] == "failed"
    assert export["exit_code"] == 3
    assert export["runs"] == []


def test_a_missing_host_command_is_not_run_never_a_pass(tmp_path):
    """Section 12.5: a run that did not happen stays not_run."""
    export = qualification.run_host(["openclaw-not-installed-here"], tmp_path / "export.json")
    assert export["status"] == "not_run"
    assert export["runs"] == []


def test_the_main_entry_requires_a_command_for_host_mode(tmp_path):
    with pytest.raises(SystemExit):
        qualification.main(["host", "--out", str(tmp_path / "x.json")])


def test_host_mode_keeps_a_separator_the_host_command_needs(tmp_path, monkeypatch):
    """Only the "--" before the command is dropped; the command keeps its own."""
    seen = {}

    def fake_run_host(command, out, *, timeout):
        seen["command"] = command
        return {"status": "completed", "runs": []}

    monkeypatch.setattr(qualification, "run_host", fake_run_host)
    argv = ["host", "--out", str(tmp_path / "x.json"), "--"]
    argv += ["openclaw", "agent", "exec", "--json", "--", "--literal prompt"]
    assert qualification.main(argv) == 0
    assert seen["command"] == ["openclaw", "agent", "exec", "--json", "--", "--literal prompt"]


def test_a_host_command_past_the_timeout_is_recorded_as_timed_out(tmp_path):
    """Section 12.5: a host that hangs still leaves an export with its runs, never a pass."""
    out = tmp_path / "export.json"
    script = "import sys, time; print('started', flush=True); time.sleep(30)"
    export = qualification.run_host([sys.executable, "-c", script], out, timeout=1)
    assert export["status"] == "timed_out"
    assert export["exit_code"] is None
    assert "started" in export["stdout"]
    assert export["runs"] == []
    assert json.loads(out.read_text("utf-8"))["status"] == "timed_out"


def test_sanitizing_replaces_run_ids_ports_dates_and_machine_headers():
    scenario = qualification.SCENARIOS[1]
    output = {
        "versions": {
            "openclaw": "2026.10.7",
            "@openclaw/ai": "2026.10.7",
            "openai": "7.27.0",
            "node": "v24.22.1",
        },
        "scenario": {"prompt": "Say hello."},
        "exchanges": [
            {
                "request": {
                    "method": "POST",
                    "path": "/v1/chat/completions",
                    "headers": {
                        "host": "127.0.0.1:41234",
                        "x-stainless-os": "Linux",
                        "authorization": "<redacted>",
                    },
                    "body": {"model": "relaymind-client-tools"},
                },
                "response": {
                    "status": 200,
                    "headers": {
                        "date": "Tue, 27 Oct 2026 10:00:00 GMT",
                        "x-relaymind-run-id": "run_abc",
                    },
                    "body": {"id": "chatcmpl-run_abc", "created": 1793000000},
                },
                "client_closed_early": False,
            }
        ],
        "results": [{"stop_reason": "stop", "text": "Hi", "usage": {"input": 0}}],
        "host_tool_executions": [],
    }
    fixture = qualification.sanitize_fixture(scenario, output, ["run_abc"], {"run_abc": "x"})
    (exchange,) = fixture["exchanges"]
    assert exchange["request"]["headers"]["host"] == "127.0.0.1:<port>"
    assert exchange["request"]["headers"]["x-stainless-os"] == "<environment>"
    assert exchange["response"]["headers"]["date"] == "<date>"
    assert exchange["response"]["headers"]["x-relaymind-run-id"] == "<run_1>"
    assert exchange["response"]["body"] == {"id": "chatcmpl-<run_1>", "created": 0}
    assert fixture["gateway_runs"] == [{"run": "<run_1>", "state": "x"}]
    assert "node" not in fixture["pins"]
    # The host's zero usage is not reported as a gateway count (section 7.5).
    assert fixture["results"] == [
        {"stop_reason": "stop", "text": "Hi", "usage_reported_by_gateway": False}
    ]


def test_every_scenario_has_a_committed_fixture():
    """#135: the committed set covers every scenario of the harness."""
    committed = {p.stem for p in (FIXTURES / "transport").glob("*.json")}
    assert committed == {s.name for s in qualification.SCENARIOS}
    versions = json.loads((FIXTURES / "VERSIONS.json").read_text("utf-8"))
    assert versions["openclaw"] == "2026.10.7"
    assert versions["@openclaw/ai"] == "2026.10.7"


# What a capture reads and writes: a change to any of them needs a new capture.
CAPTURE_INPUTS = (
    "scripts/openclaw_qualification.py",
    "integrations/openclaw/package.json",
    "integrations/openclaw/package-lock.json",
    "integrations/openclaw/src/compat.ts",
    "integrations/openclaw/test/host-transport",
    "integrations/openclaw/test/fixtures/v2026.10.7/transport",
)


def test_the_recorded_revision_holds_the_committed_harness_and_fixtures():
    """Section 12.5 provenance: VERSIONS.json names a clean revision that holds them."""
    import shutil
    import subprocess

    revision = json.loads((FIXTURES / "VERSIONS.json").read_text("utf-8"))["relaymind_revision"]
    assert revision["dirty"] is False
    head = revision["head"]
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    known = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", f"{head}^{{commit}}"], capture_output=True
    )
    if known.returncode != 0:
        pytest.skip(f"revision {head} is not in this clone (shallow checkout?)")
    diff = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--name-only", head, "HEAD", "--", *CAPTURE_INPUTS],
        capture_output=True,
        text=True,
        check=True,
    )
    assert diff.stdout == "", f"capture inputs changed since {head}; capture again"


def test_the_committed_transport_fixtures_are_reproducible(tmp_path):
    """#135 done-when: the sanitized wire fixtures are reproducible on the same pins."""
    missing = qualification_missing()
    if missing:
        pytest.skip(missing)
    out = tmp_path / "transport"
    qualification.capture_fixtures(out)
    for path in sorted((FIXTURES / "transport").glob("*.json")):
        fresh = json.loads((out / path.name).read_text("utf-8"))
        committed = json.loads(path.read_text("utf-8"))
        assert fresh == committed, path.name


def qualification_missing():
    import shutil

    if shutil.which("node") is None:
        return "node is not on PATH"
    for package in ("@openclaw/ai", "openclaw"):
        if not (ROOT / "integrations/openclaw/node_modules" / package / "package.json").exists():
            return f"run npm ci in integrations/openclaw ({package} is missing)"
    return None


def test_the_driver_knows_it_is_the_entry_point_on_every_platform():
    """The pinned host supports native Windows (#135; section 7.5).

    A driver that builds its own ``file://`` URL from ``process.argv[1]`` never
    matches ``import.meta.url`` on Windows (``file:///C:/...`` against
    ``file://C:\\...``), so it exits 0 without running the scenario or writing
    its output. The entry check compares the URL Node itself builds.
    """
    source = qualification.DRIVER.read_text("utf-8")
    assert "file://${process.argv[1]}" not in source
    assert "import.meta.url === pathToFileURL(process.argv[1]).href" in source
