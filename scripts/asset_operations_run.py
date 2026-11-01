"""Operations and platform run kit (#242; RM38, RM39, RM44; AC29, AC37).

    startup     ten real ``relaymind serve`` startups over a generated catalog,
                measured by the existing AC29 driver, with memory samples
    operations  the lexical portion of the AC29 protocol as a fixture (#241;
                lexical-only evidence never closes AC29): ten clean startups, the gateway without
                recommendations as memory baseline, and the 60-second 2 req/s
                workload with four 25-record import batches during a derived
                rebuild, summarized by the unchanged ``summarize_operations``
    platform    an AssetPlatformReportV1 from an install-check record and an
                optional ordinary installed-host capture and operations
                report, bound to the companion input manifest

Development tooling only. Output is labeled ``fixture``; a developer machine
or CI runner never satisfies the macOS / native Windows acceptance gates.
"""

import argparse
import asyncio
import contextlib
import dataclasses
import hashlib
import importlib.util
import json
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_evidence import collect_benchmark_environment
from relaymind.evaluation.asset_operations import (
    StartupSample,
    WorkloadSamples,
    measure_startups,
)
from relaymind.evaluation.asset_operations_run import (
    LAUNCH_ERRORS,
    UNUSED_KEY_ENV,
    MemorySampler,
    development_base_config,
    generate_catalog,
    launch_failure,
    memory_counter,
    memory_observation,
    operations_report,
    platform_report,
    rss_bytes,
    run_operations_workload,
    runner_identity,
    serve,
    serve_baseline,
    serve_controlled,
    write_import,
)
from relaymind.evaluation.asset_whole_task import (
    WholeTaskRejected,
    validate_whole_task,
    verify_host_tasks,
)

ROOT = Path(__file__).resolve().parents[1]
ORDINARY_QUALIFY = ROOT / "integrations/openclaw/test/host-ordinary-qualification/qualify.py"
BODY = dict(
    schema_version=1, task_text="greeting", requested_kinds=["skill"], selection_scope="discovery"
)


def ordinary_check() -> Callable[[Mapping], bool]:
    """The installed-host qualification's own ``ordinary_pass``, never a copy."""
    spec = importlib.util.spec_from_file_location("ordinary_qualify", ORDINARY_QUALIFY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def check(capture: Mapping) -> bool:
        try:
            return module.ordinary_pass(capture) is True
        except Exception:
            return False

    return check


def startup_report(
    samples: Sequence[StartupSample],
    memory: Sequence[int | None],
    *,
    assets: int,
    base_config_digest: str,
    counter: str,
    harness_errors: Sequence[str] = (),
) -> dict:
    useful = sum(sample.outcome == "useful" for sample in samples)
    return dict(
        kind="asset-operations-startup",
        qualification="fixture",
        assets=assets,
        base_config_digest=base_config_digest,
        memory_counter=counter,
        samples=[dataclasses.asdict(sample) for sample in samples],
        memory_bytes=list(memory),
        useful_startups=useful,
        harness_errors=list(harness_errors),
        # Complete means all ten startups were useful and all ten memory samples known.
        complete=useful == 10 and len(samples) == len(memory) == 10 and None not in memory,
    )


def fresh_directories(work: Path, names: Sequence[str]) -> None:
    """Remove the kit's own subdirectories of ``work`` so a reused work directory
    starts with exactly the requested catalog and new empty stores.

    Only the named, kit-owned subdirectories are removed; anything else in
    ``work`` (and an operator's ``--base-config`` store) is left alone.
    """
    for name in names:
        path = work / name
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)


async def _startup_samples(
    work: Path,
    catalog: Path,
    base_for: Callable[[int], Path],
    env: Mapping[str, str] | None = None,
) -> tuple[list[StartupSample], list[int | None], list[str]]:
    """The ten startup samples, their memory, and the launches that failed to start.

    A launch that raises (the gateway exits, its token never arrives, no port)
    is a harness error named ``startup_<ordinal>_<reason>``. A launch the
    driver's one-second deadline cancels is a slow startup, a product sample,
    and is not listed: a gateway that would fail only after the deadline cannot
    be told apart from a slow one here.
    """
    samples: list[StartupSample] = []
    memory: list[int | None] = []
    launch_errors: list[str] = []

    @contextlib.asynccontextmanager
    async def launch():
        ordinal = len(memory)
        memory.append(None)  # Reserve the slot even if launch never yields.
        extra = {} if env is None else {"env": env}
        entered = False
        try:
            async with serve(
                work / f"run-{ordinal}", catalog, base_config=base_for(ordinal), **extra
            ) as running:
                entered = True
                try:
                    yield running
                finally:
                    memory[ordinal] = rss_bytes(running[0])
        except LAUNCH_ERRORS as error:
            if not entered:
                launch_errors.append(launch_failure(f"startup_{ordinal}", error))
            raise

    await measure_startups(launch, BODY, samples=samples)
    return samples, memory, launch_errors


