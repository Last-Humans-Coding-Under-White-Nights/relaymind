"""OpenClaw host qualification harness (RM33, AC24, #135).

A disposable harness, not product code. It serves the real gateway on a
loopback socket over scripted model doubles (spec section 13; the doubles
are the ones in ``tests/``) and points an OpenClaw transport at it.

Two modes:

``fixtures``
    Runs every scenario in ``SCENARIOS`` through the pinned host
    transport (``integrations/openclaw/test/host-transport/drive.mjs``),
    one fresh gateway per scenario, and writes one sanitized fixture per
    scenario plus ``VERSIONS.json``. The fixtures are deterministic: run
    IDs, ports, dates, and machine-dependent SDK headers are replaced by
    placeholders, so a second capture on the same pins gives the same
    files (``tests/test_openclaw_qualification_script.py`` checks the
    committed set).

``host``
    Starts one gateway with the scripted turns of ``--scenario``, runs an
    operator command (for example a real ``openclaw agent exec``) with
    ``RELAYMIND_BASE_URL``, ``RELAYMIND_APP_TOKEN``, and
    ``RELAYMIND_TEST_SANDBOX`` (a fresh empty directory) in its environment
    only, and writes one export: the command's exit status (``timed_out``
    past ``--timeout``), every run the gateway recorded with its trace
    events, the versions, and the scenario's checks. The token is never
    written. This mode needs a real OpenClaw install; it is run by an
    operator and CI, not by the deterministic suite.

    Scenarios (#137): ``text-tool`` (the default, in one host turn: the
    sandbox echo call, then a final text turn); ``exec-deny`` and ``exec-full`` (a call
    to the host's own ``exec`` tool that appends one line to the sandbox's
    ``note.txt`` by absolute path, so the host's working directory cannot hide
    a run, then a final text turn), for the host's ``tools.exec.mode`` ``deny``
    and ``full``, the headless policy pair the macOS probe observed;
    ``tool-deny`` and ``tool-approve`` (a call to the mutating sandbox tool
    ``relaymind_test_write``, then a final text turn), which need an
    approval surface: headless ``agent exec`` has none, and an approval
    there fails closed. The host's own policy decides; the export checks,
    from the sandbox and the trace, that a denial wrote nothing, that an
    allowed call wrote exactly one line, that the gateway executed no tool
    and proposed the call, and that the host's result reached the next run,
    matching the call ID the way the host rewrites it
    (``host_replayed_call_id``). The host's exit status and its own JSON
    report (``host_report``: it says ``"ok": true`` for a denied tool) never
    decide a pass. A scenario that did not complete never passes.

    ``abort`` (#138): the local double holds its generation open, and once
    the gateway's generation has started the harness interrupts the host
    (SIGINT, or CTRL_BREAK_EVENT on Windows), as an operator's Ctrl-C would.
    It passes when every recorded run ended cancelled with cancel_requested,
    the adapter was asked to cancel, and no answer text was recorded; the
    export's status is ``interrupted``. The host's exit code is recorded, not
    judged (130 without a JSON report on the macOS probe).

Usage:
    python scripts/openclaw_qualification.py fixtures \\
        --out-dir integrations/openclaw/test/fixtures/v2026.10.7/transport
    python scripts/openclaw_qualification.py host --out <export.json> \\
        [--scenario text-tool|exec-deny|exec-full|tool-deny|tool-approve|abort] \\
        -- <command> [args...]

Deterministic transport fixtures are not live evidence (section 12.5).
"""

import argparse
import asyncio
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from api_helpers import RunningServer, make_app, token_of  # noqa: E402
from gateway_helpers import build_gateway, text_turn, tool_turn  # noqa: E402
from helpers import make_capabilities  # noqa: E402
from native_helpers import GatedAdapter  # noqa: E402
from relaymind.api.chat import ClientToolsChat  # noqa: E402
from relaymind.evaluation.environment import application_revision  # noqa: E402
from relaymind.orchestrator.local_loop import ConfiguredModel  # noqa: E402

