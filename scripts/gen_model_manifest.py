#!/usr/bin/env python3
"""Generate a model artifact manifest from a GGUF file (RM18, spec section 6).

Example:
    python scripts/gen_model_manifest.py models/Qwen3-0.6B-Q8_0.gguf \
        --out models/qwen3-0.6b-q8_0.manifest.json \
        --model-id qwen3-0.6b \
        --source-url https://huggingface.co/Qwen/Qwen3-0.6B-GGUF \
        --source-revision <hf-commit-sha> \
        --quantization Q8_0 \
        --runtime llama.cpp \
        --runtime-version "build 11669 commit 620d2225c" \
        --launch-settings '{"ctx_size": 8192, "jinja": true}' \
        --license Apache-2.0
"""

import argparse
import dataclasses
import json
from pathlib import Path

# Needs the package installed (pip install -e ".[dev]"), like everything else.
from relaymind.adapters.manifest import build_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, help="path to the GGUF file")
    parser.add_argument("--out", type=Path, required=True, help="manifest output path")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--quantization", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--launch-settings", required=True, help="JSON object")
    parser.add_argument("--license", required=True)
    args = parser.parse_args()

    manifest = build_manifest(
        args.artifact,
        model_id=args.model_id,
        source_url=args.source_url,
        source_revision=args.source_revision,
        quantization=args.quantization,
        runtime=args.runtime,
        runtime_version=args.runtime_version,
        launch_settings=json.loads(args.launch_settings),
        license=args.license,
    )
    args.out.write_text(json.dumps(dataclasses.asdict(manifest), indent=2) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
