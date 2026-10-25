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
        [--seed N] [--runs 10] [--early-headers] [--no-startup-warmup]
"""

import argparse
import asyncio
import json
import os
import random
import re
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import evaluation_session as session  # noqa: E402
import live_acceptance as harness  # noqa: E402
from evaluation_config import (  # noqa: E402
    RunnerConfiguration,
    add_configuration_arguments,
    configuration_from_args,
    label_report,
)

from relaymind.adapters import GenerationRequest, Message, Role  # noqa: E402
from relaymind.adapters.llamacpp import build_chat_request  # noqa: E402
from relaymind.api.app import RUN_ID_HEADER  # noqa: E402
from relaymind.canonical import canonical_digest  # noqa: E402
from relaymind.evaluation.environment import (  # noqa: E402
    checkout_environment,  # noqa: E402
    machine_manifest,
    parse_runtime_log,
    runtime_devices,
)
from relaymind.evaluation.grading import selector_provenance  # noqa: E402
from relaymind.evaluation.metrics import (  # noqa: E402
    LATENCY_METRICS,
    render_latency_markdown,
    summarize_latency,
)
from relaymind.evaluation.placement_metrics import trace_metrics  # noqa: E402
from relaymind.evaluation.prior import byte_digest  # noqa: E402

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


def benchmark_config(*, early_headers: bool = False, startup_warmup: bool = True, **options) -> str:
    """The operator file: checked-in skills, baseline sandbox, no external model, cache off.

    early_headers turns on performance.early_headers (RM32, spec 0.32 section 7.3) and
    nothing else; without it the file has no [performance] table, which is the baseline.
    startup_warmup false sets placement.warmup to false (spec 0.31 section 6), so the first
    request of the run is the first the runtime sees; the default leaves the table out and the
    gateway warms the local model up before it serves.
    """
    performance = dict(options.pop("performance", None) or {})
    if early_headers:
        if performance.get("early_headers") is False:
            raise ValueError("early_headers conflicts with performance.early_headers=false")
        performance["early_headers"] = True
    placement = dict(options.pop("placement", None) or {})
    if not startup_warmup:
        placement["warmup"] = False
    external = options.pop("external", None)
    return harness.gateway_config(
        **options,
        routing_cache=False,
        external=external,
        modes=(
            ("auto", "external_only")
            if not options.get("local_model", True)
            else harness.LOCAL_MODES
        ),
        performance=performance,
        placement=placement,
    )


def first_transport_label(early_headers: bool) -> str:
    """What the first transport event is in this configuration (spec 0.32 sections 7.3, 12.1).

    Under early headers the status line arrives when the run is accepted, so the metric is
    the header time. It says nothing about when the first useful content arrives.
    """
    if early_headers:
        return (
            "header time: the status line sent at acceptance (early response headers); "
            "not first useful content"
        )
    return "the response status, sent when the run has finished (the baseline)"


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
                "call_id": event["payload"].get("call_id"),
                "actor": event.get("actor"),
                "model": event["payload"].get("model"),
                "purpose": event["payload"].get("purpose"),
                "duration_ms": event["payload"].get("duration_ms"),
                "input_tokens": usage.get("input_tokens"),
                # None when the runtime reported no count: not measured (spec 0.31 section 6).
                "cached_tokens": usage.get("cached_tokens"),
                "reasoning_tokens": usage.get("reasoning_tokens"),
                "first_token_ms": event["payload"].get("first_token_ms"),
            }
        )
    return calls


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 3)


ERROR_EVENT_PREFIX = 'data: {"error"'


def _error_event_code(line: str) -> str:
    """The code of a mid-stream error event line; 'unknown' when it has none."""
    try:
        code = json.loads(line[len("data: ") :])["error"].get("code")
    except (json.JSONDecodeError, AttributeError):
        return "unknown"
    return code if isinstance(code, str) and code else "unknown"


async def _stream(api: harness.Api, prompt: str, sample: dict[str, Any]) -> None:
    """Send one chat request with stream true and time it on the client's monotonic clock."""
    body = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": prompt}], "stream": True}
    started = time.monotonic()
    async with api.http.stream("POST", "/v1/chat/completions", json=body) as response:
        sample["first_transport_ms"] = _elapsed_ms(started)
        sample["http_status"] = response.status_code
        sample["run_id"] = response.headers.get(RUN_ID_HEADER)
        try:
            async for line in response.aiter_lines():
                if line.startswith(ERROR_EVENT_PREFIX):
                    # The gateway's own mid-stream error event (spec 0.32 section 7.3): the
                    # end of the response. It closes the connection on purpose after it.
                    sample["stream_error_code"] = _error_event_code(line)
                    continue
                # Once the first token is seen, the rest is only drained (no parsing).
                if sample["first_answer_token_ms"] is not None:
                    continue
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                chunk = json.loads(line[len("data: ") :])
                if any((c.get("delta") or {}).get("content") for c in chunk.get("choices", [])):
                    sample["first_answer_token_ms"] = _elapsed_ms(started)
        except httpx.RemoteProtocolError:
            # Expected only after the error event; any other cut is a transport error.
            if sample["stream_error_code"] is None:
                raise
    sample["stream_duration_ms"] = _elapsed_ms(started)
    sample["completion_ms"] = sample["stream_duration_ms"]


