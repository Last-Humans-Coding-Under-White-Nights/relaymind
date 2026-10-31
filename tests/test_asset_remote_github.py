"""RM34/RM38/RM43 AC25/AC34: complete immutable Git generations and owned removals."""

import copy
import hashlib
import json
from dataclasses import replace

import httpx
import pytest

from helpers import FakeClock
from relaymind.assets.catalog import InventoryAsset, SourceInventory
from relaymind.assets.settings import SourceConfig
from relaymind.assets.sources import CaptureContext, SourceFailure
from test_asset_remote_contracts import PROFILE


def generation(version="1", names=("a", "b")):
    commit = version * 40
    root = "e" * 40
    tree = "f" * 40
    files = {}
    entries = []

    def entry(path, raw):
        return dict(
            path=path,
            mode="100644",
            type="blob",
            size=len(raw),
            sha=hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
        )

    for name in names:
        original = (
            f"---\nname: {name}\ndescription: {name} public version {version}\n"
            "---\n\nUse actual returned data.\n"
        ).encode()
        manifest = b'{"version":"0.2.0","languages":["en"]}'
        entries += [
            dict(path=name, mode="040000", type="tree", sha="a" * 40),
            entry(name + "/SKILL.md", original),
            entry(name + "/skill.manifest.json", manifest),
        ]
        files["skills/" + name + "/SKILL.md"] = original
        files["skills/" + name + "/skill.manifest.json"] = manifest
    from pathlib import Path

    license = (Path(__file__).resolve().parents[1] / "LICENSE").read_bytes()
    files["LICENSE"] = license
    docs = dict(
        ref=dict(ref="refs/heads/master", object=dict(type="commit", sha=commit)),
        commit=dict(sha=commit, tree=dict(sha=root)),
        root=dict(
            sha=root,
            truncated=False,
            tree=[
                dict(path="skills", type="tree", mode="040000", sha=tree),
                entry("LICENSE", license),
            ],
        ),
        skills=dict(sha=tree, truncated=False, tree=entries),
    )
    return docs, files


def http(docs, files, *, status=None):
    commit = docs["ref"]["object"]["sha"]
    root = docs["commit"]["tree"]["sha"]
    tree = next((e["sha"] for e in docs["root"]["tree"] if e["path"] == "skills"), None)

    def handler(request):
        path = request.url.path
        if status is not None and status in path:
            return httpx.Response(500)
        if path.endswith("/git/ref/heads/master"):
            data = docs["ref"]
        elif path.endswith("/git/commits/" + commit):
            data = docs["commit"]
        elif path.endswith("/git/trees/" + root):
            data = docs["root"]
        elif tree is not None and path.endswith("/git/trees/" + tree):
            data = docs["skills"]
        elif "/" + commit + "/" in path:
            key = path.split("/" + commit + "/", 1)[1]
            if key not in files:
                return httpx.Response(404)
            return httpx.Response(200, stream=httpx.ByteStream(files[key]))
        else:
            return httpx.Response(404)
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(data).encode()))

    return httpx.MockTransport(handler)


def captured(docs, files, previous=None, *, context=None, status=None):
    from relaymind.assets.remote_contracts import resolve_remote_profile
    from relaymind.assets.remote_github import capture_github_skills
    from relaymind.assets.remote_transport import RemoteTransport

    clock = FakeClock()
    ctx = context or CaptureContext(
        clock.utc_now(), monotonic=clock.monotonic, utc_now=clock.utc_now
    )
    source = SourceConfig("public", "qualified_remote", None, PROFILE)
    with RemoteTransport(
        resolve_remote_profile(PROFILE), http_transport=http(docs, files, status=status)
    ) as transport:
        return capture_github_skills(
            source,
            profile=resolve_remote_profile(PROFILE),
            previous=previous or SourceInventory.empty("public"),
            context=ctx,
            transport=transport,
        )


