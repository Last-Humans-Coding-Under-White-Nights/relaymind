"""Ordinary product catalogs derived from acquired originals (RM34/RM40, AC25/AC31)."""

import hashlib
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from relaymind.assets.catalog import AssetValidationError, validate_asset_record
from relaymind.assets.sources import CaptureContext, SourceFailure, _frontmatter
from relaymind.canonical import canonical_digest, canonical_json
from relaymind.evaluation.asset_archive import inspect_skill_archive
from relaymind.evaluation.asset_corpus import _catalog_checks
from relaymind.evaluation.asset_corpus_io import MIB, AssetCorpusError, document, parse_json


@dataclass(frozen=True)
class SourceOriginal:
    asset_id: str
    source_id: str
    source_url: str
    revision: str
    version: str | None
    captured_at: str
    kind: str
    name: str
    payload: bytes
    sha256: str
    format: str
    metadata_bytes: bytes | None = None
    license_bytes: bytes | None = None


@dataclass(frozen=True)
class SourceCatalogResult:
    catalog_path: Path
    admitted: int
    excluded: int
    import_paths: Mapping[str, Path]


def source_language(value: str) -> str:
    """Declared diagnostic heuristic: Han>=10% of Han+ASCII letters is zh; else mixed/en."""
    han = len(re.findall("[\u3400-\u9fff]", value))
    latin = len(re.findall("[A-Za-z]", value))
    if han and han / max(1, han + latin) >= 0.10:
        return "zh-Hans"
    if han:
        return "mixed"
    return "en" if latin else "unknown"


def _projection(source: SourceOriginal) -> tuple[dict, bytes, dict[str, bytes], str]:
    if len(source.payload) > 10 * MIB:
        raise AssetCorpusError("source_limit")
    if hashlib.sha256(source.payload).hexdigest() != source.sha256:
        raise AssetCorpusError("source_digest_mismatch")
    selected = {}
    if source.kind == "skill":
        if source.format == "skill_zip":
            selected = dict(inspect_skill_archive(source.payload).selected)
            original = selected["SKILL.md"]
        elif source.format == "skill_md":
            original = source.payload
        else:
            raise AssetCorpusError("unsupported_original")
        try:
            captured_at = datetime.fromisoformat(source.captured_at)
        except ValueError:
            raise AssetCorpusError("invalid_source_text") from None
        metadata, body = _frontmatter(original, CaptureContext(captured_at))
        if metadata.get("name") != source.name:
            raise AssetCorpusError("package_identity_mismatch")
        description = metadata.get("description")
        if description is not None and not isinstance(description, str):
            raise AssetCorpusError("invalid_source_text")
        identity = {"package_id": source.name, "package_version": source.version}
        body_language = source_language(body)
    elif source.kind == "mcp_tool" and source.format == "mcp_snapshot":
        original = source.payload
        raw = parse_json(original)
        if (
            raw.get("name") != source.name
            or not isinstance(raw.get("inputSchema"), dict)
            or not isinstance(raw.get("description"), (str, type(None)))
        ):
            raise AssetCorpusError("unsupported_tool_definition")
        description = raw.get("description")
        schema = raw["inputSchema"]
        identity = dict(
            server_id=source.source_id,
            tool_name=source.name,
            exposed_name=source.name,
            input_schema=schema,
            schema_hash=canonical_digest(schema),
        )
        body_language = source_language(description or "")
    else:
        raise AssetCorpusError("unsupported_original")
    language = source_language(description or "")
    tags = ["en", "zh-Hans"] if language == "mixed" else [] if language == "unknown" else [language]
    record = dict(
        schema_version=1,
        asset_id=source.asset_id,
        kind=source.kind,
        source_id=source.source_id,
        source_locator=source.source_url,
        source_version=source.revision,
        captured_at=source.captured_at,
        content_hash=hashlib.sha256(original).hexdigest(),
        description=description,
        language_tags=tags,
        dependencies=[],
        availability="unknown",
        stale=False,
        identity=identity,
    )
    return validate_asset_record(record).to_json_object(), original, selected, body_language


