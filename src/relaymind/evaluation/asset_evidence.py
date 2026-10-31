"""Explicit offline evidence, production projections and inventories (RM40/AC31).

Hashes establish integrity only. This module never grants source access, infers
human review or fetches artifacts. Private source locators are attestation data.
"""

import copy
import functools
import hashlib
import importlib.metadata
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from relaymind.assets.catalog import acquisition_budgets
from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_benchmark_io import (
    CONFIG_BYTES,
    read_json_path,
    validate_benchmark_document,
)
from relaymind.evaluation.asset_corpus import load_asset_release
from relaymind.evaluation.asset_corpus_io import (
    DOCUMENT_BYTES,
    ORIGINAL_BYTES,
    AssetCorpusError,
    parse_json,
    read_local,
    relative_path,
)
from relaymind.evaluation.environment import machine_manifest
from relaymind.recommendations.limits import recommendation_budgets

if TYPE_CHECKING:
    from relaymind.evaluation.asset_corpus import VerifiedAssetRelease
    from relaymind.launcher import LauncherConfig


def validate_definition(name: str, value: Mapping) -> dict:
    return validate_benchmark_document("report", value, definition=name)


def semantic_document(payload: bytes, format: str, *, semantic: bool = False) -> dict:
    try:
        if semantic:
            from relaymind.assets.embedding_manifest import (
                parse_semantic_json,
                semantic_document_copy,
            )

            if len(payload) > 65536:
                raise AssetCorpusError("configuration_limit")
            value = (
                parse_semantic_json(payload)
                if format == "json"
                else semantic_document_copy(tomllib.loads(payload.decode("utf-8")))
            )
        else:
            value = (
                parse_json(payload) if format == "json" else tomllib.loads(payload.decode("utf-8"))
            )
        canonical_digest(value)
        return value
    except (ValueError, TypeError, UnicodeError):
        raise AssetCorpusError("invalid_document") from None


class OfflineResolver:
    """One explicit root, lookup by complete committed identity and byte count."""

    def __init__(self, path: Path):
        self.path = Path(path).absolute()
        raw, _ = read_json_path(self.path)
        validate_definition("OfflineResolverV1", raw)
        keys = [(e["id"], e["hash_kind"], e["hash"]) for e in raw["entries"]]
        if keys != sorted(set(keys)):
            raise AssetCorpusError("resolver_duplicate_or_order")
        self.entries = dict(zip(keys, raw["entries"], strict=True))

    def contains(self, id: str, hash_kind: str, hash: str) -> bool:
        return (id, hash_kind, hash) in self.entries

    def read(
        self,
        id: str,
        hash_kind: str,
        hash: str,
        bytes: int,
        *,
        format: str = "json",
        limit: int = ORIGINAL_BYTES,
    ) -> bytes:
        item = self.entries.get((id, hash_kind, hash))
        if item is None:
            raise AssetCorpusError("input_unresolved")
        if item["bytes"] != bytes:
            raise AssetCorpusError("resolver_bytes")
        payload = read_local(self.path.parent, item["path"], limit)
        digest = (
            canonical_digest(semantic_document(payload, format))
            if hash_kind == "canonical"
            else hashlib.sha256(payload).hexdigest()
        )
        if len(payload) != bytes or digest != hash:
            raise AssetCorpusError("resolver_digest")
        return payload


def resolve_reference(
    item: Mapping, root: Path, resolver: OfflineResolver | None, *, evidence: bool = False
) -> bytes:
    digest = item["sha256"] if evidence else item["digest"]
    kind = "original" if evidence else "canonical"
    limit = (
        ORIGINAL_BYTES
        if evidence
        else 65536
        if item["kind"] == "referenced_config"
        else DOCUMENT_BYTES
        if item["kind"] in ("catalog", "cases", "labels")
        else CONFIG_BYTES
    )
    if resolver is not None and resolver.contains(item["id"], kind, digest):
        return resolver.read(
            item["id"], kind, digest, item["bytes"], format=item.get("format", "json"), limit=limit
        )
    if item["path"] is None:
        raise AssetCorpusError("input_unresolved")
    payload = read_local(root, item["path"], limit)
    actual = (
        hashlib.sha256(payload).hexdigest()
        if evidence
        else canonical_digest(semantic_document(payload, item["format"]))
    )
    if len(payload) != item["bytes"] or actual != digest:
        raise AssetCorpusError("input_digest")
    return payload