async def measure(
    api: harness.Api, prompt: Mapping[str, str], *, timeout: float, external: bool = False
) -> dict[str, Any]:
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
        "stream_error_code": None,
        "cancelled_after": False,
        "model_calls": [],
        "selector_provenance": selector_provenance([]),
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
    expected = (
        ("completed", None)
        if external and prompt["label"] == "routing"
        else EXPECTED[prompt["label"]]
    )
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
    sample["selector_provenance"] = selector_provenance(events)
    sample["trace"] = events
    sample["metrics"] = trace_metrics(events)
    if sample["state"] != "completed" or sample.get("stream_error_code") or sample.get("timed_out"):
        sample["completion_ms"] = None


Measure = Callable[..., Awaitable[dict[str, Any]]]


async def run_samples(
    api: harness.Api | None,
    prompts: Sequence[Mapping[str, str]],
    *,
    seed: int,
    runs: int = RUNS_PER_PROMPT,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    measure_one: Measure = measure,
    external: bool = False,
    checkpoint=None,
) -> dict[str, Any]:
    """The cold request, the warm-ups, then the measured runs in the seeded order."""
    options = {"timeout": timeout, **({"external": True} if external else {})}
    by_id = {p["id"]: p for p in prompts}

    async def observed(identity, prompt):
        if checkpoint:
            checkpoint.start(identity)
        value = await measure_one(api, prompt, **options)
        if checkpoint:
            checkpoint.finish(identity, value)
        return value

    cold = await observed("cold/0", prompts[0])
    warmups = [await observed(f"warmup/{i}", p) for i, p in enumerate(warmup_prompts(prompts))]
    measured = []
    for index, prompt_id in enumerate(schedule(list(by_id), runs=runs, seed=seed)):
        sample = await observed(f"measured/{index}/{prompt_id}", by_id[prompt_id])
        measured.append({**sample, "order": index})
        print(f"{index:3} {prompt_id} {sample.get('error') or 'ok'}", flush=True)
    return {"cold": cold, "warmups": warmups, "measured": measured}


