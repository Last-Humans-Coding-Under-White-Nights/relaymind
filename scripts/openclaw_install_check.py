"""OpenClaw install check: the packaged plugin through a host's lifecycle (#145).

RM12, RM19, RM33; AC24; spec sections 7.5, 12.5, and 14. A disposable
harness, not product code.

It serves the real gateway on a loopback socket over scripted model doubles
(spec section 13) and drives an OpenClaw host command through the life of the
release artifact, in a fresh host home (``OPENCLAW_HOME``,
``OPENCLAW_STATE_DIR``, ``OPENCLAW_CONFIG_PATH``; the operator's
``~/.openclaw`` is never touched):

1. ``--version``; ``logging.file`` set inside the host home (on the Linux VPS
   ``agent exec`` still wrote to the default ``/tmp/openclaw/``);
2. ``plugins install npm-pack:<artifact> --force --accept-capabilities``;
3. the plugin's ``baseUrl`` set to the gateway, ``plugins enable relaymind``;
4. ``plugins inspect relaymind --runtime --json``: loaded, enabled, the
   ``relaymind`` provider, no diagnostics;
5. the gateway token stored with ``models auth paste-api-key`` on standard
   input (never an argument, never in the environment);
6. ``models list --refresh``: the host discovered ``relaymind/relaymind-client-tools``;
7. one text exchange and one host-tool exchange with ``agent exec``: the
   doubles propose the host's own ``read`` tool for ``notes.txt`` in a
   sandbox, the host runs it, and its result must reach the next gateway run
   (matched through the host's call ID rewrite) holding the file's text;
8. ``plugins disable``, ``plugins uninstall --force``, then what the pinned
   host leaves behind (the config entry and the stored credential, macOS
   qualification of 27 October 2026) removed with ``config unset`` and
   ``models auth logout``.

The export records every step's command, exit code, duration, and output
(the token replaced, the host home and artifact folder as placeholders), the
gateway runs with their trace events, what was left after uninstall, and
whether the removal steps cleared it. The host's exit status and its own
``"ok": true`` never decide a pass (it says so for a denied tool); the
gateway trace and the sandbox do. A host that cannot start is ``not_run``.

The gateway answers from doubles, so a pass is a functional check of the
package and the host, never live model evidence; a run on a machine is a
platform qualification only when it is recorded as one (section 12.5).

Usage:
    python scripts/openclaw_install_check.py --tarball <relaymind-openclaw-provider-*.tgz> \\
        --out <export.json> [--label "<machine and runtime>"] [--timeout 300] \\
        -- <openclaw command, e.g. integrations/openclaw/node_modules/.bin/openclaw>
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openclaw_qualification as qualification  # noqa: E402

from api_helpers import RunningServer, make_app, token_of  # noqa: E402
from gateway_helpers import build_gateway, text_turn, tool_turn  # noqa: E402
from relaymind.api.chat import ClientToolsChat  # noqa: E402

ROOT = qualification.ROOT
PLUGIN_ID = "relaymind"
MODEL_REF = "relaymind/relaymind-client-tools"
PROFILE_ID = "relaymind:manual"
NOT_RECORDED = qualification.NOT_RECORDED
HOST_HOME = "<host-home>"
ARTIFACT_DIR = "<artifact-dir>"
REPO = "<repo>"
USER_HOME = "<home>"
DEFAULT_EVIDENCE = (
    "functional check over gateway doubles: not live model evidence, "
    "not a supported-platform qualification"
)
TEXT_PROMPT = "Say hello"
TEXT_ANSWER = "Hello from RelayMind."
TOOL_PROMPT = "Read notes.txt and report its first line."
NOTE_FILE = "notes.txt"
NOTE_TEXT = "The lantern is on the second shelf."
TOOL_CALL_ID = "call_read_1"
TOOL_ANSWER = "The note says the lantern is on the second shelf."
OUTPUT_TAIL = 4000

STEPS = (
    "version",
    "isolate-log",
    "install",
    "configure",
    "enable",
    "inspect",
    "store-token",
    "discover",
    "text-exchange",
    "tool-exchange",
    "disable",
    "uninstall",
    "remove-config",
    "remove-credential",
)
# Run once the install was attempted and passed, whatever failed after it.
CLEANUP_STEPS = ("disable", "uninstall", "remove-config", "remove-credential")


@dataclass
class Context:
    """What the steps share: the host home, the gateway, the sandbox."""

    home: Path
    env: dict[str, str]
    tarball: Path
    base_url: str
    token: str
    sandbox: Path
    gateway: Any

    @property
    def config_path(self) -> Path:
        return Path(self.env["OPENCLAW_CONFIG_PATH"])

    @property
    def state_dir(self) -> Path:
        return Path(self.env["OPENCLAW_STATE_DIR"])


def host_environment(home: Path) -> dict[str, str]:
    """The host's environment: an isolated home, no proxy, no RelayMind token."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in {"HTTP_PROXY", "HTTPS_PROXY"} and not k.startswith("RELAYMIND_")
    }
    env.update(
        {
            "OPENCLAW_HOME": str(home / "home"),
            "OPENCLAW_STATE_DIR": str(home / "state"),
            "OPENCLAW_CONFIG_PATH": str(home / "openclaw.json"),
            "NO_PROXY": "127.0.0.1,localhost,::1",
        }
    )
    return env


