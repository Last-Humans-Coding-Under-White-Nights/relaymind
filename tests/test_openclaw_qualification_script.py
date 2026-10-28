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
    # The stub offers no tools, so the gateway repairs the scripted echo call
    # and the closing text is the answer.
    assert "The tool said: echo: lantern." in export["stdout"]
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_host_command_that_cannot_start_is_not_run(tmp_path):
    """Section 12.5: a command the OS refuses to start (no execute bit) stays not_run."""
    host = tmp_path / "openclaw"
    host.write_text("#!/bin/sh\nexit 0\n", "utf-8")
    host.chmod(0o644)
    export = qualification.run_host([str(host)], tmp_path / "export.json")
    assert export["status"] == "not_run"
    assert export["exit_code"] is None
    assert export["runs"] == []


def test_the_runs_are_listed_in_the_order_the_store_created_them(tmp_path):
    """Section 12.5: store order, not the order of the random run IDs."""
    from types import SimpleNamespace

    from relaymind.store.run_store import RunStore

    with RunStore(tmp_path / "runs.sqlite3") as store:
        created = [store.create_run("accepted").run_id for _ in range(8)]
        assert qualification._run_ids(SimpleNamespace(store=store)) == created


def test_the_main_entry_requires_a_command_for_host_mode(tmp_path):
    with pytest.raises(SystemExit):
        qualification.main(["host", "--out", str(tmp_path / "x.json")])


def test_host_mode_keeps_a_separator_the_host_command_needs(tmp_path, monkeypatch):
    """Only the "--" before the command is dropped; the command keeps its own."""
    seen = {}

    def fake_run_host(command, out, *, timeout, scenario):
        seen["command"] = command
        return {"status": "completed", "runs": [], "checks": {"passed": True}}

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


# -- the host approval scenarios (#137; RM08, RM33, AC24) ----------------------------------

# A stand-in for the host's tool loop: the write call, the host's decision, and
# the result in a second request. The decision comes from the argument.
STUB_TOOL_HOST = """
import json, os, sys, urllib.request
decision = sys.argv[1]
base = os.environ["RELAYMIND_BASE_URL"]
token = os.environ["RELAYMIND_APP_TOKEN"]
sandbox = os.environ["RELAYMIND_TEST_SANDBOX"]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
write = {"type": "function", "function": {"name": "relaymind_test_write", "parameters": {
    "type": "object", "properties": {"name": {"type": "string"}, "line": {"type": "string"}},
    "required": ["name", "line"], "additionalProperties": False}}}
def post(messages):
    body = {"model": "relaymind-client-tools", "messages": messages, "tools": [write]}
    request = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with opener.open(request) as response:
        return json.loads(response.read())["choices"][0]["message"]
messages = [{"role": "user", "content": "Write a note."}]
reply = post(messages)
(call,) = reply["tool_calls"]
arguments = json.loads(call["function"]["arguments"])
if decision == "allow":
    with open(os.path.join(sandbox, arguments["name"]), "a") as f:
        f.write(arguments["line"] + "\\n")
    result = "wrote one line"
else:
    result = "denied by the host approval policy"
replayed = call["id"]
if len(sys.argv) > 2 and sys.argv[2] == "rewrite":
    # What OpenClaw 2026.10.7 did on the macOS probe: call_probe_1 -> callprobe1.
    replayed = "".join(c for c in call["id"] if c.isascii() and c.isalnum())[:40]
    reply = {**reply, "tool_calls": [{**call, "id": replayed}]}
messages += [reply, {"role": "tool", "tool_call_id": replayed, "content": result}]
final = post(messages)["content"]
# The host's own report says ok whatever the tool did (macOS probe, C2).
print(json.dumps({"ok": True, "status": "ok", "final": final,
                  "toolSummary": {"calls": 1, "failures": 0 if decision == "allow" else 1}}))
"""


@pytest.mark.parametrize(
    ("scenario", "decision", "lines"),
    [("tool-deny", "deny", 0), ("tool-approve", "allow", 1)],
)
def test_the_approval_scenarios_check_the_sandbox_and_the_gateway(
    tmp_path, scenario, decision, lines
):
    """RM08, RM33, AC24: the export says whether the host's decision held, from evidence."""
    export = qualification.run_host(
        [sys.executable, "-c", STUB_TOOL_HOST, decision],
        tmp_path / "export.json",
        scenario=scenario,
    )
    assert export["status"] == "completed", export["stderr"]
    assert export["scenario"] == scenario
    check = export["checks"]
    assert check["sandbox_lines"] == lines
    assert check["expected_sandbox_lines"] == lines
    assert check["gateway_tool_executions"] == 0
    assert [p["tool_name"] for p in check["proposed"]] == ["relaymind_test_write"]
    assert check["result_reached_next_run"] is True
    assert check["passed"] is True
    first, second = export["runs"]
    assert {r["state"] for r in (first, second)} == {"completed"}


