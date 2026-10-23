"""Latency benchmark: the section 12.2 experiment on the local path (#33).

Spec 0.28 section 12.2. The runner verifies the runtime and the pinned
artifact, starts llama-server from the manifest, and starts one gateway
with ``relaymind serve --config`` (routing cache off, no external model,
concurrency one); it reuses the live acceptance harness
(``scripts/live_acceptance.py``). It sends the twelve prompts of
``evaluation/latency_prompts.v1.json`` as compatible chat requests with
stream true: first the cold request (the first request after the
gateway starts), then three warm-ups (one per prompt label), then ten
runs per prompt in an order shuffled with a recorded seed. A run that
waits for input is cancelled after its sample is taken.

Section 12.1 metrics: first transport event (the response status) and
first answer token (the first chunk with nonempty content) on the
client's monotonic clock from just before the request is sent; first
model action and first useful content from the run's trace (elapsed_ms,
the gateway's monotonic clock from run.accepted). The two clocks are
never subtracted. Timeouts and errors are recorded with their sample.

The export holds the environment manifest (machine, backend devices,
runtime banner and log facts, /props of llama-server, model hashes,
quantization, revisions, test time), the prefix cache record, every
sample, and the summary; a Markdown report is written next to it.

Usage:
    python scripts/latency_benchmark.py --out docs/evidence/<date>-latency-<machine>.json \\
        [--seed N] [--runs 10]
"""

import argparse
import asyncio
import json
import os
import random
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_acceptance as harness  # noqa: E402

from relaymind.adapters import GenerationRequest, Message, Role  # noqa: E402
from relaymind.adapters.llamacpp import build_chat_request  # noqa: E402
from relaymind.api.app import RUN_ID_HEADER  # noqa: E402
from relaymind.canonical import canonical_digest  # noqa: E402
from relaymind.evaluation.environment import (  # noqa: E402
    machine_manifest,
    parse_runtime_log,
    runtime_devices,
)
from relaymind.evaluation.metrics import (  # noqa: E402
    LATENCY_METRICS,
    render_latency_markdown,
    summarize_latency,
)

REPO_ROOT = harness.REPO_ROOT
PROMPTS = REPO_ROOT / "evaluation" / "latency_prompts.v1.json"
LABELS = ("simple", "clarification", "routing")
PROMPT_COUNT = 12
# The run state and error code each label expects (spec 0.28 section 12.2).
EXPECTED = {
    "simple": ("completed", None),
    "clarification": ("waiting_input", None),
    "routing": ("failed", "needs_external"),
}
RUNS_PER_PROMPT = 10
REQUEST_TIMEOUT_SECONDS = 60.0
CHAT_MODEL = "relaymind"
PROPS_KEYS = ("default_generation_settings", "total_slots", "model_ftype", "build_info")


def load_prompts(path: Path = PROMPTS) -> list[dict[str, str]]:
    document = json.loads(Path(path).read_text("utf-8"))
    prompts = document.get("prompts")
    if not isinstance(prompts, list) or len(prompts) != PROMPT_COUNT:
        raise ValueError(f"the latency experiment uses {PROMPT_COUNT} prompts (section 12.2)")
    for prompt in prompts:
        if not isinstance(prompt, dict) or set(prompt) != {"id", "label", "prompt"}:
            raise ValueError("each prompt has exactly id, label, and prompt")
        if prompt["label"] not in LABELS:
            raise ValueError(f"prompt {prompt['id']}: label must be one of {', '.join(LABELS)}")
    if len({p["id"] for p in prompts}) != len(prompts):
        raise ValueError("prompt ids must be unique")
    return prompts


def schedule(prompt_ids: Sequence[str], *, runs: int, seed: int) -> list[str]:
    """runs measured runs per prompt, in an order shuffled with seed."""
    order = [prompt_id for prompt_id in prompt_ids for _ in range(runs)]
    random.Random(seed).shuffle(order)
    return order


