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
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
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
from relaymind.adapters.local_http import health_client
from relaymind.adapters.manifest import (
    _RESERVED_LAUNCH_FLAGS,
    ArtifactManifest,
    ManifestError,
    launch_adapter_settings,
    launch_settings_argv,
    load_manifest,
    verify_artifact,
)
from relaymind.stats import AdapterIdentity, GenerationStats, TimingCollector, export_stats

STARTUP_TIMEOUT_SECONDS = 120
HEALTH_POLL_SECONDS = 0.05


def parse_runtime_extra_args(text: str) -> tuple[str, ...]:
    """Split the text of --runtime-extra-args into llama-server arguments (#117).

    The text is split like a shell would split it. It is the argparse
    type of the option, so a malformed text is a usage error. The default
    of the option is no argument.
    """
    try:
        return tuple(shlex.split(text))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"cannot split the arguments ({exc}); check the quotes"
        ) from exc


def _is_number(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


def _flag_name(token: str) -> str:
    """The setting a long flag names: no dashes, no value, no --no- prefix."""
    name = token.removeprefix("--").split("=", 1)[0]
    return name.removeprefix("no-")


def check_runtime_extra_args(
    extra_args: Sequence[str], pinned_argv: Sequence[str]
) -> tuple[str, ...]:
    """Extra llama-server arguments, refused when they would change the pinned setup (RM18).

    The manifest pins the model, the chat template, and the launch
    settings (spec section 6), and the harness sets the bind address.
    Extra arguments only add settings next to them, such as a CPU-only
    runtime with fewer threads (spec 0.31 section 12.2). Only long flags
    are accepted, so a short alias cannot reach a pinned flag; a number
    after a flag is its value. Raises ValueError with the reason.
    """
    pinned = {_flag_name(token) for token in pinned_argv if token.startswith("--")}
    for index, token in enumerate(extra_args):
        if not token.startswith("-") or _is_number(token):
            if index == 0:
                raise ValueError(f"extra arguments must start with a flag, not {token!r}")
            continue
        if not token.startswith("--"):
            raise ValueError(f"extra argument {token!r} is not a long flag (--name)")
        name = _flag_name(token)
        if name in _RESERVED_LAUNCH_FLAGS:
            raise ValueError(f"extra argument {token!r} is set by the harness or selects a model")
        if name in pinned:
            raise ValueError(
                f"extra argument {token!r} would change a pinned setting of the manifest"
            )
    return tuple(extra_args)


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
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")
    manifest = raw.get("manifest")
    if not isinstance(manifest, str) or not manifest:
        raise ValueError("'manifest' must be a path to a model manifest")
    manifest_path = (path.parent / manifest).resolve()
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


def probe_requests(config: ProbeConfig) -> tuple[list[GenerationRequest], list[GenerationRequest]]:
    """The warm-up requests and the measured pass, in send order.

    Each warm-up round sends the first plain prompt and the first tool
    prompt, so the first measured tool call is not the first request
    that ever carried the system prompt and tool schema.
    """
    warmups: list[GenerationRequest] = []
    for index in range(config.warmups):
        warmups.append(_text_request(f"warmup_text_{index}", config.text_prompts[0]))
        warmups.append(_tool_request(f"warmup_tool_{index}", config.tool_prompts[0], config))
    measured = [
        _text_request(f"text_{index:02d}", prompt)
        for index, prompt in enumerate(config.text_prompts)
    ]
    measured += [
        _tool_request(f"tool_{index:02d}", prompt, config)
        for index, prompt in enumerate(config.tool_prompts)
    ]
    return warmups, measured


async def run_probe(
    adapter: ModelAdapter, config: ProbeConfig
) -> tuple[list[GenerationStats], dict[str, dict[str, object]]]:
    """Run every probe request sequentially; return stats and outcomes.

    Outcomes hold, per generation id, the answer text (None for a tool
    proposal) and the parsed tool calls, so an evidence table's
    Proposal and Answer columns come from the export, not from memory.
    An adapter exception is recorded by the collector as adapter_error
    and the pass continues.
    """
    collector = TimingCollector(adapter)
    outcomes: dict[str, dict[str, object]] = {}
    warmups, measured = probe_requests(config)
    for request in warmups + measured:
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
    command: list[str],
    cold_start_seconds: float,
    server_argv: list[str],
    model_path: Path,
    warmup_generation_ids: list[str],
    outcomes: dict[str, dict[str, object]],
    runtime_banner: str | None,
) -> dict[str, object]:
    """Everything that produced the numbers, for the export's "run" block."""
    return {
        "command": " ".join(command),
        "passes": 1,
        "cold_start_seconds": cold_start_seconds,
        "cold_start_definition": (
            "llama-server process launch to the first /health 200; the artifact was "
            "read in full for SHA-256 verification just before launch, so its pages "
            "were warm in the OS file cache"
        ),
        "warmups": config.warmups,
        "warmup_generation_ids": warmup_generation_ids,
        "manifest": config.manifest_path.name,
        "model_file": model_path.name,
        "server_argv": server_argv,
        "runtime_banner": runtime_banner,
        "system_prompt": config.system_prompt,
        "tool": {
            "name": config.tool.name,
            "description": config.tool.description,
            "input_schema": dict(config.tool.input_schema),
        },
        "text_prompts": list(config.text_prompts),
        "tool_prompts": list(config.tool_prompts),
        "outcomes": outcomes,
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
    model_path: Path, server_argv: list[str], log_path: Path
) -> tuple[subprocess.Popen, str, float]:
    """Start llama-server; return the process, its URL, and the cold start.

    The HTTP client is built before the clock starts so only the server
    is inside the measured window.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    argv = ["llama-server", "-m", str(model_path), "--host", "127.0.0.1", "--port", str(port)]
    argv += server_argv
    with health_client(base_url) as client:
        with log_path.open("wb") as log_file:
            started = time.monotonic()
            process = subprocess.Popen(argv, stdout=log_file, stderr=subprocess.STDOUT)
        try:
            while time.monotonic() - started < STARTUP_TIMEOUT_SECONDS:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"llama-server exited early with {process.returncode}; see {log_path}"
                    )
                try:
                    if client.get("/health").status_code == 200:
                        return process, base_url, time.monotonic() - started
                except httpx.HTTPError:
                    pass
                time.sleep(HEALTH_POLL_SECONDS)
        except BaseException:
            _stop(process)
            raise
    _stop(process)
    raise RuntimeError(f"llama-server not healthy in {STARTUP_TIMEOUT_SECONDS}s; see {log_path}")


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

    server_argv = launch_settings_argv(manifest.launch_settings)
    log_path = args.out.with_suffix(".server.log")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        process, base_url, cold_start = _launch_server(model_path, server_argv, log_path)
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
    warmups, _measured = probe_requests(config)
    warmup_generation_ids = [request.generation_id for request in warmups]
    run_info = build_run_info(
        config,
        # The script path as typed, so the recorded command runs from
        # the same working directory it was recorded in.
        command=["python", *sys.argv],
        cold_start_seconds=cold_start,
        server_argv=server_argv,
        model_path=model_path,
        warmup_generation_ids=warmup_generation_ids,
        outcomes=outcomes,
        runtime_banner=_runtime_banner(),
    )
    export_stats(args.out, identity=identity, records=records, run_info=run_info)
    print(f"cold start: {cold_start:.2f} s")
    skip = set(warmup_generation_ids)
    for record in records:
        if record.generation_id in skip:
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