def test_a_denial_that_wrote_anyway_does_not_pass(tmp_path):
    """Section 12.5: the check reads the sandbox; a write under tool-deny fails the scenario."""
    export = qualification.run_host(
        [sys.executable, "-c", STUB_TOOL_HOST, "allow"],
        tmp_path / "export.json",
        scenario="tool-deny",
    )
    assert export["status"] == "completed"
    assert export["checks"]["sandbox_lines"] == 1
    assert export["checks"]["passed"] is False


def test_a_scenario_that_did_not_run_is_not_a_pass(tmp_path):
    export = qualification.run_host(
        ["openclaw-not-installed-here"], tmp_path / "export.json", scenario="tool-approve"
    )
    assert export["status"] == "not_run"
    assert export["checks"]["passed"] is False


def test_the_main_entry_passes_the_scenario(tmp_path, monkeypatch):
    seen = {}

    def fake_run_host(command, out, *, timeout, scenario):
        seen["scenario"] = scenario
        return {"status": "completed", "runs": [], "checks": {"passed": True}}

    monkeypatch.setattr(qualification, "run_host", fake_run_host)
    argv = ["host", "--out", str(tmp_path / "x.json"), "--scenario", "tool-deny", "--", "true"]
    assert qualification.main(argv) == 0
    assert seen["scenario"] == "tool-deny"
    with pytest.raises(SystemExit):
        qualification.main(["host", "--out", "x.json", "--scenario", "nope", "--", "true"])


# -- the host's call ID rewrite (#137; RM19, section 7.5) ----------------------------------


def test_the_host_replay_rule_is_the_observed_and_pinned_rewrite():
    """Section 7.5: the host replays a call ID with only [A-Za-z0-9], at most 40 characters.

    Observed on the macOS probe (OpenClaw 2026.10.7): call_probe_1 came back
    as callprobe1. The other characters and the 40-character cap are read from
    the pinned source (src/agents/tool-call-id.ts, "strict" mode), not observed.
    """
    replay = qualification.host_replayed_call_id
    assert replay("call_probe_1") == "callprobe1"
    assert replay("call_run_0123abcd_1") == "callrun0123abcd1"
    assert replay("call-a.b:c") == "callabc"
    assert replay("x" * 50) == "x" * 40
    answers = qualification.answers_call
    assert answers("call_write_1", "call_write_1")
    assert answers("call_write_1", "callwrite1")
    assert not answers("call_write_1", "callwrite2")
    assert not answers("call_write_1", "")


def test_a_host_that_rewrites_the_call_id_still_reaches_the_next_run(tmp_path):
    """Section 7.5: no exact-string match across requests; the rewritten ID still answers."""
    export = qualification.run_host(
        [sys.executable, "-c", STUB_TOOL_HOST, "allow", "rewrite"],
        tmp_path / "export.json",
        scenario="tool-approve",
    )
    assert export["status"] == "completed", export["stderr"]
    check = export["checks"]
    assert [p["proposal_id"] for p in check["proposed"]] == ["call_write_1"]
    # The ID as the host sent it back, so an export shows the rewrite (Q6).
    assert check["replayed_call_ids"] == ["callwrite1"]
    assert check["result_reached_next_run"] is True
    assert check["passed"] is True


# -- the host's own report is not task success (#137; section 12.5) ------------------------


def test_the_host_report_is_kept_apart_from_the_checks(tmp_path):
    """Section 12.5: the host said ok for a denied tool on the probe; only evidence passes."""
    export = qualification.run_host(
        [sys.executable, "-c", STUB_TOOL_HOST, "deny"],
        tmp_path / "export.json",
        scenario="tool-approve",
    )
    assert export["status"] == "completed"
    check = export["checks"]
    assert check["host_report"] == {"ok": True, "status": "ok", "tool_calls": 1, "tool_failures": 1}
    assert check["sandbox_lines"] == 0
    assert check["passed"] is False


def test_a_host_report_that_is_not_json_is_recorded_as_absent(tmp_path):
    export = qualification.run_host(
        [sys.executable, "-c", STUB_HOST], tmp_path / "export.json", scenario="text-tool"
    )
    assert export["checks"]["host_report"] is None


