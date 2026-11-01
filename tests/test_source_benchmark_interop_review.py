"""RM35/RM40/RM44, AC26/AC31: source runs retain shared preparation gates."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import FakeClock
from test_asset_diagnostic import development_input as development_input
from test_asset_diagnostic_runner import api, english_case, production
from test_asset_index_preparation_integration import indexed_server as indexed_server
from test_source_index_preparation import execute


@pytest.fixture
def ready_source_report(development_input, indexed_server):
    _, _, url = indexed_server
    english_case(development_input)
    return execute(
        development_input,
        url,
        dict(
            mode="http",
            url=url + "/index",
            profile="cold",
            total_ms=5000,
            request_ms=2000,
            poll_ms=50,
            max_polls=100,
        ),
    )


@pytest.mark.parametrize("state,status", [("failed", "blocked"), ("cancelled", "interrupted")])
def test_source_cli_nonready_preparation_returns_nonzero_with_every_slot(
    development_input, indexed_server, monkeypatch, state, status
):
    """Real subprocess/HTTP, fixture terminal state; no invented user cancellation."""
    _, server, url = indexed_server
    registry = server.registry
    monkeypatch.setattr(
        registry,
        "exchange",
        lambda request: registry._response(
            request, state, "build_failed" if state == "failed" else "cancelled", elapsed=0
        ),
    )
    production_path = production(development_input, http=True)
    production_path.write_text(
        production_path.read_text(encoding="utf-8").replace("http://127.0.0.1:8913", url),
        encoding="utf-8",
        newline="\n",
    )
    config = development_input / "preparation.toml"
    config.write_text(
        '[preparation]\nmode="http"\nprofile="cold"\nurl="' + url + '/index"\n',
        encoding="utf-8",
        newline="\n",
    )
    output = development_input / "run-report.json"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts/source_asset_benchmark.py"),
            "run",
            "--catalog",
            str(development_input / "catalog.json"),
            "--cases",
            str(development_input / "diagnostic.json"),
            "--evidence-root",
            str(development_input),
            "--production",
            str(production_path),
            "--preparation-config",
            str(config),
            "--repetitions",
            "3",
            "--output",
            str(output),
        ],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=20,
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == status
    assert [row["status"] for row in report["rows"]] == ["not_run"] * 3
    assert all(row["attempts"] == [] and row["request"] is None for row in report["rows"])
    assert json.loads(result.stdout)["output"] == str(output)
    assert result.returncode != 0


@pytest.mark.parametrize("field", ["total_ms", "request_ms", "poll_ms", "max_polls"])
def test_source_comparison_rejects_each_changed_preparation_budget(
    ready_source_report, development_input, indexed_server, field
):
    settings = dict(ready_source_report["preparation"]["config"])
    settings[field] += 1
    _, _, url = indexed_server
    changed = execute(development_input, url, settings)
    with pytest.raises(ValueError, match="incompatible_development_runs"):
        api().compare_source_benchmarks(ready_source_report, changed)


@pytest.mark.parametrize(
    "where,field,value",
    [
        ("exchange", "observed_ms", -1),
        ("exchange", "observed_ms", True),
        ("exchange", "observed_ms", "0"),
        ("exchange", "unexpected", 0),
        ("record", "unexpected", 0),
        ("record", "observed_ms", -1),
        ("record", "cleanup_ms", True),
        ("exchange", "operation", "invented"),
    ],
)
def test_source_comparison_requires_shared_closed_preparation_shape(
    ready_source_report, where, field, value
):
    changed = copy.deepcopy(ready_source_report)
    target = changed["preparation"]
    if where == "exchange":
        target = target["exchanges"][0]
    target[field] = value
    with pytest.raises(ValueError, match="invalid_development_report"):
        api().compare_source_benchmarks(ready_source_report, changed)


def test_source_comparison_keeps_valid_setup_and_unknown_costs(ready_source_report):
    compared = api().compare_source_benchmarks(ready_source_report, ready_source_report)
    assert compared["compatible"]
    assert compared["preparation"]["left"]["observed_costs"] == []
    assert all(
        x["response"]["usage_claims"] is None
        for x in ready_source_report["preparation"]["exchanges"]
        if x["response"]
    )


def test_source_comparison_allows_stateless_indexed_and_same_profile_endpoints(
    ready_source_report, development_input
):
    """RM40: setup observations remain explicit without forbidding different teams."""
    import asyncio

    from test_asset_diagnostic import load

    stateless = asyncio.run(
        api().run_source_benchmark(
            load(development_input),
            clock=FakeClock(),
            production_config=production(development_input),
        )
    )
    compared = api().compare_source_benchmarks(stateless, ready_source_report)
    assert compared["compatible"]
    assert compared["preparation"]["left"]["status"] == "not_applicable"
    assert compared["preparation"]["right"]["status"] == "ready"
    another_endpoint = copy.deepcopy(ready_source_report)
    another_endpoint["preparation"]["config"]["url"] = "http://127.0.0.1:8914/index"
    assert api().compare_source_benchmarks(ready_source_report, another_endpoint)["compatible"]
