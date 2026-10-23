"""Cancel-during-prefill probe against llama-server (#117; RM14, RM26, RM27, spec 0.31).

What does a cancelled generation leave running on the local runtime, and
how long does the next request wait for it? A prompt of about 6,000 tokens
goes through ``LlamaCppAdapter``; 200 ms later the probe calls the
adapter's ``cancel`` (the call the gateway makes when a run is cancelled,
RM14); a short routing-shaped request is sent at once, and its wait is
recorded next to the same kind of request sent to an idle runtime just
before. Five repetitions. The long prompt is sized with the runtime's own
tokenizer to fit the context a slot has (``/props``), the real token count
is recorded, and each repetition uses a prompt of its own, so the runtime
cannot serve it from its cache. One uncancelled run of the long prompt
records how long its prefill takes in full, which shows whether 200 ms is
inside the prefill.

The short request is sent over raw HTTP in a thread, so the event loop
stays free for the long generation, which is what the gateway's loop is
too. Its timings and usage objects are read as in the prefix-cache probe
(``probe_prefix_cache.py``).

Sequential, concurrency one for the requests the probe waits on:
single-pass observations from one run on one machine, never p50 or p95
(spec 12.3). ``--runtime-extra-args`` adds llama-server arguments after the
manifest's launch settings (spec 0.31 section 12.2); they are recorded, and
a constrained configuration is reported as that configuration of this
machine, never as another machine.

Usage:
    python scripts/probes/probe_cancel_prefill.py scripts/probes/m7_cancel_prefill.json \\
        --out docs/evidence/<date>-cancel-prefill-<machine>.json \\
        [--runtime-extra-args='--n-gpu-layers 0 --threads 2']
"""

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

PROBES_DIR = Path(__file__).resolve().parent
# The probe helpers sit beside this file; the live harness one level up.
sys.path.insert(0, str(PROBES_DIR))
sys.path.insert(0, str(PROBES_DIR.parent))

import live_acceptance as harness  # noqa: E402
from probe_prefix_cache import (  # noqa: E402
    REPO_ROOT,
    Surfaces,
    build_surfaces,
    observe,
    request_body,
)

from relaymind.adapters.base import (  # noqa: E402
    Completed,
    GenerationFailed,
    GenerationOptions,
    GenerationRequest,
    Message,
    Role,
    Usage,
)
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig  # noqa: E402
from relaymind.adapters.manifest import launch_adapter_settings  # noqa: E402
from relaymind.evaluation.environment import (  # noqa: E402
    application_revision,
    machine_manifest,
    parse_runtime_log,
    runtime_devices,
)

KIND = "relaymind.probe.cancel_prefill"
LABEL = (
    "single-pass observations from one run on one machine; not a benchmark result "
    "(spec 12.3); never p50 or p95"
)
# Room kept in a slot's context beside the prompt and the answer.
CONTEXT_MARGIN = 256
# A rough size of one log line in tokens, for the first guess of the sizing.
TOKENS_PER_LINE = 35


@dataclass(frozen=True)
class CancelConfig:
    """A validated probe definition."""

    manifest_path: Path
    skills_root: Path
    sandbox_seed: Path
    skill: str
    target_tokens: int
    cancel_after_ms: int
    repetitions: int
    warmups: int
    warmup_prompt: str
    request_timeout_seconds: float
    long_output_tokens: int
    long_user_prompt: str
    short_prompts: tuple[str, ...]

    def to_json_object(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest_path.name,
            "skill": self.skill,
            "target_tokens": self.target_tokens,
            "cancel_after_ms": self.cancel_after_ms,
            "repetitions": self.repetitions,
            "warmups": self.warmups,
            "warmup_prompt": self.warmup_prompt,
            "request_timeout_seconds": self.request_timeout_seconds,
            "long_output_tokens": self.long_output_tokens,
            "long_user_prompt": self.long_user_prompt,
            "short_prompts": list(self.short_prompts),
        }


