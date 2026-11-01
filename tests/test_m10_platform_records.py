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
