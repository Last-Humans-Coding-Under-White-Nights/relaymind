"""The frozen correctness corpus (spec 0.28 section 12.2, #32; RM25).

Section 12.2: 40 English scenarios (12 simple, 10 complex, 6 ambiguous,
6 mutation-confirmation, 6 adversarial or fault); Chinese versions are
best-effort and reported apart. Each case has fixture state, acceptable
tools and arguments, required evidence, expected permissions, and a
grading rubric. The corpus is frozen by a digest before candidates are
compared, and development examples are kept separate.
"""

import copy
import json
import re
from pathlib import Path

import pytest

from relaymind.evaluation import corpus as corpus_module
from relaymind.evaluation.corpus import (
    CLASSES,
    ENGLISH_CLASS_SIZES,
    CorpusError,
    check_lock,
    corpus_digest,
    development_prompts,
    load_corpus,
    lock_document,
    parse_corpus,
)
from relaymind.tools.sandbox import SandboxDataService

REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "evaluation" / "corpus.v1.json"
LOCK = REPO / "evaluation" / "corpus.v1.lock.json"
SEEDS = REPO / "tests" / "fixtures" / "sandbox"


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(CORPUS, seed_dir=SEEDS)


def document():
    return json.loads(CORPUS.read_text("utf-8"))


def test_the_english_corpus_has_forty_cases_in_the_section_12_2_classes(corpus):
    english = [case for case in corpus.cases if case.language == "en"]
    assert len(english) == 40
    counts = {name: sum(1 for c in english if c.case_class == name) for name in CLASSES}
    assert (
        counts
        == ENGLISH_CLASS_SIZES
        == {
            "simple": 12,
            "complex": 10,
            "ambiguous": 6,
            "mutation": 6,
            "adversarial": 6,
        }
    )


def test_every_case_has_the_required_fields(corpus):
    """Fixture state, acceptable tools, evidence, permissions, and a rubric (section 12.2)."""
    for case in corpus.cases:
        assert case.fixture in corpus.seeds
        assert case.permissions and set(case.permissions) <= {"demo.read", "demo.mutate"}
        assert case.rubric.strip()
        assert case.expected_outcomes
        assert case.expected_route in ("local", "external", "clarify")


def test_chinese_cases_are_best_effort_versions_of_english_cases(corpus):
    by_id = {case.case_id: case for case in corpus.cases}
    chinese = [case for case in corpus.cases if case.language == "zh"]
    assert chinese, "the corpus keeps best-effort Chinese versions (section 12.2)"
    for case in chinese:
        assert case.best_effort is True
        english = by_id[case.english_case]
        assert english.language == "en" and english.case_class == case.case_class
        assert case.required_calls == english.required_calls
        assert case.fixture == english.fixture
    assert all(not c.best_effort for c in corpus.cases if c.language == "en")


def test_case_ids_are_unique(corpus):
    ids = [case.case_id for case in corpus.cases]
    assert len(ids) == len(set(ids))


def test_mutation_cases_need_the_mutate_permission_and_a_decision(corpus):
    """RM11: every mutation case is run with demo.mutate and a confirmation decision."""
    for case in corpus.cases:
        if case.case_class == "mutation":
            assert "demo.mutate" in case.permissions
            assert case.confirmation in ("approve", "deny")
            assert case.expected_databases is not None


def test_read_only_cases_run_without_the_mutate_permission(corpus):
    """RM08: only a case that asks for a delete (it has a decision) gets demo.mutate."""
    for case in corpus.cases:
        tools = {p["tool"] for group in case.required_calls for p in group}
        tools |= {p["tool"] for p in case.allowed_calls}
        if "delete_demo_database" not in tools and case.confirmation is None:
            assert case.permissions == ("demo.read",), case.case_id


def test_ambiguous_cases_carry_the_clarification_answer(corpus):
    for case in corpus.cases:
        if case.case_class == "ambiguous":
            assert case.expect_clarification and case.clarification_answer


def test_evidence_patterns_compile(corpus):
    for case in corpus.cases:
        for group in case.required_evidence:
            for pattern in group:
                re.compile(pattern)
        for pattern in case.forbidden_evidence:
            re.compile(pattern)


