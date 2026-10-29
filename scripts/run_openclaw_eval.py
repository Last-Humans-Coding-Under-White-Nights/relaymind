"""OpenClaw integration evaluation runner (spec 0.50 section 12.5, #139).

RM16, RM18, RM25, RM26, RM33; AC24. The frozen tasks
(``evaluation/openclaw_tasks.v1.json``, checked against its lock) run through a
real OpenClaw host in four arms:

``direct``
    the host's own provider entry for the external model;
``relaymind_external_only`` and ``relaymind_auto``
    the RelayMind plugin with that execution mode, against a gateway started
    with ``relaymind serve --config`` with the same external model and the
    pinned local model (relaymind_auto needs one; relaymind_external_only
    without one is launched without it and labeled so);
``relaymind_no_local``
    the plugin in auto, against a gateway launched without a local model.

Each arm has its own isolated host home (``OPENCLAW_HOME``,
``OPENCLAW_STATE_DIR``, ``OPENCLAW_CONFIG_PATH``; the operator's
``~/.openclaw`` is never touched) and, for a RelayMind arm, its own gateway.
The plugin is installed from the release artifact with
``--accept-capabilities``, configured (``baseUrl``, ``executionMode``,
``diagnosticsPath``, the copied ``limits``), enabled, and given the gateway
token through ``models auth paste-api-key`` on standard input. Every slot of
the frozen order then runs ``openclaw agent exec <prompt> --model <ref> --json``
in a fresh sandbox holding only the task's files, under the task's exec
policy, with ``--state-dir`` on a fresh, empty directory in the host home.
The export records every attempt raw (the host's JSON report, its output
tails, the plugin's diagnostics records, the gateway runs of the
attempt's window with their trace events, the sandbox after) and the grades
and summary of ``relaymind.evaluation.openclaw``; a Markdown report is written
next to it. The gateway token and the external credential are never written.

An arm that cannot run (no external endpoint or credential, no usable local
model) is recorded as not_run with its reason. This runner needs a real host,
live models, and a provider credential: it is an operator step, never part
of the deterministic suite (``tests/test_openclaw_eval_runner.py`` drives it
over a stub host and gateway doubles).

Usage:
    export RELAYMIND_EXTERNAL_API_KEY=...
    python scripts/run_openclaw_eval.py \\
        --tarball <dir>/relaymind-openclaw-provider-0.1.0.tgz \\
        --external-base-url <url> --external-model <model> --external-context-limit <n> \\
        [--manifest models/qwen3-0.6b-q8_0.manifest.json] [--no-local-model] \\
        [--arms direct relaymind_external_only relaymind_auto relaymind_no_local] \\
        [--tasks-subset ID ...] \\
        --out docs/evidence/<date>-openclaw-eval-<machine>/ \\
        -- integrations/openclaw/node_modules/.bin/openclaw
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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import openclaw_install_check as install_check  # noqa: E402

from relaymind.evaluation.environment import (  # noqa: E402
    application_revision,
    checkout_environment,
    machine_manifest,
)
from relaymind.evaluation.openclaw import (  # noqa: E402
    ARMS,
    LOCK_FILE,
    RELAYMIND_ARMS,
    RELAYMIND_HOST_MODEL,
    TASKS_FILE,
    Task,
    TaskSet,
    TaskSetError,
    check_lock,
    grade_attempt,
    load_tasks,
    render_markdown,
    sampling_order,
    summarize,
)

PLUGIN_ID = install_check.PLUGIN_ID
MODEL_REF = install_check.MODEL_REF
DIRECT_PROVIDER = "relaymind-eval-direct"
EXECUTION_MODES = {
    "relaymind_external_only": "external_only",
    "relaymind_auto": "auto",
    "relaymind_no_local": "auto",
}
EXPORT_NAME = "openclaw-eval.json"
REPORT_NAME = "openclaw-eval.md"
NOT_RECORDED = "<not recorded>"
HOST_HOME = "<host-home>"
OUTPUT_TAIL = 4000
SETUP_TIMEOUT = 300.0
SETTLE_SECONDS = 5.0
EXTERNAL_KEY_ENV = "RELAYMIND_EXTERNAL_API_KEY"
NO_EXTERNAL = (
    "no external endpoint and credential (--external-base-url, --external-model, "
    f"--external-context-limit, and {EXTERNAL_KEY_ENV})"
)
NO_LOCAL = "no usable local model (the pinned manifest, its artifact, and llama-server)"


@dataclass(frozen=True)
class DirectEndpoint:
    """The external provider the direct arm configures in the host itself."""

    base_url: str
    model: str
    api_key: str = field(repr=False)
    context_limit: int | None = None


@dataclass
class GatewayHandle:
    """One running gateway of a RelayMind arm, as the runner sees it."""

    base_url: str
    token: str = field(repr=False)
    run_ids: Callable[[], list[str]]
    run_records: Callable[[Sequence[str]], list[dict]]
    stop: Callable[[], None]
    launch: dict[str, Any]


def arm_availability(*, external: Any, local_model: bool, key_present: bool) -> dict[str, str]:
    """The arms that cannot run, each with its reason (section 12.5: recorded as not_run)."""
    # relaymind_auto is the external model and the local model together: without
    # either, its gateway is not the spec's auto arm, so it does not run.
    missing: dict[str, list[str]] = {}
    if external is None or not key_present:
        for arm in ARMS:
            missing.setdefault(arm, []).append(NO_EXTERNAL)
    if not local_model:
        missing.setdefault("relaymind_auto", []).append(NO_LOCAL)
    return {arm: "; ".join(missing[arm]) for arm in ARMS if arm in missing}


def expected_host_model(arm: str, direct: DirectEndpoint | None) -> dict[str, str]:
    if arm == "direct":
        return {"provider": DIRECT_PROVIDER, "model": direct.model if direct else ""}
    return dict(RELAYMIND_HOST_MODEL)


def model_ref(arm: str, direct: DirectEndpoint | None) -> str:
    return f"{DIRECT_PROVIDER}/{direct.model}" if arm == "direct" and direct else MODEL_REF


# -- the host --------------------------------------------------------------------------


def _merge(target: dict, patch: Mapping) -> dict:
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = json.loads(json.dumps(value))
    return target


def proxy_variables() -> dict[str, str]:
    """The operator's proxy variables (either case), as a gateway inherits them."""
    return {
        k: v
        for k, v in os.environ.items()
        if k.upper() in install_check.qualification.PROXY_VARIABLES
    }