PACKAGE = ROOT / "integrations" / "openclaw"
DRIVER = PACKAGE / "test" / "host-transport" / "drive.mjs"
FIXTURE_VERSION = 1
NOT_RECORDED = "<not recorded>"
SYSTEM_PROMPT = "You are a test host. Use relaymind_test_echo when asked to echo."
ECHO_PROMPT = "Echo the word lantern with the tool."
LONG_PROMPT = "Say hello. " * 20
# SDK headers whose value depends on the machine, not on the pins.
ENVIRONMENT_HEADERS = ("x-stainless-os", "x-stainless-arch", "x-stainless-runtime-version")


def echo_call(call_id: str = "call_echo_1", text: str = "lantern") -> list:
    return tool_turn("relaymind_test_echo", call_id=call_id, text=text)


@dataclass(frozen=True)
class Scenario:
    """One qualification exchange: the gateway's doubles and the driver's input."""

    name: str
    covers: str
    drive: Mapping[str, Any]
    turns: Callable[[], list] = list
    external_turns: Callable[[], list | None] = list
    gateway: Mapping[str, Any] = field(default_factory=dict)
    count_characters: bool = False
    gated: bool = False
    token: str | None = None


RUNTIME_CONTEXT_HISTORY = [
    {"role": "user", "content": "First question."},
    {
        "role": "assistant",
        "content": [{"type": "text", "text": "First answer."}],
        "api": "openai-completions",
        "provider": "relaymind",
        "model": "relaymind-client-tools",
        "stopReason": "stop",
    },
    {
        "role": "user",
        "content": "Runtime context: the cwd is /tmp.",
        "runtimeContext": {"retained": True},
    },
]

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "host-default-loopback",
        "stream framing: the host defaults for a loopback URL, no switch declared",
        {"compat": "host-default", "prompt": "Say hello."},
        turns=lambda: [text_turn("unused")],
    ),
    Scenario(
        "host-default-reasoning",
        "text: the host defaults with reasoning on and a runtime context message",
        {
            "compat": "host-default",
            "reasoning": True,
            "prompt": "Second question.",
            "history": RUNTIME_CONTEXT_HISTORY,
        },
        turns=lambda: [text_turn("unused")],
    ),
    Scenario(
        "developer-switch-on",
        "text: the developer switch on puts runtime context after the first user message",
        {
            "compat": {"supportsDeveloperRole": True},
            "prompt": "Second question.",
            "history": RUNTIME_CONTEXT_HISTORY,
        },
        turns=lambda: [text_turn("unused")],
    ),
    Scenario(
        "advertised-window-host-refusal",
        "context limits: the host refuses a request over the advertised window",
        {"prompt": LONG_PROMPT * 5, "contextWindow": 100, "maxTokens": 50},
        turns=lambda: [text_turn("unused")],
    ),
    Scenario(
        "text-streamed",
        "text: a streamed answer with the declared switches",
        {"prompt": "Say hello."},
        turns=lambda: [text_turn("Hello ", "there.")],
    ),
    Scenario(
        "text-buffered",
        "text: a non-streamed answer",
        {"prompt": "Say hello.", "stream": False},
        turns=lambda: [text_turn("Hello.")],
    ),
    Scenario(
        "token-limit",
        "context limits: the configured output limit as max_completion_tokens",
        {"prompt": "Say hello.", "maxTokens": 1024, "contextWindow": 8192},
        turns=lambda: [text_turn("Hello.")],
    ),
    Scenario(
        "runtime-context",
        "text: a runtime context message after the first user message",
        {"prompt": "Second question.", "history": RUNTIME_CONTEXT_HISTORY},
        turns=lambda: [text_turn("Done.")],
    ),
    Scenario(
        "tool-round-trip",
        "tools: one host tool call, executed by the host, and its result",
        {"prompt": ECHO_PROMPT},
        turns=lambda: [echo_call(), text_turn("The tool said: echo: lantern.")],
    ),
    Scenario(
        "operator-parallel-tool-calls",
        "tools: an operator parallel_tool_calls true stays refused",
        {"prompt": "Say hello.", "extraBody": {"parallel_tool_calls": True}},
        turns=lambda: [text_turn("unused")],
    ),
    Scenario(
        "oversized-auto",
        "context limits: over the local limit in auto goes external",
        {"prompt": LONG_PROMPT},
        external_turns=lambda: [text_turn("External hello.")],
        gateway={"capabilities": make_capabilities(context_limit=50)},
        count_characters=True,
    ),
    Scenario(
        "oversized-local-only",
        "context limits: over the local limit in local_only is needs_external",
        {"prompt": LONG_PROMPT, "executionMode": "local_only"},
        gateway={"capabilities": make_capabilities(context_limit=50)},
        count_characters=True,
    ),
    Scenario(
        "oversized-both",
        "context limits and errors: over both limits is context_limit_exceeded",
        {"prompt": LONG_PROMPT},
        gateway={
            "capabilities": make_capabilities(context_limit=50),
            "external_capabilities": make_capabilities(context_limit=60),
        },
        count_characters=True,
    ),
    Scenario(
        "external-only",
        "modes: the execution mode header reaches the gateway",
        {"prompt": "Say hello.", "executionMode": "external_only"},
        external_turns=lambda: [text_turn("External.")],
    ),
    Scenario(
        "stale-token",
        "auth: a token from an earlier gateway launch is 401",
        {"prompt": "Say hello."},
        turns=lambda: [text_turn("unused")],
        token="stale-token-from-an-earlier-launch-0000000000",
    ),
    Scenario(
        "abort-buffered",
        "cancellation: the host abort signal during a buffered generation",
        {"prompt": "Say hello.", "abortAfterMs": 1500},
        gated=True,
    ),
)


