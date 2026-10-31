"""RM35/RM40 AC26/AC31: indexed HTTP example and runner preserve actual readiness."""

import asyncio
import copy
import importlib.util
import json
import threading
from pathlib import Path

import pytest

from relaymind.selection.asset_contract import SelectorContext, parse_asset_request
from relaymind.selection.asset_http import HttpAssetSelector
from test_asset_preparation import inputs
from test_asset_selection_benchmark import fixture_config


def example():
    spec = importlib.util.spec_from_file_location(
        "indexed_example", Path("scripts/example_asset_selector.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def indexed_server():
    module = example()
    server = module.make_server("127.0.0.1", 0, preparation_path="/index")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield module, server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def query(binding, catalog):
    return parse_asset_request(
        dict(
            schema_version=2,
            recommendation_id="00000000-0000-4000-8000-000000000001",
            task_id="case",
            task_text="calendar",
            conversation=[],
            scope=binding["scope"],
            permissions=[],
            requested_kinds=binding["requested_kinds"],
            selection_scope="discovery",
            catalog_revision=1,
            host_snapshot_revision=None,
            language_tags=["en"],
            code_switched=False,
            catalog=catalog,
            deadline_ms=200,
            candidate_limit=10,
        )
    )


def test_indexed_server_requires_exact_preparation_and_reports_cold_then_reuse(indexed_server):
    import time

    from relaymind.selection.asset_preparation import prepare_asset_index

    _, server, url = indexed_server
    m, catalog, binding, config = inputs()
    binding["selector"] = {"name": "example_lexical", "version": "1"}
    config |= dict(url=url + "/index", total_ms=5000, request_ms=1000)
    primary = HttpAssetSelector(name="example_lexical", version="1", url=url + "/select")
    context = SelectorContext(time.monotonic, lambda: None)

    async def go():
        before = await primary.select(query(binding, catalog), context)
        assert before["outcome"] == "not_ready"
        prepared = await prepare_asset_index(config, binding, catalog)
        assert prepared["status"] == "ready"
        assert prepared["exchanges"][-1]["response"]["reused"] is False
        after = await primary.select(query(binding, catalog), context)
        assert (
            after["outcome"] == "ranked" and after["index_revision"] == prepared["index_revision"]
        )
        foreign = copy.deepcopy(binding)
        foreign["scope"]["workspace_id"] = "other"
        assert (await primary.select(query(foreign, catalog), context))["outcome"] == "not_ready"
        reused = await prepare_asset_index(config | {"profile": "reuse"}, binding, catalog)
        assert reused["status"] == "ready" and reused["exchanges"][-1]["response"]["reused"] is True
        assert reused["index_revision"] == prepared["index_revision"]

    asyncio.run(go())


def test_none_runner_record_is_explicit_and_comparison_is_self_contained(tmp_path):
    from relaymind.evaluation.asset_benchmark import run_asset_benchmark
    from relaymind.evaluation.asset_comparison import compare_asset_reports, render_asset_comparison

    report = asyncio.run(run_asset_benchmark(fixture_config(tmp_path)))
    assert report.data["config"]["preparation"]["mode"] == "none"
    assert report.data["preparation"]["status"] == "not_applicable"
    # In-memory reports deliberately lack exported input resolution; still retain
    # self-contained setup records in the incompatible comparison.
    result = compare_asset_reports(report, report, quality_only=True)
    assert result.data["preparation"]["a"] == report.data["preparation"]
    assert "Preparation" in render_asset_comparison(result)


def test_failed_preparation_preserves_all_planned_slots_without_query(tmp_path, indexed_server):
    from relaymind.evaluation.asset_benchmark import (
        load_benchmark_config,
        refresh_fixture_benchmark_config,
        run_asset_benchmark,
    )

    _, server, url = indexed_server
    config = fixture_config(tmp_path, primary="http", url=url + "/select")
    with config.path.open("a") as out:
        out.write(
            '\n[preparation]\nmode="http"\nurl="'
            + url
            + '/missing"\nprofile="cold"\ntotal_ms=5000\nrequest_ms=1000\npoll_ms=50\nmax_polls=1\n'
        )
    refresh_fixture_benchmark_config(config.path)
    report = asyncio.run(run_asset_benchmark(load_benchmark_config(config.path))).data
    assert report["status"] == "blocked"
    assert report["preparation"]["status"] == "failed"
    assert report["preparation"]["reason"] == "http_status"
    assert len(report["results"]) == len(report["planned_slots"]) == 3
    assert all(row["status"] == "not_run" and row["attempts"] == [] for row in report["results"])
    assert server.registry.jobs == {}


def test_standalone_prepare_command_emits_explicit_none_transcript(tmp_path):
    from relaymind.evaluation.asset_benchmark_cli import main

    config = fixture_config(tmp_path)
    output = tmp_path / "preparation.json"
    assert main(["prepare", "--config", str(config.path), "--output", str(output)]) == 0
    value = json.loads(output.read_text())
    assert value["status"] == "not_applicable" and value["observed_ms"] is None


@pytest.mark.parametrize("failure", ["wrong_index", "not_ready"])
def test_prepared_primary_failure_remains_visible_when_gateway_floor_succeeds(
    tmp_path, indexed_server, monkeypatch, failure
):
    from relaymind.evaluation.asset_benchmark import (
        load_benchmark_config,
        refresh_fixture_benchmark_config,
        run_asset_benchmark,
    )

    module, server, url = indexed_server
    config = fixture_config(
        tmp_path, primary="http", url=url + "/select", fallback=True, repetitions=1, warmups=0
    )
    with config.path.open("a") as out:
        out.write('\n[preparation]\nmode="http"\nurl="' + url + '/index"\nprofile="cold"\n')
    refresh_fixture_benchmark_config(config.path)
    original = server.registry.select

    async def changed(value):
        result = await original(value)
        if failure == "wrong_index":
            result["index_revision"] = "unprepared-index"
        else:
            result |= dict(outcome="not_ready", decision=None, candidates=[])
        return result

    monkeypatch.setattr(server.registry, "select", changed)
    report = asyncio.run(run_asset_benchmark(load_benchmark_config(config.path))).data
    assert report["preparation"]["status"] == "ready"
    row = report["results"][0]
    assert row["status"] == "ready" and row["result"]["fallback_reason"] is not None
    assert row["attempts"][0]["outcome"] == ("stale" if failure == "wrong_index" else "not_ready")
    assert row["attempts"][1]["selector"]["name"] == "lexical"


def test_lost_index_changes_status_to_stale_without_rewriting_binding(indexed_server):
    from relaymind.selection.asset_preparation import prepare_asset_index

    _, server, url = indexed_server
    m, catalog, binding, config = inputs()
    binding["selector"] = {"name": "example_lexical", "version": "1"}
    config |= dict(url=url + "/index", total_ms=5000, request_ms=1000)
    record = asyncio.run(prepare_asset_index(config, binding, catalog))
    with server.registry.lock:
        server.registry.ready.clear()
    result = server.registry.exchange(
        dict(
            schema_version=1,
            preparation_id=record["preparation_id"],
            binding=binding,
            profile="cold",
            operation="status",
            catalog=None,
            budget_ms=None,
        )
    )
    assert result["state"] == "stale_catalog" and result["reason"] == "catalog_stale"
    assert result["binding"] == binding and result["index_revision"] is None


@pytest.mark.parametrize("ending", ["cancelled", "deadline", "build_error"])
def test_build_terminal_state_fences_publication(indexed_server, monkeypatch, ending):
    from relaymind.selection.asset_preparation import prepare_asset_index

    module, server, url = indexed_server
    entered = threading.Event()

    async def build(*args, context, **kwargs):
        entered.set()
        if ending == "build_error":
            raise RuntimeError("fixture build failure")
        while True:
            context.check()
            await asyncio.sleep(0.002)

    monkeypatch.setattr(module, "build_lexical_index", build)
    m, catalog, binding, config = inputs()
    binding["selector"] = {"name": "example_lexical", "version": "1"}
    config |= dict(url=url + "/index", total_ms=150, request_ms=100)
    record = asyncio.run(
        prepare_asset_index(
            config, binding, catalog, cancelled=lambda: ending == "cancelled" and entered.is_set()
        )
    )
    assert (
        record["status"]
        == {"cancelled": "cancelled", "deadline": "timeout", "build_error": "failed"}[ending]
    )
    if ending == "build_error":
        assert record["reason"] == "build_failed"
    assert not server.registry.ready


def test_offline_report_rejects_self_consistent_wrong_prepared_catalog(tmp_path, indexed_server):
    from relaymind.evaluation.asset_benchmark import (
        load_asset_benchmark_report,
        load_benchmark_config,
        refresh_fixture_benchmark_config,
        run_asset_benchmark,
        write_asset_benchmark_report,
    )
    from relaymind.evaluation.asset_comparison import compare_asset_reports

    _, server, url = indexed_server
    config = fixture_config(tmp_path, primary="http", url=url + "/select", repetitions=1, warmups=0)
    with config.path.open("a") as out:
        out.write('\n[preparation]\nmode="http"\nurl="' + url + '/index"\nprofile="cold"\n')
    refresh_fixture_benchmark_config(config.path)
    report = asyncio.run(run_asset_benchmark(load_benchmark_config(config.path)))
    path = tmp_path / "report.json"
    write_asset_benchmark_report(report, path)
    loaded = load_asset_benchmark_report(
        path, resolver=path.with_name(path.name + ".resolver.json")
    )
    assert compare_asset_reports(loaded, loaded, quality_only=True).data["status"] == "compatible"
    forged = copy.copy(loaded)
    forged.data = copy.deepcopy(loaded.data)
    forged.data["preparation"]["binding"]["catalog_digest"] = "0" * 64
    for exchange in forged.data["preparation"]["exchanges"]:
        if exchange["response"] is not None:
            exchange["response"]["binding"]["catalog_digest"] = "0" * 64
    assert compare_asset_reports(loaded, forged, quality_only=True).data["status"] == "incompatible"


def test_explicit_fixture_authoring_binds_foreign_unicode_to_actual_runtime(tmp_path):
    """RM40/AC31: only explicit disposable fixture authoring may change its UCD lock."""
    import unicodedata

    from relaymind.canonical import canonical_digest
    from relaymind.evaluation.asset_benchmark import (
        load_benchmark_config,
        refresh_fixture_benchmark_config,
    )

    config = fixture_config(tmp_path)
    lock_path = config.path.parent / "lock.json"
    lock = json.loads(lock_path.read_text())
    lock["unicode_version"] = "foreign-fixture-table"
    lock.pop("digest")
    lock["digest"] = canonical_digest(lock)
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="unicode_version_mismatch"):
        refresh_fixture_benchmark_config(config.path)
    original = (config.path.parent / "originals/calendar.md").read_bytes()
    refresh_fixture_benchmark_config(config.path, reauthor_fixture_unicode=True)
    loaded = load_benchmark_config(config.path)
    assert loaded.release.lock["unicode_version"] == unicodedata.unidata_version
    assert (config.path.parent / "originals/calendar.md").read_bytes() == original


@pytest.mark.parametrize("boundary", ["genuine", "frozen", "corrupt"])
def test_fixture_unicode_authoring_cannot_rewrite_real_frozen_or_corrupt_lock(tmp_path, boundary):
    from relaymind.canonical import canonical_digest
    from relaymind.evaluation.asset_benchmark import refresh_fixture_benchmark_config

    config = fixture_config(tmp_path)
    lock_path = config.path.parent / "lock.json"
    lock = json.loads(lock_path.read_text())
    if boundary == "genuine":
        lock["purpose"] = "acceptance"
    elif boundary == "frozen":
        lock.update(state="frozen", frozen_at="2026-10-30T00:00:00Z")
    lock.pop("digest")
    lock["digest"] = canonical_digest(lock)
    if boundary == "corrupt":
        lock["digest"] = "0" * 64
    lock_path.write_text(json.dumps(lock))
    before = lock_path.read_bytes()
    with pytest.raises(ValueError):
        refresh_fixture_benchmark_config(config.path, reauthor_fixture_unicode=True)
    assert lock_path.read_bytes() == before


def test_unprepared_indexed_primary_uses_existing_floor_without_claiming_ready(
    tmp_path, indexed_server
):
    """RM35/RM40 AC26/AC31: a floor result cannot manufacture primary preparation."""
    from relaymind.evaluation.asset_benchmark import run_asset_benchmark

    _, _, url = indexed_server
    report = asyncio.run(
        run_asset_benchmark(
            fixture_config(
                tmp_path,
                primary="http",
                url=url + "/select",
                fallback=True,
                repetitions=1,
                warmups=0,
            )
        )
    ).data
    assert report["preparation"]["status"] == "not_applicable"
    row = report["results"][0]
    assert row["status"] == "ready" and row["attempts"][0]["outcome"] == "not_ready"
    assert row["attempts"][1]["selector"]["name"] == "lexical"


def test_cold_reuse_profiles_are_incompatible_in_both_comparison_modes(tmp_path, indexed_server):
    """RM40/AC31: quality-only relaxes machine facts, never preparation policy."""
    from relaymind.evaluation.asset_benchmark import (
        load_asset_benchmark_report,
        load_benchmark_config,
        refresh_fixture_benchmark_config,
        run_asset_benchmark,
        write_asset_benchmark_report,
    )
    from relaymind.evaluation.asset_comparison import compare_asset_reports

    _, _, url = indexed_server
    reports = []
    for profile in ("cold", "reuse"):
        config = fixture_config(
            tmp_path / profile, primary="http", url=url + "/select", repetitions=1, warmups=0
        )
        with config.path.open("a") as out:
            out.write(
                '\n[preparation]\nmode="http"\nurl="' + url + '/index"\nprofile="' + profile + '"\n'
            )
        refresh_fixture_benchmark_config(config.path)
        report = asyncio.run(run_asset_benchmark(load_benchmark_config(config.path)))
        path = tmp_path / (profile + ".json")
        write_asset_benchmark_report(report, path)
        reports.append(
            load_asset_benchmark_report(path, resolver=path.with_name(path.name + ".resolver.json"))
        )
    for quality_only in (False, True):
        result = compare_asset_reports(*reports, quality_only=quality_only).data
        assert result["status"] == "incompatible"
        assert dict(field="/config/preparation/profile", reason="mismatch") in result["reasons"]
