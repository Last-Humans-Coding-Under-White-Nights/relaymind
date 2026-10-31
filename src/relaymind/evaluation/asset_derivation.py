"""Reproduce a permitted, locally supplied same-source export (RM40/AC31).

Input is the normative four-document release, not a downloader manifest. Already
locked exclusions remain unchanged. Invalid input fails without a partial export.
"""

import hashlib
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from relaymind.evaluation.asset_corpus import assess_asset_release, load_asset_release
from relaymind.evaluation.asset_corpus_io import (
    DOCUMENT_BYTES,
    LOCK_BYTES,
    ORIGINAL_BYTES,
    AssetCorpusError,
    read_local,
    relative_path,
)
from relaymind.evaluation.asset_qualification import QualificationAssessment
from relaymind.schema_util import freeze_json


@dataclass(frozen=True)
class DerivationResult:
    output_paths: tuple[Path, ...]
    digests: Mapping[str, str]
    lock_digest: str
    exclusions: tuple[Mapping, ...]
    assessment: QualificationAssessment


def derive_matrix_export(input_root: Path, lock_path: Path, output_root: Path) -> DerivationResult:
    """Reproduce the exact four-document supplied Matrix/local release offline."""
    return _derive_export(input_root, lock_path, output_root, require_mcp=False)


def derive_mcp_export(input_root: Path, lock_path: Path, output_root: Path) -> DerivationResult:
    """Reproduce pinned MCP metadata/originals without executing or authenticating it."""
    return _derive_export(input_root, lock_path, output_root, require_mcp=True)


def _derive_export(
    input_root: Path, lock_path: Path, output_root: Path, *, require_mcp: bool
) -> DerivationResult:
    """Validate and copy exact locked bytes to a new private local export.

    No acquisition permission is inferred. Redistribution permission is separate:
    a locally retained permitted export may contain nonredistributable originals.
    This helper never publishes the output. Related-release or restricted-null
    inputs need their complete explicitly staged export before reproduction.
    """
    input_root, lock_path, output_root = map(Path, (input_root, lock_path, output_root))
    try:
        lock_relative = lock_path.absolute().relative_to(input_root.absolute()).as_posix()
    except ValueError:
        raise AssetCorpusError("lock_outside_input") from None
    relative_path(lock_relative)
    # Fence the entire input-relative walk, including nested lock directories.
    read_local(input_root, lock_relative, LOCK_BYTES)
    release = load_asset_release(lock_path, evidence_root=input_root, related_roots={})
    if require_mcp and not any(
        asset["record"]["kind"] == "mcp_tool" for asset in release.catalog["assets"]
    ):
        raise AssetCorpusError("mcp_catalog_required")
    if any(finding.status != "verified" for finding in release.evidence_findings):
        raise AssetCorpusError("evidence_unavailable")
    for asset in release.catalog["assets"]:
        if any(
            asset["rights"][key] != "permitted"
            for key in ("acquisition", "evaluation", "retention")
        ):
            raise AssetCorpusError("rights_unqualified")
    if output_root.exists() or output_root.is_symlink():
        raise AssetCorpusError("output_exists")
    paths = {lock_relative: LOCK_BYTES}
    relative_parent = Path(lock_relative).parent
    for key in ("catalog", "cases", "labels"):
        path = (relative_parent / release.lock[key]["path"]).as_posix()
        relative_path(path)
        paths[path] = DOCUMENT_BYTES
    for item in release.catalog["evidence"]:
        if item["path"] is None:
            raise AssetCorpusError("evidence_unavailable")
        paths[item["path"]] = min(paths.get(item["path"], ORIGINAL_BYTES), ORIGINAL_BYTES)
    created = succeeded = False
    try:
        output_root.mkdir(mode=0o700)
        created = True
        digests = {}
        for relative, limit in sorted(paths.items()):
            payload = read_local(input_root, relative, limit)
            destination = output_root / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with destination.open("xb") as file:
                file.write(payload)
            destination.chmod(0o600)
            digests[relative] = hashlib.sha256(payload).hexdigest()
        # Input changes between validation and copying never produce a valid
        # output claim. Recheck the copied closure before exposing its result.
        copied = load_asset_release(
            output_root / lock_relative, evidence_root=output_root, related_roots={}
        )
        if copied.lock["digest"] != release.lock["digest"]:
            raise AssetCorpusError("source_changed")
        succeeded = True
        return DerivationResult(
            tuple(output_root / relative for relative in sorted(paths)),
            freeze_json(digests),
            copied.lock["digest"],
            tuple(copied.catalog["exclusions"]),
            assess_asset_release(copied),
        )
    except OSError:
        raise AssetCorpusError("output_io") from None
    finally:
        # A success returns after all files and final closure verification. On
        # failure only this call's newly created directory is eligible for removal.
        if created and not succeeded:
            shutil.rmtree(output_root)