def load_probe_config(path: Path) -> CancelConfig:
    """Read and validate a probe definition; ValueError names the bad field."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read the definition: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("the definition must be a JSON object")

    def resolved(key: str) -> Path:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"'{key}' must be a path")
        target = (path.parent / value).resolve()
        if not target.exists():
            raise ValueError(f"'{key}' not found: {value}")
        return target

    def count(key: str, *, minimum: int) -> int:
        value = raw.get(key)
        if type(value) is not int or value < minimum:
            raise ValueError(f"'{key}' must be an integer of at least {minimum}")
        return value

    def text(key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"'{key}' must be a non-blank string")
        return value

    shorts = raw.get("short_prompts")
    if (
        not isinstance(shorts, list)
        or not all(isinstance(item, str) and item.strip() for item in shorts)
        or len(set(shorts)) != len(shorts)
    ):
        raise ValueError("'short_prompts' must be a list of different non-blank strings")
    repetitions = count("repetitions", minimum=1)
    if len(shorts) < 2 * repetitions:
        raise ValueError(
            f"'short_prompts' needs {2 * repetitions} texts: one idle and one after-cancel "
            f"request for each of the {repetitions} repetitions"
        )
    timeout = raw.get("request_timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("'request_timeout_seconds' must be a positive number")
    return CancelConfig(
        manifest_path=resolved("manifest"),
        skills_root=resolved("skills_root"),
        sandbox_seed=resolved("sandbox_seed"),
        skill=text("skill"),
        target_tokens=count("target_tokens", minimum=100),
        cancel_after_ms=count("cancel_after_ms", minimum=1),
        repetitions=repetitions,
        warmups=count("warmups", minimum=0),
        warmup_prompt=text("warmup_prompt"),
        request_timeout_seconds=float(timeout),
        long_output_tokens=count("long_output_tokens", minimum=1),
        long_user_prompt=text("long_user_prompt"),
        short_prompts=tuple(shorts),
    )


# -- the long prompt -----------------------------------------------------------------------------


def fit_target(target: int, *, n_ctx: int | None, output_tokens: int) -> int:
    """The prompt size to aim for: the target, or less when a slot's context is smaller."""
    if n_ctx is None:
        return target
    return min(target, n_ctx - output_tokens - CONTEXT_MARGIN)


def long_request(
    config: CancelConfig, *, repetition: int | str, lines: int, generation_id: str
) -> GenerationRequest:
    """A long prompt of its own: log lines whose numbers differ from every other repetition's.

    Repetition numbers start the lines, so two prompts share only the
    template head and the first word of the header; the runtime has no
    prefix to reuse.
    """
    label = f"{repetition:02d}" if isinstance(repetition, int) else repetition
    body = "\n".join(
        f"{label}-{index:04d} staging-api status ok, latency {(index * 37 + 11) % 900 + 100} ms, "
        f"queue {(index * 13 + 7) % 50}, worker {(index * 5) % 16}"
        for index in range(lines)
    )
    return GenerationRequest(
        generation_id=generation_id,
        messages=(
            Message(role=Role.SYSTEM, content=f"Capture {label}. Service log:\n{body}"),
            Message(role=Role.USER, content=config.long_user_prompt),
        ),
        options=GenerationOptions(max_output_tokens=config.long_output_tokens, temperature=0),
    )


