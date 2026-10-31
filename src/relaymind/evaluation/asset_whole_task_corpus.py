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
            return require_available(
                load_asset_release(
                    root / "lock.json",
                    evidence_root=evidence_root,
                    related_roots={},
                    evidence_resolver=Resolver(),
                )
            )
        except AssetCorpusError as exc:
            if str(exc) in ("missing_file", "input_unresolved"):
                raise WholeTaskBlocked("corpus_unresolved") from None
            raise WholeTaskRejected(str(exc)) from None


def require_available(release: VerifiedAssetRelease) -> VerifiedAssetRelease:
    """Every corpus original must be resolved; unavailable evidence blocks."""
    if any(finding.status == "unavailable" for finding in release.evidence_findings):
        raise WholeTaskBlocked("corpus_evidence_unresolved")
    return release


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
    cases = {case["id"]: case for case in release.cases["cases"]}
    assets = _assets(release)
    for episode in episodes["episodes"]:
        ids = [c["case_id"] for c in episode["corrections"]]
        ids += episode["paraphrase_case_ids"] + episode["control_case_ids"]
        ids += episode["reset_probe_case_ids"]
        if not set(ids) <= set(cases):
            raise WholeTaskRejected("case_binding")
        # The episode's intent families are frozen facts of its cases.
        families = {f for case_id in ids for f in cases[case_id]["task_family_ids"]}
        if not set(episode["intent_family_ids"]) <= families:
            raise WholeTaskRejected("case_binding")
        for correction in episode["corrections"]:
            known = assets.get(correction["target_asset_id"])
            if known is not None and known != (
                correction["content_hash"],
                correction["schema_hash"],
            ):
                raise WholeTaskRejected("asset_binding")


def _effective(value: object) -> dict:
    from relaymind.evaluation.asset_evidence import validate_definition

    try:
        return validate_definition("EffectiveConfigV1", value)
    except (ValueError, AssetCorpusError):
        raise WholeTaskRejected("episode_config") from None


def verify_episode_configs(freeze: Mapping, configs: Mapping[str, Mapping]) -> None:
    """Off/on are complete effective configurations differing only in adaptation.

    Section 12.6.5: off and on are EffectiveConfigV1 with their referenced
    closure. They share catalog, selector, scope, budgets, fallback and
    sources, and differ only in the declared adaptation enablement; the on arm
    binds the frozen semantic adaptation configuration. This named ablation is
    the only place the retrieval rule against adaptation is relaxed.
    """
    from relaymind.evaluation.asset_evidence import validate_effective_integrity

    off = _effective(configs[freeze["off_config_digest"]])
    on = _effective(configs[freeze["on_config_digest"]])
    if off["adaptation"] != dict(enabled=False, config_digest=None) or on["adaptation"] != dict(
        enabled=True, config_digest=freeze["adaptation_config_digest"]
    ):
        raise WholeTaskRejected("ablation_policy")
    disabled = dict(on, adaptation=dict(enabled=False, config_digest=None))
    try:
        validate_effective_integrity(off)
        validate_effective_integrity(disabled)
    except (ValueError, AssetCorpusError):
        raise WholeTaskRejected("episode_config") from None
    # Exhaustive: everything but the adaptation enablement is identical,
    # including the selector kind, name, version and endpoint.
    if {k: v for k, v in off.items() if k != "adaptation"} != {
        k: v for k, v in on.items() if k != "adaptation"
    }:
        raise WholeTaskRejected("ablation_policy")


def corpus_offers(release: VerifiedAssetRelease, effective: Mapping[str, Mapping]):
    """Independent probe offer reconstruction for the off/on arms.

    Returns ``offers(case_id, arm, revision) -> (request_digest,
    projection_digest)`` using the retrieval comparator's request
    reconstruction over the verified catalog projection and the arm's
    verified effective configuration (section 12.6.2 Row offer binding).
    """
    from relaymind.evaluation.asset_benchmark import stable_request_digest
    from relaymind.evaluation.asset_comparison import _request_for_case

    cases = {case["id"]: case for case in release.cases["cases"]}
    arms = {}
    for arm, config in effective.items():
        eligible = {e["asset_id"] for e in config["eligibility"] if e["eligible"]}
        catalog = [
            dict(a["record"])
            for a in release.catalog["assets"]
            if a["record"]["asset_id"] in eligible
            and a["record"]["kind"] in config["requested_kinds"]
        ]
        settings = config["recommendations"]
        normal = dict(
            scope={k: settings[k] for k in ("user_id", "workspace_id")},
            permissions=config["permissions"],
            requested_kinds=config["requested_kinds"],
            selection_scope=config["selection_scope"],
            budgets=config["budgets"],
        )
        arms[arm] = (normal, catalog, canonical_digest(catalog))

    def offers(case_id: str, arm: str, revision: int) -> tuple[str, str]:
        normal, catalog, projection = arms[arm]
        request = _request_for_case(cases[case_id], normal, catalog, revision)
        return stable_request_digest(request.to_json_object()), projection

    return offers


def verify_reference_closure(
    effective: Mapping,
    documents: Mapping[str, Mapping],
    manifest: Mapping,
    environment: Mapping,
    *,
    adaptation_digest: str | None,
) -> bool:
    """Resolve and validate one arm's referenced configuration closure.

    Reuses the retrieval closure verifier over a manifest view holding only
    this arm's references. A missing preimage blocks; a contradiction rejects.
    When the arm references an adaptation configuration, it is parsed and must
    be the frozen one. Returns whether an adaptation configuration was verified.
    """
    from relaymind.evaluation.asset_evidence import verify_asset_configuration_closure

    required = {reference["document_id"] for reference in effective["references"]}
    if environment["runtime_config_digest"] is not None:
        required.add("referenced_config:" + environment["runtime_config_digest"])
    view = dict(documents=[d for d in manifest["documents"] if d["id"] in required])
    try:
        verify_asset_configuration_closure(documents, view, effective, environment)
    except AssetCorpusError as exc:
        if str(exc) == "input_unresolved":
            raise WholeTaskBlocked("referenced_config_unresolved") from None
        raise WholeTaskRejected(str(exc)) from None
    adaptation = [r for r in effective["references"] if r["role"] == "adaptation"]
    if adaptation and adaptation_digest is not None:
        if adaptation[0]["document_id"] != "referenced_config:" + adaptation_digest:
            raise WholeTaskRejected("ablation_policy")
        return True
    return False
