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


@pytest.mark.parametrize("annotators", [("judge-a",), ("judge-a", "judge-a")])
def test_freezing_needs_two_distinct_annotators_per_case(tmp_path, annotators):
    """Review 4436631728: freeze_labels applies the worksheet's coverage rule."""
    catalog, cases, labels = corpus(tmp_path)
    templates = [
        filled(label_template(catalog, cases, a), labels, a) for a in ("judge-a", "judge-b")
    ]
    judgments = raw_judgments(catalog, cases, templates)
    worksheet = adjudicated(adjudication_worksheet(catalog, cases, judgments), labels)
    if len(annotators) == 1:
        kept = [j for j in judgments if j["annotator_id"] == "judge-a"]
    else:
        kept = [dict(j, annotator_id="judge-a") for j in judgments]
    by_case = {}
    for j in kept:
        by_case.setdefault(j["case_id"], []).append(j)
    for item in worksheet["items"]:
        item["judgments"] = by_case[item["case_id"]]
    with pytest.raises(AssetCorpusError, match="raw_judgment_coverage"):
        freeze_labels(catalog, cases, kept, worksheet)
