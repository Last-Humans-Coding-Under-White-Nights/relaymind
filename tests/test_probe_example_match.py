"""The example-matching probe (#118; RM28, spec 0.31 sections 12.1, 12.2, and 12.4).

scripts/probes/probe_example_match.py is a throwaway spike: a stdlib BM25
matcher over the demo skills' example requests and descriptions, scored
on the corpus prompts against the skill sidecar. The scoring functions
are pure so that the production example_match selector can port them
(#132). These tests cover the pure functions on small inputs written for
the test, and the whole analysis on the checked-in corpus; no model is
called.
"""

import json
import math
import os
import subprocess
import sys

import pytest

from helpers import REPO_ROOT, load_script_module

SKILLS = REPO_ROOT / "skills"
LABEL = "offline analysis of recorded traces, not a measured run"


@pytest.fixture(scope="module")
def probe():
    return load_script_module("probes/probe_example_match")


# -- tokens and masking -----------------------------------------------------------------


def test_latin_text_is_split_into_lowercase_word_tokens(probe):
    assert probe.tokenize("Is staging-api healthy right now?") == [
        "is",
        "staging",
        "api",
        "healthy",
        "right",
        "now",
    ]


def test_chinese_text_is_split_into_character_bigrams(probe):
    assert probe.tokenize("查询健康状态") == ["查询", "询健", "健康", "康状", "状态"]


def test_a_single_chinese_character_is_kept_as_one_token(probe):
    assert probe.tokenize("删 db") == ["删", "db"]


def test_mixed_text_gives_words_and_bigrams_and_drops_punctuation(probe):
    assert probe.tokenize("告诉我 idmask 的健康情况。") == [
        "告诉",
        "诉我",
        "idmask",
        "的健",
        "健康",
        "康情",
        "情况",
    ]


def test_an_empty_or_symbol_only_text_has_no_tokens(probe):
    assert probe.tokenize("") == [] and probe.tokenize("?! ...") == []


def test_identifiers_are_masked_as_whole_tokens(probe):
    ids = ["staging-api", "payments-api"]
    assert probe.mask_identifiers("Is staging-api up? Compare payments-api.", ids) == (
        f"Is {probe.PLACEHOLDER} up? Compare {probe.PLACEHOLDER}."
    )
    # A longer ID that contains an identifier is another resource and stays.
    text = "Is staging-api.backup up? And my-staging-api?"
    assert probe.mask_identifiers(text, ids) == text
    assert (
        probe.mask_identifiers("staging-api 现在健康吗", ids) == f"{probe.PLACEHOLDER} 现在健康吗"
    )


def test_the_masked_identifiers_are_the_enum_values_of_the_allowed_tools(probe):
    catalog = probe.load_catalog(SKILLS)
    assert {s.name for s in catalog} == {
        "check-service-health",
        "delete-demo-database",
        "health-and-diagnostic-checklist",
        "investigate-performance",
        "lookup-latest-deployment",
        "summarize-incident-counts",
    }
    assert probe.catalog_identifiers(catalog) == frozenset({"staging-api", "payments-api"})
    check = next(s for s in catalog if s.name == "check-service-health")
    assert "Is staging-api healthy right now?" in check.requests
    assert any("现在健康吗" in request for request in check.requests)
    assert "health status" in check.description


# -- BM25 -------------------------------------------------------------------------------


@pytest.fixture
def index(probe):
    return probe.build_index(
        {
            "health": "Report the health status of a service. Is it healthy? Check health.",
            "deploy": "Report the latest deployment of a service. When was it deployed?",
            "count": "Count incidents of a service over a window of minutes.",
        },
        identifiers=[],
    )


def test_a_query_scores_highest_against_the_document_that_shares_its_words(probe, index):
    scores = probe.score_query(index, "Is the service healthy?")
    assert max(scores, key=scores.get) == "health"
    scores = probe.score_query(index, "latest deployment please")
    assert max(scores, key=scores.get) == "deploy"
    assert all(score >= 0 for score in scores.values())


def test_a_word_in_every_document_adds_little_and_an_unknown_word_adds_nothing(probe, index):
    common = probe.score_query(index, "service")
    rare = probe.score_query(index, "incidents")
    assert max(common.values()) < max(rare.values())
    assert probe.score_query(index, "zzzz qqqq") == {"health": 0.0, "deploy": 0.0, "count": 0.0}
    assert probe.score_query(index, "") == {"health": 0.0, "deploy": 0.0, "count": 0.0}


