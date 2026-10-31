#!/usr/bin/env python3
"""RM34/RM40/RM44: acquire actual sources and run source-grounded development benchmarks."""

import argparse
import asyncio
import json
import sys
import tomllib
from pathlib import Path

from relaymind.evaluation.asset_benchmark_io import (
    REPORT_BYTES,
    atomic_bytes,
    json_bytes,
    read_json_path,
)
from relaymind.evaluation.asset_corpus_io import AssetCorpusError, read_local
from relaymind.evaluation.asset_diagnostic import load_asset_diagnostic
from relaymind.evaluation.asset_diagnostic_runner import (
    compare_source_benchmarks,
    run_source_benchmark,
)
from relaymind.evaluation.asset_source_capture import acquire_recipe
from relaymind.evaluation.asset_source_recipe import build_recipe, write_example_configs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("acquire", "build"):
        command = sub.add_parser(name)
        command.add_argument("--recipe", type=Path, required=True)
        command.add_argument("--cache", type=Path, required=True)
        if name == "build":
            command.add_argument("--output", type=Path, required=True)
    command = sub.add_parser("run")
    for name in ("catalog", "cases", "evidence-root", "production", "output"):
        command.add_argument("--" + name, type=Path, required=True)
    command.add_argument("--track", choices=["primary_only", "service"], default="service")
    command.add_argument("--repetitions", type=int, default=1)
    command.add_argument("--seed", type=int, default=0)
    command.add_argument("--preparation-config", type=Path)
    command = sub.add_parser("compare")
    for name in ("left", "right", "output"):
        command.add_argument("--" + name, type=Path, required=True)
    command = sub.add_parser("choose")
    command.add_argument("--report", type=Path, required=True)
    command.add_argument("--case", required=True)
    args = parser.parse_args()
    exit_code = 0
    try:
        if args.command == "acquire":
            recipe, _ = read_json_path(args.recipe)
            records = acquire_recipe(recipe, args.cache)
            value = dict(
                captured=sum(r["status"] == "captured" for r in records),
                reused=sum(r["status"] == "reused" for r in records),
                excluded=sum(r["status"] == "excluded" for r in records),
                records=records,
            )
            print(json.dumps(value))
            return int(bool(value["excluded"]))
        if args.command == "build":
            recipe, _ = read_json_path(args.recipe)
            built = build_recipe(recipe, args.cache, args.output)
            configs = write_example_configs(built)
            value = dict(
                catalog=str(built.catalog_path),
                admitted=built.admitted,
                excluded=built.excluded,
                imports={k: str(v) for k, v in built.import_paths.items()},
                production_configs={k: str(v) for k, v in configs.items()},
            )
        elif args.command == "run":
            preparation = None
            if args.preparation_config is not None:
                path = args.preparation_config.absolute()
                setup = tomllib.loads(
                    read_local(path.parent, path.name, 1024 * 1024).decode("utf-8")
                )
                if set(setup) != {"preparation"}:
                    raise AssetCorpusError("invalid_preparation_config")
                preparation = setup["preparation"]
            inputs = load_asset_diagnostic(
                args.cases, catalog_path=args.catalog, evidence_root=args.evidence_root
            )
            report = asyncio.run(
                run_source_benchmark(
                    inputs,
                    production_config=args.production,
                    track=args.track,
                    repetitions=args.repetitions,
                    seed=args.seed,
                    preparation=preparation,
                )
            )
            atomic_bytes(args.output, json_bytes(report))
            exit_code = 0 if report["status"] == "completed" else 2
            value = dict(
                output=str(args.output),
                profile=report["profile"],
                summary=report["summary"],
                language_pairs=report["language_pairs"],
            )
        elif args.command == "compare":
            left, _ = read_json_path(args.left, REPORT_BYTES)
            right, _ = read_json_path(args.right, REPORT_BYTES)
            value = compare_source_benchmarks(left, right)
            atomic_bytes(args.output, json_bytes(value))
        else:
            report, _ = read_json_path(args.report, REPORT_BYTES)
            rows = [
                r
                for r in report["rows"]
                if r["slot"]["case_id"] == args.case and r["slot"]["repetition"] == 0
            ]
            if len(rows) != 1 or rows[0]["evaluated_decision"] != "choice_required":
                raise AssetCorpusError("choice_not_required")
            row = rows[0]
            candidates = row["evaluated_candidates"][:3]
            catalog_assets = {
                a["record"]["asset_id"]: a["record"]
                for a in report.get("catalog_document", {}).get("assets", [])
                if "record" in a
            }
            catalog = catalog_assets or {
                r["asset_id"]: r for r in (row.get("request") or {}).get("catalog", [])
            }
            for n, candidate in enumerate(candidates, 1):
                record = catalog.get(candidate["asset_id"], {})
                identity = record.get("identity", {})
                option = dict(
                    option=n,
                    asset_id=candidate["asset_id"],
                    name=identity.get(
                        "package_id", identity.get("exposed_name", candidate["asset_id"])
                    ),
                    description=record.get("description", ""),
                    availability=record.get("availability", "unknown"),
                )
                print(json.dumps(option), file=sys.stderr)
            print(
                "Select an option number, or 0 to decline. This records a choice, not execution:",
                file=sys.stderr,
            )
            choice = input().strip()
            if (
                not choice.isascii()
                or not choice.isdigit()
                or not 0 <= int(choice) <= len(candidates)
            ):
                raise AssetCorpusError("invalid_user_choice")
            value = dict(
                case_id=args.case,
                catalog_digest=report["catalog_digest"],
                selected_asset_id=candidates[int(choice) - 1]["asset_id"] if int(choice) else None,
                selection_method="explicit_user_input" if int(choice) else "explicit_user_decline",
                action_executed=False,
            )
        print(json.dumps(value))
        return exit_code
    except (AssetCorpusError, ValueError, TypeError, KeyError, OSError, EOFError) as exc:
        reason = str(exc) if isinstance(exc, AssetCorpusError) else "invalid_source_input"
        print(json.dumps({"status": "failed", "reason": reason}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