def test_every_seed_loads_in_the_sandbox(corpus, tmp_path):
    """The fixture snapshot of each case is a valid section 9 seed."""
    for name in corpus.seeds:
        service = SandboxDataService(SEEDS / f"{name}.json", work_dir=tmp_path / name)
        assert service.scenario


def test_no_case_repeats_a_development_example(corpus):
    """Section 12.2: development examples are kept separate from the corpus."""
    harness_prompts = [
        "Is staging-api healthy right now?",
        "Delete the sandbox database demo-orders-db.",
        "Why is payments-api so slow right now?",
        "Check payments-api health and give me a checklist for diagnosing it.",
        "Check the health of staging-api.",
    ]
    examples = development_prompts(REPO / "skills", extra=harness_prompts)
    assert len(examples) > 10
    for case in corpus.cases:
        assert corpus_module.normalize_prompt(case.prompt) not in examples, case.case_id


def test_the_corpus_matches_its_frozen_digest(corpus):
    """The corpus is frozen before candidate comparison: the lock names its digest."""
    lock = json.loads(LOCK.read_text("utf-8"))
    assert lock["sha256"] == corpus.digest
    check_lock(corpus, LOCK)


def test_a_changed_case_no_longer_matches_the_lock(tmp_path):
    changed = document()
    changed["cases"][0]["prompt"] += " Please."
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(changed), "utf-8")
    with pytest.raises(CorpusError, match="digest"):
        check_lock(load_corpus(path, seed_dir=SEEDS), LOCK)


def test_a_changed_seed_changes_the_digest():
    doc = document()
    seeds = {
        name: json.loads((SEEDS / f"{name}.json").read_text("utf-8"))
        for name in {c["fixture"] for c in doc["cases"]}
    }
    before = corpus_digest(doc, seeds)
    seeds["baseline"]["revision"] += 1
    assert corpus_digest(doc, seeds) != before


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c: c.update(extra=1), "unknown field"),
        (lambda c: c.pop("rubric"), "missing field"),
        (lambda c: c.update({"class": "easy"}), "class"),
        (lambda c: c.update(language="fr"), "language"),
        (lambda c: c.update(fixture="nope"), "fixture"),
        (lambda c: c.update(permissions=["root"]), "permission"),
        (lambda c: c.update(expected_outcomes=["waiting_input"]), "outcome"),
        (lambda c: c.update(confirmation="maybe"), "confirmation"),
        (lambda c: c.update(required_evidence=[["("]]), "pattern"),
        (lambda c: c.update(required_evidence=["Healthy"]), "list of lists of strings"),
        (lambda c: c.update(required_evidence="Healthy"), "list of lists of strings"),
        (lambda c: c.update(forbidden_evidence="secret"), "forbidden_evidence"),
        (lambda c: c.update(required_calls=[[{"tool": "rm_rf", "arguments": {}}]]), "tool"),
        (lambda c: c.update(rubric=" "), "rubric"),
    ],
)
def test_invalid_cases_are_refused(change, message):
    doc = document()
    broken = copy.deepcopy(doc)
    change(broken["cases"][0])
    with pytest.raises(CorpusError, match=message):
        parse_corpus(broken, seed_dir=SEEDS)


def test_a_wrong_class_size_is_refused():
    doc = document()
    doc["cases"] = [c for c in doc["cases"] if c["id"] != "simple-01"]
    with pytest.raises(CorpusError, match="simple"):
        parse_corpus(doc, seed_dir=SEEDS)


def test_a_duplicate_case_id_is_refused():
    doc = document()
    doc["cases"].append(copy.deepcopy(doc["cases"][0]))
    with pytest.raises(CorpusError, match="duplicate"):
        parse_corpus(doc, seed_dir=SEEDS)


def test_a_chinese_case_must_name_an_english_case():
    doc = document()
    zh = next(c for c in doc["cases"] if c["language"] == "zh")
    zh["english_case"] = "missing-01"
    with pytest.raises(CorpusError, match="english_case"):
        parse_corpus(doc, seed_dir=SEEDS)


def test_the_lock_file_is_the_lock_document_of_the_corpus(corpus):
    """The lock names the corpus, its version, the digest, and each seed's digest."""
    assert json.loads(LOCK.read_text("utf-8")) == lock_document(corpus, "corpus.v1.json")