def node_binary() -> str:
    node = shutil.which("node")
    if node is None:
        raise SystemExit(
            "node is not on PATH (the pinned host needs Node >=24.22.1 <25 || >=26.12.1)"
        )
    return node


def _gateway(scenario: Scenario, work: Path):
    if scenario.gated:
        adapter = GatedAdapter(turns=[text_turn("never")])
        gateway = build_gateway(
            work,
            None,
            local_model=ConfiguredModel(adapter=adapter, model_id="gated"),
            external_turns=[],
        )
    else:
        gateway = build_gateway(
            work,
            scenario.turns(),
            external_turns=scenario.external_turns(),
            **scenario.gateway,
        )
        if scenario.count_characters:
            gateway.adapter.input_tokens = None
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
    return gateway, app


def child_environment(base_url: str, token: str) -> dict[str, str]:
    """The child's environment: the gateway URL and token, and no proxy for loopback."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in {"HTTP_PROXY", "HTTPS_PROXY"}}
    env.update(
        {
            "RELAYMIND_BASE_URL": base_url,
            "RELAYMIND_APP_TOKEN": token,
            "NO_PROXY": "127.0.0.1,localhost,::1",
        }
    )
    return env


def run_driver(
    scenario: Scenario, work: Path, *, driver: Path = DRIVER, node: str | None = None
) -> dict:
    """Run one scenario against a fresh gateway; return the sanitized fixture."""
    work.mkdir(parents=True, exist_ok=True)
    gateway, app = _gateway(scenario, work)
    try:
        with RunningServer(app) as running:
            drive = {"systemPrompt": SYSTEM_PROMPT, **scenario.drive}
            scenario_path = work / "scenario.json"
            out_path = work / "drive.out.json"
            scenario_path.write_text(json.dumps(drive), "utf-8")
            token = scenario.token or token_of(app)
            completed = subprocess.run(
                [node or node_binary(), str(driver), str(scenario_path), str(out_path)],
                cwd=PACKAGE,
                env=child_environment(running.base_url, token),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if completed.returncode != 0:
                raise RuntimeError(f"{scenario.name}: driver failed: {completed.stderr[-2000:]}")
            if scenario.gated:
                _wait_until_settled(gateway)
            run_ids = _run_ids(gateway)
            states = {run_id: gateway.store.get_run(run_id).state for run_id in run_ids}
            text = out_path.read_text("utf-8")
            for secret in {token_of(app), token}:
                if secret in text or secret in completed.stdout + completed.stderr:
                    raise RuntimeError(f"{scenario.name}: the token reached the driver output")
            output = json.loads(text)
    finally:
        gateway.store.close()
    return sanitize_fixture(scenario, output, run_ids, states)


def _run_ids(gateway) -> list[str]:
    """Every run the gateway's store holds, in store order."""
    return [
        row[0]
        for row in gateway.store._conn.execute("SELECT run_id FROM runs ORDER BY created_at, rowid")
    ]


def _wait_until_settled(gateway, timeout: float = 10.0) -> None:
    async def poll():
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if gateway.store.active_run_id() is None:
                return
            await asyncio.sleep(0.02)

    asyncio.run(poll())


