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
