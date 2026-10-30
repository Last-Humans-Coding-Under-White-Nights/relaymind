"""Recompute independent-case grades, Wilson rates and workload summaries (RM40)."""

import math
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from relaymind.canonical import canonical_json
from relaymind.evaluation.asset_benchmark_io import asset_dimension_sort_key
from relaymind.evaluation.asset_costs import summarize_asset_costs
from relaymind.evaluation.asset_splits import classify_latin_overlap

if TYPE_CHECKING:
    from relaymind.evaluation.asset_corpus import VerifiedAssetRelease


def asset_rate(numerator: int, denominator: int, *, independent: bool = True) -> dict:
    if (
        type(numerator) is not int
        or type(denominator) is not int
        or not 0 <= numerator <= denominator
    ):
        raise ValueError("invalid_rate")
    value, interval = None, None
    if denominator:
        value = numerator / denominator
        if independent:
            z = 1.959963984540054
            center = (value + z * z / (2 * denominator)) / (1 + z * z / denominator)
            half = (
                z
                * math.sqrt(value * (1 - value) / denominator + z * z / (4 * denominator**2))
                / (1 + z * z / denominator)
            )
            interval = dict(lower=max(0, center - half), upper=min(1, center + half))
    return dict(numerator=numerator, denominator=denominator, value=value, wilson95=interval)


def grade_asset_result(label: Mapping, result: Mapping | None) -> dict:
    kind = label["class"]
    candidates = [] if result is None else result["final_candidates"]
    decision = None if result is None else result["final_decision"]
    acceptable = set(label["acceptable_asset_ids"])
    hit = bool(acceptable.intersection(c["asset_id"] for c in candidates[:3]))
    confident = decision == "confident"
    correct = (
        confident
        and kind == "positive"
        and bool(candidates)
        and candidates[0]["asset_id"] in acceptable
    )
    return dict(
        **{"class": kind},
        hit3=kind == "positive" and hit,
        confident=confident,
        confident_correct=correct,
        ambiguous_supported3=kind == "ambiguous" and hit,
        ambiguous_choice=kind == "ambiguous" and hit and decision == "choice_required",
        false_confident=confident and not correct,
    )


def primary_asset_result(row: Mapping) -> Mapping | None:
    """Only a usable admitted primary can earn primary quality."""
    if not row["attempts"]:
        return (
            row["result"]
            if row["result"] is not None and row["result"]["final_decision"] == "empty_catalog"
            else None
        )
    attempt = row["attempts"][0]
    result = attempt["result"]
    if attempt["outcome"] not in ("ranked", "no_match") or result is None:
        return None
    return dict(final_decision=result["decision"], final_candidates=result["candidates"])


def summary_dimensions(
    release: "VerifiedAssetRelease", config: Mapping, *, effective: Mapping | None = None
) -> list[tuple[dict, set[str]]]:
    """Membership uses all acceptable targets; diagnostic subgroups are non-additive."""
    labels = {
        label["case_id"]: label
        for label in release.labels["adjudications"]
        if label["disposition"] == "include"
    }
    records = {a["record"]["asset_id"]: a["record"] for a in release.catalog["assets"]}
    eligible = (
        {entry["asset_id"] for entry in effective["eligibility"] if entry["eligible"]}
        if effective is not None
        else set(records)
    )
    offered_size = sum(
        record["kind"] in config["requested_kinds"] and asset_id in eligible
        for asset_id, record in records.items()
    )
    dimensions: dict[str, tuple[dict, set[str]]] = {}
    for case in release.cases["cases"]:
        if case["split"] != config["split"] or case["id"] not in labels:
            continue
        label = labels[case["id"]]
        targets = [records[i] for i in label["acceptable_asset_ids"]]
        base = dict(
            lane="all",
            kind="all",
            availability="all",
            stale=None,
            selection_scope=config["selection_scope"],
            catalog_size=offered_size,
            retrieval_mode="service",
            subset="all",
        )
        variants = [base, base | dict(lane=case["lane"])]
        for key in ("kind", "availability", "stale"):
            for value in {r[key] for r in targets}:
                if key == "availability" and value == "unavailable":
                    continue
                variants.append(base | {key: value})
                if key == "kind":
                    variants.append(base | dict(lane=case["lane"], kind=value))
        if case["lane"] in ("zh_hans_to_en", "en_to_zh_hans"):
            if label["class"] == "positive":
                overlap = classify_latin_overlap(
                    case, targets, unicode_version=release.lock["unicode_version"]
                )
                subset = "no_shared_latin" if overlap.all_targets_disjoint else "shared_latin"
                variants.append(base | dict(lane=case["lane"], subset=subset))
            else:
                variants.append(base | dict(lane=case["lane"], subset="no_shared_latin"))
        for variant in variants:
            for mode in ("primary", "service"):
                dim = variant | dict(retrieval_mode=mode)
                key = canonical_json(dim)
                if key not in dimensions:
                    dimensions[key] = (dim, set())
                dimensions[key][1].add(case["id"])
    return sorted(dimensions.values(), key=lambda item: asset_dimension_sort_key(item[0]))