def _startup(args) -> tuple[dict, bool]:
    if args.assets <= 0:
        raise ValueError(f"assets_must_be_positive: {args.assets}")
    base = Path(args.base_config).absolute()
    if not base.is_file():
        raise ValueError(f"base_config_not_found: {base}")
    work = Path(args.work).absolute()
    fresh_directories(work, ("catalog",))
    catalog = work / "catalog"
    generate_catalog(catalog, args.assets)
    samples, memory, harness = asyncio.run(_startup_samples(work, catalog, lambda _ordinal: base))
    report = startup_report(
        samples,
        memory,
        assets=args.assets,
        base_config_digest=hashlib.sha256(base.read_bytes()).hexdigest(),
        counter=memory_counter(),
        harness_errors=harness,
    )
    return report, not harness


# Every subdirectory ``operations`` writes below ``--work``.
_OPERATIONS_DIRECTORIES = ("catalog", "config", "imports", "startups", "baseline", "workload")


def _operations(args) -> tuple[dict, bool]:
    """The AC29 run on this machine; returns the report and whether the harness held.

    Without ``--base-config`` every launch gets a disposable development config
    with its own empty store, so each of the ten startups is clean. Product
    outcomes (a slow startup, a late batch, a failed request) are recorded in
    the report; only a harness failure (no gateway, a broken control channel)
    makes the run fail. A startup launch that raises before its gateway is
    ready is such a harness failure too, named ``startup_<ordinal>_<reason>``.
    A reused ``--work`` starts again from the requested catalog and new
    stores: the kit's own subdirectories are removed first.
    """
    if args.assets <= 0:
        raise ValueError(f"assets_must_be_positive: {args.assets}")
    if args.base_config and not Path(args.base_config).is_file():
        raise ValueError(f"base_config_not_found: {Path(args.base_config).absolute()}")
    work = Path(args.work).absolute()
    env = {UNUSED_KEY_ENV: "unused"}

    def base_for(name: str) -> Path:
        if args.base_config:
            return Path(args.base_config).absolute()
        return development_base_config(work / "config" / name, ROOT)

    fresh_directories(work, _OPERATIONS_DIRECTORIES)
    environment = collect_benchmark_environment()
    catalog = work / "catalog"
    generate_catalog(catalog, args.assets)
    startups, startup_memory, startup_errors = asyncio.run(
        _startup_samples(work / "startups", catalog, lambda n: base_for(f"startup-{n}"), env)
    )
    harness: list[str] = list(startup_errors)

    baseline_pid = None

    async def baseline() -> int | None:
        nonlocal baseline_pid
        async with serve_baseline(work / "baseline", base_config=base_for("baseline"), env=env) as (
            pid,
            _client,
        ):
            baseline_pid = pid
            await asyncio.sleep(1.0)  # Settle after the first answer before sampling.
            return rss_bytes(pid)

    try:
        baseline_bytes = asyncio.run(baseline())
    except LAUNCH_ERRORS as error:
        baseline_bytes = None
        harness.append(launch_failure("baseline", error))

    workload = WorkloadSamples(provenance="fixture", environment=environment)
    imports = work / "imports"
    write_import(imports, ())
    sampler = None

    async def run() -> list[str]:
        nonlocal sampler
        async with serve_controlled(
            work / "workload", catalog, base_config=base_for("workload"), imports=imports, env=env
        ) as gateway:
            sampler = MemorySampler(gateway.pid)
            async with sampler.running():
                plan = await run_operations_workload(gateway, imports, workload)
        return plan.channel_errors

    try:
        harness.extend(asyncio.run(run()))
    except LAUNCH_ERRORS as error:
        harness.append(launch_failure("workload", error))
    harness.extend(
        e for e in workload.errors if e in ("owned_task_failed", "job_evidence_unavailable")
    )
    memory = memory_observation(
        baseline_bytes, startup_memory, [] if sampler is None else sampler.samples
    )
    peak = memory.peak_bytes
    report = operations_report(
        startups,
        workload,
        memory,
        environment=environment,
        runner=runner_identity(args.label),
        catalog=dict(
            assets=args.assets,
            generated=True,
            committed_acceptance_catalog=False,
            reason="committed_acceptance_catalog_unavailable",
        ),
        memory_samples=dict(
            counter=memory_counter(),
            baseline_pid=baseline_pid,
            workload_pid=None if sampler is None else sampler.pid,
            baseline_bytes=baseline_bytes,
            startup_bytes=startup_memory,
            workload_bytes=[] if sampler is None else sampler.samples,
            peak_bytes=peak,
            increment_bytes=None if None in (peak, baseline_bytes) else peak - baseline_bytes,
        ),
        harness_errors=harness,
    )
    return report, not harness