# -- the default scenario in one host turn (#137; RM08, RM33, AC24) -----------------------

# A stand-in for one `openclaw agent exec`: the echo call, the host runs it, and
# the result goes back in a second request of the same turn.
STUB_ECHO_HOST = """
import json, os, sys, urllib.request
base = os.environ["RELAYMIND_BASE_URL"]
token = os.environ["RELAYMIND_APP_TOKEN"]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
echo = {"type": "function", "function": {"name": "relaymind_test_echo", "parameters": {
    "type": "object", "properties": {"text": {"type": "string"}},
    "required": ["text"], "additionalProperties": False}}}
def post(messages):
    body = {"model": "relaymind-client-tools", "messages": messages, "tools": [echo]}
    request = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with opener.open(request) as response:
        return json.loads(response.read())["choices"][0]["message"]
messages = [{"role": "user", "content": "Echo lantern with the test echo tool."}]
reply = post(messages)
calls = reply.get("tool_calls") or []
if not calls:
    print(json.dumps({"ok": True, "final": reply["content"]}))
    raise SystemExit(0)
(call,) = calls
arguments = json.loads(call["function"]["arguments"])
replayed = "".join(c for c in call["id"] if c.isascii() and c.isalnum())[:40]
reply = {**reply, "tool_calls": [{**call, "id": replayed}]}
messages += [reply, {"role": "tool", "tool_call_id": replayed,
                     "content": "echo: " + arguments["text"]}]
print(json.dumps({"ok": True, "final": post(messages)["content"]}))
"""


def test_the_default_scenario_passes_in_one_host_turn(tmp_path):
    """Q8: the documented command is one `agent exec`; the default scenario must fit it.

    On macOS at a52f8bf one exec got the scenario's opening text turn, ended,
    and could never show a proposed call, so ``checks.passed`` was false even
    though the host printed the scripted text.
    """
    export = qualification.run_host(
        [sys.executable, "-c", STUB_ECHO_HOST], tmp_path / "export.json", scenario="text-tool"
    )
    assert export["status"] == "completed", export["stderr"]
    check = export["checks"]
    assert [p["tool_name"] for p in check["proposed"]] == ["relaymind_test_echo"]
    assert check["result_reached_next_run"] is True
    assert check["gateway_tool_executions"] == 0
    assert check["passed"] is True
    assert json.loads(export["stdout"])["final"] == "The tool said: echo: lantern."


def test_a_text_only_host_turn_does_not_pass_the_default_scenario(tmp_path):
    """Section 12.5: a host that never sent the tool result has no round trip to show."""
    export = qualification.run_host(
        [sys.executable, "-c", STUB_HOST], tmp_path / "export.json", scenario="text-tool"
    )
    assert export["status"] == "completed"
    assert export["checks"]["result_reached_next_run"] is False
    assert export["checks"]["passed"] is False


# -- the host exec policy scenarios (#137; RM08, RM33, AC24) -------------------------------

