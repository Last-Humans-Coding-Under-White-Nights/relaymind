"""Run the same prompts against two or more adapter configs (issue #37).

Writes one machine-readable JSON export per config: spec 12.1 metrics
per generation with the adapter identity attached. Engineering
instrumentation for development comparisons, not the formal M6
benchmark; its numbers are never publishable results (spec 12.3).

Usage:
    python scripts/compare_adapters.py config.json --out-dir exports/

Config format:
    {
      "prompts": ["Reply with exactly one word: pong"],
      "adapters": {
        "local": {
          "kind": "llamacpp",
          "base_url": "http://127.0.0.1:8033",
          "context_limit": 8192,
          "manifest": "models/qwen3-0.6b-q8_0.manifest.json"
        },
        "external": {
          "kind": "chat_completions",
          "base_url": "https://api.provider.example/v1",
          "model_id": "provider-large-1",
          "context_limit": 128000,
          "api_key_env": "RELAYMIND_EXTERNAL_API_KEY"
        }
      }
    }

A llamacpp config names the model manifest of the artifact it serves
(models/*.manifest.json; a relative path resolves against the config
file's directory). Its model_id, quantization, and runtime_version are
read from that manifest and may not be typed into the config.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from relaymind.adapters.base import ModelAdapter
from relaymind.adapters.chat_completions import (
    ChatCompletionsAdapter,
    ChatCompletionsConfig,
)
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig
from relaymind.adapters.manifest import ManifestError, load_manifest
from relaymind.stats import AdapterIdentity, check_export_names, run_comparison

DEFAULT_API_KEY_ENV = "RELAYMIND_EXTERNAL_API_KEY"

# Identity fields a llamacpp config reads from its manifest.
_MANIFEST_FIELDS = ("model_id", "quantization", "runtime_version")


def _context_limit(name: str, spec: dict) -> int:
    value = spec["context_limit"]
    # bool is an int subclass; a float would be silently truncated.
    if type(value) is not int:
        raise ValueError(f"adapter {name!r}: context_limit must be an integer, got {value!r}")
    return value


def _build_adapter(
    name: str, spec: dict, *, base_dir: Path = Path()
) -> tuple[ModelAdapter, AdapterIdentity]:
    if not isinstance(spec, dict):
        raise ValueError(f"adapter {name!r} must be a JSON object")
    kind = spec.get("kind")
    if kind == "llamacpp":
        typed = [field for field in _MANIFEST_FIELDS if field in spec]
        if typed:
            raise ValueError(
                f"adapter {name!r}: {', '.join(typed)} come from the manifest; remove them"
            )
        if not isinstance(spec.get("manifest"), str):
            raise ValueError(f"adapter {name!r}: a llamacpp config needs a 'manifest' path")
        manifest = load_manifest(base_dir / spec["manifest"])
        identity = AdapterIdentity(
            adapter=kind,
            model_id=manifest.model_id,
            quantization=manifest.quantization,
            runtime_version=manifest.runtime_version,
        )
        config = LlamaCppConfig(
            base_url=spec["base_url"],
            context_limit=_context_limit(name, spec),
        )
        return LlamaCppAdapter(config), identity
    if kind == "chat_completions":
        identity = AdapterIdentity(
            adapter=kind,
            model_id=str(spec["model_id"]),
            quantization=spec.get("quantization"),
            runtime_version=spec.get("runtime_version"),
        )
        config = ChatCompletionsConfig(
            base_url=spec["base_url"],
            model=identity.model_id,
            context_limit=_context_limit(name, spec),
            api_key_env=spec.get("api_key_env") or DEFAULT_API_KEY_ENV,
        )
        return ChatCompletionsAdapter(config), identity
    raise ValueError(f"adapter {name!r}: unknown kind {kind!r}")


async def _run(adapters, prompts, out_dir: Path) -> dict[str, Path]:
    try:
        return await run_comparison(adapters, prompts, out_dir)
    finally:
        await _close_all(adapters)


async def _close_all(adapters) -> None:
    """Close every adapter; a close failure is reported, never raised.

    By the time adapters close, the exports are written, so a failing
    close must not hide the paths or another error behind a traceback.
    """
    for name, (adapter, _identity) in adapters.items():
        aclose = getattr(adapter, "aclose", None)
        if aclose is None:
            continue
        try:
            await aclose()
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            print(f"warning: closing adapter {name!r} failed: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", type=Path, help="comparison config JSON")
    parser.add_argument("--out-dir", type=Path, required=True, help="export directory")
    args = parser.parse_args()

    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        parser.error(f"cannot read config: {exc}")
    if not isinstance(config, dict):
        parser.error("config must be a JSON object")
    prompts = config.get("prompts")
    adapter_specs = config.get("adapters")
    if (
        not isinstance(prompts, list)
        or not prompts
        or not all(isinstance(prompt, str) for prompt in prompts)
    ):
        parser.error("config 'prompts' must be a non-empty list of strings")
    if not isinstance(adapter_specs, dict) or len(adapter_specs) < 2:
        parser.error("config needs an 'adapters' object with at least two adapters to compare")
    try:
        check_export_names(list(adapter_specs))
    except ValueError as exc:
        parser.error(f"invalid adapter config: {exc}")
    adapters: dict[str, tuple[ModelAdapter, AdapterIdentity]] = {}
    try:
        for name, spec in adapter_specs.items():
            adapters[name] = _build_adapter(name, spec, base_dir=args.config.parent)
    except (KeyError, TypeError, ValueError, ManifestError) as exc:
        # Close the adapters already built before exiting.
        asyncio.run(_close_all(adapters))
        parser.error(f"invalid adapter config: {exc}")

    paths = asyncio.run(_run(adapters, prompts, args.out_dir))
    for name, path in paths.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