def prefix_cache_record(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Whether the adapter sets cache_prompt, and the cached prompt tokens the runtime reported.

    A call whose cached count is None was not measured (spec 0.31 section 6): it is
    counted apart, never as 0, and a total over no measured call is null.
    """
    # Earlier exports had no actor and this runner supported local calls only.
    # Current mixed configurations must not attribute external cache usage to llama.cpp.
    calls = [call for call in calls if call.get("actor", "local") == "local"]
    request = GenerationRequest(
        generation_id="probe", messages=(Message(role=Role.USER, content="x"),)
    )
    sets = "cache_prompt" in build_chat_request(request, chat_template_kwargs=None)
    cached = [c["cached_tokens"] for c in calls if c.get("cached_tokens") is not None]
    return {
        "adapter_sets_cache_prompt": sets,
        "note": (
            "The llamacpp adapter sends cache_prompt"
            if sets
            else "The llamacpp adapter does not send cache_prompt, so llama-server's own "
            "default prompt (prefix) cache behavior applies"
        )
        + "; cached_tokens is what the runtime reported for each measured model call, "
        "and a call with no reported count is not measured.",
        "calls": len(calls),
        "calls_with_reported_count": len(cached),
        "calls_not_measured": len(calls) - len(cached),
        "calls_with_cached_tokens": sum(1 for c in cached if c > 0) if cached else None,
        "cached_tokens_total": sum(cached) if cached else None,
    }


WARMUP_LINE_PREFIX = "Local warm-up finished: "
WARMUP_SAMPLE = re.compile(
    r"(?P<purpose>\w+) (?P<duration>\d+) ms \(first token (?P<first>\d+ ms|n/a), "
    r"cached (?P<cached>\d+|n/a)\)"
)


def startup_warmup_record(lines: Sequence[str], *, startup_warmup: bool) -> dict[str, Any]:
    """The warm-up samples the launcher printed (spec 0.31 sections 6 and 12.2).

    The launcher prints one line when the warm-up finished; a count it did not report is n/a
    there and None here. With warm-up off, or when the launcher printed no such line (the
    warm-up failed or timed out), there are no samples, and none are invented.
    """
    line = next((x for x in lines if x.startswith(WARMUP_LINE_PREFIX)), None)
    if not startup_warmup or line is None:
        return {"enabled": startup_warmup, "line": None, "samples": None}
    samples = [
        {
            "purpose": m["purpose"],
            "duration_ms": int(m["duration"]),
            "first_token_ms": None if m["first"] == "n/a" else int(m["first"].split()[0]),
            "cached_tokens": None if m["cached"] == "n/a" else int(m["cached"]),
        }
        for m in WARMUP_SAMPLE.finditer(line)
    ]
    return {"enabled": True, "line": line, "samples": samples}


def _props(local_url: str) -> dict[str, Any] | None:
    try:
        response = httpx.get(local_url + "/props", timeout=30, trust_env=False)
        return {key: response.json().get(key) for key in PROPS_KEYS}
    except (httpx.HTTPError, ValueError):
        return None


def effective_configuration(
    configuration: RunnerConfiguration, *, no_startup_warmup: bool = False
) -> dict[str, Any]:
    """The settings actually launched/exported, separate from raw checkpoint identity."""
    document = configuration.to_json_object()
    document["placement"]["warmup"] = configuration.local_model_configured and not no_startup_warmup
    return document


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    configuration = configuration_from_args(args)
    early_headers = configuration.performance.early_headers
    effective = effective_configuration(
        configuration, no_startup_warmup=getattr(args, "no_startup_warmup", False)
    )
    startup_warmup = effective["placement"]["warmup"]
    external = None
    if getattr(args, "external_base_url", None):
        external = harness.ExternalEndpoint(
            args.external_base_url, args.external_model, args.external_context_limit
        )
    if (external is not None and not os.environ.get(harness.EXTERNAL_KEY_ENV)) or (
        not configuration.local_model_configured and external is None
    ):
        raise ValueError("an external endpoint and its API key are required for this configuration")
    prompts = load_prompts()
    runtime = (
        harness.check_runtime(Path(args.manifest), extra_args=args.runtime_extra_args)
        if configuration.local_model_configured
        else None
    )
    partition, prior, application, checkpoint = session.prepare(
        args, REPO_ROOT, runtime, external, configuration
    )
    if (previous := session.existing_result(checkpoint, args.out)) is not None:
        return previous
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**31)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    work = (
        Path(tempfile.mkdtemp(prefix="relaymind-latency-"))
        if checkpoint is None
        else session.durable_work(args, "latency", None)
    )
    # The work dir goes on any exit, an interrupt included.
    with ExitStack() as cleanup:
        if checkpoint is None:
            cleanup.callback(shutil.rmtree, work, ignore_errors=True)
        llama_log = work / "llama-server.log"
        llama, local_url, cold_start = runtime.launch(llama_log) if runtime else (None, None, None)
        try:
            props = _props(local_url) if runtime else None
            config = work / "relaymind.toml"
            config.write_text(
                benchmark_config(
                    work=work,
                    manifest=runtime.manifest_path.resolve() if runtime else None,
                    artifact=runtime.artifact if runtime else None,
                    external=external,
                    **configuration.gateway_options(),
                    local_url=local_url,
                    port=harness._free_port(),
                    early_headers=early_headers,
                    startup_warmup=startup_warmup,
                ),
                encoding="utf-8",
            )
            prior_report = session.seed_store(work, prior)
            gateway = harness.GatewayProcess(
                config, work / "gateway.log", checkout_environment(REPO_ROOT, dict(os.environ))
            )
            gateway_started = time.monotonic()
            output = gateway.start()
            gateway_ready = time.monotonic() - gateway_started
            api = harness.Api(output.url, output.token, sandbox_dir=work / "sandbox")

            async def drive():
                try:
                    return await run_samples(
                        api,
                        prompts,
                        seed=seed,
                        runs=args.runs,
                        timeout=args.timeout,
                        external=external is not None,
                        **({"checkpoint": checkpoint} if checkpoint else {}),
                    )
                finally:
                    await api.aclose()

            try:
                samples = asyncio.run(drive())
            finally:
                gateway.stop()
        finally:
            if llama is not None:
                harness._stop(llama)
        runtime_log = (
            parse_runtime_log(llama_log.read_text("utf-8", errors="replace")) if runtime else None
        )
        measured = samples["measured"]
        summary = summarize_latency(measured)
        calls = [call for sample in measured for call in sample["model_calls"]]
        document = {
            "kind": "relaymind.latency_benchmark",
            "prior_configuration": partition,
            "timing_prior": prior_report,
            "timing_snapshot": session.snapshot_store(work),
            "learning": "one gateway, online sequence after frozen initial prior",
            "configuration": effective,
            "note": "Section 12.2 latency experiment in the recorded configuration, "
            "concurrency one, "
            "routing cache off. First transport event and first answer token are client times; "
            "first model action and first useful content are trace times (elapsed_ms from "
            "run.accepted). The two clocks are never subtracted. Errors and timeouts are kept."
            + (
                " First transport event: " + first_transport_label(early_headers) + "."
                if early_headers
                else ""
            ),
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
                "early_headers": early_headers,
                "startup_warmup": startup_warmup,
                "first_transport_event": first_transport_label(early_headers),
                "external_model": external.model if external else None,
                "path": "POST /v1/chat/completions, stream true, model relaymind",
            },
            "environment": {
                "machine": machine_manifest(),
                "application": application,
                "runtime_banner": runtime.banner if runtime else None,
                "runtime_devices": runtime_devices() if runtime else None,
                "runtime_log": runtime_log,
                "llama_server_argv": runtime.server_argv if runtime else None,
                "llama_server_extra_args": list(runtime.extra_args) if runtime else None,
                "llama_server_props": props,
                **(
                    runtime.model_record()
                    if runtime
                    else {
                        "model_id": None,
                        "manifest": None,
                        "model_file": None,
                        "model_sha256": None,
                        "quantization": None,
                    }
                ),
                "context": {"ctx_size": runtime.manifest.launch_settings.get("ctx_size")}
                if runtime
                else None,
                "external_endpoint_region": getattr(args, "external_region", None)
                if external
                else None,
                "external_endpoint": {
                    "base_url": external.base_url,
                    "model": external.model,
                    "context_limit": external.context_limit,
                }
                if external
                else None,
            },
            "cold_start": {
                "llama_server_start_to_healthy_seconds": round(cold_start, 3)
                if cold_start is not None
                else None,
                "model_load_ms": runtime_log["model_load_ms"] if runtime_log else None,
                "gateway_start_to_healthy_seconds": round(gateway_ready, 3),
                "gateway_config": config.read_text("utf-8"),
                "cold_request": samples["cold"],
                "startup_warmup": startup_warmup_record(
                    output.recorded_lines, startup_warmup=startup_warmup
                ),
            },
            "prefix_cache": prefix_cache_record(calls) if runtime else None,
            "summary": summary,
            "warmups": samples["warmups"],
            "samples": measured,
        }
        out = Path(args.out)
        harness.write_json(out, document, work=work)
        report = render_latency_markdown(
            summary, title=f"Latency benchmark: {out.stem}", early_headers=early_headers
        )
        report = label_report(report, document["configuration"])
        out.with_suffix(".md").write_text(report, encoding="utf-8")
        if checkpoint:
            checkpoint.complete({"export_sha256": byte_digest(out), "samples": len(measured)})
        return document


def argument_parser() -> argparse.ArgumentParser:
    """The same CLI schema is used for invocation and checkpoint reconciliation."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="the export file (JSON); the report is .md")
    parser.add_argument(
        "--manifest", default=os.fspath(harness.DEFAULT_MANIFEST.relative_to(REPO_ROOT))
    )
    harness.add_runtime_extra_args(parser)
    add_configuration_arguments(parser)
    parser.add_argument("--external-base-url")
    parser.add_argument("--external-model")
    parser.add_argument("--external-context-limit", type=int)
    parser.add_argument("--external-region")
    parser.add_argument("--seed", type=int, help="the order seed (random when not given)")
    parser.add_argument(
        "--early-headers",
        action="store_true",
        help="run the gateway with performance.early_headers (RM32); the first transport "
        "event is then labeled header time",
    )
    parser.add_argument(
        "--no-startup-warmup",
        action="store_true",
        help="set placement.warmup to false, so the cold request is the first the runtime sees "
        "(the default gateway warms the local model up before it serves)",
    )
    parser.add_argument("--runs", type=int, default=RUNS_PER_PROMPT)
    parser.add_argument("--timeout", type=float, default=REQUEST_TIMEOUT_SECONDS)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = argument_parser()
    args = parser.parse_args(argv)
    external_args = (args.external_base_url, args.external_model, args.external_context_limit)
    if any(external_args) and not all(external_args):
        parser.error(
            "--external-base-url, --external-model, and --external-context-limit go together"
        )
    try:
        document = run_benchmark(args)
    except ValueError as exc:
        parser.error(str(exc))
    print(
        label_report(
            render_latency_markdown(
                document["summary"],
                title="Latency benchmark",
                early_headers=document["design"]["early_headers"],
            ),
            document.get("configuration"),
        )
    )


if __name__ == "__main__":
    main()