def warmup_prompts(prompts: Sequence[Mapping[str, str]]) -> list[Mapping[str, str]]:
    """Three warm-ups: the first prompt of each label."""
    return [next(p for p in prompts if p["label"] == label) for label in LABELS]


def benchmark_config(**options) -> str:
    """The operator file: checked-in skills, baseline sandbox, no external model, cache off."""
    return harness.gateway_config(**options, routing_cache=False, external=None)


def first_useful_content_ms(events: Sequence[Mapping[str, Any]]) -> float | None:
    """The first clarification question, or the first answer text after a successful tool result."""
    evidence = False
    for event in events:
        kind = event["event_type"]
        if kind == "clarification.required":
            return float(event["elapsed_ms"])
        # The event's own status: a large result is stored as result_ref, not inline.
        if kind == "tool.finished" and event["payload"].get("status") == "succeeded":
            evidence = True
        if kind == "answer.delta" and evidence and event["payload"]["text"]:
            return float(event["elapsed_ms"])
    return None


def _model_calls(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    calls = []
    for event in harness.of_type(events, "model.finished"):
        usage = event["payload"].get("usage") or {}
        calls.append(
            {
                "purpose": event["payload"].get("purpose"),
                "duration_ms": event["payload"].get("duration_ms"),
                "input_tokens": usage.get("input_tokens"),
                "cached_tokens": usage.get("cached_tokens"),
            }
        )
    return calls


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 3)


async def _stream(api: harness.Api, prompt: str, sample: dict[str, Any]) -> None:
    """Send one chat request with stream true and time it on the client's monotonic clock."""
    body = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": prompt}], "stream": True}
    started = time.monotonic()
    async with api.http.stream("POST", "/v1/chat/completions", json=body) as response:
        sample["first_transport_ms"] = _elapsed_ms(started)
        sample["http_status"] = response.status_code
        sample["run_id"] = response.headers.get(RUN_ID_HEADER)
        async for line in response.aiter_lines():
            # Once the first token is seen, the rest is only drained (no parsing in the timing).
            if sample["first_answer_token_ms"] is not None:
                continue
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[len("data: ") :])
            if any((c.get("delta") or {}).get("content") for c in chunk.get("choices", [])):
                sample["first_answer_token_ms"] = _elapsed_ms(started)
    sample["completion_ms"] = _elapsed_ms(started)


async def measure(api: harness.Api, prompt: Mapping[str, str], *, timeout: float) -> dict[str, Any]:
    """One sample: the client timings, then the trace timings; a waiting run is cancelled."""
    sample: dict[str, Any] = {
        "prompt_id": prompt["id"],
        "label": prompt["label"],
        "started_at_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "run_id": None,
        "http_status": None,
        "state": None,
        "error_code": None,
        "timed_out": False,
        "error": None,
        "cancelled_after": False,
        "model_calls": [],
        **{metric: None for metric in LATENCY_METRICS},
    }
    try:
        await asyncio.wait_for(_stream(api, prompt["prompt"], sample), timeout)
    except TimeoutError:
        sample["timed_out"] = True
        sample["error"] = "timeout"
    except httpx.HTTPError as exc:
        sample["error"] = f"transport: {type(exc).__name__}: {exc}"
    except json.JSONDecodeError as exc:
        # A malformed chunk is this sample's error, never the end of the benchmark.
        sample["error"] = f"stream: {type(exc).__name__}: {exc}"
    run_id = sample["run_id"]
    if run_id is None:
        if sample["error"] is None:
            sample["error"] = f"no run ID in the response (status {sample['http_status']})"
        return sample
    try:
        await _read_trace(api, run_id, sample)
    except (TimeoutError, httpx.HTTPError) as exc:
        sample["error"] = sample["error"] or f"trace: {type(exc).__name__}: {exc}"
        return sample
    expected = EXPECTED[prompt["label"]]
    if sample["error"] is None and (sample["state"], sample["error_code"]) != expected:
        sample["error"] = (
            f"ended {sample['state']} ({sample['error_code']}), expected {expected[0]}"
            + (f" ({expected[1]})" if expected[1] else "")
        )
    return sample


