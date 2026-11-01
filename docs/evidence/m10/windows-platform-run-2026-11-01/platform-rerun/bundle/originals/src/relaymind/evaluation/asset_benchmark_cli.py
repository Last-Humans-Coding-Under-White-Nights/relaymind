"""Bounded run/compare command parser; fixed safe diagnostics (RM40/AC31)."""

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from relaymind.evaluation.asset_benchmark import (
    load_asset_benchmark_report,
    load_benchmark_config,
    prepare_benchmark_index,
    run_asset_benchmark,
    write_asset_benchmark_report,
)
from relaymind.evaluation.asset_benchmark_io import atomic_bytes, json_bytes
from relaymind.evaluation.asset_comparison import compare_asset_reports, render_asset_comparison
from relaymind.evaluation.asset_evidence import OfflineResolver


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RM35/RM40 retrieval benchmark; fixture runs do not establish qualification."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    compare = sub.add_parser("compare")
    compare.add_argument("a", type=Path)
    compare.add_argument("b", type=Path)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--markdown", type=Path, required=True)
    compare.add_argument("--quality-only", action="store_true")
    compare.add_argument("--inputs-a", type=Path)
    compare.add_argument("--inputs-b", type=Path)
    compare.add_argument("--resolver", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command in ("run", "prepare"):
            config = load_benchmark_config(args.config)
            stop = False

            def interrupt(signum, frame):
                nonlocal stop
                stop = True

            prior = {}
            try:
                for name in ("SIGINT", "SIGTERM"):
                    sig = getattr(signal, name, None)
                    if sig is not None:
                        prior[sig] = signal.signal(sig, interrupt)
                if args.command == "prepare":
                    preparation = asyncio.run(
                        prepare_benchmark_index(config, interrupted=lambda: stop)
                    )
                    atomic_bytes(args.output, json_bytes(preparation))
                else:
                    report = asyncio.run(run_asset_benchmark(config, interrupted=lambda: stop))
                    write_asset_benchmark_report(report, args.output)
            finally:
                for sig, handler in prior.items():
                    signal.signal(sig, handler)
            if args.command == "prepare":
                return 0 if preparation["status"] in ("ready", "not_applicable") else 2
            return 0 if report.data["status"] == "completed" else 2
        resolver = OfflineResolver(args.resolver) if args.resolver is not None else None
        a = load_asset_benchmark_report(args.a, inputs=args.inputs_a, resolver=resolver)
        b = load_asset_benchmark_report(args.b, inputs=args.inputs_b, resolver=resolver)
        result = compare_asset_reports(a, b, quality_only=args.quality_only)
        atomic_bytes(args.output, json_bytes(result.to_json_object()))
        atomic_bytes(args.markdown, render_asset_comparison(result).encode("utf-8"))
        return 0 if result.data["status"] == "compatible" else 2
    except (ValueError, OSError, KeyError, TypeError):
        print("asset_benchmark: invalid_or_unresolved_input", file=sys.stderr)
        return 2
