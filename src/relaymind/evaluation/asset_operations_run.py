"""Operations and platform run kit (#242; RM38, RM39, RM44; AC29, AC37).

Development tooling around the existing AC29 startup driver and the section
12.6.5 platform export:

- ``serve`` launches a real ``relaymind serve`` process over a local skill
  directory and yields it once recommendations report ``ready``.
- ``generate_catalog`` writes a bilingual development catalog.
- ``rss_bytes`` samples resident memory (POSIX ``ps``) or working set
  (Windows ``tasklist``).
- ``platform_report`` turns an installed-plugin lifecycle record
  (``scripts/openclaw_install_check.py``) and an optional, independently
  checked ordinary installed-host capture into an ``AssetPlatformReportV1``.

Nothing here is the acceptance run. Developer machines and CI runners never
satisfy the Apple M1 8 GiB macOS / native x86-64 Windows gates, and the kit
refuses to label its own output ``measured``.
"""

import asyncio
import contextlib
import csv
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx

from relaymind.evaluation.asset_platform_export import PLATFORM_STEPS, platform_gate

_TOPICS = (
    ("greeting", "Greet the user politely.", "用礼貌的方式问候用户。"),
    ("calendar", "Show the user's calendar for a day.", "显示用户某一天的日程。"),
    ("weather", "Report the weather for a city.", "报告某个城市的天气。"),
    ("translate", "Translate a short text.", "翻译一段简短的文字。"),
    ("summary", "Summarize a document.", "总结一份文档。"),
    ("reminder", "Create a reminder.", "创建一个提醒。"),
)
_TOKEN_PREFIX = "Application token: "
_GRACE_SECONDS = 10.0


def generate_catalog(root: Path, count: int, *, start: int = 0) -> list[Path]:
    """Write ``count`` distinct SKILL.md packages (alternating English/Chinese)."""
    root = Path(root)
    paths = []
    for n in range(start, start + count):
        topic, english, chinese = _TOPICS[n % len(_TOPICS)]
        name = f"{topic}-{n:04d}"
        description = english if n % 2 == 0 else chinese
        folder = root / name
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "SKILL.md"
        path.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n{description}\n",
            encoding="utf-8",
            newline="\n",
        )
        paths.append(path)
    return paths


def memory_counter() -> str:
    """The counter ``rss_bytes`` reads on this platform."""
    return "tasklist_working_set" if sys.platform == "win32" else "ps_rss"


def rss_bytes(pid: int) -> int | None:
    """Resident memory of ``pid`` in bytes, or None when unknown or gone."""
    if sys.platform == "win32":
        argv = ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
    else:
        argv = ["ps", "-o", "rss=", "-p", str(pid)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=5)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    if sys.platform == "win32":
        # "image","pid","session","#","12,345 K"; no match prints an INFO line.
        try:
            for fields in csv.reader(text.splitlines(), strict=True):
                if len(fields) >= 5 and fields[1] == str(pid):
                    text = "".join(c for c in fields[-1] if c in "0123456789")
                    return int(text) * 1024 if text.isdigit() else None
            return None
        except csv.Error:
            return None
    return int(text) * 1024 if text.isdigit() else None


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


_PATH_FIELDS = {
    "store": ("path",),
    "sandbox": ("seed", "work_dir"),
    "skills": ("roots",),
    "local_model": ("manifest", "artifact"),
    "user_actions": ("registrations_path",),
    "asset_hybrid": ("manifest", "config"),
    "asset_adaptation": ("config",),
}


def _relative_paths(config: Mapping[str, object]) -> list[str]:
    """Known filesystem fields whose meaning would change on relocation."""
    relative = []
    for table, fields in _PATH_FIELDS.items():
        values = config.get(table, {})
        if not isinstance(values, dict):
            continue  # The launcher owns configuration type validation.
        for field in fields:
            value = values.get(field)
            paths = value if isinstance(value, list) else [value]
            if any(isinstance(p, str) and not Path(p).is_absolute() for p in paths):
                relative.append(f"{table}.{field}")
    return relative


