"""Blind double annotation, adjudication worksheet and label freeze (#236, #237).

Spec 0.53 section 12.6.1 (RM40, RM42, RM44; AC31, AC33, AC36): two independent
annotators who are not tuners judge every case blind to each other and to
algorithm outputs; raw judgments are immutable; one final adjudication per
case; agreement is recomputed over all raw pairs. The adaptation probes and
host-task prompt cases are graded against these frozen labels. Fixture data
only: the people here are synthetic and no custodian review is implied.
"""

import copy
import json

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_corpus_io import AssetCorpusError
from relaymind.evaluation.asset_labelling import (
    adjudication_worksheet,
    freeze_labels,
    label_template,
    raw_judgments,
)
from whole_task_helpers import expanded_release


def corpus(tmp_path, count=3):
    root = expanded_release(tmp_path / "release", count)

    def read(name):
        return json.loads((root / (name + ".json")).read_text(encoding="utf-8"))

    return read("catalog"), read("cases"), read("labels")


def filled(template, labels, annotator):
    out = copy.deepcopy(template)
    raw = {j["case_id"]: j for j in labels["judgments"] if j["annotator_id"] == annotator}
    for item in out["items"]:
        source = raw[item["case_id"]]
        item["judgment"] = {
            k: source[k]
            for k in (
                "recorded_at",
                "class",
                "acceptable_asset_ids",
                "language_valid",
                "independent_case",
                "notes",
                "independence_evidence_ids",
            )
        }
    return out


def adjudicated(worksheet, labels):
    out = copy.deepcopy(worksheet)
    finals = {a["case_id"]: a for a in labels["adjudications"]}
    for item in out["items"]:
        item["adjudication"] = {k: v for k, v in finals[item["case_id"]].items() if k != "case_id"}
    return out


def test_a_template_is_blind_and_bound_to_the_frozen_cases(tmp_path):
    catalog, cases, _ = corpus(tmp_path)
    template = label_template(catalog, cases, "judge-a")
    assert template["annotator_id"] == "judge-a"
    assert template["cases_digest"] == canonical_digest(cases)
    assert template["catalog_digest"] == canonical_digest(catalog)
    assert [i["case_id"] for i in template["items"]] == [c["id"] for c in cases["cases"]]
    assert all(i["judgment"] is None for i in template["items"])
    # Nothing but the prompt: no labels, families, splits, authors or outputs.
    assert set(template["items"][0]) == {
        "case_id",
        "lane",
        "task_text",
        "conversation",
        "requested_kinds",
        "language_tags",
        "code_switched",
        "judgment",
    }


@pytest.mark.parametrize("person", ["author", "reviewer", "nobody"])
def test_only_a_declared_non_tuner_annotator_gets_a_template(tmp_path, person):
    catalog, cases, _ = corpus(tmp_path)
    with pytest.raises(AssetCorpusError):
        label_template(catalog, cases, person)


def test_a_tuner_cannot_annotate(tmp_path):
    catalog, cases, _ = corpus(tmp_path)
    people = {p["id"]: p for p in catalog["people"]}
    people["judge-a"]["roles"] = sorted(["annotator", "tuner"])
    with pytest.raises(AssetCorpusError, match="role_conflict"):
        label_template(catalog, cases, "judge-a")


def test_the_round_trip_reproduces_the_frozen_labels(tmp_path):
    catalog, cases, labels = corpus(tmp_path)
    templates = [
        filled(label_template(catalog, cases, a), labels, a) for a in ("judge-b", "judge-a")
    ]
    judgments = raw_judgments(catalog, cases, templates)
    assert judgments == labels["judgments"]
    worksheet = adjudication_worksheet(catalog, cases, judgments)
    assert worksheet["agreement"] == labels["agreement"]
    assert all(i["agrees"]["joint"] for i in worksheet["items"])
    frozen = freeze_labels(catalog, cases, judgments, adjudicated(worksheet, labels))
    assert frozen == labels


