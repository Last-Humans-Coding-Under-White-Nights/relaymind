"""The frozen acceptance MCP tool capture stays intact (#261; RM40; AC31; spec 0.66 12.6.6).

Section 12.6.6: each genuine MCP tool's original listing is the locally
running server's ``tools/list`` response, frozen by the original-byte SHA-256
of that capture plus its canonical ``schema_hash``, with origin
``genuine_mcp``. A changed source needs a new release and review, never a
silent substitution. These checks recompute, from the committed bytes:

- the lock: the canonical digest of ``capture.v1.json`` and the SHA-256 and
  length of every locked file, with no unlocked file in the frozen folders;
- each server's original digests and each tool's ``original_sha256`` and
  ``schema_hash``, the tool set of every listing (one page, no
  ``nextCursor``), and the product's tool-schema check;
- the selection: SHA-256 of ``<server_id>/<tool_name>`` ascending, first 100,
  the rest excluded as ``beyond_quota``.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from relaymind.canonical import canonical_digest
from relaymind.tools.registry import check_parameters_schema

CAPTURE = Path(__file__).resolve().parents[1] / "evaluation" / "assets" / "acceptance-mcp-v1"
QUOTA = 100
# Files outside the lock: the lock itself, the locked document and the notes.
UNLOCKED = {"lock.v1.json", "capture.v1.json", "README.md"}


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def capture_problems(root: Path) -> list[str]:
    """Every way the folder differs from its lock and its own rules; empty when intact."""
    problems = []
    lock = json.loads((root / "lock.v1.json").read_bytes())
    document = json.loads((root / lock["document"]["path"]).read_bytes())
    if canonical_digest(document) != lock["document"]["digest"]:
        problems.append("document_digest")
    locked = {entry["path"]: entry for entry in lock["files"]}
    present = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} - UNLOCKED
    if present != set(locked):
        problems.append(f"lock_membership:{sorted(present ^ set(locked))}")
    for path, entry in sorted(locked.items()):
        payload = (root / path).read_bytes() if (root / path).is_file() else None
        if payload is None or (_sha(payload), len(payload)) != (entry["sha256"], entry["bytes"]):
            problems.append(f"file_digest:{path}")
    tool_script = (root / document["capture_tool"]["path"]).read_bytes()
    if _sha(tool_script) != document["capture_tool"]["sha256"]:
        problems.append("capture_tool_digest")

    listed = {}
    for server in document["servers"]:
        license_bytes = (root / server["license"]["path"]).read_bytes()
        if (_sha(license_bytes), len(license_bytes)) != (
            server["license"]["sha256"],
            server["license"]["bytes"],
        ):
            problems.append(f"license_digest:{server['id']}")
        for original in server["originals"]:
            payload = (root / original["path"]).read_bytes()
            if (_sha(payload), len(payload)) != (original["sha256"], original["bytes"]):
                problems.append(f"original_digest:{original['path']}")
            if original["kind"] != "tools_list":
                continue
            result = json.loads(payload)["result"]
            if "nextCursor" in result:
                problems.append(f"next_cursor:{original['path']}")
            for tool in result["tools"]:
                listed[(server["id"], tool["name"])] = (original["path"], _sha(payload), tool)

    recorded = {(t["server_id"], t["name"]): t for t in document["tools"]}
    if set(recorded) != set(listed):
        problems.append("tool_membership")
    for key, tool in sorted(recorded.items()):
        if key not in listed:
            continue
        path, digest, original = listed[key]
        schema = original.get("inputSchema")
        if (tool["original"], tool["original_sha256"]) != (path, digest):
            problems.append(f"tool_original:{key[0]}/{key[1]}")
        if not isinstance(schema, dict) or tool["schema_hash"] != canonical_digest(schema):
            problems.append(f"schema_hash:{key[0]}/{key[1]}")
        if tool["origin"] != "genuine_mcp":
            problems.append(f"origin:{key[0]}/{key[1]}")
        try:
            check_parameters_schema(schema)
            admitted = True
        except Exception:
            admitted = False
        if admitted is not tool["product_schema_check"]:
            problems.append(f"product_schema_check:{key[0]}/{key[1]}")

    names = [f"{server}/{name}" for server, name in recorded]
    ordered = sorted(names, key=lambda name: _sha(name.encode("utf-8")))
    selection = document["selection"]
    if selection["quota"] != QUOTA or sorted(ordered[:QUOTA]) != selection["selected"]:
        problems.append("selection")
    excluded = [{"tool": name, "reason": "beyond_quota"} for name in sorted(ordered[QUOTA:])]
    if selection["excluded"] != excluded:
        problems.append("excluded")
    if not all(
        recorded[tuple(name.split("/", 1))]["product_schema_check"] for name in ordered[:QUOTA]
    ):
        problems.append("selected_not_admitted")
    return problems


def test_the_committed_capture_is_intact():
    assert capture_problems(CAPTURE) == []


def test_the_capture_holds_exactly_100_selected_genuine_tools():
    document = json.loads((CAPTURE / "capture.v1.json").read_bytes())
    assert len(document["selection"]["selected"]) == QUOTA
    assert len(document["tools"]) == QUOTA + len(document["selection"]["excluded"])
    assert {t["origin"] for t in document["tools"]} == {"genuine_mcp"}


@pytest.fixture
def copy(tmp_path):
    target = tmp_path / "capture"
    shutil.copytree(CAPTURE, target)
    return target


def _rewrite_json(path: Path, change) -> None:
    document = json.loads(path.read_bytes())
    change(document)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_a_changed_original_byte_is_found(copy):
    original = sorted((copy / "originals").glob("*.tools-list.0.json"))[0]
    original.write_bytes(original.read_bytes() + b" ")  # Still valid JSON.
    problems = capture_problems(copy)
    assert f"file_digest:originals/{original.name}" in problems
    assert f"original_digest:originals/{original.name}" in problems


def test_a_changed_schema_hash_is_found(copy):
    def change(document):
        document["tools"][0]["schema_hash"] = "0" * 64

    _rewrite_json(copy / "capture.v1.json", change)
    problems = capture_problems(copy)
    assert "document_digest" in problems
    assert any(p.startswith("schema_hash:") for p in problems)


def test_a_document_edit_without_a_new_lock_is_found(copy):
    def change(document):
        document["captured_at"] = "2026-11-02T00:00:00Z"

    _rewrite_json(copy / "capture.v1.json", change)
    assert capture_problems(copy) == ["document_digest"]


def test_a_changed_selection_is_found(copy):
    def change(document):
        selection = document["selection"]
        swapped = selection["excluded"].pop(0)["tool"]
        dropped = selection["selected"].pop(0)
        selection["selected"] = sorted([*selection["selected"], swapped])
        selection["excluded"] = sorted(
            [*selection["excluded"], {"tool": dropped, "reason": "beyond_quota"}],
            key=lambda e: e["tool"],
        )

    _rewrite_json(copy / "capture.v1.json", change)
    problems = capture_problems(copy)
    assert {"selection", "excluded"} <= set(problems)


def test_an_unlocked_file_in_the_frozen_folders_is_found(copy):
    (copy / "originals" / "extra.tools-list.0.json").write_text("{}\n")
    assert any(p.startswith("lock_membership:") for p in capture_problems(copy))


def test_a_changed_capture_script_is_found(copy):
    script = copy / "freeze_mcp_capture.py"
    script.write_bytes(script.read_bytes() + b"\n")
    assert "file_digest:freeze_mcp_capture.py" in capture_problems(copy)


def _replay_inputs(root: Path, capture_dir: Path) -> Path:
    """A capture directory and servers.json as capture_mcp_tools.py writes them.

    Rebuilt from the committed originals and licenses; launch arguments are
    the recorded placeholders, which the freeze keeps unchanged.
    """
    document = json.loads((CAPTURE / "capture.v1.json").read_bytes())
    for name in ("originals", "licenses"):
        shutil.copytree(CAPTURE / name, capture_dir / name)
    tools = {}
    for tool in document["tools"]:
        tools.setdefault(tool["server_id"], []).append(
            dict(
                name=tool["name"],
                original=tool["original"],
                schema_hash=tool["schema_hash"],
                product_schema_check=tool["product_schema_check"],
                product_schema_refusal=None,
            )
        )
    raw = [
        dict(
            id=s["id"],
            package=s["package"],
            source_url=s["source_url"],
            license=s["license"],
            server_info=s["server_info"],
            protocol_version=s["protocol_version"],
            originals=s["originals"],
            tools=tools[s["id"]],
        )
        for s in document["servers"]
    ]
    (capture_dir / "capture.raw.json").write_text(json.dumps(raw), encoding="utf-8")
    servers = [
        dict(
            id=s["id"],
            package=s["package"],
            source_url=s["source_url"],
            license_spdx=s["license"]["spdx"],
            license_file=f"<license>/{s['license']['evidence']}",
            argv=s["launch"],
        )
        for s in document["servers"]
    ]
    servers_json = root / "servers.json"
    servers_json.write_text(json.dumps(servers), encoding="utf-8")
    return servers_json


def test_the_documented_freeze_reproduces_the_committed_capture_and_lock(tmp_path):
    """README "Reproduce": copy both scripts into OUT_DIR, then freeze (#261; AC31)."""
    capture_dir, out = tmp_path / "capture", tmp_path / "out"
    capture_dir.mkdir()
    servers_json = _replay_inputs(tmp_path, capture_dir)
    out.mkdir()
    for name in ("capture_mcp_tools.py", "freeze_mcp_capture.py"):
        shutil.copyfile(CAPTURE / name, out / name)
    document = json.loads((CAPTURE / "capture.v1.json").read_bytes())
    env = dict(os.environ, PYTHONPATH=str(CAPTURE.parents[2] / "src"))
    result = subprocess.run(
        [
            sys.executable,
            str(out / "freeze_mcp_capture.py"),
            str(capture_dir),
            str(servers_json),
            str(out),
            document["captured_at"],
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    for name in ("capture.v1.json", "lock.v1.json"):
        written = (out / name).read_bytes()
        if os.name == "nt":  # write_text uses CRLF there; the capture tool is POSIX-only.
            written = written.replace(b"\r\n", b"\n")
        assert written == (CAPTURE / name).read_bytes(), name
    assert "copy both scripts into OUT_DIR" in (CAPTURE / "README.md").read_text(encoding="utf-8")