def _config(work: Path, base_config: Path, catalog: Path, port: int) -> Path:
    """The operator's base config plus the kit's server, recommendation and source tables."""
    base = Path(base_config).read_text(encoding="utf-8")
    parsed = tomllib.loads(base)
    if {"server", "recommendations"} & set(parsed) or "sources" in parsed.get("assets", {}):
        raise ValueError("base_config_owns_kit_tables")
    # The generated file lives in the run directory, so a relative path in the
    # base config would silently change meaning; it is refused instead.
    relative = _relative_paths(parsed)
    if relative:
        raise ValueError("base_config_relative_path: " + ", ".join(relative))
    path = work / "relaymind.toml"
    path.write_text(
        "\n".join(
            [
                base.strip(),
                "[server]",
                'host = "127.0.0.1"',
                f"port = {port}",
                "[recommendations]",
                "enabled = true",
                'user_id = "operations"',
                'workspace_id = "development"',
                "[[assets.sources]]",
                'source_id = "operations"',
                'kind = "local_directory"',
                f"path = {json.dumps(str(catalog))}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _child_argv(config: Path) -> list[str]:
    return [
        sys.executable,
        "-c",
        "from relaymind.launcher import cli; cli()",
        "serve",
        "--config",
        str(config),
    ]


def _child_env(extra: Mapping[str, str] | None) -> dict[str, str]:
    env = {**os.environ, **(extra or {})}
    # The child inherits the caller's import path even when it is relative.
    if env.get("PYTHONPATH"):
        env["PYTHONPATH"] = os.pathsep.join(
            os.path.abspath(entry) for entry in env["PYTHONPATH"].split(os.pathsep) if entry
        )
    return env


async def _read_token(process: asyncio.subprocess.Process, deadline: float, log: Path) -> str:
    with log.open("ab") as target:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("gateway_token_unavailable")
            try:
                line = await asyncio.wait_for(process.stdout.readline(), remaining)
            except TimeoutError:
                raise TimeoutError("gateway_token_unavailable") from None
            if not line:
                raise TimeoutError("gateway_token_unavailable")
            text = line.decode("utf-8", "replace")
            if text.startswith(_TOKEN_PREFIX):
                if time.monotonic() >= deadline:
                    raise TimeoutError("gateway_token_unavailable")
                return text.removeprefix(_TOKEN_PREFIX).strip()
            target.write(line)
            target.flush()


async def _drain(stream: asyncio.StreamReader, log: Path) -> None:
    """Keep the child's stdout pipe from filling; the token line is never logged."""
    with log.open("ab") as target:
        while line := await stream.readline():
            if not line.startswith(_TOKEN_PREFIX.encode()):
                target.write(line)
                target.flush()


async def _wait_ready(
    client: httpx.AsyncClient, deadline: float, process: asyncio.subprocess.Process
) -> None:
    while True:
        try:
            response = await client.get("/relay/v1/recommendations/readiness")
            if response.status_code == 200:
                body = response.json()
                if isinstance(body, dict) and body.get("state") == "ready":
                    return
        except (httpx.HTTPError, ValueError):
            pass
        if process.returncode is not None:
            raise RuntimeError("gateway_exited")
        if time.monotonic() >= deadline:
            raise TimeoutError("gateway_not_ready")
        await asyncio.sleep(0.02)


async def _stop(process: asyncio.subprocess.Process) -> bool:
    """Terminate, kill after the grace period, and reap the child.

    Every wait is bounded: a process that ignores ``kill()`` is reported on
    stderr and never blocks shutdown. True means the child was reaped.
    """
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), _GRACE_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
    try:
        await asyncio.wait_for(process.wait(), _GRACE_SECONDS)
    except TimeoutError:
        print("relaymind: gateway process could not be reaped", file=sys.stderr)
        return False
    return True


@contextlib.asynccontextmanager
async def serve(
    work: Path,
    catalog: Path,
    *,
    base_config: Path,
    env: Mapping[str, str] | None = None,
    timeout: float = 60.0,
):
    """Start ``relaymind serve`` and yield ``(pid, client)`` once it is ready.

    ``base_config`` is the operator's own TOML (store, skills, sandbox and
    model tables, absolute paths only) without ``[server]``,
    ``[recommendations]`` or asset sources, which the kit owns. The child's
    stdout (without the token) and stderr go to ``stdout.log`` and
    ``stderr.log`` in ``work``. Both logs start clean on every launch, so a
    reused work directory never mixes two launches. On exit, or on
    cancellation at any point, the process is terminated, killed after a grace
    period, and reaped.
    """
    work = Path(work).absolute()
    work.mkdir(parents=True, exist_ok=True)
    stdout_log, stderr_log = work / "stdout.log", work / "stderr.log"
    stdout_log.unlink(missing_ok=True)
    stderr_log.unlink(missing_ok=True)
    port = _free_port()
    config = _config(work, base_config, Path(catalog).absolute(), port)
    deadline = time.monotonic() + timeout
    with stderr_log.open("ab") as stderr:
        process = await asyncio.create_subprocess_exec(
            *_child_argv(config),
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            env=_child_env(env),
        )
    drain = None
    try:
        token = await _read_token(process, deadline, stdout_log)
        drain = asyncio.create_task(_drain(process.stdout, stdout_log))
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer " + token},
            timeout=10,
        ) as client:
            await _wait_ready(client, deadline, process)
            yield process.pid, client
    finally:
        # Cleanup is owned: a cancellation arriving now still kills and reaps.
        cleanup = asyncio.ensure_future(_stop(process))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
        finally:
            if drain is not None:
                drain.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drain


# --- platform report -----------------------------------------------------------------


def _read_json(path: Path) -> tuple[object, bytes]:
    """The parsed document and the exact bytes it was parsed from (one read)."""
    payload = Path(path).read_bytes()
    return json.loads(payload.decode("utf-8")), payload


def _evidence(payload: bytes, evidence_id: str, served_path: str) -> dict:
    return dict(
        id=evidence_id,
        path=served_path,
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
        provenance="observed",
    )


# Platform step -> install-check steps that must all have passed.
_LIFECYCLE = {
    "install": ("install",),
    "configure": ("configure", "enable", "store-token"),
    # remove-credential passes only when every after_removal leftover is clean.
    "uninstall": ("disable", "uninstall", "remove-config", "remove-credential"),
}
_ORDINARY_ACTIONS = ("recommend", "choose", "feedback", "delete", "reset")
_FAILED = ("failed", "timed_out")


def _lifecycle_statuses(check: Mapping) -> dict:
    steps = check.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        raise ValueError("install_check_invalid")
    return {s.get("name"): s.get("status") for s in steps}


def _lifecycle_status(statuses: Mapping, names: tuple[str, ...]) -> str:
    observed = [statuses.get(name, "not_run") for name in names]
    if any(status in _FAILED for status in observed):
        return "failed"
    if all(status == "passed" for status in observed):
        return "passed"
    return "not_run"


def platform_report(
    *,
    install_check: Path,
    ordinary: Path | None,
    evidence_paths: Mapping[str, str],
    input_digest: str,
    input_manifest: Mapping,
    platform: Mapping,
    ordinary_check: Callable[[Mapping], bool] | None = None,
    qualification: str = "fixture",
) -> dict:
    """An ``AssetPlatformReportV1`` from actual lifecycle records.

    ``evidence_paths`` maps ``install-check`` (and ``ordinary-capture``) to the
    path registered for that original in the companion input manifest. The
    five ordinary actions pass only when ``ordinary_check`` (the installed-host
    qualification's own ``ordinary_pass``) accepts the capture. Skill use,
    restart and MCP use stay ``not_run``: a selection receipt is not observed
    instruction use. The kit never emits ``measured``.
    """
    if qualification != "fixture":
        raise ValueError("measured_requires_acceptance_platform")
    if ordinary is not None and ordinary_check is None:
        raise ValueError("ordinary_check_required")
    check, payload = _read_json(install_check)
    if not isinstance(check, dict):
        raise ValueError("install_check_invalid")
    evidence = [_evidence(payload, "install-check", evidence_paths["install-check"])]
    ordinary_ok = False
    if ordinary is not None:
        capture, payload = _read_json(ordinary)
        ordinary_ok = isinstance(capture, dict) and ordinary_check(capture) is True
        evidence.append(_evidence(payload, "ordinary-capture", evidence_paths["ordinary-capture"]))
    statuses = _lifecycle_statuses(check)
    steps = []
    for name in PLATFORM_STEPS:
        status, ids, reason = "not_run", [], f"{name}_not_run"
        if name in _LIFECYCLE:
            status = _lifecycle_status(statuses, _LIFECYCLE[name])
            ids = [] if status == "not_run" else ["install-check"]
            reason = None if status == "passed" else f"{name}_{status}"
        elif name in _ORDINARY_ACTIONS:
            if ordinary_ok:
                status, ids, reason = "passed", ["ordinary-capture"], None
            elif ordinary is not None:
                reason = "ordinary_capture_unverified"
        elif name == "skill_use":
            reason = "skill_use_not_observed"
        steps.append(dict(name=name, status=status, evidence_ids=ids, reason=reason))
    report = dict(
        schema_version=1,
        qualification=qualification,
        input_digest=input_digest,
        input_manifest=dict(input_manifest),
        platform=dict(platform),
        artifacts=[],
        steps=steps,
        operations_evidence=None,
        approvals_evidence=[],
        evidence=sorted(evidence, key=lambda e: e["id"].encode()),
        gate=None,
    )
    report["gate"] = platform_gate(report)
    return report
