"""Closed bounded benchmark I/O sharing the corpus ordinary-file fence (RM40)."""

import math
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import FormatChecker, ValidationError
from jsonschema.validators import extend

from relaymind.canonical import canonical_json
from relaymind.evaluation.asset_corpus_io import (
    AssetCorpusError,
    parse_json,
    read_local,
    relative_path,
)
from relaymind.schema_util import StrictValidator, load_packaged_schema

CONFIG_BYTES = 4 * 1024 * 1024
REPORT_BYTES = 256 * 1024 * 1024


def _max_bytes(validator, limit, instance, schema):
    if isinstance(instance, str) and len(instance.encode("utf-8")) > limit:
        yield ValidationError("byte_bound")


def _relative(validator, enabled, instance, schema):
    if enabled and isinstance(instance, str):
        try:
            relative_path(instance)
            if "$" in instance or "`" in instance or "~" in instance:
                raise ValueError("unsafe_path")
        except ValueError:
            yield ValidationError("unsafe_path")


def _sorted_ids(validator, enabled, instance, schema):
    if (
        enabled
        and isinstance(instance, (list, tuple))
        and all(isinstance(item, str) for item in instance)
        and list(instance) != sorted(set(instance), key=lambda item: item.encode("utf-8"))
    ):
        yield ValidationError("unsorted_id_set")


def _gpu_order(validator, enabled, instance, schema):
    if not enabled or not isinstance(instance, list):
        return
    if not all(
        isinstance(item, Mapping)
        and all(
            field in item and isinstance(item[field], (str, type(None)))
            for field in ("model", "device", "cores")
        )
        for item in instance
    ):
        return
    keys = [
        tuple(
            (item[field] is not None, item[field] or "") for field in ("model", "device", "cores")
        )
        for item in instance
    ]
    if keys != sorted(set(keys)):
        yield ValidationError("unsorted_gpu")


def asset_dimension_sort_key(dimensions: Mapping) -> tuple:
    """Published dimensions order; stale null<false<true and size is numeric."""
    return (
        dimensions["lane"].encode("utf-8"),
        dimensions["kind"].encode("utf-8"),
        dimensions["availability"].encode("utf-8"),
        0 if dimensions["stale"] is None else 2 if dimensions["stale"] else 1,
        dimensions["selection_scope"].encode("utf-8"),
        dimensions["catalog_size"],
        dimensions["retrieval_mode"].encode("utf-8"),
        dimensions["subset"].encode("utf-8"),
    )


_Validator = extend(
    StrictValidator,
    {
        "x-maxBytes": _max_bytes,
        "x-relative": _relative,
        "x-sorted": _sorted_ids,
        "x-gpu-order": _gpu_order,
    },
)
_SCHEMAS = {
    name: load_packaged_schema("relaymind.evaluation", f"asset_benchmark_{name}.v1.json")
    for name in ("config", "report", "comparison")
}


def validate_closed_document(schema: Mapping, value: Any, error: str) -> None:
    """Strict closed-schema check shared by every asset evaluation export.

    Adds the byte-bound, safe-relative-path and sorted-set keywords, finite
    numbers and UTF-8 text; any violation raises ``ValueError(error)``.
    """
    try:
        if not isinstance(value, Mapping) or next(
            _Validator(schema, format_checker=FormatChecker()).iter_errors(value), None
        ):
            raise ValueError(error)
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, Mapping):
                pending.extend(current.values())
            elif isinstance(current, (list, tuple)):
                pending.extend(current)
            elif isinstance(current, float) and not math.isfinite(current):
                raise ValueError("invalid_number")
            elif isinstance(current, str):
                current.encode("utf-8")
    except (TypeError, RecursionError, UnicodeError, OverflowError):
        raise ValueError(error) from None


def validate_benchmark_document(kind: str, value: Any, *, definition: str | None = None) -> dict:
    """Validate exact JSON types, byte bounds and closed nested product contracts."""
    schema = _SCHEMAS[kind]
    if definition:
        schema = {"$ref": "#/$defs/" + definition, "$defs": schema["$defs"]}
    validate_closed_document(schema, value, "invalid_benchmark_" + kind)
    if definition is None and kind == "comparison":
        rows = value["paired"]
        keys = [asset_dimension_sort_key(row["dimensions"]) for row in rows]
        if keys != sorted(set(keys)):
            raise ValueError("dimension_order")
    if kind == "report" and definition is None and not value["created_at"].endswith("Z"):
        raise ValueError("invalid_timestamp")
    return dict(value)


def read_json_path(path: Path, limit: int = CONFIG_BYTES) -> tuple[dict, bytes]:
    path = Path(path).absolute()
    payload = read_local(path.parent, path.name, limit)
    return parse_json(payload), payload


def atomic_bytes(path: Path, payload: bytes, *, limit: int = REPORT_BYTES) -> None:
    """Write one bounded artifact atomically; no partial report is published."""
    if len(payload) > limit:
        raise AssetCorpusError("size_limit")
    path = Path(path).absolute()
    for ancestor in (path.parent, *path.parent.parents):
        if ancestor.is_symlink() or ancestor.exists() and not ancestor.is_dir():
            raise AssetCorpusError("unsafe_file")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists() and not path.is_file() or not path.parent.is_dir():
        raise AssetCorpusError("unsafe_file")
    fd, temporary = tempfile.mkstemp(prefix=".asset-benchmark-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def json_bytes(value: Mapping) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")
