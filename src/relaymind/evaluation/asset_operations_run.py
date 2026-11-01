"""Operations and platform run kit (#242; RM38, RM39, RM44; AC29, AC37).

Development tooling around the existing AC29 startup driver and the section
12.6.5 platform export:

- ``serve`` launches a real ``relaymind serve`` process over a local skill
  directory and yields it once recommendations report ``ready``.
- ``generate_catalog`` writes a bilingual development catalog.
- ``rss_bytes`` samples resident memory (POSIX ``ps``) or working set
  (Windows ``tasklist``).
- ``serve_controlled`` adds the evaluation-only control channel of
  ``asset_operations_child`` (source rescan and derived rebuild), and
  ``workload_plan`` builds the 60-second AC29 workload over it (#241);
  ``serve_baseline`` starts the gateway without recommendations for the
  memory baseline.
- ``operations_report`` keeps the samples next to the unchanged
  ``summarize_operations`` result, and ``load_operations_report`` reads them
  back so a downloaded report can be graded again.
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
import dataclasses
import hashlib
import itertools
import json
import os
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx

from relaymind.evaluation.asset_operations import (
    BatchSample,
    ImportBatch,
    MemoryObservation,
    RequestSample,
    StartupSample,
    VisibilityProbe,
    WorkloadSamples,
    run_workload,
    summarize_operations,
)
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
_PID_PREFIX = "Gateway-Pid: "
_PID_ENTRY = "import os; print('Gateway-Pid: ' + str(os.getpid()), flush=True); "
_CONTROL_PREFIX = b"Control-Reply: "
_CONTROL_SECONDS = 60.0
_GRACE_SECONDS = 10.0
SOURCE_ID = "operations"
IMPORT_SOURCE_ID = "operations-import"
UNUSED_KEY_ENV = "RELAYMIND_OPERATIONS_UNUSED_KEY"
# Fixed reasons a gateway launch raises; anything else is named by its type.
_LAUNCH_REASONS = frozenset(
    {
        "gateway_exited",
        "gateway_not_ready",
        "gateway_token_unavailable",
        "gateway_pid_invalid",
        "gateway_pid_missing",
        "control_closed",
    }
)
LAUNCH_ERRORS = (OSError, TimeoutError, RuntimeError, ValueError)


def launch_failure(phase: str, error: BaseException) -> str:
    """A fixed harness-error name for a gateway that did not start or answer."""
    reason = str(error) if str(error) in _LAUNCH_REASONS else type(error).__name__
    return f"{phase}_{reason}"


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


def _config(
    work: Path,
    base_config: Path,
    catalog: Path | None,
    port: int,
    *,
    recommendations=True,
    imports: Path | None = None,
) -> Path:
    """The operator's base config plus the kit's server, recommendation and source tables.

    Without recommendations (the AC29 memory baseline) the kit writes only its
    server table and ``[recommendations] enabled = false``.
    """
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
    lines = [base.strip(), "[server]", 'host = "127.0.0.1"', f"port = {port}", "[recommendations]"]
    if recommendations:
        lines += [
            "enabled = true",
            'user_id = "operations"',
            'workspace_id = "development"',
            "[[assets.sources]]",
            f'source_id = "{SOURCE_ID}"',
            'kind = "local_directory"',
            f"path = {json.dumps(str(catalog))}",
        ]
        if imports is not None:
            lines += [
                "[[assets.sources]]",
                f'source_id = "{IMPORT_SOURCE_ID}"',
                'kind = "metadata_import"',
                f"path = {json.dumps(str(imports / 'import.json'))}",
            ]
    else:
        lines.append("enabled = false")
    path.write_text("\n".join([*lines, ""]), encoding="utf-8")
    return path


def _child_argv(config: Path) -> list[str]:
    return [
        sys.executable,
        "-c",
        _PID_ENTRY + "from relaymind.launcher import cli; cli()",
        "serve",
        "--config",
        str(config),
    ]


def _controlled_argv(config: Path) -> list[str]:
    entry = "from relaymind.evaluation.asset_operations_child import main; raise SystemExit(main())"
    return [sys.executable, "-c", _PID_ENTRY + entry, "serve", "--config", str(config)]


def _child_env(extra: Mapping[str, str] | None) -> dict[str, str]:
    env = {**os.environ, **(extra or {})}
    # The child inherits the caller's import path even when it is relative.
    if env.get("PYTHONPATH"):
        env["PYTHONPATH"] = os.pathsep.join(
            os.path.abspath(entry) for entry in env["PYTHONPATH"].split(os.pathsep) if entry
        )
    return env


async def _read_token(
    process: asyncio.subprocess.Process, deadline: float, log: Path
) -> tuple[str, int]:
    pid = None
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
            if text.startswith(_PID_PREFIX):
                value = text.removeprefix(_PID_PREFIX).strip()
                if (
                    not value.isascii()
                    or not value.isdecimal()
                    or int(value) <= 0
                    or pid is not None
                ):
                    raise ValueError("gateway_pid_invalid")
                pid = int(value)
            elif text.startswith(_TOKEN_PREFIX):
                if time.monotonic() >= deadline:
                    raise TimeoutError("gateway_token_unavailable")
                if pid is None:
                    raise ValueError("gateway_pid_missing")
                return text.removeprefix(_TOKEN_PREFIX).strip(), pid
            else:
                target.write(line)
                target.flush()


async def _drain(stream: asyncio.StreamReader, log: Path, replies: dict | None = None) -> None:
    """Keep the child's stdout pipe from filling; the token line is never logged.

    Control replies go to their waiting request, not to the log. When the
    stream ends, a request still waiting fails instead of waiting forever.
    """
    try:
        with log.open("ab") as target:
            while line := await stream.readline():
                if replies is not None and line.startswith(_CONTROL_PREFIX):
                    try:
                        reply = json.loads(line[len(_CONTROL_PREFIX) :])
                        future = replies.get(reply.get("id"))
                    except (ValueError, AttributeError, TypeError):
                        future = None
                    if future is not None and not future.done():
                        future.set_result(reply)
                elif not line.startswith(_TOKEN_PREFIX.encode()):
                    target.write(line)
                    target.flush()
    finally:
        for future in (replies or {}).values():
            if not future.done():
                future.set_exception(ConnectionError("control_closed"))


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


async def _wait_serving(
    client: httpx.AsyncClient, deadline: float, process: asyncio.subprocess.Process
) -> None:
    """A gateway without recommendations is ready once it answers its model list."""
    while True:
        try:
            if (await client.get("/v1/models")).status_code == 200:
                return
        except httpx.HTTPError:
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


@dataclasses.dataclass(frozen=True)
class Gateway:
    """A running gateway child: its PID, an authenticated client and its control call."""

    pid: int
    client: httpx.AsyncClient
    control: Callable[..., Awaitable[dict]]


@contextlib.asynccontextmanager
async def _gateway(
    work: Path,
    catalog: Path | None,
    *,
    base_config: Path,
    env: Mapping[str, str] | None,
    timeout: float,
    controlled: bool = False,
    recommendations: bool = True,
    imports: Path | None = None,
):
    work = Path(work).absolute()
    work.mkdir(parents=True, exist_ok=True)
    # Both logs start clean on every launch, so a reused work directory never
    # mixes two launches.
    stdout_log, stderr_log = work / "stdout.log", work / "stderr.log"
    stdout_log.unlink(missing_ok=True)
    stderr_log.unlink(missing_ok=True)
    port = _free_port()
    catalog = None if catalog is None else Path(catalog).absolute()
    imports = None if imports is None else Path(imports).absolute()
    config = _config(
        work, base_config, catalog, port, recommendations=recommendations, imports=imports
    )
    deadline = time.monotonic() + timeout
    with stderr_log.open("ab") as stderr:
        process = await asyncio.create_subprocess_exec(
            *(_controlled_argv if controlled else _child_argv)(config),
            stdin=asyncio.subprocess.PIPE if controlled else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            env=_child_env(env),
        )
    drain = None
    replies: dict[int, asyncio.Future] = {}
    ordinals = itertools.count()

    async def control(op: str, **fields) -> dict:
        if not controlled:
            raise RuntimeError("control_unavailable")
        ordinal = next(ordinals)
        future = asyncio.get_running_loop().create_future()
        replies[ordinal] = future
        try:
            line = json.dumps(dict(fields, id=ordinal, op=op), ensure_ascii=True) + "\n"
            process.stdin.write(line.encode("ascii"))
            await process.stdin.drain()
            return await asyncio.wait_for(future, _CONTROL_SECONDS)
        finally:
            replies.pop(ordinal, None)

    try:
        token, pid = await _read_token(process, deadline, stdout_log)
        drain = asyncio.create_task(_drain(process.stdout, stdout_log, replies))
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer " + token},
            timeout=10,
            trust_env=False,
        ) as client:
            await (_wait_ready if recommendations else _wait_serving)(client, deadline, process)
            yield Gateway(pid, client, control)
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
    async with _gateway(work, catalog, base_config=base_config, env=env, timeout=timeout) as gw:
        yield gw.pid, gw.client


@contextlib.asynccontextmanager
async def serve_controlled(
    work: Path,
    catalog: Path,
    *,
    base_config: Path,
    imports: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 60.0,
):
    """As ``serve``, through ``asset_operations_child``; yields a ``Gateway``.

    ``imports`` adds the ``operations-import`` metadata import source over
    ``imports/import.json`` (see ``write_import``).
    ``await gateway.control("rescan", source_id=...)`` and
    ``await gateway.control("rebuild")`` return the child's reply.
    """
    async with _gateway(
        work,
        catalog,
        base_config=base_config,
        env=env,
        timeout=timeout,
        controlled=True,
        imports=imports,
    ) as gateway:
        yield gateway


@contextlib.asynccontextmanager
async def serve_baseline(
    work: Path,
    *,
    base_config: Path,
    env: Mapping[str, str] | None = None,
    timeout: float = 60.0,
):
    """The same gateway with recommendations disabled; yields ``(pid, client)``."""
    async with _gateway(
        work, None, base_config=base_config, env=env, timeout=timeout, recommendations=False
    ) as gateway:
        yield gateway.pid, gateway.client


# --- development configuration and runner identity ----------------------------------


def development_base_config(work: Path, root: Path) -> Path:
    """A disposable base config for runner runs: its own store, absolute paths only.

    The external model table names an unused endpoint and the key variable
    ``RELAYMIND_OPERATIONS_UNUSED_KEY``; lexical recommendations never call it.
    """
    work, root = Path(work).absolute(), Path(root).absolute()
    work.mkdir(parents=True, exist_ok=True)
    path = work / "base.toml"
    lines = [
        "[store]",
        f"path = {json.dumps(str(work / 'runs.sqlite3'))}",
        "[skills]",
        f"roots = [{json.dumps(str(root / 'skills'))}]",
        "[sandbox]",
        f"seed = {json.dumps(str(root / 'tests/fixtures/sandbox/baseline.json'))}",
        f"work_dir = {json.dumps(str(work / 'sandbox'))}",
        "[external_model]",
        'base_url = "https://example.invalid/v1"',
        'model = "unused"',
        "context_limit = 16000",
        f'api_key_env = "{UNUSED_KEY_ENV}"',
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


_RUNNER_VARIABLES = (
    "RUNNER_OS",
    "RUNNER_ARCH",
    "RUNNER_ENVIRONMENT",
    "ImageOS",
    "ImageVersion",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_SHA",
    "GITHUB_REF",
)


def runner_identity(label: str | None) -> dict:
    """The operator's label and the CI runner's own variables, when present."""
    return dict(label=label, variables={k: os.environ.get(k) for k in _RUNNER_VARIABLES})


# --- the AC29 workload ------------------------------------------------------------------

# Batch offsets (seconds after the workload origin); the first batch arrives
# with the derived rebuild so their work can overlap.
IMPORT_OFFSETS = (0.0, 15.0, 30.0, 45.0)
_BATCH_RECORDS = 25
_GROUP_RECORDS = 5  # Five records share one probe token; the candidate limit is 10.


@dataclasses.dataclass(frozen=True)
class ImportPackage:
    name: str
    token: str
    language: str
    description: str
    payload: bytes
    identity: tuple[str, str, None]

    @property
    def original_path(self) -> str:
        return f"originals/{self.name}.md"


def _token(batch: int, group: int) -> str:
    return "opsvisibility" + "abcd"[batch] + "abcde"[group] + "marker"


def import_batch_packages(batch: int) -> list[ImportPackage]:
    """The 25 bilingual metadata records of one import batch, with fixed original bytes.

    The identity is what the ``metadata_import`` source publishes: the record's
    asset ID, the SHA-256 of its original bytes and no source version.
    """
    packages = []
    for n in range(_BATCH_RECORDS):
        token = _token(batch, n // _GROUP_RECORDS)
        name = f"import-{batch}-{n:02d}"
        english = n % 2 == 0
        description = (
            f"Imported operations record {token} number {n}."
            if english
            else f"导入的运营记录 {token} 第 {n} 号。"
        )
        data = f"---\nname: {name}\ndescription: {description}\n---\n{description}\n".encode()
        identity = (f"skill:{IMPORT_SOURCE_ID}-{name}", hashlib.sha256(data).hexdigest(), None)
        packages.append(
            ImportPackage(name, token, "en" if english else "zh", description, data, identity)
        )
    return packages


def _import_document(packages: Sequence[ImportPackage], captured_at: str) -> dict:
    return dict(
        schema_version=1,
        source_id=IMPORT_SOURCE_ID,
        source_version=None,
        captured_at=captured_at,
        complete=not packages,  # The empty start is complete; each batch only adds.
        records=[
            dict(
                record=dict(
                    asset_id=p.identity[0],
                    kind="skill",
                    source_locator=f"{IMPORT_SOURCE_ID}/{p.name}",
                    source_version=None,
                    captured_at=captured_at,
                    content_hash=p.identity[1],
                    description=p.description,
                    language_tags=[p.language],
                    dependencies=[],
                    identity=dict(package_id=p.name, package_version=None),
                ),
                original_path=p.original_path,
            )
            for p in packages
        ],
        removed_asset_ids=[],
    )


def write_import(imports: Path, packages: Sequence[ImportPackage]) -> Path:
    """Write the originals, then replace ``import.json`` in one step."""
    imports = Path(imports)
    (imports / "originals").mkdir(parents=True, exist_ok=True)
    for package in packages:
        (imports / package.original_path).write_bytes(package.payload)
    captured = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    target, staged = imports / "import.json", imports / "import.json.tmp"
    staged.write_text(json.dumps(_import_document(packages, captured)), encoding="utf-8")
    os.replace(staged, target)
    return target


def workload_bodies() -> list[dict]:
    """120 scheduled discovery requests over the generated catalog's topics."""
    bodies = []
    for n in range(120):
        _topic, english, chinese = _TOPICS[n % len(_TOPICS)]
        bodies.append(
            dict(
                schema_version=1,
                task_text=english if n % 2 == 0 else chinese,
                requested_kinds=["skill"],
                selection_scope="discovery",
            )
        )
    return bodies


