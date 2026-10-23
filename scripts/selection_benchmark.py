#!/usr/bin/env python3
"""Run section 12.4 selection benchmarks (RM02, RM08, RM09, RM25, RM28).

Run --help for track, selector, endpoint and output options.

ToolRet reports contain locally downloaded source instruction text. Keep those
reports outside the repository; publish aggregate metrics and row IDs only.
"""

# ruff: noqa: E402 -- allow direct script execution from a checkout.

import argparse
import asyncio
import importlib.util
import json
import os
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from relaymind.adapters.chat_completions import ChatCompletionsAdapter, ChatCompletionsConfig
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig
from relaymind.adapters.local_http import is_loopback_url
from relaymind.evaluation.environment import application_revision, machine_manifest
from relaymind.evaluation.selection_benchmark import (
    RouterBenchmarkSelector,
    file_digest,
    load_locked,
    run_skills,
    run_tools,
)
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.selection.contract import SelectionContext, SelectionRequest, parse_selection_result
from relaymind.selection.example_match import ExampleMatchSelector
from relaymind.selection.settings import SelectionSettings
from relaymind.tools import ToolRegistry


class HttpSelector:
    """RM28 JSON transport. The caller owns its client and timeout."""

    name, version, model_based = "http", "reported-per-case", False

    def __init__(self, url: str, client: httpx.AsyncClient):
        if not is_loopback_url(url):
            raise ValueError("HTTP selector endpoint must be loopback")
        self.url, self.client = url, client

    async def select(self, request: SelectionRequest, context: SelectionContext) -> dict:
        response = await self.client.post(self.url, json=request.to_json_object())
        response.raise_for_status()
        return parse_selection_result(response.json()).to_json_object()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    result.add_argument("--track", choices=("skills", "tools"), required=True)
    result.add_argument(
        "--selector", required=True, help="model_router, example_match, or HTTP URL"
    )
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--evaluation-dir", type=Path, default=ROOT / "evaluation")
    result.add_argument("--seed", type=int, default=42)
    result.add_argument(
        "--limit", type=int, help="Seeded subset; external proposals default to 100"
    )
    result.add_argument(
        "--timeout",
        type=float,
        help="Per-case seconds; defaults to local active, external, or selection budget",
    )
    result.add_argument("--local-url", default="http://127.0.0.1:8033")
    result.add_argument("--model", default="configured-local-model")
    result.add_argument("--model-manifest", type=Path)
    result.add_argument("--context-limit", type=int, default=4096)
    result.add_argument("--routing-input-tokens", type=int, default=2048)
    result.add_argument("--routing-output-tokens", type=int, default=128)
    result.add_argument("--proposal", choices=("none", "local", "external"), default="none")
    result.add_argument("--calls-per-generation", choices=("one", "many"), default="many")
    result.add_argument("--external-url", default=os.environ.get("RELAYMIND_EXTERNAL_BASE_URL"))
    result.add_argument("--external-model", default=os.environ.get("RELAYMIND_EXTERNAL_MODEL"))
    result.add_argument("--api-key-env", default="RELAYMIND_EXTERNAL_API_KEY")
    result.add_argument("--external-request-options", type=json.loads, default={})
    result.add_argument(
        "--toolret-cache", type=Path, default=Path.home() / ".cache/relaymind/toolret"
    )
    result.add_argument("--example-score", type=float, default=0.5)
    result.add_argument("--example-margin", type=float, default=0.1)
    result.add_argument(
        "--source-training",
        choices=("unknown", "trained", "tuned", "neither"),
        default="unknown",
        help="Declare whether this selector/model trained or tuned on the source",
    )
    return result