def sanitize_fixture(
    scenario: Scenario, output: dict, run_ids: Sequence[str], states: Mapping[str, str]
) -> dict:
    """The deterministic fixture: placeholders for run IDs, ports, dates, machine headers.

    Runs are numbered in the order the exchanges name them, then the rest
    (a run whose ID the host never received) in store order.
    """
    seen = json.dumps(output["exchanges"])
    ordered = sorted(run_ids, key=lambda r: (seen.find(r) < 0, seen.find(r), run_ids.index(r)))
    names = {run_id: f"<run_{index + 1}>" for index, run_id in enumerate(ordered)}
    exchanges = []
    for exchange in output["exchanges"]:
        request = dict(exchange["request"])
        request["headers"] = _sanitize_headers(request["headers"])
        response = exchange["response"]
        if response is not None:
            response = dict(response)
            response["headers"] = _sanitize_headers(response.get("headers") or {})
        exchanges.append({**exchange, "request": request, "response": response})
    fixture = {
        "fixture_version": FIXTURE_VERSION,
        "scenario": scenario.name,
        "covers": scenario.covers,
        "captured_by": "scripts/openclaw_qualification.py fixtures (drive.mjs)",
        "transport": "@openclaw/ai openai-completions, host transport policy",
        "compat": _compat_label(scenario.drive.get("compat")),
        "pins": {k: v for k, v in output["versions"].items() if k != "node"},
        "drive": output["scenario"],
        "exchanges": exchanges,
        "results": [_sanitize_result(r) for r in output["results"]],
        "host_tool_executions": output["host_tool_executions"],
        "gateway_runs": [{"run": names[r], "state": states[r]} for r in ordered],
    }
    text = json.dumps(fixture, ensure_ascii=False)
    for run_id, name in names.items():
        text = text.replace(run_id, name)
    text = re.sub(r'"created": \d+', '"created": 0', text)
    return json.loads(text)


def _compat_label(compat: Any) -> str:
    if compat == "host-default":
        return "host-default"
    return "plugin-modified" if isinstance(compat, Mapping) else "plugin"


def _sanitize_headers(headers: Mapping[str, Any]) -> dict:
    out = {}
    for name, value in sorted(headers.items()):
        if name == "host":
            value = "127.0.0.1:<port>"
        elif name == "date":
            value = "<date>"
        elif name in ENVIRONMENT_HEADERS:
            value = "<environment>"
        out[name] = value
    return out


def _sanitize_result(result: dict) -> dict:
    # The host's own usage record is zeros when the gateway sends none; keep only
    # what the host received, and say so.
    out = {k: v for k, v in result.items() if k != "usage"}
    out["usage_reported_by_gateway"] = False
    return out


