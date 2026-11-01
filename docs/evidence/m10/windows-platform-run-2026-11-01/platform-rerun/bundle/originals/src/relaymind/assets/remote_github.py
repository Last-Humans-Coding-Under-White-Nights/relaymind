"""One complete commit-pinned public skill generation (RM34/RM38/RM43)."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote

from relaymind.assets.catalog import (
    MAX_ASSET_BYTES,
    MAX_METADATA_BYTES,
    SourceCapture,
    SourceInventory,
    StagedFile,
)
from relaymind.assets.remote_contracts import RemoteSourceProfile
from relaymind.assets.settings import SourceConfig
from relaymind.assets.sources import (
    CaptureContext,
    SourceFailure,
    _check_owner,
    _frontmatter,
    _json,
    _relative_path,
    _skill_document,
    source_config_digest,
)
from relaymind.canonical import canonical_digest
from relaymind.schema_util import freeze_json
from relaymind.store.run_store import utc_iso

if TYPE_CHECKING:
    from relaymind.assets.remote_transport import RemoteTransport

_RIGHTS = re.compile(
    r"^(license|licence|licenses|unlicense|copying|copyright|notice|legal)(?:[.\-_].*)?$", re.I
)


def _sha(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise SourceFailure("invalid_data")
    return value


def _tree(document, expected, context):
    if document.get("sha") != expected or document.get("truncated") is not False:
        raise SourceFailure("invalid_data")
    values = document.get("tree")
    if not isinstance(values, list) or len(values) > 1000:
        raise SourceFailure("record_limit")
    entries = {}
    seen = set()
    for value in values:
        context.check()
        if not isinstance(value, dict):
            raise SourceFailure("invalid_data")
        path = _relative_path(value.get("path"))
        normalized = unicodedata.normalize("NFC", path).casefold()
        if normalized in seen:
            raise SourceFailure("invalid_data")
        seen.add(normalized)
        _sha(value.get("sha"))
        if (value.get("type"), value.get("mode")) not in (
            ("blob", "100644"),
            ("blob", "100755"),
            ("blob", "120000"),
            ("tree", "040000"),
            ("commit", "160000"),
        ):
            raise SourceFailure("invalid_data")
        entries[path] = value
    return entries


def _ordinary(entry, limit):
    if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
        raise SourceFailure("invalid_data")
    if type(entry.get("size")) is not int or not 0 <= entry["size"] <= limit:
        raise SourceFailure("size_limit")


def _reject_secret_echo(value, context):
    """Check bounded staged public fields without rewriting immutable identities."""
    secrets = {unicodedata.normalize("NFC", secret) for secret in context.secrets if secret}
    if not secrets:
        return
    pending = [value]
    while pending:
        context.check()
        item = pending.pop()
        if isinstance(item, Mapping):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list | tuple):
            pending.extend(item)
        elif isinstance(item, str):
            # Source locators percent-encode original paths; check that spelling
            # as well as the actual projected string and both NFC forms.
            spellings = (item, unquote(item)) if "%" in item else (item,)
            if any(
                secret in unicodedata.normalize("NFC", spelling)
                for spelling in spellings
                for secret in secrets
            ):
                raise SourceFailure("invalid_data")


def capture_github_skills(
    source: SourceConfig,
    *,
    profile: RemoteSourceProfile,
    previous: SourceInventory,
    context: CaptureContext,
    transport: RemoteTransport,
) -> SourceCapture:
    """No partial success, mutable-branch originals or inferred cross-source removal."""
    if previous.source_id != source.source_id or any(
        item.record.source_id != source.source_id
        or previous.owners.get(item.record.asset_id, source.source_id) != source.source_id
        for item in previous.records
    ):
        raise SourceFailure("invalid_data")
    api = "https://api.github.com/repos/" + profile.repository
    raw_origin = "https://raw.githubusercontent.com/" + profile.repository
    files = []
    identities = []

    def metadata(url, label):
        response = transport.get(url, context=context, max_bytes=MAX_METADATA_BYTES, api_auth=True)
        files.append(StagedFile(".github/" + label, response.sha256, len(response.body)))
        return _json(response.body, context)

    ref = metadata(api + "/git/ref/" + profile.ref, "ref.json")
    if ref.get("ref") != "refs/" + profile.ref or ref.get("object", {}).get("type") != "commit":
        raise SourceFailure("invalid_data")
    commit = _sha(ref["object"].get("sha"))
    document = metadata(api + "/git/commits/" + commit, "commit.json")
    if document.get("sha") != commit:
        raise SourceFailure("invalid_data")
    root_sha = _sha(document.get("tree", {}).get("sha"))
    root = _tree(metadata(api + "/git/trees/" + root_sha, "root-tree.json"), root_sha, context)
    if [p for p in root if _RIGHTS.fullmatch(p)] != ["LICENSE"]:
        raise SourceFailure("invalid_data")

    def original(path, entry, limit):
        _ordinary(entry, limit)
        response = transport.get(
            raw_origin + "/" + commit + "/" + quote(path, safe="/"),
            context=context,
            max_bytes=max(1, entry["size"]),
            api_auth=False,
        )
        raw = response.body
        git = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0")
        for start in range(0, len(raw), 1024 * 1024):
            context.check()
            git.update(raw[start : start + 1024 * 1024])
        if len(raw) != entry["size"] or git.hexdigest() != entry["sha"]:
            raise SourceFailure("invalid_data")
        files.append(StagedFile(path, response.sha256, len(raw)))
        identities.append(
            dict(path=path, git_blob_sha1=git.hexdigest(), sha256=response.sha256, bytes=len(raw))
        )
        return raw, response.sha256

    _, license_digest = original("LICENSE", root["LICENSE"], MAX_METADATA_BYTES)
    if license_digest != profile.rights_sha256:
        raise SourceFailure("invalid_data")
    tree_sha = None
    entries = {}
    if "skills" in root:
        if (root["skills"].get("type"), root["skills"].get("mode")) != ("tree", "040000"):
            raise SourceFailure("invalid_data")
        tree_sha = _sha(root["skills"]["sha"])
        entries = _tree(
            metadata(api + "/git/trees/" + tree_sha + "?recursive=1", "skills-tree.json"),
            tree_sha,
            context,
        )
    if any(_RIGHTS.fullmatch(p.split("/")[-1]) for p in entries):
        raise SourceFailure("invalid_data")
    for path, entry in entries.items():
        context.check()
        if "/" not in path and entry["mode"] in ("120000", "160000"):
            raise SourceFailure("invalid_data")
    packages = (
        ["."]
        if "SKILL.md" in entries
        else sorted(
            (p[:-9] for p in entries if p.count("/") == 1 and p.endswith("/SKILL.md")),
            key=lambda p: p.encode("utf-8"),
        )
    )
    documents = []
    paths = {}
    for package in packages:
        context.add_record()
        if package != ".":
            parent = entries.get(package, {})
            if (parent.get("type"), parent.get("mode")) != ("tree", "040000"):
                raise SourceFailure("invalid_data")
        relative = "SKILL.md" if package == "." else package + "/SKILL.md"
        raw, digest = original("skills/" + relative, entries[relative], MAX_ASSET_BYTES)
        metadata_value, body = _frontmatter(raw, context)
        manifest = "skill.manifest.json" if package == "." else package + "/skill.manifest.json"
        fallback = {}
        if manifest in entries:
            encoded, _ = original("skills/" + manifest, entries[manifest], MAX_METADATA_BYTES)
            fallback = _json(encoded, context)
        for values in (metadata_value, metadata_value.get("metadata", {}), fallback):
            if isinstance(values, dict) and any(
                values.get(k) not in (None, "MIT") for k in ("license", "licence")
            ):
                raise SourceFailure("invalid_data")
        # Credential echoes are literal source secrets, including embedded or
        # normalized forms. Keep original-byte identity while exposing only a
        # safe public description/body. The final staged-field check below
        # refuses literal/NFC echoes in immutable identity and provenance.
        metadata_value = dict(metadata_value)
        if context.secrets:
            description = metadata_value.get("description")
            if isinstance(description, str):
                if len(description.encode("utf-8")) > 8192:
                    raise SourceFailure("size_limit")
                metadata_value["description"] = unicodedata.normalize("NFC", description)
            body = unicodedata.normalize("NFC", body)
        for secret in context.secrets | {unicodedata.normalize("NFC", v) for v in context.secrets}:
            if isinstance(metadata_value.get("description"), str):
                metadata_value["description"] = metadata_value["description"].replace(
                    secret, "[redacted]"
                )
            body = body.replace(secret, "[redacted]")
        document = _skill_document(
            source,
            package,
            metadata_value,
            body,
            fallback,
            digest,
            raw_origin + "/" + commit + "/skills/" + quote(relative, safe="/"),
            context,
            source_version=commit,
        )
        _check_owner(document.record.asset_id, previous, source.source_id)
        documents.append(document)
        paths[document.record.asset_id] = package
        context.finish_record()
    context.finish_batch()
    context.check()
    removed = frozenset(
        item.record.asset_id for item in previous.records if item.record.asset_id not in paths
    )
    _reject_secret_echo(
        {
            "source_id": source.source_id,
            "source_version": commit,
            "documents": [document.to_json_object() for document in documents],
            "files": [(file.relative_path, file.sha256, file.size) for file in files],
            "package_paths": paths,
            "removed_asset_ids": tuple(removed),
        },
        context,
    )
    digest = canonical_digest(
        dict(
            profile=profile.profile_id,
            commit=commit,
            root_tree=root_sha,
            skills_tree=tree_sha,
            files=sorted(identities, key=lambda f: f["path"].encode("utf-8")),
        )
    )
    return SourceCapture(
        source.source_id,
        source_config_digest(source),
        digest,
        commit,
        utc_iso(context.captured_at),
        tuple(documents),
        tuple(files),
        True,
        removed,
        freeze_json(paths),
        context.received_bytes,
        tuple(context.batch_durations_ms),
    )
