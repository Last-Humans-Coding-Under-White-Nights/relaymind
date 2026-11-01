"""Blind double annotation, adjudication worksheet and label freeze.

Spec 0.53 section 12.6.1 (RM40, RM42, RM44; AC31, AC33, AC36). The frozen
AssetLabelsV1 that grades retrieval cases, adaptation probes (#236) and host
prompt cases (#237) needs exactly two raw judgments per case by two distinct
declared annotators who are not tuners, made blind to each other's judgments and
to algorithm outputs, plus one final adjudication per case and agreement
recomputed over every raw pair before adjudication.

The working files here are not specification exports:

* a **label template** is what one annotator sees: the frozen prompt of each
  case and nothing else (no labels, families, splits, authors, other
  judgments or algorithm outputs), bound to the cases and catalog digests;
* the **adjudication worksheet** is for the adjudicator only: both raw
  judgments per case, their per-answer agreement and the recomputed agreement.

``freeze_labels`` assembles and validates the AssetLabelsV1 with the unchanged
release validator. Separate templates make blindness possible; keeping them
apart, and the custodian review of people, competence and independence, remain
process obligations that no file format proves.
"""

import copy
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_corpus_io import _VALIDATORS, AssetCorpusError, _CorpusValidator
from relaymind.evaluation.asset_judgments import (
    METHODS,
    _answer,
    agreement,
    check_object_order,
    check_owner_machine_pair,
    check_person,
    id_map,
    validate_judgments,
)

TEMPLATE_FORMAT = "relaymind.label_template.v1"
WORKSHEET_FORMAT = "relaymind.adjudication_worksheet.v1"
RUBRIC = "relevance.v1"
_PROMPT = (
    "lane",
    "task_text",
    "conversation",
    "requested_kinds",
    "language_tags",
    "code_switched",
)
_JUDGMENT = {
    "recorded_at",
    "class",
    "acceptable_asset_ids",
    "language_valid",
    "independent_case",
    "notes",
    "independence_evidence_ids",
}
_ADJUDICATION = {
    "adjudicator_id",
    "recorded_at",
    "class",
    "acceptable_asset_ids",
    "language_valid",
    "independent_case",
    "disposition",
    "reason",
    "evidence_ids",
}
_JUDGMENT_ITEM = _CorpusValidator(_VALIDATORS["labels"].schema["properties"]["judgments"]["items"])


def _documents(catalog: Mapping, cases: Mapping) -> tuple[dict, dict]:
    for kind, value in (("catalog_set", catalog), ("cases", cases)):
        if next(_VALIDATORS[kind].iter_errors(value), None) is not None:
            raise AssetCorpusError("invalid_schema")
    if (catalog["dataset_id"], catalog["release"]) != (cases["dataset_id"], cases["release"]):
        raise AssetCorpusError("release_mismatch")
    return id_map(catalog["people"]), id_map(cases["cases"])


def _binding(catalog: Mapping, cases: Mapping) -> dict:
    return dict(
        dataset_id=cases["dataset_id"],
        release=cases["release"],
        cases_digest=canonical_digest(cases),
        catalog_digest=canonical_digest(catalog),
        rubric_version=RUBRIC,
    )


def _prompt(case: Mapping) -> dict:
    return dict(case_id=case["id"], **{k: copy.deepcopy(case[k]) for k in _PROMPT})


def _method(labelling: str) -> str:
    if labelling not in METHODS:
        raise AssetCorpusError("invalid_labelling")
    return METHODS[labelling]


def label_template(
    catalog: Mapping, cases: Mapping, annotator_id: str, *, labelling: str = "independent"
) -> dict:
    """One annotator's blind template over every frozen case.

    ``labelling`` is ``independent`` (section 12.6.1) or the spec 0.66
    section 12.6.6 ``owner_machine`` profile, where the owner may also tune.
    """
    owner_machine = _method(labelling) == METHODS["owner_machine"]
    people, case_map = _documents(catalog, cases)
    check_person(annotator_id, people, "annotator", owner_machine=owner_machine)
    return dict(
        format=TEMPLATE_FORMAT,
        **_binding(catalog, cases),
        annotator_id=annotator_id,
        items=[dict(_prompt(case), judgment=None) for case in case_map.values()],
    )