def node_identity(host_command: Sequence[str], env: Mapping[str, str]) -> dict[str, str | None]:
    """The Node that runs the host: an explicit node in the command, else node on PATH."""
    first = host_command[0] if host_command else ""
    if Path(first).name.lower() in ("node", "node.exe"):
        node: str | None = first
    else:
        node = shutil.which("node", path=env.get("PATH"))
    if node is None:
        return {"executable": None, "version": None}
    try:
        done = subprocess.run(
            [node, "--version"], env=dict(env), capture_output=True, text=True, timeout=30
        )
        version = (done.stdout.strip() or None) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        version = None
    return {"executable": node, "version": version}


class Host:
    """One isolated host home and the host command run in it."""

    def __init__(
        self, command: Sequence[str], home: Path, *, provider_network: bool = False
    ) -> None:
        """provider_network: this host calls the external provider (the direct arm).

        It then keeps the operator's proxy variables, the way a gateway does, so
        the direct arm and the RelayMind arms reach the provider by the same
        network path; loopback still never goes through a proxy.
        """
        self.command = list(command)
        self.home = home
        home.mkdir(parents=True, exist_ok=True)
        self.env = install_check.host_environment(home)
        if provider_network:
            self.env.update(proxy_variables())
            operator = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
            if operator:
                self.env["NO_PROXY"] = f"{operator},{self.env['NO_PROXY']}"

    @property
    def config_path(self) -> Path:
        return Path(self.env["OPENCLAW_CONFIG_PATH"])

    @property
    def diagnostics_path(self) -> Path:
        return self.home / "diagnostics.jsonl"

    def merge_config(self, patch: Mapping) -> None:
        """Merge into the host's config file; the file must be strict JSON."""
        path = self.config_path
        config = json.loads(path.read_text("utf-8")) if path.exists() else {}
        if not isinstance(config, dict):
            raise ValueError("the host config file is not a JSON object")
        path.write_text(json.dumps(_merge(config, patch), indent=2), "utf-8")

    def run(self, args: Sequence[str], *, stdin: str | None = None, timeout: float) -> dict:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [*self.command, *args],
                env=self.env,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            status = "completed" if completed.returncode == 0 else "failed"
            code, out, err = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as error:
            status, code = "timed_out", None
            out = install_check.qualification._text(error.stdout)
            err = install_check.qualification._text(error.stderr) + f"\ntimed out after {timeout} s"
        except OSError as error:
            # The host did not start (missing, not executable, or on Windows a shim
            # that is not an executable): not_run, never a crash of the runner.
            status, code, out, err = "not_run", None, "", str(error)
        return {
            "argv": list(args),
            "status": status,
            "exit_code": code,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "stdout": out,
            "stderr": err,
        }