def file_evidence_id(path: str) -> str:
    """The resolver ID of one inventory file's original bytes."""
    return "file:" + hashlib.sha256(path.encode()).hexdigest()


def file_inventory(root: Path, *, protocol: bool = False) -> dict:
    root = Path(root)
    paths = (
        [Path("docs/SPECIFICATION.md")]
        if protocol
        else [Path("pyproject.toml"), Path("scripts/asset_selection_benchmark.py")]
    )
    suffix = "*.json" if protocol else "*.py"
    paths.extend(
        path.relative_to(root)
        for path in (root / "src/relaymind").rglob(suffix)
        if "__pycache__" not in path.parts
    )
    if len(paths) > 10000:
        raise AssetCorpusError("inventory_limit")
    files = []
    for path in sorted(set(paths), key=lambda p: p.as_posix()):
        payload = read_local(root, path.as_posix(), CONFIG_BYTES)
        files.append(
            dict(
                path=path.as_posix(), sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload)
            )
        )
    return dict(schema_version=1, files=files)


# Non-package build inputs are reviewed anchors, not locations inferred from a
# report, cwd or checkout during offline comparison. The package itself is
# verified below against the actual imported implementation. New anchor bytes
# require a reviewed implementation update, never caller self-attestation.
_GENERATOR_ANCHORS = {
    "pyproject.toml": frozenset(
        {
            ("ef222b8befa59a1edb4cdc3a0718bb4c1f5bdda0ab9dca0cb1d21f42c020872c", 2244),
            # Reviewed semantic/refresh integration adds only optional embedding extras.
            ("950dff8707e05c8b16e3ea6c55e32c2a78597b3318f236c16023eab8da5b4cc2", 2397),
        }
    ),
    "scripts/asset_selection_benchmark.py": frozenset(
        {("faac6cd65170f4f2f330cfee875daaef3f7d5dfa4f72f0633f3d08cfe54c4f6b", 182)}
    ),
    "docs/SPECIFICATION.md": frozenset(
        {
            # Reviewed native0.62 direct-action contract with the inherited
            # activation registry clarification from the Native parent.
            ("4146586ecdf314d2144c62cf5288a3185e48350c58ddfc046a883646f50f30fe", 581129),
            # Reviewed native0.62 direct-action contract (ad09abd).
            ("bd412e5fd93cc3d187d03b57ae3e8af2c67e709fb84ad6171a6ed1cf10b20d63", 580897),
            # Reviewed native 0.61 activation registry clarification.
            ("0ca7d5cdf2a842dc636525d372cafd7ed9c1e63400788e844ff0a3b2ebf37d7d", 569761),
            # Reviewed native0.61 and corrected Source/master union (9c1c0ddb).
            ("0fe6b740ad6303782112c39dbb98b193ff242b88ad0e5ec708eb6a779770fc71", 569529),
            # Reviewed normative0.61 local preparation and runtime display (082202f).
            ("070693f5fd489a9076e6a663575d7af986cc92a8bac6623789ce20c6ae305912", 568282),
            # Reviewed Source 0.59 and master benchmark union (cd7b8a95).
            ("184d14da9cfca536769744967fdf87811b7fcbaf6de0603e17943f8f59f70d86", 527306),
            # Reviewed normative0.59 semantic and qualified-refresh contracts.
            ("056c4c32119be9385b1c39e617f6b61a59bb48006fbcf4b21d2baf6a37607b95", 526059),
            # Reviewed master 0.56 and preparation 0.57 union (769cc247).
            ("b66b775e4d40cd9386689c43c5a7bce98243e08683b81967aa3cec0132beb756", 485852),
            # Reviewed normative0.57 companion preparation contract (d1490fe).
            ("8c2a7c3b64e04ea97f94c09c7a78e041567829f28e04e320cd54860e271036c9", 484605),
            # The unintegrated fixture parent is recorded honestly; the published
            # 0.55 contract and its reviewed ordering clarification are supported.
            ("85cb23f1e985c857aa4e507b9cc3a88ab94999b434535ce7a659c4294c98e172", 420774),
            ("26c5f46f556df4ba603c7c739228500914112f8750e2cf61675e0a9666a15acb", 464062),
            ("c35e250824a01f16ee94f812bf0bba95a631608db0a8daf97ed4440d9042ef7c", 464557),
        }
    ),
}


