"""Timing probe against the pinned local artifact (RM25, spec section 12.1).

Launches llama-server from the model manifest, measures cold start
(process launch to the first /health 200), sends warm-up requests of
both kinds, then runs one pass over plain prompts and tool-call prompts
through LlamaCppAdapter wrapped in TimingCollector. The export records
the metrics per generation plus everything that produced them: the
command, prompts, system prompt, tool schema, warm-up ids, server argv,
and each generation's answer or tool proposal. An EVIDENCE.md entry can
therefore name the exact command and point at one file (RM16, section
12).

Engineering instrumentation: sequential, concurrency one, a single
measured pass. Numbers are single-pass observations, never p50 or p95
and never publishable results (spec 12.3).

Usage:
    python scripts/probe_timing.py scripts/probes/m1_timing.json \\
        --out docs/evidence/<date>-m1-timing-<machine>.json [--model <gguf>]

Config format (paths resolve against the config file's directory):
    {
      "manifest": "../../models/qwen3-0.6b-q8_0.manifest.json",
      "warmups": 3,
      "system_prompt": "You check service health. ...",
      "tool": {"name": ..., "description": ..., "input_schema": {...}},
      "text_prompts": ["Reply with exactly one word: pong"],
      "tool_prompts": ["Is staging-api healthy right now?"]
    }
"""

import argparse
import asyncio
import json
import platform
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from relaymind.adapters.base import (
    GenerationRequest,
    Message,
    ModelAdapter,
    Role,
    TextDelta,
    ToolCall,
    ToolSchema,
)
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig
from relaymind.adapters.manifest import (
    ArtifactManifest,
    ManifestError,
    launch_adapter_settings,
    launch_settings_argv,
    load_manifest,
    verify_artifact,
)
from relaymind.stats import AdapterIdentity, GenerationStats, TimingCollector, export_stats

WARMUP_PREFIX = "warmup_"
STARTUP_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class ProbeConfig:
    """A validated probe definition."""

    manifest_path: Path
    warmups: int
    system_prompt: str
    tool: ToolSchema
    text_prompts: tuple[str, ...]
    tool_prompts: tuple[str, ...]


