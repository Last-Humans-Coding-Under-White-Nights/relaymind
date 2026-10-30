"""Bounded ordinary local input for RM40/AC31; locators never execute."""

import json
import math
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from jsonschema import ValidationError
from jsonschema.validators import extend

from relaymind.assets.source_io import open_source_tree
from relaymind.schema_util import StrictValidator, load_packaged_schema

MIB = 1024 * 1024
DOCUMENT_BYTES = 64 * MIB
LOCK_BYTES = 4 * MIB
ORIGINAL_BYTES = 10 * MIB


class AssetCorpusError(ValueError):
    """A fixed safe failure reason; never includes supplied content."""


class EvidenceResolver(Protocol):
    """Explicit offline I/O only; returned paths must be in supplied roots."""

    def resolve(self, evidence_id: str) -> Path | None: ...


def relative_path(value: str) -> str:
    """The source reader's no-follow walk additionally fences every component."""
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 2048
        or "\\" in value
        or "\x00" in value
        or ":" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or Path(value).is_absolute()
    ):
        raise AssetCorpusError("unsafe_path")
    return value


def read_local(root: Path, relative: str, limit: int) -> bytes:
    """Read in ≤1 MiB chunks, fencing identity, growth and root replacement."""
    relative_path(relative)
    tree = file = None
    try:
        tree = open_source_tree(Path(root))
        before = tree.info(relative)
        if not before.is_regular or before.is_reparse:
            raise AssetCorpusError("unsafe_file")
        if before.size > limit:
            raise AssetCorpusError("size_limit")
        file = tree.open_file(relative)
        opened = file.info()
        if opened.fingerprint != before.fingerprint or not opened.is_regular:
            raise AssetCorpusError("file_changed")
        chunks, count = [], 0
        while True:
            chunk = file.read(min(MIB, limit + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            if count > limit or count > before.size:
                raise AssetCorpusError("file_growth")
            chunks.append(chunk)
        if count != before.size or file.info().fingerprint != before.fingerprint:
            raise AssetCorpusError("file_changed")
        if tree.info(relative).fingerprint != before.fingerprint:
            raise AssetCorpusError("file_changed")
        tree.verify_root()
        return b"".join(chunks)
    except FileNotFoundError:
        raise AssetCorpusError("missing_file") from None
    except OSError:
        raise AssetCorpusError("unsafe_file") from None
    finally:
        if file is not None:
            file.close()
        if tree is not None:
            tree.close()


def _once(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise AssetCorpusError("duplicate_key")
        result[key] = value
    return result


def _finite(value: str) -> None:
    raise AssetCorpusError("invalid_number")


def parse_json(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_once, parse_constant=_finite)
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, Mapping):
                pending.extend(current.keys())
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current)
            elif isinstance(current, str):
                current.encode("utf-8")
            elif isinstance(current, float) and not math.isfinite(current):
                raise AssetCorpusError("invalid_number")
        if not isinstance(value, dict):
            raise AssetCorpusError("invalid_document")
        return value
    except AssetCorpusError:
        raise
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise AssetCorpusError("invalid_json") from None


def _max_bytes(validator, limit, instance, schema):
    if isinstance(instance, str) and len(instance.encode("utf-8")) > limit:
        yield ValidationError("UTF-8 byte limit exceeded")


def _sorted(validator, enabled, instance, schema):
    if enabled and isinstance(instance, list) and all(isinstance(v, str) for v in instance):
        if instance != sorted(instance, key=lambda v: v.encode("utf-8")):
            yield ValidationError("set must be sorted")


def _utc(validator, enabled, instance, schema):
    if enabled and isinstance(instance, str):
        try:
            datetime.fromisoformat(instance)
        except ValueError:
            yield ValidationError("invalid UTC timestamp")


_CorpusValidator = extend(
    StrictValidator, {"x-maxBytes": _max_bytes, "x-sorted": _sorted, "x-utc": _utc}
)
_VALIDATORS = {
    name: _CorpusValidator(load_packaged_schema("relaymind.evaluation", f"asset_{name}.v1.json"))
    for name in ("catalog_set", "cases", "labels", "lock")
}


def document(root: Path, relative: str, kind: str) -> dict[str, Any]:
    raw = parse_json(read_local(root, relative, LOCK_BYTES if kind == "lock" else DOCUMENT_BYTES))
    try:
        if next(_VALIDATORS[kind].iter_errors(raw), None) is not None:
            raise AssetCorpusError("invalid_schema")
    except (RecursionError, UnicodeError):
        raise AssetCorpusError("invalid_schema") from None
    return raw