def verify_compiled_asset_inventory(kind: str, inventory: Mapping) -> None:
    """Require every imported Python/schema member and a supported generator.

    Reading the program's own installed resources establishes compiled identity;
    it is not an alternative lookup root for missing comparison input evidence.
    Supplied originals are still resolved and verified independently.
    """
    package_root = Path(__file__).absolute().parents[1]
    suffix = "*.py" if kind == "runner_inventory" else "*.json"
    anchors = (
        {"pyproject.toml", "scripts/asset_selection_benchmark.py"}
        if kind == "runner_inventory"
        else {"docs/SPECIFICATION.md"}
    )
    expected = {}
    for path in package_root.rglob(suffix):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_root).as_posix()
        payload = read_local(package_root, relative, CONFIG_BYTES)
        expected["src/relaymind/" + relative] = (hashlib.sha256(payload).hexdigest(), len(payload))
    supplied = {item["path"]: (item["sha256"], item["bytes"]) for item in inventory["files"]}
    if len(supplied) != len(inventory["files"]) or set(supplied) != set(expected) | anchors:
        raise AssetCorpusError("inventory_membership")
    if any(supplied[path] != identity for path, identity in expected.items()):
        raise AssetCorpusError("unsupported_generator")
    if any(supplied[path] not in _GENERATOR_ANCHORS[path] for path in anchors):
        raise AssetCorpusError("unsupported_generator")


@functools.cache
def _dependencies_digest() -> str:
    """The installed distributions this process imports from, scanned once."""
    return canonical_digest(
        sorted(
            (d.metadata["Name"].lower(), d.version)
            for d in importlib.metadata.distributions()
            if d.metadata.get("Name")
        )
    )


def collect_benchmark_environment(*, production: "LauncherConfig | None" = None) -> dict:
    facts = machine_manifest()
    # The standard collector cannot establish GPU absence on every platform.
    # Leave that unknown rather than inventing a performance match.
    gpu = facts["gpu"]
    if gpu is not None:
        gpu = sorted(
            {
                (g.get("model"), None, None if g.get("cores") is None else str(g["cores"]))
                for g in gpu
            },
            key=lambda item: tuple((part is not None, part or "") for part in item),
        )
        gpu = [dict(zip(("model", "device", "cores"), item, strict=True)) for item in gpu]
    value = dict(
        os=facts["os"],
        os_version=facts["os_version"],
        architecture=facts["machine"],
        cpu=facts["cpu"],
        ram_bytes=facts["ram_bytes"],
        gpu=gpu,
        python=facts["python"],
        dependencies_digest=_dependencies_digest(),
        runtime=None,
        runtime_version=None,
        runtime_config_digest=None,
    )
    if production is not None and production.recommendations.primary == "hybrid":
        semantic = production.asset_semantics
        if semantic is None or semantic.hybrid is None:
            raise AssetCorpusError("semantic_configuration")
        manifest = semantic.hybrid.manifest
        value.update(
            runtime=manifest.data["runtime"]["backend"],
            runtime_version=manifest.data["runtime"]["version"],
            runtime_config_digest=manifest.manifest_digest,
        )
    return validate_definition("Environment", value)


def _semantic_projection(semantics) -> tuple[list, list, dict]:
    references, artifacts, documents = {}, {}, {}

    def reference(role, value):
        raw = copy.deepcopy(value)
        id = "referenced_config:" + canonical_digest(raw)
        documents[id] = raw
        references[(role, id)] = dict(role=role, document_id=id)

    def artifact(role, identity, digest):
        value = dict(role=role, **dict(identity), digest=digest, provenance="observed")
        artifacts[(role, value["name"], value["version"], digest)] = value

    if semantics is not None:
        for name in ("hybrid", "adaptation"):
            config = getattr(semantics, name)
            if config is None:
                continue
            manifest = config.manifest
            reference("embedding", manifest.data)
            reference("index_config" if name == "hybrid" else "adaptation", config.data)
            artifact(
                "embedding" if name == "hybrid" else "adaptation",
                manifest.data["model"],
                manifest.artifact_hash,
            )
            if name == "hybrid":
                artifact("index_config", config.data["identity"], config.digest)
    return (
        [references[key] for key in sorted(references)],
        [artifacts[key] for key in sorted(artifacts)],
        documents,
    )


def semantic_config_documents(production: "LauncherConfig") -> dict:
    """Exact validated semantic preimages; no launcher, credentials or downloads."""
    return _semantic_projection(production.asset_semantics)[2]


