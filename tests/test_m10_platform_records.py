"""Committed owner-run platform records stay loadable (RM38; AC29, AC37; spec 0.66 12.6.6)."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from relaymind.evaluation.asset_qualification_record import load_record

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "m10"
OWNER_RUNS = EVIDENCE / "owner-platform-runs-2026-11-01"
RECORDS = sorted(
    {
        *EVIDENCE.glob("*platform-run*/*platform-run*-review.json"),
        *EVIDENCE.glob("owner-platform-run-*.json"),
    }
)


@pytest.mark.parametrize("path", RECORDS, ids=lambda path: path.name)
def test_platform_record_loads_with_the_current_schema(path):
    """A record committed before a schema field was added must still validate."""
    assert load_record(path).scope == "platform"


def test_the_windows_platform_record_is_present():
    assert any("windows" in path.name for path in RECORDS)


@pytest.mark.parametrize("machine", ["macbook", "vps"])
def test_owner_run_records_live_where_section_12_6_6_says(machine):
    """Records live in docs/evidence/m10/ as <record_id>.json (spec 0.66 12.6.6)."""
    path = EVIDENCE / f"owner-platform-run-{machine}-2026-11-01.json"
    record = load_record(path)
    assert record.record_id == path.stem
    paths = [subject["path"] for subject in record.document["subjects"]]
    assert "owner-platform-runs-2026-11-01/README.md" in paths
    assert f"owner-platform-runs-2026-11-01/{machine}/platform/platform.json" in paths
    assert not list(OWNER_RUNS.glob("owner-platform-run-*.json"))


def _verify_block() -> str:
    """The shell block of the README's Verify section, exactly as documented."""
    text = (OWNER_RUNS / "README.md").read_text(encoding="utf-8")
    section = text.split("\n## Verify\n", 1)[1].split("\n## ", 1)[0]
    (block,) = re.findall(r"```bash\n(.*?)```", section, flags=re.S)
    return block


def _run_commit() -> str:
    commits = {
        json.loads(path.read_bytes())["revisions"]["repository_commit"]
        for path in EVIDENCE.glob("owner-platform-run-*.json")
    }
    (commit,) = commits
    return commit


def test_the_documented_verify_procedure_names_the_recorded_run_commit():
    """The procedure grades at the generator commit the records bind (12.6.5, 12.6.6)."""
    block = _verify_block()
    assert f"run={_run_commit()}\n" in block
    # It never writes into the committed evidence: only into $GEN and $WORK.
    assert 'gen="${GEN:?' in block and 'work="${WORK:?' in block


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def test_the_documented_verify_procedure_reproduces_verify_txt(tmp_path):
    """Running the README's Verify block reproduces both verify.txt files (#261).

    The grader's inventory check compares a report's source inventory with the
    running package, so a report verifies only with its generator's code. The
    block therefore extracts the recorded run commit and runs that commit's
    ``verify`` on copies of the evidence. The committed evidence must stay
    byte-identical.
    """
    run = _run_commit()
    if _git("cat-file", "-e", f"{run}^{{commit}}").returncode != 0:
        pytest.skip(f"run commit {run} is not in this clone (shallow checkout)")
    if shutil.which("bash") is None or shutil.which("tar") is None:
        pytest.skip("the documented procedure needs bash and tar")
    # The run commit is reachable from this branch, not only a loose object.
    assert _git("merge-base", "--is-ancestor", run, "HEAD").returncode == 0
    shim = tmp_path / "bin"
    shim.mkdir()
    # A wrapper, not a symlink: a venv interpreter finds its packages by its path.
    (shim / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (shim / "python").chmod(0o755)
    before = {
        p.relative_to(OWNER_RUNS).as_posix(): p.read_bytes()
        for p in OWNER_RUNS.rglob("*")
        if p.is_file()
    }
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    env["GEN"], env["WORK"] = str(tmp_path / "gen"), str(tmp_path / "work")
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _verify_block()],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    after = {
        p.relative_to(OWNER_RUNS).as_posix(): p.read_bytes()
        for p in OWNER_RUNS.rglob("*")
        if p.is_file()
    }
    assert after == before
