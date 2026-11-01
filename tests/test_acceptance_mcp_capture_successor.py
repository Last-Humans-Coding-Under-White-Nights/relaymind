"""The maintained MCP capture successor (#261; RM40; AC31; spec 0.66 section 12.6.6).

``scripts/acceptance_mcp_capture.py`` is the successor to the as-run copies in
``evaluation/assets/acceptance-mcp-v1/``, whose bytes ``lock.v1.json`` binds
and which therefore stay unchanged. The successor:

- writes every JSON file as UTF-8 with LF line endings on every platform, so
  hashes and lengths do not depend on the operating system;
- locks its own bytes, copying itself into OUT_DIR when it is missing there
  and refusing an OUT_DIR copy whose bytes differ from the running script;
- closes both the server's stdin and stdout pipes when a session ends.
"""

import _pyio
import io
import json
import os
import runpy
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from relaymind.canonical import canonical_digest

REPO = Path(__file__).resolve().parents[1]
SUCCESSOR = REPO / "scripts" / "acceptance_mcp_capture.py"
CAPTURE = REPO / "evaluation" / "assets" / "acceptance-mcp-v1"


@pytest.fixture
def successor():
    return SimpleNamespace(**runpy.run_path(str(SUCCESSOR)))


@pytest.fixture
def windows_newlines(monkeypatch):
    """Text-mode writes translate "\\n" to "\\r\\n", as Python does on Windows."""
    monkeypatch.setattr(os, "linesep", "\r\n")
    monkeypatch.setattr(io, "open", _pyio.open)
    monkeypatch.setattr("builtins.open", _pyio.open)


def _replay_inputs(root: Path) -> tuple[Path, Path]:
    """A capture directory and servers.json rebuilt from the committed capture."""
    document = json.loads((CAPTURE / "capture.v1.json").read_bytes())
    capture_dir = root / "capture"
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
    (capture_dir / "capture.raw.json").write_bytes(json.dumps(raw).encode("utf-8"))
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
    servers_json.write_bytes(json.dumps(servers).encode("utf-8"))
    return capture_dir, servers_json


def _freeze(module, root: Path, out: Path) -> int:
    capture_dir, servers_json = _replay_inputs(root)
    captured_at = json.loads((CAPTURE / "capture.v1.json").read_bytes())["captured_at"]
    return module.freeze(capture_dir, servers_json, out, captured_at)


def test_the_simulated_windows_newline_makes_the_as_run_freeze_write_crlf(
    tmp_path, windows_newlines
):
    """The simulation is effective: the bound as-run copy writes native newlines."""
    as_run = SimpleNamespace(**runpy.run_path(str(CAPTURE / "freeze_mcp_capture.py")))
    capture_dir, servers_json = _replay_inputs(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    for name in ("capture_mcp_tools.py", "freeze_mcp_capture.py"):
        shutil.copyfile(CAPTURE / name, out / name)
    captured_at = json.loads((CAPTURE / "capture.v1.json").read_bytes())["captured_at"]
    assert as_run.main(str(capture_dir), str(servers_json), str(out), captured_at) == 0
    assert b"\r\n" in (out / "lock.v1.json").read_bytes()


def test_the_successor_writes_lf_under_a_windows_newline(successor, tmp_path, windows_newlines):
    """RM40/AC31: capture.v1.json and lock.v1.json have the same bytes on every platform."""
    out = tmp_path / "out"
    assert _freeze(successor, tmp_path, out) == 0
    for name in ("capture.v1.json", "lock.v1.json"):
        assert b"\r\n" not in (out / name).read_bytes(), name


def test_the_successor_writes_the_raw_capture_with_lf(successor, tmp_path, windows_newlines):
    out = tmp_path / "raw"
    successor.write_json(out / "capture.raw.json", [{"a": 1}])
    assert (out / "capture.raw.json").read_bytes() == b'[\n  {\n    "a": 1\n  }\n]\n'


def test_the_successor_reproduces_the_committed_tool_selection(successor, tmp_path):
    """The successor freezes the same tools and selection; only its own script differs."""
    out = tmp_path / "out"
    assert _freeze(successor, tmp_path, out) == 0
    committed = json.loads((CAPTURE / "capture.v1.json").read_bytes())
    written = json.loads((out / "capture.v1.json").read_bytes())
    script = SUCCESSOR.read_bytes()
    assert written.pop("capture_tool") == dict(
        path="acceptance_mcp_capture.py", sha256=successor._sha(script)
    )
    committed.pop("capture_tool")
    assert written == committed
    lock = json.loads((out / "lock.v1.json").read_bytes())
    assert lock["document"]["digest"] == canonical_digest(
        json.loads((out / "capture.v1.json").read_bytes())
    )
    entries = {entry["path"]: entry for entry in lock["files"]}
    assert entries["acceptance_mcp_capture.py"] == dict(
        path="acceptance_mcp_capture.py", sha256=successor._sha(script), bytes=len(script)
    )
    assert "capture_mcp_tools.py" not in entries and "freeze_mcp_capture.py" not in entries
    assert (out / "acceptance_mcp_capture.py").read_bytes() == script


def test_a_missing_script_is_copied_from_the_successor_itself(successor, tmp_path):
    """No FileNotFoundError when OUT_DIR does not yet hold the script."""
    out = tmp_path / "out"
    out.mkdir()
    assert not (out / "acceptance_mcp_capture.py").exists()
    assert _freeze(successor, tmp_path, out) == 0
    assert (out / "acceptance_mcp_capture.py").read_bytes() == SUCCESSOR.read_bytes()


def test_an_identical_script_already_in_out_dir_is_accepted(successor, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    shutil.copyfile(SUCCESSOR, out / "acceptance_mcp_capture.py")
    assert _freeze(successor, tmp_path, out) == 0


def test_a_different_script_in_out_dir_is_refused(successor, tmp_path, capsys):
    """The lock never binds script bytes other than the ones that ran."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "acceptance_mcp_capture.py").write_bytes(SUCCESSOR.read_bytes() + b"# edited\n")
    assert _freeze(successor, tmp_path, out) == 2
    assert "script_mismatch" in capsys.readouterr().err
    assert not (out / "lock.v1.json").exists()
    assert not (out / "capture.v1.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="capture uses POSIX select on pipes")
def test_session_close_closes_stdin_and_stdout(successor, tmp_path):
    """RM40/AC31: a finished session leaks neither pipe descriptor."""
    session = successor.Session(
        [sys.executable, "-u", "-c", "import sys; sys.stdin.buffer.read()"],
        tmp_path,
        os.environ,
    )
    session.close()
    assert session.process.stdin.closed
    assert session.process.stdout.closed
    assert session.process.returncode is not None