def effective_asset_config(
    production: "LauncherConfig",
    release: "VerifiedAssetRelease",
    *,
    runner_digest: str,
    protocol_digest: str,
    requested_kinds: Sequence[str] = ("mcp_tool", "skill"),
) -> dict:
    """Project actual production parser defaults; no credentials/path hashes."""
    settings = production.recommendations
    if not settings.enabled or not settings.user_id or not settings.workspace_id:
        raise AssetCorpusError("recommendations_disabled")
    if production.asset_semantics is not None and production.asset_semantics.adaptation_enabled:
        raise AssetCorpusError("adaptation_unavailable")
    references, artifacts, _ = _semantic_projection(production.asset_semantics)
    source_ids = {s.source_id for s in production.assets.sources}
    sources = [
        dict(
            source_id=s.source_id,
            kind=s.kind,
            locator_id="source-" + str(i),
            adapter_contract=s.adapter_contract,
            proof_evidence_ids=["benchmark-policy"],
        )
        for i, s in enumerate(sorted(production.assets.sources, key=lambda s: s.source_id))
    ]
    eligibility = []
    for asset in release.catalog["assets"]:
        record = asset["record"]
        eligibility.append(
            dict(
                asset_id=record["asset_id"],
                content_hash=record["content_hash"],
                schema_hash=record["identity"].get("schema_hash"),
                eligible=record["source_id"] in source_ids,
                proof_evidence_ids=["benchmark-policy"],
            )
        )
    value = dict(
        schema_version=1,
        generator=dict(runner_digest=runner_digest, protocol_digest=protocol_digest),
        recommendations=asdict(settings),
        permissions=sorted(production.permissions),
        selection_scope="discovery",
        requested_kinds=sorted(set(requested_kinds)),
        budgets=recommendation_budgets(settings.total_ms, settings.primary_ms, settings.fallback),
        job_budgets=acquisition_budgets(),
        sources=sources,
        refresh=asdict(production.assets.refresh),
        eligibility=sorted(eligibility, key=lambda e: e["asset_id"]),
        references=references,
        adaptation=dict(enabled=False, config_digest=None),
        artifacts=artifacts,
    )
    return validate_definition("EffectiveConfigV1", value)


def effective_selector_identity(effective: Mapping) -> dict:
    """Selector identity from existing verified effective/artifact slots."""
    settings = effective["recommendations"]
    if settings["primary"] == "lexical":
        return dict(name="lexical", version="1", transport="in_process")
    if settings["primary"] == "hybrid":
        identities = [item for item in effective["artifacts"] if item["role"] == "index_config"]
        if len(identities) != 1 or identities[0]["provenance"] != "observed":
            raise AssetCorpusError("selector_binding")
        return dict(
            name=identities[0]["name"], version=identities[0]["version"], transport="in_process"
        )
    return dict(name=settings["name"], version=settings["version"], transport="http")


def source_proof(production: "LauncherConfig", effective: Mapping) -> dict:
    actual = {s.source_id: s for s in production.assets.sources}
    value = dict(
        schema_version=1,
        effective_config_digest=canonical_digest(effective),
        bindings=[
            dict(
                source_id=s["source_id"],
                locator_id=s["locator_id"],
                locator=str(actual[s["source_id"]].path.absolute()),
            )
            for s in effective["sources"]
        ],
        policy_evidence_ids=["benchmark-policy"],
        reviewer_ids=[],
    )
    return validate_definition("SourceProofV1", value)


def compatibility_policy(effective: Mapping) -> dict:
    keys = ("enabled", "user_id", "workspace_id", "total_ms", "primary_ms", "fallback")
    value = {
        key: effective[key]
        for key in (
            "permissions",
            "selection_scope",
            "requested_kinds",
            "budgets",
            "job_budgets",
            "refresh",
            "adaptation",
        )
    }
    value["recommendations"] = {key: effective["recommendations"][key] for key in keys}
    for key in ("sources", "eligibility"):
        value[key] = [
            {k: v for k, v in item.items() if k != "proof_evidence_ids"} for item in effective[key]
        ]
    return value


@dataclass(frozen=True)
class SourcePolicyReview:
    """Trusted custodian assertion after actual review; never parsed from JSON.

    The scope includes exact effective settings and original source-proof bytes.
    A caller must perform that review before constructing this Python object.
    """

    effective_config_digest: str
    proof_sha256: str
    custodian_id: str
    review_record_evidence_ids: tuple[str, ...]