def test_disagreement_is_shown_to_the_adjudicator_and_counted(tmp_path):
    catalog, cases, labels = corpus(tmp_path)
    b = filled(label_template(catalog, cases, "judge-b"), labels, "judge-b")
    b["items"][0]["judgment"].update({"class": "negative", "acceptable_asset_ids": []})
    a = filled(label_template(catalog, cases, "judge-a"), labels, "judge-a")
    worksheet = adjudication_worksheet(catalog, cases, raw_judgments(catalog, cases, [a, b]))
    first = worksheet["items"][0]
    assert first["agrees"] == {
        "class": False,
        "acceptable_set": False,
        "language": True,
        "independence": True,
        "joint": False,
    }
    assert [j["annotator_id"] for j in first["judgments"]] == ["judge-a", "judge-b"]
    group = next(g for g in worksheet["agreement"]["groups"] if g["lane"] == "all")
    assert group["paired_cases"] == 3 and group["joint_agreements"] == 2
    assert group["jaccard_sum"] == 2.0


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda t: t.update(cases_digest="a" * 64), "template_binding"),
        (lambda t: t["items"][0].update(task_text="edited"), "template_binding"),
        (lambda t: t["items"][0].update(judgment=None), "unjudged_case"),
        (lambda t: t["items"][0]["judgment"].update(extra=1), "invalid_judgment"),
        (
            lambda t: t["items"][0]["judgment"].update(acceptable_asset_ids=[]),
            "invalid_judgment_class",
        ),
    ],
)
def test_a_changed_or_incomplete_template_rejects(tmp_path, mutate, reason):
    catalog, cases, labels = corpus(tmp_path)
    a = filled(label_template(catalog, cases, "judge-a"), labels, "judge-a")
    b = filled(label_template(catalog, cases, "judge-b"), labels, "judge-b")
    mutate(a)
    with pytest.raises(AssetCorpusError, match=reason):
        raw_judgments(catalog, cases, [a, b])


def test_every_case_needs_exactly_two_distinct_annotators(tmp_path):
    catalog, cases, labels = corpus(tmp_path)
    a = filled(label_template(catalog, cases, "judge-a"), labels, "judge-a")
    with pytest.raises(AssetCorpusError, match="raw_judgment_coverage"):
        raw_judgments(catalog, cases, [a])
    with pytest.raises(AssetCorpusError, match="duplicate_id"):
        raw_judgments(catalog, cases, [a, copy.deepcopy(a)])


def test_raw_judgments_cannot_change_during_adjudication(tmp_path):
    catalog, cases, labels = corpus(tmp_path)
    templates = [
        filled(label_template(catalog, cases, a), labels, a) for a in ("judge-a", "judge-b")
    ]
    judgments = raw_judgments(catalog, cases, templates)
    worksheet = adjudicated(adjudication_worksheet(catalog, cases, judgments), labels)
    changed = copy.deepcopy(worksheet)
    changed["items"][0]["judgments"][0]["notes"] = "rewritten"
    with pytest.raises(AssetCorpusError, match="raw_judgment_changed"):
        freeze_labels(catalog, cases, judgments, changed)
    missing = copy.deepcopy(worksheet)
    missing["items"][1]["adjudication"] = None
    with pytest.raises(AssetCorpusError, match="adjudication_coverage"):
        freeze_labels(catalog, cases, judgments, missing)
    tuner = copy.deepcopy(worksheet)
    tuner["items"][0]["adjudication"]["adjudicator_id"] = "author"
    with pytest.raises(AssetCorpusError, match="role_conflict"):
        freeze_labels(catalog, cases, judgments, tuner)


# --- spec 0.66 section 12.6.6 owner_machine labelling (#251) -----------------------


