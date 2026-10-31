"""Corpus closure for the section 12.6.5 host-task and episode exports.

Spec 0.60 section 12.6.5 (RM40, RM42, RM44; AC31, AC33, AC36). The frozen corpus
lock and its catalog, cases and labels are resolved from explicit companion
bytes, never from a checkout or directory search. They are materialized only in
a private temporary directory and verified by the unchanged release verifier
(section 12.6.1). Host tasks and adaptation episodes are then bound to the
verified cases and catalog. A missing document blocks; a contradiction rejects.
"""

import hashlib
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_corpus import VerifiedAssetRelease, load_asset_release
from relaymind.evaluation.asset_corpus_io import AssetCorpusError, relative_path
from relaymind.evaluation.asset_whole_task import WholeTaskBlocked, WholeTaskRejected

_KINDS = ("catalog", "cases", "labels")


def _document(documents: Mapping[str, bytes], kind: str, digest: str) -> bytes:
    raw = documents.get(kind + ":" + digest)
    if raw is None:
        raise WholeTaskBlocked("corpus_unresolved")
    try:
        if canonical_digest(json.loads(raw)) != digest:
            raise WholeTaskRejected("corpus_digest")
    except (ValueError, UnicodeError):
        raise WholeTaskRejected("corpus_digest") from None
    return raw


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != payload:
        raise WholeTaskRejected("conflicting_document")
    path.write_bytes(payload)


def resolve_corpus(
    lock_digest: str,
    documents: Mapping[str, bytes],
    evidence: Mapping[str, Mapping],
) -> VerifiedAssetRelease:
    """Verify the frozen corpus from companion bytes.

    ``documents`` maps manifest document IDs (``lock:<digest>``,
    ``catalog:<digest>`` …) to original bytes. ``evidence`` maps evidence IDs to
    their manifest entry plus ``payload`` bytes. Related locks are not
    supported by this revision and block.
    """
    lock_raw = _document(documents, "lock", lock_digest)
    lock = json.loads(lock_raw)
    if not isinstance(lock, dict) or not all(isinstance(lock.get(kind), dict) for kind in _KINDS):
        raise WholeTaskRejected("invalid_corpus_lock")
    if lock.get("related_locks"):
        raise WholeTaskBlocked("corpus_related_locks_unsupported")
    with tempfile.TemporaryDirectory(prefix="asset-whole-task-") as temporary:
        root = Path(temporary).resolve(strict=True)
        _write(root / "lock.json", lock_raw)
        for kind in _KINDS:
            reference = lock[kind]
            try:
                relative_path(reference["path"])
            except (ValueError, KeyError, TypeError):
                raise WholeTaskRejected("invalid_corpus_lock") from None
            _write(root / reference["path"], _document(documents, kind, reference["digest"]))
        catalog = json.loads(_document(documents, "catalog", lock["catalog"]["digest"]))
        # The release verifier reads redistributable evidence at the path the
        # catalog declares; everything else is resolved by evidence ID.
        declared = {
            item["id"]: item["path"]
            for item in catalog.get("evidence", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        evidence_root = root / "evidence"
        evidence_root.mkdir()
        located = {}
        for evidence_id, item in sorted(evidence.items()):
            payload = item["payload"]
            if (
                hashlib.sha256(payload).hexdigest() != item["sha256"]
                or len(payload) != item["bytes"]
            ):
                raise WholeTaskRejected("evidence_digest")
            private = evidence_root / "by-id" / hashlib.sha256(evidence_id.encode()).hexdigest()
            _write(private, payload)
            located[evidence_id] = private
            if evidence_id in declared:
                try:
                    relative_path(declared[evidence_id])
                except ValueError:
                    raise WholeTaskRejected("invalid_evidence_path") from None
                _write(evidence_root / declared[evidence_id], payload)

        class Resolver:
            def resolve(self, evidence_id):
                return located.get(evidence_id)

        try:
            return load_asset_release(
                root / "lock.json",
                evidence_root=evidence_root,
                related_roots={},
                evidence_resolver=Resolver(),
            )
        except AssetCorpusError as exc:
            if str(exc) in ("missing_file", "input_unresolved"):
                raise WholeTaskBlocked("corpus_unresolved") from None
            raise WholeTaskRejected(str(exc)) from None


def release_labels(release: VerifiedAssetRelease) -> dict[str, Mapping]:
    """Frozen included adjudications by case ID (the probe labels)."""
    return {
        label["case_id"]: label
        for label in release.labels["adjudications"]
        if label["disposition"] == "include"
    }


def _assets(release: VerifiedAssetRelease) -> dict[str, tuple]:
    return {
        a["record"]["asset_id"]: (
            a["record"]["content_hash"],
            a["record"]["identity"].get("schema_hash"),
        )
        for a in release.catalog["assets"]
    }


def bind_host_tasks(tasks: Mapping, release: VerifiedAssetRelease) -> None:
    """Each task's prompt case, lane, families and asset bindings are frozen corpus facts."""
    cases = {case["id"]: case for case in release.cases["cases"]}
    assets = _assets(release)
    for task in tasks["tasks"]:
        case = cases.get(task["prompt_case_id"])
        if (
            case is None
            or case["lane"] != task["lane"]
            or not set(task["family_ids"]) <= set(case["task_family_ids"])
        ):
            raise WholeTaskRejected("case_binding")
        for binding in task["asset_bindings"]:
            if assets.get(binding["asset_id"]) != (
                binding["content_hash"],
                binding["schema_hash"],
            ):
                raise WholeTaskRejected("asset_binding")


def bind_episodes(episodes: Mapping, release: VerifiedAssetRelease) -> None:
    """Every episode case exists; a present correction target keeps its exact bytes.

    A correction target missing from the pool is retained (and graded
    incorrect), never replaced, as section 12.6.5 requires.
    """
    cases = {case["id"] for case in release.cases["cases"]}
    assets = _assets(release)
    for episode in episodes["episodes"]:
        ids = [c["case_id"] for c in episode["corrections"]]
        ids += episode["paraphrase_case_ids"] + episode["control_case_ids"]
        ids += episode["reset_probe_case_ids"]
        if not set(ids) <= cases:
            raise WholeTaskRejected("case_binding")
        for correction in episode["corrections"]:
            known = assets.get(correction["target_asset_id"])
            if known is not None and known != (
                correction["content_hash"],
                correction["schema_hash"],
            ):
                raise WholeTaskRejected("asset_binding")
