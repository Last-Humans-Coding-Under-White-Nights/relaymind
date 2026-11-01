"""RM35/RM40/RM44: source references reuse the shared optional index lifecycle."""

import asyncio
import copy

import pytest

from helpers import FakeClock
from test_asset_diagnostic import development_input as development_input
from test_asset_diagnostic import load
from test_asset_diagnostic_runner import api, english_case, production
from test_asset_index_preparation_integration import indexed_server as indexed_server


def execute(root, url, preparation, *, repetitions=1):
    config = production(root, http=True)
    config.write_text(
        config.read_text(encoding="utf-8").replace("http://127.0.0.1:8913", url),
        encoding="utf-8",
        newline="\n",
    )
    return asyncio.run(
        api().run_source_benchmark(
            load(root),
            clock=FakeClock(),
            production_config=config,
            preparation=preparation,
            repetitions=repetitions,
        )
    )


def test_rm40_source_cold_reuse_pins_same_full_offer_and_keeps_setup_outside_queries(
    development_input, indexed_server
):
    _, _, url = indexed_server
    english_case(development_input)
    settings = dict(
        mode="http",
        url=url + "/index",
        profile="cold",
        total_ms=5000,
        request_ms=2000,
        poll_ms=50,
        max_polls=100,
    )
    cold = execute(development_input, url, settings)
    reuse = execute(development_input, url, settings | {"profile": "reuse"})
    for report, reused in [(cold, False), (reuse, True)]:
        setup = report["preparation"]
        row = report["rows"][0]
        assert setup["status"] == "ready"
        assert setup["exchanges"][-1]["response"]["reused"] is reused
        assert setup["binding"]["catalog_digest"] == row["offered_projection_digest"]
        assert setup["binding"]["catalog_revision"] == row["request"]["catalog_revision"]
        assert row["result"]["final_decision"] == "choice_required"
        assert row["attempts"][0]["outcome"] == "ranked" and row["grade"]["hit3"]
        assert setup["observed_ms"] is not None and setup["observed_costs"] == []
        assert report["latency"]["known"] == 1
        assert report["qualification"] == "provisional"
    with pytest.raises(ValueError, match="incompatible_development_runs"):
        api().compare_source_benchmarks(cold, reuse)
    compared = api().compare_source_benchmarks(cold, cold)
    assert compared["compatible"] is True
    assert compared["preparation"]["left"] == cold["preparation"]
    changed = copy.deepcopy(cold)
    changed["preparation"]["binding"]["catalog_digest"] = "0" * 64
    with pytest.raises(ValueError, match="invalid_development_report"):
        api().compare_source_benchmarks(cold, changed)
    changed = copy.deepcopy(cold)
    changed["rows"][0]["result"]["attempts"][0]["result"]["index_revision"] = "unprepared-index"
    changed["rows"][0]["attempts"] = copy.deepcopy(changed["rows"][0]["result"]["attempts"])
    with pytest.raises(ValueError, match="invalid_development_report"):
        api().compare_source_benchmarks(cold, changed)


def test_rm40_unready_source_preparation_conserves_all_slots(development_input, indexed_server):
    _, _, url = indexed_server
    settings = dict(
        mode="http",
        url=url + "/missing",
        profile="cold",
        total_ms=1000,
        request_ms=500,
        poll_ms=50,
        max_polls=10,
    )
    report = execute(development_input, url, settings, repetitions=3)
    assert report["status"] == "blocked"
    assert report["preparation"]["status"] != "ready"
    assert [r["status"] for r in report["rows"]] == ["not_run"] * 3
    assert all(r["request"] is None and r["observed_ms"] is None for r in report["rows"])
    assert report["latency"] == dict(known=0, unknown=3, p50_ms=None, p95_ms=None)
    assert api().compare_source_benchmarks(report, report)["compatible"]


def test_rm40_source_builtin_none_preparation_has_no_invented_setup(development_input):
    report = asyncio.run(
        api().run_source_benchmark(
            load(development_input),
            clock=FakeClock(),
            production_config=production(development_input),
        )
    )
    assert report["preparation"]["status"] == "not_applicable"
    assert report["preparation"]["observed_ms"] is None
    assert report["preparation"]["exchanges"] == []