def _owner_machine(
    tmp_path, owner_roles=("adjudicator", "annotator", "tuner"), machine_roles=("annotator",)
):
    catalog, cases, labels = corpus(tmp_path)
    people = {p["id"]: p for p in catalog["people"]}
    people["judge-a"]["roles"] = sorted(owner_roles)
    people["judge-b"]["roles"] = sorted(machine_roles)
    return catalog, cases, labels


def _owner_adjudicated(worksheet, labels, adjudicator="judge-a", reason=None):
    out = adjudicated(worksheet, labels)
    for item in out["items"]:
        item["adjudication"]["adjudicator_id"] = adjudicator
        if reason is not None:
            item["adjudication"]["reason"] = reason
    return out


def _owner_templates(catalog, cases, labels):
    return [
        filled(label_template(catalog, cases, a, labelling="owner_machine"), labels, a)
        for a in ("judge-a", "judge-b")
    ]


def test_owner_machine_labelling_admits_the_owner_who_also_tunes(tmp_path):
    """Spec 12.6.6: the owner may hold the tuner role; the label says owner_machine."""
    catalog, cases, labels = _owner_machine(tmp_path)
    with pytest.raises(AssetCorpusError, match="role_conflict"):
        label_template(catalog, cases, "judge-a")
    judgments = raw_judgments(
        catalog, cases, _owner_templates(catalog, cases, labels), labelling="owner_machine"
    )
    worksheet = adjudication_worksheet(catalog, cases, judgments, labelling="owner_machine")
    assert worksheet["agreement"]["method"] == "owner_machine.v1"
    frozen = freeze_labels(
        catalog, cases, judgments, _owner_adjudicated(worksheet, labels), labelling="owner_machine"
    )
    assert frozen["agreement"]["method"] == "owner_machine.v1"
    assert frozen["agreement"]["groups"] == labels["agreement"]["groups"]


@pytest.mark.parametrize(
    ("owner_roles", "machine_roles"),
    [
        (("adjudicator", "annotator", "tuner"), ("annotator", "tuner")),
        (("annotator",), ("annotator", "tuner")),
        (("adjudicator", "annotator"), ("annotator", "reviewer")),
    ],
)
def test_owner_machine_labelling_needs_a_machine_with_the_annotator_role_only(
    tmp_path, owner_roles, machine_roles
):
    """The machine annotator holds annotator only and is never a tuner.

    With the roles reversed, the annotator-only owner is taken as the machine,
    so the owner's adjudication is by the wrong person and rejects at freeze.
    """
    catalog, cases, labels = _owner_machine(tmp_path, owner_roles, machine_roles)
    templates = _owner_templates(catalog, cases, labels)
    with pytest.raises(AssetCorpusError, match="role_conflict"):
        judgments = raw_judgments(catalog, cases, templates, labelling="owner_machine")
        worksheet = adjudication_worksheet(catalog, cases, judgments, labelling="owner_machine")
        adjudicated_ = _owner_adjudicated(worksheet, labels)
        freeze_labels(catalog, cases, judgments, adjudicated_, labelling="owner_machine")


# --- spec 0.66 section 12.6.6 machine_dual_owner_approved labelling (#260) ----------


def _machine_dual(tmp_path, adjudicator_roles=("adjudicator",)):
    """Two machine annotators and a distinct machine adjudicator; no human labeller."""
    catalog, cases, labels = corpus(tmp_path)
    catalog["people"].append(
        dict(
            id="machine-adjudicator",
            roles=sorted(adjudicator_roles),
            competencies=[],
            evidence_ids=["adjudication", "independence"],
        )
    )
    catalog["people"].sort(key=lambda p: p["id"].encode())
    for row in labels["adjudications"]:
        row["adjudicator_id"] = "machine-adjudicator"
    return catalog, cases, labels


