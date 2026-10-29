"""The OpenClaw evaluation runner over a stub host and doubles gateways (spec 0.50, #139).

RM12, RM16, RM25, RM26, RM33; AC24; section 12.5. ``scripts/run_openclaw_eval.py``
gives each arm its own isolated host home (and, for a RelayMind arm, its own
gateway), installs and configures the plugin, stores the token through the
host's credential command on standard input, and runs every task of the
frozen order with ``openclaw agent exec --json`` in a fresh sandbox. Here the
host is a stub that keeps the real host's command forms and observed
behavior (macOS probe, 27 October 2026: the decorated user message, the call
ID replayed without underscores, ``"ok": true`` whatever happened), the
gateway answers from model doubles, and the "external provider" of the
direct arm is a doubles gateway too. Nothing here is live evidence; the
measurement itself is an operator run.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from api_helpers import RunningServer, make_app, token_of
from gateway_helpers import build_gateway, text_turn, tool_turn
from helpers import ScriptedAdapter, load_script_module, make_capabilities
from relaymind.adapters import Role
from relaymind.api.app import ChatSettings
from relaymind.api.chat import ClientToolsChat
from relaymind.evaluation.openclaw import ARMS, load_tasks, sampling_order
from relaymind.orchestrator.local_loop import ConfiguredModel
from relaymind.orchestrator.local_status import LocalStatus
from relaymind.orchestrator.route_decision import ExecutionMode

runner = load_script_module("run_openclaw_eval")

REPO = Path(__file__).resolve().parent.parent
TASKS = load_tasks(REPO / "evaluation" / "openclaw_tasks.v1.json")
SELECTED = ("text-reply-en", "read-note-en", "exec-append-deny-zh")
EVIDENCE = "not live evidence: a stub host over gateway doubles"

# The host stand-in: config in OPENCLAW_CONFIG_PATH, the stored key under
# OPENCLAW_STATE_DIR, a log of every call (calls.jsonl beside the stub, so it
# outlives the runner's work directory; the token itself is never logged, only
# whether the environment held it and a hash of what standard input gave), and
# the plugin's diagnostics records written the way the plugin writes them.
STUB_HOST = r"""
import hashlib, json, os, re, subprocess, sys, urllib.request
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["OPENCLAW_STATE_DIR"])
state.mkdir(parents=True, exist_ok=True)
config_path = Path(os.environ["OPENCLAW_CONFIG_PATH"])
call = {"args": args, "home": os.environ["OPENCLAW_HOME"], "stdin_sha256": None}
if args[:3] == ["models", "auth", "paste-api-key"]:
    pasted = sys.stdin.readline().strip()
    call["stdin_sha256"] = hashlib.sha256(pasted.encode()).hexdigest()
    (state / "key").write_text(pasted)
stored = (state / "key").read_text() if (state / "key").exists() else None
call["env_token"] = "RELAYMIND_APP_TOKEN" in os.environ or bool(
    stored and any(stored in value for value in os.environ.values())
)
if "--state-dir" in args:
    exec_state = Path(args[args.index("--state-dir") + 1])
    call["state_dir"] = {"exists": exec_state.is_dir(),
                         "empty": exec_state.is_dir() and not any(exec_state.iterdir())}
with open(Path(__file__).with_name("calls.jsonl"), "a") as log:
    log.write(json.dumps(call) + "\n")

def load():
    return json.loads(config_path.read_text()) if config_path.exists() else {}

def save(config):
    config_path.write_text(json.dumps(config))

config = load()
entries = config.setdefault("plugins", {}).setdefault("entries", {})
if args == ["--version"]:
    print("OpenClaw 2026.10.7 (stub)")
elif args[:2] == ["plugins", "install"]:
    if "--accept-capabilities" not in args:
        raise SystemExit("requires capability consent")
    entries["relaymind"] = {"enabled": False}
    save(config)
elif args[:2] == ["plugins", "enable"]:
    entries.setdefault(args[2], {})["enabled"] = True
    save(config)
elif args[:3] == ["models", "auth", "paste-api-key"]:
    pass  # the key was read from standard input and stored above
