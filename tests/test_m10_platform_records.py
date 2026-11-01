"""Committed owner-run platform records stay loadable (RM38; AC29, AC37; spec 0.66 12.6.6)."""

import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

from relaymind.evaluation.asset_qualification_record import load_record
from relaymind.evaluation.asset_whole_task_qualification import _check_platform_machine

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "m10"
OWNER_RUNS = EVIDENCE / "owner-platform-runs-2026-11-01"
# The two records whose subjects are in OWNER_RUNS (record ID = file stem, 12.6.6).
OWNER_MACHINES = ("macbook", "vps")
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


@pytest.mark.parametrize("machine", OWNER_MACHINES)
def test_owner_run_records_live_where_section_12_6_6_says(machine):
    """Records live in docs/evidence/m10/ as <record_id>.json (spec 0.66 12.6.6)."""
    path = EVIDENCE / f"owner-platform-run-{machine}-2026-11-01.json"
    record = load_record(path)
    assert record.record_id == path.stem
    paths = [subject["path"] for subject in record.document["subjects"]]
    assert "owner-platform-runs-2026-11-01/README.md" in paths
    assert f"owner-platform-runs-2026-11-01/{machine}/platform/platform.json" in paths
    assert not list(OWNER_RUNS.glob("owner-platform-run-*.json"))


@pytest.mark.parametrize(
    ("machine", "expected_reasons"), [("macbook", set()), ("vps", {"platform_not_target"})]
)
def test_owner_run_record_machine_matches_its_platform_report(machine, expected_reasons):
    """RM38/AC29, AC37; 12.6.6: identity matches; the VPS is never a target."""
    record = load_record(EVIDENCE / f"owner-platform-run-{machine}-2026-11-01.json")
    platform = json.loads((OWNER_RUNS / machine / "platform/platform.json").read_bytes())[
        "platform"
    ]
    # A contradicting field raises; a missing one adds machine_identity_unverified.
    reasons: set[str] = set()
    _check_platform_machine(record.machine, platform, reasons)
    assert reasons == expected_reasons


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _verify_block() -> str:
    """The shell block of the README's Verify section, exactly as documented."""
    text = (OWNER_RUNS / "README.md").read_text(encoding="utf-8")
    section = text.split("\n## Verify\n", 1)[1].split("\n## ", 1)[0]
    (block,) = re.findall(r"```bash\n(.*?)```", section, flags=re.S)
    return block


def _run_commit() -> str:
    """The commit both owner-platform-runs-2026-11-01 records bind, named by record ID."""
    commits = {
        json.loads((EVIDENCE / f"owner-platform-run-{machine}-2026-11-01.json").read_bytes())[
            "revisions"
        ]["repository_commit"]
        for machine in OWNER_MACHINES
    }
    (commit,) = commits
    return commit


def test_the_run_commit_comes_only_from_the_2026_11_01_owner_records(tmp_path, monkeypatch):
    """A later owner-run record with another commit does not change this run's commit."""
    for machine in OWNER_MACHINES:
        name = f"owner-platform-run-{machine}-2026-11-01.json"
        shutil.copyfile(EVIDENCE / name, tmp_path / name)
    later = json.loads((EVIDENCE / "owner-platform-run-vps-2026-11-01.json").read_bytes())
    later["revisions"]["repository_commit"] = "0" * 40
    (tmp_path / "owner-platform-run-vps-2026-12-01.json").write_text(json.dumps(later))
    expected = _run_commit()
    monkeypatch.setitem(globals(), "EVIDENCE", tmp_path)
    assert _run_commit() == expected


def test_the_documented_verify_procedure_names_the_recorded_run_commit():
    """The procedure grades at the generator commit the records bind (12.6.5, 12.6.6)."""
    block = _verify_block()
    assert f"run={_run_commit()}\n" in block
    # It never writes into the committed evidence: only into $GEN and $WORK.
    assert 'gen="${GEN:?' in block and 'work="${WORK:?' in block


def test_the_verify_procedure_skips_windows_even_with_shell_tools(tmp_path, monkeypatch):
    """RM16/AC29: Windows must skip the POSIX procedure even with bash and tar."""
    namespace = globals()
    monkeypatch.setitem(
        namespace, "sys", SimpleNamespace(platform="win32", executable=sys.executable)
    )
    monkeypatch.setitem(namespace, "_git", lambda *args: SimpleNamespace(returncode=0))
    monkeypatch.setattr(shutil, "which", lambda name: name)

    def unexpected_shell(*args, **kwargs):
        pytest.fail("the POSIX verify procedure must not launch on Windows")

    monkeypatch.setattr(subprocess, "run", unexpected_shell)
    with pytest.raises(pytest.skip.Exception, match="POSIX"):
        test_the_documented_verify_procedure_reproduces_verify_txt(tmp_path)


