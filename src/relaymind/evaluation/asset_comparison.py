"""Offline regrading and narrowly compatible retrieval comparisons (RM40/AC31)."""

import copy
import hashlib
import html
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from relaymind.assets.catalog import CatalogSnapshot, validate_asset_record
from relaymind.canonical import canonical_digest, canonical_json
from relaymind.evaluation.asset_benchmark import AssetBenchmarkReport, load_asset_benchmark_report
from relaymind.evaluation.asset_benchmark_io import validate_benchmark_document
from relaymind.evaluation.asset_corpus import assess_asset_release
from relaymind.evaluation.asset_evidence import (
    compatibility_policy,
    verify_asset_configuration_closure,
    verify_compiled_asset_inventory,
)
from relaymind.evaluation.asset_metrics import (
    grade_asset_result,
    primary_asset_result,
    summarize_asset_rows,
    summary_dimensions,
)
from relaymind.evaluation.asset_schedule import plan_asset_slots, validate_asset_rows
from relaymind.selection.asset_admission import admit_current, validate_against_offer
from relaymind.selection.asset_contract import (
    AttemptFailure,
    SelectorIdentity,
    parse_asset_request,
    parse_asset_result,
    parse_recommendation_result,
)
from relaymind.selection.asset_http import HttpAssetSelector
from relaymind.selection.asset_lexical import LexicalAssetSelector
from relaymind.selection.asset_preparation import (
    normalize_preparation,
    preparation_binding,
    preparation_policy_mismatches,
    validate_preparation_record,
)


@dataclass(frozen=True)
class AssetBenchmarkComparison:
    data: Mapping

    def to_json_object(self) -> dict:
        return copy.deepcopy(dict(self.data))


def _request_for_case(case, config, catalog, revision):
    return parse_asset_request(
        dict(
            schema_version=2,
            recommendation_id="00000000-0000-0000-0000-000000000000",
            task_id="evaluation",
            task_text=case["task_text"],
            conversation=case["conversation"],
            scope=config["scope"],
            permissions=config["permissions"],
            requested_kinds=config["requested_kinds"],
            selection_scope=config["selection_scope"],
            catalog_revision=revision,
            host_snapshot_revision=None,
            language_tags=case["language_tags"],
            code_switched=case["code_switched"],
            catalog=catalog,
            deadline_ms=1,
            candidate_limit=config["budgets"]["candidate_limit"],
        )
    )


