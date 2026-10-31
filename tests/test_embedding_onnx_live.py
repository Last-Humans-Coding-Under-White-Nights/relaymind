"""RM41/AC32 actual local candidate probes; separate from deterministic acceptance."""

import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest


@pytest.mark.live
def test_pinned_onnx_candidate_uses_verified_segmented_inputs_and_actual_inference():
    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.assets.embedding_manifest import load_embedding_manifest, verify_embedding_files
    from relaymind.assets.embedding_onnx import OnnxCpuBackend, device_identity, runtime_identity
    from relaymind.assets.embeddings import cosine, represent_query

    root = Path("/var/tmp/rm-m10-embedding-development/e5-small")
    if not (root / "download-evidence.json").is_file():
        pytest.skip("Explicitly downloaded candidate required; no automatic test download")
    source = json.loads((root / "download-evidence.json").read_text())
    roles = {
        "onnx/model_qint8_avx512_vnni.onnx": ["weights"],
        "tokenizer.json": ["tokenizer"],
        "config.json": ["configuration"],
        "README.md": ["license"],
    }
    manifest = dict(
        schema_version=1,
        model=dict(name="intfloat/multilingual-e5-small", version=source["revision"]),
        source=dict(url=source["source"], revision=source["revision"]),
        quantization="qint8_avx512_vnni",
        license="MIT",
        runtime=runtime_identity(),
        device=device_identity(),
        dimensions=384,
        files=[
            dict(path=f["path"], roles=roles[f["path"]], bytes=f["bytes"], sha256=f["sha256"])
            for f in source["files"]
        ],
    )
    path = root / "embedding.manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    parsed = load_embedding_manifest(path)
    with verify_embedding_files(parsed) as artifact:
        native = OnnxCpuBackend(artifact)
    engine = EmbeddingEngine(parsed, native)
    batch = asyncio.run(
        engine.embed(
            [
                represent_query("分析应用内存泄漏的根因", "query: "),
                represent_query(
                    "Analyze application memory leaks and diagnose retained objects", "query: "
                ),
                represent_query("Schedule a calendar meeting", "query: "),
            ],
            max_tokens=512,
            deadline=time.monotonic() + 10,
        )
    )
    assert len(batch.vectors) == 3 and len(batch.vectors[0]) == 384
    assert cosine(batch.vectors[0], batch.vectors[1]) > cosine(batch.vectors[0], batch.vectors[2])
    assert batch.observation["cost_observations"][0]["value"]["calls"] == 1
    assert batch.observation["input_tokens"] > 0
    assert batch.observation["duration_ms"] > 0
    evidence = dict(
        profile="source_grounded_development",
        qualification="provisional",
        acceptance_eligible=False,
        manifest_digest=parsed.manifest_digest,
        artifact_hash=parsed.artifact_hash,
        runtime=manifest["runtime"],
        device=manifest["device"],
        observation=dict(batch.observation),
        cross_language_cosine=cosine(batch.vectors[0], batch.vectors[1]),
        unrelated_cosine=cosine(batch.vectors[0], batch.vectors[2]),
    )
    (root / "native-probe.json").write_text(json.dumps(evidence, indent=2) + "\n")
    assert (
        hashlib.sha256((root / "onnx/model_qint8_avx512_vnni.onnx").read_bytes()).hexdigest()
        == "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88"
    )