def raw_judgments(
    catalog: Mapping,
    cases: Mapping,
    templates: Sequence[Mapping],
    *,
    labelling: str = "independent",
) -> list[dict]:
    """The immutable raw Judgment rows from returned templates, sorted.

    Every case needs exactly two judgments by distinct annotators; a template
    whose binding or prompt text differs from the frozen cases rejects.
    """
    owner_machine = _method(labelling) == METHODS["owner_machine"]
    people, case_map = _documents(catalog, cases)
    assets = {entry["record"]["asset_id"]: entry for entry in catalog["assets"]}
    binding = _binding(catalog, cases)
    judgments = []
    for template in templates:
        if (
            not isinstance(template, Mapping)
            or set(template) != {"format", *binding, "annotator_id", "items"}
            or template["format"] != TEMPLATE_FORMAT
            or any(template[k] != v for k, v in binding.items())
            or not isinstance(template["items"], list)
        ):
            raise AssetCorpusError("template_binding")
        annotator = template["annotator_id"]
        check_person(annotator, people, "annotator", owner_machine=owner_machine)
        ids = set()
        for item in template["items"]:
            case = case_map.get(item.get("case_id")) if isinstance(item, Mapping) else None
            if (
                case is None
                or item["case_id"] in ids
                or {k: v for k, v in item.items() if k != "judgment"} != _prompt(case)
                or "judgment" not in item
            ):
                raise AssetCorpusError("template_binding")
            ids.add(item["case_id"])
            answer = item["judgment"]
            if answer is None:
                raise AssetCorpusError("unjudged_case")
            row = dict(case_id=item["case_id"], annotator_id=annotator)
            if not isinstance(answer, Mapping) or set(answer) != _JUDGMENT:
                raise AssetCorpusError("invalid_judgment")
            row.update(copy.deepcopy(dict(answer)))
            if next(_JUDGMENT_ITEM.iter_errors(row), None) is not None:
                raise AssetCorpusError("invalid_judgment")
            _answer(row, assets)
            judgments.append(row)
        if ids != set(case_map):
            # A deleted case is a changed template, not a missing judgment.
            raise AssetCorpusError("template_binding")
    judgments.sort(key=lambda j: (j["case_id"].encode(), j["annotator_id"].encode()))
    check_object_order(judgments, ("case_id", "annotator_id"))
    counts = Counter(row["case_id"] for row in judgments)
    if any(counts[case_id] != 2 for case_id in case_map):
        raise AssetCorpusError("raw_judgment_coverage")
    if owner_machine:
        check_owner_machine_pair((j["annotator_id"] for j in judgments), people)
    return judgments


def _by_case(judgments: Sequence[Mapping]) -> dict[str, list]:
    rows = defaultdict(list)
    for row in judgments:
        rows[row["case_id"]].append(dict(row))
    return rows


def _require_double_annotation(case_map: Mapping, rows: Mapping) -> None:
    """Two distinct annotators per case, exactly as the release validator requires."""
    if set(rows) != set(case_map) or any(
        len(r) != 2 or r[0]["annotator_id"] == r[1]["annotator_id"] for r in rows.values()
    ):
        raise AssetCorpusError("raw_judgment_coverage")


def _groups(case_map: Mapping, rows: Mapping, labelling: str) -> dict:
    groups = agreement(case_map, rows)
    ordered = sorted(groups, key=lambda lane: (lane != "all", lane.encode()))
    return dict(method=_method(labelling), groups=[groups[lane] for lane in ordered])


def adjudication_worksheet(
    catalog: Mapping,
    cases: Mapping,
    judgments: Sequence[Mapping],
    *,
    labelling: str = "independent",
) -> dict:
    """The adjudicator's view: both raw judgments, their agreement, one final per case."""
    _, case_map = _documents(catalog, cases)
    rows = _by_case(judgments)
    _require_double_annotation(case_map, rows)
    items = []
    for case_id in case_map:
        left, right = rows[case_id]
        agrees = {
            "class": left["class"] == right["class"],
            "acceptable_set": set(left["acceptable_asset_ids"])
            == set(right["acceptable_asset_ids"]),
            "language": left["language_valid"] == right["language_valid"],
            "independence": left["independent_case"] == right["independent_case"],
        }
        agrees["joint"] = all(agrees.values())
        items.append(
            dict(case_id=case_id, judgments=[left, right], agrees=agrees, adjudication=None)
        )
    return dict(
        format=WORKSHEET_FORMAT,
        **_binding(catalog, cases),
        agreement=_groups(case_map, rows, labelling),
        items=items,
    )


def freeze_labels(
    catalog: Mapping,
    cases: Mapping,
    judgments: Sequence[Mapping],
    worksheet: Mapping,
    *,
    labelling: str = "independent",
) -> dict:
    """The AssetLabelsV1 from raw judgments and the completed worksheet.

    Raw judgments are never overwritten by adjudication; agreement is
    recomputed, never taken from the worksheet; the release validator checks
    roles, coverage, classes, inclusion and target families.
    """
    _, case_map = _documents(catalog, cases)
    binding = _binding(catalog, cases)
    if (
        not isinstance(worksheet, Mapping)
        or worksheet.get("format") != WORKSHEET_FORMAT
        or any(worksheet.get(k) != v for k, v in binding.items())
        or not isinstance(worksheet.get("items"), list)
        or not all(isinstance(item, Mapping) for item in worksheet["items"])
    ):
        raise AssetCorpusError("worksheet_binding")
    rows = _by_case(judgments)
    _require_double_annotation(case_map, rows)
    if [i.get("case_id") for i in worksheet["items"]] != list(case_map):
        raise AssetCorpusError("adjudication_coverage")
    adjudications = []
    for item in worksheet["items"]:
        if item.get("judgments") != rows.get(item["case_id"]):
            raise AssetCorpusError("raw_judgment_changed")
        final = item.get("adjudication")
        if final is None:
            raise AssetCorpusError("adjudication_coverage")
        if not isinstance(final, Mapping) or set(final) != _ADJUDICATION:
            raise AssetCorpusError("invalid_adjudication")
        adjudications.append(dict(case_id=item["case_id"], **copy.deepcopy(dict(final))))
    labels = dict(
        schema_version=1,
        dataset_id=binding["dataset_id"],
        release=binding["release"],
        cases_digest=binding["cases_digest"],
        rubric_version=RUBRIC,
        judgments=[dict(j) for j in judgments],
        adjudications=adjudications,
        agreement=_groups(case_map, rows, labelling),
    )
    if next(_VALIDATORS["labels"].iter_errors(labels), None) is not None:
        raise AssetCorpusError("invalid_schema")
    validate_judgments(dict(catalog), dict(cases), labels)
    return labels
