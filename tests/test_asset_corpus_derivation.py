"""RM40/AC31: an offline supplied export preserves exact originals and exclusions."""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_asset_corpus import read, relock, stage_fixture, write


def derivation():
    try:
        return importlib.import_module("relaymind.evaluation.asset_derivation")
    except ModuleNotFoundError:
        pytest.fail("RM40/AC31 offline export derivation is not implemented")


@pytest.fixture
def input_root(tmp_path):
    root = tmp_path / "supplied"
    stage_fixture(root)
    catalog = read(root, "catalog")
    catalog["assets"][0]["rights"].update(
        acquisition="permitted",
        evaluation="permitted",
        retention="permitted",
        redistribution="prohibited",
    )
    catalog["exclusions"] = [
        {
            "source_id": "fixture-denied",
            "original_id": "denied-input",
            "reason": "acquisition_denied",
            "evidence_ids": ["permission"],
        }
    ]
    write(root, "catalog", catalog)
    relock(root)
    return root


def test_rm40_ac31_derivation_reproduces_supplied_originals_and_exclusions(input_root, tmp_path):
    output = tmp_path / "derived"
    result = derivation().derive_matrix_export(input_root, input_root / "lock.json", output)
    assert result.lock_digest == read(input_root, "lock")["digest"]
    assert result.exclusions[0]["original_id"] == "denied-input"
    assert result.assessment.status == "fixture"
    for path in result.output_paths:
        relative = path.relative_to(output)
        assert path.read_bytes() == (input_root / relative).read_bytes()
    assert (output / "originals/calendar.md").read_bytes() == (
        input_root / "originals/calendar.md"
    ).read_bytes()
    corpus = importlib.import_module("relaymind.evaluation.asset_corpus")
    release = corpus.load_asset_release(
        output / "lock.json", evidence_root=output, related_roots={}
    )
    assert release.lock["digest"] == result.lock_digest


@pytest.mark.parametrize("rights", ["unknown", "prohibited"])
def test_rm40_ac31_denied_derivation_has_no_output(input_root, tmp_path, rights):
    catalog = read(input_root, "catalog")
    catalog["assets"][0]["rights"]["acquisition"] = rights
    write(input_root, "catalog", catalog)
    relock(input_root)
    output = tmp_path / "derived"
    with pytest.raises(ValueError, match="rights_unqualified"):
        derivation().derive_matrix_export(input_root, input_root / "lock.json", output)
    assert not output.exists()


def test_rm40_ac31_derivation_never_overwrites_unrelated_originals(input_root, tmp_path):
    output = tmp_path / "already-there"
    output.mkdir()
    sentinel = output / "originals"
    sentinel.write_bytes(b"Unrelated user content")
    with pytest.raises(ValueError, match="output_exists"):
        derivation().derive_matrix_export(input_root, input_root / "lock.json", output)
    assert sentinel.read_bytes() == b"Unrelated user content"
    assert list(output.iterdir()) == [sentinel]


def test_rm40_ac31_tampered_export_never_leaves_partial_output(input_root, tmp_path):
    (input_root / "originals/calendar.md").write_text("Changed source")
    output = tmp_path / "derived"
    with pytest.raises(ValueError, match="evidence_digest"):
        derivation().derive_matrix_export(input_root, input_root / "lock.json", output)
    assert not output.exists()


def test_rm40_ac31_export_cannot_execute_network_or_archive_locators(input_root, tmp_path):
    catalog = read(input_root, "catalog")
    catalog["assets"][0]["source_url"] = "https://example.invalid/archive.tar.gz"
    write(input_root, "catalog", catalog)
    relock(input_root)
    (input_root / "do-not-open.zip").write_bytes(b"PK unsupported unused archive")
    result = derivation().derive_matrix_export(
        input_root, input_root / "lock.json", tmp_path / "out"
    )
    assert not any(p.suffix == ".zip" for p in result.output_paths)
    assert len(result.output_paths) == 16  # four documents plus twelve exact evidence files


def test_rm40_ac31_offline_derivation_cli(input_root, tmp_path):
    # Execute the real CLI; it must work from a checkout or installed package.
    script = Path(__file__).parents[1] / "scripts/benchmarks/derive_matrix.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--input",
            str(input_root),
            "--lock",
            str(input_root / "lock.json"),
            "--output",
            str(tmp_path / "cli-output"),
        ],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["qualification"] == "fixture"
    assert summary["lock_digest"] == read(input_root, "lock")["digest"]
    assert summary["excluded_original_ids"] == ["denied-input"]


def test_rm40_ac31_success_inside_handled_exception_keeps_verified_export(input_root, tmp_path):
    output = tmp_path / "successful"
    try:
        raise ValueError("unrelated caller state")
    except ValueError:
        result = derivation().derive_matrix_export(input_root, input_root / "lock.json", output)
    assert (output / "lock.json").exists()
    assert all(path.exists() for path in result.output_paths)