def _prompt_list(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ValueError(f"'{key}' must be a non-empty list of non-blank strings")
    return tuple(value)


def _tool_schema(raw: object) -> ToolSchema:
    if not isinstance(raw, Mapping):
        raise ValueError("'tool' must be a JSON object")
    name = raw.get("name")
    description = raw.get("description")
    input_schema = raw.get("input_schema")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("'tool.name' must be a non-blank string")
    if not isinstance(description, str):
        raise ValueError("'tool.description' must be a string")
    if not isinstance(input_schema, Mapping):
        raise ValueError("'tool.input_schema' must be a JSON object")
    return ToolSchema(name=name, description=description, input_schema=input_schema)


def load_probe_config(path: Path) -> ProbeConfig:
    """Read and validate a probe config; ValueError names the bad field."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")
    manifest = raw.get("manifest")
    if not isinstance(manifest, str) or not manifest:
        raise ValueError("'manifest' must be a path to a model manifest")
    manifest_path = (Path(path).parent / manifest).resolve()
    if not manifest_path.is_file():
        raise ValueError(f"manifest not found: {manifest}")
    warmups = raw.get("warmups", 0)
    if type(warmups) is not int or warmups < 0:
        raise ValueError("'warmups' must be a nonnegative integer")
    system_prompt = raw.get("system_prompt")
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ValueError("'system_prompt' must be a non-blank string")
    return ProbeConfig(
        manifest_path=manifest_path,
        warmups=warmups,
        system_prompt=system_prompt,
        tool=_tool_schema(raw.get("tool")),
        text_prompts=_prompt_list(raw, "text_prompts"),
        tool_prompts=_prompt_list(raw, "tool_prompts"),
    )


def _text_request(generation_id: str, prompt: str) -> GenerationRequest:
    return GenerationRequest(
        generation_id=generation_id,
        messages=(Message(role=Role.USER, content=prompt),),
    )


def _tool_request(generation_id: str, prompt: str, config: ProbeConfig) -> GenerationRequest:
    return GenerationRequest(
        generation_id=generation_id,
        messages=(
            Message(role=Role.SYSTEM, content=config.system_prompt),
            Message(role=Role.USER, content=prompt),
        ),
        tool_schemas=(config.tool,),
    )


def probe_requests(config: ProbeConfig) -> list[GenerationRequest]:
    """Warm-ups of both kinds first, then the measured pass.

    Each warm-up round sends the first plain prompt and the first tool
    prompt, so the first measured tool call is not the first request
    that ever carried the system prompt and tool schema.
    """
    requests: list[GenerationRequest] = []
    for index in range(config.warmups):
        requests.append(_text_request(f"{WARMUP_PREFIX}text_{index}", config.text_prompts[0]))
        requests.append(
            _tool_request(f"{WARMUP_PREFIX}tool_{index}", config.tool_prompts[0], config)
        )
    for index, prompt in enumerate(config.text_prompts):
        requests.append(_text_request(f"text_{index:02d}", prompt))
    for index, prompt in enumerate(config.tool_prompts):
        requests.append(_tool_request(f"tool_{index:02d}", prompt, config))
    return requests


def warmup_ids(records: Sequence[GenerationStats]) -> list[str]:
    return [r.generation_id for r in records if r.generation_id.startswith(WARMUP_PREFIX)]


async def run_probe(
    adapter: ModelAdapter,
    config: ProbeConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[GenerationStats], dict[str, dict[str, object]]]:
    """Run every probe request sequentially; return stats and outcomes.

    Outcomes hold, per generation id, the answer text (None for a tool
    proposal) and the parsed tool calls, so an evidence table's
    Proposal and Answer columns come from the export, not from memory.
    An adapter exception is recorded by the collector as adapter_error
    and the pass continues.
    """
    collector = TimingCollector(adapter, clock=clock)
    outcomes: dict[str, dict[str, object]] = {}
    for request in probe_requests(config):
        text_parts: list[str] = []
        tool_calls: list[dict[str, object]] = []
        try:
            async for event in collector.generate(request):
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
                elif isinstance(event, ToolCall):
                    tool_calls.append(
                        {"tool_name": event.tool_name, "arguments": dict(event.arguments)}
                    )
        except Exception:  # noqa: BLE001 - recorded as adapter_error by the collector
            pass
        outcomes[request.generation_id] = {
            "answer": "".join(text_parts) if text_parts else None,
            "tool_calls": tool_calls,
        }
    return collector.drain(), outcomes


def build_run_info(
    config: ProbeConfig,
    *,
    command: Sequence[str],
    cold_start_seconds: float | None,
    server_argv: Sequence[str],
    model_path: Path,
    warmup_generation_ids: Sequence[str],
    outcomes: Mapping[str, Mapping[str, object]],
    runtime_banner: str | None,
) -> dict[str, object]:
    """Everything that produced the numbers, for the export's "run" block."""
    return {
        "command": " ".join(command),
        "passes": 1,
        "cold_start_seconds": cold_start_seconds,
        "cold_start_definition": "llama-server process launch to the first /health 200",
        "warmups": config.warmups,
        "warmup_generation_ids": list(warmup_generation_ids),
        "manifest": str(config.manifest_path.name),
        "model_file": model_path.name,
        "server_argv": list(server_argv),
        "runtime_banner": runtime_banner,
        "system_prompt": config.system_prompt,
        "tool": {
            "name": config.tool.name,
            "description": config.tool.description,
            "input_schema": dict(config.tool.input_schema),
        },
        "text_prompts": list(config.text_prompts),
        "tool_prompts": list(config.tool_prompts),
        "outcomes": {key: dict(value) for key, value in outcomes.items()},
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _runtime_banner() -> str | None:
    try:
        result = subprocess.run(
            ["llama-server", "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    banner = (result.stdout + result.stderr).strip()
    return banner or None


def _launch_server(
    manifest: ArtifactManifest, model_path: Path, log_path: Path
) -> tuple[subprocess.Popen, str, list[str], float]:
    """Start llama-server from the manifest; return process, URL, argv, cold start."""
    server_argv = launch_settings_argv(manifest.launch_settings)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    argv = ["llama-server", "-m", str(model_path), "--host", "127.0.0.1", "--port", str(port)]
    argv += server_argv
    log_file = log_path.open("wb")
    started = time.monotonic()
    process = subprocess.Popen(argv, stdout=log_file, stderr=subprocess.STDOUT)
    try:
        deadline = started + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"llama-server exited early with {process.returncode}; see {log_path}"
                )
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    return process, base_url, server_argv, time.monotonic() - started
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise RuntimeError(
            f"llama-server not healthy in {STARTUP_TIMEOUT_SECONDS}s; see {log_path}"
        )
    except BaseException:
        _stop(process)
        raise
    finally:
        log_file.close()


def _stop(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


async def _measure(base_url: str, manifest: ArtifactManifest, config: ProbeConfig):
    ctx_size, template_kwargs = launch_adapter_settings(manifest.launch_settings)
    adapter = LlamaCppAdapter(
        LlamaCppConfig(
            base_url=base_url, context_limit=ctx_size, chat_template_kwargs=template_kwargs
        )
    )
    try:
        return await run_probe(adapter, config)
    finally:
        await adapter.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", type=Path, help="probe config JSON")
    parser.add_argument("--out", type=Path, required=True, help="export file path")
    parser.add_argument(
        "--model",
        type=Path,
        default=None,
        help="path to the GGUF file; defaults to the manifest's file_name next to the manifest",
    )
    args = parser.parse_args()

    try:
        config = load_probe_config(args.config)
        manifest = load_manifest(config.manifest_path)
    except (ValueError, ManifestError) as exc:
        parser.error(str(exc))
    if shutil.which("llama-server") is None:
        parser.error("llama-server is not on PATH")
    model_path = args.model or config.manifest_path.parent / manifest.file_name
    if not model_path.is_file():
        parser.error(f"model artifact not found: {model_path}")
    try:
        verify_artifact(manifest, model_path)
    except ManifestError as exc:
        parser.error(f"artifact failed verification: {exc}")

    log_path = args.out.with_suffix(".server.log")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        process, base_url, server_argv, cold_start = _launch_server(manifest, model_path, log_path)
    except RuntimeError as exc:
        parser.error(str(exc))
    try:
        records, outcomes = asyncio.run(_measure(base_url, manifest, config))
    finally:
        _stop(process)
        log_path.unlink(missing_ok=True)

    identity = AdapterIdentity(
        adapter="llamacpp",
        model_id=manifest.model_id,
        quantization=manifest.quantization,
        runtime_version=manifest.runtime_version,
    )
    run_info = build_run_info(
        config,
        command=[Path(sys.argv[0]).name, *sys.argv[1:]],
        cold_start_seconds=cold_start,
        server_argv=server_argv,
        model_path=model_path,
        warmup_generation_ids=warmup_ids(records),
        outcomes=outcomes,
        runtime_banner=_runtime_banner(),
    )
    export_stats(args.out, identity=identity, records=records, run_info=run_info)
    print(f"cold start: {cold_start:.2f} s")
    for record in records:
        if record.generation_id.startswith(WARMUP_PREFIX):
            continue
        outcome = outcomes[record.generation_id]
        action = record.first_model_action_seconds
        answer = record.first_answer_token_seconds
        shown = outcome["tool_calls"] or outcome["answer"]
        print(
            f"{record.generation_id}: first_model_action={action} "
            f"first_answer_token={answer} completion={record.completion_seconds} "
            f"finish={record.finish_reason} failure={record.failure_code} -> {shown}"
        )
    print(f"export: {args.out}")


if __name__ == "__main__":
    main()
