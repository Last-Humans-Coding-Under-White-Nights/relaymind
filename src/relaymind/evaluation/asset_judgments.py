"""Immutable raw double annotation and recomputed agreement (RM40/AC31)."""

from collections import defaultdict
from collections.abc import Collection, Mapping

from relaymind.evaluation.asset_corpus_io import AssetCorpusError

LANES = (
    "code_switch",
    "en_to_en",
    "en_to_zh_hans",
    "zh_hans_to_en",
    "zh_hans_to_zh_hans",
    "zh_hant_diagnostic",
)
COUNTS = (
    "paired_cases",
    "class_agreements",
    "acceptable_set_agreements",
    "language_agreements",
    "independence_agreements",
    "joint_agreements",
)


def check_object_order(rows: list, fields: tuple[str, ...]) -> None:
    """Canonical composite UTF-8 keys; nullable IDs precede nonempty IDs."""
    keys = []
    for row in rows:
        key = []
        for field in fields:
            value = row
            for part in field.split("."):
                value = value[part]
            key.append(b"" if value is None else value.encode("utf-8"))
        keys.append(tuple(key))
    if len(set(keys)) != len(keys):
        raise AssetCorpusError("duplicate_id")
    if keys != sorted(keys):
        raise AssetCorpusError("unsorted_set")


def id_map(rows: list, field: str = "id") -> dict:
    check_object_order(rows, (field,))
    return {row[field]: row for row in rows}


def check_references(ids, available: Collection[str]) -> None:
    if any(value not in available for value in ids):
        raise AssetCorpusError("unknown_reference")


# Spec 0.66 section 12.6.6: the owner-run profiles' agreement methods.
OWNER_MACHINE = "owner_machine.v1"
MACHINE_DUAL = "machine_dual.v1"
METHODS = {
    "independent": "double_annotation.v1",
    "owner_machine": OWNER_MACHINE,
    "machine_dual_owner_approved": MACHINE_DUAL,
}


def check_person(person_id: str, people: Mapping, role: str, *, owner_machine=False) -> None:
    """Section 12.6.1 roles; under owner_machine the owner may also be a tuner."""
    check_references([person_id], people)
    roles = people[person_id]["roles"]
    conflict = role in ("annotator", "adjudicator") and "tuner" in roles
    if role not in roles or (conflict and not owner_machine):
        raise AssetCorpusError("role_conflict")


def check_machine_dual(annotator_ids, adjudicator_ids, people: Mapping) -> None:
    """Spec 12.6.6 machine_dual: two annotator-only machines and one adjudicator-only machine."""
    annotators, adjudicators = set(annotator_ids), set(adjudicator_ids)
    if len(annotators) != 2:
        raise AssetCorpusError("raw_judgment_coverage")
    if (
        any(people[i]["roles"] != ["annotator"] for i in annotators)
        or len(adjudicators) > 1
        or adjudicators & annotators
        or any(people[i]["roles"] != ["adjudicator"] for i in adjudicators)
    ):
        raise AssetCorpusError("role_conflict")


DRAFTED = "machine_drafted_owner_approved"


def check_drafted_cases(cases, people: Mapping) -> None:
    """Spec 12.6.6: one recorded drafter writes every drafted prompt of the release.

    The drafter is a Person with role ``author`` only and empty competencies,
    never an annotator, adjudicator or tuner; each drafted case names it alone.
    """
    drafters = set()
    for case in cases:
        if case["authorship"] != DRAFTED:
            continue
        if len(case["author_ids"]) != 1:
            raise AssetCorpusError("drafter_role")
        drafters.update(case["author_ids"])
    if len(drafters) > 1:
        raise AssetCorpusError("drafter_role")
    for drafter in drafters:
        check_references([drafter], people)
        if people[drafter]["roles"] != ["author"] or people[drafter]["competencies"]:
            raise AssetCorpusError("drafter_role")


def check_owner_machine_pair(annotator_ids, people: Mapping) -> tuple[str, str]:
    """Spec 12.6.6: exactly the owner and one machine annotator.

    The machine holds the ``annotator`` role only (never a tuner); the other
    annotator is the owner. Returns ``(owner_id, machine_id)``.
    """
    ids = sorted(set(annotator_ids))
    if len(ids) != 2:
        raise AssetCorpusError("raw_judgment_coverage")
    machines = [i for i in ids if list(people[i]["roles"]) == ["annotator"]]
    if len(machines) != 1:
        raise AssetCorpusError("role_conflict")
    owner = next(i for i in ids if i != machines[0])
    return owner, machines[0]