def verify_source_policy_review(
    effective: Mapping,
    proof: Mapping,
    manifest: Mapping,
    evidence: Mapping,
    review: SourcePolicyReview,
) -> None:
    proof_entries = [
        e
        for e in manifest["evidence"]
        if e["id"] in manifest["source_proof_evidence_ids"] and e["sha256"] == review.proof_sha256
    ]
    if (
        review.effective_config_digest != canonical_digest(effective)
        or len(proof_entries) != 1
        or hashlib.sha256(evidence[proof_entries[0]["id"]]).hexdigest() != review.proof_sha256
        or review.custodian_id not in proof["reviewer_ids"]
        or not review.review_record_evidence_ids
        or any(id not in evidence for id in review.review_record_evidence_ids)
    ):
        raise AssetCorpusError("evidence_unverified")


@dataclass(frozen=True)
class VerifiedBenchmarkInputs:
    manifest: Mapping
    documents: Mapping
    evidence: Mapping
    release: object
    effective: Mapping
    source_proof: Mapping


def validate_effective_integrity(effective: Mapping) -> None:
    """Enforce parser defaults and canonical identities beyond structural types."""
    validate_definition("EffectiveConfigV1", effective)
    for key, identity in (
        ("sources", lambda item: item["source_id"]),
        ("eligibility", lambda item: item["asset_id"]),
        ("references", lambda item: (item["role"], item["document_id"])),
        (
            "artifacts",
            lambda item: (item["role"], item["name"], item["version"], item["digest"] or ""),
        ),
    ):
        keys = [identity(item) for item in effective[key]]
        if keys != sorted(set(keys)):
            raise AssetCorpusError("effective_order")
    for key in ("permissions", "requested_kinds"):
        if effective[key] != sorted(set(effective[key])):
            raise AssetCorpusError("effective_order")
    for pool in (effective["sources"], effective["eligibility"]):
        for item in pool:
            if item["proof_evidence_ids"] != sorted(set(item["proof_evidence_ids"])):
                raise AssetCorpusError("effective_order")
    if any(
        a["provenance"] == "unknown" and a["digest"] is not None for a in effective["artifacts"]
    ):
        raise AssetCorpusError("artifact_provenance")
    settings = effective["recommendations"]
    from relaymind.assets.settings import parse_recommendation_settings

    if effective["adaptation"]["enabled"] or effective["adaptation"]["config_digest"] is not None:
        raise AssetCorpusError("adaptation_unavailable")
    reference = {}
    for item in effective["references"]:
        reference.setdefault(item["role"], item["document_id"])
    hybrid = (
        dict(manifest=reference["embedding"], config=reference["index_config"])
        if "embedding" in reference and "index_config" in reference
        else None
    )
    parse_recommendation_settings(settings, source_count=len(effective["sources"]), hybrid=hybrid)
    if (
        effective["refresh"]["backoff_max_seconds"]
        < effective["refresh"]["backoff_initial_seconds"]
    ):
        raise AssetCorpusError("effective_defaults")