def _toolret_cases(lock_path: Path, cache: Path) -> list[dict]:
    path = ROOT / "scripts/benchmarks/derive_toolret.py"
    spec = importlib.util.spec_from_file_location("derive_toolret", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_cases(lock_path, cache)


def _subset(cases: list[dict], limit: int | None, seed: int) -> list[dict]:
    if limit is None or limit >= len(cases):
        return cases
    chosen = sorted(random.Random(seed).sample(range(len(cases)), limit))
    return [cases[i] for i in chosen]


async def run(args: argparse.Namespace) -> dict:
    """Run an identified, frozen sample and export every attempt."""
    if args.timeout is None:
        args.timeout = (
            LocalBudgets().external_seconds
            if args.proposal == "external"
            else LocalBudgets().active_seconds
            if args.selector == "model_router" or args.proposal == "local"
            else SelectionSettings().timeout_seconds
        )
    if args.timeout <= 0 or args.context_limit <= 0 or (args.limit is not None and args.limit <= 0):
        raise ValueError("timeout, context limit and sample limit must be positive")
    files = {}
    if args.track == "skills":
        path, lock = (
            args.evaluation_dir / "skills.v1.json",
            args.evaluation_dir / "skills.v1.lock.json",
        )
        data = load_locked(path, lock)
        # Leave-one-task-out examples always use the full frozen dataset, even
        # when the measured cases are a seeded subset.
        cases = data["cases"]
        files[path.name], files[lock.name] = file_digest(path), file_digest(lock)
    else:
        lock = args.evaluation_dir / "tools.v1.lock.json"
        cases = _toolret_cases(lock, args.toolret_cache)
        data = {"source": json.loads(lock.read_text())}
        files[lock.name] = file_digest(lock)
    limit = args.limit if args.limit is not None else (100 if args.proposal == "external" else None)
    selected = _subset(cases, limit, args.seed)
    local = external = None
    try:
        if (args.selector == "model_router" and args.track == "skills") or args.proposal == "local":
            if not is_loopback_url(args.local_url):
                raise ValueError("local model endpoint must be loopback")
            local = LlamaCppAdapter(
                LlamaCppConfig(
                    args.local_url, args.context_limit, request_timeout_seconds=args.timeout
                )
            )
        if args.proposal == "external":
            if not args.external_url or not args.external_model:
                raise ValueError("external proposal needs external URL and model")
            external = ChatCompletionsAdapter(
                ChatCompletionsConfig(
                    base_url=args.external_url,
                    model=args.external_model,
                    context_limit=args.context_limit,
                    api_key_env=args.api_key_env,
                    request_timeout_seconds=args.timeout,
                    request_options=args.external_request_options,
                )
            )
        async with httpx.AsyncClient(timeout=args.timeout, trust_env=False) as client:
            if args.selector == "model_router":
                selector = (
                    RouterBenchmarkSelector(
                        local,
                        args.model,
                        budgets=LocalBudgets(
                            routing_input_tokens=args.routing_input_tokens,
                            routing_output_tokens=args.routing_output_tokens,
                        ),
                    )
                    if args.track == "skills"
                    else SimpleNamespace(name="model_router", version="1")
                )
            elif args.selector == "example_match":
                selector = ExampleMatchSelector(
                    ToolRegistry(), score=args.example_score, margin=args.example_margin
                )
            elif args.selector.startswith(("http://", "https://")):
                selector = HttpSelector(args.selector, client)
            else:
                raise ValueError(
                    "unknown selector; use model_router, example_match, or an HTTP URL"
                )
            if args.track == "skills":
                results = await run_skills(
                    data,
                    selector,
                    seed=args.seed,
                    timeout=args.timeout,
                    case_ids={c["id"] for c in selected},
                )
            else:
                results = await run_tools(
                    selected,
                    selector,
                    adapter=external or local,
                    timeout=args.timeout,
                    context_limit=args.context_limit,
                    calls_per_generation=args.calls_per_generation,
                    seed=args.seed,
                )
            selector_identity = {
                "name": selector.name,
                "version": selector.version,
                "endpoint": args.selector if selector.name == "http" else None,
            }
    finally:
        if local is not None:
            await local.aclose()
        if external is not None:
            await external.aclose()
    return {
        "schema_version": 1,
        "benchmark": "selection",
        "track": args.track,
        "created_at": datetime.now(UTC).isoformat(),
        "selector": selector_identity,
        "source_training": args.source_training,
        "files": files,
        "source": data.get("source"),
        "seed": args.seed,
        "sample_ids": [c["id"] for c in selected],
        "machine": machine_manifest(),
        "application": application_revision(ROOT),
        "configuration": {
            "timeout_seconds": args.timeout,
            "context_limit": args.context_limit,
            "routing_input_tokens": args.routing_input_tokens,
            "routing_output_tokens": args.routing_output_tokens,
            "calls_per_generation": args.calls_per_generation,
            "example_score": args.example_score,
            "example_margin": args.example_margin,
            "proposal": args.proposal,
        },
        "model": {
            "name": args.external_model if args.proposal == "external" else args.model,
            "endpoint": args.external_url if args.proposal == "external" else args.local_url,
            "manifest": json.loads(args.model_manifest.read_text())
            if args.model_manifest
            else None,
            "request_options": args.external_request_options
            if args.proposal == "external"
            else None,
        }
        if local is not None or external is not None
        else None,
        "caveats": [
            "Office/coding tasks and web APIs; not ops corpus evidence.",
            "Router catalog capped at eight; one router pick bounds multi-skill recall.",
            "Long prompts that exceed measured input limits are counted as bypasses.",
            "Tool arguments are schema-checked, not graded for semantic correctness.",
            "External inputs use the production conservative UTF-8 byte context guard.",
            "Source training declaration is operator-reported; unknown is not held-out evidence.",
        ],
        "results": results,
    }


def main() -> None:
    args = parser().parse_args()
    try:
        report = asyncio.run(run(args))
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {args.output} ({len(report['sample_ids'])} cases)")


if __name__ == "__main__":
    main()