def build_source_catalog(
    sources: Sequence[SourceOriginal],
    output: Path,
    *,
    dataset_id: str,
    exclusions: Sequence[Mapping] = (),
    additional_evidence: Sequence[tuple[str, bytes]] = (),
) -> SourceCatalogResult:
    """Build actual-source records and explicit exclusions; never invent labels or people."""
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise AssetCorpusError("output_exists")
    if len(sources) + len(exclusions) > 1000 or sum(len(s.payload) for s in sources) > 100 * MIB:
        raise AssetCorpusError("source_limit")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = {}
    assets = []
    rejected = [dict(row) for row in exclusions]
    identities = set()
    hashes = set()
    asset_ids = set()

    def retain(payload: bytes, kind: str) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        key = kind + ":" + digest
        relative = "originals/" + digest
        path = output / relative
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            path.write_bytes(payload)
        evidence[key] = dict(
            id=key, kind=kind, access="restricted", path=relative, sha256=digest, bytes=len(payload)
        )
        return key

    try:
        for kind, payload in additional_evidence:
            if kind not in ("license", "notice", "source_provenance") or len(payload) > 4 * MIB:
                raise AssetCorpusError("source_limit")
            retain(payload, kind)
        for source in sources:
            if len(source.payload) > 10 * MIB:
                rejected.append(
                    dict(
                        source_id=source.source_id,
                        original_id=source.asset_id,
                        reason="source_limit",
                        evidence_ids=[],
                    )
                )
                continue
            proof = retain(source.payload, "source_provenance")
            try:
                record, original, selected, language = _projection(source)
                identity = (source.source_id, source.kind, source.name)
                if (
                    source.asset_id in asset_ids
                    or identity in identities
                    or (source.kind == "skill" and record["content_hash"] in hashes)
                ):
                    raise AssetCorpusError("duplicate_original")
                identities.add(identity)
                hashes.add(record["content_hash"])
                asset_ids.add(source.asset_id)
                original_id = retain(original, "original")
                proof_ids = [proof]
                if source.metadata_bytes is not None:
                    proof_ids.append(retain(source.metadata_bytes, "source_provenance"))
                rights_ids = []
                notices = []
                if source.license_bytes is not None:
                    rights_ids.append(retain(source.license_bytes, "license"))
                for name, payload in selected.items():
                    if name.startswith(("LICENSE", "COPYING", "COPYRIGHT")):
                        rights_ids.append(retain(payload, "license"))
                    elif name.startswith("NOTICE"):
                        notices.append(retain(payload, "notice"))
                rights = {
                    k: "unknown"
                    for k in ("acquisition", "evaluation", "retention", "redistribution")
                }
                rights.update(
                    evidence_ids=sorted(set(rights_ids)), notice_evidence_ids=sorted(set(notices))
                )
                origin = (
                    "genuine_mcp"
                    if source.kind == "mcp_tool"
                    else "original_en"
                    if language == "en"
                    else "matrix_original_zh"
                    if source.source_id == "matrix" and language == "zh-Hans"
                    else "other_original"
                )
                languages = (
                    ["en", "zh-Hans"]
                    if language == "mixed"
                    else []
                    if language == "unknown"
                    else [language]
                )
                assets.append(
                    dict(
                        record=record,
                        origin=origin,
                        original_languages=languages,
                        asset_family_ids=["source:" + source.asset_id],
                        original_evidence_id=original_id,
                        source_url=source.source_url,
                        source_revision=source.revision,
                        rights=rights,
                        language_review_evidence_ids=[],
                        provenance_evidence_ids=sorted(set(proof_ids)),
                    )
                )
            except (AssetCorpusError, AssetValidationError, SourceFailure, UnicodeError) as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, (AssetCorpusError, AssetValidationError))
                    else "invalid_source_text"
                )
                rejected.append(
                    dict(
                        source_id=source.source_id,
                        original_id=source.asset_id,
                        reason=reason,
                        evidence_ids=[proof],
                    )
                )
        catalog = dict(
            schema_version=1,
            dataset_id=dataset_id,
            release=1,
            purpose="development",
            assets=sorted(assets, key=lambda x: x["record"]["asset_id"].encode()),
            people=[],
            equivalent_pairs=[],
            exclusions=sorted(
                rejected,
                key=lambda x: (
                    x["source_id"].encode(),
                    b"" if x["original_id"] is None else x["original_id"].encode(),
                    x["reason"].encode(),
                ),
            ),
            evidence=sorted(evidence.values(), key=lambda x: x["id"]),
        )
        _catalog_checks(catalog)
        path = output / "catalog.json"
        path.write_bytes((canonical_json(catalog) + "\n").encode("utf-8"))
        document(output, "catalog.json", "catalog_set")
        imports = {}
        by_id = {e["id"]: e for e in evidence.values()}
        for source_id in sorted(
            {s.source_id for s in sources} | {e["source_id"] for e in rejected}
        ):
            group = [a for a in catalog["assets"] if a["record"]["source_id"] == source_id]
            import_path = output / (
                "import-" + hashlib.sha256(source_id.encode()).hexdigest()[:16] + ".json"
            )
            records = []
            for asset in group:
                raw = {
                    k: v
                    for k, v in asset["record"].items()
                    if k not in ("schema_version", "source_id", "availability", "stale")
                }
                records.append(
                    dict(record=raw, original_path=by_id[asset["original_evidence_id"]]["path"])
                )
            imported = dict(
                schema_version=1,
                source_id=source_id,
                source_version=canonical_digest(records),
                captured_at=max(
                    (a["record"]["captured_at"] for a in group),
                    default=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                ),
                complete=False,
                records=records,
                removed_asset_ids=[],
            )
            import_path.write_bytes((canonical_json(imported) + "\n").encode("utf-8"))
            imports[source_id] = import_path
        return SourceCatalogResult(path, len(assets), len(rejected), MappingProxyType(imports))
    except BaseException:
        shutil.rmtree(output)
        raise