def validate_report_evidence(report: AssetBenchmarkReport) -> tuple[list[dict], dict]:
    """Recompute every grade/summary; reject contradictory admitted evidence."""
    raw = report.data
    validate_benchmark_document("report", raw)
    if report.inputs is None:
        raise ValueError(report.input_error or "input_unresolved")
    inputs = report.inputs
    config = raw["config"]
    if canonical_digest(inputs.manifest) != raw["input_manifest"]["digest"]:
        raise ValueError("input_manifest_digest")
    for reference in inputs.manifest["documents"]:
        if canonical_digest(inputs.documents[reference["id"]]) != reference["digest"]:
            raise ValueError("document_digest")
    for reference in inputs.manifest["evidence"]:
        payload = inputs.evidence[reference["id"]]
        if (
            len(payload) != reference["bytes"]
            or hashlib.sha256(payload).hexdigest() != reference["sha256"]
        ):
            raise ValueError("evidence_digest")
    for kind, key in (
        ("runner_inventory", "runner_digest"),
        ("protocol_inventory", "protocol_digest"),
    ):
        inventory = inputs.documents[kind]
        verify_compiled_asset_inventory(kind, inventory)
        if raw["implementation"][key] != canonical_digest(inventory) or inputs.effective[
            "generator"
        ][key] != canonical_digest(inventory):
            raise ValueError("inventory_binding")
    frozen_config = inputs.documents["benchmark_config"]
    if any(
        config[key] != frozen_config[key]
        for key in ("split", "track", "seed", "repetitions", "warmups")
    ):
        raise ValueError("config_binding")
    if config["preparation"] != normalize_preparation(
        frozen_config.get("preparation"), http_primary=config["selector"]["transport"] == "http"
    ):
        raise ValueError("preparation_config_binding")
    validate_preparation_record(raw["preparation"], config=config["preparation"])
    effective = inputs.effective
    verify_asset_configuration_closure(
        inputs.documents, inputs.manifest, effective, inputs.documents["environment_lock"]
    )
    if (
        config["production_config_digest"] != canonical_digest(effective)
        or raw["lock_digest"] != inputs.release.lock["digest"]
    ):
        raise ValueError("effective_binding")
    for key in ("permissions", "selection_scope", "requested_kinds", "budgets"):
        if config[key] != effective[key]:
            raise ValueError("effective_binding")
    if config["scope"] != {
        key: effective["recommendations"][key] for key in ("user_id", "workspace_id")
    }:
        raise ValueError("effective_binding")
    if (
        config["fallback"] != effective["recommendations"]["fallback"]
        or config["adaptation_enabled"] != effective["adaptation"]["enabled"]
        or config["adaptation_config_digest"] != effective["adaptation"]["config_digest"]
    ):
        raise ValueError("effective_binding")
    if any(
        value is not None and value != raw["environment"][key]
        for key, value in inputs.documents["environment_lock"].items()
    ):
        raise ValueError("environment_binding")
    if (
        canonical_digest(config) != raw["config_digest"]
        or canonical_digest(raw["environment"]) != raw["environment_digest"]
    ):
        raise ValueError("report_digest")
    if inputs.effective["artifacts"] != raw["artifacts"]:
        raise ValueError("artifact_binding")
    if raw["qualification"] != assess_asset_release(inputs.release).status:
        raise ValueError("qualification_unverified")
    labels = {
        label["case_id"]: label
        for label in inputs.release.labels["adjudications"]
        if label["disposition"] == "include"
    }
    cases = {
        c["id"]: c
        for c in inputs.release.cases["cases"]
        if c["id"] in labels and c["split"] == config["split"]
    }
    if any(
        set(case["requested_kinds"]) != set(config["requested_kinds"]) for case in cases.values()
    ):
        raise ValueError("case_envelope_binding")
    slots = plan_asset_slots(
        cases, seed=config["seed"], repetitions=config["repetitions"], warmups=config["warmups"]
    )
    if raw["planned_slots"] != slots:
        raise ValueError("slot_plan")
    validate_asset_rows(slots, raw["results"])
    eligibility = {e["asset_id"] for e in inputs.effective["eligibility"] if e["eligible"]}
    catalog = [
        dict(a["record"])
        for a in inputs.release.catalog["assets"]
        if a["record"]["asset_id"] in eligibility
        and a["record"]["kind"] in config["requested_kinds"]
    ]
    expected_projection = canonical_digest(catalog)
    if config["preparation"]["mode"] == "http":
        prepared_binding = preparation_binding(
            selector={k: config["selector"][k] for k in ("name", "version")},
            catalog_revision=1 if eligibility else 0,
            catalog=catalog,
            scope=config["scope"],
            permissions=config["permissions"],
            selection_scope=config["selection_scope"],
            requested_kinds=config["requested_kinds"],
        )
        validate_preparation_record(
            raw["preparation"], config=config["preparation"], binding=prepared_binding
        )
        if raw["preparation"]["status"] != "ready" and (
            raw["status"] == "completed" or any(r["status"] != "not_run" for r in raw["results"])
        ):
            raise ValueError("preparation_dispatch")
    request_digests = {}
    for row in raw["results"]:
        label = labels[row["slot"]["case_id"]]
        expected_slot_cost = []
        if row["result"] is not None and row["result"]["decision"] == "empty_catalog":
            expected_slot_cost = [
                dict(provenance="observed", value=dict(LexicalAssetSelector.observed_cost))
            ]
        if canonical_json(row["observed_costs"]) != canonical_json(expected_slot_cost):
            raise ValueError("slot_cost_source")
        expected_grade = grade_asset_result(label, row["result"])
        if row["grade"] != expected_grade:
            raise ValueError("grade_mismatch")
        if row["status"] == "not_run":
            if (
                row["result"] is not None
                or any(row[k] for k in ("attempts", "embeddings", "observed_costs"))
                or any(
                    row[k] is not None
                    for k in (
                        "request_digest",
                        "offered_projection_digest",
                        "catalog_revision",
                        "observed_ms",
                    )
                )
                or row["resources"] != dict(cpu_ms=None, peak_bytes=None, coverage="unknown")
            ):
                raise ValueError("not_run_work")
            continue
        if (row["request_digest"] is None) != (row["offered_projection_digest"] is None) or (
            row["request_digest"] is None
        ) != (row["catalog_revision"] is None):
            raise ValueError("partial_offer_identity")
        if row["request_digest"] is None:
            if (
                row["status"] == "ready"
                or row["attempts"]
                or row["embeddings"]
                or row["observed_costs"]
            ):
                raise ValueError("dispatch_without_offer")
            continue
        request = _request_for_case(
            cases[row["slot"]["case_id"]], config, catalog, row["catalog_revision"]
        )
        stable = {
            k: v
            for k, v in request.to_json_object().items()
            if k not in ("recommendation_id", "task_id", "deadline_ms")
        }
        expected_request = canonical_digest(stable)
        if (
            row["request_digest"] != expected_request
            or row["offered_projection_digest"] != expected_projection
        ):
            raise ValueError("offer_digest")
        request_digests[row["slot"]["case_id"]] = (expected_request, expected_projection)
        snapshot = CatalogSnapshot(
            1,
            row["catalog_revision"],
            "2026-01-01T00:00:00Z",
            (),
            tuple(validate_asset_record(r) for r in catalog),
        )
        usable = []
        admitted_revision = snapshot.catalog_revision
        for i, attempt in enumerate(row["attempts"]):
            if usable:
                raise ValueError("attempt_after_admission")
            if attempt["attempt"] != i + 1:
                raise ValueError("attempt_order")
            identity = (
                SelectorIdentity(config["selector"]["name"], config["selector"]["version"])
                if i == 0
                else SelectorIdentity("lexical", "1")
            )
            if (
                attempt["selector"] != identity.to_json_object()
                or i
                and (
                    not config["fallback"]
                    or config["selector"]["transport"] == "in_process"
                    or attempt["mechanism"] != "gateway_lexical_fallback"
                )
            ):
                raise ValueError("attempt_identity")
            raw_result = attempt["result"]
            builtin = i > 0 or config["selector"]["transport"] == "in_process"
            expected_costs = (
                [dict(provenance="observed", value=dict(LexicalAssetSelector.observed_cost))]
                if builtin
                else []
            )
            if raw_result is not None and raw_result["usage_claims"] is not None:
                if builtin:
                    raise ValueError("builtin_usage_claim")
                expected_costs.append(raw_result["usage_claims"])
            if canonical_json(attempt["cost_observations"]) != canonical_json(expected_costs):
                raise ValueError("attempt_cost_source")
            expected_reported = None if raw_result is None else raw_result["elapsed_ms"]
            expected_index = None if raw_result is None else raw_result["index_revision"]
            if (
                attempt["reported_ms"] != expected_reported
                or attempt["index_revision"] != expected_index
            ):
                raise ValueError("attempt_reported_metadata")
            default_mechanism = (
                HttpAssetSelector.mechanism
                if config["selector"]["transport"] == "http"
                else LexicalAssetSelector.mechanism
            )
            if i == 0:
                if attempt["mechanism"] == "gateway_lexical_fallback":
                    raise ValueError("primary_floor_attribution")
                if raw_result is None:
                    if attempt["mechanism"] != default_mechanism:
                        raise ValueError("attempt_mechanism")
                elif attempt["mechanism"] != default_mechanism:
                    if attempt["mechanism"] != raw_result["mechanism"]:
                        raise ValueError("attempt_mechanism")
                    # A changed primary mechanism is recorded only after offer
                    # validation. Rejected pre-validation raw evidence retains
                    # the configured transport mechanism and remains visible.
                    claimed_request = parse_asset_request(
                        request.to_json_object()
                        | dict(recommendation_id=raw_result["recommendation_id"])
                    )
                    validate_against_offer(
                        parse_asset_result(raw_result),
                        claimed_request,
                        identity,
                        http=config["selector"]["transport"] == "http",
                    )
            elif (
                raw_result is not None and raw_result["mechanism"] != LexicalAssetSelector.mechanism
            ):
                raise ValueError("floor_mechanism")
            if attempt["outcome"] not in ("ranked", "no_match") and attempt["reason"] is None:
                raise ValueError("attempt_failure_reason")
            if attempt["outcome"] in ("ranked", "no_match"):
                if (
                    i == 0
                    and config["preparation"]["mode"] == "http"
                    and (
                        raw_result is None
                        or raw_result["index_revision"] != raw["preparation"]["index_revision"]
                    )
                ):
                    raise ValueError("prepared_index_binding")
                if (
                    raw_result is None
                    or raw_result["outcome"] != attempt["outcome"]
                    or attempt["reason"] is not None
                    or i == 0
                    and attempt["mechanism"] != raw_result["mechanism"]
                ):
                    raise ValueError("attempt_admission")
                # IDs are volatile, but actual attempt request/result IDs still
                # have to agree within the one admitted recommendation.
                actual_request = parse_asset_request(
                    request.to_json_object()
                    | dict(recommendation_id=raw_result["recommendation_id"])
                )
                result = parse_asset_result(raw_result)
                validate_against_offer(
                    result,
                    actual_request,
                    identity,
                    http=i == 0 and config["selector"]["transport"] == "http",
                )
                checked_admission = admit_current(
                    result, actual_request, snapshot, lambda r, req: r.asset_id in eligibility
                )
                admitted_revision = checked_admission.rechecked_catalog_revision
                usable.append(raw_result)
        if row["status"] == "ready":
            if row["reason"] is not None or row["result"] is None:
                raise ValueError("ready_contract")
            result = parse_recommendation_result(row["result"]).data
            if (
                result["catalog_revision"] != row["catalog_revision"]
                or result["attempts"] != row["attempts"]
                or result["embeddings"] != row["embeddings"]
                or result["host_snapshot_revision"] != request.data["host_snapshot_revision"]
                or result["admission"]["rechecked_catalog_revision"] != admitted_revision
                or result["fallback_reason"]
                != (row["attempts"][0]["reason"] if len(row["attempts"]) > 1 else None)
            ):
                raise ValueError("result_evidence")
            if result["decision"] == "empty_catalog":
                if catalog or row["attempts"]:
                    raise ValueError("empty_catalog_evidence")
            elif (
                not usable
                or result["base_candidates"] != usable[-1]["candidates"]
                or result["base_decision"] != usable[-1]["decision"]
            ):
                raise ValueError("result_producer")
            if result["adaptation"] is not None or result["host_run_ids"]:
                raise ValueError("unsupported_adaptation_or_host")
        elif row["result"] is not None or row["reason"] is None:
            raise ValueError("terminal_contract")
    if raw["status"] == "completed" and any(r["status"] == "not_run" for r in raw["results"]):
        raise ValueError("completed_missing_work")
    summaries = summarize_asset_rows(
        inputs.release,
        config,
        raw["results"],
        qualification=raw["qualification"],
        effective=inputs.effective,
    )
    if raw["summaries"] != summaries:
        raise ValueError("summary_mismatch")
    return summaries, request_digests