def _recorded(result: Mapping) -> dict:
    """A host command result as the export keeps it: its output cut to the tails."""
    return {
        "argv": result["argv"],
        "status": result["status"],
        "exit_code": result["exit_code"],
        "elapsed_ms": result["elapsed_ms"],
        "stdout_tail": result["stdout"][-OUTPUT_TAIL:],
        "stderr_tail": result["stderr"][-OUTPUT_TAIL:],
    }


def _step(name: str, result: Mapping) -> dict:
    return {"name": name, **_recorded(result)}


def _configured(name: str, keys: Sequence[str]) -> dict:
    """A config file merge, recorded as a setup step the runner made (not a host command)."""
    return {
        "name": name,
        "argv": ["<runner: merge into the host config file>", *keys],
        "status": "completed",
        "exit_code": 0,
        "elapsed_ms": 0,
        "stdout_tail": "",
        "stderr_tail": "",
    }


def plugin_config(arm: str, handle: GatewayHandle, host: Host, limits: Mapping) -> dict:
    config: dict[str, Any] = {
        "baseUrl": handle.base_url,
        "executionMode": EXECUTION_MODES[arm],
        "diagnosticsPath": str(host.diagnostics_path),
    }
    if limits:
        config["limits"] = dict(limits)
    return config


def setup_relaymind(
    arm: str, host: Host, handle: GatewayHandle, tarball: Path, limits: Mapping
) -> tuple[list[dict], dict, bool]:
    """Install, configure, enable, and give the plugin the token (on standard input)."""
    config = plugin_config(arm, handle, host, limits)
    steps: list[dict] = []

    def ran(name: str, args: list[str], stdin: str | None = None) -> bool:
        steps.append(_step(name, host.run(args, stdin=stdin, timeout=SETUP_TIMEOUT)))
        return steps[-1]["status"] == "completed"

    install = ["plugins", "install", f"npm-pack:{tarball}", "--force", "--accept-capabilities"]
    if not (ran("version", ["--version"]) and ran("install", install)):
        return steps, config, False
    host.merge_config({"plugins": {"entries": {PLUGIN_ID: {"config": config}}}})
    steps.append(_configured("configure", sorted(config)))
    ok = ran("enable", ["plugins", "enable", PLUGIN_ID]) and ran(
        "store-token",
        ["models", "auth", "paste-api-key", "--provider", PLUGIN_ID],
        handle.token + "\n",
    )
    return steps, config, ok


def setup_direct(host: Host, direct: DirectEndpoint) -> tuple[list[dict], bool]:
    """The host's own provider entry for the external model (the probe's config form)."""
    steps = [_step("version", host.run(["--version"], timeout=SETUP_TIMEOUT))]
    if steps[0]["status"] != "completed":
        return steps, False
    model: dict[str, Any] = {
        "id": direct.model,
        "name": direct.model,
        "reasoning": False,
        "input": ["text"],
    }
    if direct.context_limit:
        model["contextWindow"] = direct.context_limit
    host.merge_config(
        {
            "models": {
                "providers": {
                    DIRECT_PROVIDER: {
                        "baseUrl": direct.base_url,
                        "api": "openai-completions",
                        "apiKey": direct.api_key,
                        "models": [model],
                    }
                }
            }
        }
    )
    steps.append(_configured("configure", [f"models.providers.{DIRECT_PROVIDER}"]))
    return steps, True


