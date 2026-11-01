"""The committed Windows platform record describes the machine its report measured.

RM38; AC29, AC37; spec 0.66 sections 12.6.5-12.6.6. The record loads with the
packaged schema, and its machine block matches the bound platform report
field by field, so the record can be assessed against that report.
"""

import json
import re
from pathlib import Path

from relaymind.evaluation.asset_qualification_record import load_record
from relaymind.evaluation.asset_whole_task_qualification import (
    _MACHINE_FIELDS,
    _check_platform_machine,
)

RUN = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "evidence"
    / "m10"
    / "windows-platform-run-2026-11-01"
)
RECORD = RUN / "windows-platform-run-2026-11-01-review.json"


def _platform() -> dict:
    return json.loads((RUN / "platform.json").read_text(encoding="utf-8"))["platform"]


def test_the_windows_record_loads_and_binds_the_platform_report():
    record = load_record(RECORD)
    assert record.scope == "platform"
    reports = [s for s in record.document["subjects"] if s["role"] == "report"]
    assert [s["path"] for s in reports] == ["platform.json"]


def test_the_windows_record_machine_matches_the_platform_report():
    record = load_record(RECORD)
    platform = _platform()
    for report_field, machine_field in _MACHINE_FIELDS:
        assert record.machine[machine_field] == platform[report_field], machine_field
    reasons: set[str] = set()
    _check_platform_machine(record.machine, platform, reasons)
    assert reasons == set()


def test_the_windows_record_names_no_moving_adapter_reference():
    """RM16, spec 12.6.6: revisions bind exact revisions, never a branch name.

    The archived full record named the branch ``m10-control-ui-l10n``, which
    moves. The measured adapter's native revision could not be recomputed, so
    the committed record leaves ``adapter_revision`` null; the tarball is
    identified by its SHA-256 in ``release.json``. A later record may name a
    40-hex git commit or a 64-hex digest; any other string is rejected.
    """
    revision = load_record(RECORD).document["revisions"]["adapter_revision"]
    assert revision is None or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision), revision
    release = json.loads((RUN / "release.json").read_text(encoding="utf-8"))
    assert release["sha256"] in (RUN / "README.md").read_text(encoding="utf-8")
