"""Committed owner-run platform records stay loadable (RM38; AC29, AC37; spec 0.66 12.6.6)."""

from pathlib import Path

import pytest

from relaymind.evaluation.asset_qualification_record import load_record

EVIDENCE = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "m10"
RECORDS = sorted(EVIDENCE.glob("*platform-run*/*platform-run*-review.json"))


@pytest.mark.parametrize("path", RECORDS, ids=lambda path: path.name)
def test_platform_record_loads_with_the_current_schema(path):
    """A record committed before a schema field was added must still validate."""
    assert load_record(path).scope == "platform"


def test_the_windows_platform_record_is_present():
    assert any("windows" in path.name for path in RECORDS)


def _owner_run_records():
    """Every committed spec 12.6.6 record under docs/evidence/m10, at any depth."""
    import json

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
    ``docs/evidence/m10`` subfolder may be a text file Git converts.
    """
    import subprocess

    root = Path(__file__).resolve().parents[1]
    files = subprocess.run(
        ["git", "ls-files", "docs/evidence/m10/*/**"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert files
    # Git converts newlines only for files it treats as text; ``eol`` alone
    # has no effect once ``text`` is unset.
    attrs = subprocess.run(
        ["git", "check-attr", "text", "--stdin"],
        cwd=root,
        input="\n".join(files),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    converted = sorted(
        line.split(": ")[0]
        for line in attrs
        if line.endswith(": text: set") or line.endswith(": text: auto")
    )
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
