"""RM34/RM40 AC25/AC31: new source paths preserve UTF-8/LF on Windows."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

from test_asset_archive import archive
from test_asset_diagnostic import development_input as development_input
from test_asset_diagnostic import load, save_input
from test_asset_source_catalog import api, original
from test_source_benchmark_cli import recipe_files as recipe_files


def windows_text_defaults(monkeypatch):
    """Model actual implicit cp1252 and output CRLF at pathlib's open boundary."""
    original_open = Path.open

    def open_text(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if "b" not in mode:
            encoding = "cp1252" if encoding in (None, "locale") else encoding
            if newline is None and any(flag in mode for flag in "wax+"):
                newline = "\r\n"
        return original_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", open_text)


def test_chinese_source_catalog_and_import_keep_exact_utf8_lf(tmp_path, monkeypatch):
    source = original(
        payload=archive(
            [
                (
                    "SKILL.md",
                    (
                        "---\nname: english-skill\ndescription: 检查日历。\n---\n"
                        "读取日历并总结安排。\n"
                    ).encode(),
                )
            ]
        )
    )
    first = api().build_source_catalog([source], tmp_path / "baseline", dataset_id="windows-source")
    expected = {p.name: p.read_bytes() for p in [first.catalog_path, *first.import_paths.values()]}
    windows_text_defaults(monkeypatch)
    second = api().build_source_catalog([source], tmp_path / "windows", dataset_id="windows-source")
    assert {
        p.name: p.read_bytes() for p in [second.catalog_path, *second.import_paths.values()]
    } == expected
    assert "检查日历" in second.catalog_path.read_bytes().decode("utf-8")
    assert all(b"\r\n" not in value for value in expected.values())


def test_generated_production_configs_keep_exact_lf_under_windows_defaults(tmp_path, monkeypatch):
    from relaymind.evaluation.asset_source_recipe import write_example_configs

    built = api().build_source_catalog(
        [original()], tmp_path / "catalog", dataset_id="windows-source"
    )
    paths = write_example_configs(built)
    expected = {name: p.read_bytes() for name, p in paths.items()}
    windows_text_defaults(monkeypatch)
    actual = write_example_configs(built)
    assert {name: p.read_bytes() for name, p in actual.items()} == expected


def test_diagnostic_fixture_keeps_chinese_under_windows_defaults(development_input, monkeypatch):
    doc = copy.deepcopy(load(development_input).document)
    before = (development_input / "diagnostic.json").read_bytes()
    windows_text_defaults(monkeypatch)
    save_input(development_input, doc)
    assert (development_input / "diagnostic.json").read_bytes() == before
    assert load(development_input).document["cases"][0]["task_text"] == "查看我的日历安排"


def test_source_cli_json_survives_cp1252_pipe_with_chinese_output_path(recipe_files):
    recipe, cache, destination = recipe_files
    destination = destination.parent / "中文目录"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts/source_asset_benchmark.py"),
            "build",
            "--recipe",
            str(recipe),
            "--cache",
            str(cache),
            "--output",
            str(destination),
        ],
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode("cp1252")
    output = json.loads(result.stdout.decode("ascii"))
    assert output["catalog"] == str(destination / "catalog.json")
    assert output["admitted"] == 1
