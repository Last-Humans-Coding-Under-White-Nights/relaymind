"""Bounded local embedding manifest and stable no-follow file ownership (RM38/RM41)."""

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaymind.assets.embeddings import EmbeddingError
from relaymind.assets.source_io import open_source_tree
from relaymind.canonical import canonical_digest
from relaymind.schema_util import StrictValidator, freeze_json, load_packaged_schema

_ROLES = ("weights", "tokenizer", "configuration", "license", "notice")
_VALIDATOR = StrictValidator(load_packaged_schema("relaymind.assets", "embedding_manifest.v1.json"))


def _path(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 2048
        or "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or any(p in ("", ".", "..") for p in value.split("/"))
    ):
        raise EmbeddingError("invalid_artifact_path")
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise EmbeddingError("duplicate_key")
        result[key] = value
    return result


def _check_depth(raw):
    depth, quoted, escaped = 0, False, False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            depth += 1
            if depth > 4:
                raise EmbeddingError("depth_limit")
        elif char in "}]":
            depth -= 1


def read_semantic_json(path: Path) -> dict:
    """Read one closed semantic input with the fixed 64 KiB/four-container caps."""
    tree = opened = None
    try:
        tree = open_source_tree(path.parent)
        opened = tree.open_file(path.name)
        before = opened.info()
        if not before.is_regular or before.size > 65536:
            raise EmbeddingError("semantic_size_limit")
        raw = bytearray()
        while chunk := opened.read(min(4096, 65537 - len(raw))):
            raw.extend(chunk)
            if len(raw) > 65536:
                raise EmbeddingError("semantic_size_limit")
        if opened.info().fingerprint != before.fingerprint:
            raise EmbeddingError("semantic_changed")
        tree.verify_root()
        if tree.info(path.name).fingerprint != before.fingerprint:
            raise EmbeddingError("semantic_changed")
        text = raw.decode("utf-8")
        _check_depth(text)
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=lambda _: _invalid_json())
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise EmbeddingError("invalid_semantic_file") from None
    finally:
        if opened is not None:
            opened.close()
        if tree is not None:
            tree.close()


def _invalid_json():
    raise EmbeddingError("invalid_json")


def check_strings(value, *, key=""):
    if isinstance(value, dict):
        for field, child in value.items():
            check_strings(child, key=field)
    elif isinstance(value, list):
        for child in value:
            check_strings(child, key=key)
    elif isinstance(value, str):
        limit = 2048 if key in ("path", "url", "manifest") else 256
        if len(value.encode("utf-8")) > limit:
            raise EmbeddingError("string_limit")
    elif type(value) is float and not math.isfinite(value):
        raise EmbeddingError("invalid_number")


@dataclass(frozen=True)
class EmbeddingManifest:
    root: Path
    data: object
    manifest_digest: str
    artifact_hash: str


def load_embedding_manifest(path: Path) -> EmbeddingManifest:
    raw = read_semantic_json(path)
    try:
        if not _VALIDATOR.is_valid(raw):
            raise EmbeddingError("invalid_manifest")
        check_strings(raw)
        paths, roles = set(), set()
        for entry in raw["files"]:
            path_value = _path(entry["path"])
            normalized = unicodedata.normalize("NFC", path_value)
            if normalized in paths:
                raise EmbeddingError("duplicate_artifact_path")
            paths.add(normalized)
            roles.update(entry["roles"])
            entry["roles"].sort(key=_ROLES.index)
        if not {"weights", "tokenizer", "license"} <= roles:
            raise EmbeddingError("missing_artifact_role")
        raw["files"].sort(key=lambda entry: entry["path"].encode("utf-8"))
        return EmbeddingManifest(
            path.parent, freeze_json(raw), canonical_digest(raw), canonical_digest(raw["files"])
        )
    except (UnicodeError, TypeError, KeyError, OverflowError):
        raise EmbeddingError("invalid_manifest") from None


class VerifiedEmbeddingArtifact:
    """Own verified opened objects through backend loading, without filename reopen."""

    def __init__(self, manifest: EmbeddingManifest, *, control=None):
        self.manifest = manifest
        self.control = control
        self.tree = None
        self.files = {}
        self.fingerprints = {}
        try:
            self.tree = self.io(open_source_tree, manifest.root)
            identities = set()
            for entry in manifest.data["files"]:
                name = entry["path"]
                opened = self.io(self.tree.open_file, name)
                self.files[name] = opened
                before = self.io(opened.info)
                if not before.is_regular or before.is_reparse or before.size != entry["bytes"]:
                    raise EmbeddingError("invalid_artifact_file")
                if before.identity in identities:
                    raise EmbeddingError("aliased_artifact_file")
                identities.add(before.identity)
                self.fingerprints[name] = before.fingerprint
                digest, received = hashlib.sha256(), 0
                while chunk := self.io(opened.read, 1024 * 1024):
                    received += len(chunk)
                    if received > entry["bytes"]:
                        raise EmbeddingError("artifact_changed")
                    digest.update(chunk)
                if received != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
                    raise EmbeddingError("artifact_hash_mismatch")
            self.guard()
        except BaseException as failure:
            self.close()
            if isinstance(failure, OSError):
                raise EmbeddingError("invalid_artifact_file") from None
            raise

    def guard(self) -> None:
        try:
            self.io(self.tree.verify_root)
            for name, opened in self.files.items():
                expected = self.fingerprints[name]
                if (
                    self.io(opened.info).fingerprint != expected
                    or self.io(self.tree.info, name).fingerprint != expected
                ):
                    raise EmbeddingError("artifact_changed")
        except OSError:
            raise EmbeddingError("artifact_changed") from None

    def read_bytes(self, name: str, *, limit: int) -> bytes:
        self.guard()
        if type(limit) is not int or limit <= 0 or self.fingerprints[name][2] > limit:
            raise EmbeddingError("artifact_read_limit")
        # A new verified handle is checked against the pinned object before any
        # read; this is not an unchecked filename reopen across the load boundary.
        opened = self.io(self.tree.open_file, name)
        try:
            if self.io(opened.info).fingerprint != self.fingerprints[name]:
                raise EmbeddingError("artifact_changed")
            result = bytearray()
            while chunk := self.io(opened.read, min(1024 * 1024, limit + 1 - len(result))):
                result.extend(chunk)
                if len(result) > limit:
                    raise EmbeddingError("artifact_read_limit")
            self.guard()
            if self.io(opened.info).fingerprint != self.fingerprints[name]:
                raise EmbeddingError("artifact_changed")
            return bytes(result)
        finally:
            self.io(opened.close, resume=False)

    def io(self, function, *args, resume=True):
        if self.control is None:
            return function(*args)
        return self.control.io(function, *args, resume=resume)

    def run_cpu(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Resume owned CPU after I/O and quiesce only after native work ends."""
        if self.control is None:
            return function(*args, **kwargs)
        self.control.check()
        try:
            result = function(*args, **kwargs)
            self.control.check_limits()
            return result
        finally:
            if self.control.finish_cpu is not None:
                self.control.finish_cpu(self.control)
            else:
                self.control.end_cpu_window()

    def close(self) -> None:
        for opened in self.files.values():
            self.io(opened.close, resume=False)
        self.files.clear()
        if self.tree is not None:
            self.io(self.tree.close, resume=False)
            self.tree = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def verify_embedding_files(
    manifest: EmbeddingManifest, *, control=None
) -> VerifiedEmbeddingArtifact:
    return VerifiedEmbeddingArtifact(manifest, control=control)
