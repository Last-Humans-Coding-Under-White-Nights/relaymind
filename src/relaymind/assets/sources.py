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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from yaml.tokens import AliasToken, AnchorToken, TagToken

from relaymind.assets.catalog import (
    JOB_SECONDS,
    MAX_ASSET_BYTES,
    MAX_JOB_RECEIVED_BYTES,
    MAX_JOB_RECORDS,
    MAX_METADATA_BYTES,
    READ_BUFFER_BYTES,
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
from relaymind.store.run_store import ForegroundWindow, utc_iso
from relaymind.tools.registry import StrictValidator

__all__ = [
    "CandidateRejected",
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
_CHUNK = READ_BUFFER_BYTES


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
        self._kept_rejections = False

    def keep_rejections(self, rejections: Sequence[Mapping[str, Any]]) -> "SourceFailure":
        """Put the scan's earlier candidate rejections before this failure, once.

        Spec 0.66 section 4.6.1: a failed scan records its rejection evidence,
        so a later scan-level failure never drops a candidate's exclusion.
        """
        if not self._kept_rejections:
            self._kept_rejections = True
            self.rejected_count += len(rejections)
            self.exclusions = (*rejections, *self.exclusions)
        return self


class CandidateRejected(SourceFailure):
    """One local candidate's own bytes failed a per-file check (spec 0.66 section 4.6.1).

    A local directory scan excludes that candidate and keeps the others; every
    other reader still treats it as a whole-source failure.
    """


# Reasons a candidate's own staged bytes can produce. Gate reasons (cancelled,
# deadline) and budget reasons from the shared job context stay scan-level.
_CANDIDATE_REASONS = frozenset({"invalid_data", "size_limit"})


def _validation_reason(exc: AssetValidationError) -> str:
    return "size_limit" if str(exc) == "size_limit" else "invalid_data"


def _candidate(function: Callable[[], Any]) -> Any:
    """Run a pure check of staged candidate bytes; content errors reject only it."""
    try:
        return function()
    except SourceFailure as exc:
        if exc.reason not in _CANDIDATE_REASONS:
            raise
        raise CandidateRejected(exc.reason) from None
    except AssetValidationError as exc:
        raise CandidateRejected(_validation_reason(exc)) from None
    except (UnicodeError, yaml.YAMLError, RecursionError, ValueError, TypeError, KeyError):
        raise CandidateRejected("invalid_data") from None


@dataclass
class CaptureContext:
    captured_at: datetime
    monotonic: Callable[[], float] = time.monotonic
    cancelled: Callable[[], bool] = lambda: False
    redactor: Redactor = field(default_factory=Redactor)
    secrets: frozenset[str] = frozenset()
    received_bytes: int = 0
    record_count: int = 0
    remote_request_count: int = 0
    remote_not_before_at: datetime | None = None
    defer_remote: Callable[[datetime], None] | None = None
    observe_busy: Callable[[], bool] = lambda: False
    observe_busy_since: Callable[[], float | None] = lambda: None
    busy_observed_at: float | None = None
    batch_durations_ms: list[float] = field(default_factory=list)
    batch_started: float = field(init=False)
    batch_records: int = 0
    batch_rejected: int = 0
    batch_observations: list[dict] = field(default_factory=list)
    batch_bytes: int = 0
    rejected_count: int = 0
    # Candidate exclusions so far; a later scan-level failure keeps them.
    rejections: list[Mapping[str, Any]] = field(default_factory=list)
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)
    deadline: float = field(init=False)
    work_gate: Callable[["CaptureContext"], None] | None = None
    async_gate: Callable[["CaptureContext"], Awaitable[None]] | None = None
    work_batch_started: float = field(init=False)
    work_batches: list[dict] = field(default_factory=list)
    busy_since: float | None = None
    drain_ms: float | None = None
    foreground_window: ForegroundWindow | None = None
    finish_cpu: Callable[["CaptureContext"], None] | None = None
    native_io_ms: list[float] = field(default_factory=list)
    cpu_resuming: bool = True
    cpu_quiescent_at: float | None = field(init=False)
    external_check: Callable[[], None] = lambda: None

    def __post_init__(self) -> None:
        utc_iso(self.captured_at)
        self.batch_started = self.monotonic()
        self.deadline = self.batch_started + JOB_SECONDS
        self.work_batch_started = self.batch_started
        self.cpu_quiescent_at = self.batch_started

    def check(self) -> None:
        if self.work_gate is not None:
            self.work_gate(self)
        else:
            self.check_limits()

    def end_cpu_window(self) -> tuple[bool, bool]:
        if self.foreground_window is None:
            return True, False
        active, onset, now = self.foreground_window.end(self.observe_busy_since())
        if active:
            self.cpu_resuming = True
            self.cpu_quiescent_at = now
        self.busy_since = onset
        if active and onset is not None:
            if self.busy_observed_at is None:
                self.busy_observed_at = now
            self.drain_ms = max(self.drain_ms or 0, max(0, now - onset) * 1000)
        return active, onset is not None

    def io(self, function: Callable, *args: Any, resume: bool = True, **kwargs: Any) -> Any:
        """Native I/O is quiescent CPU time, under the unchanged job deadline."""
        if self.finish_cpu is not None:
            self.finish_cpu(self)
        started = self.monotonic()
        try:
            result = function(*args, **kwargs)
        finally:
            self.native_io_ms.append(max(0, self.monotonic() - started) * 1000)
        if resume:
            self.check()
        return result

    def check_limits(self) -> None:
        if self.observe_busy():
            self.busy_since = self.observe_busy_since()
            if self.busy_observed_at is None:
                self.busy_observed_at = self.monotonic()
        else:
            self.busy_since = None
        if self.cancelled():
            raise SourceFailure("cancelled", rejected_count=0)
        self.external_check()
        if self.monotonic() >= self.deadline:
            raise SourceFailure("deadline")

    async def async_checkpoint(self) -> None:
        if self.async_gate is not None:
            await self.async_gate(self)
        else:
            self.check_limits()

    def add_bytes(self, count: int) -> None:
        self.check()
        self.received_bytes += count
        if self.received_bytes > MAX_JOB_RECEIVED_BYTES:
            raise SourceFailure("size_limit")

    def add_record(self) -> None:
        self.check()
        self.record_count += 1
        if self.record_count > MAX_JOB_RECORDS:
            raise SourceFailure("record_limit")

    def finish_record(self) -> None:
        self.batch_records += 1
        self._finish_full_batch()

    def reject_record(self, exclusion: Mapping[str, Any]) -> None:
        """One candidate excluded on its own (spec 0.66 section 4.6.1)."""
        self.rejected_count += 1
        self.rejections.append(exclusion)
        self.batch_rejected += 1
        self._finish_full_batch()

    def _finish_full_batch(self) -> None:
        if self.batch_records + self.batch_rejected == 25:
            self.finish_batch()

    def finish_batch(self) -> None:
        if self.batch_records or self.batch_rejected:
            now = self.monotonic()
            duration = max(0, (now - self.batch_started) * 1000)
            self.batch_durations_ms.append(duration)
            self.batch_observations.append(
                dict(
                    timestamp=utc_iso(self.utc_now()),
                    accepted_count=self.batch_records,
                    rejected_count=self.batch_rejected,
                    received_bytes=self.received_bytes - self.batch_bytes,
                    duration_ms=duration,
                )
            )
            self.batch_bytes = self.received_bytes
            self.batch_started = now
            self.batch_records = 0
            self.batch_rejected = 0


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
        self.tree = context.io(open_source_tree, root, control=context, resume=False)
        try:
            context.check()
        except BaseException:
            context.io(self.tree.close, resume=False)
            raise
        self.observed: dict[str, tuple[int, int, int, int, int]] = {}
        self.files: list[StagedFile] = []
        # Physical identity -> (fingerprint, digest, retained bytes), hashed once.
        self.physical: dict[
            tuple[int, int], tuple[tuple[int, int, int, int, int], str, bytes | None]
        ] = {}

    def close(self) -> None:
        self.context.io(self.tree.close, resume=False)

    def exists(self, relative: str) -> bool:
        try:
            self.context.io(self.tree.info, relative)
            return True
        except FileNotFoundError:
            self.context.check()
            return False

    def info(self, relative: str) -> SourceInfo:
        return self.context.io(self.tree.info, relative)

    def names(self):
        entries = self.tree.names()
        exhausted = False
        try:
            while True:
                try:
                    name = self.context.io(next, entries)
                except StopIteration:
                    exhausted = True
                    return
                yield name
        finally:
            close = getattr(entries, "close", None)
            if close is not None:
                self.context.io(close, resume=False)
            if exhausted:
                self.context.check()

    def read(
        self,
        relative: str,
        limit: int,
        *,
        retain: bool = True,
        forbidden: tuple[int, int] | None = None,
    ) -> tuple[bytes | None, str]:
        self.context.check()
        source_file = self.context.io(self.tree.open_file, relative, resume=False)
        complete = False
        try:
            self.context.check()
            initial = self.context.io(source_file.info)
            if not initial.is_regular or initial.is_reparse:
                raise SourceFailure("invalid_data")
            key = initial.identity
            if key == forbidden:
                raise SourceFailure("invalid_data")
            if initial.size > limit:
                # stage.verify() still checks a rejected file: a change to it
                # during capture fails the whole scan (spec 0.66 section 4.6.1).
                self.observed[relative] = initial.fingerprint
                raise CandidateRejected("size_limit")
            if key in self.physical:
                fingerprint, digest, data = self.physical[key]
                if fingerprint != initial.fingerprint:
                    raise SourceFailure("source_unavailable")
                if relative not in self.observed:
                    self.files.append(StagedFile(relative, digest, initial.size))
                self.observed[relative] = initial.fingerprint
                complete = True
                return data, digest
            chunks = []
            digest = hashlib.sha256()
            total = 0
            while True:
                self.context.check()
                chunk = self.context.io(source_file.read, min(_CHUNK, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                self.context.add_bytes(len(chunk))
                if total > limit:
                    # The file fit at its size check, so it grew while being
                    # read: a change during capture fails the whole scan and
                    # retains the prior generation (spec 0.66 section 4.6.1).
                    raise SourceFailure("source_unavailable")
                digest.update(chunk)
                if retain:
                    chunks.append(chunk)
            final = self.context.io(source_file.info)
            current = self.context.io(self.tree.info, relative)
            if initial.fingerprint != final.fingerprint or final.fingerprint != current.fingerprint:
                raise SourceFailure("source_unavailable")
            self.observed[relative] = final.fingerprint
            data = b"".join(chunks) if retain else None
            result = digest.hexdigest()
            self.physical[key] = (final.fingerprint, result, data)
            self.files.append(StagedFile(relative, result, total))
            complete = True
            return data, result
        finally:
            self.context.io(source_file.close, resume=False)
            if complete:
                self.context.check()

    def verify(self) -> None:
        self.context.check()
        self.context.io(self.tree.verify_root)
        for relative, expected in self.observed.items():
            self.context.check()
            info = self.context.io(self.tree.info, relative)
            if not info.is_regular or info.is_reparse or info.fingerprint != expected:
                raise SourceFailure("source_unavailable")


def _json(raw: bytes, context: CaptureContext | None = None) -> dict[str, Any]:
    check = context.check if context is not None else lambda: None

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            check()
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

    decoder = json.JSONDecoder(
        object_pairs_hook=pairs,
        parse_constant=invalid_constant,
        parse_float=finite,
    )

    def checked(scanner):
        def scan_once(string, index):
            check()
            return scanner(string, index)

        return scan_once

    parse_object, parse_array = decoder.parse_object, decoder.parse_array

    def object_value(state, strict, scan_once, *args):
        check()
        return parse_object(state, strict, checked(scan_once), *args)

    def array_value(state, scan_once):
        check()
        return parse_array(state, checked(scan_once))

    # The standard Python scanner exposes each array/object value to the same
    # gate. The C decoder otherwise traverses a whole document in one call.
    decoder.parse_object = object_value
    decoder.parse_array = array_value
    decoder.scan_once = json.scanner.py_make_scanner(decoder)
    value = decoder.decode(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise SourceFailure("invalid_data")
    return value


class _DataLoader(yaml.SafeLoader):
    """The existing safe YAML parser, with gates inside its Python CPU loops."""

    def __init__(self, stream, *, check: Callable[[], None] = lambda: None):
        self._check = check
        self._characters = 0
        self._advanced = 0
        super().__init__(stream)

    def peek(self, index=0):
        # A single scalar can consume most of the allowed header. Token/node
        # gates alone cannot interrupt its scanner character loop.
        self._characters += 1
        if self._characters >= 256:
            self._characters = 0
            self._check()
        return super().peek(index)

    def forward(self, length=1):
        # Reader.forward itself loops in Python over every consumed character;
        # a scalar's single large advance must be interruptible too.
        while length:
            step = min(length, 256)
            self._advanced += step
            if self._advanced >= 256:
                self._advanced = 0
                self._check()
            super().forward(step)
            length -= step

    def fetch_more_tokens(self):
        self._check()
        return super().fetch_more_tokens()

    def compose_node(self, parent, index):
        self._check()
        return super().compose_node(parent, index)

    def construct_object(self, node, deep=False):
        self._check()
        return super().construct_object(node, deep=deep)

    def construct_sequence(self, node, deep=False):
        self._check()
        return super().construct_sequence(node, deep=deep)


def _mapping(loader: _DataLoader, node: yaml.MappingNode) -> dict[str, Any]:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise SourceFailure("invalid_data")
        result[key] = loader.construct_object(value_node)
    return result


_DataLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _frontmatter(raw: bytes, context: CaptureContext) -> tuple[dict[str, Any], str]:
    decoded = raw.decode("utf-8")
    lines = decoded.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise SourceFailure("invalid_data")
    closing = None
    for i in range(1, len(lines)):
        context.check()
        if lines[i].rstrip("\r\n") == "---":
            closing = i
            break
    if closing is None:
        raise SourceFailure("invalid_data")
    header, body = "".join(lines[1:closing]), "".join(lines[closing + 1 :])
    if len(header.encode("utf-8")) > 4 * _CHUNK or len(body.encode("utf-8")) > 256 * 1024:
        raise SourceFailure("size_limit")
    if not body.strip():
        raise SourceFailure("invalid_data")
    scanner = _DataLoader(header, check=context.check)
    try:
        while scanner.check_token():
            context.check()
            if isinstance(scanner.get_token(), AliasToken | AnchorToken | TagToken):
                raise SourceFailure("invalid_data")
    finally:
        scanner.dispose()
    loader = _DataLoader(header, check=context.check)
    try:
        node = loader.get_single_node()
    finally:
        loader.dispose()
    if node is None:
        raise SourceFailure("invalid_data")

    def depth(current: yaml.Node | None, level: int = 0) -> None:
        context.check()
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
    constructor = _DataLoader("", check=context.check)
    try:
        value = constructor.construct_document(node)
    finally:
        constructor.dispose()
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


def _skill_asset_id(source: SourceConfig, package: str) -> str:
    return "skill:" + canonical_digest(
        {"source_id": source.source_id, "relative_package_path": package}
    )


def _skill_document(
    source, package, metadata, body, fallback, digest, locator, context, *, source_version=None
):
    """Shared ordinary-byte projection; acquisition never grants execution authority."""
    extra = metadata.get("metadata", {})
    if not isinstance(extra, dict):
        raise SourceFailure("invalid_data")
    if "version" in fallback:
        text(fallback["version"], 256)
    if "languages" in fallback:
        check_language_tags(fallback["languages"])
    if "version" in extra:
        text(extra["version"], 256)
    version = extra.get("version", fallback.get("version"))
    languages = extra.get("languages", fallback.get("languages", []))
    check_language_tags(languages)
    value = dict(
        schema_version=1,
        asset_id=_skill_asset_id(source, package),
        kind="skill",
        source_id=source.source_id,
        source_locator=locator,
        source_version=version if source_version is None else source_version,
        captured_at=utc_iso(context.captured_at),
        content_hash=digest,
        description=metadata.get("description"),
        language_tags=languages,
        dependencies=extra.get("dependencies", []),
        availability="unknown",
        stale=False,
        identity={"package_id": metadata.get("name"), "package_version": version},
    )
    secrets = (
        context.secrets
        | context.redactor.secret_values(metadata)
        | context.redactor.secret_values(fallback)
    )
    return project_asset(
        validate_asset_record(value), body=body, redactor=context.redactor, secrets=secrets
    )


def _check_regular(info: SourceInfo) -> None:
    """A symlink/reparse point or non-regular file fails the whole scan."""
    if info.is_reparse or not info.is_regular:
        raise SourceFailure("invalid_data")


def _local_candidate(
    stage: _Stage, source: SourceConfig, package: str, relative: str, context: CaptureContext
):
    """Stage and project one candidate; CandidateRejected names only its own bytes.

    Path safety of SKILL.md and the optional manifest is a scan-level check
    (spec 0.66 section 4.6.1). The manifest's runs before any content
    admission, so a malformed SKILL.md can never hide an unsafe manifest;
    ``stage.read`` refuses a symlink or non-regular SKILL.md at scan level.
    """
    manifest = "skill.manifest.json" if package == "." else f"{package}/skill.manifest.json"
    has_manifest = stage.exists(manifest)
    if has_manifest:
        _check_regular(stage.info(manifest))
    original, digest = stage.read(relative, MAX_ASSET_BYTES)
    metadata, body = _candidate(lambda: _frontmatter(original, context))
    if not isinstance(metadata.get("metadata", {}), dict):
        raise CandidateRejected("invalid_data")
    fallback = {}
    if has_manifest:
        raw, _ = stage.read(manifest, MAX_METADATA_BYTES)
        fallback = _candidate(lambda: _json(raw, context))
    return _candidate(
        lambda: _skill_document(
            source,
            package,
            metadata,
            body,
            fallback,
            digest,
            str(stage.root.absolute() / relative),
            context,
        )
    )


def _local(
    stage: _Stage, source: SourceConfig, previous: SourceInventory, context: CaptureContext
) -> SourceCapture:
    candidates: list[str] = []
    if stage.exists("SKILL.md"):
        candidates.append(".")
    else:
        entries = []
        for name in stage.names():
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
    documents, paths, exclusions = [], {}, []
    for package in candidates:
        context.add_record()
        relative = "SKILL.md" if package == "." else f"{package}/SKILL.md"
        asset_id = _skill_asset_id(source, package)
        # The derived ID can collide with another source's record; that is a
        # source-level conflict, never one candidate's rejection.
        _check_owner(asset_id, previous, source.source_id)
        try:
            document = _local_candidate(stage, source, package, relative, context)
        except CandidateRejected as exc:
            # Spec 0.66 section 4.6.1: excluded by itself with a fixed reason.
            exclusion = failure_exclusions(exc.reason, asset_id)[0]
            exclusions.append(exclusion)
            context.reject_record(exclusion)
            continue
        documents.append(document)
        paths[asset_id] = package
        context.finish_record()
    context.finish_batch()
    complete = not exclusions
    absent = set()
    # A partial generation applies no removals, so it never checks absence: a
    # retained record of a rejected or otherwise omitted package (such as a
    # child hidden by a rejected root SKILL.md) just stays and is marked stale.
    for item in previous.records if complete else ():
        context.check()
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
        complete,
        # A partial generation applies no removals; omitted records stay stale.
        frozenset(absent) if complete else frozenset(),
        freeze_json(paths),
        context.received_bytes,
        tuple(context.batch_durations_ms),
        tuple(exclusions),
    )


def _import(
    stage: _Stage, source: SourceConfig, previous: SourceInventory, context: CaptureContext
) -> SourceCapture:
    raw, digest = stage.read(source.path.name, MAX_METADATA_BYTES)
    data = _json(raw, context)
    schema = _IMPORT_VALIDATOR.schema
    properties = dict(schema["properties"])
    # Preserve every envelope constraint, then apply the exact published item
    # schemas under the same gate. One full validator traversal of 1000 records
    # would otherwise bypass capture's per-record scheduling checkpoints.
    for name in ("records", "removed_asset_ids"):
        properties[name] = {
            key: value
            for key, value in properties[name].items()
            if key not in ("items", "uniqueItems")
        }
    context.check()
    if not _IMPORT_VALIDATOR.evolve(schema=schema | {"properties": properties}).is_valid(data):
        raise SourceFailure("invalid_data")
    entry_validator = _IMPORT_VALIDATOR.evolve(schema=schema["properties"]["records"]["items"])
    removed_validator = _IMPORT_VALIDATOR.evolve(
        schema=schema["properties"]["removed_asset_ids"]["items"]
    )
    seen = set()
    for asset_id in data["removed_asset_ids"]:
        context.check()
        if not removed_validator.is_valid(asset_id) or asset_id in seen:
            raise SourceFailure("invalid_data")
        seen.add(asset_id)
    for entry in data["records"]:
        context.check()
        validate_document(entry, entry_validator)
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
        context.check()
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
        _, original_digest = stage.read(
            relative, MAX_ASSET_BYTES, retain=False, forbidden=forbidden
        )
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
    try:
        return _capture_source(source, previous=previous, context=context)
    except SourceFailure as exc:
        # A scan-level failure keeps the candidate rejections recorded before it.
        exc.keep_rejections(context.rejections)
        raise


def _capture_source(
    source: SourceConfig, *, previous: SourceInventory, context: CaptureContext
) -> SourceCapture:
    stage = None
    try:
        context.check()
        if source.kind == "qualified_remote":
            from relaymind.assets.remote_contracts import resolve_remote_profile
            from relaymind.assets.remote_github import capture_github_skills
            from relaymind.assets.remote_transport import RemoteTransport

            if source.path is not None:
                raise SourceFailure("invalid_data")
            profile = resolve_remote_profile(source.adapter_contract)
            with RemoteTransport(profile, secret_ref=source.secret_ref) as transport:
                return capture_github_skills(
                    source, profile=profile, previous=previous, context=context, transport=transport
                )
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
        raise SourceFailure(
            exc.reason,
            rejected_count=0 if exc.reason == "cancelled" else 1,
            received_bytes=context.received_bytes,
        ) from None
    except SourceFailure as exc:
        exc.received_bytes = context.received_bytes
        raise
    except AssetValidationError as exc:
        raise SourceFailure(
            _validation_reason(exc), received_bytes=context.received_bytes
        ) from None
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
