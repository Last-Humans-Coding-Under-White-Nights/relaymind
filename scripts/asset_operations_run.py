"""Operations and platform run kit (#242; RM38, RM39, RM44; AC29, AC37).

    startup   ten real ``relaymind serve`` startups over a generated catalog,
              measured by the existing AC29 driver, with memory samples
    platform  an AssetPlatformReportV1 from an install-check record and an
              optional ordinary installed-host capture, bound to the
              companion input manifest

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
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_operations import StartupSample, measure_startups
from relaymind.evaluation.asset_operations_run import (
    generate_catalog,
    memory_counter,
    platform_report,
    rss_bytes,
    serve,
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
        # Complete means all ten startups were useful and all ten memory samples known.
        complete=len(samples) == 10
        and useful == 10
        and len(memory) == len(samples) == 10
        and None not in memory,
    )


def _startup(args) -> dict:
    if args.assets <= 0:
        raise ValueError(f"assets_must_be_positive: {args.assets}")
    base = Path(args.base_config).absolute()
    if not base.is_file():
        raise ValueError(f"base_config_not_found: {base}")
    work = Path(args.work).absolute()
    catalog = work / "catalog"
    generate_catalog(catalog, args.assets)
    samples: list[StartupSample] = []
    memory: list[int | None] = []
    counter = iter(range(1000))

    @contextlib.asynccontextmanager
    async def launch():
        ordinal = next(counter)
        memory.append(None)  # Reserve the slot even if launch never yields.
        async with serve(work / f"run-{ordinal}", catalog, base_config=base) as running:
            try:
                yield running
            finally:
                memory[ordinal] = rss_bytes(running[0])

    asyncio.run(measure_startups(launch, BODY, samples=samples))
    return startup_report(
        samples,
        memory,
        assets=args.assets,
        base_config_digest=hashlib.sha256(base.read_bytes()).hexdigest(),
        counter=memory_counter(),
    )


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
    return manifest, payload, input_digest, registry, document("environment_lock")


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
    paths = {"install-check": _registered(registry, "install-check", Path(args.install_check))}
    if args.ordinary:
        paths["ordinary-capture"] = _registered(registry, "ordinary-capture", Path(args.ordinary))
    report = platform_report(
        install_check=Path(args.install_check),
        ordinary=Path(args.ordinary) if args.ordinary else None,
        ordinary_check=ordinary_check() if args.ordinary else None,
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
    platform = sub.add_parser("platform")
    platform.add_argument("--install-check", required=True)
    platform.add_argument("--ordinary")
    platform.add_argument(
        "--inputs",
        required=True,
        help="companion input manifest registering install-check (and ordinary-capture)",
    )
    platform.add_argument("--platform", required=True)
    platform.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        report = _startup(args) if args.command == "startup" else _platform(args)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, WholeTaskRejected) as error:
        print(f"asset_operations_run: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
