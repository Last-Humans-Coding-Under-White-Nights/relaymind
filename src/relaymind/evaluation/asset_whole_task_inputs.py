"""Companion input manifest resolution for the section 12.6.5 exports.

Spec 0.60 section 12.6.5 reuses section 12.6.2's InputManifestV1, OfflineResolverV1
and original-byte rules, extending DocumentRef.kind only here with host_tasks,
host_freeze, episodes and episode_freeze. No cwd/checkout inference, directory
search, network fetch or default reconstruction is performed. A missing
document or preimage is *blocked* (unverifiable, with a fixed safe reason);
a digest, identity or structure contradiction is *rejected*.
"""

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_benchmark_io import CONFIG_BYTES, read_json_path
from relaymind.evaluation.asset_corpus_io import (
    DOCUMENT_BYTES,
    ORIGINAL_BYTES,
    AssetCorpusError,
    read_local,
    read_local_files,
)
from relaymind.evaluation.asset_evidence import (
    _GENERATOR_ANCHORS,
    OfflineResolver,
    semantic_document,
    validate_definition,
)
from relaymind.evaluation.asset_whole_task import (
    WholeTaskBlocked,
    WholeTaskRejected,
    validate_whole_task,
)
from relaymind.evaluation.asset_whole_task_corpus import (
    bind_episodes,
    bind_host_tasks,
    release_labels,
    resolve_corpus,
    verify_episode_configs,
    verify_reference_closure,
)
from relaymind.evaluation.asset_whole_task_host_config import PINNED_VALIDATOR, validate_verdict

SINGLETONS = ("environment_lock", "runner_inventory", "protocol_inventory")
_ROOTS = {
    "host": ("host_tasks", "host_freeze"),
    "adaptation": ("episodes", "episode_freeze"),
}
_SHAPES = {
    "host": ("AssetHostTasksV1", "HostFreezeV1", "corpus_lock_digest", "freeze_digest"),
    "adaptation": ("AssetEpisodesV1", "EpisodeFreezeV1", "dataset_lock_digest", "config_digest"),
}
# What this revision does not verify; always reported next to a grade.
UNVERIFIED = {
    "host": ("configuration_semantics", "qualified_evidence_review"),
    "adaptation": ("adaptation_config_semantics", "qualified_evidence_review"),
}
_UNRESOLVED = {"input_unresolved", "unsafe_file", "missing_file"}


# Reviewed non-package members of the S3 runner inventory. The command script
# is a fixed thin entry point; new bytes need a reviewed update here.
_S3_SCRIPT = "scripts/asset_whole_task.py"
_S3_SCRIPT_ANCHORS = frozenset(
    {("c3cfec3349512e5ff331bc6369850f28de5b243605585495faee47140d50d1be", 198)}
)


def verify_whole_task_inventory(kind: str, inventory: Mapping) -> None:
    """The S3 driver/grader identity: every packaged member plus reviewed anchors.

    Package members must equal the running implementation; anchors must be
    reviewed bytes. An inventory of another implementation is rejected.
    """
    package_root = Path(__file__).absolute().parents[1]
    suffix = "*.py" if kind == "runner_inventory" else "*.json"
    anchors = (
        {"pyproject.toml": _GENERATOR_ANCHORS["pyproject.toml"], _S3_SCRIPT: _S3_SCRIPT_ANCHORS}
        if kind == "runner_inventory"
        else {"docs/SPECIFICATION.md": _GENERATOR_ANCHORS["docs/SPECIFICATION.md"]}
    )
    expected = {}
    relatives = sorted(
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob(suffix)
        if "__pycache__" not in path.parts
    )
    # One held source tree for the whole package (the per-file walk is slow on
    # Windows); every file keeps its own identity and size fences.
    for relative, payload in zip(
        relatives, read_local_files(package_root, relatives, CONFIG_BYTES), strict=True
    ):
        expected["src/relaymind/" + relative] = (hashlib.sha256(payload).hexdigest(), len(payload))
    paths = [item["path"] for item in inventory["files"]]
    if paths != sorted(set(paths)):
        raise WholeTaskRejected("inventory_order")
    supplied = {item["path"]: (item["sha256"], item["bytes"]) for item in inventory["files"]}
    if set(supplied) != set(expected) | set(anchors):
        raise WholeTaskRejected("inventory_membership")
    if any(supplied[path] != identity for path, identity in expected.items()) or any(
        supplied[path] not in allowed for path, allowed in anchors.items()
    ):
        raise WholeTaskRejected("unsupported_generator")


@dataclass(frozen=True)
class WholeTaskInputs:
    manifest: Mapping
    root: Mapping
    freeze: Mapping
    environment: Mapping
    configs: Mapping[str, Mapping]
    registry: Mapping[str, Mapping]
    release: Any = None
    labels: Mapping[str, Mapping] = field(default_factory=dict)
    evidence: Mapping[str, Mapping] = field(default_factory=dict)
    unverified: tuple[str, ...] = ()
    host_config_validation: Mapping[str, Mapping] = field(default_factory=dict)