def verify_asset_configuration_closure(
    documents: Mapping, manifest: Mapping, effective: Mapping, environment: Mapping
) -> None:
    """Resolve every actual config preimage; unknown schemas grant no activation."""
    supplied = {
        reference["id"]
        for reference in manifest["documents"]
        if reference["kind"] == "referenced_config"
    }
    required = {reference["document_id"] for reference in effective["references"]}
    if environment["runtime_config_digest"] is not None:
        required.add("referenced_config:" + environment["runtime_config_digest"])
    if not required.issubset(supplied) or any(id not in documents for id in required):
        raise AssetCorpusError("input_unresolved")
    if supplied != required:
        raise AssetCorpusError("configuration_closure")
    if not required:
        if effective["recommendations"]["primary"] == "hybrid":
            raise AssetCorpusError("input_unresolved")
        if any(
            environment[key] is not None
            for key in ("runtime", "runtime_version", "runtime_config_digest")
        ):
            raise AssetCorpusError("runtime_not_configured")
        return
    from types import SimpleNamespace

    from relaymind.assets.embedding_config import (
        validate_adaptation_config,
        validate_hybrid_config,
    )
    from relaymind.assets.embedding_manifest import validate_embedding_manifest

    refs = {}
    for item in effective["references"]:
        refs.setdefault(item["role"], []).append(item["document_id"])
    if set(refs) - {"embedding", "index_config", "adaptation"}:
        raise AssetCorpusError("unsupported_configuration")
    if len(refs.get("index_config", [])) > 1 or len(refs.get("adaptation", [])) > 1:
        raise AssetCorpusError("configuration_closure")
    try:
        for item in manifest["documents"]:
            if item["id"] not in required:
                continue
            if item["bytes"] > 65536:
                raise AssetCorpusError("configuration_limit")
            if item["id"] != "referenced_config:" + canonical_digest(documents[item["id"]]):
                raise AssetCorpusError("configuration_binding")
        manifests = {}
        for id in refs.get("embedding", []):
            parsed = validate_embedding_manifest(documents[id])
            if id != "referenced_config:" + parsed.manifest_digest:
                raise AssetCorpusError("configuration_binding")
            manifests[parsed.manifest_digest] = parsed
        configs = {}
        used = set()
        for role, name, validate in (
            ("index_config", "hybrid", validate_hybrid_config),
            ("adaptation", "adaptation", validate_adaptation_config),
        ):
            ids = refs.get(role, [])
            if not ids:
                configs[name] = None
                continue
            raw = documents[ids[0]]
            digest = raw["manifest_digest"]
            if digest not in manifests:
                raise AssetCorpusError("input_unresolved")
            configs[name] = validate(raw, manifests[digest])
            used.add(digest)
        if used != set(manifests):
            raise AssetCorpusError("configuration_closure")
        semantics = SimpleNamespace(**configs)
        expected_refs, expected_artifacts, _ = _semantic_projection(semantics)
        if effective["references"] != expected_refs or effective["artifacts"] != expected_artifacts:
            raise AssetCorpusError("configuration_binding")
        if effective["recommendations"]["primary"] == "hybrid":
            if semantics.hybrid is None:
                raise AssetCorpusError("input_unresolved")
            parsed = semantics.hybrid.manifest
            if (
                environment["runtime"] != parsed.data["runtime"]["backend"]
                or environment["runtime_version"] != parsed.data["runtime"]["version"]
                or environment["runtime_config_digest"] != parsed.manifest_digest
            ):
                raise AssetCorpusError("runtime_configuration")
        elif any(
            environment[key] is not None
            for key in ("runtime", "runtime_version", "runtime_config_digest")
        ):
            raise AssetCorpusError("runtime_not_configured")
    except AssetCorpusError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise AssetCorpusError("semantic_configuration") from None


def source_locator_is_absolute(locator: str) -> bool:
    """Recognize opaque ordinary-root locators without opening them on this OS."""
    return PurePosixPath(locator).is_absolute() or PureWindowsPath(locator).is_absolute()


