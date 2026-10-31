"""Operations and platform run kit (#242; RM38, RM39, RM44; AC29, AC37).

    startup   ten real ``relaymind serve`` startups over a generated catalog,
              measured by the existing AC29 driver, with resident memory samples
    platform  an AssetPlatformReportV1 from an install-check record and an
              optional ordinary installed-host capture

Development tooling only. Output is labeled ``fixture``; a developer machine
or CI runner never satisfies the macOS / native Windows acceptance gates.
"""

import argparse
import asyncio
import contextlib
import dataclasses
import json
import sys
from pathlib import Path

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_operations import StartupSample, measure_startups
from relaymind.evaluation.asset_operations_run import (
    generate_catalog,
    platform_report,
    rss_bytes,
    serve,
)
from relaymind.evaluation.asset_whole_task import validate_whole_task

BODY = dict(
    schema_version=1, task_text="greeting", requested_kinds=["skill"], selection_scope="discovery"
)


def _startup(args) -> dict:
    work = Path(args.work)
    catalog = work / "catalog"
    generate_catalog(catalog, args.assets)
    base = Path(args.base_config).read_text(encoding="utf-8")
    samples: list[StartupSample] = []
    peaks: list[int] = []
    counter = iter(range(1000))

    @contextlib.asynccontextmanager
    async def launch():
        async with serve(work / f"run-{next(counter)}", catalog, base_config=base) as running:
            yield running
            if (value := rss_bytes(running[0])) is not None:
                peaks.append(value)

    asyncio.run(measure_startups(launch, BODY, samples=samples))
    return dict(
        kind="asset-operations-startup",
        qualification="fixture",
        assets=args.assets,
        base_config_digest=canonical_digest(base),
        samples=[dataclasses.asdict(sample) for sample in samples],
        rss_bytes=peaks,
        complete=len(peaks) == len(samples) == 10,
    )


def _platform(args) -> dict:
    manifest = Path(args.input_manifest).read_bytes()
    report = platform_report(
        install_check=Path(args.install_check),
        ordinary=Path(args.ordinary) if args.ordinary else None,
        input_digest=canonical_digest(json.loads(manifest)),
        input_manifest=dict(
            path=Path(args.input_manifest).name,
            digest=canonical_digest(json.loads(manifest)),
            bytes=len(manifest),
        ),
        platform=json.loads(Path(args.platform).read_text(encoding="utf-8")),
    )
    validate_whole_task("AssetPlatformReportV1", report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    startup = sub.add_parser("startup")
    startup.add_argument("--base-config", required=True)
    startup.add_argument("--work", required=True)
    startup.add_argument("--assets", type=int, default=100)
    startup.add_argument("--output", required=True)
    platform = sub.add_parser("platform")
    platform.add_argument("--install-check", required=True)
    platform.add_argument("--ordinary")
    platform.add_argument("--input-manifest", required=True)
    platform.add_argument("--platform", required=True)
    platform.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        report = _startup(args) if args.command == "startup" else _platform(args)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"asset_operations_run: {error}", file=sys.stderr)
        return 2
    Path(args.output).write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
