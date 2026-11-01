"""Pinned semantic settings and deferred native ownership (RM38/RM41/RM42)."""

from dataclasses import dataclass
from pathlib import Path

from relaymind.assets.embedding_activation import EmbeddingLoader
from relaymind.assets.embedding_backends import EmbeddingFailure
from relaymind.assets.embedding_config import load_adaptation_config, load_hybrid_config
from relaymind.assets.embedding_manifest import load_embedding_manifest
from relaymind.assets.embeddings import EmbeddingError
from relaymind.canonical import canonical_digest


def _locator(root, value):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 2048:
        raise EmbeddingError("invalid_semantic_path")
    return Path(root) / value


@dataclass(frozen=True)
class SemanticBundle:
    hybrid: object
    adaptation: object
    adaptation_enabled: bool
    loader: object

    def identity(self):
        def config(value):
            if value is None:
                return None
            return dict(
                config_digest=value.digest,
                manifest_digest=value.manifest.manifest_digest,
                artifact_hash=value.manifest.artifact_hash,
                runtime=value.manifest.data["runtime"],
                device=value.manifest.data["device"],
            )

        return dict(
            hybrid=config(self.hybrid),
            adaptation=config(self.adaptation),
            adaptation_enabled=self.adaptation_enabled,
            implementation=implementation_identity(),
        )

    def validate(self):
        for value in (self.hybrid, self.adaptation):
            if value is not None:
                self.loader.validate(value)


def implementation_identity():
    """Active implementation closure, independent of checkout/installation root."""
    from hashlib import sha256

    from relaymind import assets, selection

    inventory = {}
    modules = {
        assets: (
            "embeddings",
            "embedding_manifest",
            "embedding_config",
            "embedding_activation",
            "embedding_backends",
            "embedding_onnx",
            "embedding_bundle",
            "embedding_lifecycle",
            "scheduler",
        ),
        selection: ("asset_hybrid", "asset_adaptation", "adaptation_stage", "asset_chain"),
    }
    for package, names in modules.items():
        root = Path(package.__file__).parent
        for name in names:
            inventory[package.__name__ + "." + name] = sha256(
                (root / (name + ".py")).read_bytes()
            ).hexdigest()
    return canonical_digest(inventory)


def load_semantic_bundle(hybrid, adaptation, *, config_dir, loader=None):
    if not hybrid and not adaptation.get("enabled") and "config" not in adaptation:
        return None
    loader = EmbeddingLoader() if loader is None else loader
    hconfig = aconfig = None
    if hybrid:
        if set(hybrid) != {"manifest", "config"}:
            raise EmbeddingError("incomplete_hybrid_config")
        manifest = load_embedding_manifest(_locator(config_dir, hybrid["manifest"]))
        hconfig = load_hybrid_config(_locator(config_dir, hybrid["config"]), manifest)
    if "config" in adaptation:
        aconfig = load_adaptation_config(_locator(config_dir, adaptation["config"]))
    if adaptation.get("enabled") and aconfig is None:
        raise EmbeddingError("missing_adaptation_config")
    bundle = SemanticBundle(hconfig, aconfig, adaptation.get("enabled", False), loader)
    bundle.validate()
    return bundle


class DeferredEmbedding:
    """Only the shared background owner can load this engine."""

    engine = None

    async def embed(self, *args, **kwargs):
        if self.engine is None:
            raise EmbeddingFailure("embedding_not_ready")
        return await self.engine.embed(*args, **kwargs)


class SemanticLifecycle:
    def __init__(self, bundle, provider, hybrid_owner=None):
        self.bundle, self.provider, self.hybrid_owner = bundle, provider, hybrid_owner
        self.adaptation = DeferredEmbedding()
        self.config_digest = canonical_digest(bundle.identity())

    def generation_config(self):
        return self.config_digest

    @property
    def revision(self):
        return None if self.hybrid_owner is None else self.hybrid_owner.revision

    async def rebuild(self, *, context, run_cpu):
        from relaymind.store.asset_catalog import Publication

        result = Publication(self.provider.store.revision(), False, (), ())
        if self.hybrid_owner is not None:
            result = await self.hybrid_owner.rebuild(context=context, run_cpu=run_cpu)
        if self.bundle.adaptation_enabled and self.adaptation.engine is None:
            shared = None if self.hybrid_owner is None else self.hybrid_owner.selector.engine
            if (
                shared is not None
                and shared.manifest.manifest_digest
                == self.bundle.adaptation.manifest.manifest_digest
            ):
                # Both semantic configs qualified before startup. Native bytes/runtime
                # are identical; each call still supplies its own pinned representation.
                self.adaptation.engine = shared
            else:
                self.adaptation.engine = await run_cpu(
                    self.bundle.loader.load, self.bundle.adaptation, control=context
                )
        return result

    async def stop(self):
        if self.hybrid_owner is not None:
            await self.hybrid_owner.stop()
        if self.adaptation.engine is not None:
            await self.adaptation.engine.drain()


def effective_launch_identity(config, redactor):
    """Canonical effective launch values, with semantic preimages and implementation closure."""
    from collections.abc import Mapping
    from dataclasses import fields, is_dataclass
    from enum import Enum

    from relaymind.assets.catalog import acquisition_budgets

    def value(item, name=""):
        if name in ("api_key_env", "secret_ref", "registrations_path") or redactor.is_secret(name):
            return "[redacted]"
        if name == "asset_semantics":
            return None if item is None else item.identity()
        if isinstance(item, Path):
            return str(item)
        if isinstance(item, Enum):
            return item.value
        if is_dataclass(item):
            return {f.name: value(getattr(item, f.name), f.name) for f in fields(item)}
        if isinstance(item, Mapping):
            return {key: value(child, key) for key, child in item.items()}
        if isinstance(item, set | frozenset):
            return sorted(value(child) for child in item)
        if isinstance(item, list | tuple):
            return [value(child) for child in item]
        return item

    return dict(launch=redactor.redact(value(config)), scheduler=acquisition_budgets())