def test_the_documented_verify_procedure_reproduces_verify_txt(tmp_path):
    """Running the README's Verify block reproduces both verify.txt files (#261).

    The grader's inventory check compares a report's source inventory with the
    running package, so a report verifies only with its generator's code. The
    block therefore extracts the recorded run commit and runs that commit's
    ``verify`` on copies of the evidence. The committed evidence must stay
    byte-identical.
    """
    if sys.platform == "win32":
        pytest.skip("the documented procedure needs a POSIX shell and executable wrapper")
    run = _run_commit()
    if _git("cat-file", "-e", f"{run}^{{commit}}").returncode != 0:
        pytest.skip(f"run commit {run} is not in this clone (shallow checkout)")
    if shutil.which("bash") is None or shutil.which("tar") is None:
        pytest.skip("the documented procedure needs bash and tar")
    # The procedure needs only the commit object, not its ancestry: after a
    # squash merge the run commit is no longer an ancestor of HEAD.
    for machine in ("macbook", "vps"):
        lock = OWNER_RUNS / machine / "platform" / "bundle" / "release" / "lock.json"
        recorded = json.loads(lock.read_bytes())["unicode_version"]
        if recorded != unicodedata.unidata_version:
            # The grader refuses a different Unicode database by design.
            pytest.skip(
                f"{machine} was graded with Unicode {recorded}; this Python has "
                f"{unicodedata.unidata_version}"
            )
    shim = tmp_path / "bin"
    shim.mkdir()
    # A wrapper, not a symlink: a venv interpreter finds its packages by its path.
    (shim / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (shim / "python").chmod(0o755)
    before = _tree(OWNER_RUNS)
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
    assert _tree(OWNER_RUNS) == before


def _owner_run_records():
    """Every committed spec 12.6.6 record under docs/evidence/m10, at any depth."""
    found = []
    for path in sorted(EVIDENCE.rglob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(document, dict) and document.get("profile") == "owner_run":
            found.append(path)
    return found


OWNER_RUN_RECORDS = _owner_run_records()
M1_TRIAL = EVIDENCE / "m10-early-trial-m1-openclaw-2026.10.7-20261101.json"


@pytest.mark.parametrize(
    "path", OWNER_RUN_RECORDS, ids=lambda path: path.relative_to(EVIDENCE).as_posix()
)
def test_every_committed_owner_run_record_loads(path):
    """RM16; spec 0.66 12.6.6 (review of #276): every committed record still loads."""
    assert load_record(path).document["profile"] == "owner_run"


def test_the_m1_trial_record_is_a_pending_machine_dual_drafted_host_trial():
    assert M1_TRIAL in OWNER_RUN_RECORDS
    record = load_record(M1_TRIAL)
    assert record.scope == "host_trial"
    assert record.labelling == "machine_dual_owner_approved"
    assert record.prompt_authorship == "machine_drafted_owner_approved"
    assert record.machine["role"] == "target_macos_m1_8gib"
    assert not record.approved


def test_every_m10_evidence_folder_keeps_its_exact_bytes_on_every_checkout():
    """RM16/AC29: digest-bound M10 evidence must not get CRLF on a Windows checkout.

    Records bind every file in an evidence folder by SHA-256, including
    extensionless captures, TOML and TXT files, so no file under a
    ``docs/evidence/m10`` subfolder may be a text file Git converts. That
    includes a file whose ``text`` attribute is unspecified: a Windows checkout
    with ``core.autocrlf=true`` gives it CRLF line endings.
    """
    files = subprocess.run(
        ["git", "ls-files", "docs/evidence/m10/*/**"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert files
    # Git converts newlines only for files it treats as text; ``eol`` alone
    # has no effect once ``text`` is unset.
    attrs = subprocess.run(
        ["git", "check-attr", "text", "--stdin"],
        cwd=ROOT,
        input="\n".join(files),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    converted = sorted(line.split(": ")[0] for line in attrs if not line.endswith(": text: unset"))
    assert converted == []


# Raw captured output, kept byte for byte as the capturing tool wrote it and
# bound by digest in the release archive. Everything else under
# docs/evidence/m10 is authored here and must be plain UTF-8 without a BOM.
RAW_CAPTURED_WITH_BOM = frozenset({"windows-platform-run-2026-11-01/verify.txt"})


def test_authored_m10_evidence_files_have_no_byte_order_mark():
    with_bom = {
        path.relative_to(EVIDENCE).as_posix()
        for path in EVIDENCE.rglob("*")
        if path.is_file() and path.read_bytes().startswith(b"\xef\xbb\xbf")
    }
    assert with_bom == RAW_CAPTURED_WITH_BOM