async def _read_trace(api: harness.Api, run_id: str, sample: dict[str, Any]) -> None:
    """Cancel a run that has not ended, then take the trace timings of the sample."""
    view = await api.view(run_id)
    if view["state"] not in harness.TERMINAL:
        if not sample["timed_out"]:
            sample["state"] = view["state"]
        cancelled = await api.cancel(run_id, view["state_version"])
        sample["cancelled_after"] = cancelled.is_success
        view = await api.settle(run_id, harness.TERMINAL, timeout=30)
    if sample["state"] is None:
        sample["state"] = view["state"]
    sample["error_code"] = view["error_code"]
    events = await api.events(run_id)
    action = harness.first_model_action_ms(events)
    sample["first_model_action_ms"] = None if action is None else float(action)
    sample["first_useful_content_ms"] = first_useful_content_ms(events)
    sample["model_calls"] = _model_calls(events)


Measure = Callable[..., Awaitable[dict[str, Any]]]


async def run_samples(
    api: harness.Api | None,
    prompts: Sequence[Mapping[str, str]],
    *,
    seed: int,
    runs: int = RUNS_PER_PROMPT,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    measure_one: Measure = measure,
) -> dict[str, Any]:
    """The cold request, the warm-ups, then the measured runs in the seeded order."""
    by_id = {p["id"]: p for p in prompts}
    cold = await measure_one(api, prompts[0], timeout=timeout)
    warmups = [await measure_one(api, p, timeout=timeout) for p in warmup_prompts(prompts)]
    measured = []
    for index, prompt_id in enumerate(schedule(list(by_id), runs=runs, seed=seed)):
        sample = await measure_one(api, by_id[prompt_id], timeout=timeout)
        measured.append({**sample, "order": index})
        print(f"{index:3} {prompt_id} {sample.get('error') or 'ok'}", flush=True)
    return {"cold": cold, "warmups": warmups, "measured": measured}