elif args[:2] == ["agent", "exec"]:
    prompt = args[2]
    ref = args[args.index("--model") + 1]
    cwd = Path(args[args.index("--cwd") + 1])
    assert args[args.index("--config") + 1] == str(config_path)
    provider, model = ref.split("/", 1)
    headers = {"Content-Type": "application/json"}
    diagnostics = None
    if provider == "relaymind":
        plugin = entries["relaymind"]
        assert plugin["enabled"] is True
        base = plugin["config"]["baseUrl"]
        key = (state / "key").read_text()
        headers["X-RelayMind-Execution-Mode"] = plugin["config"]["executionMode"]
        diagnostics = plugin["config"].get("diagnosticsPath")
    else:
        entry = config["models"]["providers"][provider]
        base, key = entry["baseUrl"], entry["apiKey"]
        assert entry["models"][0]["id"] == model
    headers["Authorization"] = "Bearer " + key
    mode = config.get("tools", {}).get("exec", {}).get("mode")
    tools = [
        {"type": "function", "function": {"name": n, "description": n, "parameters": {
            "type": "object", "properties": {p: {"type": "string"}}}}}
        for n, p in (("read", "path"), ("exec", "command"), ("write", "path"))
    ]
    messages = [
        {"role": "system", "content": "You are OpenClaw (stub)."},
        {"role": "user",
         "content": "[Tue 2026-10-27 10:00 UTC] " + prompt + "\n\nRuntime: agent=main"},
    ]
    called, failures, final = [], 0, ""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for turn in range(4):
        body = {"model": model, "messages": messages, "stream": False, "tools": tools,
                "tool_choice": "auto"}
        request = urllib.request.Request(base + "/chat/completions",
                                         data=json.dumps(body).encode(), headers=headers)
        with opener.open(request, timeout=30) as response:
            run_id = response.headers.get("X-RelayMind-Run-ID")
            reply = json.loads(response.read())
        message = reply["choices"][0]["message"]
        if diagnostics:
            with open(diagnostics, "a") as out:
                out.write(json.dumps({"event": "relaymind.inference_attempt", "turn": "t",
                    "attempt": 1, "runId": run_id or "unavailable",
                    "outcome": "tool_call" if message.get("tool_calls") else "answer",
                    "status": 200, "code": None, "retried": False, "usage": "unknown"}) + "\n")
        if not message.get("tool_calls"):
            final = message.get("content") or ""
            break
        call = message["tool_calls"][0]
        name = call["function"]["name"]
        arguments = json.loads(call["function"]["arguments"])
        called.append(name)
        if name == "read":
            result = (cwd / arguments["path"]).read_text()
        elif name == "exec" and mode == "full":
            result = subprocess.run(arguments["command"], shell=True, cwd=cwd,
                                    capture_output=True, text=True).stdout or "(no output)"
        else:
            failures += 1
            result = json.dumps({"status": "error", "error": "exec denied"})
        replayed = re.sub("[^A-Za-z0-9]", "", call["id"])
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {**call, "id": replayed}]})
        messages.append({"role": "tool", "tool_call_id": replayed, "content": result})
    print(json.dumps({"ok": True, "status": "ok", "final": final,
        "usage": {"cacheRead": 0, "cacheWrite": 0}, "assistantTurns": len(called) + 1,
        "toolSummary": {"calls": len(called), "tools": called, "failures": failures},
        "provider": provider, "model": model}))
else:
    raise SystemExit("stub: unknown command " + " ".join(args))
"""


class TaskDouble(ScriptedAdapter):
    """A model double that answers each frozen task by what the request holds."""

    async def generate(self, request):
        self.requests.append(request)
        messages = request.messages
        results = [m for m in messages if m.role == Role.TOOL]
        user = next(m.content for m in messages if m.role == Role.USER)
        if results:
            text = results[-1].content or ""
            turn = text_turn(f"The tool said: {text.strip()} second shelf, lantern.")
        elif "notes.txt" in user:
            turn = tool_turn("read", call_id="call_read_1", path="notes.txt")
        elif "log.txt" in user:
            turn = tool_turn("exec", call_id="call_exec_1", command="echo ready >> log.txt")
        else:
            turn = text_turn("lantern")
        for item in turn:
            yield item


class DoublesBackend:
    """The runner's backend over in-process doubles gateways (spec section 13)."""

    MODES = frozenset({ExecutionMode.AUTO, ExecutionMode.EXTERNAL_ONLY})

    def __init__(self, tmp_path: Path, *, local: bool = True, local_request_tokens=None):
        self.tmp_path = tmp_path
        self.local = local
        # A host-sized request at the local model's 8192-token context (Q1: 10,500
        # to 10,577 tokens with the pinned tokenizer); None keeps the double's count.
        self.local_request_tokens = local_request_tokens
        self.provider = self._serve("provider", absent=True)
        # Each RelayMind arm's gateway token, so a test can follow where it went.
        self.tokens: dict[str, str] = {}

    def _serve(self, name: str, *, absent: bool = False):
        work = self.tmp_path / name
        work.mkdir()
        external = ConfiguredModel(adapter=TaskDouble(locality="external"), model_id="double-ext")
        options = (
            {"local_status": LocalStatus.absent()}
            if absent
            else {"local_model": ConfiguredModel(adapter=self._local(), model_id="double-local")}
        )
        gateway = build_gateway(work, None, external_model=external, **options)
        app = make_app(
            gateway,
            client_tools=ClientToolsChat(gateway.orchestrator),
            settings=ChatSettings(allowed_modes=self.MODES),
        )
        server = RunningServer(app)
        server.__enter__()
        return gateway, app, server

    def _local(self):
        if self.local_request_tokens is None:
            return TaskDouble()
        return TaskDouble(
            capabilities=make_capabilities(context_limit=8192),
            input_tokens=self.local_request_tokens,
        )

    def unavailable(self, arm: str) -> str | None:
        if arm == "relaymind_no_local":
            return "not part of this test"
        return None

    def direct(self):
        _gateway, app, server = self.provider
        return runner.DirectEndpoint(
            base_url=server.base_url, model="relaymind-client-tools", api_key=token_of(app)
        )

    def start(self, arm: str, work: Path):
        gateway, app, server = self._serve(arm)
        self.tokens[arm] = token_of(app)

        def stop():
            server.__exit__(None, None, None)
            gateway.store.close()

        return runner.GatewayHandle(
            base_url=server.base_url,
            token=token_of(app),
            run_ids=lambda: [
                r[0] for r in gateway.store._conn.execute("SELECT run_id FROM runs ORDER BY rowid")
            ],
            run_records=lambda ids: [
                {
                    "run_id": i,
                    "state": gateway.store.get_run(i).state,
                    "events": [e.to_json_object() for e in gateway.store.events(i)],
                }
                for i in ids
            ],
            stop=stop,
            launch={"arm": arm, "local_model": arm != "relaymind_no_local"},
        )

    def secrets(self):
        return [token_of(self.provider[1])]

    def identity(self):
        return {"backend": "doubles"}

    def close(self):
        gateway, _app, server = self.provider
        server.__exit__(None, None, None)
        gateway.store.close()