def test_scoring_matches_the_bm25_formula(probe):
    index = probe.build_index({"a": "red red blue", "b": "green"}, identifiers=[], k1=1.5, b=0.75)
    # N = 2, "red" is in one document: idf = ln(1 + (2 - 1 + 0.5) / (1 + 0.5)) = ln 2.
    # Document a has 3 tokens, the average length is 2, red occurs twice.
    idf = math.log(2)
    expected = idf * (2 * 2.5) / (2 + 1.5 * (1 - 0.75 + 0.75 * 3 / 2))
    assert probe.score_query(index, "red")["a"] == pytest.approx(expected)
    assert probe.score_query(index, "red")["b"] == 0.0
    # Each distinct query word counts once.
    assert probe.score_query(index, "red red") == probe.score_query(index, "red")


def test_masking_applies_to_documents_and_queries_alike(probe):
    index = probe.build_index(
        {"health": "Is staging-api healthy?", "deploy": "Which build is on payments-api?"},
        identifiers=["staging-api", "payments-api"],
    )
    scores = probe.score_query(index, "Is payments-api healthy?")
    assert max(scores, key=scores.get) == "health"
    assert "staging" not in index.idf and "payments" not in index.idf


# -- the decision -----------------------------------------------------------------------


def test_the_pick_needs_its_score_and_its_margin_to_the_second(probe):
    scores = {"a": 5.0, "b": 3.0, "c": 0.0}
    pick = probe.choose(scores, min_score=0.0, min_margin=0.0)
    assert (pick.skill, pick.score, pick.second, pick.margin) == ("a", 5.0, 3.0, 2.0)
    assert probe.choose(scores, min_score=5.0, min_margin=2.0).skill == "a"
    assert probe.choose(scores, min_score=5.1, min_margin=0.0).skill is None
    assert probe.choose(scores, min_score=0.0, min_margin=2.1).skill is None
    # The top of a tie has a margin of zero; ties are broken by name, so the result is stable.
    tie = probe.choose({"b": 2.0, "a": 2.0}, min_score=0.0, min_margin=0.0)
    assert (tie.skill, tie.margin) == ("a", 0.0)
    # No evidence at all is never a pick.
    assert probe.choose({"a": 0.0, "b": 0.0}, min_score=0.0, min_margin=0.0).skill is None
    assert probe.choose({}, min_score=0.0, min_margin=0.0).skill is None


# -- precision, recall, Jaccard (section 12.4) ------------------------------------------


@pytest.mark.parametrize(
    ("predicted", "expected", "values"),
    [
        ({"a"}, {"a"}, (1.0, 1.0, 1.0)),
        ({"b"}, {"a"}, (0.0, 0.0, 0.0)),
        (set(), {"a"}, (0.0, 0.0, 0.0)),
        ({"a", "b"}, {"a"}, (0.5, 1.0, 0.5)),
        ({"a"}, {"a", "b"}, (1.0, 0.5, 0.5)),
        (set(), set(), (0.0, 0.0, 0.0)),
    ],
)
def test_set_metrics_follow_the_spec_and_are_zero_for_an_empty_prediction(
    probe, predicted, expected, values
):
    result = probe.set_metrics(predicted, expected)
    assert (result["precision"], result["recall"], result["jaccard"]) == values


def test_macro_and_micro_averages_differ_when_a_selector_declines(probe):
    pairs = [({"a"}, {"a"}), ({"b"}, {"a"}), (set(), {"a"}), ({"a"}, {"a"})]
    result = probe.summarize_sets(pairs)
    assert result["cases"] == 4
    assert result["macro"] == {"precision": 0.5, "recall": 0.5, "jaccard": 0.5}
    # Micro: 2 right of 3 picks; 2 right of 4 expected; 2 shared of 5 in the unions.
    assert result["micro"]["precision"] == pytest.approx(2 / 3)
    assert result["micro"]["recall"] == pytest.approx(2 / 4)
    assert result["micro"]["jaccard"] == pytest.approx(2 / 5)
    assert (result["picks"], result["right"], result["wrong"], result["none"]) == (3, 2, 1, 1)


def test_an_empty_summary_has_no_averages(probe):
    result = probe.summarize_sets([])
    assert result["cases"] == 0 and result["macro"] is None and result["micro"] is None


# -- thresholds -------------------------------------------------------------------------


def threshold_rows():
    """(expected, scores) pairs: right picks have higher scores and margins than wrong ones."""
    return [
        ("a", {"a": 9.0, "b": 1.0}),
        ("a", {"a": 7.0, "b": 2.0}),
        ("b", {"b": 6.0, "a": 1.0}),
        ("a", {"a": 3.0, "b": 2.5}),  # a low-margin right pick
        ("b", {"a": 2.0, "b": 1.0}),  # a wrong pick, low score
        ("b", {"a": 4.0, "b": 3.5}),  # a wrong pick, low margin
    ]