def _json(stdout: str) -> Any:
    try:
        return json.loads(stdout.strip())
    except ValueError:
        return None


def check_inspect(stdout: str, _ctx: Context) -> tuple[bool, dict]:
    report = _json(stdout)
    plugin = report.get("plugin", {}) if isinstance(report, dict) else {}
    check = {
        "status": plugin.get("status"),
        "enabled": plugin.get("enabled"),
        "version": plugin.get("version"),
        "provider_ids": plugin.get("providerIds"),
        "diagnostics": report.get("diagnostics") if isinstance(report, dict) else None,
    }
    passed = (
        check["status"] == "loaded"
        and check["enabled"] is True
        and PLUGIN_ID in (check["provider_ids"] or [])
        and check["diagnostics"] == []
    )
    return passed, check


def check_discover(stdout: str, _ctx: Context) -> tuple[bool, dict]:
    report = _json(stdout)
    models = report.get("models", []) if isinstance(report, dict) else []
    keys = [m.get("key") for m in models if isinstance(m, dict)]
    relaymind = [k for k in keys if isinstance(k, str) and k.startswith(f"{PLUGIN_ID}/")]
    return MODEL_REF in relaymind, {"relaymind_models": relaymind}


def _runs(ctx: Context, run_ids: Sequence[str]) -> list[dict]:
    return [
        {
            "run_id": run_id,
            "state": ctx.gateway.store.get_run(run_id).state,
            "events": [
                {"type": e.event_type.value, "payload": e.payload}
                for e in ctx.gateway.store.events(run_id)
            ],
        }
        for run_id in run_ids
    ]


def _executions(runs: Sequence[Mapping]) -> int:
    return sum(1 for run in runs for e in run["events"] if e["type"] == "tool.started")


def text_checks(stdout: str, runs: Sequence[Mapping]) -> dict:
    answer = "".join(
        e["payload"].get("text", "")
        for run in runs
        for e in run["events"]
        if e["type"] == "answer.delta"
    )
    checks = {
        "runs": [run["run_id"] for run in runs],
        "gateway_answer": answer,
        "answer_in_host_output": TEXT_ANSWER in stdout,
        "gateway_tool_executions": _executions(runs),
        "host_report": qualification.host_report(stdout),
    }
    checks["passed"] = (
        bool(runs)
        and all(run["state"] == "completed" for run in runs)
        and answer == TEXT_ANSWER
        and checks["answer_in_host_output"]
        and checks["gateway_tool_executions"] == 0
    )
    return checks


