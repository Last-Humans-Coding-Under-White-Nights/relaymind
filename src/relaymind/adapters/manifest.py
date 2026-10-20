"""Model artifact manifest (RM18, spec section 6).

The manifest pins one model artifact: source, revision, file SHA-256,
quantization, tokenizer and chat-template hashes, runtime version,
launch settings, and license metadata. A missing or mismatched hash is
an explicit failure, never a silent substitution.
"""

import hashlib
import json
import math
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
        # Settings the renderer rejects fail here, when the manifest is
        # built or loaded, not later when a server is launched.
        launch_settings_argv(self.launch_settings)
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


# The harness owns the bind address and the artifact path; launch
# settings must not override them or load a model other than the
# pinned artifact (llama-server's -hf, -hff, -mu and -dr all pick one).
_RESERVED_LAUNCH_FLAGS = frozenset(
    {
        "host",
        "port",
        "model",
        "m",
        "hf-repo",
        "hf",
        "hfr",
        "hf-file",
        "hff",
        "model-url",
        "mu",
        "docker-repo",
        "dr",
    }
)

# A lowercase multi-character snake_case key, so it renders as one valid
# --long-flag. One spelling per flag: "ctx-size" is rejected, so it can
# never sit next to "ctx_size" with a different value.
_LAUNCH_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def _launch_flag(key: object) -> str:
    if not isinstance(key, str):
        raise ManifestError(f"launch setting key must be a string, got {key!r}")
    name = key.replace("_", "-")
    if name in _RESERVED_LAUNCH_FLAGS:
        raise ManifestError(f"launch setting {key!r} is reserved for the harness")
    if len(name) < 2 or not _LAUNCH_KEY_RE.fullmatch(key):
        raise ManifestError(
            f"launch setting {key!r} does not render as a valid --long-flag; "
            "use a lowercase snake_case key"
        )
    if name.startswith("no-"):
        raise ManifestError(
            f"launch setting {key!r}: use the positive name with false instead of a no- prefix"
        )
    return f"--{name}"


def launch_settings_argv(launch_settings: Mapping[str, object]) -> list[str]:
    """Render manifest launch settings as llama-server arguments.

    Snake_case keys become kebab-case flags; true is a bare flag and
    false its --no- form, a mapping is passed as compact JSON, and a
    string or finite number is passed as text. Any other value (a list,
    null, NaN, infinity) has no defined rendering and raises
    ManifestError, as do keys reserved for the harness (the bind address
    and anything that selects the model) or keys that are not valid
    snake_case long flags. Launch settings are manifest data (spec section 6): tests and
    tooling derive their command line here instead of hand-copying the
    qualified settings.
    """
    argv: list[str] = []
    for key, value in launch_settings.items():
        flag = _launch_flag(key)
        if isinstance(value, bool):
            argv.append(flag if value else f"--no-{flag[2:]}")
        elif isinstance(value, Mapping):
            try:
                rendered = json.dumps(dict(value), separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ManifestError(f"launch setting {key!r} is not JSON: {exc}") from exc
            argv.extend([flag, rendered])
        elif isinstance(value, float) and not math.isfinite(value):
            raise ManifestError(f"launch setting {key!r} must be a finite number, got {value!r}")
        elif isinstance(value, (int, float, str)):
            argv.extend([flag, str(value)])
        else:
            raise ManifestError(
                f"launch setting {key!r} has unsupported type {type(value).__name__}; "
                "use a string, number, boolean, or object"
            )
    return argv


def launch_adapter_settings(launch_settings: Mapping[str, object]) -> tuple[int, dict]:
    """Adapter context limit and base template kwargs from launch settings.

    The adapter must run with the same context size and chat-template
    kwargs the server was launched with (spec section 6), so both are
    read from the same validated launch settings that
    launch_settings_argv renders. Settings the adapter cannot use raise
    ManifestError.
    """
    launch_settings_argv(launch_settings)
    ctx_size = launch_settings.get("ctx_size")
    if type(ctx_size) is not int or ctx_size <= 0:
        raise ManifestError(
            f"launch_settings.ctx_size must be a positive integer, got {ctx_size!r}"
        )
    template_kwargs = launch_settings.get("chat_template_kwargs", {})
    if not isinstance(template_kwargs, Mapping):
        raise ManifestError(
            "launch_settings.chat_template_kwargs must be an object, "
            f"got {type(template_kwargs).__name__}"
        )
    return ctx_size, dict(template_kwargs)