def versions(node: str | None = None) -> dict:
    """The pins and the machine the fixtures were captured on (section 12.5)."""

    def package(name: str) -> str | None:
        path = PACKAGE / "node_modules" / name / "package.json"
        return json.loads(path.read_text("utf-8"))["version"] if path.exists() else None

    node_version = None
    try:
        node_version = subprocess.run(
            [node or node_binary(), "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, SystemExit):
        pass
    return {
        "openclaw": package("openclaw"),
        "@openclaw/ai": package("@openclaw/ai"),
        "openai": package("openai"),
        "node": node_version,
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "relaymind_revision": application_revision(ROOT),
    }


def capture_fixtures(out_dir: Path, *, driver: Path = DRIVER, node: str | None = None) -> list:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with tempfile.TemporaryDirectory(prefix="openclaw-qualification-") as tmp:
        for scenario in SCENARIOS:
            fixture = run_driver(scenario, Path(tmp) / scenario.name, driver=driver, node=node)
            path = out_dir / f"{scenario.name}.json"
            path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", "utf-8")
            written.append(path)
    (out_dir.parent / "VERSIONS.json").write_text(
        json.dumps(versions(node), indent=2) + "\n", "utf-8"
    )
    return written


def _text(output: bytes | str | None) -> str:
    """Partial output of a timed-out child; it can be bytes even in text mode."""
    if output is None:
        return ""
    return output.decode("utf-8", "replace") if isinstance(output, bytes) else output


WRITE_TOOL = "relaymind_test_write"
WRITE_FILE = "note.txt"
# The host's own shell tool, under its tools.exec.mode policy (macOS probe, C2).
EXEC_TOOL = "exec"
# The pinned host's replay rule for a tool call ID (section 7.5).
HOST_CALL_ID_LIMIT = 40


def write_call(call_id: str = "call_write_1") -> list:
    return tool_turn(WRITE_TOOL, call_id=call_id, name=WRITE_FILE, line="written by the host")


def exec_command(sandbox: Path) -> str:
    """The shell command the exec scenarios propose: one line into the sandbox file.

    The path is absolute, so the line lands in the sandbox whatever working
    directory the host's exec uses: a deny the host ignored, or ran outside
    ``--cwd``, still shows up in ``sandbox_lines`` and never passes.
    """
    return f"echo written-by-the-host >> {shlex.quote(str(sandbox / WRITE_FILE))}"


def exec_call(sandbox: Path, call_id: str = "call_exec_1") -> list:
    return tool_turn(
        EXEC_TOOL, call_id=call_id, title="relaymind note", command=exec_command(sandbox)
    )


def host_replayed_call_id(call_id: str) -> str:
    """The ID the pinned host sends back for a call ID it received (section 7.5).

    Observed on the macOS probe (OpenClaw 2026.10.7): ``call_probe_1`` was
    replayed as ``callprobe1``, in the assistant call and the tool message
    alike. The rest of the rule is read from the pinned source
    (``src/agents/tool-call-id.ts``, "strict" mode, applied to an
    openai-completions model whose provider declares no replay policy): only
    ``[A-Za-z0-9]`` is kept, at most 40 characters. The source's extra hash
    for two IDs that collide after that is not reproduced; such a reply does
    not match here and is reported, never guessed.
    """
    kept = "".join(c for c in call_id if c.isascii() and c.isalnum())
    return kept[:HOST_CALL_ID_LIMIT]


def answers_call(issued: str, replayed: str) -> bool:
    """Whether a later request's ID refers to a call the gateway issued.

    Never an exact-string match alone: the host rewrites the IDs it replays.
    """
    return bool(replayed) and replayed in {issued, host_replayed_call_id(issued)}


def host_report(stdout: str) -> dict | None:
    """The host's own JSON report (``agent exec --json``), kept apart from the checks.

    On the macOS probe the host reported ``"ok": true`` for a run whose tool
    was denied; only ``toolSummary.failures`` showed it. The report is
    recorded as the host's statement, never as task success (section 12.5).
    """
    try:
        report = json.loads(stdout.strip())
    except ValueError:
        return None
    if not isinstance(report, dict):
        return None
    summary = report.get("toolSummary") if isinstance(report.get("toolSummary"), dict) else {}
    return {
        "ok": report.get("ok"),
        "status": report.get("status"),
        "tool_calls": summary.get("calls"),
        "tool_failures": summary.get("failures"),
    }


# The scripted turns of each host scenario (built for its sandbox), and the
# sandbox lines it must leave.
HOST_SCENARIOS: dict[str, tuple[Callable[[Path], list], int | None]] = {
    # One host turn (one `agent exec`): the echo call, then the closing text.
    # A scripted opening text turn would end that turn before any call (Q8).
    "text-tool": (
        lambda _sandbox: [echo_call(), text_turn("The tool said: echo: lantern.")],
        None,
    ),
    "tool-deny": (
        lambda _sandbox: [write_call(), text_turn("The host did not allow the write.")],
        0,
    ),
    "tool-approve": (lambda _sandbox: [write_call(), text_turn("The note was written.")], 1),
    "exec-deny": (
        lambda sandbox: [exec_call(sandbox), text_turn("The host did not allow the command.")],
        0,
    ),
    "exec-full": (lambda sandbox: [exec_call(sandbox), text_turn("The note was written.")], 1),
    # #138: the local double never answers; the harness interrupts the host mid-turn.
    "abort": (lambda _sandbox: [text_turn("never")], None),
}
# How long the harness lets the generation run before it interrupts the host.
ABORT_AFTER_SECONDS = 0.5


def host_checks(
    scenario: str,
    status: str,
    runs: Sequence[Mapping],
    requests: Sequence,
    sandbox: Path,
    stdout: str = "",
) -> dict:
    """What the scenario must show, read from the sandbox and the gateway (section 12.5).

    The call ID is matched the way the host rewrites it, never by the exact
    string the gateway issued (section 7.5). The host's exit status and its
    own report do not decide a pass: the pinned host ends a run whose tool
    was denied with exit 0 and ``"ok": true`` (macOS probe, C2).
    """
    expected = HOST_SCENARIOS[scenario][1]
    note = sandbox / WRITE_FILE
    lines = len(note.read_text("utf-8").splitlines()) if note.exists() else 0
    proposed = [
        {"run_id": run["run_id"], **{k: e["payload"].get(k) for k in ("tool_name", "proposal_id")}}
        for run in runs
        for e in run["events"]
        if e["type"] == "tool.proposed"
    ]
    executions = sum(1 for run in runs for e in run["events"] if e["type"] == "tool.started")
    ids = [p["proposal_id"] for p in proposed if p["proposal_id"]]
    # The IDs the host sent back in its tool results, as it sent them: the
    # export shows the host's rewrite instead of only whether it matched.
    replayed = [
        m.tool_call_id or ""
        for request in requests[1:]
        for m in request.messages
        if m.role.value == "tool"
    ]
    reached = any(answers_call(i, r) for i in ids for r in replayed)
    checks: dict[str, Any] = {
        "sandbox_lines": lines,
        "expected_sandbox_lines": expected,
        "gateway_tool_executions": executions,
        "proposed": proposed,
        "replayed_call_ids": list(dict.fromkeys(replayed)),
        "result_reached_next_run": reached,
        "host_report": host_report(stdout),
    }
    checks["passed"] = (
        status == "completed"
        and executions == 0
        and bool(proposed)
        and reached
        and (expected is None or lines == expected)
    )
    return checks


def _run_command(
    command: Sequence[str], env: Mapping[str, str], timeout: float
) -> tuple[str, int | None, str, str]:
    """Run the command to its end; a timeout keeps what it wrote so far."""
    try:
        completed = subprocess.run(
            list(command), env=dict(env), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        # The runs the gateway recorded so far still go into the export.
        stderr = _text(error.stderr) + f"\ntimed out after {timeout} s"
        return "timed_out", None, _text(error.stdout), stderr
    status = "completed" if completed.returncode == 0 else "failed"
    return status, completed.returncode, completed.stdout, completed.stderr


def abort_checks(signal_sent: bool, runs: Sequence[Mapping], adapter: Any) -> dict:
    """What the abort scenario must show (section 7.5, RM14, AC12).

    The host was interrupted while the gateway's generation was in flight; its
    closed request must have cancelled the run it owned, with no answer text.
    The host's exit code (130 on the macOS probe) is recorded, not judged.
    """
    cancelled = [
        run["run_id"]
        for run in runs
        if run["state"] == "cancelled"
        and any(
            e["type"] == "run.cancelled" and e["payload"].get("reason") == "cancel_requested"
            for e in run["events"]
        )
    ]
    deltas = sum(1 for run in runs for e in run["events"] if e["type"] == "answer.delta")
    checks: dict[str, Any] = {
        "signal_sent": signal_sent,
        "cancelled_runs": cancelled,
        "answer_deltas": deltas,
        "adapter_cancel_requested": bool(adapter.cancelled or adapter.interrupted),
    }
    checks["passed"] = (
        signal_sent
        and bool(runs)
        and len(cancelled) == len(runs)
        and deltas == 0
        and checks["adapter_cancel_requested"]
    )
    return checks


def _interrupt_when_started(
    command: Sequence[str], env: Mapping[str, str], adapter: Any, timeout: float
) -> tuple[str, int | None, str, str, bool]:
    """Run the command and interrupt it once the gateway's generation started.

    POSIX gets SIGINT, as an operator's Ctrl-C; Windows gets CTRL_BREAK_EVENT in
    the command's own process group. Never longer than timeout.
    """
    windows = sys.platform == "win32"
    process = subprocess.Popen(
        list(command),
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,  # type: ignore[attr-defined]
    )
    deadline = time.monotonic() + timeout
    while process.poll() is None and not adapter.started.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)
    sent = False
    if process.poll() is None and adapter.started.is_set():
        time.sleep(ABORT_AFTER_SECONDS)
        process.send_signal(signal.CTRL_BREAK_EVENT if windows else signal.SIGINT)  # type: ignore[attr-defined]
        sent = True
    try:
        stdout, stderr = process.communicate(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        return "timed_out", None, stdout, stderr + f"\ntimed out after {timeout} s", sent
    if sent:
        status = "interrupted"
    else:
        status = "completed" if process.returncode == 0 else "failed"
    return status, process.returncode, stdout, stderr, sent


def run_host(
    command: Sequence[str], out: Path, *, timeout: float = 900, scenario: str = "text-tool"
) -> dict:
    """Serve one doubles gateway, run the operator command against it, export the runs."""
    turns, _ = HOST_SCENARIOS[scenario]
    with tempfile.TemporaryDirectory(prefix="openclaw-host-") as tmp:
        sandbox = Path(tmp) / "host-sandbox"
        sandbox.mkdir()
        adapter = None
        if scenario == "abort":
            adapter = GatedAdapter(turns=turns(sandbox))
            gateway = build_gateway(
                Path(tmp),
                None,
                local_model=ConfiguredModel(adapter=adapter, model_id="gated"),
                external_turns=[],
            )
        else:
            gateway = build_gateway(Path(tmp), turns(sandbox), external_turns=[])
        app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
        signal_sent = False
        token = token_of(app)
        try:
            with RunningServer(app) as running:
                env = child_environment(running.base_url, token)
                env["RELAYMIND_TEST_SANDBOX"] = str(sandbox)
                try:
                    if adapter is None:
                        status, exit_code, stdout, stderr = _run_command(command, env, timeout)
                    else:
                        status, exit_code, stdout, stderr, signal_sent = _interrupt_when_started(
                            command, env, adapter, timeout
                        )
                        _wait_until_settled(gateway)
                except OSError as error:
                    # Missing, not executable, or otherwise not started: no run happened.
                    status, exit_code, stdout, stderr = "not_run", None, "", str(error)
                stdout = stdout.replace(token, NOT_RECORDED)
                stderr = stderr.replace(token, NOT_RECORDED)
                run_ids = _run_ids(gateway)
                runs = [
                    {
                        "run_id": run_id,
                        "state": gateway.store.get_run(run_id).state,
                        "events": [
                            {"type": e.event_type.value, "payload": e.payload}
                            for e in gateway.store.events(run_id)
                        ],
                    }
                    for run_id in run_ids
                ]
                export = {
                    "kind": "openclaw-host-qualification",
                    "evidence": "deterministic gateway doubles; not live model evidence",
                    "scenario": scenario,
                    "status": status,
                    "command": list(command),
                    "exit_code": exit_code,
                    "stdout": stdout[-20000:],
                    "stderr": stderr[-20000:],
                    "token": NOT_RECORDED,
                    "base_url": running.base_url,
                    "versions": versions(),
                    "runs": runs,
                    "host_tool_executions_by_gateway": sum(
                        1 for run in runs for e in run["events"] if e["type"] == "tool.started"
                    ),
                    "checks": (
                        abort_checks(signal_sent, runs, adapter)
                        if adapter is not None
                        else host_checks(
                            scenario, status, runs, gateway.adapter.requests, sandbox, stdout
                        )
                    ),
                }
        finally:
            gateway.store.close()
    text = json.dumps(export, indent=2, ensure_ascii=False, default=str)
    if token in text:
        raise RuntimeError("the token reached the export")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", "utf-8")
    return export


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    fixtures = sub.add_parser("fixtures", help="capture the deterministic transport fixtures")
    fixtures.add_argument("--out-dir", type=Path, required=True)
    fixtures.add_argument("--driver", type=Path, default=DRIVER)
    host = sub.add_parser("host", help="run an operator command against a doubles gateway")
    host.add_argument("--out", type=Path, required=True)
    host.add_argument("--timeout", type=float, default=900)
    host.add_argument("--scenario", choices=sorted(HOST_SCENARIOS), default="text-tool")
    host.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.mode == "fixtures":
        for path in capture_fixtures(args.out_dir, driver=args.driver):
            print(path)
        return 0
    # Drop only the separator before the command; the command keeps its own "--".
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("host needs a command after --")
    export = run_host(command, args.out, timeout=args.timeout, scenario=args.scenario)
    passed = export["checks"]["passed"]
    print(json.dumps({"status": export["status"], "runs": len(export["runs"]), "passed": passed}))
    finished = "interrupted" if args.scenario == "abort" else "completed"
    return 0 if export["status"] == finished and passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
