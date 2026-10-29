"""RM34/AC25: actual native Windows handle captures; CI functional evidence only."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_asset_sources import capture, imported, readers, skill

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires native Windows filesystem APIs")


def junction(link, target):
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
    )
    assert result.returncode == 0, "could not create disposable native junction"


def test_native_unicode_long_path_readonly_and_case_lookup(tmp_path):
    root = tmp_path / ("资料𐐀" + "x" * 90) / ("nested" + "x" * 90) / ("more" + "x" * 60)
    extended = Path("\\\\?\\" + str(root))
    source = skill(extended / "Greeting")
    source.chmod(0o444)
    try:
        got = capture(extended)
        assert got.documents[0].record.identity["package_id"] == "Hello World"
        assert capture(extended / "greeting").documents
    finally:
        source.chmod(0o666)


def test_native_manifest_hardlink_alias_and_original_dedup(tmp_path):
    s, _ = readers()
    manifest, data = imported(tmp_path)
    os.link(manifest, tmp_path / "alias.json")
    data["records"][0]["original_path"] = "alias.json"
    manifest.write_text(json.dumps(data))
    with pytest.raises(s.SourceFailure):
        capture(manifest, "metadata_import")
    (tmp_path / "alias.json").unlink()
    manifest, data = imported(tmp_path)
    os.link(tmp_path / "hello.bin", tmp_path / "other.bin")
    second = json.loads(json.dumps(data["records"][0]))
    second["record"]["asset_id"] = "example:second"
    second["original_path"] = "other.bin"
    data["records"].append(second)
    manifest.write_text(json.dumps(data))
    got = capture(manifest, "metadata_import")
    assert got.received_bytes == manifest.stat().st_size + 5


@pytest.mark.parametrize("location", ["root", "ancestor", "candidate", "original"])
def test_native_junctions_never_traversed(tmp_path, location):
    s, _ = readers()
    real = tmp_path / "real"
    skill(real / "child")
    link = tmp_path / "junction"
    junction(link, real)
    source, kind = link, "local_directory"
    if location == "ancestor":
        source = link / "child"
    elif location == "candidate":
        source = tmp_path / "container"
        source.mkdir()
        junction(source / "child", real / "child")
    elif location == "original":
        source, data = imported(tmp_path / "import")
        original = (real / "child" / "SKILL.md").read_bytes()
        (source.parent / "hello.bin").write_bytes(original)
        data["records"][0]["record"]["content_hash"] = hashlib.sha256(original).hexdigest()
        source.write_text(json.dumps(data))
        assert capture(source, "metadata_import").documents
        junction(source.parent / "nested", real)
        data["records"][0]["original_path"] = "nested/child/SKILL.md"
        source.write_text(json.dumps(data))
        kind = "metadata_import"
    with pytest.raises(s.SourceFailure):
        capture(source, kind)


def test_native_ads_and_nonregular_originals_refused(tmp_path):
    s, _ = readers()
    manifest, data = imported(tmp_path)
    for path in ("hello.bin:secret", "hello.bin::$DATA", "folder"):
        if path == "folder":
            (tmp_path / path).mkdir()
        data["records"][0]["original_path"] = path
        manifest.write_text(json.dumps(data))
        with pytest.raises(s.SourceFailure):
            capture(manifest, "metadata_import")


def test_native_retained_handles_block_rename_write_and_delete(tmp_path):
    from relaymind.assets.source_io import open_source_tree

    skill(tmp_path / "root")
    tree = open_source_tree(tmp_path / "root")
    source_file = tree.open_file("SKILL.md")
    try:
        code = """import pathlib,sys
root=pathlib.Path(sys.argv[1])
blocked=0
for operation in (lambda:root.rename(root.with_name('renamed')),
                  lambda:(root/'SKILL.md').write_bytes(b'unexpected'),
                  lambda:(root/'SKILL.md').unlink()):
    try: operation()
    except OSError: blocked+=1
raise SystemExit(0 if blocked==3 else 1)
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path / "root")], capture_output=True, check=False
        )
        assert result.returncode == 0
        assert source_file.read(4) == b"---\n"
        tree.verify_root()
    finally:
        source_file.close()
        tree.close()


@pytest.mark.parametrize(
    "scenario",
    ["oplock-capture", "pending-open-cancel", "pending-read-cancel", "pending-read-deadline"],
)
def test_native_io_oplock_and_cancellation_with_external_watchdog(tmp_path, scenario):
    """RM34/AC25: real pending native calls must fence, complete and release ownership."""
    helper = Path(__file__).with_name("asset_windows_io_helper.py")
    child = subprocess.Popen(
        [sys.executable, str(helper), scenario, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = child.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        # This process tree contains only this disposable test's holder/capture.
        subprocess.run(
            ["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, check=False
        )
        stdout, stderr = child.communicate(timeout=5)
        pytest.fail(f"native I/O watchdog expired: {scenario}; {stdout}; {stderr}")
    assert child.returncode == 0, stdout + stderr
    evidence = json.loads(stdout.strip().splitlines()[-1])
    assert evidence["completed"]
    assert evidence["elapsed_seconds"] < 2
    if scenario == "oplock-capture":
        assert evidence["retained_revision"] == 1
        assert evidence["ordinary_open_pended"]
    else:
        assert evidence["reason"] == ("deadline" if scenario.endswith("deadline") else "cancelled")
        assert evidence["native_call_was_pending"]