# -- one attempt -----------------------------------------------------------------------


def _host_report(stdout: str) -> Any:
    text = stdout.strip()
    for candidate in (text, *reversed(text.splitlines())):
        try:
            report = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(report, dict):
            return report
    return None


def _diagnostics(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text("utf-8", errors="replace").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            records.append({"event": "unparsed", "runId": None, "outcome": None})
    return records


def _settled(handle: GatewayHandle, run_ids: Sequence[str]) -> list[dict]:
    """The window's runs once they are terminal, or as they are after SETTLE_SECONDS."""
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        runs = handle.run_records(run_ids)
        terminal = all(r["state"] in ("completed", "failed", "cancelled") for r in runs)
        if terminal or time.monotonic() > deadline:
            return runs
        time.sleep(0.05)


def run_attempt(
    slot: Any,
    task: Task,
    host: Host,
    handle: GatewayHandle | None,
    direct: DirectEndpoint | None,
    timeout: float,
) -> dict[str, Any]:
    sandbox = host.home / "sandboxes" / f"{slot.index:04d}-{task.task_id}"
    sandbox.mkdir(parents=True)
    for name, text in task.sandbox.items():
        (sandbox / name).write_text(text, "utf-8")
    # The install check's exec_state_dir: without --state-dir the pinned host deletes a
    # temporary state directory after the turn, which fails on Windows (EBUSY) and ends
    # every turn ok: false. A fresh, empty one per attempt, in the arm's host home.
    state_dir = host.home / "agent-exec" / f"{slot.index:04d}-{task.task_id}"
    state_dir.mkdir(parents=True)
    host.merge_config({"tools": {"exec": {"mode": task.exec_policy}}})
    before = set(handle.run_ids()) if handle else set()
    seen = len(_diagnostics(host.diagnostics_path))
    args = [
        "agent",
        "exec",
        task.prompt,
        "--model",
        model_ref(slot.arm, direct),
        "--json",
        "--config",
        str(host.config_path),
        "--cwd",
        str(sandbox),
        "--state-dir",
        str(state_dir),
    ]
    result = host.run(args, timeout=timeout)
    window = [i for i in handle.run_ids() if i not in before] if handle else []
    runs = _settled(handle, window) if handle else []
    after = {
        p.name: p.read_text("utf-8", errors="replace")
        for p in sorted(sandbox.iterdir())
        if p.is_file()
    }
    return {
        "index": slot.index,
        "repeat": slot.repeat,
        "task_id": task.task_id,
        "arm": slot.arm,
        "language": task.language,
        "exec_policy": task.exec_policy,
        "expected_host_model": expected_host_model(slot.arm, direct),
        "host": {**_recorded(result), "report": _host_report(result["stdout"])},
        "sandbox_after": after,
        "diagnostics": _diagnostics(host.diagnostics_path)[seen:] if handle else [],
        "window_runs": window,
        "runs": runs,
    }


# -- the run ---------------------------------------------------------------------------


def _read_only(store: Path) -> sqlite3.Connection:
    """The gateway store, read-only, by its file URI (portable: a Windows drive and
    backslashes, or a ``#``, ``?``, or ``%`` in the path, still name the file)."""
    return sqlite3.connect(store.resolve().as_uri() + "?mode=ro", uri=True)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _command_dirs(host_command: Sequence[str]) -> list[Path]:
    """The directories the host command names (either slash, as Path reads it)."""
    return [Path(part).parent for part in host_command if Path(part).parent != Path(".")]


def _sanitize(text: str, secrets: Sequence[str], places: Sequence[tuple[Path, str]]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, NOT_RECORDED)
    home = Path.home()
    # In the order given, most specific first, so <repo> wins over <home>.
    for path, placeholder in places:
        # Only absolute forms: a relative one (``.`` for ``--tarball x.tgz``) would
        # rewrite unrelated text, and the working directory names nothing of its own.
        if path == Path("."):
            continue
        absolute = [p for p in (path, path.resolve()) if p.is_absolute()]
        forms = {str(p) for p in absolute}
        # The pinned host prints a path under the user's home as ``~/...``
        # (every skill location in its system prompt), so that form goes too.
        for candidate in absolute:
            if candidate != home and candidate.is_relative_to(home):
                forms.add(f"~/{candidate.relative_to(home).as_posix()}")
        forms |= {json.dumps(form)[1:-1] for form in forms}
        for form in sorted(forms, key=len, reverse=True):
            text = text.replace(form, placeholder)
    # Whatever else the host printed under the user's home.
    text = text.replace("~/", "<home>/")
    # The machine's names (the host= field of the host's runtime line on every
    # user message, the hostname, the macOS computer name) become <host>.
    return install_check.qualification.mask_machine_names(text)


def _aborted_line(aborted: Mapping[str, Any] | None) -> str:
    """The report's line for an evaluation the operator interrupted (spec 0.50)."""
    if not aborted:
        return ""
    if aborted["stage"] == "setup":
        where = f"during the setup of {aborted['arm']}"
    elif aborted["index"] is None:
        where = "after the last attempt"
    else:
        where = (
            f"at slot {aborted['index']} ({aborted['arm']}, {aborted['task_id']}, "
            f"repeat {aborted['repeat']})"
        )
    return (
        f"\nAborted by the operator {where}: {aborted['slots_not_run']} slot(s) not run; "
        "the counts cover the attempts that ended.\n"
    )


def run_evaluation(
    tasks: TaskSet,
    *,
    arms: Sequence[str] = ARMS,
    host_command: Sequence[str],
    tarball: Path,
    backend: Any,
    out: Path,
    evidence: str,
    task_ids: Sequence[str] | None = None,
    limits: Mapping[str, int] | None = None,
    command: str | None = None,
) -> dict[str, Any]:
    """Run the frozen order in every available arm; write the export and the report."""
    selected = list(task_ids) if task_ids else [t.task_id for t in tasks.tasks]
    unknown = sorted(set(selected) - {t.task_id for t in tasks.tasks})
    if unknown:
        raise TaskSetError(f"unknown task id(s) {', '.join(unknown)}")
    not_run: dict[str, str] = {}
    for arm in ARMS:
        if arm not in arms:
            not_run[arm] = "not requested"
        elif (reason := backend.unavailable(arm)) is not None:
            not_run[arm] = reason
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    secrets = [s for s in backend.secrets() if s]
    arm_records: dict[str, dict[str, Any]] = {}
    attempts: list[dict] = []
    grades: list[dict] = []
    work = Path(tempfile.mkdtemp(prefix="relaymind-openclaw-eval-"))
    # The hosts' environment, so the Node on its PATH is the one that runs them.
    node = node_identity(host_command, install_check.host_environment(work / "node"))
    hosts: dict[str, Host] = {}
    handles: dict[str, GatewayHandle] = {}
    direct = None
    aborted: dict[str, Any] | None = None
    places: list[tuple[Path, str]] = []
    # The slots of every arm still to be set up: an interrupt in setup leaves them all not run.
    planned = [s for s in sampling_order(tasks) if s.task_id in selected and s.arm not in not_run]
    slots: list[Any] = []
    setting_up: str | None = None
    try:
        try:
            for arm in ARMS:
                if arm in not_run:
                    continue
                setting_up = arm
                host = Host(host_command, work / arm / "host", provider_network=arm == "direct")
                places.append((host.home, HOST_HOME))
                record: dict[str, Any] = {
                    "status": "ran",
                    "host_model_ref": None,
                    "expected_host_model": None,
                }
                try:
                    if arm in RELAYMIND_ARMS:
                        handle = backend.start(arm, work / arm / "gateway")
                        handles[arm] = handle
                        secrets.append(handle.token)
                        steps, config, ok = setup_relaymind(
                            arm, host, handle, tarball, limits or {}
                        )
                        record.update(plugin_config=config, launch=handle.launch)
                    else:
                        direct = backend.direct()
                        secrets.append(direct.api_key)
                        steps, ok = setup_direct(host, direct)
                except Exception as error:  # noqa: BLE001 - a setup failure is recorded, never hidden
                    steps, ok = [], False
                    record["setup_error"] = f"{type(error).__name__}: {error}"
                record["setup"] = steps
                record["host_version"] = steps[0]["stdout_tail"].strip() if steps else None
                record["host_model_ref"] = model_ref(arm, direct)
                record["expected_host_model"] = expected_host_model(arm, direct)
                if not ok:
                    failed = next((s["name"] for s in steps if s["status"] != "completed"), "start")
                    not_run[arm] = f"setup failed at {failed}"
                    record.update(status="not_run", reason=not_run[arm])
                else:
                    hosts[arm] = host
                arm_records[arm] = record
                setting_up = None
            slots = [s for s in planned if s.arm in hosts]
            for slot in slots:
                task = tasks.task(slot.task_id)
                print(f"{slot.index} {slot.arm} {slot.task_id} r{slot.repeat}", flush=True)
                attempt = run_attempt(
                    slot,
                    task,
                    hosts[slot.arm],
                    handles.get(slot.arm),
                    direct,
                    tasks.time_limit_seconds,
                )
                attempts.append(attempt)
                grade = grade_attempt(task, attempt)
                grades.append(grade)
                print(f"  {'pass' if grade['success'] else 'fail'} {grade['failures']}", flush=True)
        except KeyboardInterrupt:
            # Spec 0.50: an operator interrupt, wherever it lands (arm setup, an attempt,
            # its grading). The attempts that ended are kept and graded; the rest are
            # not run, never failures.
            for ended in attempts[len(grades) :]:
                grades.append(grade_attempt(tasks.task(ended["task_id"]), ended))
            if setting_up is not None:
                # No slot ran: every planned slot of an arm whose setup did not fail.
                stopped = None
                not_started = sum(1 for s in planned if s.arm not in not_run)
                # The interrupted arm and every arm after it that was never set up
                # are not_run, with all their planned slots (section 12.5).
                for arm in ARMS[ARMS.index(setting_up) :]:
                    if arm in not_run or (arm != setting_up and arm in arm_records):
                        continue
                    reason = "setup interrupted by the operator"
                    if arm != setting_up:
                        reason += " before this arm"
                    not_run[arm] = reason
                    arm_records[arm] = {
                        "status": "not_run",
                        "reason": reason,
                        "slots_not_run": sum(1 for s in planned if s.arm == arm),
                    }
            else:
                stopped = slots[len(attempts)] if len(attempts) < len(slots) else None
                not_started = len(slots) - len(attempts)
            aborted = {
                "stage": "setup" if setting_up is not None else "attempt",
                "index": stopped.index if stopped else None,
                "repeat": stopped.repeat if stopped else None,
                "task_id": stopped.task_id if stopped else None,
                "arm": setting_up if setting_up is not None else (stopped.arm if stopped else None),
                "slots_not_run": not_started,
            }
            print(f"aborted ({aborted['stage']})", file=sys.stderr, flush=True)
    finally:
        for handle in handles.values():
            handle.stop()
        close = getattr(backend, "close_runtime", None)
        if close is not None:
            close()
        # The host homes hold the direct arm's credential and the stored token.
        shutil.rmtree(work, ignore_errors=True)
    # An arm without a record was not_run before setup; a failed setup marked its own.
    for arm in ARMS:
        arm_records.setdefault(arm, {"status": "not_run", "reason": not_run.get(arm)})
    summary = summarize(grades, arms=ARMS, not_run=not_run)
    document = {
        "kind": "relaymind.openclaw_evaluation",
        "evidence": evidence,
        "specification": "0.50 section 12.5",
        "command": command,
        "tasks": {
            "file": TASKS_FILE,
            "version": tasks.document["version"],
            "sha256": tasks.digest,
            "repeats": tasks.repeats,
            "sampling_seed": tasks.sampling_seed,
            "time_limit_seconds": tasks.time_limit_seconds,
            "selected": selected,
            "full_set": len(selected) == len(tasks.tasks),
        },
        "host_command": list(host_command),
        "node": node,
        "proxy_variables": sorted(proxy_variables()),
        "plugin_artifact": {"file": tarball.name, "sha256": _sha256(tarball)},
        "backend": backend.identity(),
        "machine": machine_manifest(),
        "application": application_revision(ROOT),
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "arms": {arm: arm_records[arm] for arm in ARMS},
        "attempts": attempts,
        "grades": grades,
        "summary": summary,
        "aborted": aborted,
    }
    places += [
        (work, "<work>"),
        (tarball.parent, "<artifact-dir>"),
        *((path, "<host-command-dir>") for path in _command_dirs(host_command)),
        (ROOT, "<repo>"),
        (Path.home(), "<home>"),
    ]
    text = _sanitize(json.dumps(document, indent=2, ensure_ascii=False), secrets, places)
    report = _sanitize(
        render_markdown(summary, title="OpenClaw integration evaluation")
        + _aborted_line(aborted)
        + f"\nEvidence: {evidence}. Machine: {json.dumps(document['machine'])}\n",
        secrets,
        places,
    )
    if any(secret in text or secret in report for secret in secrets):
        raise RuntimeError("a secret reached the export")
    out.mkdir(parents=True, exist_ok=True)
    (out / EXPORT_NAME).write_text(text + "\n", "utf-8")
    (out / REPORT_NAME).write_text(report, "utf-8")
    return json.loads(text)


# -- the live backend ------------------------------------------------------------------


class LiveBackend:
    """Gateways started with ``relaymind serve --config`` over the live models."""

    def __init__(self, *, external: Any, api_key: str | None, runtime: Any) -> None:
        import live_acceptance as harness

        self.harness = harness
        self.external = external
        self.api_key = api_key
        self.runtime = runtime
        self.reasons = arm_availability(
            external=external, local_model=runtime is not None, key_present=bool(api_key)
        )
        self._llama = None
        self._local_url = None
        self._cold_start = None

    def unavailable(self, arm: str) -> str | None:
        return self.reasons.get(arm)

    def direct(self) -> DirectEndpoint:
        e = self.external
        return DirectEndpoint(e.base_url, e.model, self.api_key or "", e.context_limit)

    def _local(self, work: Path) -> str:
        if self._llama is None:
            self._llama, self._local_url, self._cold_start = self.runtime.launch(
                work / "llama-server.log"
            )
        return self._local_url

    def start(self, arm: str, work: Path) -> GatewayHandle:
        harness = self.harness
        work.mkdir(parents=True)
        local = arm != "relaymind_no_local" and self.runtime is not None
        external = self.external if self.api_key else None
        modes = ["auto"]
        if external is not None:
            modes.append("external_only")
        if local:
            modes.append("local_only")
        config = work / "relaymind.toml"
        config.write_text(
            harness.gateway_config(
                work=work,
                manifest=self.runtime.manifest_path.resolve() if local else None,
                artifact=self.runtime.artifact if local else None,
                local_url=self._local(work.parent.parent) if local else None,
                port=harness._free_port(),
                external=external,
                modes=tuple(modes),
                local_model=local,
            ),
            "utf-8",
        )
        gateway = harness.GatewayProcess(
            config, work / "gateway.log", checkout_environment(ROOT, dict(os.environ))
        )
        output = gateway.start()
        store = work / "runs.sqlite3"

        def run_ids() -> list[str]:
            conn = _read_only(store)
            try:
                return [r[0] for r in conn.execute("SELECT run_id FROM runs ORDER BY rowid")]
            finally:
                conn.close()

        def run_records(ids: Sequence[str]) -> list[dict]:
            conn = _read_only(store)
            try:
                states = dict(conn.execute("SELECT run_id, state FROM runs"))
            finally:
                conn.close()
            traces = harness._traces(store, ids)
            return [{"run_id": i, "state": states.get(i), "events": traces[i]} for i in ids]

        return GatewayHandle(
            base_url=output.url.rstrip("/") + "/v1",
            token=output.token,
            run_ids=run_ids,
            run_records=run_records,
            stop=gateway.stop,
            launch={
                "arm": arm,
                "local_model": local,
                "external_model": None if external is None else external.model,
                "allowed_modes": modes,
                "launch_output": output.recorded_lines,
            },
        )

    def close_runtime(self) -> None:
        if self._llama is not None:
            self.harness._stop(self._llama)
            self._llama = None

    def secrets(self) -> list[str]:
        return [self.api_key] if self.api_key else []

    def identity(self) -> dict[str, Any]:
        e = self.external
        return {
            "backend": "live",
            "external": None
            if e is None
            else {"base_url": e.base_url, "model": e.model, "context_limit": e.context_limit},
            "local": None if self.runtime is None else self.runtime.model_record(),
            "runtime_banner": None if self.runtime is None else self.runtime.banner,
            "llama_server_cold_start_seconds": self._cold_start,
        }


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks", type=Path, default=ROOT / TASKS_FILE)
    parser.add_argument("--lock", type=Path, default=ROOT / LOCK_FILE)
    parser.add_argument("--tarball", type=Path, required=True, help="the plugin release artifact")
    parser.add_argument("--out", type=Path, required=True, help="the export directory")
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    parser.add_argument("--tasks-subset", nargs="+", metavar="ID", help="a labeled subset")
    parser.add_argument("--manifest", default="models/qwen3-0.6b-q8_0.manifest.json")
    parser.add_argument("--no-local-model", action="store_true")
    parser.add_argument("--external-base-url")
    parser.add_argument("--external-model")
    parser.add_argument("--external-context-limit", type=int)
    parser.add_argument("--local-context-limit", type=int, help="the plugin's limits copy")
    parser.add_argument("--max-completion-tokens", type=int, help="the plugin's limits copy")
    parser.add_argument("host", nargs=argparse.REMAINDER, help="-- <openclaw command>")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = argument_parser()
    args = parser.parse_args(argv)
    host = [a for a in args.host if a != "--"] if args.host[:1] == ["--"] else list(args.host)
    if not host:
        parser.error("give the OpenClaw command after --")
    try:
        tasks = load_tasks(args.tasks)
        check_lock(tasks, args.lock)
    except (TaskSetError, OSError, ValueError) as error:
        parser.error(str(error))
    external_args = (args.external_base_url, args.external_model, args.external_context_limit)
    if any(a is not None for a in external_args) and not all(a is not None for a in external_args):
        parser.error(
            "--external-base-url, --external-model, and --external-context-limit go together"
        )
    import live_acceptance as harness

    external = (
        harness.ExternalEndpoint(*external_args)
        if all(a is not None for a in external_args)
        else None
    )
    runtime = None
    if not args.no_local_model:
        try:
            runtime = harness.check_runtime(ROOT / args.manifest)
        except (SystemExit, Exception) as error:  # noqa: BLE001 - the auto arm is not_run then
            print(f"local model not usable: {error}", file=sys.stderr)
    limits = {
        key: value
        for key, value in (
            ("externalContextLimit", args.external_context_limit),
            ("localContextLimit", args.local_context_limit),
            ("maxCompletionTokens", args.max_completion_tokens),
        )
        if value is not None
    }
    backend = LiveBackend(
        external=external, api_key=os.environ.get(EXTERNAL_KEY_ENV), runtime=runtime
    )
    document = run_evaluation(
        tasks,
        arms=args.arms,
        host_command=host,
        tarball=args.tarball,
        backend=backend,
        out=args.out,
        evidence="live models through a real OpenClaw host (section 12.5); only arms marked ran"
        " are evidence",
        task_ids=args.tasks_subset,
        limits=limits,
        command=" ".join(["python", "scripts/run_openclaw_eval.py", *(argv or sys.argv[1:])]),
    )
    print((args.out / REPORT_NAME).read_text("utf-8"))
    if document.get("aborted"):
        return 130
    return 0 if document["grades"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
