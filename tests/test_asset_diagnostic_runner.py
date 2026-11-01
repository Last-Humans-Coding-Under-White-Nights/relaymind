"""RM35/RM40/RM44: shared full-catalog development runs retain misses and user choice."""

import asyncio
import copy
import importlib
import json
import threading
from pathlib import Path

import pytest

from helpers import FakeClock
from relaymind.canonical import canonical_digest
from relaymind.store import TraceClock
from test_asset_corpus import read, write
from test_asset_diagnostic import development_input as development_input
from test_asset_diagnostic import load, save_input


def api():
    try:
        return importlib.import_module("relaymind.evaluation.asset_diagnostic_runner")
    except ModuleNotFoundError:
        pytest.fail("RM40 shared source-grounded benchmark runner is missing")


def production(root, *, http=False, fallback=False, primary_ms=150):
    path = root / ("http.toml" if http else "lexical.toml")
    path.write_text(
        """[external_model]
base_url="https://unused.example.invalid/v1"
model="unused-by-retrieval"
context_limit=4096
[assets]
sources=[{source_id="fixture",kind="metadata_import",path="catalog.json"}]
[recommendations]
enabled=true
user_id="source-benchmark"
workspace_id="disposable"
total_ms=500
"""
        + f"primary_ms={primary_ms}\nfallback={str(fallback).lower()}\n"
        + (
            'primary="http"\nname="example_lexical"\nversion="1"\nurl="http://127.0.0.1:8913/select"\n'
            if http
            else 'primary="lexical"\n'
        ),
        encoding="utf-8",
        newline="\n",
    )
    return path


def english_case(root):
    doc = json.loads((root / "diagnostic.json").read_text(encoding="utf-8"))
    doc["cases"][0].update(task_text="Inspect my calendar", query_language="en", lane="en_to_en")
    save_input(root, doc)


def run(root, config=None, **options):
    options.setdefault("clock", FakeClock())
    return asyncio.run(
        api().run_source_benchmark(
            load(root), production_config=config or production(root), **options
        )
    )


def test_rm40_cn_query_to_english_original_miss_remains_in_language_pair_counts(development_input):
    report = run(development_input)
    assert report["profile"] == "source_grounded_development"
    assert report["qualification"] == "provisional" and report["acceptance_eligible"] is False
    assert len(report["rows"]) == 1 and report["rows"][0]["status"] == "ready"
    assert report["rows"][0]["grade"]["hit3"] is False
    assert report["rows"][0]["hard_subset"] == "no_shared_latin"
    assert report["rows"][0]["latin_token_intersections"] == {"calendar": []}
    assert report["summary"]["positive_cases"] == 1 and report["summary"]["hit3"] == 0
    assert report["language_pairs"][0]["query_language"] == "zh"
    assert report["language_pairs"][0]["target_description_language"] == "en"
    assert report["language_pairs"][0]["positive_cases"] == 1


def test_rm35_low_confidence_exposes_choice_and_records_only_explicit_user_selection(
    development_input,
):
    root = development_input
    english_case(root)
    report = run(root, user_choices={"cn-en": "calendar"})
    row = report["rows"][0]
    assert row["result"]["final_decision"] == "choice_required"
    assert row["result"]["final_candidates"][0]["asset_id"] == "calendar"
    assert row["user_choice"] == "calendar"
    assert report["summary"]["choice_supported3"] == 1
    assert report["summary"]["choice_required"] == 1
    assert report["summary"]["confident"] == 0
    assert report["summary"]["user_choices"] == 1
    assert run(root)["rows"][0]["user_choice"] is None


def test_rm40_invalid_user_choice_never_invents_an_admitted_choice(development_input):
    root = development_input
    english_case(root)
    with pytest.raises(ValueError, match="invalid_user_choice"):
        run(root, user_choices={"cn-en": "not-offered"})


def test_rm40_repetition_keeps_case_counts_and_all_planned_rows(development_input):
    root = development_input
    english_case(root)
    report = run(root, repetitions=3)
    assert len(report["rows"]) == 3
    assert report["summary"]["positive_cases"] == 1
    assert report["summary"]["hit3"] == 1
    assert report["planned_slots"] == 3
    assert len(report["costs"]) > 0