async def size_long_prompt(
    adapter: LlamaCppAdapter, config: CancelConfig, *, target: int
) -> tuple[int, int]:
    """The number of log lines that gives about target tokens, and the real count.

    The count is the runtime's own (``/apply-template`` and ``/tokenize``),
    never an estimate, and the lines are adjusted until it is within 1 % of
    the target (at most five rounds).
    """
    lines = max(1, target // TOKENS_PER_LINE)
    tokens = 0
    for _ in range(5):
        request = long_request(config, repetition="size", lines=lines, generation_id="size")
        tokens = await adapter.count_tokens(request.messages, ())
        if abs(tokens - target) <= target * 0.01:
            break
        lines = max(1, round(lines * target / tokens))
    return lines, tokens


# -- the repetitions -----------------------------------------------------------------------------


async def _drain(adapter: LlamaCppAdapter, request: GenerationRequest, seen: list, ended: list):
    """Consume one generation; record each event with its time, and the time it ended."""
    try:
        async for event in adapter.generate(request):
            seen.append((time.monotonic(), event))
    finally:
        ended.append(time.monotonic())


def _ended_by(seen: Sequence[tuple[float, Any]]) -> str:
    events = [event for _, event in seen]
    if any(isinstance(event, GenerationFailed) for event in events):
        return "failed"
    return "completed" if any(isinstance(event, Completed) for event in events) else "cancelled"


async def run_reference(
    adapter: LlamaCppAdapter, config: CancelConfig, *, lines: int
) -> dict[str, Any]:
    """One long prompt, not cancelled: how long its prefill takes in full."""
    request = long_request(config, repetition="ref", lines=lines, generation_id="long_reference")
    seen: list = []
    ended: list = []
    started = time.monotonic()
    await asyncio.wait_for(_drain(adapter, request, seen, ended), config.request_timeout_seconds)
    usage = next((e for _, e in seen if isinstance(e, Usage)), None)
    return {
        "wall_ms": (ended[0] - started) * 1000,
        "ended_by": _ended_by(seen),
        "input_tokens": None if usage is None else usage.input_tokens,
    }


async def run_repetition(
    adapter: LlamaCppAdapter,
    client: httpx.Client,
    surfaces: Surfaces,
    config: CancelConfig,
    template_kwargs: Mapping[str, object],
    *,
    repetition: int,
    lines: int,
) -> dict[str, Any]:
    """One repetition: an idle short request, then the cancelled long one, then a short one.

    The second short request is sent right after the cancel call and while
    the long generation may still be running, as a user's next request is.
    Its wait (first_delta_ms and wall_ms) is read next to the idle request's.
    """

    def short(text: str, name: str) -> dict[str, Any]:
        request = surfaces.request("routing", text, f"{name}_{repetition}")
        return observe(client, request_body(request, template_kwargs, True))

    request = long_request(
        config, repetition=repetition, lines=lines, generation_id=f"long_{repetition}"
    )
    long_tokens = await adapter.count_tokens(request.messages, ())
    idle = await asyncio.to_thread(short, config.short_prompts[2 * repetition], "idle")

    seen: list = []
    ended: list = []
    task = asyncio.create_task(_drain(adapter, request, seen, ended))
    await asyncio.sleep(config.cancel_after_ms / 1000)
    cancel_called = time.monotonic()
    started = cancel_called - config.cancel_after_ms / 1000
    events_before_cancel = len(seen)
    await adapter.cancel(request.generation_id)
    sent_at = time.monotonic()
    after = await asyncio.to_thread(short, config.short_prompts[2 * repetition + 1], "after")
    after["sent_after_cancel_ms"] = (sent_at - cancel_called) * 1000

    try:
        await asyncio.wait_for(asyncio.shield(task), config.request_timeout_seconds)
        ended_by = _ended_by(seen)
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        ended_by = "timed_out_waiting"
    return {
        "repetition": repetition,
        "long_prompt_tokens": long_tokens,
        "idle": idle,
        "cancel": {
            "requested_after_ms": config.cancel_after_ms,
            "called_after_ms": (cancel_called - started) * 1000,
            "events_before_cancel": events_before_cancel,
        },
        "after_cancel": after,
        "long_generation": {
            "ended_by": ended_by,
            "ended_after_cancel_ms": (ended[0] - cancel_called) * 1000 if ended else None,
            "events": [type(event).__name__ for _, event in seen],
        },
    }


# -- the run -------------------------------------------------------------------------------------


def _server_props(client: httpx.Client) -> dict[str, Any] | None:
    try:
        data = client.get("/props").json()
    except (httpx.HTTPError, ValueError):
        return None
    settings = data.get("default_generation_settings") or {}
    return {
        "n_ctx": settings.get("n_ctx"),
        "total_slots": data.get("total_slots"),
        "build_info": data.get("build_info"),
    }


def _slots_processing(client: httpx.Client) -> int | None:
    """How many slots are still processing a request; None when /slots does not answer."""
    try:
        response = client.get("/slots")
        slots = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(slots, list):
        return None
    return sum(1 for slot in slots if isinstance(slot, dict) and slot.get("is_processing"))


async def measure(
    base_url: str,
    surfaces: Surfaces,
    config: CancelConfig,
    template_kwargs: Mapping[str, object],
    ctx_size: int,
) -> dict[str, Any]:
    llama = LlamaCppConfig(
        base_url=base_url,
        context_limit=ctx_size,
        chat_template_kwargs=template_kwargs,
        request_timeout_seconds=config.request_timeout_seconds,
    )
    async with LlamaCppAdapter(llama) as adapter:
        with httpx.Client(
            base_url=base_url, timeout=config.request_timeout_seconds, trust_env=False
        ) as client:
            props = _server_props(client)
            target = fit_target(
                config.target_tokens,
                n_ctx=props["n_ctx"] if props else None,
                output_tokens=config.long_output_tokens,
            )
            for _ in range(config.warmups):
                request = surfaces.request("routing", config.warmup_prompt, "warmup")
                await asyncio.to_thread(
                    observe, client, request_body(request, template_kwargs, True)
                )
            lines, tokens = await size_long_prompt(adapter, config, target=target)
            reference = await run_reference(adapter, config, lines=lines)
            repetitions = []
            for repetition in range(config.repetitions):
                print(f"repetition {repetition}", flush=True)
                repetitions.append(
                    await run_repetition(
                        adapter,
                        client,
                        surfaces,
                        config,
                        template_kwargs,
                        repetition=repetition,
                        lines=lines,
                    )
                )
            slots_processing_after = _slots_processing(client)
    return {
        "props": props,
        "sizing": {
            "target_tokens": config.target_tokens,
            "aimed_tokens": target,
            "lines": lines,
            "counted_tokens": tokens,
            "reference_uncancelled": reference,
        },
        "slots_processing_after": slots_processing_after,
        "repetitions": repetitions,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", type=Path, help="probe definition JSON")
    parser.add_argument("--out", type=Path, required=True, help="export file path")
    harness.add_runtime_extra_args(parser)
    args = parser.parse_args(argv)
    try:
        config = load_probe_config(args.config)
    except ValueError as exc:
        parser.error(str(exc))
    runtime = harness.check_runtime(config.manifest_path, extra_args=args.runtime_extra_args)
    # Read before the run writes its export into the checkout.
    application = application_revision(REPO_ROOT)
    ctx_size, template_kwargs = launch_adapter_settings(runtime.manifest.launch_settings)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    work = Path(tempfile.mkdtemp(prefix="relaymind-cancel-prefill-"))
    try:
        surfaces = build_surfaces(
            skills_root=config.skills_root,
            sandbox_seed=config.sandbox_seed,
            skill=config.skill,
            work_dir=work / "sandbox",
        )
        log = work / "llama-server.log"
        process, base_url, cold_start = runtime.launch(log)
        try:
            result = asyncio.run(measure(base_url, surfaces, config, template_kwargs, ctx_size))
        finally:
            harness._stop(process)
        runtime_log = parse_runtime_log(log.read_text("utf-8", errors="replace"))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    document = {
        "kind": KIND,
        "label": LABEL,
        "command": " ".join(["python", *sys.argv]),
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "machine": machine_manifest(),
        "application": application,
        "runtime_banner": runtime.banner,
        "runtime_devices": runtime_devices(),
        "llama_server": runtime.server_record(cold_start),
        **runtime.model_record(),
        "runtime_log": runtime_log,
        "definition": config.to_json_object(),
        **result,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", "utf-8")
    sizing = result["sizing"]
    print(
        f"long prompt: {sizing['counted_tokens']} tokens; uncancelled run "
        f"{sizing['reference_uncancelled']['wall_ms']:.0f} ms"
    )
    for row in result["repetitions"]:
        idle, after = row["idle"], row["after_cancel"]
        print(
            f"rep {row['repetition']}: idle first_delta={idle['first_delta_ms']} "
            f"wall={idle['wall_ms']:.0f} ms; after cancel first_delta={after['first_delta_ms']} "
            f"wall={after['wall_ms']:.0f} ms; long ended {row['long_generation']['ended_by']} "
            f"{row['long_generation']['ended_after_cancel_ms']} ms after the cancel"
        )
    print(f"export: {args.out}")


if __name__ == "__main__":
    main()