def _manifest_inputs(path: Path) -> tuple[dict, bytes, str, dict, dict]:
    """The manifest, its bytes, the host-task digest, its evidence and environment lock."""
    payload = path.read_bytes()
    manifest = json.loads(payload)
    validate_whole_task("S3InputManifestV1", manifest)
    root = path.parent

    def document(kind: str) -> dict:
        found = [d for d in manifest["documents"] if d["kind"] == kind]
        if len(found) != 1:
            raise ValueError(f"manifest_{kind}_ambiguous")
        value = json.loads((root / found[0]["path"]).read_bytes())
        if canonical_digest(value) != found[0]["digest"]:
            raise WholeTaskRejected("document_digest")
        return value

    input_digest = verify_host_tasks(document("host_tasks"), document("host_freeze"))
    registry = {e["id"]: e for e in manifest["evidence"]}
    environment = document("environment_lock")
    if not isinstance(environment, dict):
        raise ValueError("environment_lock_invalid")
    return manifest, payload, input_digest, registry, environment


def _registered(registry: Mapping, evidence_id: str, original: Path) -> str:
    entry = registry.get(evidence_id)
    payload = original.read_bytes()
    if entry is None or (entry["sha256"], entry["bytes"]) != (
        hashlib.sha256(payload).hexdigest(),
        len(payload),
    ):
        raise ValueError(f"evidence_unregistered: {evidence_id}")
    return entry["path"]


def _platform(args) -> dict:
    inputs = Path(args.inputs).absolute()
    manifest, payload, input_digest, registry, environment = _manifest_inputs(inputs)
    platform = json.loads(Path(args.platform).read_text(encoding="utf-8"))
    if not isinstance(platform, dict):
        raise ValueError("platform_invalid")
    # The grader binds the observed platform to the frozen environment lock.
    if any(v is not None and v != platform.get(k) for k, v in environment.items()):
        raise ValueError("environment_binding")
    install_check = Path(args.install_check)
    ordinary = Path(args.ordinary) if args.ordinary else None
    operations = Path(args.operations) if args.operations else None
    paths = {"install-check": _registered(registry, "install-check", install_check)}
    if ordinary is not None:
        paths["ordinary-capture"] = _registered(registry, "ordinary-capture", ordinary)
    if operations is not None:
        paths["operations-report"] = _registered(registry, "operations-report", operations)
    report = platform_report(
        install_check=install_check,
        ordinary=ordinary,
        ordinary_check=ordinary_check() if ordinary is not None else None,
        operations=operations,
        evidence_paths=paths,
        input_digest=input_digest,
        input_manifest=dict(
            path=inputs.name, digest=canonical_digest(manifest), bytes=len(payload)
        ),
        platform=platform,
    )
    validate_whole_task("AssetPlatformReportV1", report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    startup = sub.add_parser("startup")
    startup.add_argument("--base-config", required=True, help="TOML with absolute paths only")
    startup.add_argument("--work", required=True)
    startup.add_argument("--assets", type=int, default=300, help="AC29 uses 300")
    startup.add_argument("--output", required=True)
    operations = sub.add_parser("operations")
    operations.add_argument(
        "--base-config", help="TOML with absolute paths only; default: a disposable one per launch"
    )
    operations.add_argument("--work", required=True)
    operations.add_argument("--assets", type=int, default=300, help="AC29 uses 300")
    operations.add_argument("--label", help="names the machine, e.g. the CI runner image")
    operations.add_argument("--output", required=True)
    platform = sub.add_parser("platform")
    platform.add_argument("--install-check", required=True)
    platform.add_argument("--ordinary")
    platform.add_argument("--operations", help="an operations report registered as evidence")
    platform.add_argument(
        "--inputs",
        required=True,
        help="companion input manifest registering install-check (and ordinary-capture)",
    )
    platform.add_argument("--platform", required=True)
    platform.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    held = True
    try:
        if args.command == "startup":
            report, held = _startup(args)
        elif args.command == "operations":
            report, held = _operations(args)
        else:
            report = _platform(args)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, WholeTaskRejected) as error:
        print(f"asset_operations_run: {error}", file=sys.stderr)
        return 2
    if not held:
        print(f"asset_operations_run: harness errors: {report['harness_errors']}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