# A stand-in for the host's exec tool under tools.exec.mode: deny returns the
# host's error result and runs nothing; full runs the command in the sandbox.
STUB_EXEC_HOST = """
import json, os, subprocess, sys, urllib.request
mode = sys.argv[1]
base = os.environ["RELAYMIND_BASE_URL"]
token = os.environ["RELAYMIND_APP_TOKEN"]
sandbox = os.environ["RELAYMIND_TEST_SANDBOX"]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
exec_tool = {"type": "function", "function": {"name": "exec", "parameters": {
    "type": "object", "required": ["command"],
    "properties": {"title": {"type": "string"}, "command": {"type": "string"}}}}}
def post(messages):
    body = {"model": "relaymind-client-tools", "messages": messages, "tools": [exec_tool]}
    request = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with opener.open(request) as response:
        return json.loads(response.read())["choices"][0]["message"]
messages = [{"role": "user", "content": "Write the note with exec."}]
reply = post(messages)
(call,) = reply["tool_calls"]
command = json.loads(call["function"]["arguments"])["command"]
if mode in ("full", "full-elsewhere"):
    # full-elsewhere: the host's exec workdir is not the sandbox.
    workdir = sandbox if mode == "full" else os.path.dirname(sandbox)
    subprocess.run(command, shell=True, cwd=workdir, check=True)
    result = "(no output)"
else:
    result = json.dumps({"status": "error", "tool": "exec",
                         "error": "exec denied: host=gateway security=deny"})
replayed = "".join(c for c in call["id"] if c.isalnum())[:40]
reply = {**reply, "tool_calls": [{**call, "id": replayed}]}
messages += [reply, {"role": "tool", "tool_call_id": replayed, "content": result}]
post(messages)
print(json.dumps({"ok": True, "status": "ok"}))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the stub host runs a POSIX shell command")
@pytest.mark.parametrize(
    ("scenario", "mode", "lines"),
    [("exec-deny", "deny", 0), ("exec-full", "full", 1)],
)
def test_the_exec_policy_scenarios_check_the_sandbox_and_the_gateway(
    tmp_path, scenario, mode, lines
):
    """RM08, RM33, AC24: the observed headless policy pair (macOS probe, C2): deny and full."""
    export = qualification.run_host(
        [sys.executable, "-c", STUB_EXEC_HOST, mode], tmp_path / "export.json", scenario=scenario
    )
    assert export["status"] == "completed", export["stderr"]
    check = export["checks"]
    assert check["sandbox_lines"] == lines
    assert check["expected_sandbox_lines"] == lines
    assert check["gateway_tool_executions"] == 0
    assert [p["tool_name"] for p in check["proposed"]] == ["exec"]
    assert check["result_reached_next_run"] is True
    assert check["passed"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="the stub host runs a POSIX shell command")
def test_an_exec_denial_that_ran_anyway_does_not_pass(tmp_path):
    export = qualification.run_host(
        [sys.executable, "-c", STUB_EXEC_HOST, "full"],
        tmp_path / "export.json",
        scenario="exec-deny",
    )
    assert export["checks"]["sandbox_lines"] == 1
    assert export["checks"]["passed"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="the stub host runs a POSIX shell command")
def test_an_exec_denial_that_ran_in_another_workdir_does_not_pass(tmp_path):
    """RM08, AC24: the exec command names the sandbox file by absolute path.

    A host that ignored ``tools.exec.mode`` and ran the command outside the
    sandbox must still leave its line in the sandbox, so the deny never passes.
    """
    export = qualification.run_host(
        [sys.executable, "-c", STUB_EXEC_HOST, "full-elsewhere"],
        tmp_path / "export.json",
        scenario="exec-deny",
    )
    assert export["status"] == "completed", export["stderr"]
    assert export["checks"]["sandbox_lines"] == 1
    assert export["checks"]["passed"] is False


def test_the_exec_command_writes_to_the_sandbox_by_absolute_path(tmp_path):
    """The scripted exec proposal does not depend on the host's working directory."""
    sandbox = tmp_path / "host sandbox"
    command = qualification.exec_command(sandbox)
    assert command == f"echo written-by-the-host >> '{sandbox / 'note.txt'}'"


