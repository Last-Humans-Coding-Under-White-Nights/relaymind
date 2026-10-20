"""Model artifact manifest (RM18, spec section 6).

The manifest pins one model artifact: source, revision, file SHA-256,
quantization, tokenizer and chat-template hashes, runtime version,
launch settings, and license metadata. A missing or mismatched hash is
an explicit failure, never a silent substitution.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

from relaymind.adapters.gguf import GGUFError, artifact_hashes

SUPPORTED_SCHEMA_VERSION = 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_HASH_FIELDS = ("file_sha256", "tokenizer_hash", "chat_template_hash")


class ManifestError(Exception):
    """The manifest is invalid or does not match the artifact."""


@dataclass(frozen=True)
class ArtifactManifest:
    """Pinned description of one model artifact."""

    schema_version: int
    model_id: str
    source_url: str
    source_revision: str
    file_name: str
    file_sha256: str
    quantization: str
    tokenizer_hash: str
    chat_template_hash: str
    runtime: str
    runtime_version: str
    launch_settings: Mapping[str, object]
    license: str

    def __post_init__(self) -> None:
        # bool is a subclass of int and True == 1, so check the exact type.
        if type(self.schema_version) is not int or self.schema_version != SUPPORTED_SCHEMA_VERSION:
            raise ManifestError(
                f"unsupported schema_version {self.schema_version!r}; "
                f"supported: {SUPPORTED_SCHEMA_VERSION}"
            )
        for f in fields(self):
            if f.type is str and not isinstance(getattr(self, f.name), str):
                raise ManifestError(f"{f.name} must be a string")
        if not isinstance(self.launch_settings, Mapping):
            raise ManifestError("launch_settings must be an object")
        object.__setattr__(self, "launch_settings", dict(self.launch_settings))
        for name in _HASH_FIELDS:
            if not _SHA256_RE.fullmatch(getattr(self, name)):
                raise ManifestError(f"{name} must be 64 lowercase hex characters")


def load_manifest(path: Path) -> ArtifactManifest:
    """Load and validate a manifest file."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")

    known = {f.name for f in fields(ArtifactManifest)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ManifestError(f"unknown manifest fields: {', '.join(unknown)}")
    missing = sorted(known - set(data))
    if missing:
        raise ManifestError(f"missing manifest fields: {', '.join(missing)}")
    return ArtifactManifest(**data)


def sha256_file(path: Path) -> str:
    """SHA-256 of a file, streamed."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    artifact_path: Path,
    *,
    model_id: str,
    source_url: str,
    source_revision: str,
    quantization: str,
    runtime: str,
    runtime_version: str,
    launch_settings: Mapping[str, object],
    license: str,
) -> ArtifactManifest:
    """Build a manifest from an actual artifact file.

    Every hash is computed from the artifact, never copied by hand.
    """
    artifact_path = Path(artifact_path)
    if not artifact_path.is_file():
        raise ManifestError(f"artifact file not found: {artifact_path}")
    tokenizer_hash, chat_template_hash = artifact_hashes(artifact_path)
    return ArtifactManifest(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        model_id=model_id,
        source_url=source_url,
        source_revision=source_revision,
        file_name=artifact_path.name,
        file_sha256=sha256_file(artifact_path),
        quantization=quantization,
        tokenizer_hash=tokenizer_hash,
        chat_template_hash=chat_template_hash,
        runtime=runtime,
        runtime_version=runtime_version,
        launch_settings=launch_settings,
        license=license,
    )


def verify_artifact(manifest: ArtifactManifest, artifact_path: Path) -> None:
    """Raise unless the artifact file matches the manifest hashes.

    The file hash always applies. When the file is a GGUF artifact, the
    tokenizer and chat-template hashes are recomputed too, so a manifest
    whose metadata hashes were edited by hand is also rejected.
    """
    artifact_path = Path(artifact_path)
    if not artifact_path.is_file():
        raise ManifestError(f"artifact file not found: {artifact_path}")
    actual = sha256_file(artifact_path)
    if actual != manifest.file_sha256:
        raise ManifestError(
            f"artifact sha256 mismatch for {artifact_path.name}: "
            f"manifest {manifest.file_sha256}, actual {actual}"
        )
    with open(artifact_path, "rb") as handle:
        is_gguf = handle.read(4) == b"GGUF"
    if is_gguf:
        try:
            tokenizer_hash, template_hash = artifact_hashes(artifact_path)
        except GGUFError as exc:
            raise ManifestError(
                f"cannot read GGUF metadata from {artifact_path.name}: {exc}"
            ) from exc
        if tokenizer_hash != manifest.tokenizer_hash:
            raise ManifestError(f"tokenizer hash mismatch for {artifact_path.name}")
        if template_hash != manifest.chat_template_hash:
            raise ManifestError(f"chat template hash mismatch for {artifact_path.name}")
