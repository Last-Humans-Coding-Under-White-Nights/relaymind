"""Bounded, offline four-document release verification (RM40/AC31, spec 12.6.1).

Structural integrity is independent of externally reviewed qualification. No
input grants host authority, performs acquisition or supplies human authenticity.
"""

import hashlib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from relaymind.assets.catalog import validate_asset_record
from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_corpus_io import (
    ORIGINAL_BYTES,
    AssetCorpusError,
    EvidenceResolver,
    document,
    read_local,
    relative_path,
)
from relaymind.evaluation.asset_judgments import (
    check_object_order,
    check_references,
    id_map,
    validate_judgments,
)
from relaymind.evaluation.asset_splits import hard_subsets, validate_splits
from relaymind.schema_util import freeze_json


@dataclass(frozen=True)
class EvidenceFinding:
    evidence_id: str
    sha256: str
    bytes: int
    status: str


@dataclass(frozen=True)
class LockedAssetDocuments:
    catalog: Mapping[str, Any]
    cases: Mapping[str, Any]
    labels: Mapping[str, Any]
    lock: Mapping[str, Any]


@dataclass(frozen=True)
class VerifiedAssetRelease(LockedAssetDocuments):
    evidence_findings: tuple[EvidenceFinding, ...]
    agreement: Mapping[str, Any]
    split_diagnostics: Mapping[str, Any]
    hard_subsets: Mapping[str, Any]
    related_releases: tuple[LockedAssetDocuments, ...]


def _resolved_bytes(path: Path, roots: tuple[Path, ...]) -> bytes:
    # Resolve no symlinks here: the shared source reader must see and refuse them.
    absolute = Path(path).absolute()
    for root in roots:
        try:
            relative = absolute.relative_to(root.absolute()).as_posix()
        except ValueError:
            continue
        return read_local(root, relative, ORIGINAL_BYTES)
    raise AssetCorpusError("resolver_outside_roots")


def _evidence_references(value: Any):
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key.endswith("evidence_ids"):
                yield from item
            elif key.endswith("evidence_id") and item is not None:
                yield item
            elif key not in ("record", "evidence"):
                yield from _evidence_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _evidence_references(item)


def _catalog_checks(catalog: dict) -> None:
    check_object_order(catalog["equivalent_pairs"], ("zh_asset_id", "en_asset_id"))
    check_object_order(catalog["exclusions"], ("source_id", "original_id", "reason"))
    ids, identities, aliases, locators = set(), set(), set(), set()
    ordered = [entry["record"]["asset_id"] for entry in catalog["assets"]]
    if ordered != sorted(ordered):
        raise AssetCorpusError("unsorted_set")
    for entry in catalog["assets"]:
        record = validate_asset_record(entry["record"])
        if record.asset_id in ids:
            raise AssetCorpusError("duplicate_asset")
        ids.add(record.asset_id)
        identity = record.identity
        try:
            url = urlsplit(entry["source_url"])
        except ValueError:
            raise AssetCorpusError("invalid_source_url") from None
        if url.username is not None or url.password is not None:
            raise AssetCorpusError("source_credentials")
        locator_key = (record.kind, record.source_id, record.source_locator)
        # Multiple MCP tools can share a source capture URL; the namespace is
        # the identity there. A skill locator identifies one original package.
        if record.kind == "skill" and locator_key in locators:
            raise AssetCorpusError("duplicate_source_identity")
        locators.add(locator_key)
        if record.kind == "skill":
            source_key = (record.kind, record.source_id, identity["package_id"])
            alias = (record.kind, record.content_hash)
        else:
            source_key = (record.kind, identity["server_id"], identity["tool_name"])
            # One genuine original may contain many tools. Only a repeated tool
            # on the same server is an alias; different real servers stay distinct.
            alias = source_key
        if source_key in identities or alias in aliases:
            raise AssetCorpusError("duplicate_source_identity")
        identities.add(source_key)
        aliases.add(alias)
        if record.source_version != entry["source_revision"]:
            raise AssetCorpusError("source_revision_mismatch")
        if entry["origin"] in ("matrix_original_zh", "original_en") and record.kind != "skill":
            raise AssetCorpusError("origin_kind_mismatch")
        if entry["origin"] == "genuine_mcp" and record.kind != "mcp_tool":
            raise AssetCorpusError("origin_kind_mismatch")
    people = id_map(catalog["people"])
    pair_ids = set()
    by_asset = {a["record"]["asset_id"]: a for a in catalog["assets"]}
    for pair in catalog["equivalent_pairs"]:
        key = pair["zh_asset_id"], pair["en_asset_id"]
        if key in pair_ids or key[0] == key[1]:
            raise AssetCorpusError("duplicate_pair")
        pair_ids.add(key)
        check_references(key, by_asset)
        check_references(pair["reviewer_ids"], people)
        if any("reviewer" not in people[p]["roles"] for p in pair["reviewer_ids"]):
            raise AssetCorpusError("role_conflict")
        if not any(t.lower().startswith("zh") for t in by_asset[key[0]]["original_languages"]):
            raise AssetCorpusError("pair_language_mismatch")
        if "en" not in by_asset[key[1]]["original_languages"]:
            raise AssetCorpusError("pair_language_mismatch")