def inventory(capture):
    return SourceInventory(
        "public",
        1,
        tuple(
            InventoryAsset(d.record, capture.package_paths[d.record.asset_id])
            for d in capture.documents
        ),
        owners={d.record.asset_id: "public" for d in capture.documents},
    )


def test_complete_generations_update_add_and_explicitly_remove_only_owned_assets():
    docs, files = generation()
    first = captured(docs, files)
    docs2, files2 = generation("2", ("a", "c"))
    second = captured(docs2, files2, inventory(first))
    a, b = first.documents
    assert first.complete and second.complete
    assert second.documents[0].record.asset_id == a.record.asset_id
    assert second.removed_asset_ids == {b.record.asset_id}
    assert (
        second.documents[0].record.content_hash
        == hashlib.sha256(files2["skills/a/SKILL.md"]).hexdigest()
    )
    assert second.documents[0].record.content_hash != a.record.content_hash
    assert second.source_version == "2" * 40
    assert all(
        d.record.source_version == "2" * 40
        and d.record.identity["package_version"] == "0.2.0"
        and d.record.availability == "unknown"
        for d in second.documents
    )
    assert all("/" + "2" * 40 + "/skills/" in d.record.source_locator for d in second.documents)


def test_complete_missing_scope_is_empty_but_promised_failed_tree_is_not():
    docs, files = generation()
    previous = inventory(captured(docs, files))
    empty = copy.deepcopy(docs)
    empty["root"]["tree"] = [e for e in empty["root"]["tree"] if e["path"] != "skills"]
    result = captured(empty, files, previous)
    assert result.complete and result.documents == () and len(result.removed_asset_ids) == 2
    with pytest.raises(SourceFailure):
        captured(docs, files, previous, status="/git/trees/" + "f" * 40)


@pytest.mark.parametrize(
    "change",
    [
        "truncated",
        "missing_false",
        "tree_id",
        "commit_id",
        "blob",
        "symlink",
        "submodule",
        "duplicate",
        "case",
        "traversal",
        "rights",
        "rights_bytes",
        "invalid_skill",
        "nonfinite",
        "duplicate_json",
    ],
)
def test_bad_final_stage_cannot_authorize_removal(change):
    docs, files = generation()
    previous = inventory(captured(docs, files))
    if change == "truncated":
        docs["skills"]["truncated"] = True
    elif change == "missing_false":
        docs["root"].pop("truncated")
    elif change == "tree_id":
        docs["skills"]["sha"] = "d" * 40
    elif change == "commit_id":
        docs["commit"]["sha"] = "d" * 40
    elif change == "blob":
        files["skills/b/SKILL.md"] += b"changed"
    elif change == "symlink":
        docs["skills"]["tree"][1]["mode"] = "120000"
    elif change == "submodule":
        docs["skills"]["tree"][0].update(mode="160000", type="commit")
    elif change == "duplicate":
        docs["skills"]["tree"].append(docs["skills"]["tree"][1])
    elif change == "case":
        docs["skills"]["tree"].append(dict(docs["skills"]["tree"][1], path="A/skill.md"))
    elif change == "traversal":
        docs["skills"]["tree"][1]["path"] = "../SKILL.md"
    elif change == "rights":
        docs["skills"]["tree"].append(dict(docs["skills"]["tree"][1], path="a/NOTICE"))
    elif change == "rights_bytes":
        files["LICENSE"] = b"different rights"
    else:
        raw = {
            "invalid_skill": b"not frontmatter",
            "nonfinite": b'{"version":NaN}',
            "duplicate_json": b'{"version":"1","version":"2"}',
        }[change]
        path = "skills/a/SKILL.md" if change == "invalid_skill" else "skills/a/skill.manifest.json"
        files[path] = raw
        entry = next(e for e in docs["skills"]["tree"] if "skills/" + e["path"] == path)
        entry.update(
            size=len(raw),
            sha=hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
        )
    with pytest.raises(SourceFailure):
        captured(docs, files, previous)


