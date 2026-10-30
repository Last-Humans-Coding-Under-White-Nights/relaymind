"""Bounded staging of local folders and explicit metadata imports (RM34).

Original bytes stay under operator control. Captures contain only safe projections,
original digests and locators. No file is executed and no archive is unpacked.
"""

import errno
import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from yaml.tokens import AliasToken, AnchorToken, TagToken

from relaymind.assets.catalog import (
    AssetValidationError,
    InventoryAsset,
    SourceCapture,
    SourceInventory,
    StagedFile,
    check_language_tags,
    failure_exclusions,
    project_asset,
    text,
    timestamp,
    validate_asset_record,
    validate_document,
)
from relaymind.assets.settings import SourceConfig
from relaymind.assets.source_io import NativeIoAborted, SourceInfo, open_source_tree
from relaymind.canonical import canonical_digest
from relaymind.redaction import Redactor
from relaymind.schema_util import freeze_json, load_packaged_schema
from relaymind.store.run_store import utc_iso
from relaymind.tools.registry import StrictValidator

__all__ = [
    "CaptureContext",
    "InventoryAsset",
    "SourceCapture",
    "SourceFailure",
    "SourceInventory",
    "StagedFile",
    "capture_source",
    "source_config_digest",
]

_IMPORT_VALIDATOR = StrictValidator(
    load_packaged_schema("relaymind.assets", "asset_metadata_import.v1.json")
)
_CHUNK = 1024 * 1024


class SourceFailure(Exception):
    """Safe acquisition failure; candidate content is never included."""

    def __init__(
        self,
        reason: str,
        *,
        asset_id: str | None = None,
        rejected_count: int = 1,
        received_bytes: int = 0,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rejected_count = rejected_count
        self.received_bytes = received_bytes
        self.exclusions = failure_exclusions(reason, asset_id)


@dataclass
class CaptureContext:
    captured_at: datetime
    monotonic: Callable[[], float] = time.monotonic
    cancelled: Callable[[], bool] = lambda: False
    redactor: Redactor = field(default_factory=Redactor)
    secrets: frozenset[str] = frozenset()
    received_bytes: int = 0
    record_count: int = 0
    observe_busy: Callable[[], bool] = lambda: False
    busy_observed_at: float | None = None
    batch_durations_ms: list[float] = field(default_factory=list)
    batch_started: float = field(init=False)
    batch_records: int = 0
    batch_observations: list[dict] = field(default_factory=list)
    batch_bytes: int = 0
    rejected_count: int = 0
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)
    deadline: float = field(init=False)

    def __post_init__(self) -> None:
        utc_iso(self.captured_at)
        self.batch_started = self.monotonic()
        self.deadline = self.batch_started + 60

    def check(self) -> None:
        # Observe busy only until the first overlap: it costs store queries.
        # capture_source sets received_bytes on every failure it raises.
        if self.busy_observed_at is None and self.observe_busy():
            self.busy_observed_at = self.monotonic()
        if self.cancelled():
            raise SourceFailure("cancelled")
        if self.monotonic() >= self.deadline:
            raise SourceFailure("deadline")

    def add_bytes(self, count: int) -> None:
        self.check()
        self.received_bytes += count
        if self.received_bytes > 100 * 1024 * 1024:
            raise SourceFailure("size_limit")

    def add_record(self) -> None:
        self.check()
        self.record_count += 1
        if self.record_count > 1000:
            raise SourceFailure("record_limit")

    def finish_record(self) -> None:
        self.batch_records += 1
        if self.batch_records == 25:
            self.finish_batch()

    def finish_batch(self) -> None:
        if self.batch_records:
            now = self.monotonic()
            duration = max(0, (now - self.batch_started) * 1000)
            self.batch_durations_ms.append(duration)
            self.batch_observations.append(
                dict(
                    timestamp=utc_iso(self.utc_now()),
                    accepted_count=self.batch_records,
                    rejected_count=0,
                    received_bytes=self.received_bytes - self.batch_bytes,
                    duration_ms=duration,
                )
            )
            self.batch_bytes = self.received_bytes
            self.batch_started = now
            self.batch_records = 0


def source_config_digest(source: SourceConfig) -> str:
    # A path/reference can itself contain a credential; evidence records only a
    # redacted config identity rather than exporting the raw configuration.
    return canonical_digest(
        {
            "source_id": source.source_id,
            "kind": source.kind,
            "path_digest": canonical_digest(str(source.path)),
            "adapter_contract": source.adapter_contract,
            "secret_ref": "[redacted]" if source.secret_ref else None,
        }
    )