def _arm(raw):
    return dict(
        selector=copy.deepcopy(raw["config"]["selector"]), artifacts=copy.deepcopy(raw["artifacts"])
    )


def _comparison_preparation(report, verified):
    try:
        validate_preparation_record(report["preparation"], config=report["config"]["preparation"])
        if report["config"]["preparation"]["mode"] == "http" and verified is None:
            return None
        return copy.deepcopy(report["preparation"])
    except (ValueError, KeyError, TypeError):
        return None


def compare_asset_reports(
    a: AssetBenchmarkReport | Path | str,
    b: AssetBenchmarkReport | Path | str,
    *,
    quality_only: bool = False,
) -> AssetBenchmarkComparison:
    """Compare verified input closures; hardware alone may differ in quality mode."""
    a = load_asset_benchmark_report(a) if isinstance(a, (str, Path)) else a
    b = load_asset_benchmark_report(b) if isinstance(b, (str, Path)) else b
    for report in (a, b):
        validate_benchmark_document("report", report.data)
    reasons = []

    def reject(field, reason="mismatch"):
        reasons.append(dict(field=field, reason=reason))

    validated = []
    for side, report in [("a", a), ("b", b)]:
        try:
            validated.append(validate_report_evidence(report))
        except (AttemptFailure, ValueError, TypeError, KeyError, IndexError):
            reason = report.input_error or (
                "input_unresolved" if report.inputs is None else "evidence_unverified"
            )
            reject("/" + side, reason)
            validated.append(None)
    ar, br = a.data, b.data
    for key in ("lock_digest", "planned_slots"):
        if ar[key] != br[key]:
            reject("/" + key)
    invariant_keys = (
        "split",
        "track",
        "seed",
        "repetitions",
        "warmups",
        "scope",
        "permissions",
        "selection_scope",
        "requested_kinds",
        "budgets",
        "fallback",
        "adaptation_enabled",
        "adaptation_config_digest",
    )
    for key in invariant_keys:
        if ar["config"][key] != br["config"][key]:
            reject("/config/" + key)
    if ar["config"]["adaptation_enabled"] or br["config"]["adaptation_enabled"]:
        reject("/config/adaptation_enabled", "adaptation_requires_episode_protocol")
    for key in ("runner_digest", "protocol_digest"):
        if ar["implementation"][key] != br["implementation"][key]:
            reject("/implementation/" + key)
    if ar["implementation"]["dirty"] is None or br["implementation"]["dirty"] is None:
        reject("/implementation/dirty", "unknown")
    hardware = {"os", "os_version", "architecture", "cpu", "ram_bytes", "gpu"}
    for key in ar["environment"]:
        if quality_only and key in hardware:
            continue
        av, bv = ar["environment"][key], br["environment"][key]
        # No runtime is needed by either lexical or HTTP gateway transport.
        # Third-party selector backend identity is an intended arm variable.
        inapplicable = (
            key in ("runtime", "runtime_version", "runtime_config_digest")
            and av is None
            and bv is None
            and a.inputs is not None
            and b.inputs is not None
            and a.inputs.effective["recommendations"]["primary"] in ("lexical", "http")
            and b.inputs.effective["recommendations"]["primary"] in ("lexical", "http")
        )
        unknown_gpu = key == "gpu" and any(
            field is None
            for rows in (av, bv)
            if rows is not None
            for row in rows
            for field in (row["model"], row["device"], row["cores"])
        )
        if not inapplicable and (av is None or bv is None or unknown_gpu):
            reject("/environment/" + key, "unknown")
        elif av != bv:
            reject("/environment/" + key)
    if a.inputs is not None and b.inputs is not None:
        if compatibility_policy(a.inputs.effective) != compatibility_policy(b.inputs.effective):
            reject("/effective_config/policy")
        # Each private binding verifies against its own exact production input.
        # Stable reviewed source aliases, rather than machine-specific absolute
        # paths, are the normative cross-arm policy identity above.
        # Equal root locks commit closure, but compare the resolved corpus too.
        for key in ("catalog", "cases", "labels", "lock"):
            if getattr(a.inputs.release, key) != getattr(b.inputs.release, key):
                reject("/inputs/" + key)
    if validated[0] is not None and validated[1] is not None:
        for case_id in set(validated[0][1]) & set(validated[1][1]):
            if validated[0][1][case_id] != validated[1][1][case_id]:
                reject("/results/request_digest")
    ap, bp = ar["config"]["preparation"], br["config"]["preparation"]
    for key in preparation_policy_mismatches(ap, bp):
        reject("/config/preparation/" + key)
    reasons = sorted({(r["field"], r["reason"]) for r in reasons})
    data = dict(
        schema_version=1,
        reports=dict(a=canonical_digest(ar), b=canonical_digest(br)),
        arms=dict(a=_arm(ar), b=_arm(br)),
        mode="quality_only" if quality_only else "replacement",
        preparation=dict(
            a=_comparison_preparation(ar, validated[0]), b=_comparison_preparation(br, validated[1])
        ),
        status="incompatible" if reasons else "compatible",
        reasons=[dict(field=f, reason=r) for f, r in reasons],
        performance_comparable=not quality_only and not reasons,
        paired=[],
        gate_status="blocked",
    )
    if not reasons:
        a_summaries, b_summaries = validated[0][0], validated[1][0]
        b_map = {canonical_json(s["dimensions"]): s for s in b_summaries}
        a_rows = {
            r["slot"]["case_id"]: r
            for r in ar["results"]
            if r["slot"]["phase"] == "measured" and r["slot"]["repetition"] == 0
        }
        b_rows = {
            r["slot"]["case_id"]: r
            for r in br["results"]
            if r["slot"]["phase"] == "measured" and r["slot"]["repetition"] == 0
        }
        labels = {label["case_id"]: label for label in a.inputs.release.labels["adjudications"]}
        memberships = {
            canonical_json(dim): ids
            for dim, ids in summary_dimensions(
                a.inputs.release, ar["config"], effective=a.inputs.effective
            )
        }
        for summary in a_summaries:
            key = canonical_json(summary["dimensions"])
            other = b_map[key]
            primary = summary["dimensions"]["retrieval_mode"] == "primary"
            both = a_only = b_only = neither = 0
            for case_id in memberships[key]:
                ra, rb = a_rows[case_id], b_rows[case_id]
                ga = grade_asset_result(
                    labels[case_id], primary_asset_result(ra) if primary else ra["result"]
                )
                gb = grade_asset_result(
                    labels[case_id], primary_asset_result(rb) if primary else rb["result"]
                )
                ha, hb = ga["hit3"], gb["hit3"]
                both += ha and hb
                a_only += ha and not hb
                b_only += hb and not ha
                neither += not ha and not hb

            def delta(k, summary=summary, other=other):
                av, bv = summary[k]["value"], other[k]["value"]
                return None if av is None or bv is None else bv - av

            data["paired"].append(
                dict(
                    dimensions=summary["dimensions"],
                    cases=both + a_only + b_only + neither,
                    a_only_hit=a_only,
                    b_only_hit=b_only,
                    both_hit=both,
                    neither_hit=neither,
                    hit3_difference=delta("hit3"),
                    confident_precision_difference=delta("confident_precision"),
                    confident_coverage_difference=delta("confident_coverage"),
                    a=summary,
                    b=other,
                )
            )
        gates = {s[side]["gate"] for s in data["paired"] for side in ("a", "b")}
        data["gate_status"] = (
            "fail"
            if "fail" in gates
            else "blocked"
            if "blocked" in gates
            else "pass"
            if "pass" in gates
            else "not_applicable"
        )
    validate_benchmark_document("comparison", data)
    return AssetBenchmarkComparison(data)