def test_other_source_inventory_cannot_be_used_for_removal_proof():
    docs, files = generation()
    previous = inventory(captured(docs, files))
    foreign = replace(previous, source_id="other")
    with pytest.raises(SourceFailure):
        captured(docs, files, foreign)


@pytest.mark.parametrize(
    "secret,echo", [("PRIVATE-token", "PRIVATE-token"), ("é-token", "e\u0301-token")]
)
def test_remote_echoed_credential_is_scrubbed_literally_in_projection_and_body(secret, echo):
    """RM12/RM43: an embedded literal echo cannot evade free-text token boundaries."""
    docs, files = generation(names=("a",))
    raw = (
        files["skills/a/SKILL.md"]
        .replace(b"a public version 1", ("prefix" + echo + "suffix").encode())
        .replace(b"Use actual returned data.", ("Return x" + echo + "y safely.").encode())
    )
    files["skills/a/SKILL.md"] = raw
    item = next(e for e in docs["skills"]["tree"] if e["path"] == "a/SKILL.md")
    item.update(
        size=len(raw), sha=hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    )
    clock = FakeClock()
    ctx = CaptureContext(clock.utc_now(), secrets=frozenset({secret}))
    capture = captured(docs, files, context=ctx)
    projected = json.dumps(capture.documents[0].to_json_object(), ensure_ascii=False)
    assert secret not in projected and echo not in projected
    assert "[redacted]" in projected
    assert capture.documents[0].record.content_hash == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "field,secret,echo",
    [
        ("package_id", "PRIVATE-token", "PRIVATE-token"),
        ("package_version", "PRIVATE-token", "PRIVATE-token"),
        ("dependencies", "PRIVATE-token", "PRIVATE-token"),
        ("package_path", "PRIVATE-token", "PRIVATE-token"),
        ("package_version", "é-token", "e\u0301-token"),
        ("dependencies", "e\u0301-token", "é-token"),
        ("package_path", "é-token", "e\u0301-token"),
    ],
)
def test_embedded_known_credential_refuses_immutable_public_fields(field, secret, echo):
    """RM12/RM43: valid immutable identities/provenance refuse literal/NFC echoes."""
    import unicodedata
    from urllib.parse import unquote

    docs, files = generation(names=("a",))
    value = "prefix" + echo + "suffix"
    if field == "package_path":
        for item in docs["skills"]["tree"]:
            item["path"] = value + item["path"][1:]
        files = {
            key.replace("skills/a/", "skills/" + value + "/"): raw for key, raw in files.items()
        }
    else:
        path = "skills/a/skill.manifest.json" if field == "package_version" else "skills/a/SKILL.md"
        raw = files[path]
        if field == "package_id":
            raw = raw.replace(b"name: a", ("name: " + value).encode())
        elif field == "package_version":
            raw = json.dumps({"version": value}).encode()
        else:
            raw = raw.replace(
                b"---\n\n", ("metadata:\n  dependencies: [" + value + "]\n---\n\n").encode()
            )
        files[path] = raw
        item = next(e for e in docs["skills"]["tree"] if "skills/" + e["path"] == path)
        item.update(
            size=len(raw),
            sha=hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
        )
    # The exact same immutable generation is admitted when it contains no known secret.
    clean = captured(docs, files)
    public = json.dumps(
        {
            "documents": [d.to_json_object() for d in clean.documents],
            "paths": dict(clean.package_paths),
        },
        ensure_ascii=False,
    )
    assert unicodedata.normalize("NFC", secret) in unicodedata.normalize("NFC", unquote(public))
    clock = FakeClock()
    context = CaptureContext(clock.utc_now(), secrets=frozenset({secret}))
    with pytest.raises(SourceFailure) as caught:
        captured(docs, files, context=context)
    assert str(caught.value) == "invalid_data"