def tool_checks(stdout: str, runs: Sequence[Mapping], requests: Sequence) -> dict:
    """The host ran its read tool once and sent the file's text to the next run."""
    proposed = [
        {"run_id": run["run_id"], **{k: e["payload"].get(k) for k in ("tool_name", "proposal_id")}}
        for run in runs
        for e in run["events"]
        if e["type"] == "tool.proposed"
    ]
    ids = [p["proposal_id"] for p in proposed if p["proposal_id"]]
    answers = [
        m
        for request in requests
        for m in request.messages
        if m.role.value == "tool"
        and any(qualification.answers_call(i, m.tool_call_id or "") for i in ids)
    ]
    checks: dict[str, Any] = {
        "runs": [run["run_id"] for run in runs],
        "proposed": proposed,
        "replayed_call_id": answers[0].tool_call_id if answers else None,
        "result_reached_next_run": bool(answers),
        "host_result_holds_the_file": any(NOTE_TEXT in (m.content or "") for m in answers),
        "answer_in_host_output": TOOL_ANSWER in stdout,
        "gateway_tool_executions": _executions(runs),
        "host_report": qualification.host_report(stdout),
    }
    checks["passed"] = (
        [p["tool_name"] for p in proposed] == ["read"]
        and checks["result_reached_next_run"]
        and checks["host_result_holds_the_file"]
        and checks["answer_in_host_output"]
        and checks["gateway_tool_executions"] == 0
    )
    return checks


def _profile_present(config_path: Path) -> tuple[bool, bool]:
    try:
        config = json.loads(config_path.read_text("utf-8"))
    except (OSError, ValueError):
        return False, False
    entry = PLUGIN_ID in (config.get("plugins") or {}).get("entries", {})
    profile = PROFILE_ID in ((config.get("auth") or {}).get("profiles") or {})
    return entry, profile


