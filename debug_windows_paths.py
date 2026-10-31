"""Temporary Windows diagnostic: capture report.input_error for the temp-alias case."""

import asyncio
import os
import platform
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))

from relaymind.evaluation.asset_benchmark import (  # noqa: E402
    run_asset_benchmark,
    write_asset_benchmark_report,
)
from relaymind.evaluation.asset_evidence import verify_benchmark_inputs  # noqa: E402
from test_asset_selection_benchmark import fixture_config  # noqa: E402


def main() -> int:
    print("python", sys.version)
    print("platform", platform.platform())
    base = Path(tempfile.mkdtemp(prefix="debug-paths-"))
    physical = base / "physical"
    physical.mkdir()
    alias = base / "os-temp-alias"
    alias.symlink_to(physical, target_is_directory=True)
    config = fixture_config(base, repetitions=1, warmups=0)
    previous = tempfile.tempdir
    tempfile.tempdir = str(alias)
    try:
        report = asyncio.run(run_asset_benchmark(config))
    finally:
        tempfile.tempdir = previous
    print("status", report.data["status"])
    print("inputs present:", report.inputs is not None)
    print("input_error:", report.input_error)

    previous = tempfile.tempdir
    tempfile.tempdir = str(alias)
    try:
        with tempfile.TemporaryDirectory(prefix="asset-report-inputs-") as probe:
            temporary_root = Path(probe)
            resolved_parent = temporary_root.parent.resolve(strict=True)
            print("tempdir setting:", tempfile.tempdir)
            print("temporary_root:", temporary_root)
            print("temporary_root.parent:", temporary_root.parent)
            print("parent is symlink:", temporary_root.parent.is_symlink())
            print("resolved parent:", resolved_parent)
            print("resolved equals parent:", resolved_parent == temporary_root.parent)
            root = resolved_parent / temporary_root.name
            print("verification root:", root)
            print("os.path.realpath parent:", os.path.realpath(temporary_root.parent))
    finally:
        tempfile.tempdir = previous

    output = base / "out"
    output.mkdir()
    write_asset_benchmark_report(report, output / "capture.json")
    print("after write inputs present:", report.inputs is not None)
    print("after write input_error:", report.input_error)

    previous = tempfile.tempdir
    tempfile.tempdir = str(alias)
    try:
        with tempfile.TemporaryDirectory(prefix="asset-report-inputs-") as again:
            temporary_root = Path(again)
            root = temporary_root.parent.resolve(strict=True) / temporary_root.name
            write_asset_benchmark_report(report, root / "capture.json")
            print("in-temp inputs present:", report.inputs is not None)
            print("in-temp input_error:", report.input_error)
            if report.inputs is None:
                try:
                    verify_benchmark_inputs(report.data, root / "capture.json.inputs.json")
                except BaseException:
                    traceback.print_exc()
    finally:
        tempfile.tempdir = previous
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
