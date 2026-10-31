"""Temporary Windows diagnostic: capture report.input_error for the temp-alias case."""

import asyncio
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

    output = base / "out"
    output.mkdir()
    write_asset_benchmark_report(report, output / "capture.json")
    print("after write inputs present:", report.inputs is not None)
    print("after write input_error:", report.input_error)
    if report.inputs is None:
        manifest = output / "capture.json.inputs.json"
        try:
            verify_benchmark_inputs(report.data, manifest)
        except BaseException:
            traceback.print_exc()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