def verify_benchmark_inputs(
    report: Mapping,
    manifest_path: Path,
    *,
    resolver: OfflineResolver | None = None,
    source_policy_review: SourcePolicyReview | None = None,
) -> VerifiedBenchmarkInputs:
    manifest_path = Path(manifest_path).absolute()
    manifest, payload = read_json_path(manifest_path)
    reference = report["input_manifest"]
    if canonical_digest(manifest) != reference["digest"] or len(payload) != reference["bytes"]:
        raise AssetCorpusError("input_manifest_digest")
    validate_definition("InputManifestV1", manifest)
    documents, raw_documents, evidence = {}, {}, {}
    for key, pool in [("documents", manifest["documents"]), ("evidence", manifest["evidence"])]:
        ids = [item["id"] for item in pool]
        if ids != sorted(set(ids)):
            raise AssetCorpusError("manifest_duplicate_or_order")
        for item in pool:
            raw = resolve_reference(
                item, manifest_path.parent, resolver, evidence=key == "evidence"
            )
            if key == "documents":
                expected = (
                    item["kind"]
                    if item["kind"]
                    in (
                        "benchmark_config",
                        "effective_config",
                        "environment_lock",
                        "runner_inventory",
                        "protocol_inventory",
                    )
                    else item["kind"] + ":" + item["digest"]
                )
                if item["id"] != expected:
                    raise AssetCorpusError("document_identity")
                documents[item["id"]] = semantic_document(
                    raw, item["format"], semantic=item["kind"] == "referenced_config"
                )
                raw_documents[item["id"]] = raw
            else:
                evidence[item["id"]] = raw
    for key in (
        "benchmark_config",
        "effective_config",
        "environment_lock",
        "runner_inventory",
        "protocol_inventory",
    ):
        if key not in documents or sum(item["kind"] == key for item in manifest["documents"]) != 1:
            raise AssetCorpusError("manifest_cardinality")
    config = validate_benchmark_document("config", documents["benchmark_config"])
    effective = validate_definition("EffectiveConfigV1", documents["effective_config"])
    validate_effective_integrity(effective)
    environment = validate_definition("Environment", documents["environment_lock"])
    verify_asset_configuration_closure(documents, manifest, effective, environment)
    if any(
        config[key] != report["config"][key]
        for key in ("split", "track", "seed", "repetitions", "warmups")
    ):
        raise AssetCorpusError("config_binding")
    if (
        config["lock"] != "lock.json"
        or config["production_config"] != "production.toml"
        or config["environment_lock"] != "environment.json"
    ):
        raise AssetCorpusError("config_locator")
    if (
        config["production_config_digest"] != canonical_digest(effective)
        or report["config"]["production_config_digest"] != canonical_digest(effective)
        or config["environment_lock_digest"] != canonical_digest(environment)
        or any(v is not None and v != report["environment"][k] for k, v in environment.items())
        or config["lock_digest"] != report["lock_digest"]
    ):
        raise AssetCorpusError("config_binding")
    normal = report["config"]
    settings = effective["recommendations"]
    if (
        any(
            normal[k] != effective[k]
            for k in ("permissions", "selection_scope", "requested_kinds", "budgets")
        )
        or normal["scope"] != {k: settings[k] for k in ("user_id", "workspace_id")}
        or normal["fallback"] != settings["fallback"]
        or normal["fallback"] != effective["budgets"]["fallback_enabled"]
        or normal["adaptation_enabled"] != effective["adaptation"]["enabled"]
        or normal["adaptation_config_digest"] != effective["adaptation"]["config_digest"]
    ):
        raise AssetCorpusError("effective_binding")
    if (
        not settings["enabled"]
        or effective["budgets"]
        != recommendation_budgets(
            settings["total_ms"], settings["primary_ms"], settings["fallback"]
        )
        or effective["job_budgets"] != acquisition_budgets()
    ):
        raise AssetCorpusError("effective_defaults")
    if normal["track"] == "primary_only" and (normal["fallback"] or normal["adaptation_enabled"]):
        raise AssetCorpusError("primary_policy")
    expected_selector = effective_selector_identity(effective)
    if normal["selector"] != expected_selector or report["artifacts"] != effective["artifacts"]:
        raise AssetCorpusError("selector_binding")
    if settings["url"] is not None:
        from relaymind.selection.http import validate_url

        validate_url(settings["url"])
    for kind, key in [
        ("runner_inventory", "runner_digest"),
        ("protocol_inventory", "protocol_digest"),
    ]:
        inventory = validate_definition("FileInventoryV1", documents[kind])
        verify_compiled_asset_inventory(kind, inventory)
        paths = [f["path"] for f in inventory["files"]]
        if paths != sorted(set(paths)):
            raise AssetCorpusError("inventory_order")
        mandatory = (
            {"docs/SPECIFICATION.md"}
            if kind == "protocol_inventory"
            else {"pyproject.toml", "scripts/asset_selection_benchmark.py"}
        )
        suffix = ".json" if kind == "protocol_inventory" else ".py"
        if not mandatory.issubset(paths) or any(
            path not in mandatory
            and not (path.startswith("src/relaymind/") and path.endswith(suffix))
            for path in paths
        ):
            raise AssetCorpusError("inventory_membership")
        for item in inventory["files"]:
            if resolver is None:
                raise AssetCorpusError("input_unresolved")
            resolver.read(
                file_evidence_id(item["path"]),
                "original",
                item["sha256"],
                item["bytes"],
                limit=CONFIG_BYTES,
            )
        digest = canonical_digest(inventory)
        if report["implementation"][key] != digest or effective["generator"][key] != digest:
            raise AssetCorpusError("inventory_binding")
    proof_ids = manifest["source_proof_evidence_ids"]
    if not proof_ids or any(i not in evidence for i in proof_ids):
        raise AssetCorpusError("evidence_unverified")
    proofs = []
    for id in proof_ids:
        if len(evidence[id]) > CONFIG_BYTES:
            raise AssetCorpusError("size_limit")
        try:
            candidate = parse_json(evidence[id])
        except ValueError:
            continue
        if "bindings" in candidate:
            proofs.append(validate_definition("SourceProofV1", candidate))
    if len(proofs) != 1:
        raise AssetCorpusError("evidence_unverified")
    proof = proofs[0]
    source_proof_entries = [e for e in manifest["evidence"] if e["id"] in proof_ids]
    if any(
        e["kind"] != "source_provenance" or e["access"] != "restricted" or e["path"] is not None
        for e in source_proof_entries
    ):
        raise AssetCorpusError("evidence_unverified")
    # Policy evidence is original-byte evidence, not a new unversioned config
    # dialect. Its authenticity/default/source binding requires actual custodian
    # review for genuine inputs; a successful hash check cannot supply that.
    if (
        proof["effective_config_digest"] != canonical_digest(effective)
        or [(b["source_id"], b["locator_id"]) for b in proof["bindings"]]
        != [(s["source_id"], s["locator_id"]) for s in effective["sources"]]
        or any(not source_locator_is_absolute(b["locator"]) for b in proof["bindings"])
        or not proof["policy_evidence_ids"]
        or any(i not in proof_ids or i not in evidence for i in proof["policy_evidence_ids"])
    ):
        raise AssetCorpusError("evidence_unverified")
    if any(
        i not in evidence
        for pool in (effective["sources"], effective["eligibility"])
        for entry in pool
        for i in entry["proof_evidence_ids"]
    ):
        raise AssetCorpusError("evidence_unverified")
    # Materialize only in an authorized private temporary directory and run the
    # existing corpus parser unchanged. No exported/private-label parser fork.
    with tempfile.TemporaryDirectory(prefix="asset-compare-") as temporary:
        temporary_root = Path(temporary)
        root = temporary_root.parent.resolve(strict=True) / temporary_root.name
        if root.is_symlink():
            raise AssetCorpusError("unsafe_file")
        related_roots = {}
        locks = {doc["digest"]: doc for key, doc in documents.items() if key.startswith("lock:")}
        lock_raw = {
            doc["digest"]: raw_documents[key]
            for key, doc in documents.items()
            if key.startswith("lock:")
        }
        if report["lock_digest"] not in locks:
            raise AssetCorpusError("input_unresolved")

        def put(path, raw):
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.read_bytes() != raw:
                raise AssetCorpusError("conflicting_document")
            path.write_bytes(raw)

        locations = {digest: {"lock.json"} for digest in locks}
        for lock in locks.values():
            for reference in lock["related_locks"]:
                if reference["digest"] not in locks:
                    raise AssetCorpusError("input_unresolved")
                child = root / reference["digest"]
                if reference["id"] in related_roots and related_roots[reference["id"]] != child:
                    raise AssetCorpusError("conflicting_document")
                related_roots[reference["id"]] = child
                relative_path(reference["path"])
                locations[reference["digest"]].add(reference["path"])
        for digest, lock in locks.items():
            for relative in sorted(locations[digest]):
                location = root / digest / relative
                put(location, lock_raw[digest])
                for kind in ("catalog", "cases", "labels"):
                    reference = lock[kind]
                    relative_path(reference["path"])
                    id = kind + ":" + reference["digest"]
                    if id not in raw_documents:
                        raise AssetCorpusError("input_unresolved")
                    put(location.parent / reference["path"], raw_documents[id])
        evidence_root = root / "evidence"
        evidence_root.mkdir()
        paths = {}
        for item in manifest["evidence"]:
            private_path = evidence_root / (
                "by-id/" + hashlib.sha256(item["id"].encode()).hexdigest()
            )
            put(private_path, evidence[item["id"]])
            paths[item["id"]] = private_path
            if item["path"] is not None:
                relative_path(item["path"])
                put(evidence_root / item["path"], evidence[item["id"]])

        class Resolver:
            def resolve(self, id):
                return paths.get(id)

        release = load_asset_release(
            root / report["lock_digest"] / "lock.json",
            evidence_root=evidence_root,
            related_roots=related_roots,
            evidence_resolver=Resolver(),
        )
    if source_policy_review is not None:
        verify_source_policy_review(effective, proof, manifest, evidence, source_policy_review)
    elif release.lock["purpose"] != "fixture":
        # Opaque reviewer IDs and an operator JSON boolean cannot create review.
        raise AssetCorpusError("evidence_unverified")
    expected_eligibility = {
        a["record"]["asset_id"]: (
            a["record"]["content_hash"],
            a["record"]["identity"].get("schema_hash"),
        )
        for a in release.catalog["assets"]
    }
    if {
        e["asset_id"]: (e["content_hash"], e["schema_hash"]) for e in effective["eligibility"]
    } != expected_eligibility or len(effective["eligibility"]) != len(expected_eligibility):
        raise AssetCorpusError("eligibility_binding")
    return VerifiedBenchmarkInputs(manifest, documents, evidence, release, effective, proof)
