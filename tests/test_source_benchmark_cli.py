"""RM34/RM40: tested acquire/build/run/compare and explicit shortlist selection commands."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from relaymind.canonical import canonical_digest
from test_asset_archive import archive
from test_asset_index_preparation_integration import indexed_server as indexed_server

SCRIPT = Path(__file__).parents[1] / "scripts/source_asset_benchmark.py"


def cli(*args, input=None):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        input=input,
        text=True,
        capture_output=True,
        env=os.environ.copy(),
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def recipe_files(tmp_path):
    payload = archive(
        [
            (
                "SKILL.md",
                (
                    b"---\nname: calendar\ndescription: Inspect a calendar.\n---\n"
                    b"Inspect and summarize a calendar."
                ),
            )
        ]
    )
    sha = hashlib.sha256(payload).hexdigest()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / (sha + ".original")).write_bytes(payload)
    value = dict(
        schema_version=1,
        dataset_id="fixture-source-benchmark",
        files=[
            dict(
                id="archive",
                url="https://source.example/calendar.zip",
                sha256=sha,
                bytes=len(payload),
            )
        ],
        assets=[
            dict(
                file_id="archive",
                asset_id="fixture:calendar",
                source_id="fixture-source",
                source_url="https://source.example/calendar",
                revision="revision1",
                version="1",
                captured_at="2026-10-30T00:00:00Z",
                kind="skill",
                name="calendar",
                format="skill_zip",
            )
        ],
    )
    recipe = tmp_path / "recipe.json"
    recipe.write_text(json.dumps(value), encoding="utf-8", newline="\n")
    return recipe, cache, tmp_path / "dataset"


def test_rm40_cli_acquire_and_build_actual_pinned_originals(recipe_files):
    recipe, cache, out = recipe_files
    assert cli("acquire", "--recipe", recipe, "--cache", cache)["reused"] == 1
    built = cli("build", "--recipe", recipe, "--cache", cache, "--output", out)
    assert built["admitted"] == 1
    assert (out / "catalog.json").is_file() and (out / "builtin-production.toml").is_file()
    catalog = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    assert catalog["people"] == []
    assert catalog["assets"][0]["record"]["availability"] == "unknown"


@pytest.mark.parametrize("indexed", [False, True])
def test_rm40_cli_shared_run_compare_and_explicit_choice(recipe_files, indexed_server, indexed):
    recipe, cache, out = recipe_files
    cli("build", "--recipe", recipe, "--cache", cache, "--output", out)
    catalog = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    original = catalog["assets"][0]["original_evidence_id"]
    case = dict(
        id="english",
        task_text="Inspect a calendar",
        conversation=[],
        lane="en_to_en",
        query_language="en",
        requested_kinds=["skill", "mcp_tool"],
        source_asset_ids=["fixture:calendar"],
        source_evidence_ids=[original],
        authorship="generated",
        reference_kind="author_defined",
        reference_class="positive",
        acceptable_asset_ids=["fixture:calendar"],
        reference_evidence_ids=[original],
        notes="Synthetic CLI test source reference, no independent humans.",
    )
    cases = dict(
        schema_version=1,
        dataset_id=catalog["dataset_id"],
        profile="source_grounded_development",
        catalog_digest=canonical_digest(catalog),
        evidence=[],
        cases=[case],
    )
    cases["digest"] = canonical_digest(cases)
    path = out / "diagnostic.json"
    path.write_text(json.dumps(cases), encoding="utf-8", newline="\n")
    report = out / "report.json"
    production_path = out / "builtin-production.toml"
    extra = []
    if indexed:
        _, _, url = indexed_server
        production_path = out / "http-production.toml"
        production_path.write_text(
            production_path.read_text(encoding="utf-8").replace("http://127.0.0.1:8913", url),
            encoding="utf-8",
            newline="\n",
        )
        prep = out / "cold.toml"
        prep.write_text(
            '[preparation]\nmode="http"\nprofile="cold"\nurl="' + url + '/index"\n',
            encoding="utf-8",
            newline="\n",
        )
        extra = ["--preparation-config", prep]
    response = cli(
        "run",
        "--catalog",
        out / "catalog.json",
        "--cases",
        path,
        "--evidence-root",
        out,
        "--production",
        production_path,
        "--output",
        report,
        *extra,
    )
    assert response["summary"]["hit3"] == 1 and response["summary"]["choice_required"] == 1
    assert (
        cli("compare", "--left", report, "--right", report, "--output", out / "comparison.json")[
            "compatible"
        ]
        is True
    )
    choice = cli("choose", "--report", report, "--case", "english", input="1\n")
    assert choice["selected_asset_id"] == "fixture:calendar"
    assert choice["action_executed"] is False


def test_rm35_low_confidence_selection_can_be_declined_without_fake_choice(recipe_files):
    recipe, cache, out = recipe_files
    cli("build", "--recipe", recipe, "--cache", cache, "--output", out)
    # Selection UI consumes an actual ranked result; this fixture never represents user evidence.
    report = {
        "catalog_digest": "0" * 64,
        "rows": [
            {
                "slot": {"case_id": "choice", "repetition": 0},
                "evaluated_decision": "choice_required",
                "evaluated_candidates": [{"asset_id": "fixture:calendar"}],
                "request": {
                    "catalog": [
                        {
                            "asset_id": "fixture:calendar",
                            "identity": {"package_id": "calendar"},
                            "description": "Inspect a calendar.",
                            "availability": "unknown",
                        }
                    ]
                },
            }
        ],
    }
    path = out / "choice-report.json"
    path.write_text(json.dumps(report), encoding="utf-8", newline="\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "choose", "--report", str(path), "--case", "choice"],
        input="0\n",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["selected_asset_id"] is None
    assert "Inspect a calendar." in result.stderr


def test_rm35_choose_treats_explicit_null_catalog_document_as_absent(tmp_path):
    # RM35/AC26: an explicit null catalog document falls back to row evidence.
    report = {
        "catalog_digest": "0" * 64,
        "catalog_document": None,
        "rows": [
            {
                "slot": {"case_id": "choice", "repetition": 0},
                "evaluated_decision": "choice_required",
                "evaluated_candidates": [{"asset_id": "fixture:calendar"}],
                "request": {
                    "catalog": [
                        {
                            "asset_id": "fixture:calendar",
                            "identity": {"package_id": "calendar"},
                            "description": "Inspect a calendar.",
                            "availability": "unknown",
                        }
                    ]
                },
            }
        ],
    }
    path = tmp_path / "choice-report.json"
    path.write_text(json.dumps(report), encoding="utf-8", newline="\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "choose", "--report", str(path), "--case", "choice"],
        input="1\n",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["selected_asset_id"] == "fixture:calendar"
    assert "Inspect a calendar." in result.stderr


@pytest.mark.parametrize("row", [{"kind": "license"}, ["license"], 1])
def test_rm35_malformed_evidence_file_rows_are_invalid_source_recipe(tmp_path, row):
    # RM35/AC26: a partial evidence row is a recipe error, not a crash.
    from relaymind.evaluation.asset_corpus_io import AssetCorpusError
    from relaymind.evaluation.asset_source_recipe import build_recipe

    recipe = dict(
        schema_version=1,
        dataset_id="fixture-source-benchmark",
        files=[],
        assets=[],
        evidence_files=[row],
    )
    with pytest.raises(AssetCorpusError, match="invalid_source_recipe"):
        build_recipe(recipe, tmp_path / "cache", tmp_path / "catalog.json")


@pytest.mark.parametrize(
    "case", ["not_mapping", "list", "missing_file_id", "missing_asset_id", "unexpected"]
)
def test_rm35_malformed_asset_rows_are_invalid_source_recipe(tmp_path, case):
    # RM35/AC26: a partial asset row is a recipe error, not a crash.
    from relaymind.evaluation.asset_corpus_io import AssetCorpusError
    from relaymind.evaluation.asset_source_recipe import build_recipe

    base = dict(
        file_id="file",
        asset_id="asset",
        source_id="source",
        source_url="https://example.org/original",
        revision="r1",
        version="1",
        captured_at="2026-10-31T00:00:00Z",
        kind="skill",
        name="skill",
        format="skill_md",
    )
    if case == "not_mapping":
        raw = 1
    elif case == "list":
        raw = ["file"]
    elif case == "missing_file_id":
        raw = dict(base)
        del raw["file_id"]
    elif case == "missing_asset_id":
        raw = dict(base)
        del raw["asset_id"]
    else:
        raw = dict(base, unexpected=1)
    recipe = dict(
        schema_version=1,
        dataset_id="fixture-source-benchmark",
        files=[],
        assets=[raw],
        evidence_files=[],
    )
    with pytest.raises(AssetCorpusError, match="invalid_source_recipe"):
        build_recipe(recipe, tmp_path / "cache", tmp_path / "catalog.json")


def test_rm35_missing_dataset_id_is_invalid_source_recipe(tmp_path):
    from relaymind.evaluation.asset_corpus_io import AssetCorpusError
    from relaymind.evaluation.asset_source_recipe import build_recipe

    recipe = dict(schema_version=1, files=[], assets=[], evidence_files=[])
    with pytest.raises(AssetCorpusError, match="invalid_source_recipe"):
        build_recipe(recipe, tmp_path / "cache", tmp_path / "catalog.json")


def test_rm35_file_rows_require_a_non_empty_string_id(tmp_path):
    from relaymind.evaluation.asset_corpus_io import AssetCorpusError
    from relaymind.evaluation.asset_source_recipe import build_recipe

    recipe = dict(
        schema_version=1,
        dataset_id="fixture-source-benchmark",
        files=[{"sha256": "0" * 64, "bytes": 0}],
        assets=[],
        evidence_files=[],
    )
    with pytest.raises(AssetCorpusError, match="invalid_source_recipe"):
        build_recipe(recipe, tmp_path / "cache", tmp_path / "catalog.json")


def test_rm35_asset_version_is_optional(tmp_path):
    # RM35/AC26: SourceOriginal.version is nullable; a missing key means None.
    from relaymind.evaluation.asset_source_recipe import build_recipe

    payload = archive(
        [
            (
                "SKILL.md",
                b"---\nname: english-skill\ndescription: Inspect source code.\n---\nInspect it.",
            )
        ]
    )
    digest = hashlib.sha256(payload).hexdigest()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / (digest + ".original")).write_bytes(payload)
    recipe = dict(
        schema_version=1,
        dataset_id="fixture-source-benchmark",
        files=[
            {
                "id": "file",
                "url": "https://example.org/original",
                "sha256": digest,
                "bytes": len(payload),
            }
        ],
        assets=[
            {
                "file_id": "file",
                "asset_id": "source:skill",
                "source_id": "source",
                "source_url": "https://example.org/original",
                "revision": "r1",
                "captured_at": "2026-10-30T20:00:00Z",
                "kind": "skill",
                "name": "english-skill",
                "format": "skill_zip",
            }
        ],
        evidence_files=[],
    )
    result = build_recipe(recipe, cache, tmp_path / "catalog.json")
    assert result.admitted == 1
    assert result.excluded == 0


@pytest.mark.parametrize(
    "change",
    [
        {"files": None},
        {"files": "files"},
        {"assets": None},
        {"assets": {}},
        {"evidence_files": None},
        {"evidence_files": [{"kind": "metadata", "file_id": "f"}]},
        {"evidence_files": [{"kind": "license", "file_id": "f", "extra": 1}]},
        {"evidence_files": [{"kind": "license", "file_id": ""}]},
        {"exclusions": None},
        {"exclusions": "x"},
        {"exclusions": [{"source_id": "s"}]},
        {"exclusions": [{"source_id": 1, "original_id": None, "reason": "r"}]},
        {"exclusions": [{"source_id": "s", "original_id": 2, "reason": "r"}]},
        {"exclusions": [{"source_id": "s", "original_id": None, "reason": ""}]},
    ],
)
def test_rm35_recipe_structure_errors_are_invalid_source_recipe(tmp_path, change):
    """RM35/AC26: a malformed recipe is a recipe error; nothing is built or deleted."""
    from relaymind.evaluation.asset_corpus_io import AssetCorpusError
    from relaymind.evaluation.asset_source_recipe import build_recipe

    recipe = dict(
        schema_version=1,
        dataset_id="fixture-source-benchmark",
        files=[],
        assets=[],
        evidence_files=[],
        exclusions=[],
    )
    recipe.update(change)
    with pytest.raises(AssetCorpusError, match="invalid_source_recipe"):
        build_recipe(recipe, tmp_path / "cache", tmp_path / "catalog")
    assert not (tmp_path / "catalog").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"file_id": []},
        {"file_id": 123},
        {"file_id": ""},
        {"metadata_file_id": ""},
        {"license_file_id": ""},
    ],
)
def test_rm35_asset_file_references_must_be_non_empty_strings(tmp_path, change):
    from relaymind.evaluation.asset_corpus_io import AssetCorpusError
    from relaymind.evaluation.asset_source_recipe import build_recipe

    asset = dict(
        file_id="file",
        asset_id="asset",
        source_id="source",
        source_url="https://example.org/original",
        revision="r1",
        captured_at="2026-10-31T00:00:00Z",
        kind="skill",
        name="skill",
        format="skill_md",
    )
    asset.update(change)
    recipe = dict(
        schema_version=1,
        dataset_id="fixture-source-benchmark",
        files=[],
        assets=[asset],
        evidence_files=[],
        exclusions=[],
    )
    with pytest.raises(AssetCorpusError, match="invalid_source_recipe"):
        build_recipe(recipe, tmp_path / "cache", tmp_path / "catalog")


@pytest.mark.parametrize(
    "catalog_document, request_catalog",
    [({"assets": None}, None), (None, None), ({"assets": [None, 1]}, [None, "x"])],
)
def test_rm35_choose_tolerates_null_or_malformed_catalog_lists(
    tmp_path, catalog_document, request_catalog
):
    report = {
        "catalog_digest": "0" * 64,
        "catalog_document": catalog_document,
        "rows": [
            {
                "slot": {"case_id": "choice", "repetition": 0},
                "evaluated_decision": "choice_required",
                "evaluated_candidates": [{"asset_id": "fixture:calendar"}],
                "request": {"catalog": request_catalog},
            }
        ],
    }
    path = tmp_path / "choice-report.json"
    path.write_text(json.dumps(report), encoding="utf-8", newline="\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "choose", "--report", str(path), "--case", "choice"],
        input="1\n",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["selected_asset_id"] == "fixture:calendar"