def render_asset_comparison(comparison: AssetBenchmarkComparison | Mapping) -> str:
    """Render exclusively from the self-contained closed comparison object."""
    raw = (
        comparison.to_json_object()
        if isinstance(comparison, AssetBenchmarkComparison)
        else comparison
    )
    validate_benchmark_document("comparison", raw)
    lines = [
        "# Asset selector comparison",
        "",
        f"Status: {raw['status']}; mode: {raw['mode']}; gates: {raw['gate_status']}.",
        "",
        "Performance and costs: "
        + ("comparable." if raw["performance_comparable"] else "not comparable."),
        "",
    ]
    for side in ("a", "b"):
        arm = raw["arms"][side]
        selector = arm["selector"]
        # JSON quoting prevents source-supplied identifiers from becoming markup.
        lines += [
            f"Arm {side}: `{html.escape(canonical_json(selector)).replace(chr(96), '&#96;')}`.",
            f"Report hash: `{raw['reports'][side]}`.",
            "Preparation: `"
            + html.escape(canonical_json(raw["preparation"][side])).replace(chr(96), "&#96;")
            + "`.",
            "Artifacts: `"
            + html.escape(canonical_json(arm["artifacts"])).replace(chr(96), "&#96;")
            + "`.",
            "",
        ]
    if raw["reasons"]:
        lines += ["| Field | Reason |", "| --- | --- |"]
        lines += [f"| `{r['field']}` | `{r['reason']}` |" for r in raw["reasons"]]
        return "\n".join(lines) + "\n"
    lines += [
        "Quality rates use measured repetition zero only. Workload counts, latency "
        "and costs cover every measured repetition. Diagnostic subgroup totals are "
        "non-additive. Fixture/provisional evidence cannot establish acceptance.",
        "",
    ]
    for pair in raw["paired"]:
        dim = pair["dimensions"]
        lines += [
            f"## {dim['lane']} / {dim['kind']} / {dim['availability']} / "
            f"stale={dim['stale']} / {dim['retrieval_mode']} / {dim['subset']}",
            "",
            f"Independent cases: {pair['cases']}; a-only hit {pair['a_only_hit']}, "
            f"b-only hit {pair['b_only_hit']}, both {pair['both_hit']}, "
            f"neither {pair['neither_hit']}.",
            "",
            "| Arm | Hit@3 (Wilson 95%) | Confident precision (Wilson 95%) | "
            "Positive confident coverage | Planned/ready/failed/cancelled/not run | p50 / p95 ms |",
            "| --- | --- | --- | --- | --- | --- |",
        ]

        def rate(r):
            interval = r["wilson95"]
            return f"{r['numerator']}/{r['denominator']}" + (
                f" [{interval['lower']:.6f}, {interval['upper']:.6f}]" if interval else " [unknown]"
            )

        for side in ("a", "b"):
            s = pair[side]
            lines.append(
                f"| {side} | {rate(s['hit3'])} | {rate(s['confident_precision'])} | "
                f"{rate(s['confident_coverage'])} | {s['planned']}/{s['ready']}/"
                f"{s['failed']}/{s['cancelled']}/{s['not_run']} | "
                f"{s['latency']['p50_ms']} / {s['latency']['p95_ms']} |"
            )
        lines += [
            "",
            "Cost totals and known subtotals are retrieval-only; "
            "unknown coverage never implies zero.",
            "",
            "| Arm | Metric | Provenance | Known / unknown rows | "
            "Complete total | Known subtotal (partial if unknown) |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for side in ("a", "b"):
            for cost in pair[side]["costs"]:
                lines.append(
                    f"| {side} | {cost['metric']} | {cost['provenance']} | "
                    f"{cost['known']} / {cost['unknown']} | {cost['total']} | "
                    f"{cost['known_subtotal']} |"
                )
        lines.append("")
    return "\n".join(lines) + "\n"
