"""Offline integrity check for an operator's Matrix ZIP capture (#188).

RM07/RM12/RM16/RM25; specification 0.50 sections 8, 12.4, 13.
Probe-only metadata, not a market API or production import contract.
Original archives are opaque bytes: never extracted, imported, or executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_ASSETS = 200
IDENTITY_FIELDS = (
    "source_id",
    "asset_id",
    "name",
    "version",
    "captured_at",
    "source_url",
    "download_url",
    "original_path",
    "sha256",
)


def bounded_bytes(path: Path, limit: int, reason: str) -> bytes:
    """Read only regular files, bounded even if a file grows during inspection."""
    if not path.is_file():
        raise ValueError("capture input is not a regular file")
    if path.stat().st_size > limit:
        raise ValueError(reason)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(reason)
    return raw


def inspect_capture(manifest_path: Path, capture_root: Path) -> dict[str, object]:
    """Check declared identities and byte hashes, without asserting source rights."""
    manifest = json.loads(
        bounded_bytes(manifest_path, MAX_MANIFEST_BYTES, "manifest exceeds byte limit")
    )
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "matrix-capture-probe.v1"
    ):
        raise ValueError("unsupported manifest schema")
    entries = manifest.get("assets")
    if not isinstance(entries, list):
        raise ValueError("assets must be a list")
    if len(entries) > MAX_ASSETS:
        raise ValueError("asset count exceeds limit")
    root = capture_root.resolve(strict=True)
    seen = set()
    pending = []
    total = 0
    for asset in entries:
        for field in IDENTITY_FIELDS:
            if (
                not isinstance(asset, dict)
                or not isinstance(asset.get(field), str)
                or not asset[field].strip()
            ):
                raise ValueError("missing or invalid " + field)
        try:
            captured = datetime.fromisoformat(asset["captured_at"])
            if captured.utcoffset() != timedelta(0):
                raise ValueError
        except ValueError:
            raise ValueError("missing or invalid captured_at") from None
        for field in ("license", "attribution"):
            if asset.get(field) is not None and not isinstance(asset[field], str):
                raise ValueError("missing or invalid " + field)
        identity = (asset["source_id"], asset["asset_id"])
        if identity in seen:
            raise ValueError("duplicate source identity")
        seen.add(identity)
        relative = Path(asset["original_path"])
        original = (root / relative).resolve(strict=True)
        if relative.is_absolute() or not original.is_relative_to(root):
            raise ValueError("original path escapes capture root")
        size = original.stat().st_size
        if size > MAX_ASSET_BYTES:
            raise ValueError("asset exceeds byte limit")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("capture exceeds total byte limit")
        pending.append((asset, original))
    assets = []
    for asset, original in pending:
        raw = bounded_bytes(original, MAX_ASSET_BYTES, "asset exceeds byte limit")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != asset["sha256"]:
            raise ValueError("hash mismatch")
        assets.append(
            {
                **{field: asset[field] for field in IDENTITY_FIELDS},
                "sha256": digest,
                "size_bytes": len(raw),
                "license": asset.get("license") or "unknown",
                "attribution": asset.get("attribution"),
                "redistribution_permission": "not_assessed",
            }
        )
    return {
        "schema_version": "matrix-integrity-probe.v1",
        "asset_count": len(assets),
        "assets": assets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = inspect_capture(args.manifest, args.capture_root)
        protected = [
            args.manifest,
            *(args.capture_root / a["original_path"] for a in report["assets"]),
        ]
        if any(
            args.output.resolve() == path.resolve()
            or (args.output.exists() and args.output.samefile(path))
            for path in protected
        ):
            raise ValueError("output would overwrite input")
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