def _dual_round_trip(catalog, cases, labels):
    templates = [
        filled(
            label_template(catalog, cases, a, labelling="machine_dual_owner_approved"), labels, a
        )
        for a in ("judge-a", "judge-b")
    ]
    judgments = raw_judgments(catalog, cases, templates, labelling="machine_dual_owner_approved")
    worksheet = adjudication_worksheet(
        catalog, cases, judgments, labelling="machine_dual_owner_approved"
    )
    return freeze_labels(
        catalog,
        cases,
        judgments,
        adjudicated(worksheet, labels),
        labelling="machine_dual_owner_approved",
    )


def test_machine_dual_labelling_freezes_with_its_own_method(tmp_path):
    """Spec 12.6.6: two machine annotators, a third machine adjudicator, machine_dual.v1."""
    frozen = _dual_round_trip(*_machine_dual(tmp_path))
    assert frozen["agreement"]["method"] == "machine_dual.v1"
    assert {a["adjudicator_id"] for a in frozen["adjudications"]} == {"machine-adjudicator"}


def test_machine_dual_adjudicator_is_a_distinct_adjudicator_only_person(tmp_path):
    """The third pass is neither an annotator nor a tuner (spec 12.6.6)."""
    catalog, cases, labels = _machine_dual(tmp_path)
    for row in labels["adjudications"]:
        row["adjudicator_id"] = "judge-a"
    next(p for p in catalog["people"] if p["id"] == "judge-a")["roles"] = [
        "adjudicator",
        "annotator",
    ]
    with pytest.raises(AssetCorpusError, match="role_conflict"):
        _dual_round_trip(catalog, cases, labels)


@pytest.mark.parametrize("roles", [("adjudicator", "tuner"), ("adjudicator", "custodian")])
def test_machine_dual_adjudicator_holds_no_other_role(tmp_path, roles):
    with pytest.raises(AssetCorpusError, match="role_conflict"):
        _dual_round_trip(*_machine_dual(tmp_path, adjudicator_roles=roles))


def test_machine_dual_annotators_hold_the_annotator_role_only(tmp_path):
    catalog, cases, labels = _machine_dual(tmp_path)
    next(p for p in catalog["people"] if p["id"] == "judge-b")["roles"] = ["annotator", "author"]
    with pytest.raises(AssetCorpusError, match="role_conflict"):
        _dual_round_trip(catalog, cases, labels)


# --- spec 0.66 section 12.6.6 machine-drafted prompts: the drafter (review of #276) --


def _drafted(tmp_path, roles=("author",), competencies=()):
    """machine_dual labels over cases written by one recorded drafting model."""
    catalog, cases, labels = _machine_dual(tmp_path)
    catalog["people"].append(
        dict(
            id="machine-drafter",
            roles=sorted(roles),
            competencies=sorted(competencies),
            evidence_ids=["authorship"],
        )
    )
    catalog["people"].sort(key=lambda p: p["id"].encode())
    for case in cases["cases"]:
        case.update(authorship="machine_drafted_owner_approved", author_ids=["machine-drafter"])
    labels["cases_digest"] = canonical_digest(cases)
    return catalog, cases, labels


def test_machine_drafted_cases_freeze_with_an_author_only_drafter(tmp_path):
    """Spec 12.6.6: the drafter has role author only and empty competencies."""
    frozen = _dual_round_trip(*_drafted(tmp_path))
    assert frozen["agreement"]["method"] == "machine_dual.v1"


@pytest.mark.parametrize("roles", [("author", "tuner"), ("annotator", "author")])
def test_machine_drafter_holds_no_other_role(tmp_path, roles):
    """A tuner, annotator or adjudicator is never the drafter."""
    with pytest.raises(AssetCorpusError, match="drafter_role"):
        _dual_round_trip(*_drafted(tmp_path, roles=roles))


def test_machine_drafter_has_no_competencies(tmp_path):
    with pytest.raises(AssetCorpusError, match="drafter_role"):
        _dual_round_trip(*_drafted(tmp_path, competencies=("proficient_en",)))