def test_rm35_timeout_retains_failed_row_and_is_not_abstention(development_input, monkeypatch):
    class Slow:
        name = "example_lexical"
        version = "1"
        builtin = True
        mechanism = "slow_test"

        async def select(self, request, context):
            await asyncio.sleep(0.1)

    monkeypatch.setattr(api(), "build_asset_selector", lambda settings: Slow())
    root = development_input
    english_case(root)
    report = run(root, config=production(root, http=True, primary_ms=1), clock=TraceClock())
    assert report["rows"][0]["status"] == "failed"
    assert report["rows"][0]["observed_ms"] is not None
    assert report["summary"]["failed"] == 1 and report["summary"]["abstentions"] == 0
    assert report["summary"]["positive_cases"] == 1 and report["summary"]["hit3"] == 0


def test_rm40_comparison_refuses_changed_cases_or_catalog(development_input):
    root = development_input
    report = run(root)
    assert api().compare_source_benchmarks(report, copy.deepcopy(report))["compatible"] is True
    for field in ("catalog_digest", "cases_digest", "policy_digest"):
        changed = copy.deepcopy(report)
        changed[field] = "0" * 64
        with pytest.raises(ValueError, match="incompatible_development_runs"):
            api().compare_source_benchmarks(report, changed)


def test_rm35_http_team_receives_same_full_catalog_without_reference_labels(development_input):
    import importlib.util

    root = development_input
    english_case(root)
    catalog = read(root, "catalog")
    decoy = copy.deepcopy(catalog["assets"][0])
    record = decoy["record"]
    record.update(
        asset_id="decoy",
        kind="mcp_tool",
        description="Manage unrelated repository issues.",
        identity=dict(
            server_id="fixture-server",
            tool_name="issues",
            exposed_name="issues",
            input_schema={"type": "object"},
            schema_hash=canonical_digest({"type": "object"}),
        ),
    )
    catalog["assets"].append(decoy)
    write(root, "catalog", catalog)
    doc = json.loads((root / "diagnostic.json").read_text(encoding="utf-8"))
    doc["catalog_digest"] = canonical_digest(catalog)
    save_input(root, doc)
    spec = importlib.util.spec_from_file_location(
        "source_benchmark_example", Path(__file__).parents[1] / "scripts/example_asset_selector.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    requests = []
    select = module.selection

    async def observed(value):
        requests.append(value)
        return await select(value)

    module.selection = observed
    with module.make_server("127.0.0.1", 0) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        config = production(root, http=True)
        config.write_text(
            config.read_text(encoding="utf-8").replace(":8913/", f":{server.server_port}/"),
            encoding="utf-8",
            newline="\n",
        )
        try:
            report = run(root, config=config)
        finally:
            server.shutdown()
            worker.join(timeout=2)
    assert report["rows"][0]["status"] == "ready"
    assert {r["asset_id"] for r in requests[0]["catalog"]} == {"calendar", "decoy"}
    assert "acceptable_asset_ids" not in requests[0] and "reference_kind" not in requests[0]
    assert report["rows"][0]["request"] == requests[0]
    assert report["coverage"]["catalog_assets"] == 2
    assert report["coverage"]["targeted_assets"] == 1
    assert report["missing_language_pairs"]
    builtin = run(root)
    assert (
        report["rows"][0]["offered_projection_digest"]
        == builtin["rows"][0]["offered_projection_digest"]
    )
    assert api().compare_source_benchmarks(builtin, report)["compatible"] is True


def test_rm40_comparison_recomputes_references_and_grades_instead_of_trusting_summary(
    development_input,
):
    report = run(development_input)
    changed = copy.deepcopy(report)
    changed["rows"][0]["grade"]["hit3"] = True
    changed["summary"]["hit3"] = 1
    with pytest.raises(ValueError, match="invalid_development_report"):
        api().compare_source_benchmarks(report, changed)


def test_rm40_report_byte_limit_aborts_explicitly_without_partial_report(
    development_input, monkeypatch
):
    monkeypatch.setattr(api(), "REPORT_BYTES", 1000, raising=False)
    with pytest.raises(ValueError, match="report_limit"):
        run(development_input)


def test_rm40_comparison_recomputes_language_pair_results(development_input):
    report = run(development_input)
    changed = copy.deepcopy(report)
    changed["language_pairs"][0]["hit3"] = 1
    with pytest.raises(ValueError, match="invalid_development_report"):
        api().compare_source_benchmarks(report, changed)


def test_rm40_latency_includes_failed_and_cancelled_work_without_zeroing_unknown():
    rows = [
        {"status": "ready", "observed_ms": 1},
        {"status": "failed", "observed_ms": 9},
        {"status": "cancelled", "observed_ms": 5},
        {"status": "not_run", "observed_ms": None},
    ]
    assert api().summarize_source_latency(rows) == {
        "known": 3,
        "unknown": 1,
        "p50_ms": 5,
        "p95_ms": 9,
    }


@pytest.mark.parametrize("tamper", ["evaluated", "hard_subset", "reference"])
def test_rm40_compare_rejects_favorable_duplicates_contradicting_actual_miss(
    development_input, tamper
):
    """RM40/RM44: recalculated summaries cannot turn an admitted miss into a hit."""
    report = run(development_input)
    assert report["rows"][0]["result"]["final_decision"] == "no_match"
    changed = copy.deepcopy(report)
    row = changed["rows"][0]
    if tamper == "evaluated":
        row.update(
            evaluated_candidates=[{"asset_id": "calendar"}],
            evaluated_decision="choice_required",
            user_choice="calendar",
        )
    elif tamper == "hard_subset":
        row.update(hard_subset="shared_latin", latin_token_intersections={"calendar": ["invented"]})
    else:
        row.update(reference_kind="invented")
    changed["summary"] = api()._summary(changed["rows"])
    changed["language_pairs"] = api()._language_pairs(changed["rows"], changed["catalog_document"])
    with pytest.raises(ValueError, match="invalid_development_report"):
        api().compare_source_benchmarks(report, changed)


def test_rm40_compare_rejects_erased_failed_primary_with_retained_floor_result(
    development_input, monkeypatch
):
    """RM35/RM40: the admitted floor result retains its consumed failed primary."""

    class Slow:
        # The timeout double retains the configured HTTP transport identity.
        name, version, builtin, mechanism = "example_lexical", "1", False, "http"

        async def select(self, request, context):
            await asyncio.sleep(0.1)

    monkeypatch.setattr(api(), "build_asset_selector", lambda settings: Slow())
    english_case(development_input)
    report = run(
        development_input,
        config=production(development_input, http=True, fallback=True, primary_ms=1),
        clock=TraceClock(),
    )
    assert len(report["rows"][0]["result"]["attempts"]) == 2
    assert api().compare_source_benchmarks(report, report)["compatible"]
    changed = copy.deepcopy(report)
    changed["rows"][0]["attempts"] = []
    changed["summary"] = api()._summary(changed["rows"])
    changed["language_pairs"] = api()._language_pairs(changed["rows"], changed["catalog_document"])
    changed["costs"] = api().summarize_asset_costs(changed["rows"])
    with pytest.raises(ValueError, match="invalid_development_report"):
        api().compare_source_benchmarks(report, changed)


def test_rm35_compare_rejects_user_selection_without_admitted_visible_choice(development_input):
    """RM35/RM40: a real no-match never supplies a user shortlist."""
    report = run(development_input)
    changed = copy.deepcopy(report)
    changed["rows"][0]["user_choice"] = "calendar"
    changed["summary"] = api()._summary(changed["rows"])
    changed["language_pairs"] = api()._language_pairs(changed["rows"], changed["catalog_document"])
    with pytest.raises(ValueError, match="invalid_development_report"):
        api().compare_source_benchmarks(report, changed)


@pytest.mark.parametrize("completed", [0, 1])
def test_rm40_cancellation_conserves_planned_slots_and_consumed_attempts(
    development_input, monkeypatch, completed
):
    """RM35/RM40: interruption exports current evidence and unknown future slots."""
    english_case(development_input)
    dispatched = asyncio.Event()
    drained = []
    calls = []
    lexical = api().LexicalAssetSelector

    class Interruptible(lexical):
        async def select(self, request, context):
            calls.append(request)
            if len(calls) <= completed:
                return await super().select(request, context)
            dispatched.set()
            try:
                await asyncio.sleep(60)
            finally:
                drained.append(True)

    monkeypatch.setattr(api(), "LexicalAssetSelector", Interruptible)
    # The machine facts are real but collected before the timed window: on
    # Windows the operating system queries alone can take seconds, and the
    # 2 s guards below bound only dispatch and cancellation.
    environment = api().collect_benchmark_environment(
        production=api()._load_production_config(production(development_input))
    )
    monkeypatch.setattr(
        api(), "collect_benchmark_environment", lambda *, production: copy.deepcopy(environment)
    )

    async def interrupt():
        task = asyncio.create_task(
            api().run_source_benchmark(
                load(development_input),
                clock=FakeClock(),
                production_config=production(development_input),
                repetitions=3,
            )
        )
        await asyncio.wait_for(dispatched.wait(), timeout=2)
        task.cancel()
        return await asyncio.wait_for(task, timeout=2)

    report = asyncio.run(interrupt())
    assert report["status"] == "interrupted"
    assert len(report["rows"]) == report["planned_slots"] == 3
    assert [r["status"] for r in report["rows"]] == ["ready"] * completed + ["cancelled"] + [
        "not_run"
    ] * (2 - completed)
    current = report["rows"][completed]
    assert current["attempts"][0]["outcome"] == "cancelled"
    # RM40: a coarse monotonic clock may report zero for consumed work.
    assert current["observed_ms"] is not None and current["observed_ms"] >= 0
    assert current["terminal_observation"]["attempts"] == current["attempts"]
    for future in report["rows"][completed + 1 :]:
        assert future["observed_ms"] is None
        assert future["attempts"] == [] and future["observed_costs"] == []
    assert report["latency"]["unknown"] == 2 - completed
    assert report["latency"]["known"] == completed + 1
    assert drained == [True]
    assert report["environment"] == environment
    assert api().compare_source_benchmarks(report, copy.deepcopy(report))["compatible"]


def test_rm40_compare_rejects_final_decision_not_produced_by_the_admitted_attempt(
    development_input,
):
    """RM35/RM40: an edited confident decision cannot replace the admitted choice_required."""
    from relaymind.evaluation.asset_metrics import grade_asset_result

    english_case(development_input)
    report = run(development_input)
    row = report["rows"][0]
    assert row["result"]["final_decision"] == "choice_required"
    assert row["result"]["attempts"][-1]["result"]["decision"] == "choice_required"
    changed = copy.deepcopy(report)
    row = changed["rows"][0]
    for key in ("decision", "base_decision", "final_decision"):
        row["result"][key] = "confident"
    row["evaluated_decision"] = "confident"
    case = next(c for c in changed["input_document"]["cases"] if c["id"] == row["slot"]["case_id"])
    row["grade"] = grade_asset_result(
        {"class": case["reference_class"], "acceptable_asset_ids": case["acceptable_asset_ids"]},
        row["result"],
    )
    changed["summary"] = api()._summary(changed["rows"])
    changed["language_pairs"] = api()._language_pairs(changed["rows"], changed["catalog_document"])
    with pytest.raises(ValueError, match="invalid_development_report"):
        api().compare_source_benchmarks(report, changed)


def test_rm40_a_non_empty_decision_without_attempts_is_not_produced():
    assert (
        api()._produced_by_admitted_attempt(
            {
                "decision": "confident",
                "attempts": [],
                "base_decision": "confident",
                "base_candidates": [],
            }
        )
        is False
    )


def test_rm40_compare_rejects_malformed_effective_sources_with_matching_digest(
    development_input,
):
    report = run(development_input)
    changed = copy.deepcopy(report)
    changed["effective_config"]["sources"] = [None]
    changed["config_digest"] = canonical_digest(changed["effective_config"])
    with pytest.raises(ValueError, match="invalid_development_report"):
        api().compare_source_benchmarks(report, changed)
