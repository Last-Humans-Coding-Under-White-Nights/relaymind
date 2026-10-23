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

from relaymind.canonical import canonical_digest
from relaymind.evaluation import corpus as corpus_module
from relaymind.evaluation.corpus import (
    CLASSES,
    ENGLISH_CLASS_SIZES,
    CorpusError,
    check_lock,
    corpus_digest,
    demo_skill_names,
    development_prompts,
    load_corpus,
    load_skills_sidecar,
    lock_document,
    parse_corpus,
    parse_skills_sidecar,
)
from relaymind.tools.sandbox import SandboxDataService

REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "evaluation" / "corpus.v1.json"
LOCK = REPO / "evaluation" / "corpus.v1.lock.json"
SIDECAR = REPO / "evaluation" / "corpus.v1.skills.json"
SEEDS = REPO / "tests" / "fixtures" / "sandbox"
SKILLS = REPO / "skills"
# The digest that froze corpus version 1 (2026-10-22); a sidecar never changes it.
FROZEN_CORPUS_SHA256 = "83023ab1ae1d29508caf367d28a4adbb0a4e4d93e977ea4628d94823ecd5d3bd"


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
        (lambda c: c.update(best_effort="false"), "best_effort must be a boolean"),
        (lambda c: c.update(expect_clarification="false"), "expect_clarification must be"),
        (lambda c: c.update(clarification_answer=["staging"]), "clarification_answer must be"),
        (lambda c: c.update(english_case=["simple-01"]), "english_case must be"),
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
    """The lock names the corpus, its digest, and the digest of each seed and each sidecar."""
    sidecars = {SIDECAR.name: json.loads(SIDECAR.read_text("utf-8"))}
    assert json.loads(LOCK.read_text("utf-8")) == lock_document(
        corpus, "corpus.v1.json", sidecars=sidecars
    )


def test_a_lock_without_sidecars_is_the_earlier_lock_document(corpus):
    """The sidecars key is optional: a lock made before spec 0.31 has none."""
    document = lock_document(corpus, "corpus.v1.json")
    assert "sidecars" not in document
    assert set(document) == {"corpus", "version", "sha256", "seeds", "frozen_on"}


# -- the skill sidecar (RM28, spec 0.31 sections 12.1 and 12.2, #118) ----------------------


def sidecar():
    return json.loads(SIDECAR.read_text("utf-8"))


def expected_skills(corpus, document=None):
    return parse_skills_sidecar(document or sidecar(), corpus, skill_names=demo_skill_names(SKILLS))


def test_the_demo_skill_names_are_the_six_skill_packages():
    assert demo_skill_names(SKILLS) == frozenset(
        {
            "check-service-health",
            "delete-demo-database",
            "health-and-diagnostic-checklist",
            "investigate-performance",
            "lookup-latest-deployment",
            "summarize-incident-counts",
        }
    )


def test_the_sidecar_names_every_corpus_case_once(corpus):
    """Spec 12.2: the expected skill of every corpus case, and nothing else."""
    skills = load_skills_sidecar(SIDECAR, corpus, skill_names=demo_skill_names(SKILLS))
    assert sorted(skills) == sorted(case.case_id for case in corpus.cases)
    assert len(skills) == 52


def test_every_sidecar_value_is_a_demo_skill_or_null(corpus):
    names = demo_skill_names(SKILLS)
    assert all(value is None or value in names for value in expected_skills(corpus).values())


def test_every_demo_skill_is_expected_by_some_case(corpus):
    """A skill no case expects could never be measured."""
    assert set(expected_skills(corpus).values()) >= demo_skill_names(SKILLS)


def test_a_chinese_case_expects_the_skill_of_its_english_case(corpus):
    skills = expected_skills(corpus)
    for case in corpus.cases:
        if case.language == "zh":
            assert skills[case.case_id] == skills[case.english_case], case.case_id


def test_the_expected_skill_allows_a_tool_of_every_required_call_group(corpus):
    """The sidecar follows the acceptable calls: the skill can make a required call."""
    skills = expected_skills(corpus)
    allowed = {
        name: set(
            json.loads((SKILLS / name / "skill.manifest.json").read_text("utf-8"))["allowed_tools"]
        )
        for name in demo_skill_names(SKILLS)
    }
    for case in corpus.cases:
        skill = skills[case.case_id]
        for group in case.required_calls:
            assert skill is not None and {p["tool"] for p in group} & allowed[skill], case.case_id