def host_calls(tmp_path: Path) -> list[dict]:
    """Every call the stub host logged, in order (calls.jsonl beside the stub)."""
    lines = (tmp_path / "calls.jsonl").read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines]


def arm_of(call: dict) -> str:
    """The arm a logged call ran in: OPENCLAW_HOME is <work>/<arm>/host/home."""
    return Path(call["home"]).parents[1].name


@pytest.fixture
def evaluated(tmp_path):
    host = tmp_path / "openclaw_stub.py"
    host.write_text(STUB_HOST, "utf-8")
    tarball = tmp_path / "relaymind-openclaw-provider-0.1.0.tgz"
    tarball.write_bytes(b"not a real tarball")
    backend = DoublesBackend(tmp_path)
    out = tmp_path / "out"
    try:
        document = runner.run_evaluation(
            TASKS,
            arms=ARMS,
            host_command=[sys.executable, str(host)],
            tarball=tarball,
            backend=backend,
            out=out,
            evidence=EVIDENCE,
            task_ids=SELECTED,
        )
    finally:
        backend.close()
    return document, out, backend


def test_every_arm_runs_the_frozen_order_and_an_unavailable_arm_is_not_run(evaluated):
    document, _out, _backend = evaluated
    assert document["kind"] == "relaymind.openclaw_evaluation"
    assert document["arms"]["relaymind_no_local"] == {
        "status": "not_run",
        "reason": "not part of this test",
    }
    ran = [a for a in ARMS if a != "relaymind_no_local"]
    assert all(document["arms"][a]["status"] == "ran" for a in ran)
    expected = [
        (s.index, s.repeat, s.task_id, s.arm)
        for s in sampling_order(TASKS)
        if s.task_id in SELECTED and s.arm in ran
    ]
    got = [(a["index"], a["repeat"], a["task_id"], a["arm"]) for a in document["attempts"]]
    assert got == expected
    assert len(got) == len(SELECTED) * TASKS.repeats * len(ran)
    assert document["tasks"]["sha256"] == TASKS.digest
    assert document["tasks"]["selected"] == list(SELECTED)
    assert document["tasks"]["full_set"] is False


def test_every_attempt_is_graded_from_the_host_sandbox_and_trace(evaluated):
    document, _out, _backend = evaluated
    for attempt, grade in zip(document["attempts"], document["grades"], strict=True):
        assert grade["arm"] == attempt["arm"] and grade["task_id"] == attempt["task_id"]
        assert grade["success"] is True, (attempt["arm"], attempt["task_id"], grade["failures"])
        if attempt["arm"] == "direct":
            assert attempt["runs"] == [] and grade["inference_attempts"] is None
        else:
            assert grade["gateway_runs"] >= 1
            assert grade["uncorrelated_runs"] == []
            assert grade["gateway_tool_executions"] == 0
    summary = document["summary"]["arms"]
    assert summary["relaymind_auto"]["languages"]["en"]["successes"] == 2 * TASKS.repeats
    # The execution mode reached the gateway: external_only never ran the local model.
    external_only = summary["relaymind_external_only"]["languages"]["en"]
    assert external_only["local_calls"] == 0 and external_only["external_calls"] > 0
    auto = summary["relaymind_auto"]["languages"]["en"]
    assert auto["local_calls"] > 0 and auto["local_turns"] > 0
    assert summary["direct"]["languages"]["en"]["external_calls"] is None
    # The host's tool runs reached the next request: a read task's runs end with its text.
    read = [
        a for a in document["attempts"] if a["task_id"] == "read-note-en" and a["arm"] != "direct"
    ]
    assert all(len(a["runs"]) == 2 for a in read)
    assert summary["relaymind_auto"]["languages"]["zh"]["attempts"] == TASKS.repeats