def disagree(left: Mapping, right: Mapping) -> bool:
    """Whether two raw judgments differ in any of the four answers."""
    return (
        left["class"] != right["class"]
        or set(left["acceptable_asset_ids"]) != set(right["acceptable_asset_ids"])
        or left["language_valid"] != right["language_valid"]
        or left["independent_case"] != right["independent_case"]
    )


def validate_judgments(catalog: dict, cases: dict, labels: dict) -> dict:
    """Keep both raw answers; final adjudication never replaces either one."""
    people = id_map(catalog["people"])
    assets = {entry["record"]["asset_id"]: entry for entry in catalog["assets"]}
    case_map = id_map(cases["cases"])
    owner_machine = labels["agreement"]["method"] == OWNER_MACHINE
    check_object_order(labels["judgments"], ("case_id", "annotator_id"))
    rows = defaultdict(list)
    for row in labels["judgments"]:
        check_references([row["case_id"]], case_map)
        check_person(row["annotator_id"], people, "annotator", owner_machine=owner_machine)
        _answer(row, assets)
        rows[row["case_id"]].append(row)
    finals = id_map(labels["adjudications"], "case_id")
    if set(finals) != set(case_map):
        raise AssetCorpusError("adjudication_coverage")
    for case_id, case in case_map.items():
        raw = rows[case_id]
        if len(raw) != 2 or raw[0]["annotator_id"] == raw[1]["annotator_id"]:
            raise AssetCorpusError("raw_judgment_coverage")
        final = finals[case_id]
        check_person(final["adjudicator_id"], people, "adjudicator", owner_machine=owner_machine)
        _answer(final, assets)
        if final["disposition"] == "include" and not (
            final["language_valid"] and final["independent_case"]
        ):
            raise AssetCorpusError("invalid_inclusion")
        for asset_id in final["acceptable_asset_ids"]:
            if not set(assets[asset_id]["asset_family_ids"]) <= set(
                case["target_asset_family_ids"]
            ):
                raise AssetCorpusError("target_family_coverage")
        for author in case["author_ids"]:
            check_person(author, people, "author")
    check_drafted_cases(case_map.values(), people)
    if owner_machine:
        owner, _ = check_owner_machine_pair(
            (j["annotator_id"] for j in labels["judgments"]), people
        )
        for case_id, final in finals.items():
            # The owner adjudicates, with a reason on every disagreement.
            if final["adjudicator_id"] != owner:
                raise AssetCorpusError("role_conflict")
            if disagree(*rows[case_id]) and not final["reason"].strip():
                raise AssetCorpusError("adjudication_reason_missing")
    if labels["agreement"]["method"] == MACHINE_DUAL:
        check_machine_dual(
            (j["annotator_id"] for j in labels["judgments"]),
            (a["adjudicator_id"] for a in labels["adjudications"]),
            people,
        )
    actual = agreement(case_map, rows)
    recorded = id_map(labels["agreement"]["groups"], "lane")
    if set(recorded) != set(actual):
        raise AssetCorpusError("agreement_coverage")
    for lane, group in actual.items():
        other = recorded[lane]
        if (
            any(group[key] != other[key] for key in COUNTS)
            or abs(group["jaccard_sum"] - other["jaccard_sum"]) > 1e-12 * group["paired_cases"]
        ):
            raise AssetCorpusError("agreement_mismatch")
    return actual


def _answer(row: dict, assets: Mapping) -> None:
    check_references(row["acceptable_asset_ids"], assets)
    if bool(row["acceptable_asset_ids"]) != (row["class"] in ("positive", "ambiguous")):
        raise AssetCorpusError("invalid_judgment_class")


def agreement(case_map: Mapping, rows: Mapping) -> dict:
    groups = {
        lane: {"lane": lane, **dict.fromkeys(COUNTS, 0), "jaccard_sum": 0.0}
        for lane in ("all", *LANES)
    }
    for case_id, case in case_map.items():
        left, right = rows[case_id]
        a, b = set(left["acceptable_asset_ids"]), set(right["acceptable_asset_ids"])
        checks = (
            left["class"] == right["class"],
            a == b,
            left["language_valid"] == right["language_valid"],
            left["independent_case"] == right["independent_case"],
        )
        for lane in ("all", case["lane"]):
            group = groups[lane]
            group["paired_cases"] += 1
            for key, equal in zip(COUNTS[1:-1], checks, strict=True):
                group[key] += int(equal)
            group["joint_agreements"] += int(all(checks))
            group["jaccard_sum"] += len(a & b) / len(a | b) if a | b else 1.0
    return groups