def test_a_case_with_no_acceptable_call_is_the_only_kind_that_may_expect_none(corpus):
    """null means a clarification before any skill is chosen; no case that has calls expects it."""
    for case_id, skill in expected_skills(corpus).items():
        if skill is None:
            assert not corpus.case(case_id).acceptable_calls, case_id


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["skills"].pop("simple-01"), "missing"),
        (lambda d: d["skills"].update({"simple-99": None}), "unknown case"),
        (lambda d: d["skills"].update({"simple-01": "delete-everything"}), "demo skill"),
        (lambda d: d["skills"].update({"simple-01": 3}), "demo skill"),
        (lambda d: d["skills"].update({"simple-01-zh": "lookup-latest-deployment"}), "english"),
        (lambda d: d.update(extra=1), "unknown field"),
        (lambda d: d.pop("note"), "missing field"),
        (lambda d: d.update(schema_version=2), "schema_version"),
        (lambda d: d.update(version=2), "version"),
        (lambda d: d.update(skills=["simple-01"]), "object"),
    ],
)
def test_an_invalid_sidecar_is_refused(corpus, change, message):
    document = sidecar()
    change(document)
    with pytest.raises(CorpusError, match=message):
        expected_skills(corpus, document)


def test_a_case_named_twice_is_refused(corpus, tmp_path):
    text = SIDECAR.read_text("utf-8")
    duplicate = text.replace(
        '"simple-01": "check-service-health",',
        '"simple-01": "check-service-health",\n    "simple-01": "check-service-health",',
        1,
    )
    assert duplicate != text
    path = tmp_path / "corpus.v1.skills.json"
    path.write_text(duplicate, "utf-8")
    with pytest.raises(CorpusError, match="twice"):
        load_skills_sidecar(path, corpus, skill_names=demo_skill_names(SKILLS))


def test_the_corpus_digest_is_the_one_that_froze_version_1(corpus):
    """Adding a sidecar does not change the corpus or its lock digest."""
    assert corpus.digest == FROZEN_CORPUS_SHA256
    assert json.loads(LOCK.read_text("utf-8"))["sha256"] == FROZEN_CORPUS_SHA256


def test_the_lock_holds_the_digest_of_the_sidecar(corpus):
    lock = json.loads(LOCK.read_text("utf-8"))
    assert lock["sidecars"] == {SIDECAR.name: canonical_digest(sidecar())}
    check_lock(corpus, LOCK)


def copy_lock_with_sidecar(tmp_path, document):
    (tmp_path / LOCK.name).write_text(LOCK.read_text("utf-8"), "utf-8")
    (tmp_path / SIDECAR.name).write_text(json.dumps(document), "utf-8")
    return tmp_path / LOCK.name


def test_a_changed_sidecar_is_refused_by_its_digest(corpus, tmp_path):
    document = sidecar()
    document["skills"]["simple-01"] = "lookup-latest-deployment"
    lock = copy_lock_with_sidecar(tmp_path, document)
    with pytest.raises(CorpusError, match="sidecar.*digest"):
        check_lock(corpus, lock)


def test_the_same_sidecar_in_another_layout_is_accepted(corpus, tmp_path):
    """The digest is of the canonical JSON, not of the bytes on disk."""
    lock = copy_lock_with_sidecar(tmp_path, sidecar())
    (tmp_path / SIDECAR.name).write_text(json.dumps(sidecar(), indent=8), "utf-8")
    check_lock(corpus, lock)


def test_a_missing_sidecar_is_refused(corpus, tmp_path):
    (tmp_path / LOCK.name).write_text(LOCK.read_text("utf-8"), "utf-8")
    with pytest.raises(CorpusError, match="sidecar"):
        check_lock(corpus, tmp_path / LOCK.name)


def test_a_lock_without_sidecars_still_checks_the_corpus(corpus, tmp_path):
    lock = json.loads(LOCK.read_text("utf-8"))
    del lock["sidecars"]
    path = tmp_path / LOCK.name
    path.write_text(json.dumps(lock), "utf-8")
    check_lock(corpus, path)
    lock["sha256"] = "0" * 64
    path.write_text(json.dumps(lock), "utf-8")
    with pytest.raises(CorpusError, match="digest"):
        check_lock(corpus, path)