def load_asset_release(
    lock_path: Path,
    *,
    evidence_root: Path,
    related_roots: Mapping[str, Path],
    evidence_resolver: EvidenceResolver | None = None,
) -> VerifiedAssetRelease:
    """Verify canonical documents, exact-byte originals and transitive closure.

    related_roots maps each declared alias ID to an explicit allowed root.
    Each related path selects its lock below that root; document refs resolve
    relative to their owning lock, with the complete walk fenced at the root.
    No alternate-base search or digest-based path fallback is performed.
    Evidence paths use evidence_root; resolvers may also use declared roots.
    """
    nodes, visiting, visited, release_digests = [], set(), {}, {}

    def read_node(root: Path, relative: str, expected: str | None) -> tuple[dict, dict, dict, dict]:
        relative_path(relative)
        lock = document(root, relative, "lock")
        digest = canonical_digest({key: value for key, value in lock.items() if key != "digest"})
        if lock["digest"] != digest or (expected is not None and digest != expected):
            raise AssetCorpusError("lock_digest")
        if lock["unicode_version"] != unicodedata.unidata_version:
            raise AssetCorpusError("unicode_version_mismatch")
        if (lock["state"] == "frozen") != (lock["frozen_at"] is not None):
            raise AssetCorpusError("freeze_state_mismatch")
        docs = []
        for name, kind in [("catalog", "catalog_set"), ("cases", "cases"), ("labels", "labels")]:
            relative_path(lock[name]["path"])
            linked_path = (Path(relative).parent / lock[name]["path"]).as_posix()
            raw = document(root, linked_path, kind)
            if canonical_digest(raw) != lock[name]["digest"]:
                raise AssetCorpusError("document_digest")
            if (raw["dataset_id"], raw["release"]) != (lock["dataset_id"], lock["release"]):
                raise AssetCorpusError("release_mismatch")
            docs.append(raw)
        catalog, cases, labels = docs
        if catalog["purpose"] != lock["purpose"]:
            raise AssetCorpusError("purpose_mismatch")
        if labels["cases_digest"] != lock["cases"]["digest"]:
            raise AssetCorpusError("cases_digest")
        _catalog_checks(catalog)
        id_map(lock["related_locks"])
        return catalog, cases, labels, lock

    # Enter/leave frames keep arbitrarily deep valid closures off Python's
    # call stack. Each distinct digest is parsed and checked once.
    main = Path(lock_path)
    stack = [(main.parent, main.name, None, None)]
    while stack:
        root, relative, expected, completed = stack.pop()
        if completed is not None:
            digest = completed[3]["digest"]
            visiting.remove(digest)
            visited[digest] = completed
            nodes.append(completed)
            continue
        node = read_node(root, relative, expected)
        item_lock = node[3]
        digest = item_lock["digest"]
        release_key = (item_lock["dataset_id"], item_lock["release"])
        if release_key in release_digests and release_digests[release_key] != digest:
            raise AssetCorpusError("conflicting_release")
        release_digests[release_key] = digest
        if digest in visiting:
            raise AssetCorpusError("lock_cycle")
        if digest in visited:
            continue
        if len(nodes) + len(visiting) >= 10000:
            raise AssetCorpusError("closure_limit")
        visiting.add(digest)
        stack.append((root, relative, expected, node))
        for related in reversed(item_lock["related_locks"]):
            if related["id"] not in related_roots:
                raise AssetCorpusError("missing_related_root")
            relative_path(related["path"])
            stack.append(
                (Path(related_roots[related["id"]]), related["path"], related["digest"], None)
            )
    catalog, cases, labels, lock = nodes[-1]
    declared, locked, committed_evidence = {}, {}, {}
    for cat, _, _, item_lock in nodes:
        local = id_map(cat["evidence"])
        local_locks = id_map(item_lock["evidence"])
        if set(local) != set(local_locks):
            raise AssetCorpusError("evidence_closure")
        # Nodes are in descendant-first order. Each release can rely only on
        # evidence it commits locally or through its own directed related links.
        reachable = set(local)
        for related in item_lock["related_locks"]:
            reachable.update(committed_evidence[related["digest"]])
        committed_evidence[item_lock["digest"]] = reachable
        for evidence_id, item in local.items():
            relative_path(item["path"]) if item["path"] is not None else None
            if item["access"] == "redistributable" and item["path"] is None:
                raise AssetCorpusError("missing_public_path")
            if any(item[key] != local_locks[evidence_id][key] for key in ("sha256", "bytes")):
                raise AssetCorpusError("evidence_lock_mismatch")
            if evidence_id in locked and locked[evidence_id] != local_locks[evidence_id]:
                raise AssetCorpusError("conflicting_evidence")
            if evidence_id in declared and declared[evidence_id] != item:
                raise AssetCorpusError("conflicting_evidence")
            locked[evidence_id], declared[evidence_id] = local_locks[evidence_id], item
    findings = []
    roots = (Path(evidence_root), *[Path(p) for p in related_roots.values()])
    for evidence_id, item in sorted(declared.items()):
        if item["bytes"] > ORIGINAL_BYTES:
            raise AssetCorpusError("evidence_size_limit")
        payload = None
        if item["path"] is not None:
            try:
                payload = read_local(evidence_root, item["path"], ORIGINAL_BYTES)
            except AssetCorpusError as exc:
                if item["access"] != "restricted" or str(exc) != "missing_file":
                    raise
        if payload is None and evidence_resolver is not None:
            resolved = evidence_resolver.resolve(evidence_id)
            if resolved is not None:
                try:
                    payload = _resolved_bytes(resolved, roots)
                except AssetCorpusError as exc:
                    if str(exc) != "missing_file":
                        raise
        if payload is None:
            findings.append(
                EvidenceFinding(evidence_id, item["sha256"], item["bytes"], "unavailable")
            )
        else:
            if (
                len(payload) != item["bytes"]
                or hashlib.sha256(payload).hexdigest() != item["sha256"]
            ):
                raise AssetCorpusError("evidence_digest")
            findings.append(EvidenceFinding(evidence_id, item["sha256"], item["bytes"], "verified"))
    actual_agreement = None
    for cat, cs, ls, lk in nodes:
        for doc in (cat, cs, ls, lk):
            check_references(_evidence_references(doc), committed_evidence[lk["digest"]])
        for asset in cat["assets"]:
            original = declared[asset["original_evidence_id"]]
            if (
                original["kind"] != "original"
                or asset["record"]["content_hash"] != original["sha256"]
            ):
                raise AssetCorpusError("original_hash_mismatch")
        computed = validate_judgments(cat, cs, ls)
        for case in cs["cases"]:
            if sum(len(m["text"].encode("utf-8")) for m in case["conversation"]) > 32768:
                raise AssetCorpusError("query_size_limit")
        if lk is lock:
            actual_agreement = computed
    split = validate_splits([(cat, cs, ls) for cat, cs, ls, _ in nodes])
    related = tuple(
        LockedAssetDocuments(*(freeze_json(doc) for doc in node))
        for node in nodes
        if node[3] is not lock
    )
    return VerifiedAssetRelease(
        *(freeze_json(doc) for doc in (catalog, cases, labels, lock)),
        tuple(findings),
        freeze_json(actual_agreement),
        freeze_json(split),
        freeze_json(hard_subsets(catalog, cases, labels)),
        related,
    )


# Qualification has its own trusted input boundary, independent from I/O.
from relaymind.evaluation.asset_qualification import (  # noqa: E402
    CustodianReview,
    QualificationAssessment,
    assess_acceptance_release,
    assess_asset_release,
)

__all__ = [
    "AssetCorpusError",
    "CustodianReview",
    "EvidenceResolver",
    "QualificationAssessment",
    "VerifiedAssetRelease",
    "assess_acceptance_release",
    "assess_asset_release",
    "load_asset_release",
]
