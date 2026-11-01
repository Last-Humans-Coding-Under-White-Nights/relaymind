"""Committed owner-run platform records stay loadable (RM38; AC29, AC37; spec 0.66 12.6.6)."""

from pathlib import Path

import pytest

from relaymind.evaluation.asset_qualification_record import load_record

EVIDENCE = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "m10"
RECORDS = sorted(
    {
        *EVIDENCE.glob("*platform-run*/*platform-run*-review.json"),
        *EVIDENCE.glob("owner-platform-runs-*/owner-platform-run-*.json"),
    }
)


@pytest.mark.parametrize("path", RECORDS, ids=lambda path: path.name)
def test_platform_record_loads_with_the_current_schema(path):
    """A record committed before a schema field was added must still validate."""
    assert load_record(path).scope == "platform"


def test_the_windows_platform_record_is_present():
    assert any("windows" in path.name for path in RECORDS)