class _Stage:
    def __init__(self, root: Path, context: CaptureContext) -> None:
        self.root = root
        self.context = context
        self.tree = open_source_tree(root, control=context)
        self.observed: dict[str, tuple[int, int, int, int, int]] = {}
        self.files: list[StagedFile] = []
        # Physical identity -> (fingerprint, digest, retained bytes), hashed once.
        self.physical: dict[
            tuple[int, int], tuple[tuple[int, int, int, int, int], str, bytes | None]
        ] = {}

    def close(self) -> None:
        self.tree.close()

    def exists(self, relative: str) -> bool:
        try:
            self.tree.info(relative)
            return True
        except FileNotFoundError:
            return False

    def info(self, relative: str) -> SourceInfo:
        return self.tree.info(relative)

    def read(
        self,
        relative: str,
        limit: int,
        *,
        retain: bool = True,
        forbidden: tuple[int, int] | None = None,
    ) -> tuple[bytes | None, str]:
        self.context.check()
        source_file = self.tree.open_file(relative)
        try:
            initial = source_file.info()
            if not initial.is_regular or initial.is_reparse:
                raise SourceFailure("invalid_data")
            key = initial.identity
            if key == forbidden:
                raise SourceFailure("invalid_data")
            if initial.size > limit:
                raise SourceFailure("size_limit")
            if key in self.physical:
                fingerprint, digest, data = self.physical[key]
                if fingerprint != initial.fingerprint:
                    raise SourceFailure("source_unavailable")
                if relative not in self.observed:
                    self.files.append(StagedFile(relative, digest, initial.size))
                self.observed[relative] = initial.fingerprint
                return data, digest
            chunks = []
            digest = hashlib.sha256()
            total = 0
            while True:
                self.context.check()
                chunk = source_file.read(min(_CHUNK, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                self.context.add_bytes(len(chunk))
                if total > limit:
                    raise SourceFailure("size_limit")
                digest.update(chunk)
                if retain:
                    chunks.append(chunk)
            final = source_file.info()
            current = self.tree.info(relative)
            if initial.fingerprint != final.fingerprint or final.fingerprint != current.fingerprint:
                raise SourceFailure("source_unavailable")
            self.observed[relative] = final.fingerprint
            data = b"".join(chunks) if retain else None
            result = digest.hexdigest()
            self.physical[key] = (final.fingerprint, result, data)
            self.files.append(StagedFile(relative, result, total))
            return data, result
        finally:
            source_file.close()

    def verify(self) -> None:
        self.context.check()
        self.tree.verify_root()
        for relative, expected in self.observed.items():
            self.context.check()
            info = self.tree.info(relative)
            if not info.is_regular or info.is_reparse or info.fingerprint != expected:
                raise SourceFailure("source_unavailable")


def _json(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise SourceFailure("invalid_data")
            result[key] = value
        return result

    def invalid_constant(_: str) -> None:
        raise SourceFailure("invalid_data")

    def finite(literal: str) -> float:
        # 1e999 parses to infinity without reaching parse_constant.
        number = float(literal)
        if not math.isfinite(number):
            raise SourceFailure("invalid_data")
        return number

    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=pairs,
        parse_constant=invalid_constant,
        parse_float=finite,
    )
    if not isinstance(value, dict):
        raise SourceFailure("invalid_data")
    return value


class _DataLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _DataLoader, node: yaml.MappingNode) -> dict[str, Any]:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise SourceFailure("invalid_data")
        result[key] = loader.construct_object(value_node)
    return result


_DataLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _frontmatter(raw: bytes) -> tuple[dict[str, Any], str]:
    decoded = raw.decode("utf-8")
    lines = decoded.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise SourceFailure("invalid_data")
    closing = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---"), None)
    if closing is None:
        raise SourceFailure("invalid_data")
    header, body = "".join(lines[1:closing]), "".join(lines[closing + 1 :])
    if len(header.encode("utf-8")) > 4 * _CHUNK or len(body.encode("utf-8")) > 256 * 1024:
        raise SourceFailure("size_limit")
    if not body.strip():
        raise SourceFailure("invalid_data")
    if any(isinstance(token, AliasToken | AnchorToken | TagToken) for token in yaml.scan(header)):
        raise SourceFailure("invalid_data")
    node = yaml.compose(header, Loader=_DataLoader)
    if node is None:
        raise SourceFailure("invalid_data")

    def depth(current: yaml.Node | None, level: int = 0) -> None:
        if level > 16:
            raise SourceFailure("invalid_data")
        if isinstance(current, yaml.MappingNode):
            for key, value in current.value:
                depth(key, level + 1)
                depth(value, level + 1)
        elif isinstance(current, yaml.SequenceNode):
            for child in current.value:
                depth(child, level + 1)

    depth(node)
    # Build the value from the composed node instead of parsing the header again.
    value = _DataLoader("").construct_document(node)
    if not isinstance(value, dict):
        raise SourceFailure("invalid_data")
    return value, body


def _relative_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or re.match(r"^[A-Za-z]:", value)
        or value.startswith("/")
        or any(p in ("", ".", "..") for p in value.split("/"))
    ):
        raise SourceFailure("invalid_data")
    text(value, 2048)
    return value


