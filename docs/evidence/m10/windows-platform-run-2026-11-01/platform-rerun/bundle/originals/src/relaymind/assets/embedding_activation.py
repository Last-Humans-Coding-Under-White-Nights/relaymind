"""Closed backend dispatch and reviewed identity gates before load (RM38/RM41)."""

from collections.abc import Callable
from dataclasses import dataclass

from relaymind.assets.embedding_backends import EmbeddingEngine
from relaymind.assets.embedding_manifest import verify_embedding_files
from relaymind.assets.embedding_qualification import QUALIFIED_IDENTITIES, qualification_key
from relaymind.assets.embeddings import EmbeddingError
from relaymind.assets.sources import SourceFailure


@dataclass(frozen=True)
class BackendRegistration:
    runtime: Callable
    device: Callable
    load: Callable


def _production_backends():
    # Explicit implemented imports; manifest strings are never import expressions.
    from relaymind.assets.embedding_onnx import OnnxCpuBackend, device_identity, runtime_identity

    return {"onnx_e5_cpu": BackendRegistration(runtime_identity, device_identity, OnnxCpuBackend)}


class EmbeddingLoader:
    """Constructor injection belongs to trusted tests, never launch JSON/env fields."""

    def __init__(self, *, qualifications=None, backends=None):
        self.qualifications = (
            QUALIFIED_IDENTITIES if qualifications is None else frozenset(qualifications)
        )
        self._backends = backends

    def _registration(self, config):
        if qualification_key(config.manifest, config.digest) not in self.qualifications:
            raise EmbeddingError("unqualified_artifact")
        raw = config.manifest.data
        backends = _production_backends() if self._backends is None else self._backends
        entry = backends.get(raw["runtime"]["backend"])
        if entry is None:
            raise EmbeddingError("unsupported_embedding_backend")
        if dict(entry.runtime()) != dict(raw["runtime"]) or entry.device() != raw["device"]:
            raise EmbeddingError("embedding_runtime_mismatch")
        return entry

    def validate(self, config) -> None:
        self._registration(config)
        with verify_embedding_files(config.manifest):
            pass

    def load(self, config, *, control=None) -> EmbeddingEngine:
        registration = self._registration(config)
        with verify_embedding_files(config.manifest, control=control) as artifact:
            try:
                native = registration.load(artifact)
                artifact.guard()
                engine = artifact.run_cpu(EmbeddingEngine, config.manifest, native)
            except (EmbeddingError, SourceFailure):
                raise
            except Exception:
                raise EmbeddingError("embedding_model_unavailable") from None
        if control is not None:
            control.check_limits()
        return engine
