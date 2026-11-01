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


def check_person(person_id: str, people: Mapping, role: str) -> None:
    check_references([person_id], people)
    roles = people[person_id]["roles"]
    if role not in roles or (role in ("annotator", "adjudicator") and "tuner" in roles):
        raise AssetCorpusError("role_conflict")


def validate_judgments(catalog: dict, cases: dict, labels: dict) -> dict:
    """Keep both raw answers; final adjudication never replaces either one."""
    people = id_map(catalog["people"])
    assets = {entry["record"]["asset_id"]: entry for entry in catalog["assets"]}
    case_map = id_map(cases["cases"])
    check_object_order(labels["judgments"], ("case_id", "annotator_id"))
    rows = defaultdict(list)
    for row in labels["judgments"]:
        check_references([row["case_id"]], case_map)
        check_person(row["annotator_id"], people, "annotator")
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
        check_person(final["adjudicator_id"], people, "adjudicator")
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


def pair_checks(left: Mapping, right: Mapping) -> tuple[bool, bool, bool, bool]:
    """Class, acceptable-set, language and independence agreement of one pair."""
    return (
        left["class"] == right["class"],
        set(left["acceptable_asset_ids"]) == set(right["acceptable_asset_ids"]),
        left["language_valid"] == right["language_valid"],
        left["independent_case"] == right["independent_case"],
    )


def agreement(case_map: Mapping, rows: Mapping) -> dict:
    groups = {
        lane: {"lane": lane, **dict.fromkeys(COUNTS, 0), "jaccard_sum": 0.0}
        for lane in ("all", *LANES)
    }
    for case_id, case in case_map.items():
        left, right = rows[case_id]
        a, b = set(left["acceptable_asset_ids"]), set(right["acceptable_asset_ids"])
        checks = pair_checks(left, right)
        for lane in ("all", case["lane"]):
            group = groups[lane]
            group["paired_cases"] += 1
            for key, equal in zip(COUNTS[1:-1], checks, strict=True):
                group[key] += int(equal)
            group["joint_agreements"] += int(all(checks))
            group["jaccard_sum"] += len(a & b) / len(a | b) if a | b else 1.0
    return groups