def _limit(kind: str) -> int:
    if kind in ("host_tasks", "episodes", "host_freeze", "episode_freeze"):
        return DOCUMENT_BYTES
    # Section 12.6.5: configurations, native inventories and manifests keep the
    # 4 MiB limit; the 64 KiB semantic-config parser is not applied to them.
    return CONFIG_BYTES


def _read(item: Mapping, root: Path, resolver: OfflineResolver | None, *, evidence: bool) -> bytes:
    digest = item["sha256"] if evidence else item["digest"]
    hash_kind = "original" if evidence else "canonical"
    limit = ORIGINAL_BYTES if evidence else _limit(item["kind"])
    try:
        if resolver is not None and resolver.contains(item["id"], hash_kind, digest):
            return resolver.read(
                item["id"],
                hash_kind,
                digest,
                item["bytes"],
                format=item.get("format", "json"),
                limit=limit,
            )
        if item["path"] is None:
            raise WholeTaskBlocked("input_unresolved")
        payload = read_local(root, item["path"], limit)
    except AssetCorpusError as exc:
        if str(exc) in _UNRESOLVED or isinstance(exc, FileNotFoundError):
            raise WholeTaskBlocked("input_unresolved") from None
        raise WholeTaskRejected(str(exc)) from None
    except FileNotFoundError:
        raise WholeTaskBlocked("input_unresolved") from None
    if evidence:
        actual = hashlib.sha256(payload).hexdigest()
    else:
        try:
            actual = canonical_digest(semantic_document(payload, item["format"]))
        except AssetCorpusError:
            raise WholeTaskRejected("invalid_document") from None
    if len(payload) != item["bytes"] or actual != digest:
        raise WholeTaskRejected("input_digest")
    return payload


def _config_digests(kind: str, freeze: Mapping) -> set[str]:
    if kind == "host":
        digests = {freeze["native_inventory_digest"]}
        for config in freeze["configurations"]:
            for key in (
                "provider_config_digest",
                "sampling_config_digest",
                "host_config_digest",
                "recommendation_config_digest",
            ):
                if config[key] is not None:
                    digests.add(config[key])
        digests.update(p["budgets_digest"] for p in freeze["task_policies"])
        return digests
    return {
        freeze[k] for k in ("off_config_digest", "on_config_digest", "adaptation_config_digest")
    }