def test_candidate_thresholds_keep_the_wrong_picks_at_or_below_the_limit(probe):
    rows = threshold_rows()
    best = probe.candidate_thresholds(rows, max_wrong=0)
    assert best["wrong"] == 0 and best["right"] == 3
    picks = [
        probe.choose(s, min_score=best["min_score"], min_margin=best["min_margin"]) for _, s in rows
    ]
    assert [p.skill == e for p, (e, _) in zip(picks, rows, strict=True)].count(True) == 3
    assert sum(1 for p, (e, _) in zip(picks, rows, strict=True) if p.skill not in (None, e)) == 0
    looser = probe.candidate_thresholds(rows, max_wrong=1)
    assert looser["wrong"] <= 1 and looser["right"] >= best["right"]
    # With room for both wrong picks, the pair keeps all four right picks at the fewest wrong
    # ones: the low-score wrong pick can be cut without losing a right pick, the other cannot.
    loosest = probe.candidate_thresholds(rows, max_wrong=len(rows))
    assert (loosest["right"], loosest["wrong"]) == (4, 1)


def test_no_thresholds_are_chosen_when_no_pair_meets_the_limit(probe):
    rows = [("a", {"b": 5.0, "a": 1.0})]
    assert probe.candidate_thresholds(rows, max_wrong=0)["right"] == 0
    assert probe.candidate_thresholds([], max_wrong=0) is None


def test_the_frontier_lists_the_right_picks_at_each_number_of_wrong_picks(probe):
    frontier = probe.frontier(threshold_rows())
    assert frontier[0]["wrong"] == 0
    assert [f["wrong"] for f in frontier] == sorted({f["wrong"] for f in frontier})
    assert [f["right"] for f in frontier] == sorted(f["right"] for f in frontier)


# -- the router's picks -----------------------------------------------------------------


def route_event(mechanism, skill, route="local"):
    return {
        "event_type": "route.selected",
        "payload": {"mechanism": mechanism, "decision": {"skill_id": skill, "route": route}},
    }


def test_router_picks_are_the_first_native_function_calling_decision_of_the_run(probe):
    document = {
        "grades": [
            {"case_id": "simple-01", "mode": "auto", "run_id": "r1"},
            {"case_id": "simple-01", "mode": "local_only", "run_id": "r2"},
            {"case_id": "simple-02", "mode": "auto", "run_id": "r3"},
            {"case_id": "simple-02", "mode": "external_only", "run_id": "r4"},
            {"case_id": "simple-03", "mode": "auto", "run_id": "r5"},
        ],
        "traces": {
            "r1": [
                route_event("native_function_calling", "check-service-health"),
                route_event("clarification_resume", "lookup-latest-deployment"),
            ],
            "r2": [route_event("native_function_calling", "health-and-diagnostic-checklist")],
            "r3": [route_event("native_function_calling", "check-service-health", "clarify")],
            "r4": [route_event("native_function_calling", "check-service-health")],
            "r5": [{"event_type": "run.failed", "payload": {}}],
        },
    }
    picks = probe.router_picks(document)
    assert picks == {
        ("simple-01", "auto"): "check-service-health",
        ("simple-01", "local_only"): "health-and-diagnostic-checklist",
        ("simple-02", "auto"): "check-service-health",
        ("simple-03", "auto"): None,
    }


# -- the analysis on the checked-in corpus ----------------------------------------------


@pytest.fixture(scope="module")
def analysis(probe):
    return probe.analyse(probe.load_corpus_and_sidecar(), probe.load_catalog(SKILLS), export=None)


def test_the_analysis_scores_the_fifty_two_corpus_prompts(analysis):
    assert analysis["label"] == LABEL
    cases = analysis["cases"]
    assert len(cases) == 52
    assert {c["language"] for c in cases} == {"en", "zh"}
    by_language = analysis["argmax"]["by_language"]
    assert (by_language["en"]["cases"], by_language["zh"]["cases"]) == (40, 12)
    for part in by_language.values():
        assert part["right"] + part["wrong"] + part["none"] == part["cases"]
        assert part["picks"] == part["right"] + part["wrong"]


def test_the_analysis_is_reported_per_language_and_class_and_never_merges_chinese(analysis):
    table = analysis["argmax"]["by_language_and_class"]
    keys = {(row["language"], row["case_class"]) for row in table}
    assert ("en", "simple") in keys and ("zh", "simple") in keys
    assert ("zh", "complex") not in keys
    assert sum(row["cases"] for row in table if row["language"] == "en") == 40
    assert sum(row["cases"] for row in table if row["language"] == "zh") == 12


def test_the_distributions_separate_right_and_wrong_picks(analysis):
    dist = analysis["distributions"]
    assert set(dist) == {"en", "zh"}
    for language, picks in dist.items():
        assert set(picks) == {"right", "wrong"}
        for part in picks.values():
            assert set(part) >= {"count", "score", "margin"}
        total = picks["right"]["count"] + picks["wrong"]["count"]
        assert total == analysis["argmax"]["by_language"][language]["picks"]