def _check_owner(asset_id: str, previous: SourceInventory, source_id: str) -> None:
    owner = previous.owners.get(asset_id)
    if owner is not None and owner != source_id:
        raise SourceFailure("invalid_data")


def _local(
    stage: _Stage, source: SourceConfig, previous: SourceInventory, context: CaptureContext
) -> SourceCapture:
    candidates: list[str] = []
    if stage.exists("SKILL.md"):
        candidates.append(".")
    else:
        entries = []
        for name in stage.tree.names():
            context.check()
            if len(entries) >= 1000:
                raise SourceFailure("record_limit")
            entries.append(name)
        for name in sorted(entries, key=lambda n: n.encode("utf-8")):
            info = stage.info(name)
            if info.is_reparse:
                raise SourceFailure("invalid_data")
            if info.is_directory and stage.exists(f"{name}/SKILL.md"):
                candidates.append(name)
    documents, paths = [], {}
    for package in candidates:
        context.add_record()
        relative = "SKILL.md" if package == "." else f"{package}/SKILL.md"
        original, digest = stage.read(relative, 10 * _CHUNK)
        metadata, body = _frontmatter(original)
        extra = metadata.get("metadata", {})
        if not isinstance(extra, dict):
            raise SourceFailure("invalid_data")
        manifest = "skill.manifest.json" if package == "." else f"{package}/skill.manifest.json"
        fallback = {}
        if stage.exists(manifest):
            raw, _ = stage.read(manifest, 4 * _CHUNK)
            fallback = _json(raw)
            # Validate recognized values even when frontmatter wins.
            if "version" in fallback:
                text(fallback["version"], 256)
            if "languages" in fallback:
                check_language_tags(fallback["languages"])
        if "version" in extra:
            text(extra["version"], 256)
        version = extra.get("version", fallback.get("version"))
        languages = extra.get("languages", fallback.get("languages", []))
        check_language_tags(languages)
        dependencies = extra.get("dependencies", [])
        asset_id = "skill:" + canonical_digest(
            {"source_id": source.source_id, "relative_package_path": package}
        )
        value = {
            "schema_version": 1,
            "asset_id": asset_id,
            "kind": "skill",
            "source_id": source.source_id,
            "source_locator": str(stage.root.absolute() / relative),
            "source_version": version,
            "captured_at": utc_iso(context.captured_at),
            "content_hash": digest,
            "description": metadata.get("description"),
            "language_tags": languages,
            "dependencies": dependencies,
            "availability": "unknown",
            "stale": False,
            "identity": {"package_id": metadata.get("name"), "package_version": version},
        }
        secrets = (
            context.secrets
            | context.redactor.secret_values(metadata)
            | context.redactor.secret_values(fallback)
        )
        record = validate_asset_record(value)
        documents.append(
            project_asset(record, body=body, redactor=context.redactor, secrets=secrets)
        )
        paths[asset_id] = package
        _check_owner(asset_id, previous, source.source_id)
        context.finish_record()
    context.finish_batch()
    absent = set()
    for item in previous.records:
        if item.record.asset_id in paths:
            continue
        package = item.relative_package_path
        if package is None:
            raise SourceFailure("invalid_data")
        location = "SKILL.md" if package == "." else f"{package}/SKILL.md"
        if stage.exists(location):
            raise SourceFailure("invalid_data")
        absent.add(item.record.asset_id)
    stage.verify()
    entries = sorted(
        ({"relative_path": f.relative_path, "sha256": f.sha256} for f in stage.files),
        key=lambda f: f["relative_path"].encode("utf-8"),
    )
    return SourceCapture(
        source.source_id,
        source_config_digest(source),
        canonical_digest(entries),
        None,
        utc_iso(context.captured_at),
        tuple(documents),
        tuple(stage.files),
        True,
        frozenset(absent),
        freeze_json(paths),
        context.received_bytes,
        tuple(context.batch_durations_ms),
    )