def resolve_whole_task_inputs(
    kind: str,
    report: Mapping,
    manifest_path: Path,
    *,
    resolver: OfflineResolver | None = None,
    host_config_checker: Callable[[Mapping], dict] | None = None,
    host_config_validation: Mapping[str, Mapping] | None = None,
) -> WholeTaskInputs:
    """Resolve and bind every companion document a host/adaptation report needs.

    ``kind`` is ``host`` (host and platform reports) or ``adaptation``.
    Native preflight is explicit: pass ``check_host_config`` as
    ``host_config_checker`` at capture time. Offline callers may instead supply
    captured closed verdicts keyed by the exact configuration digests. Neither
    path authenticates evidence, source extraction or effective defaults, so
    configuration semantics and qualified evidence review remain unverified.
    With neither option, resolution never launches Node or a host.
    """
    root_kind, freeze_kind = _ROOTS[kind]
    manifest_path = Path(manifest_path).absolute()
    try:
        manifest, payload = read_json_path(manifest_path)
    except AssetCorpusError as exc:
        if str(exc) in _UNRESOLVED:
            raise WholeTaskBlocked("input_manifest_unresolved") from None
        raise WholeTaskRejected(str(exc)) from None
    except (FileNotFoundError, ValueError):
        raise WholeTaskBlocked("input_manifest_unresolved") from None
    reference = report["input_manifest"]
    if canonical_digest(manifest) != reference["digest"] or len(payload) != reference["bytes"]:
        raise WholeTaskRejected("input_manifest_digest")
    validate_whole_task("S3InputManifestV1", manifest)
    documents, raw_documents, registry, payloads = {}, {}, {}, {}
    for pool in ("documents", "evidence"):
        ids = [item["id"] for item in manifest[pool]]
        if ids != sorted(set(ids), key=lambda i: i.encode("utf-8")):
            raise WholeTaskRejected("manifest_duplicate_or_order")
    for item in manifest["documents"]:
        expected = (
            item["kind"] if item["kind"] in SINGLETONS else item["kind"] + ":" + item["digest"]
        )
        if (
            item["id"] != expected
            or item["format"] != "json"
            and item["kind"] != "referenced_config"
        ):
            raise WholeTaskRejected("document_identity")
        raw = _read(item, manifest_path.parent, resolver, evidence=False)
        documents[item["id"]] = semantic_document(raw, item["format"])
        raw_documents[item["id"]] = raw
    for item in manifest["evidence"]:
        payload = _read(item, manifest_path.parent, resolver, evidence=True)
        registry[item["id"]] = dict(sha256=item["sha256"], bytes=item["bytes"], path=item["path"])
        payloads[item["id"]] = dict(item, payload=payload)
    for key in SINGLETONS:
        if sum(item["kind"] == key for item in manifest["documents"]) != 1:
            raise WholeTaskBlocked("manifest_cardinality")
    root_id = root_kind + ":" + report["input_digest"]
    if root_id not in documents:
        raise WholeTaskBlocked("root_input_unresolved")
    root_shape, freeze_shape, lock_key, freeze_key = _SHAPES[kind]
    root = validate_whole_task(root_shape, documents[root_id])
    # The frozen corpus and its evidence are verified by the release verifier,
    # and the root input is bound to its cases and catalog.
    release = resolve_corpus(root[lock_key], raw_documents, payloads)
    if kind == "host":
        bind_host_tasks(root, release)
    else:
        bind_episodes(root, release)
    freeze_digest = root[freeze_key]
    freeze_id = freeze_kind + ":" + freeze_digest
    if freeze_id not in documents:
        raise WholeTaskBlocked("freeze_unresolved")
    freeze = validate_whole_task(freeze_shape, documents[freeze_id])
    configs = {}
    for digest in sorted(_config_digests(kind, freeze)):
        key = "referenced_config:" + digest
        if key not in documents:
            raise WholeTaskBlocked("referenced_config_unresolved")
        configs[digest] = documents[key]
    environment = validate_definition("Environment", documents["environment_lock"])
    unverified = list(UNVERIFIED[kind])
    if kind == "adaptation":
        verify_episode_configs(freeze, configs)
        verified = [
            verify_reference_closure(
                configs[freeze[key]],
                documents,
                manifest,
                environment,
                adaptation_digest=freeze["adaptation_config_digest"],
            )
            for key in ("off_config_digest", "on_config_digest")
        ]
        if all(verified):
            unverified.remove("adaptation_config_semantics")
    observed = report["environment"] if "environment" in report else report["platform"]
    if any(value is not None and value != observed[k] for k, value in environment.items()):
        raise WholeTaskRejected("environment_binding")
    for key in ("runner_inventory", "protocol_inventory"):
        inventory = validate_definition("FileInventoryV1", documents[key])
        verify_whole_task_inventory(key, inventory)
        for item in inventory["files"]:
            if resolver is None:
                raise WholeTaskBlocked("inventory_preimage_unresolved")
            file_id = "file:" + hashlib.sha256(item["path"].encode()).hexdigest()
            if not resolver.contains(file_id, "original", item["sha256"]):
                raise WholeTaskBlocked("inventory_preimage_unresolved")
            try:
                resolver.read(
                    file_id, "original", item["sha256"], item["bytes"], limit=CONFIG_BYTES
                )
            except FileNotFoundError:
                raise WholeTaskBlocked("inventory_preimage_unresolved") from None
            except AssetCorpusError as exc:
                if str(exc) in _UNRESOLVED:
                    raise WholeTaskBlocked("inventory_preimage_unresolved") from None
                raise WholeTaskRejected(str(exc)) from None
    validation = {}
    if host_config_checker is not None or host_config_validation is not None:
        if kind != "host" or (
            host_config_checker is not None and host_config_validation is not None
        ):
            raise WholeTaskRejected("host_config_validation_mode")
        if (
            freeze["host"]["name"] != "openclaw"
            or freeze["host"]["version"] != PINNED_VALIDATOR["host_version"]
        ):
            raise WholeTaskBlocked("host_config_validator_unavailable")
        digests = {arm["host_config_digest"] for arm in freeze["configurations"]}
        if host_config_validation is not None and set(host_config_validation) != digests:
            raise WholeTaskRejected("host_config_validation_binding")
        for digest in sorted(digests):
            try:
                verdict = validate_verdict(
                    host_config_checker(configs[digest])
                    if host_config_checker is not None
                    else host_config_validation[digest]
                )
            except (ValueError, TypeError):
                raise WholeTaskRejected("host_config_validation_verdict") from None
            if verdict["status"] == "invalid":
                raise WholeTaskRejected("host_config_invalid")
            if verdict["status"] == "unavailable":
                raise WholeTaskBlocked("host_config_validator_unavailable")
            validation[digest] = verdict
    return WholeTaskInputs(
        manifest,
        root,
        freeze,
        environment,
        configs,
        registry,
        release=release,
        labels=release_labels(release),
        evidence=payloads,
        unverified=tuple(unverified),
        host_config_validation=validation,
    )