@dataclasses.dataclass
class WorkloadPlan:
    """The four import batches and the rebuild hook over one controlled gateway."""

    batches: list[ImportBatch]
    rebuild: Callable[[], Awaitable[object]]
    batch_job_ids: dict[str, str]
    jobs: list[dict]
    channel_errors: list[str]
    rebuild_job_id: list[str]

    def job_evidence(self) -> list[dict]:
        return list(self.jobs)

    def identities(self) -> dict:
        return dict(
            rebuild_job_id=self.rebuild_job_id[0] if self.rebuild_job_id else None,
            batch_job_ids=dict(self.batch_job_ids),
        )


def workload_plan(
    imports: Path,
    control: Callable[..., Awaitable[dict]],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> WorkloadPlan:
    """Receive writes a batch as an incremental metadata import; rescan asks the gateway.

    A refused rescan or rebuild is a product outcome and fails its batch or
    rebuild in the driver. A control call that never gets a reply is a harness
    error, recorded in ``channel_errors``.
    """
    imports = Path(imports)
    batch_job_ids: dict[str, str] = {}
    jobs: list[dict] = []
    channel_errors: list[str] = []
    rebuild_job_id: list[str] = []

    async def call(op: str, **fields) -> dict:
        try:
            reply = await control(op, **fields)
        except (ConnectionError, TimeoutError, OSError, RuntimeError) as error:
            channel_errors.append(f"{op}_{type(error).__name__}")
            raise
        if not reply.get("ok"):
            raise RuntimeError(f"{op}_refused")
        jobs.extend(reply.get("jobs") or ())
        return reply

    def make(batch: int) -> ImportBatch:
        batch_id = f"batch-{batch}"
        packages = import_batch_packages(batch)
        groups: dict[str, list[ImportPackage]] = {}
        for package in packages:
            groups.setdefault(package.token, []).append(package)
        probes = tuple(
            VisibilityProbe(
                dict(
                    schema_version=1,
                    task_text=token,
                    requested_kinds=["skill"],
                    selection_scope="discovery",
                ),
                tuple(p.identity for p in members),
            )
            for token, members in groups.items()
        )

        async def receive() -> float:
            write_import(imports, packages)
            return clock()

        async def rescan() -> None:
            reply = await call("rescan", source_id=IMPORT_SOURCE_ID)
            if reply.get("jobs"):
                batch_job_ids[batch_id] = reply["jobs"][0]["job_id"]

        return ImportBatch(batch_id, IMPORT_OFFSETS[batch], probes, receive, rescan)

    async def rebuild() -> None:
        reply = await call("rebuild")
        if reply.get("jobs"):
            rebuild_job_id[:] = [reply["jobs"][0]["job_id"]]

    return WorkloadPlan(
        [make(b) for b in range(4)], rebuild, batch_job_ids, jobs, channel_errors, rebuild_job_id
    )


async def run_operations_workload(
    gateway: Gateway, imports: Path, samples: WorkloadSamples
) -> WorkloadPlan:
    """The fixed 60-second schedule of ``run_workload`` against a controlled gateway."""
    plan = workload_plan(imports, gateway.control)
    await run_workload(
        gateway.client,
        workload_bodies(),
        batches=plan.batches,
        rebuild=plan.rebuild,
        job_evidence=plan.job_evidence,
        samples=samples,
    )
    samples.identities = plan.identities()
    return plan


class MemorySampler:
    """Periodic resident memory samples of one process; unknown stays None."""

    def __init__(self, pid: int, *, interval: float = 0.25):
        self.pid = pid
        self.interval = interval
        self.samples: list[int | None] = []

    def sample(self) -> None:
        self.samples.append(rss_bytes(self.pid))

    @contextlib.asynccontextmanager
    async def running(self):
        async def loop():
            while True:
                await asyncio.to_thread(self.sample)
                await asyncio.sleep(self.interval)

        task = asyncio.create_task(loop())
        try:
            yield self
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await asyncio.to_thread(self.sample)

    @property
    def peak(self) -> int | None:
        known = [s for s in self.samples if s is not None]
        return max(known) if known else None

    @property
    def complete(self) -> bool:
        return bool(self.samples) and None not in self.samples


_WORKLOAD_PHASES = ("rebuild", "import", "query")


def memory_observation(
    baseline_bytes: int | None,
    startup_bytes: Sequence[int | None],
    workload_bytes: Sequence[int | None],
) -> MemoryObservation:
    """Gateway-only memory with the phases that were actually sampled.

    Startup counts only when all ten startup samples are known, and then its
    samples join the peak. The workload samples cover rebuild, import and
    query, which overlap in the 60-second run. Complete needs the baseline,
    every startup sample and at least one known workload sample, all known.
    """
    startup_known = len(startup_bytes) == 10 and None not in startup_bytes
    workload_known = bool(workload_bytes) and None not in workload_bytes
    phases = (("startup",) if startup_known else ()) + (_WORKLOAD_PHASES if workload_bytes else ())
    known = [
        b for b in (*workload_bytes, *(startup_bytes if startup_known else ())) if b is not None
    ]
    return MemoryObservation(
        baseline_bytes=baseline_bytes,
        peak_bytes=max(known) if known else None,
        counter_source=memory_counter(),
        # Lexical recommendations start no child process or embedding device here;
        # those coverages are not measured, so the grader leaves memory unknown.
        coverage=("gateway",),
        phases=phases,
        complete=baseline_bytes is not None and startup_known and workload_known,
    )


# --- the operations report ----------------------------------------------------------------

# Section 12.6 bounds, listed for reading next to the numbers. The grader's
# ``passes`` fields decide; a fixture report never passes them.
BOUNDS = dict(
    launches=10,
    startup_to_first_lexical_ms=1000,
    batches=4,
    batch_records=25,
    receipt_to_visibility_ms=1000,
    workload_seconds=60,
    requests_per_second=2,
    concurrency=1,
    service_p95_ms=300,
    scheduled_p95_ms=300,
    slot_deadline_ms=500,
    incremental_memory_bytes=1024**3,
)


def _workload_dict(workload: WorkloadSamples) -> dict:
    value = dataclasses.asdict(workload)
    if workload.origin_utc is not None:
        value["origin_utc"] = workload.origin_utc.isoformat()
    return value


def operations_report(
    startups: Sequence[StartupSample],
    workload: WorkloadSamples,
    memory: MemoryObservation | None,
    *,
    environment: Mapping,
    runner: Mapping,
    catalog: Mapping,
    memory_samples: Mapping,
    harness_errors: Sequence[str],
) -> dict:
    """The samples and the unchanged ``summarize_operations`` result, labeled fixture.

    Spec 12.6.5: CI runners and developer machines never satisfy the AC29
    platform gates, so the kit refuses a report its caller labeled measured.
    """
    if workload.provenance != "fixture" or any(s.provenance != "fixture" for s in startups):
        raise ValueError("measured_requires_acceptance_platform")
    return dict(
        kind="asset-operations-run",
        schema_version=1,
        qualification="fixture",
        acceptance="not_applicable",
        acceptance_reason="not_an_acceptance_platform",
        environment=dict(environment),
        runner=dict(runner),
        catalog=dict(catalog),
        bounds=BOUNDS,
        startups=[dataclasses.asdict(s) for s in startups],
        workload=_workload_dict(workload),
        memory=None if memory is None else dataclasses.asdict(memory),
        memory_samples=dict(memory_samples),
        harness_errors=list(harness_errors),
        summary=summarize_operations(startups, workload, memory),
    )


def _request_sample(value: Mapping) -> RequestSample:
    return RequestSample(**dict(value, events=tuple(value["events"])))


def _startup_sample(value: Mapping) -> StartupSample:
    attempts = tuple(_request_sample(a) for a in value["attempts"])
    return StartupSample(**dict(value, attempts=attempts))


def _batch_sample(value: Mapping) -> BatchSample:
    return BatchSample(
        **dict(
            value,
            probes=tuple(_request_sample(p) for p in value["probes"]),
            expected=tuple(tuple(identity) for identity in value["expected"]),
        )
    )


def load_operations_report(
    report: Mapping,
) -> tuple[list[StartupSample], WorkloadSamples, MemoryObservation | None]:
    """The samples of a stored report, ready for ``summarize_operations`` again."""
    raw = report["workload"]
    origin_utc = raw["origin_utc"]
    workload = WorkloadSamples(
        provenance=raw["provenance"],
        identities=raw["identities"],
        environment=raw["environment"],
        requests=[_request_sample(r) for r in raw["requests"]],
        batches=[_batch_sample(b) for b in raw["batches"]],
        rebuild=raw["rebuild"],
        jobs=[
            dict(job, events=tuple(job["events"])) if "events" in job else dict(job)
            for job in raw["jobs"]
        ],
        errors=list(raw["errors"]),
        origin_at=raw["origin_at"],
        origin_utc=None if origin_utc is None else datetime.fromisoformat(origin_utc),
    )
    memory = report["memory"]
    if memory is not None:
        memory = MemoryObservation(
            **dict(memory, coverage=tuple(memory["coverage"]), phases=tuple(memory["phases"]))
        )
    return [_startup_sample(s) for s in report["startups"]], workload, memory


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
# Native UI action of the ordinary harness -> platform step it exercises.
_UI_STEPS = {
    "recommend": "recommend",
    "choose": "choose",
    "correct": "feedback",
    "inspect": "feedback",
    "delete": "delete",
    "reset": "reset",
}
# Error classes that mean the UI action itself did not complete; a closed
# browser or an unclassified error is a harness failure.
_PRODUCT_ERROR_CLASSES = frozenset({"value_error", "playwright_timeout"})


def ordinary_product_failure(capture: Mapping) -> str | None:
    """The UI action of a recorded ordinary product failure, or None.

    A product failure is a failed capture whose setup completed and whose
    failure happened inside one of the recommendation UI actions (the harness
    records ``ordinary_failure_step == "ui_actions"`` and the action). Anything
    else (setup, a browser crash, an unreadable capture) is not one.
    """
    if not isinstance(capture, Mapping) or capture.get("status") != "failed":
        return None
    action = capture.get("ordinary_ui_action")
    if (
        capture.get("ordinary_failure_step") == "ui_actions"
        and action in _UI_STEPS
        and capture.get("ordinary_error_class") in _PRODUCT_ERROR_CLASSES
    ):
        return action
    return None


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
    operations: Path | None = None,
    qualification: str = "fixture",
) -> dict:
    """An ``AssetPlatformReportV1`` from actual lifecycle records.

    ``evidence_paths`` maps ``install-check`` (and ``ordinary-capture``,
    ``operations-report``) to the path registered for that original in the
    companion input manifest. The five ordinary actions pass only when
    ``ordinary_check`` (the installed-host qualification's own
    ``ordinary_pass``) accepts the capture; a capture of the Linux-only
    person-ingress harness on another platform keeps them ``not_run`` with
    ``ordinary_harness_linux_only``. Skill use, MCP use and restart stay
    ``not_run``: a selection receipt is not observed instruction use, MCP use
    remains disabled (spec 7.6.1/7.6.2), and the lifecycle record has no
    restart. An operations report becomes ``operations_evidence``. The kit
    never emits ``measured``.
    """
    if qualification != "fixture":
        raise ValueError("measured_requires_acceptance_platform")
    if ordinary is not None and ordinary_check is None:
        raise ValueError("ordinary_check_required")
    check, payload = _read_json(install_check)
    if not isinstance(check, dict):
        raise ValueError("install_check_invalid")
    evidence = [_evidence(payload, "install-check", evidence_paths["install-check"])]
    ordinary_ok, linux_only, failed_action = False, False, None
    if ordinary is not None:
        capture, payload = _read_json(ordinary)
        ordinary_ok = isinstance(capture, dict) and ordinary_check(capture) is True
        linux_only = isinstance(capture, dict) and capture.get("reason") == "linux_runner_required"
        failed_action = None if ordinary_ok else ordinary_product_failure(capture)
        evidence.append(_evidence(payload, "ordinary-capture", evidence_paths["ordinary-capture"]))
    operations_entry = None
    if operations is not None:
        operations_entry = _evidence(
            Path(operations).read_bytes(), "operations-report", evidence_paths["operations-report"]
        )
        evidence.append(operations_entry)
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
            elif failed_action is not None and _UI_STEPS[failed_action] == name:
                status, ids = "failed", ["ordinary-capture"]
                reason = f"ordinary_{failed_action}_failed"
            elif linux_only:
                reason = "ordinary_harness_linux_only"
            elif ordinary is not None:
                reason = "ordinary_capture_unverified"
        elif name == "skill_use":
            reason = "skill_use_not_observed"
        elif name == "mcp_use":
            reason = "mcp_use_disabled"
        elif name == "restart":
            reason = "restart_not_observed"
        steps.append(dict(name=name, status=status, evidence_ids=ids, reason=reason))
    report = dict(
        schema_version=1,
        qualification=qualification,
        input_digest=input_digest,
        input_manifest=dict(input_manifest),
        platform=dict(platform),
        artifacts=[],
        steps=steps,
        operations_evidence=None if operations_entry is None else dict(operations_entry),
        approvals_evidence=[],
        evidence=sorted(evidence, key=lambda e: e["id"].encode()),
        gate=None,
    )
    report["gate"] = platform_gate(report)
    return report