def _import(
    stage: _Stage, source: SourceConfig, previous: SourceInventory, context: CaptureContext
) -> SourceCapture:
    raw, digest = stage.read(source.path.name, 4 * _CHUNK)
    data = validate_document(_json(raw), _IMPORT_VALIDATOR)
    if data["source_id"] != source.source_id:
        raise SourceFailure("invalid_data")
    if len(data["records"]) + len(data["removed_asset_ids"]) > 1000:
        raise SourceFailure("record_limit")
    timestamp(data["captured_at"])
    if data["source_version"] is not None:
        text(data["source_version"], 256)
    forbidden = stage.observed[source.path.name][:2]
    removed = frozenset(data["removed_asset_ids"])
    if removed and not data["complete"]:
        raise SourceFailure("invalid_data")
    records_by_id = {item.record.asset_id for item in previous.records}
    known = records_by_id | previous.tombstones
    for asset_id in removed:
        _check_owner(asset_id, previous, source.source_id)
        if asset_id not in known:
            raise SourceFailure("invalid_data")
    documents, ids = [], set()
    for entry in data["records"]:
        context.add_record()
        value = entry["record"]
        asset_id = value["asset_id"]
        if asset_id in ids or asset_id in removed:
            raise SourceFailure("invalid_data")
        ids.add(asset_id)
        _check_owner(asset_id, previous, source.source_id)
        relative = _relative_path(entry["original_path"])
        _, original_digest = stage.read(relative, 10 * _CHUNK, retain=False, forbidden=forbidden)
        if original_digest != value["content_hash"]:
            raise SourceFailure("invalid_data")
        record = validate_asset_record(
            {
                **value,
                "schema_version": 1,
                "source_id": source.source_id,
                "availability": "unknown",
                "stale": False,
            }
        )
        documents.append(
            project_asset(record, body=None, redactor=context.redactor, secrets=context.secrets)
        )
        context.finish_record()
    context.finish_batch()
    if data["complete"] and records_by_id - ids - removed:
        raise SourceFailure("invalid_data")
    stage.verify()
    return SourceCapture(
        source.source_id,
        source_config_digest(source),
        digest,
        data["source_version"],
        data["captured_at"],
        tuple(documents),
        tuple(stage.files),
        data["complete"],
        removed,
        freeze_json({}),
        context.received_bytes,
        batch_durations_ms=tuple(context.batch_durations_ms),
    )


def capture_source(
    source: SourceConfig, *, previous: SourceInventory, context: CaptureContext
) -> SourceCapture:
    stage = None
    try:
        context.check()
        if source.path is None or source.kind not in ("local_directory", "metadata_import"):
            raise SourceFailure("source_unavailable")
        root = source.path if source.kind == "local_directory" else source.path.parent
        stage = _Stage(root, context)
        capture = (_local if source.kind == "local_directory" else _import)(
            stage, source, previous, context
        )
        context.check()
        return capture
    except NativeIoAborted as exc:
        raise SourceFailure(exc.reason, received_bytes=context.received_bytes) from None
    except SourceFailure as exc:
        exc.received_bytes = context.received_bytes
        raise
    except AssetValidationError as exc:
        reason = "size_limit" if str(exc) == "size_limit" else "invalid_data"
        raise SourceFailure(reason, received_bytes=context.received_bytes) from None
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        yaml.YAMLError,
        RecursionError,
    ) as exc:
        reason = (
            "source_unavailable"
            if isinstance(exc, OSError) and exc.errno not in (errno.ELOOP, errno.ENOTDIR)
            else "invalid_data"
        )
        raise SourceFailure(reason, received_bytes=context.received_bytes) from None
    finally:
        if stage is not None:
            stage.close()
