"""Building a manifest from an actual artifact file.

Covers RM18 / spec section 6: hashes in the manifest come from the
artifact itself, computed, not copied by hand. Issue #2.
"""

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from gguf_builder import sample_kvs, write_gguf
from relaymind.adapters.gguf import artifact_hashes
from relaymind.adapters.manifest import (
    ManifestError,
    build_manifest,
    load_manifest,
    sha256_file,
    verify_artifact,
)

# Resolved from this file, not the current working directory, so the
# suite passes no matter where pytest is invoked from.
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "gen_model_manifest.py"

SOURCE = dict(
    model_id="qwen3-0.6b",
    source_url="https://huggingface.co/Qwen/Qwen3-0.6B-GGUF",
    source_revision="23749fefcc72300e3a2ad315e1317431b06b590a",
    quantization="Q8_0",
    runtime="llama.cpp",
    runtime_version="build 11669 commit 620d2225c",
    launch_settings={"ctx_size": 8192, "jinja": True},
    license="Apache-2.0",
)


def test_build_manifest_computes_all_hashes_from_the_artifact(tmp_path):
    artifact = write_gguf(tmp_path / "model.gguf", sample_kvs())
    manifest = build_manifest(artifact, **SOURCE)

    assert manifest.file_name == "model.gguf"
    assert manifest.file_sha256 == sha256_file(artifact)
    tokenizer_hash, template_hash = artifact_hashes(artifact)
    assert manifest.tokenizer_hash == tokenizer_hash
    assert manifest.chat_template_hash == template_hash
    verify_artifact(manifest, artifact)


def test_build_manifest_rejects_a_missing_artifact(tmp_path):
    with pytest.raises(ManifestError):
        build_manifest(tmp_path / "absent.gguf", **SOURCE)


def test_build_manifest_rejects_a_file_that_is_not_gguf(tmp_path):
    # RM18: an unreadable artifact is a ManifestError, as in verify_artifact.
    artifact = tmp_path / "model.gguf"
    artifact.write_bytes(b"not a gguf file")
    with pytest.raises(ManifestError, match="model.gguf"):
        build_manifest(artifact, **SOURCE)


def test_gen_manifest_script_rejects_a_file_that_is_not_gguf(tmp_path):
    artifact = tmp_path / "model.gguf"
    artifact.write_bytes(b"not a gguf file")
    out = tmp_path / "manifest.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            str(artifact),
            "--out",
            str(out),
            *[f"--{k.replace('_', '-')}={v}" for k, v in SOURCE.items() if k != "launch_settings"],
            f"--launch-settings={json.dumps(SOURCE['launch_settings'])}",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "model.gguf" in result.stderr
    assert "Traceback" not in result.stderr
    assert not out.exists()


def test_verify_artifact_detects_tampered_metadata_hashes(tmp_path):
    artifact = write_gguf(tmp_path / "model.gguf", sample_kvs())
    manifest = build_manifest(artifact, **SOURCE)
    tampered = dataclasses.replace(manifest, tokenizer_hash="0" * 64)
    with pytest.raises(ManifestError):
        verify_artifact(tampered, artifact)


def test_gen_manifest_script_rejects_non_object_launch_settings(tmp_path):
    artifact = write_gguf(tmp_path / "model.gguf", sample_kvs())
    out = tmp_path / "manifest.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            str(artifact),
            "--out",
            str(out),
            *[f"--{k.replace('_', '-')}={v}" for k, v in SOURCE.items() if k != "launch_settings"],
            "--launch-settings=[1, 2]",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "launch-settings" in result.stderr
    assert "Traceback" not in result.stderr


def test_gen_manifest_script_rejects_launch_settings_that_do_not_render(tmp_path):
    # RM18: the generator never writes a manifest the renderer rejects.
    artifact = write_gguf(tmp_path / "model.gguf", sample_kvs())
    out = tmp_path / "manifest.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            str(artifact),
            "--out",
            str(out),
            *[f"--{k.replace('_', '-')}={v}" for k, v in SOURCE.items() if k != "launch_settings"],
            '--launch-settings={"model": "x", "no_mmap": true}',
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "model" in result.stderr
    assert "Traceback" not in result.stderr
    assert not out.exists()


def test_gen_manifest_script_writes_a_loadable_manifest(tmp_path):
    artifact = write_gguf(tmp_path / "model.gguf", sample_kvs())
    out = tmp_path / "manifest.json"
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            str(artifact),
            "--out",
            str(out),
            *[f"--{k.replace('_', '-')}={v}" for k, v in SOURCE.items() if k != "launch_settings"],
            f"--launch-settings={json.dumps(SOURCE['launch_settings'])}",
        ],
        check=True,
    )
    manifest = load_manifest(out)
    assert manifest.model_id == "qwen3-0.6b"
    verify_artifact(manifest, artifact)
