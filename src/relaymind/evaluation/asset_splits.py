"""Cross-release exposure and recomputable Latin-token diagnostics (RM40/AC31)."""

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from referencing.jsonschema import DRAFT202012, specification_with

from relaymind.assets.catalog import AssetRecord, validate_asset_record
from relaymind.evaluation.asset_corpus_io import AssetCorpusError


def normalize_prompt(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def input_identity(case: Mapping) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Normalized task plus ordered, role-sensitive conversation; IDs are not input."""
    return (
        normalize_prompt(case["task_text"]),
        tuple((row["role"], normalize_prompt(row["text"])) for row in case["conversation"]),
    )


def latin_tokens(value: str) -> frozenset[str]:
    """latin_tokens.v1: non-Latin letters break runs, including mixed scripts."""
    result, run, has_latin = set(), [], False
    for char in unicodedata.normalize("NFC", value).casefold() + " ":
        latin = unicodedata.category(char).startswith("L") and "LATIN" in unicodedata.name(char, "")
        if latin or char in "0123456789_":
            run.append(char)
            has_latin = has_latin or latin
        else:
            if run and has_latin:
                result.add("".join(run))
            run, has_latin = [], False
    return frozenset(result)


def _schema_strings(value):
    """Visit schema nodes, never property maps or enum/default instance data."""
    dialect = value.get("$schema")
    specification = specification_with(
        dialect if isinstance(dialect, str) else "urn:unknown-dialect", default=DRAFT202012
    )
    # Match the product schema boundary's dialect and subresource walk. This
    # recognizes real subschema positions without interpreting instance values.
    pending = [specification.create_resource(value)]
    while pending:
        resource = pending.pop()
        schema = resource.contents
        if not isinstance(schema, Mapping):
            continue
        description = schema.get("description")
        if isinstance(description, str):
            yield description
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            yield from properties
        pending.extend(resource.subresources())


@dataclass(frozen=True)
class LatinOverlap:
    unicode_version: str
    query_tokens: tuple[str, ...]
    token_intersections: Mapping[str, tuple[str, ...]]
    all_targets_disjoint: bool


def classify_latin_overlap(
    case: Mapping, acceptable_records: Sequence[AssetRecord | Mapping], *, unicode_version: str
) -> LatinOverlap:
    """latin_tokens.v1 over every offered target, pinned to the Unicode database."""
    if unicode_version != unicodedata.unidata_version:
        raise AssetCorpusError("unicode_version_mismatch")
    query = latin_tokens(" ".join([case["task_text"], *[m["text"] for m in case["conversation"]]]))
    intersections = {}
    for raw in acceptable_records:
        record = raw if isinstance(raw, AssetRecord) else validate_asset_record(raw)
        if record.asset_id in intersections:
            raise AssetCorpusError("duplicate_asset")
        identity = record.identity
        strings = [record.description]
        if record.kind == "skill":
            strings.append(identity["package_id"])
        else:
            strings.extend([identity["server_id"], identity["tool_name"], identity["exposed_name"]])
            strings.extend(_schema_strings(identity["input_schema"]))
        intersections[record.asset_id] = tuple(sorted(query & latin_tokens(" ".join(strings))))
    return LatinOverlap(
        unicode_version,
        tuple(sorted(query)),
        MappingProxyType(intersections),
        not any(intersections.values()),
    )


def hard_subsets(catalog: dict, cases: dict, labels: dict) -> dict:
    assets = {entry["record"]["asset_id"]: entry["record"] for entry in catalog["assets"]}
    finals = {row["case_id"]: row for row in labels["adjudications"]}
    diagnostics = {}
    for case in cases["cases"]:
        final = finals[case["id"]]
        if case["lane"] not in ("zh_hans_to_en", "en_to_zh_hans"):
            continue
        overlap = classify_latin_overlap(
            case,
            [assets[asset_id] for asset_id in final["acceptable_asset_ids"]],
            unicode_version=unicodedata.unidata_version,
        )
        diagnostics[case["id"]] = {
            "subset": ("no_shared_latin" if overlap.all_targets_disjoint else "shared_latin")
            if final["class"] == "positive"
            else "negative_or_ambiguous",
            "token_intersections": {
                key: list(value) for key, value in overlap.token_intersections.items()
            },
            "hard_precision_included": final["class"] != "positive" or overlap.all_targets_disjoint,
        }
    return diagnostics


def validate_splits(releases: list[tuple[dict, dict, dict]]) -> dict:
    """Only labeled targets count as exposure; the shared catalog stays public."""
    exposure, case_rows, links = {}, {}, []
    counts = {"development": 0, "held_out": 0, "diagnostic": 0}
    for catalog, cases, labels in releases:
        assets = {entry["record"]["asset_id"]: entry for entry in catalog["assets"]}
        finals = {row["case_id"]: row for row in labels["adjudications"]}
        for case in cases["cases"]:
            identity = (cases["dataset_id"], cases["release"], case["id"])
            case_rows[identity] = case
            counts[case["split"]] += 1
            if case["derived_from_case_id"] is not None:
                links.append((identity, case["derived_from_case_id"]))
            keys = (
                [("task_family", value) for value in case["task_family_ids"]]
                + [
                    ("target_family", value)
                    for asset_id in finals[case["id"]]["acceptable_asset_ids"]
                    for value in assets[asset_id]["asset_family_ids"]
                ]
                + [
                    ("prompt", normalize_prompt(case["task_text"])),
                    ("case_identity", (cases["dataset_id"], case["id"])),
                ]
                + (
                    [("source_case", case["source_case_evidence_id"])]
                    if case["source_case_evidence_id"] is not None
                    else []
                )
            )
            for key in keys:
                before = exposure.setdefault(key, case["split"])
                if before != case["split"]:
                    raise AssetCorpusError("split_leakage")
    graph = {}
    for identity, parent_id in links:
        candidates = [key for key in case_rows if key[0] == identity[0] and key[2] == parent_id]
        if not candidates:
            raise AssetCorpusError("unknown_derivation")
        for parent in candidates:
            if case_rows[identity]["split"] != case_rows[parent]["split"]:
                raise AssetCorpusError("cross_split_derivation")
        # A later release can add a parent edge, never erase an earlier
        # committed lineage. Keep all edges for each stable dataset/case ID.
        source, target = (identity[0], identity[2]), (identity[0], parent_id)
        graph.setdefault(source, set()).add(target)
        graph.setdefault(target, set())
    incoming = dict.fromkeys(graph, 0)
    for parents in graph.values():
        for parent in parents:
            incoming[parent] += 1
    ready = [node for node, count in incoming.items() if count == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for parent in graph[node]:
            incoming[parent] -= 1
            if incoming[parent] == 0:
                ready.append(parent)
    if visited != len(graph):
        raise AssetCorpusError("derivation_cycle")
    return {"split_counts": counts, "training_overlap": "unknown"}