# A stand-in for a host that is interrupted mid-turn (#138): one streamed chat
# request that it never finishes reading; on SIGINT it exits 130, the way the
# pinned host did on the macOS probe, without a JSON report.
STUB_INTERRUPTED_HOST = """
import json, os, signal, sys, urllib.request
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
base = os.environ["RELAYMIND_BASE_URL"]
token = os.environ["RELAYMIND_APP_TOKEN"]
body = {"model": "relaymind-client-tools", "stream": True,
        "messages": [{"role": "user", "content": "Hi"}]}
request = urllib.request.Request(
    base + "/chat/completions", data=json.dumps(body).encode(), method="POST",
    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
opener.open(request).read()
print(json.dumps({"ok": True}))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the stub host handles POSIX SIGINT")
def test_the_abort_scenario_interrupts_the_host_mid_turn_and_checks_the_run(tmp_path):
    """RM14, RM33, AC12, AC24: SIGINT to the host closes its request; the run is cancelled."""
    export = qualification.run_host(
        [sys.executable, "-c", STUB_INTERRUPTED_HOST],
        tmp_path / "export.json",
        scenario="abort",
    )
    assert export["status"] == "interrupted", export["stderr"]
    assert export["exit_code"] == 130
    check = export["checks"]
    assert check["signal_sent"] is True
    (run,) = export["runs"]
    assert run["state"] == "cancelled"
    assert check["cancelled_runs"] == [run["run_id"]]
    assert check["answer_deltas"] == 0
    assert check["passed"] is True


def test_an_abort_scenario_whose_host_finishes_unsignalled_does_not_pass(tmp_path):
    """The host ended before the harness could interrupt it: nothing was cancelled."""
    export = qualification.run_host(
        [sys.executable, "-c", "print('done')"], tmp_path / "export.json", scenario="abort"
    )
    assert export["checks"]["signal_sent"] is False
    assert export["checks"]["passed"] is False


# A host that logs more than a pipe buffer (64 KiB) before its request, as a
# real host with verbose logging can (#138 review).
STUB_VERBOSE_INTERRUPTED_HOST = STUB_INTERRUPTED_HOST.replace(
    "signal.signal(signal.SIGINT, lambda *_: sys.exit(130))",
    "signal.signal(signal.SIGINT, lambda *_: sys.exit(130))\n"
    "sys.stderr.write('log line\\n' * 30000); sys.stderr.flush()\n"
    "sys.stdout.write('out line\\n' * 30000); sys.stdout.flush()",
)


@pytest.mark.skipif(sys.platform == "win32", reason="the stub host handles POSIX SIGINT")
def test_the_abort_scenario_reads_a_verbose_host_output_while_it_waits(tmp_path):
    """A host that writes more than a pipe holds before its request is still interrupted."""
    assert "log line" in STUB_VERBOSE_INTERRUPTED_HOST
    export = qualification.run_host(
        [sys.executable, "-c", STUB_VERBOSE_INTERRUPTED_HOST],
        tmp_path / "export.json",
        scenario="abort",
        timeout=30,
    )
    assert export["status"] == "interrupted", export["stderr"][-500:]
    assert export["checks"]["signal_sent"] is True
    assert export["checks"]["passed"] is True
    assert export["stderr"].endswith("log line\n")


# A host request the size of the pinned host's: a system prompt of about 30,000
# characters (macOS probe, fact 5) and 56 KB on the wire in the Linux VPS check.
# The doubles count one unit per character unless their count is fixed.
STUB_LARGE_INTERRUPTED_HOST = STUB_INTERRUPTED_HOST.replace(
    '"messages": [{"role": "user", "content": "Hi"}]',
    '"messages": [{"role": "system", "content": "x" * 40000}, {"role": "user", "content": "Hi"}]',
)


@pytest.mark.skipif(sys.platform == "win32", reason="the stub host handles POSIX SIGINT")
def test_the_abort_scenario_holds_a_host_sized_request_in_the_local_generation(tmp_path):
    """RM14, AC12: the real host's request must reach the held generation to be aborted.

    In the Linux VPS functional check the gated double counted the host's
    request by character, so it fit neither model's limit and the run failed
    with context_limit_exceeded before any generation started.
    """
    assert '"x" * 40000' in STUB_LARGE_INTERRUPTED_HOST
    export = qualification.run_host(
        [sys.executable, "-c", STUB_LARGE_INTERRUPTED_HOST],
        tmp_path / "export.json",
        scenario="abort",
        timeout=30,
    )
    assert export["status"] == "interrupted", export["stderr"][-500:]
    (run,) = export["runs"]
    assert run["state"] == "cancelled"
    assert export["checks"]["passed"] is True


# -- the host error scenario (#138; RM16, RM33, AC24, section 11) --------------------------

# A stand-in for a host that reports a gateway error the way the plugin's message
# does: "RelayMind <code> (HTTP <status>): ... Gateway run <id>, ..." (errors.ts).
# With "silent" it prints nothing about the error.
STUB_ERROR_HOST = """
import json, os, sys, urllib.error, urllib.request
base = os.environ["RELAYMIND_BASE_URL"]
token = os.environ["RELAYMIND_APP_TOKEN"]
echo = {"type": "function", "function": {"name": "relaymind_test_echo", "parameters": {
    "type": "object", "properties": {}}}}
body = {"model": "relaymind-client-tools", "tools": [echo],
        "messages": [{"role": "system", "content": "x" * 40000},
                     {"role": "user", "content": "Say hello."}]}