def summarize_asset_rows(
    release: "VerifiedAssetRelease",
    config: Mapping,
    rows: Sequence[Mapping],
    *,
    qualification: str,
    effective: Mapping | None = None,
) -> list[dict]:
    labels = {
        label["case_id"]: label
        for label in release.labels["adjudications"]
        if label["disposition"] == "include"
    }
    summaries = []
    for dimensions, ids in summary_dimensions(release, config, effective=effective):
        measured = [
            r for r in rows if r["slot"]["phase"] == "measured" and r["slot"]["case_id"] in ids
        ]
        trial = [r for r in measured if r["slot"]["repetition"] == 0]
        primary = dimensions["retrieval_mode"] == "primary"
        grades = [
            grade_asset_result(
                labels[r["slot"]["case_id"]], primary_asset_result(r) if primary else r["result"]
            )
            for r in trial
        ]
        counts = {
            c: sum(g["class"] == c for g in grades) for c in ("positive", "negative", "ambiguous")
        }
        hit = asset_rate(sum(g["hit3"] for g in grades), counts["positive"])
        precision = asset_rate(
            sum(g["confident_correct"] for g in grades), sum(g["confident"] for g in grades)
        )
        coverage = asset_rate(
            sum(g["class"] == "positive" and g["confident"] for g in grades), counts["positive"]
        )
        fallback = sum(len(r["attempts"]) > 1 for r in measured)
        latency = sorted(r["observed_ms"] for r in measured if r["observed_ms"] is not None)
        gates_required = (
            dimensions["kind"] == "all"
            and dimensions["availability"] == "all"
            and dimensions["stale"] is None
            and dimensions["subset"] != "shared_latin"
            and dimensions["lane"] != "zh_hant_diagnostic"
            and config["split"] != "diagnostic"
        )
        # Fixture/provisional inputs cannot establish an empirical pass. A known
        # failed bound stays visible even where qualification is incomplete.
        failed_bound = (
            any(
                rate["wilson95"] is not None and rate["wilson95"]["lower"] < threshold
                for rate, threshold in [(hit, 0.90), (precision, 0.95)]
            )
            or coverage["value"] is not None
            and coverage["value"] < 0.5
        )
        ambiguous_confident = any(g["class"] == "ambiguous" and g["confident"] for g in grades)
        multiplier = 5 if dimensions["lane"] == "all" else 1
        positive_minimum = 100 if dimensions["subset"] == "no_shared_latin" else 200 * multiplier
        minima_met = (
            counts["positive"] >= positive_minimum
            and counts["negative"] >= 50 * multiplier
            and counts["ambiguous"] >= 30 * multiplier
        )
        known_quality = all(rate["value"] is not None for rate in (hit, precision, coverage))
        if not gates_required:
            gate = "not_applicable"
        elif failed_bound or ambiguous_confident or known_quality and not minima_met:
            gate = "fail"
        elif (
            qualification != "qualified"
            or not known_quality
            or set(config["requested_kinds"]) != {"mcp_tool", "skill"}
            or dimensions["catalog_size"] != len(release.catalog["assets"])
        ):
            gate = "blocked"
        else:
            gate = "pass"
        summaries.append(
            dict(
                dimensions=dimensions,
                planned=len(measured),
                completed=sum(r["status"] == "ready" for r in measured),
                failed=sum(r["status"] == "failed" for r in measured),
                cancelled=sum(r["status"] == "cancelled" for r in measured),
                not_run=sum(r["status"] == "not_run" for r in measured),
                **counts,
                hit3=hit,
                confident_precision=precision,
                confident_coverage=coverage,
                false_confident=sum(g["false_confident"] for g in grades),
                abstained=sum(
                    (primary_asset_result(r) if primary else r["result"]) is not None
                    and (primary_asset_result(r) if primary else r["result"])["final_decision"]
                    in ("no_match", "empty_catalog")
                    for r in trial
                ),
                ambiguous_supported3=asset_rate(
                    sum(g["ambiguous_supported3"] for g in grades), counts["ambiguous"]
                ),
                ambiguous_choice=asset_rate(
                    sum(g["ambiguous_choice"] for g in grades), counts["ambiguous"]
                ),
                primary_failures=sum(
                    bool(r["attempts"]) and primary_asset_result(r) is None for r in measured
                ),
                fallback_used=fallback,
                fallback_fraction=asset_rate(fallback, len(measured), independent=False),
                latency=dict(
                    known=len(latency),
                    unknown=len(measured) - len(latency),
                    p50_ms=latency[math.ceil(0.5 * len(latency)) - 1] if latency else None,
                    p95_ms=latency[math.ceil(0.95 * len(latency)) - 1] if latency else None,
                ),
                costs=summarize_asset_costs(measured),
                gate=gate,
            )
        )
    return summaries