def prefix_cache_record(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Whether the adapter sets cache_prompt, and the cached prompt tokens the runtime reported."""
    request = GenerationRequest(
        generation_id="probe", messages=(Message(role=Role.USER, content="x"),)
    )
    sets = "cache_prompt" in build_chat_request(request, chat_template_kwargs=None)
    cached = [c["cached_tokens"] for c in calls if c.get("cached_tokens") is not None]
    return {
        "adapter_sets_cache_prompt": sets,
        "note": "The llamacpp adapter does not send cache_prompt, so llama-server's own default "
        "prompt (prefix) cache behavior applies; cached_tokens is what the runtime reported "
        "for each measured model call."
        if not sets
        else "The llamacpp adapter sends cache_prompt.",
        "calls": len(cached),
        "calls_with_cached_tokens": sum(1 for c in cached if c > 0),
        "cached_tokens_total": sum(cached),
    }


def _props(local_url: str) -> dict[str, Any] | None:
    try:
        response = httpx.get(local_url + "/props", timeout=30, trust_env=False)
        return {key: response.json().get(key) for key in PROPS_KEYS}
    except (httpx.HTTPError, ValueError):
        return None


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    prompts = load_prompts()
    runtime = harness.check_runtime(Path(args.manifest), extra_args=args.runtime_extra_args)
    application = harness.application_revision()
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**31)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    work = Path(tempfile.mkdtemp(prefix="relaymind-latency-"))
    # The work dir goes on any exit, an interrupt included.
    with ExitStack() as cleanup:
        cleanup.callback(shutil.rmtree, work, ignore_errors=True)
        llama_log = work / "llama-server.log"
        llama, local_url, cold_start = runtime.launch(llama_log)
        try:
            props = _props(local_url)
            config = work / "relaymind.toml"
            config.write_text(
                benchmark_config(
                    work=work,
                    manifest=runtime.manifest_path.resolve(),
                    artifact=runtime.artifact,
                    local_url=local_url,
                    port=harness._free_port(),
                ),
                encoding="utf-8",
            )
            gateway = harness.GatewayProcess(config, work / "gateway.log", os.environ)
            gateway_started = time.monotonic()
            output = gateway.start()
            gateway_ready = time.monotonic() - gateway_started
            api = harness.Api(output.url, output.token, sandbox_dir=work / "sandbox")

            async def drive():
                try:
                    return await run_samples(
                        api, prompts, seed=seed, runs=args.runs, timeout=args.timeout
                    )
                finally:
                    await api.aclose()

            try:
                samples = asyncio.run(drive())
            finally:
                gateway.stop()
        finally:
            harness._stop(llama)
        runtime_log = parse_runtime_log(llama_log.read_text("utf-8", errors="replace"))
        measured = samples["measured"]
        summary = summarize_latency(measured)
        calls = [call for sample in measured for call in sample["model_calls"]]
        document = {
            "kind": "relaymind.latency_benchmark",
            "note": "Spec 0.28 section 12.2 latency experiment on the local path, concurrency one, "
            "routing cache off. First transport event and first answer token are client times; "
            "first model action and first useful content are trace times (elapsed_ms from "
            "run.accepted). The two clocks are never subtracted. Errors and timeouts are kept.",
            "command": " ".join(["python", "scripts/latency_benchmark.py", *sys.argv[1:]]),
            "started_at_utc": started_at,
            "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "prompts": {
                "file": "evaluation/latency_prompts.v1.json",
                "sha256": canonical_digest(prompts),
                "language": "en",
            },
            "design": {
                "seed": seed,
                "warmups": len(samples["warmups"]),
                "runs_per_prompt": args.runs,
                "request_timeout_seconds": args.timeout,
                "concurrency": 1,
                "routing_cache": False,
                "external_model": None,
                "path": "POST /v1/chat/completions, stream true, model relaymind",
            },
            "environment": {
                "machine": machine_manifest(),
                "application": application,
                "runtime_banner": runtime.banner,
                "runtime_devices": runtime_devices(),
                "runtime_log": runtime_log,
                "llama_server_argv": runtime.server_argv,
                "llama_server_extra_args": list(runtime.extra_args),
                "llama_server_props": props,
                **runtime.model_record(),
                "context": {"ctx_size": runtime.manifest.launch_settings.get("ctx_size")},
                "external_endpoint_region": None,
            },
            "cold_start": {
                "llama_server_start_to_healthy_seconds": round(cold_start, 3),
                "model_load_ms": runtime_log["model_load_ms"],
                "gateway_start_to_healthy_seconds": round(gateway_ready, 3),
                "gateway_config": config.read_text("utf-8"),
                "cold_request": samples["cold"],
            },
            "prefix_cache": prefix_cache_record(calls),
            "summary": summary,
            "warmups": samples["warmups"],
            "samples": measured,
        }
        out = Path(args.out)
        harness.write_json(out, document, work=work)
        report = render_latency_markdown(summary, title=f"Latency benchmark: {out.stem}")
        out.with_suffix(".md").write_text(report, encoding="utf-8")
        return document


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="the export file (JSON); the report is .md")
    parser.add_argument(
        "--manifest", default=os.fspath(harness.DEFAULT_MANIFEST.relative_to(REPO_ROOT))
    )
    harness.add_runtime_extra_args(parser)
    parser.add_argument("--seed", type=int, help="the order seed (random when not given)")
    parser.add_argument("--runs", type=int, default=RUNS_PER_PROMPT)
    parser.add_argument("--timeout", type=float, default=REQUEST_TIMEOUT_SECONDS)
    document = run_benchmark(parser.parse_args(argv))
    print(render_latency_markdown(document["summary"], title="Latency benchmark"))


if __name__ == "__main__":
    main()