def test_each_task_runs_in_a_fresh_sandbox_under_its_frozen_policy(evaluated, tmp_path):
    document, _out, _backend = evaluated
    deny = [a for a in document["attempts"] if a["task_id"] == "exec-append-deny-zh"]
    assert deny and all("log.txt" not in a["sandbox_after"] for a in deny)
    reads = [a for a in document["attempts"] if a["task_id"] == "read-note-en"]
    assert all(a["sandbox_after"] == TASKS.task("read-note-en").sandbox for a in reads)
    assert all(
        a["exec_policy"] == TASKS.task(a["task_id"]).exec_policy for a in document["attempts"]
    )


def test_the_host_gets_the_frozen_prompt_and_the_token_only_on_standard_input(evaluated, tmp_path):
    """RM12, section 12.5: the gateway token reaches the host only on standard input.

    The stub host's own log (taken before the export is sanitized) shows the
    token in no argument and in no call's environment, and the store-token
    call of each RelayMind arm got that arm's token on standard input.
    """
    document, _out, backend = evaluated
    calls = host_calls(tmp_path)
    assert set(backend.tokens) == {"relaymind_auto", "relaymind_external_only"}
    secrets = [*backend.tokens.values(), *backend.secrets()]
    for call in calls:
        assert not any(secret in arg for secret in secrets for arg in call["args"]), call["args"]
        assert call["env_token"] is False, call["args"]
    for arm, token in backend.tokens.items():
        pasted = [
            c
            for c in calls
            if arm_of(c) == arm and c["args"][:3] == ["models", "auth", "paste-api-key"]
        ]
        assert len(pasted) == 1
        assert pasted[0]["stdin_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    # The export's host arguments needed no redaction: no secret was ever in them.
    for arm in ("relaymind_auto", "relaymind_external_only", "direct"):
        assert runner.NOT_RECORDED not in json.dumps(document["arms"][arm]["setup"])
    assert runner.NOT_RECORDED not in json.dumps([a["host"]["argv"] for a in document["attempts"]])
    for arm in ("relaymind_auto", "relaymind_external_only", "direct"):
        setup = document["arms"][arm]["setup"]
        names = [step["name"] for step in setup]
        if arm == "direct":
            assert names == ["version", "configure"]
        else:
            assert names == ["version", "install", "configure", "enable", "store-token"]
            install = next(s for s in setup if s["name"] == "install")
            assert "--accept-capabilities" in install["argv"]
        assert all(step["exit_code"] == 0 for step in setup)
    for attempt in document["attempts"]:
        argv = attempt["host"]["argv"]
        assert argv[:3] == ["agent", "exec", TASKS.task(attempt["task_id"]).prompt]
        assert "--json" in argv


def test_every_attempt_runs_with_a_fresh_state_dir_in_the_arms_host_home(evaluated, tmp_path):
    """Section 12.5: each agent exec gets --state-dir on a fresh, empty directory.

    Without it the pinned host deletes a temporary state directory after each
    turn, which fails on Windows (EBUSY) and ends every turn ok: false; the
    install check gives each exchange its own directory the same way.
    """
    document, _out, _backend = evaluated
    execs = [c for c in host_calls(tmp_path) if c["args"][:2] == ["agent", "exec"]]
    assert len(execs) == len(document["attempts"])
    dirs = []
    for call in execs:
        args = call["args"]
        assert "--state-dir" in args
        state_dir = Path(args[args.index("--state-dir") + 1])
        host_home = Path(call["home"]).parent
        assert state_dir.is_relative_to(host_home) and state_dir != host_home
        assert call["state_dir"] == {"exists": True, "empty": True}
        dirs.append(state_dir)
    assert len(set(dirs)) == len(dirs)
    for attempt in document["attempts"]:
        argv = attempt["host"]["argv"]
        assert argv[argv.index("--state-dir") + 1].startswith(runner.HOST_HOME)


def test_neither_secret_nor_a_host_home_path_reaches_the_exports(evaluated, tmp_path):
    document, out, backend = evaluated
    export = (out / "openclaw-eval.json").read_text("utf-8")
    report = (out / "openclaw-eval.md").read_text("utf-8")
    for secret in backend.secrets():
        assert secret not in export and secret not in report
    for attempt in document["attempts"]:
        if attempt["arm"] != "direct":
            assert attempt["runs"]
    assert str(tmp_path) not in export
    assert json.loads(export)["evidence"].startswith("not live")


def test_a_relative_place_is_replaced_by_its_absolute_forms_only(tmp_path, monkeypatch):
    """A relative path given to the runner (``--tarball x.tgz``, ``-- ./openclaw.mjs``)
    has parent ``.``: replacing that text would rewrite every period of the export.
    A place is replaced in its absolute forms, raw and JSON-escaped (on Windows the
    export holds ``C:\\\\Users``); the working directory itself names nothing."""
    monkeypatch.chdir(tmp_path)
    document = {"version": "3.14", "file": "tasks.v1.json", "at": "2026-10-29T10:00:00"}
    text = json.dumps(document)
    assert runner._sanitize(text, [], [(Path("x.tgz").parent, "<artifact-dir>")]) == text
    # A backslash in a POSIX name stands in for a Windows separator: JSON escapes it.
    odd = tmp_path / "rel\\dir"
    odd.mkdir()
    text = json.dumps({"log": f"wrote {odd.resolve() / 'host.log'}"})
    cleaned = runner._sanitize(text, [], [(Path("rel\\dir"), "<dir>")])
    assert json.loads(cleaned) == {"log": f"wrote <dir>{os.sep}host.log"}


def test_the_host_commands_directory_is_found_with_either_slash(monkeypatch):
    """``node integrations/openclaw/.../openclaw.mjs`` names a directory on Windows
    too, where the separator is a backslash but Path also reads a forward slash."""
    monkeypatch.setattr(runner.os, "sep", "\\")
    command = ["node", "integrations/openclaw/node_modules/openclaw/openclaw.mjs"]
    assert runner._command_dirs(command) == [Path("integrations/openclaw/node_modules/openclaw")]
    assert runner._command_dirs(["openclaw", "--flag"]) == []


def test_the_plugin_is_configured_with_the_arms_mode_and_its_diagnostics(evaluated):
    document, _out, _backend = evaluated
    assert document["arms"]["relaymind_auto"]["plugin_config"]["executionMode"] == "auto"
    assert (
        document["arms"]["relaymind_external_only"]["plugin_config"]["executionMode"]
        == "external_only"
    )
    for arm in ("relaymind_auto", "relaymind_external_only"):
        config = document["arms"][arm]["plugin_config"]
        # The plugin gets the host's native path; the export scrubs its home prefix.
        assert config["diagnosticsPath"] == f"<host-home>{os.sep}diagnostics.jsonl"
        assert config["baseUrl"].startswith("http://127.0.0.1:")


def test_the_markdown_report_is_written_next_to_the_export(evaluated):
    _document, out, _backend = evaluated
    report = (out / "openclaw-eval.md").read_text("utf-8")
    assert "relaymind_no_local" in report and "not_run" in report
    assert "Linux" in report or "machine" in report.lower()


def run_with(tmp_path, backend, **options):
    host = tmp_path / "openclaw_stub.py"
    host.write_text(STUB_HOST, "utf-8")
    tarball = tmp_path / "relaymind-openclaw-provider-0.1.0.tgz"
    tarball.write_bytes(b"not a real tarball")
    out = tmp_path / "out"
    try:
        document = runner.run_evaluation(
            TASKS,
            host_command=[sys.executable, str(host)],
            tarball=tarball,
            backend=backend,
            out=out,
            evidence=EVIDENCE,
            **options,
        )
    finally:
        backend.close()
    return document, out


def test_auto_at_the_8192_local_context_reports_zero_local_turns_as_a_result(tmp_path):
    """D1 and the macOS qualification (Q1, Q2): the local context stays 8192.

    A host-sized request (10,510 tokens, the live AC24 turn's count) is over the
    local limit, so every auto turn goes to the external model. The attempts
    still pass, and the export and report show 0 local turns with the reason,
    context_limit, never a blank or a dropped arm.
    """
    backend = DoublesBackend(tmp_path, local_request_tokens=10510)
    document, out = run_with(
        tmp_path,
        backend,
        arms=["relaymind_external_only", "relaymind_auto"],
        task_ids=["text-reply-en", "read-note-en"],
    )
    auto = document["summary"]["arms"]["relaymind_auto"]["languages"]["en"]
    assert auto["successes"] == auto["attempts"] == 2 * TASKS.repeats
    assert auto["local_turns"] == 0 and auto["local_calls"] == 0
    assert auto["external_calls"] == auto["gateway_runs"] > 0
    assert auto["escalation_reasons"] == {"context_limit": auto["gateway_runs"]}
    assert auto["needs_external"] == 0
    external_only = document["summary"]["arms"]["relaymind_external_only"]["languages"]["en"]
    assert external_only["escalation_reasons"] == {"execution_mode": external_only["gateway_runs"]}
    comparison = document["summary"]["comparisons"]["local_inference_auto"]["languages"]["en"]
    assert comparison["local_turns"] == 0
    report = (out / "openclaw-eval.md").read_text("utf-8")
    assert f"relaymind_auto, en: 0 local turns of {auto['gateway_runs']} gateway runs" in report
    assert "compares no local inference" in report


def test_an_operator_interrupt_still_writes_the_export_marked_aborted(tmp_path, monkeypatch):
    """Spec 0.50: the attempts that ended are kept; the rest are not run, never failures."""
    real = runner.run_attempt
    calls = []

    def interrupted(slot, *args, **kwargs):
        calls.append(slot)
        if len(calls) == 3:
            raise KeyboardInterrupt
        return real(slot, *args, **kwargs)

    monkeypatch.setattr(runner, "run_attempt", interrupted)
    backend = DoublesBackend(tmp_path)
    document, out = run_with(
        tmp_path, backend, arms=["relaymind_external_only"], task_ids=["text-reply-en"]
    )
    stopped = calls[2]
    assert document["aborted"] == {
        "stage": "attempt",
        "index": stopped.index,
        "repeat": stopped.repeat,
        "task_id": stopped.task_id,
        "arm": stopped.arm,
        "slots_not_run": 1,
    }
    assert len(document["attempts"]) == len(document["grades"]) == 2
    assert all(g["success"] for g in document["grades"])
    summary = document["summary"]["arms"]["relaymind_external_only"]["languages"]["en"]
    assert summary["attempts"] == 2 and summary["host_interrupts"] == 0
    saved = json.loads((out / "openclaw-eval.json").read_text("utf-8"))
    assert saved["aborted"] == document["aborted"]
    report = (out / "openclaw-eval.md").read_text("utf-8")
    assert "Aborted by the operator at slot" in report
    assert "1 slot(s) not run" in report


def test_an_interrupt_during_arm_setup_still_writes_the_export_marked_aborted(tmp_path):
    """Spec 0.50: Ctrl-C while a gateway starts or the plugin is set up keeps the record.

    The arm set up before is kept with its setup steps; the arm whose setup was
    stopped and every slot are not run, never failures.
    """

    class InterruptedStart(DoublesBackend):
        def start(self, arm, work):
            if arm == "relaymind_auto":
                raise KeyboardInterrupt
            return super().start(arm, work)

    backend = InterruptedStart(tmp_path)
    document, out = run_with(
        tmp_path,
        backend,
        arms=["relaymind_external_only", "relaymind_auto"],
        task_ids=["text-reply-en"],
    )
    planned = [
        s
        for s in sampling_order(TASKS)
        if s.task_id == "text-reply-en" and s.arm in {"relaymind_external_only", "relaymind_auto"}
    ]
    assert document["aborted"] == {
        "stage": "setup",
        "index": None,
        "repeat": None,
        "task_id": None,
        "arm": "relaymind_auto",
        "slots_not_run": len(planned),
    }
    assert document["attempts"] == [] and document["grades"] == []
    arms = document["arms"]
    assert arms["relaymind_external_only"]["status"] == "ran"
    assert arms["relaymind_external_only"]["setup"]
    assert arms["relaymind_auto"]["status"] == "not_run"
    assert "interrupted" in arms["relaymind_auto"]["reason"]
    saved = json.loads((out / "openclaw-eval.json").read_text("utf-8"))
    assert saved["aborted"] == document["aborted"]
    report = (out / "openclaw-eval.md").read_text("utf-8")
    assert "Aborted by the operator during the setup of relaymind_auto" in report
    assert f"{len(planned)} slot(s) not run" in report


def test_an_interrupt_in_an_early_setup_leaves_every_later_arm_not_run(tmp_path):
    """Spec 0.50 section 12.5: arms never set up are not_run, never "ran".

    Ctrl-C in the setup of the second of four arms: the first arm keeps its
    setup, the interrupted arm and the two after it are not_run (export,
    summary, and report) with every planned slot not run, and no attempt ran.
    """

    class InterruptedSecond(DoublesBackend):
        def unavailable(self, arm):
            return None

        def start(self, arm, work):
            if arm == "relaymind_external_only":
                raise KeyboardInterrupt
            return super().start(arm, work)

    backend = InterruptedSecond(tmp_path)
    document, out = run_with(tmp_path, backend, arms=list(ARMS), task_ids=["text-reply-en"])
    planned = [s for s in sampling_order(TASKS) if s.task_id == "text-reply-en"]
    assert document["aborted"] == {
        "stage": "setup",
        "index": None,
        "repeat": None,
        "task_id": None,
        "arm": "relaymind_external_only",
        "slots_not_run": len(planned),
    }
    assert document["attempts"] == [] and document["grades"] == []
    arms = document["arms"]
    assert arms["direct"]["status"] == "ran" and arms["direct"]["setup"]
    report = (out / "openclaw-eval.md").read_text("utf-8")
    # Set up, no attempt ran: its slots are counted not run, and the report keeps its row.
    assert "| direct | all | 0/0: no attempt ended |" in report
    later = ("relaymind_auto", "relaymind_no_local")
    for arm in ("relaymind_external_only", *later):
        assert arms[arm]["status"] == "not_run", arm
        assert "interrupted by the operator" in arms[arm]["reason"]
        assert arms[arm]["slots_not_run"] == sum(1 for s in planned if s.arm == arm)
        assert document["summary"]["arms"][arm] == {
            "status": "not_run",
            "reason": arms[arm]["reason"],
            "languages": {},
        }
        assert f"| {arm} | all | not_run: {arms[arm]['reason']} |" in report
    for arm in later:
        assert arms[arm]["reason"] == "setup interrupted by the operator before this arm"
    saved = json.loads((out / "openclaw-eval.json").read_text("utf-8"))
    assert saved["arms"] == arms


def test_an_interrupt_while_grading_keeps_the_attempt_that_ended_graded(tmp_path, monkeypatch):
    """Spec 0.50: the attempts that ended are graded, even when Ctrl-C hit the grading."""
    real = runner.grade_attempt
    calls = []

    def interrupted(task, attempt):
        calls.append(attempt)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(task, attempt)

    monkeypatch.setattr(runner, "grade_attempt", interrupted)
    backend = DoublesBackend(tmp_path)
    document, _out = run_with(
        tmp_path, backend, arms=["relaymind_external_only"], task_ids=["text-reply-en"]
    )
    slots = [
        s
        for s in sampling_order(TASKS)
        if s.task_id == "text-reply-en" and s.arm == "relaymind_external_only"
    ]
    assert len(document["attempts"]) == len(document["grades"]) == 2
    assert all(g["success"] for g in document["grades"])
    stopped = slots[2]
    assert document["aborted"] == {
        "stage": "attempt",
        "index": stopped.index,
        "repeat": stopped.repeat,
        "task_id": stopped.task_id,
        "arm": stopped.arm,
        "slots_not_run": len(slots) - 2,
    }


def test_a_run_without_an_abort_says_so(evaluated):
    document, _out, _backend = evaluated
    assert document["aborted"] is None


def test_the_cli_exits_130_after_an_aborted_evaluation(tmp_path, monkeypatch):
    class Harness:
        ExternalEndpoint = None

    monkeypatch.setitem(sys.modules, "live_acceptance", Harness)
    out = tmp_path / "out"
    out.mkdir()
    (out / "openclaw-eval.md").write_text("report\n", "utf-8")
    monkeypatch.setattr(
        runner,
        "run_evaluation",
        lambda *a, **k: {"grades": [{}], "aborted": {"index": 3}},
    )
    argv = ["--no-local-model", "--tarball", str(tmp_path / "x.tgz"), "--out", str(out)]
    assert runner.main([*argv, "--", "openclaw"]) == 130
    monkeypatch.setattr(runner, "run_evaluation", lambda *a, **k: {"grades": [{}], "aborted": None})
    assert runner.main([*argv, "--", "openclaw"]) == 0


def test_without_an_external_endpoint_or_a_local_model_the_arms_are_not_run():
    reasons = runner.arm_availability(external=None, local_model=False, key_present=False)
    assert set(reasons) == set(ARMS)
    assert all(reason for reason in reasons.values())
    reasons = runner.arm_availability(external=object(), local_model=False, key_present=True)
    assert set(reasons) == {"relaymind_auto"}
    assert runner.arm_availability(external=object(), local_model=True, key_present=True) == {}


def test_relaymind_auto_without_the_external_model_is_not_run():
    """Section 12.5: the auto arm's gateway has the same external model and the local model.

    With only the local model, a gateway would be local-only, which is not the
    spec's auto arm, so the arm is not_run with the missing external model as
    its reason, never "ran" with zero successes.
    """
    for external, key in ((None, False), (object(), False), (None, True)):
        reasons = runner.arm_availability(external=external, local_model=True, key_present=key)
        assert set(reasons) == set(ARMS)
        assert runner.NO_EXTERNAL in reasons["relaymind_auto"]
    both = runner.arm_availability(external=None, local_model=False, key_present=False)
    assert runner.NO_EXTERNAL in both["relaymind_auto"]
    assert runner.NO_LOCAL in both["relaymind_auto"]


def test_the_export_records_the_node_identity(evaluated):
    """Section 12.5: the export holds the host, Node, plugin, and model identities."""
    document, _out, _backend = evaluated
    node = document["node"]
    assert set(node) == {"executable", "version"}
    if node["executable"] is not None:
        assert node["version"] and node["version"].startswith("v")


def test_the_node_identity_is_the_node_that_runs_the_host(tmp_path, monkeypatch):
    """Section 12.5: the export names the Node that runs the host and its version.

    The fake node is only a file named as the platform names Node (node.exe on
    Windows); its --version call is answered here, so the test runs the same on
    every platform without a script interpreter for the fake.
    """
    fake = tmp_path / "bin" / ("node.exe" if os.name == "nt" else "node")
    fake.parent.mkdir()
    fake.write_text("", "utf-8")
    fake.chmod(0o755)
    env = {"PATH": str(fake.parent)}
    calls = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, stdout="v26.12.1\n", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", run)
    # The host started by an explicit node binary, and through a shebang on PATH.
    explicit = runner.node_identity([str(fake), "openclaw.mjs"], env)
    assert explicit == {"executable": str(fake), "version": "v26.12.1"}
    on_path = runner.node_identity(["node_modules/.bin/openclaw"], env)
    assert Path(on_path["executable"]) == fake and on_path["version"] == "v26.12.1"
    assert calls == [([str(fake), "--version"], env)] + [
        ([on_path["executable"], "--version"], env)
    ]
    assert runner.node_identity(["openclaw"], {"PATH": str(tmp_path / "none")}) == {
        "executable": None,
        "version": None,
    }

    def fails(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no")

    monkeypatch.setattr(runner.subprocess, "run", fails)
    assert runner.node_identity([str(fake)], env) == {"executable": str(fake), "version": None}


def test_the_direct_host_reaches_the_provider_through_the_operators_proxy(tmp_path, monkeypatch):
    """The direct arm and the gateways reach the external provider the same way.

    A gateway inherits the operator's environment, proxies included; the direct
    arm's host is the one that calls the provider, so it keeps the proxy
    variables too. A RelayMind arm's host talks only to the loopback gateway and
    keeps none. Loopback never goes through a proxy.
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("NO_PROXY", "corp.invalid")
    direct = runner.Host(["openclaw"], tmp_path / "direct", provider_network=True)
    plugin = runner.Host(["openclaw"], tmp_path / "plugin")
    assert direct.env["HTTPS_PROXY"] == "http://proxy.invalid:3128"
    assert {"127.0.0.1", "localhost", "corp.invalid"} <= set(direct.env["NO_PROXY"].split(","))
    assert "HTTPS_PROXY" not in plugin.env
    assert plugin.env["NO_PROXY"] == "127.0.0.1,localhost,::1"


def test_a_host_command_the_system_cannot_start_is_not_run(tmp_path, monkeypatch):
    """Any start failure is not_run, never a crash of the runner: a missing
    command, a file without execute permission, or another OSError (on Windows,
    WinError 193 for a shim that is not an executable)."""
    missing = runner.Host([str(tmp_path / "absent")], tmp_path / "a").run(["--version"], timeout=5)
    assert missing["status"] == "not_run" and missing["exit_code"] is None
    plain = tmp_path / "openclaw"
    plain.write_text("#!/bin/sh\n", "utf-8")
    plain.chmod(0o644)
    denied = runner.Host([str(plain)], tmp_path / "b").run(["--version"], timeout=5)
    assert denied["status"] == "not_run" and denied["exit_code"] is None

    def refuses(*args, **kwargs):
        raise OSError(193, "%1 is not a valid Win32 application")

    monkeypatch.setattr(runner.subprocess, "run", refuses)
    other = runner.Host(["openclaw.cmd"], tmp_path / "c").run(["--version"], timeout=5)
    assert other["status"] == "not_run"
    assert "not a valid Win32 application" in other["stderr"]


def test_the_gateway_store_opens_read_only_whatever_its_path_holds(tmp_path):
    """The store is opened by its file URI, so a path with URI characters (or,
    on Windows, a drive and backslashes) names the file, read-only."""
    store = tmp_path / "a#b?c%20d" / "runs.sqlite3"
    store.parent.mkdir()
    with sqlite3.connect(store) as db:
        db.execute("CREATE TABLE runs (run_id TEXT)")
        db.execute("INSERT INTO runs VALUES ('run_1')")
    db.close()
    conn = runner._read_only(store)
    try:
        assert conn.execute("SELECT run_id FROM runs").fetchall() == [("run_1",)]
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO runs VALUES ('run_2')")
    finally:
        conn.close()


def test_the_cli_refuses_a_task_file_that_differs_from_its_lock(tmp_path, capsys):
    changed = json.loads((REPO / "evaluation" / "openclaw_tasks.v1.json").read_text("utf-8"))
    changed["tasks"][0]["prompt"] += "!"
    tasks = tmp_path / "openclaw_tasks.v1.json"
    tasks.write_text(json.dumps(changed), "utf-8")
    with pytest.raises(SystemExit):
        runner.main(
            [
                "--tasks",
                str(tasks),
                "--lock",
                str(REPO / "evaluation" / "openclaw_tasks.v1.lock.json"),
                "--tarball",
                str(tmp_path / "x.tgz"),
                "--out",
                str(tmp_path / "out"),
                "--",
                "openclaw",
            ]
        )
    assert "new version" in capsys.readouterr().err
