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
          "model_id": "qwen3-0.6b",
          "quantization": "Q8_0",
          "runtime_version": "build 11669 commit 620d2225c"
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

model_id, quantization, and runtime_version for a local artifact come
from its model manifest (models/*.manifest.json).
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from relaymind.adapters.base import ModelAdapter  # noqa: E402
from relaymind.adapters.chat_completions import (  # noqa: E402
    ChatCompletionsAdapter,
    ChatCompletionsConfig,
)
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig  # noqa: E402
from relaymind.stats import AdapterIdentity, run_comparison  # noqa: E402


def _build_adapter(name: str, spec: dict) -> tuple[ModelAdapter, AdapterIdentity]:
    kind = spec.get("kind")
    identity = AdapterIdentity(
        adapter=str(kind),
        model_id=str(spec.get("model_id", "")),
        quantization=spec.get("quantization"),
        runtime_version=spec.get("runtime_version"),
    )
    if kind == "llamacpp":
        config = LlamaCppConfig(
            base_url=spec["base_url"],
            context_limit=int(spec["context_limit"]),
        )
        return LlamaCppAdapter(config), identity
    if kind == "chat_completions":
        config = ChatCompletionsConfig(
            base_url=spec["base_url"],
            model=str(spec["model_id"]),
            context_limit=int(spec["context_limit"]),
            api_key_env=spec.get("api_key_env", "RELAYMIND_EXTERNAL_API_KEY"),
        )
        return ChatCompletionsAdapter(config), identity
    raise ValueError(f"adapter {name!r}: unknown kind {kind!r}")


async def _run(adapters, prompts, out_dir: Path) -> dict[str, Path]:
    try:
        return await run_comparison(adapters, prompts, out_dir)
    finally:
        for adapter, _identity in adapters.values():
            await adapter.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", type=Path, help="comparison config JSON")
    parser.add_argument("--out-dir", type=Path, required=True, help="export directory")
    args = parser.parse_args()

    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        parser.error(f"cannot read config: {exc}")
    prompts = config.get("prompts") or []
    adapter_specs = config.get("adapters") or {}
    if not prompts:
        parser.error("config has no prompts")
    if len(adapter_specs) < 2:
        parser.error("config needs at least two adapters to compare")
    try:
        adapters = {name: _build_adapter(name, spec) for name, spec in adapter_specs.items()}
    except (KeyError, TypeError, ValueError) as exc:
        parser.error(f"invalid adapter config: {exc}")

    paths = asyncio.run(_run(adapters, prompts, args.out_dir))
    for name, path in paths.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