def test_machine_drafted_cases_name_exactly_one_drafter(tmp_path):
    """One recorded drafting model writes every drafted prompt of the release."""
    catalog, cases, labels = _drafted(tmp_path)
    catalog["people"].append(
        dict(id="machine-drafter-2", roles=["author"], competencies=[], evidence_ids=["authorship"])
    )
    catalog["people"].sort(key=lambda p: p["id"].encode())
    cases["cases"][0]["author_ids"] = ["machine-drafter", "machine-drafter-2"]
    labels["cases_digest"] = canonical_digest(cases)
    with pytest.raises(AssetCorpusError, match="drafter_role"):
        _dual_round_trip(catalog, cases, labels)


def _owner_labels(tmp_path, *, disagree=False):
    catalog, cases, labels = _owner_machine(tmp_path)
    templates = _owner_templates(catalog, cases, labels)
    if disagree:
        templates[1]["items"][0]["judgment"]["language_valid"] = False
    judgments = raw_judgments(catalog, cases, templates, labelling="owner_machine")
    worksheet = adjudication_worksheet(catalog, cases, judgments, labelling="owner_machine")
    return catalog, cases, labels, judgments, worksheet


def test_owner_machine_adjudication_is_by_the_owner(tmp_path):
    catalog, cases, labels, judgments, worksheet = _owner_labels(tmp_path)
    third_party = _owner_adjudicated(worksheet, labels, adjudicator="reviewer")
    with pytest.raises(AssetCorpusError, match="role_conflict"):
        freeze_labels(catalog, cases, judgments, third_party, labelling="owner_machine")


def test_an_owner_machine_disagreement_needs_an_adjudication_reason(tmp_path):
    """Spec 12.6.6: a nonempty reason on every disagreement; agreeing pairs need none."""
    catalog, cases, labels, judgments, worksheet = _owner_labels(tmp_path, disagree=True)
    for reason in ("", "  "):
        with pytest.raises(AssetCorpusError, match="adjudication_reason_missing"):
            freeze_labels(
                catalog,
                cases,
                judgments,
                _owner_adjudicated(worksheet, labels, reason=reason),
                labelling="owner_machine",
            )
    explained = _owner_adjudicated(worksheet, labels, reason="")
    explained["items"][0]["adjudication"]["reason"] = "owner: language is valid"
    freeze_labels(catalog, cases, judgments, explained, labelling="owner_machine")


def test_the_worksheet_needs_two_distinct_annotators_per_case(tmp_path):
    """Review 4435556020: self-agreement of one annotator is not double annotation."""
    catalog, cases, labels = corpus(tmp_path)
    templates = [
        filled(label_template(catalog, cases, a), labels, a) for a in ("judge-a", "judge-b")
    ]
    judgments = raw_judgments(catalog, cases, templates)
    doubled = [dict(j, annotator_id="judge-a") for j in judgments]
    with pytest.raises(AssetCorpusError, match="raw_judgment_coverage"):
        adjudication_worksheet(catalog, cases, doubled)


@pytest.mark.parametrize("item", [None, 3, "case-00", []])
def test_a_non_object_worksheet_item_is_a_binding_error(tmp_path, item):
    """Review 4435556027: a malformed worksheet rejects with a fixed reason."""
    catalog, cases, labels = corpus(tmp_path)
    templates = [
        filled(label_template(catalog, cases, a), labels, a) for a in ("judge-a", "judge-b")
    ]
    judgments = raw_judgments(catalog, cases, templates)
    worksheet = adjudicated(adjudication_worksheet(catalog, cases, judgments), labels)
    worksheet["items"][0] = item
    with pytest.raises(AssetCorpusError, match="worksheet_binding"):
        freeze_labels(catalog, cases, judgments, worksheet)


def test_a_template_needs_matching_catalog_and_cases_releases(tmp_path):
    catalog, cases, _ = corpus(tmp_path)
    catalog["release"] = 2
    with pytest.raises(AssetCorpusError, match="release_mismatch"):
        label_template(catalog, cases, "judge-a")


