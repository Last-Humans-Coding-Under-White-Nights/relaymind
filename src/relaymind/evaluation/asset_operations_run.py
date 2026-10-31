"""Operations and platform run kit (#242; RM38, RM39, RM44; AC29, AC37).

Development tooling around the existing AC29 drivers and the section 12.6.5
platform export. ``serve`` launches a real ``relaymind serve`` process over a
local skill directory. ``generate_catalog`` writes a bilingual development
catalog, and ``rss_bytes`` samples resident memory. ``platform_report`` turns
an installed-plugin lifecycle record (``scripts/openclaw_install_check.py``)
plus an optional ordinary installed-host capture into an
``AssetPlatformReportV1``.

Nothing here is the acceptance run. Developer machines and CI runners never
satisfy the Apple M1 8 GiB macOS / native x86-64 Windows gates, and the kit
refuses to label its own output ``measured``.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
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


def generate_catalog(root: Path, count: int, *, start: int = 0) -> list[Path]:
    """Write ``count`` distinct SKILL.md packages (alternating English/Chinese)."""
    root = Path(root)
    paths = []
    for n in range(start, start + count):
        topic, english, chinese = _TOPICS[n % len(_TOPICS)]
        name = f"{topic}-{n:04d}"
        description = english if n % 2 == 0 else chinese
        folder = root / name
        folder.mkdir(parents=True, exist_ok=False)
        path = folder / "SKILL.md"
        path.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n{description}\n",
            encoding="utf-8",
            newline="\n",
        )
        paths.append(path)
    return paths


def rss_bytes(pid: int) -> int | None:
    """Resident set size of ``pid`` in bytes, or None when unknown or gone."""
    if sys.platform == "win32":
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = result.stdout.strip()
    return int(text) * 1024 if result.returncode == 0 and text.isdigit() else None


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _config(work: Path, base: str, catalog: Path, port: int) -> Path:
    """The operator's base config plus the kit's server, recommendation and source tables."""
    if "[server]" in base or "[recommendations]" in base or "[[assets.sources]]" in base:
        raise ValueError("base_config_owns_kit_tables")
    path = work / "relaymind.toml"
    path.write_text(
        "\n".join(
            [
                "[server]",
                'host = "127.0.0.1"',
                f"port = {port}",
                base.strip(),
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


@contextlib.asynccontextmanager
async def serve(
    work: Path,
    catalog: Path,
    *,
    base_config: str,
    env: Mapping[str, str] | None = None,
    timeout: float = 60.0,
):
    """Start ``relaymind serve`` and yield ``(pid, client)``; stop it on exit.

    ``base_config`` is the operator's own TOML (store, skills, sandbox and
    model tables) without ``[server]``, ``[recommendations]`` or asset sources,
    which the kit owns so every run serves the generated catalog on loopback.
    """
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    config = _config(work, base_config, Path(catalog).resolve(), port)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from relaymind.launcher import cli; cli()",
            "serve",
            "--config",
            str(config),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=work,
        env={**os.environ, **(env or {})},
    )
    try:
        token = await asyncio.to_thread(_read_token, process, time.monotonic() + timeout)
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(
            base_url=base, headers={"Authorization": "Bearer " + token}, timeout=10
        ) as client:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    await client.get("/relay/v1/recommendations/readiness")
                    break
                except httpx.TransportError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("gateway_not_listening") from None
                    await asyncio.sleep(0.02)
            yield process.pid, client
    finally:
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 10)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)


def _read_token(process: subprocess.Popen, deadline: float) -> str:
    for line in process.stdout:
        if line.startswith("Application token: "):
            return line.removeprefix("Application token: ").strip()
        if time.monotonic() >= deadline:
            break
    raise TimeoutError("gateway_token_unavailable")


def _evidence(path: Path, evidence_id: str) -> dict:
    payload = Path(path).read_bytes()
    return dict(
        id=evidence_id,
        path=None,
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
        provenance="observed",
    )


_LIFECYCLE = {
    "install": ("install",),
    "configure": ("configure", "enable", "store-token"),
    "uninstall": ("disable", "uninstall", "remove-config", "remove-credential"),
}
_ORDINARY = {
    "recommend": "recommend",
    "choose": "choice",
    "feedback": "correction",
    "delete": "delete",
    "reset": "reset",
}


def platform_report(
    *,
    install_check: Path,
    ordinary: Path | None,
    input_digest: str,
    input_manifest: Mapping,
    platform: Mapping,
    qualification: str = "fixture",
) -> dict:
    """An ``AssetPlatformReportV1`` from actual lifecycle records.

    Steps without a record stay ``not_run`` with a fixed reason; restart and
    MCP use are not exercised by these records. The kit never emits
    ``measured``: only the actual acceptance platforms may, through review.
    """
    if qualification != "fixture":
        raise ValueError("measured_requires_acceptance_platform")
    check = json.loads(Path(install_check).read_text(encoding="utf-8"))
    statuses = {step["name"]: step["status"] for step in check["steps"]}
    evidence = [_evidence(install_check, "install-check")]
    capture = None
    if ordinary is not None:
        capture = json.loads(Path(ordinary).read_text(encoding="utf-8"))
        evidence.append(_evidence(ordinary, "ordinary-capture"))
    steps = []
    for name in PLATFORM_STEPS:
        status, ids = "not_run", []
        if name in _LIFECYCLE:
            observed = [statuses.get(step) for step in _LIFECYCLE[name]]
            if all(value == "passed" for value in observed) and (
                name != "uninstall" or not check.get("leftovers")
            ):
                status, ids = "passed", ["install-check"]
            elif any(value is not None for value in observed):
                status, ids = "failed", ["install-check"]
        elif capture is not None and name in _ORDINARY:
            snapshots = capture.get("ordinary_actions", {}).get("ui", {}).get("snapshots", {})
            if capture.get("checks", {}).get("ordinary_actions") is True and (
                _ORDINARY[name] in snapshots
            ):
                status, ids = "passed", ["ordinary-capture"]
        elif capture is not None and name == "skill_use":
            receipt = capture.get("ordinary_skill", {}).get("receipt", {})
            if capture.get("checks", {}).get("ordinary_skill") is True and receipt.get("ok"):
                status, ids = "passed", ["ordinary-capture"]
        steps.append(
            dict(
                name=name,
                status=status,
                evidence_ids=ids,
                reason=None if status == "passed" else f"{name}_{status}",
            )
        )
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
