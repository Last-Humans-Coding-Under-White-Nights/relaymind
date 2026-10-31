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
