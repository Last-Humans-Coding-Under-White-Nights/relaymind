"""RM34/RM38/RM43 AC25/AC29/AC34: qualification is bounded read-only evidence."""

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


def api():
    path = Path(__file__).resolve().parents[1] / "scripts/probes/qualify_github_skill_source.py"
    spec = importlib.util.spec_from_file_location("github_qualification_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    skill = (
        b"---\nname: fixture-skill\ndescription: Read public health metadata.\n"
        b"---\n\nUse only actual returned health data.\n"
    )
    license = (Path(__file__).resolve().parents[1] / "LICENSE").read_bytes()

    def blob(raw):
        return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()

    commit = "1" * 40
    root = "2" * 40
    skills = "3" * 40
    entries = [
        dict(path="example", mode="040000", type="tree", sha="4" * 40),
        dict(path="example/SKILL.md", mode="100644", type="blob", sha=blob(skill), size=len(skill)),
        dict(
            path="example/skill.manifest.json", mode="100644", type="blob", sha=blob(b"{}"), size=2
        ),
    ]
    root_entries = [
        dict(path="skills", mode="040000", type="tree", sha=skills),
        dict(path="LICENSE", mode="100644", type="blob", sha=blob(license), size=len(license)),
    ]
    docs = dict(
        ref=dict(ref="refs/heads/master", object=dict(type="commit", sha=commit)),
        commit=dict(sha=commit, tree=dict(sha=root)),
        root=dict(sha=root, tree=root_entries, truncated=False),
        skills=dict(sha=skills, tree=entries, truncated=False),
    )
    bodies = {
        "LICENSE": license,
        "skills/example/SKILL.md": skill,
        "skills/example/skill.manifest.json": b"{}",
    }
    return docs, bodies


def transport(docs, bodies, *, broken=None):
    repo = "/repos/LHCWN/relaymind"

    def handle(request):
        suffix = request.url.path.removeprefix(repo)
        if suffix == "/git/ref/heads/master":
            body = docs["ref"]
        elif suffix == "/git/commits/" + "1" * 40:
            body = docs["commit"]
        elif suffix == "/git/trees/" + "2" * 40:
            body = docs["root"]
        elif suffix == "/git/trees/" + "3" * 40:
            body = docs["skills"]
        else:
            key = request.url.path.split("/" + "1" * 40 + "/", 1)[-1]
            if broken == key:
                raise httpx.ReadError("PRIVATE transport exception", request=request)
            if key not in bodies:
                return httpx.Response(404, content=b"PRIVATE error body")
            return httpx.Response(200, stream=httpx.ByteStream(bodies[key]))
        return httpx.Response(
            200,
            stream=httpx.ByteStream(json.dumps(body).encode()),
            headers={"X-Poll-Interval": "300"},
        )

    return httpx.MockTransport(handle)


def run(tmp_path, docs, bodies, **kwargs):
    return asyncio.run(
        api().qualify_github_source(tmp_path, transport=transport(docs, bodies, **kwargs))
    )


def test_complete_immutable_scope_retains_original_git_and_sha256_identities(tmp_path):
    docs, bodies = fixture()
    result = run(tmp_path, docs, bodies)
    assert result["complete"] is True and result["candidate_count"] == 1
    selected = next(f for f in result["files"] if f["path"] == "skills/example/SKILL.md")
    assert selected["git_blob_sha1"] == docs["skills"]["tree"][1]["sha"]
    assert selected["sha256"] == hashlib.sha256(bodies["skills/example/SKILL.md"]).hexdigest()
    assert selected["bytes"] == len(bodies["skills/example/SKILL.md"])
    assert (
        result["rights"]["sha256"]
        == "a0df745636ded7b39d42761f4e5000fb0718c04e98be6b004bf00efdcd79509a"
    )
    assert result["production_activation"] is False
    assert all(o["method"] == "GET" for o in result["requests"])


@pytest.mark.parametrize(
    "change",
    [
        "truncated",
        "missing_false",
        "object_id",
        "duplicate",
        "case",
        "nfc",
        "symlink",
        "submodule",
        "traversal",
        "rights",
        "missing_rights",
        "blob",
        "size",
    ],
)
def test_partial_or_unsafe_inventory_never_qualifies_complete(tmp_path, change):
    docs, bodies = fixture()
    if change == "truncated":
        docs["skills"]["truncated"] = True
    elif change == "missing_false":
        docs["skills"].pop("truncated")
    elif change == "object_id":
        docs["skills"]["sha"] = "9" * 40
    elif change == "duplicate":
        docs["skills"]["tree"].append(dict(docs["skills"]["tree"][1]))
    elif change == "case":
        docs["skills"]["tree"].append(dict(docs["skills"]["tree"][1], path="EXAMPLE/skill.md"))
    elif change == "nfc":
        docs["skills"]["tree"] += [
            dict(docs["skills"]["tree"][0], path="é"),
            dict(docs["skills"]["tree"][0], path="e\u0301"),
        ]
    elif change == "symlink":
        docs["skills"]["tree"][1]["mode"] = "120000"
    elif change == "submodule":
        docs["skills"]["tree"][0].update(mode="160000", type="commit")
    elif change == "traversal":
        docs["skills"]["tree"][1]["path"] = "../SKILL.md"
    elif change == "rights":
        docs["skills"]["tree"].append(dict(docs["skills"]["tree"][1], path="example/NOTICE"))
    elif change == "missing_rights":
        bodies.pop("LICENSE")
    elif change == "blob":
        bodies["skills/example/SKILL.md"] += b"changed"
    elif change == "size":
        docs["skills"]["tree"][1]["size"] = 10 * 1024 * 1024 + 1
    result = run(tmp_path, docs, bodies)
    assert result["complete"] is False
    assert result["checks"]["acquisition"] == "fail"
    assert result["production_activation"] is False


def test_interrupted_original_fetch_keeps_incomplete_safe_evidence(tmp_path):
    docs, bodies = fixture()
    result = run(tmp_path, docs, bodies, broken="skills/example/SKILL.md")
    assert result["complete"] is False
    raw = (tmp_path / "observations.json").read_text()
    assert "PRIVATE" not in raw and "source_unavailable" in raw


def test_oversized_metadata_refuses_before_original_fetch(tmp_path):
    docs, bodies = fixture()
    docs["skills"]["padding"] = "x" * (4 * 1024 * 1024)
    result = run(tmp_path, docs, bodies)
    assert result["complete"] is False and result["reason"] == "size_limit"
    assert not any(f["path"].startswith("skills/") for f in result["files"])
