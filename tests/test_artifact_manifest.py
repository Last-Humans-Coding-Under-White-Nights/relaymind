"""Model artifact manifest: format, validation, verification.

Covers RM18 and spec section 6: the manifest records model source,
revision, file SHA-256, quantization, tokenizer and template hashes,
runtime commit, launch settings, and license metadata. Missing or
mismatched hashes cause an explicit failure. Issue #2.
"""

import dataclasses
import hashlib
import json

import pytest

from helpers import (
    LIVE_MANIFEST_ENV,
    MODELS_DIR,
    PINNED_MANIFEST_PATH,
    REPO_ROOT,
    SECOND_MANIFEST_PATH,
    live_manifest_path,
)
from relaymind.adapters.manifest import (
    ArtifactManifest,
    ManifestError,
    load_manifest,
    verify_artifact,
)

VALID = {
    "schema_version": 1,
    "model_id": "qwen3-0.6b",
    "source_url": "https://huggingface.co/Qwen/Qwen3-0.6B-GGUF",
    "source_revision": "23749fefcc72300e3a2ad315e1317431b06b590a",
    "file_name": "Qwen3-0.6B-Q8_0.gguf",
    "file_sha256": "a" * 64,
    "quantization": "Q8_0",
    "tokenizer_hash": "b" * 64,
    "chat_template_hash": "c" * 64,
    "runtime": "llama.cpp",
    "runtime_version": "b6730",
    "launch_settings": {"ctx_size": 8192, "jinja": True},
    "license": "Apache-2.0",
}


def write_manifest(tmp_path, data):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_round_trip(tmp_path):
    path = write_manifest(tmp_path, VALID)
    manifest = load_manifest(path)
    assert manifest == ArtifactManifest(**VALID)
    assert dataclasses.asdict(manifest) == VALID
    assert manifest.quantization == "Q8_0"


def test_load_rejects_missing_field(tmp_path):
    data = dict(VALID)
    del data["file_sha256"]
    path = write_manifest(tmp_path, data)
    with pytest.raises(ManifestError) as exc:
        load_manifest(path)
    assert "file_sha256" in str(exc.value)


def test_load_rejects_unknown_field(tmp_path):
    path = write_manifest(tmp_path, VALID | {"surprise": True})
    with pytest.raises(ManifestError) as exc:
        load_manifest(path)
    assert "surprise" in str(exc.value)


def test_load_rejects_malformed_sha256(tmp_path):
    path = write_manifest(tmp_path, VALID | {"file_sha256": "zz"})
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_load_rejects_unsupported_schema_version(tmp_path):
    path = write_manifest(tmp_path, VALID | {"schema_version": 2})
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_load_rejects_boolean_schema_version(tmp_path):
    # bool is a subclass of int and True == 1; it must not pass as 1.
    path = write_manifest(tmp_path, VALID | {"schema_version": True})
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_load_rejects_non_string_hash_with_manifest_error(tmp_path):
    for bad in (None, 123, ["a" * 64]):
        path = write_manifest(tmp_path, VALID | {"file_sha256": bad})
        with pytest.raises(ManifestError):
            load_manifest(path)


def test_load_rejects_wrongly_typed_fields(tmp_path):
    path = write_manifest(tmp_path, VALID | {"launch_settings": "not a dict"})
    with pytest.raises(ManifestError):
        load_manifest(path)
    path = write_manifest(tmp_path, VALID | {"model_id": 7})
    with pytest.raises(ManifestError):
        load_manifest(path)


@pytest.mark.parametrize("bad", [{"model": "x"}, {"no_mmap": True}, {"temp": float("nan")}])
def test_launch_settings_that_do_not_render_fail_at_creation(bad):
    # RM18: an unusable manifest fails when it is created or loaded, not
    # later when the live suite renders its server argv.
    with pytest.raises(ManifestError):
        ArtifactManifest(**VALID | {"launch_settings": bad})


def test_load_rejects_launch_settings_that_do_not_render(tmp_path):
    path = write_manifest(tmp_path, VALID | {"launch_settings": {"hf_repo": "x/y"}})
    with pytest.raises(ManifestError, match="hf_repo"):
        load_manifest(path)


@pytest.mark.parametrize("path", sorted(MODELS_DIR.glob("*.manifest.json")), ids=lambda p: p.name)
def test_the_checked_in_manifests_are_valid(path):
    manifest = load_manifest(path)
    assert manifest.schema_version == 1


@pytest.mark.parametrize(
    "path", sorted(MODELS_DIR.glob("qwen3-0.6b-*.manifest.json")), ids=lambda p: p.name
)
def test_qwen_manifests_keep_their_artifact_names_and_launch_settings(path):
    # RM18, spec section 6: these naming and reasoning settings belong to
    # this candidate; other models still pass the general validation above.
    manifest = load_manifest(path)
    assert manifest.model_id == "qwen3-0.6b"
    assert manifest.launch_settings["reasoning"] == "off"
    assert manifest.launch_settings["reasoning_format"] == "deepseek"
    # Spec section 6: never relabeled. The file and the manifest name the
    # quantization the file holds.
    assert manifest.file_name == f"Qwen3-0.6B-{manifest.quantization}.gguf"
    assert path.name == f"qwen3-0.6b-{manifest.quantization.lower()}.manifest.json"


def test_the_second_artifact_differs_only_in_the_artifact():
    """AC08, RM18: the second local artifact is a different file of the same
    model, on the same runtime and launch settings, so swapping the two
    is a configuration change only."""
    first = load_manifest(PINNED_MANIFEST_PATH)
    second = load_manifest(SECOND_MANIFEST_PATH)
    assert second.model_id == first.model_id
    assert second.quantization != first.quantization
    assert second.file_sha256 != first.file_sha256
    assert second.runtime == first.runtime
    assert second.runtime_version == first.runtime_version
    assert second.launch_settings == first.launch_settings


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, PINNED_MANIFEST_PATH),
        ({LIVE_MANIFEST_ENV: ""}, PINNED_MANIFEST_PATH),
        (
            {LIVE_MANIFEST_ENV: "elsewhere/other.manifest.json"},
            REPO_ROOT / "elsewhere/other.manifest.json",
        ),
    ],
)
def test_the_live_suite_manifest_is_a_configuration_value(environ, expected):
    # AC08: the live suite runs against the pinned manifest unless the
    # variable names another, with no code change.
    assert live_manifest_path(environ) == expected


def test_verify_accepts_matching_artifact(tmp_path):
    artifact = tmp_path / "model.gguf"
    artifact.write_bytes(b"model-bytes")
    digest = hashlib.sha256(b"model-bytes").hexdigest()
    manifest = ArtifactManifest(**VALID | {"file_sha256": digest})
    verify_artifact(manifest, artifact)


def test_verify_fails_explicitly_on_hash_mismatch(tmp_path):
    artifact = tmp_path / "model.gguf"
    artifact.write_bytes(b"tampered-bytes")
    manifest = ArtifactManifest(**VALID)
    with pytest.raises(ManifestError) as exc:
        verify_artifact(manifest, artifact)
    assert "sha256" in str(exc.value).lower()


def test_verify_fails_explicitly_on_missing_file(tmp_path):
    manifest = ArtifactManifest(**VALID)
    with pytest.raises(ManifestError):
        verify_artifact(manifest, tmp_path / "absent.gguf")