def test_the_analysis_is_deterministic(probe, analysis):
    again = probe.analyse(probe.load_corpus_and_sidecar(), probe.load_catalog(SKILLS), export=None)
    assert json.dumps(again, sort_keys=True) == json.dumps(analysis, sort_keys=True)


def test_without_an_export_there_is_no_router_baseline(analysis):
    assert analysis["router"] is None and analysis["candidate"] is None


def test_the_router_baseline_and_the_candidate_thresholds_come_from_the_export(probe):
    """The wrong picks of the router on the same cases limit the thresholds (issue #118)."""
    grades = [
        {"case_id": "simple-01", "mode": "auto", "run_id": "r1"},
        {"case_id": "simple-02", "mode": "auto", "run_id": "r2"},
        {"case_id": "simple-05", "mode": "auto", "run_id": "r3"},
        {"case_id": "simple-01", "mode": "local_only", "run_id": "r4"},
        {"case_id": "simple-02", "mode": "local_only", "run_id": "r5"},
    ]
    native = "native_function_calling"
    traces = {
        "r1": [route_event(native, "check-service-health")],
        "r2": [route_event(native, "lookup-latest-deployment")],
        "r3": [{"event_type": "run.failed", "payload": {}}],
        "r4": [route_event(native, "check-service-health")],
        "r5": [route_event(native, "check-service-health")],
    }
    document = {"grades": grades, "traces": traces}
    result = probe.analyse(
        probe.load_corpus_and_sidecar(), probe.load_catalog(SKILLS), export=document
    )
    assert (result["router"]["cases_in_both"], result["router"]["samples_differ"]) == (2, 1)
    samples = result["router"]["samples"]
    assert set(samples) == {"auto", "local_only"}
    auto = samples["auto"]["by_language"]["en"]
    local = samples["local_only"]["by_language"]["en"]
    assert (auto["cases"], auto["right"], auto["wrong"], auto["none"]) == (3, 1, 1, 1)
    assert (local["cases"], local["right"], local["wrong"], local["none"]) == (2, 2, 0, 0)
    candidate = result["candidate"]
    assert candidate["language"] == "en"
    assert candidate["router_wrong_picks"] == {"auto": 1, "local_only": 0}
    assert candidate["max_wrong"] == 0
    assert candidate["english"]["wrong"] <= candidate["max_wrong"]
    assert candidate["thresholds"]["min_score"] >= 0 and candidate["frontier"]
    assert candidate["chinese"]["cases"] == 12


def test_the_cli_writes_the_analysis_and_a_report(probe, tmp_path):
    out = tmp_path / "example-match.json"
    probe.main(["--out", str(out)])
    written = json.loads(out.read_text("utf-8"))
    assert written["label"] == LABEL
    assert set(written["analysed_with"]["application"]) == {"head", "dirty"}
    report = out.with_suffix(".md").read_text("utf-8")
    assert LABEL in report.splitlines()[0]
    assert "precision" in report and "Jaccard" in report
    # Chinese is reported apart: one total row per language, none for both.
    assert "| en all |" in report and "| zh all |" in report


def test_the_probe_does_not_change_the_corpus_or_the_sidecar(probe, tmp_path):
    before = {
        path: path.read_bytes()
        for path in (
            REPO_ROOT / "evaluation" / "corpus.v1.json",
            REPO_ROOT / "evaluation" / "corpus.v1.skills.json",
            REPO_ROOT / "evaluation" / "corpus.v1.lock.json",
        )
    }
    probe.main(["--out", str(tmp_path / "x.json")])
    assert {path: path.read_bytes() for path in before} == before


def test_the_result_does_not_depend_on_the_hash_seed(tmp_path):
    """BM25 sums over a set of words; the order of the sum must not change the last bits."""
    export = tmp_path / "export.json"
    export.write_text(
        json.dumps(
            {
                "grades": [{"case_id": "simple-02", "mode": "auto", "run_id": "r1"}],
                "traces": {"r1": [route_event("native_function_calling", "check-service-health")]},
            }
        ),
        "utf-8",
    )
    outputs = set()
    for seed in ("1", "2", "3", "4", "5", "6"):
        out = tmp_path / f"seed-{seed}.json"
        script = REPO_ROOT / "scripts" / "probes" / "probe_example_match.py"
        subprocess.run(
            [sys.executable, str(script), "--export", str(export), "--out", str(out)],
            check=True,
            capture_output=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        document = json.loads(out.read_text("utf-8"))
        document.pop("analysed_with")
        outputs.add(json.dumps(document, sort_keys=True))
    assert len(outputs) == 1
