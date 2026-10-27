"""RM16/RM25: UTF-8 evaluation artifacts survive a Windows-style text default."""

import json
from pathlib import Path

import pytest

import test_m7_metrics as metrics_tests
import test_m7_report as report_tests
from helpers import load_script_module


@pytest.fixture(autouse=True)
def cp1252_text_default(monkeypatch):
    """Exercise real decoding/encoding when callers leave the file encoding implicit.

    Explicit encodings retain their normal behavior. This changes only this test's
    pathlib boundary, without depending on installed locales or Python UTF-8 mode.
    """
    original_read = Path.read_text
    original_write = Path.write_text

    def read_text(path, encoding=None, errors=None, **kwargs):
        return original_read(
            path,
            encoding="cp1252" if encoding in (None, "locale") else encoding,
            errors=errors,
            **kwargs,
        )

    def write_text(path, data, encoding=None, errors=None, **kwargs):
        return original_write(
            path,
            data,
            encoding="cp1252" if encoding in (None, "locale") else encoding,
            errors=errors,
            **kwargs,
        )

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "write_text", write_text)


@pytest.mark.parametrize(
    "check",
    [
        metrics_tests.test_regrade_preserves_source_and_prior_analysis_with_metric_enrichment,
        metrics_tests.test_regrade_cli_writes_distinct_output_and_records_byte_digest,
        report_tests.test_report_retains_cost_of_cell_without_any_completed_attempt,
    ],
    ids=["historical-regrade", "regrade-cli", "failed-cell-analysis"],
)
def test_ci_artifact_paths_under_cp1252_default(tmp_path, check):
    """RM16/RM25: preserve source hashes, correction totals and failed-attempt cost."""
    check(tmp_path)


def test_offline_plan_round_trip_preserves_unicode_under_cp1252(tmp_path, monkeypatch):
    """RM16/RM25: an existing UTF-8 plan keeps its immutable identity on Windows."""
    driver = load_script_module("run_m7_matrix")
    plan = driver.make_plan("a" * 40, omit_criteria=True)
    plan["note"] = "证据：中文计划"
    monkeypatch.setattr(driver, "make_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        driver, "provider_environment", lambda _: pytest.fail("offline plan read credentials")
    )
    monkeypatch.setattr(
        driver.subprocess, "run", lambda *args, **kwargs: pytest.fail("offline plan ran a child")
    )
    out = tmp_path / "计划"
    args = ["--out", str(out), "--revision", "a" * 40, "--omit-criteria"]
    driver.main(args)
    before = (out / "plan.json").read_bytes()
    assert json.loads(before)["note"] == plan["note"]
    driver.main(args)
    assert (out / "plan.json").read_bytes() == before


def test_analysis_cli_writes_utf8_markdown_under_cp1252(tmp_path, monkeypatch):
    """RM16/RM25: reviewable reports retain Chinese evidence in their actual bytes."""
    report = load_script_module("analyze_m7")
    markdown = "# Evidence / 证据\n\n中文结果：校正次数 7/12。\n"
    result = {"reporting_accepted": True, "note": "中文证据"}
    monkeypatch.setattr(report, "analyze", lambda _: (result, markdown))
    out = tmp_path / "report.json"
    report.main(["--matrix", str(tmp_path / "matrix"), "--out", str(out)])
    assert json.loads(out.read_bytes()) == result
    # Text writes retain the platform's normal newline translation; encoding is
    # the contract under test, so compare logical lines after strict UTF-8 decode.
    assert out.with_suffix(".md").read_bytes().decode("utf-8").splitlines() == markdown.splitlines()
