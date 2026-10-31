"""Source-grounded development inputs, separate from human qualification (RM40)."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_corpus import _catalog_checks, _evidence_references
from relaymind.evaluation.asset_corpus_io import (
    ORIGINAL_BYTES,
    AssetCorpusError,
    document,
    read_local,
)
from relaymind.evaluation.asset_judgments import check_references, id_map
from relaymind.schema_util import freeze_json
from relaymind.selection.asset_contract import parse_asset_request


@dataclass(frozen=True)
class VerifiedDiagnostic:
    document: Mapping
    catalog: Mapping
    qualification: str = field(default="provisional", init=False)
    acceptance_eligible: bool = field(default=False, init=False)


def query_envelope(
    case: Mapping, *, recommendation_id: str, revision: int = 1, deadline_ms: int = 500
) -> dict:
    """Normal v2 query, with no references, original bodies or development labels."""
    language = case["query_language"]
    return dict(
        schema_version=2,
        recommendation_id=recommendation_id,
        task_id=case["id"],
        task_text=case["task_text"],
        conversation=case["conversation"],
        scope={"user_id": "source-benchmark", "workspace_id": "disposable"},
        permissions=["assets.read"],
        requested_kinds=case["requested_kinds"],
        selection_scope="discovery",
        catalog_revision=revision,
        host_snapshot_revision=None,
        language_tags={"zh": ["zh-Hans"], "en": ["en"], "mixed": ["en", "zh-Hans"], "unknown": []}[
            language
        ],
        code_switched=language == "mixed",
        deadline_ms=deadline_ms,
        candidate_limit=10,
    )


def load_asset_diagnostic(
    path: Path, *, catalog_path: Path, evidence_root: Path
) -> VerifiedDiagnostic:
    """Verify the complete supplied catalog and original bytes, without inventing people."""
    path, catalog_path = Path(path), Path(catalog_path)
    raw = document(path.parent, path.name, "diagnostic")
    catalog = document(catalog_path.parent, catalog_path.name, "catalog_set")
    if raw["digest"] != canonical_digest({k: v for k, v in raw.items() if k != "digest"}):
        raise AssetCorpusError("diagnostic_digest")
    if raw["catalog_digest"] != canonical_digest(catalog):
        raise AssetCorpusError("catalog_digest")
    if raw["dataset_id"] != catalog["dataset_id"]:
        raise AssetCorpusError("dataset_mismatch")
    _catalog_checks(catalog)
    evidence = id_map(catalog["evidence"])
    for key, row in id_map(raw["evidence"]).items():
        if key in evidence and evidence[key] != row:
            raise AssetCorpusError("conflicting_evidence")
        evidence[key] = row
    for row in evidence.values():
        if row["path"] is None:
            raise AssetCorpusError("evidence_unavailable")
        payload = read_local(evidence_root, row["path"], ORIGINAL_BYTES)
        if len(payload) != row["bytes"] or hashlib.sha256(payload).hexdigest() != row["sha256"]:
            raise AssetCorpusError("evidence_digest")
    check_references(_evidence_references(catalog), evidence)
    assets = {row["record"]["asset_id"]: row for row in catalog["assets"]}
    for entry in assets.values():
        original = evidence.get(entry["original_evidence_id"])
        if original is None or original["sha256"] != entry["record"]["content_hash"]:
            raise AssetCorpusError("original_digest")
    for case in id_map(raw["cases"]).values():
        parse_asset_request(
            query_envelope(case, recommendation_id="00000000-0000-4000-8000-000000000001")
            | {"catalog": []}
        )
        check_references(case["source_asset_ids"], assets)
        check_references(case["acceptable_asset_ids"], assets)
        check_references(case["source_evidence_ids"], evidence)
        check_references(case["reference_evidence_ids"], evidence)
        if bool(case["acceptable_asset_ids"]) != (
            case["reference_class"] in ("positive", "ambiguous")
        ):
            raise AssetCorpusError("reference_class")
        if any(
            assets[asset_id]["record"]["kind"] not in case["requested_kinds"]
            for asset_id in case["acceptable_asset_ids"]
        ):
            raise AssetCorpusError("reference_kind")
    return VerifiedDiagnostic(freeze_json(raw), freeze_json(catalog))