@pytest.mark.parametrize("field", ["cases_digest", "catalog_digest", "format", "release"])
def test_a_worksheet_for_other_documents_is_a_binding_error(tmp_path, field):
    catalog, cases, labels = corpus(tmp_path)
    templates = [
        filled(label_template(catalog, cases, a), labels, a) for a in ("judge-a", "judge-b")
    ]
    judgments = raw_judgments(catalog, cases, templates)
    worksheet = adjudicated(adjudication_worksheet(catalog, cases, judgments), labels)
    worksheet[field] = 9 if field == "release" else "a" * 64
    with pytest.raises(AssetCorpusError, match="worksheet_binding"):
        freeze_labels(catalog, cases, judgments, worksheet)


# --- spec 0.66 section 12.6.6 machine_dual adjudication reasons (review of #276) ----


def _dual_disagreement(tmp_path, reason):
    """machine_dual labels where judge-b says negative to judge-a's positive."""
    catalog, cases, labels = _machine_dual(tmp_path)
    templates = [
        filled(
            label_template(catalog, cases, a, labelling="machine_dual_owner_approved"), labels, a
        )
        for a in ("judge-a", "judge-b")
    ]
    templates[1]["items"][0]["judgment"].update({"class": "negative", "acceptable_asset_ids": []})
    judgments = raw_judgments(catalog, cases, templates, labelling="machine_dual_owner_approved")
    worksheet = adjudicated(
        adjudication_worksheet(catalog, cases, judgments, labelling="machine_dual_owner_approved"),
        labels,
    )
    worksheet["items"][0]["adjudication"]["reason"] = reason
    return catalog, cases, judgments, worksheet


@pytest.mark.parametrize("reason", ["", " ", "\t\n "])
def test_a_machine_dual_disagreement_needs_an_adjudication_reason(tmp_path, reason):
    """Spec 12.6.6: the machine adjudicator gives a nonempty reason on every disagreement."""
    catalog, cases, judgments, worksheet = _dual_disagreement(tmp_path, reason)
    with pytest.raises(AssetCorpusError, match="adjudication_reason_missing"):
        freeze_labels(catalog, cases, judgments, worksheet, labelling="machine_dual_owner_approved")


def test_a_machine_dual_agreement_needs_no_adjudication_reason(tmp_path):
    catalog, cases, labels = _machine_dual(tmp_path)
    for row in labels["adjudications"]:
        row["reason"] = ""
    frozen = _dual_round_trip(catalog, cases, labels)
    assert frozen["agreement"]["method"] == "machine_dual.v1"


def test_release_validation_rejects_a_machine_dual_disagreement_without_a_reason(tmp_path):
    """The same check runs when a committed release is loaded, not only at freeze."""
    from relaymind.evaluation.asset_corpus import load_asset_release

    catalog, cases, judgments, worksheet = _dual_disagreement(tmp_path, "adjudicator: positive")
    frozen = freeze_labels(
        catalog, cases, judgments, worksheet, labelling="machine_dual_owner_approved"
    )
    root = tmp_path / "release"

    def write(name, document):
        (root / f"{name}.json").write_text(json.dumps(document, indent=2), encoding="utf-8")

    def relock():
        lock = json.loads((root / "lock.json").read_text(encoding="utf-8"))
        for name in ("catalog", "cases", "labels"):
            lock[name]["digest"] = canonical_digest(
                json.loads((root / f"{name}.json").read_text(encoding="utf-8"))
            )
        lock.pop("digest")
        lock["digest"] = canonical_digest(lock)
        write("lock", lock)

    def load():
        return load_asset_release(root / "lock.json", evidence_root=root, related_roots={})

    write("catalog", catalog)
    write("labels", frozen)
    relock()
    assert load().labels["agreement"]["method"] == "machine_dual.v1"
    frozen["adjudications"][0]["reason"] = "  "
    write("labels", frozen)
    relock()
    with pytest.raises(AssetCorpusError, match="adjudication_reason_missing"):
        load()