request = urllib.request.Request(
    base + "/chat/completions", data=json.dumps(body).encode(), method="POST",
    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    opener.open(request).read()
except urllib.error.HTTPError as error:
    detail = json.loads(error.read())["error"]
    if sys.argv[1:] != ["silent"]:
        sys.stderr.write(f"RelayMind {detail['code']} (HTTP {error.code}): failed. "
                         f"Gateway run {detail['run_id']}, reason {detail['reason_code']}.\\n")
    print(json.dumps({"ok": False, "status": "error"}))
    raise SystemExit(1)
print(json.dumps({"ok": True}))
"""

# A host that retries the same model after a server error, as the pinned host did
# in the Linux VPS functional check (7 same-model retries, each with a rewritten
# conversation, so each a new gateway run), and logs the plugin's attempt record.
STUB_RETRYING_ERROR_HOST = """
import json, os, sys, urllib.error, urllib.request
base = os.environ["RELAYMIND_BASE_URL"]
token = os.environ["RELAYMIND_APP_TOKEN"]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(8):
    body = {"model": "relaymind-client-tools",
            "messages": [{"role": "system", "content": "x" * 40000},
                         {"role": "user", "content": "Say hello." + " Continue." * attempt}]}
    request = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        opener.open(request).read()
    except urllib.error.HTTPError as error:
        detail = json.loads(error.read())["error"]
        record = {"event": "relaymind.inference_attempt", "attempt": 1,
                  "runId": detail["run_id"], "outcome": "failed", "status": error.code,
                  "code": detail["code"], "retried": False}
        sys.stderr.write("\\x1b[36mRelayMind inference attempt " + json.dumps(record)
                         + "\\x1b[39m\\n")
        last = (f"RelayMind {detail['code']} (HTTP {error.code}): failed. "
                f"Gateway run {detail['run_id']}.")
sys.stderr.write(last + "\\n")
raise SystemExit(1)
"""


def test_the_external_failed_scenario_checks_the_failed_run_and_the_host_message(tmp_path):
    """RM16, AC24, section 11: both models fail, the host gets 503 external_failed,
    and the message it shows names the gateway's failed run."""
    export = qualification.run_host(
        [sys.executable, "-c", STUB_ERROR_HOST],
        tmp_path / "export.json",
        scenario="external-failed",
        timeout=30,
    )
    assert export["status"] == "failed", export["stderr"][-500:]
    assert export["exit_code"] == 1
    check = export["checks"]
    (run,) = export["runs"]
    assert run["state"] == "failed"
    assert check["failed_runs"] == [
        {"run_id": run["run_id"], "error_code": "external_failed", "reason_code": "invalid_output"}
    ]
    assert check["expected"] == {"code": "external_failed", "status": 503}
    assert check["host_message"] is True
    assert check["named_runs"] == [run["run_id"]]
    assert check["answer_deltas"] == 0
    assert check["gateway_tool_executions"] == 0
    assert check["host_report"]["ok"] is False
    assert check["passed"] is True


def test_an_external_failed_scenario_without_the_host_message_does_not_pass(tmp_path):
    """The run failed, but the host never showed the plugin's named failure."""
    export = qualification.run_host(
        [sys.executable, "-c", STUB_ERROR_HOST, "silent"],
        tmp_path / "export.json",
        scenario="external-failed",
        timeout=30,
    )
    assert export["runs"][0]["state"] == "failed"
    assert export["checks"]["host_message"] is False
    assert export["checks"]["named_runs"] == []
    assert export["checks"]["passed"] is False


def test_an_external_failed_scenario_whose_host_answered_does_not_pass(tmp_path):
    """A host that got no error (here: no request at all) shows no failed run."""
    export = qualification.run_host(
        [sys.executable, "-c", "print('{}')"], tmp_path / "export.json", scenario="external-failed"
    )
    assert export["runs"] == []
    assert export["checks"]["passed"] is False


def test_the_main_entry_accepts_a_failed_host_for_the_error_scenario(tmp_path, monkeypatch):
    """The host's exit status is recorded, not judged: an error scenario's host fails."""

    def fake_run_host(command, out, *, timeout, scenario):
        return {"status": "failed", "runs": [{}], "checks": {"passed": True}}

    monkeypatch.setattr(qualification, "run_host", fake_run_host)
    argv = ["host", "--out", str(tmp_path / "x.json"), "--scenario", "external-failed"]
    assert qualification.main([*argv, "--", "true"]) == 0


def test_an_external_failed_scenario_survives_the_host_s_same_model_retries(tmp_path):
    """Every host retry is a new gateway run that fails the same way, never an
    unscripted double (internal_error); the plugin's attempt records name them."""
    export = qualification.run_host(
        [sys.executable, "-c", STUB_RETRYING_ERROR_HOST],
        tmp_path / "export.json",
        scenario="external-failed",
        timeout=60,
    )
    check = export["checks"]
    assert len(export["runs"]) == 8
    assert {f["error_code"] for f in check["failed_runs"]} == {"external_failed"}
    recorded = {run["run_id"] for run in export["runs"]}
    assert [a["status"] for a in check["attempts"]] == [503] * 8
    assert {a["runId"] for a in check["attempts"]} == recorded
    assert check["attempt_runs_recorded"] is True
    assert check["passed"] is True