def _credential_in(path: Path, token: str) -> bool:
    """Whether the token is in a host file: SQLite tables read as SQLite, others as bytes."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if token.encode() in data:
        return True
    if not data.startswith(b"SQLite format 3"):
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = [
                r[0] for r in conn.execute("select name from sqlite_master where type='table'")
            ]
            return any(
                token in repr(row)
                for table in tables
                for row in conn.execute(f'select * from "{table}"')  # noqa: S608
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def leftovers(ctx: Context) -> dict:
    entry, profile = _profile_present(ctx.config_path)
    files = [p for p in ctx.home.rglob("*") if p.is_file() and ctx.sandbox not in p.parents]
    return {
        "config_entry": entry,
        "auth_profile": profile,
        "credential_in_host_state": any(_credential_in(p, ctx.token) for p in files),
    }


def _argv(ctx: Context, name: str) -> tuple[list[str], str | None]:
    """The host arguments of one step and its standard input."""
    return {
        "version": (["--version"], None),
        "isolate-log": (
            ["config", "set", "logging.file", str(ctx.home / "openclaw.log")],
            None,
        ),
        "install": (
            ["plugins", "install", f"npm-pack:{ctx.tarball}", "--force", "--accept-capabilities"],
            None,
        ),
        "configure": (
            ["config", "set", f"plugins.entries.{PLUGIN_ID}.config.baseUrl", ctx.base_url],
            None,
        ),
        "enable": (["plugins", "enable", PLUGIN_ID], None),
        "inspect": (["plugins", "inspect", PLUGIN_ID, "--runtime", "--json"], None),
        "store-token": (
            ["models", "auth", "paste-api-key", "--provider", PLUGIN_ID],
            ctx.token + "\n",
        ),
        "discover": (["models", "list", "--refresh", "--provider", PLUGIN_ID, "--json"], None),
        "text-exchange": (_exec(ctx, TEXT_PROMPT), None),
        "tool-exchange": (_exec(ctx, TOOL_PROMPT), None),
        "disable": (["plugins", "disable", PLUGIN_ID], None),
        "uninstall": (["plugins", "uninstall", PLUGIN_ID, "--force"], None),
        "remove-config": (["config", "unset", f"plugins.entries.{PLUGIN_ID}"], None),
        "remove-credential": (["models", "auth", "logout", PROFILE_ID, "--yes"], None),
    }[name]


def _exec(ctx: Context, prompt: str) -> list[str]:
    return [
        "agent",
        "exec",
        prompt,
        "--model",
        MODEL_REF,
        "--json",
        "--config",
        str(ctx.config_path),
        "--cwd",
        str(ctx.sandbox),
    ]


def _sanitize(text: str, ctx: Context) -> str:
    text = text.replace(ctx.token, NOT_RECORDED)
    # Most specific first: the host home and the artifact may sit under the others.
    places = (
        (ctx.home, HOST_HOME),
        (ctx.tarball.parent, ARTIFACT_DIR),
        (ROOT, REPO),
        (Path.home(), USER_HOME),
    )
    for path, placeholder in places:
        for form in {str(path), str(path.resolve()), json.dumps(str(path))[1:-1]}:
            text = text.replace(form, placeholder)
    return text


def _run(command: Sequence[str], args: list[str], stdin: str | None, env, timeout: float):
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [*command, *args],
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        result = (completed.returncode, completed.stdout, completed.stderr, None)
    except subprocess.TimeoutExpired as error:
        result = (
            None,
            qualification._text(error.stdout),
            qualification._text(error.stderr),
            "timed_out",
        )
    return (*result, round(time.monotonic() - started, 3))


CHECKS: dict[str, Callable[[str, Context], tuple[bool, dict]]] = {
    "inspect": check_inspect,
    "discover": check_discover,
}


def run_steps(command: Sequence[str], ctx: Context, timeout: float) -> tuple[list, dict, str]:
    steps: list[dict] = []
    exchanges: dict[str, dict] = {}
    found: dict[str, dict] = {}
    status = "completed"
    stopped = False
    installed = False
    for name in STEPS:
        args, stdin = _argv(ctx, name)
        step: dict[str, Any] = {
            "name": name,
            "argv": [_sanitize(a, ctx) for a in args],
            "status": "not_run",
        }
        steps.append(step)
        if (stopped and name not in CLEANUP_STEPS) or (name in CLEANUP_STEPS and not installed):
            continue
        before = set(qualification._run_ids(ctx.gateway))
        requests_before = len(ctx.gateway.adapter.requests)
        try:
            code, stdout, stderr, timed_out, seconds = _run(command, args, stdin, ctx.env, timeout)
        except OSError as error:
            # The host command cannot start: nothing ran, nothing passed.
            step["stderr"] = str(error)
            return steps, exchanges, "not_run"
        step.update(
            {
                "exit_code": code,
                "seconds": seconds,
                "stdout": _sanitize(stdout, ctx)[-OUTPUT_TAIL:],
                "stderr": _sanitize(stderr, ctx)[-OUTPUT_TAIL:],
            }
        )
        passed = code == 0 and timed_out is None
        if name in CHECKS:
            ok, check = CHECKS[name](stdout, ctx)
            step["check"] = check
            passed = passed and ok
        if name in ("text-exchange", "tool-exchange"):
            qualification._wait_until_settled(ctx.gateway)
            new = [r for r in qualification._run_ids(ctx.gateway) if r not in before]
            runs = _runs(ctx, new)
            if name == "text-exchange":
                checks = text_checks(stdout, runs)
            else:
                checks = tool_checks(stdout, runs, ctx.gateway.adapter.requests[requests_before:])
            exchanges[name.removesuffix("-exchange")] = checks
            passed = passed and checks["passed"]
        if name == "uninstall":
            found["after_uninstall"] = leftovers(ctx)
        if name == "remove-credential":
            found["after_removal"] = leftovers(ctx)
            passed = passed and not any(found["after_removal"].values())
        step["status"] = "timed_out" if timed_out else ("passed" if passed else "failed")
        if name == "install" and passed:
            installed = True
        if not passed:
            stopped = True
    exchanges["_leftovers"] = found
    return steps, exchanges, status


def _artifact(tarball: Path) -> dict:
    data = tarball.read_bytes()
    return {
        "filename": tarball.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def run_install_check(
    command: Sequence[str],
    tarball: Path,
    out: Path,
    *,
    timeout: float = 300,
    label: str | None = None,
    host_home: Path | None = None,
) -> dict:
    """Run the lifecycle against one doubles gateway; write and return the export."""
    tarball = Path(tarball).resolve()
    with tempfile.TemporaryDirectory(prefix="openclaw-install-check-") as tmp:
        home = Path(host_home).resolve() if host_home else Path(tmp) / "host"
        sandbox = home / "sandbox"
        sandbox.mkdir(parents=True, exist_ok=True)
        (sandbox / NOTE_FILE).write_text(NOTE_TEXT + "\n", "utf-8")
        turns = [
            text_turn(TEXT_ANSWER),
            tool_turn("read", call_id=TOOL_CALL_ID, path=NOTE_FILE),
            text_turn(TOOL_ANSWER),
        ]
        gateway = build_gateway(Path(tmp) / "gateway", turns, external_turns=[])
        app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator))
        token = token_of(app)
        try:
            with RunningServer(app) as running:
                ctx = Context(
                    home=home,
                    env=host_environment(home),
                    tarball=tarball,
                    base_url=running.base_url,
                    token=token,
                    sandbox=sandbox,
                    gateway=gateway,
                )
                steps, exchanges, status = run_steps(command, ctx, timeout)
                found = exchanges.pop("_leftovers", {})
                runs = _runs(ctx, qualification._run_ids(gateway))
                version = next(
                    (s.get("stdout", "").strip() for s in steps if s["name"] == "version"), ""
                )
                export = {
                    "kind": "openclaw-install-check",
                    "evidence": f"{label}; {DEFAULT_EVIDENCE}" if label else DEFAULT_EVIDENCE,
                    "status": status,
                    "passed": status == "completed" and all(s["status"] == "passed" for s in steps),
                    "command": [_sanitize(c, ctx) for c in command],
                    "host_version": version or None,
                    "artifact": _artifact(tarball),
                    "isolation": {
                        k: _sanitize(ctx.env[k], ctx)
                        for k in ("OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH")
                    },
                    "token": NOT_RECORDED,
                    "steps": steps,
                    "exchanges": exchanges,
                    "leftovers": found,
                    "runs": runs,
                    "host_tool_executions_by_gateway": _executions(runs),
                    "versions": qualification.versions(),
                }
                text = _sanitize(json.dumps(export, indent=2, ensure_ascii=False, default=str), ctx)
        finally:
            gateway.store.close()
    if token in text:
        raise RuntimeError("the token reached the export")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", "utf-8")
    return json.loads(text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tarball", type=Path, required=True, help="the release artifact")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300, help="seconds per host command")
    parser.add_argument("--label", help="the machine and runtime, recorded in the export")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("the openclaw command goes after --")
    if not args.tarball.is_file():
        raise SystemExit(f"no release artifact at {args.tarball} (npm run pack:release)")
    if shutil.which(command[0]) is None and not Path(command[0]).exists():
        print(f"{command[0]} is not installed: the export records not_run", file=sys.stderr)
    export = run_install_check(
        command, args.tarball, args.out, timeout=args.timeout, label=args.label
    )
    failed = [s["name"] for s in export["steps"] if s["status"] != "passed"]
    print(json.dumps({"status": export["status"], "passed": export["passed"], "failed": failed}))
    return 0 if export["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
