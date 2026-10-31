"""Source-grounded runs through the production service and shared benchmark helpers."""

import asyncio
import math
import tempfile
import time
import unicodedata
import uuid
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from relaymind.canonical import canonical_digest, canonical_json
from relaymind.evaluation.asset_benchmark import (
    _load_production_config,
    _LockedProjectionProvider,
    _not_run,
    benchmark_repository_root,
    stable_request_digest,
    terminal_payload,
)
from relaymind.evaluation.asset_benchmark_io import REPORT_BYTES, validate_benchmark_document
from relaymind.evaluation.asset_corpus_io import _VALIDATORS, AssetCorpusError
from relaymind.evaluation.asset_costs import summarize_asset_costs
from relaymind.evaluation.asset_diagnostic import QUERY_LANGUAGE_TAGS, VerifiedDiagnostic
from relaymind.evaluation.asset_evidence import (
    compatibility_policy,
    effective_asset_config,
    file_inventory,
)
from relaymind.evaluation.asset_metrics import grade_asset_result, primary_asset_result
from relaymind.evaluation.asset_schedule import plan_asset_slots, validate_asset_rows
from relaymind.evaluation.asset_source_catalog import source_language
from relaymind.evaluation.asset_splits import classify_latin_overlap, input_identity
from relaymind.recommendations.errors import RecommendationError
from relaymind.recommendations.service import CancellationSignal, RecommendationService
from relaymind.selection.asset_contract import SelectorContext, parse_recommendation_result
from relaymind.selection.asset_factory import build_asset_selector
from relaymind.selection.asset_lexical import LexicalAssetSelector
from relaymind.selection.asset_preparation import (
    PreparedAssetSelector,
    normalize_preparation,
    preparation_binding,
    preparation_policy_mismatches,
    prepare_asset_index,
    request_preparation_binding,
    validate_preparation_record,
)
from relaymind.store.recommendations import RecommendationClaim, RecommendationStore
from relaymind.store.run_store import RunStore


class _ObservedPrimary:
    """Record the actual gateway dispatch, including its real ID/deadline."""

    def __init__(self, primary, row):
        self.primary, self.row = primary, row

    def __getattr__(self, name):
        return getattr(self.primary, name)

    async def select(self, request, context):
        self.row["request"] = request.to_json_object()
        return await self.primary.select(request, context)


def summarize_source_latency(rows: list[Mapping]) -> dict:
    """Nearest-rank observed durations retain failed/cancelled work and unknowns."""
    values = sorted(row["observed_ms"] for row in rows if row["observed_ms"] is not None)
    if any(
        type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values
    ):
        raise AssetCorpusError("invalid_latency")
    return dict(
        known=len(values),
        unknown=len(rows) - len(values),
        p50_ms=values[math.ceil(0.5 * len(values)) - 1] if values else None,
        p95_ms=values[math.ceil(0.95 * len(values)) - 1] if values else None,
    )


def _summary(rows: list[Mapping]) -> dict:
    # Quality counts use each preregistered reference once, never repeated observations.
    first = [r for r in rows if r["slot"]["repetition"] == 0]
    positive = [r for r in first if r["grade"]["class"] == "positive"]
    return dict(
        reference_cases=len(first),
        positive_cases=len(positive),
        hit3=sum(r["grade"]["hit3"] for r in positive),
        top1=sum(
            bool(r["evaluated_candidates"])
            and r["evaluated_candidates"][0]["asset_id"] in r["acceptable_asset_ids"]
            for r in positive
        ),
        confident=sum(r["grade"]["confident"] for r in first),
        confident_correct=sum(r["grade"]["confident_correct"] for r in first),
        false_confident=sum(r["grade"]["false_confident"] for r in first),
        choice_required=sum(r["evaluated_decision"] == "choice_required" for r in first),
        choice_supported3=sum(
            r["evaluated_decision"] == "choice_required"
            and bool(
                set(r["acceptable_asset_ids"])
                & {c["asset_id"] for c in r["evaluated_candidates"][:3]}
            )
            for r in first
        ),
        user_choices=sum(r["user_choice"] is not None for r in first),
        user_choice_correct=sum(r["user_choice"] in r["acceptable_asset_ids"] for r in first),
        abstentions=sum(r["evaluated_decision"] in ("no_match", "empty_catalog") for r in first),
        failed=sum(r["status"] != "ready" for r in first),
        primary_attempt_failures=sum(
            bool(r["attempts"]) and r["attempts"][0]["outcome"] not in ("ranked", "no_match")
            for r in first
        ),
        primary_not_ready=sum(
            bool(r["attempts"]) and r["attempts"][0]["outcome"] == "not_ready" for r in first
        ),
    )


async def run_source_benchmark(
    inputs: VerifiedDiagnostic,
    *,
    production_config: Path,
    track: str = "service",
    repetitions: int = 1,
    seed: int = 0,
    user_choices: Mapping[str, str] | None = None,
    preparation: Mapping | None = None,
) -> dict:
    """Run all source-grounded references; no independent-human qualification is inferred."""
    if not isinstance(inputs, VerifiedDiagnostic) or track not in ("primary_only", "service"):
        raise AssetCorpusError("invalid_development_profile")
    cases = {c["id"]: c for c in inputs.document["cases"]}
    choices = dict(user_choices or {})
    if set(choices) - set(cases):
        raise AssetCorpusError("invalid_user_choice")
    slots = plan_asset_slots(cases, seed=seed, repetitions=repetitions, warmups=0)
    production = _load_production_config(Path(production_config))
    setup_config = normalize_preparation(
        preparation, http_primary=production.recommendations.primary == "http"
    )
    kinds = sorted({k for c in cases.values() for k in c["requested_kinds"]})
    if setup_config["mode"] == "http" and any(
        sorted(c["requested_kinds"]) != kinds for c in cases.values()
    ):
        raise AssetCorpusError("incompatible_preparation_workload")
    root = benchmark_repository_root()
    runner = file_inventory(root)
    protocol = file_inventory(root, protocol=True)
    effective = effective_asset_config(
        production,
        inputs,
        runner_digest=canonical_digest(runner),
        protocol_digest=canonical_digest(protocol),
    )
    if not all(e["eligible"] for e in effective["eligibility"]):
        raise AssetCorpusError("catalog_source_not_enabled")
    provider = _LockedProjectionProvider(inputs, effective)
    started = time.monotonic()
    await provider.publish()
    preparation_ms = (time.monotonic() - started) * 1000
    rows = []
    retained_bytes = 0
    status = "completed"
    prepared_primary = None
    setup = await prepare_asset_index(normalize_preparation(None), None, [])
    if setup_config["mode"] == "http":
        offer = provider.pin(kinds)
        setup = await prepare_asset_index(
            setup_config,
            _source_binding(effective, list(offer.catalog), offer.catalog_revision, kinds),
            list(offer.catalog),
        )
        if setup["status"] != "ready":
            status = "interrupted" if setup["status"] == "cancelled" else "blocked"
        else:
            prepared_primary = PreparedAssetSelector(
                build_asset_selector(production.recommendations), setup
            )
    with tempfile.TemporaryDirectory(prefix="source-benchmark-") as directory:
        runs = RunStore(Path(directory) / "disposable.sqlite3")
        service = RecommendationService(
            RecommendationStore(runs), provider, production.recommendations
        )
        try:
            for slot in slots:
                case = cases[slot["case_id"]]
                label = {
                    "class": case["reference_class"],
                    "acceptable_asset_ids": list(case["acceptable_asset_ids"]),
                }
                row = _not_run(slot, label)
                row.update(
                    user_choice=None,
                    acceptable_asset_ids=list(case["acceptable_asset_ids"]),
                    reference_kind=case["reference_kind"],
                    query_language=case["query_language"],
                    request=None,
                    terminal_observation=None,
                    evaluated_decision=None,
                    evaluated_candidates=[],
                )
                row["hard_subset"], row["latin_token_intersections"] = _latin_overlap(
                    case, inputs.catalog, unicodedata.unidata_version
                )
                if status != "completed":
                    row["reason"] = (
                        "interrupted" if status == "interrupted" else "preparation_unavailable"
                    )
                    rows.append(row)
                    continue
                language = case["query_language"]
                request = dict(
                    schema_version=1,
                    task_text=case["task_text"],
                    conversation=list(case["conversation"]),
                    requested_kinds=list(case["requested_kinds"]),
                    selection_scope="discovery",
                    host_snapshot_revision=None,
                    language_tags=list(QUERY_LANGUAGE_TAGS[language]),
                    code_switched=language == "mixed",
                )
                budget = service.new_budget()
                cancel = CancellationSignal()
                try:
                    offer = service.precheck(request)
                    envelope = dict(
                        request,
                        schema_version=2,
                        recommendation_id=str(uuid.uuid4()),
                        task_id=case["id"],
                        scope=provider.scope,
                        permissions=list(provider.permissions),
                        catalog_revision=offer.catalog_revision,
                        deadline_ms=budget.remaining_ms(),
                        candidate_limit=10,
                    )
                    selection = offer.request(
                        envelope, context=SelectorContext(budget.clock.monotonic, budget.check)
                    )
                    row.update(
                        request_digest=stable_request_digest(selection.to_json_object()),
                        offered_projection_digest=canonical_digest(list(offer.catalog)),
                        catalog_revision=offer.catalog_revision,
                    )
                    primary = prepared_primary or (
                        LexicalAssetSelector(offer.index)
                        if production.recommendations.primary == "lexical"
                        else build_asset_selector(production.recommendations)
                    )
                    service.primary = _ObservedPrimary(primary, row)
                    claim = RecommendationClaim(
                        provider.scope,
                        "source-development",
                        "POST",
                        "/relay/v1/recommendations",
                        None,
                        str(slot["ordinal"]),
                        canonical_digest(request),
                    )
                    response = await service.create(request, claim, budget, cancel)
                    body = (
                        dict(service.get(provider.scope, cancel.recommendation_id))
                        if cancel.recommendation_id
                        else dict(response.body)
                    )
                    if body.get("state") == "ready":
                        result = dict(body["result"])
                        row.update(
                            status="ready",
                            reason=None,
                            result=result,
                            attempts=list(result["attempts"]),
                            embeddings=list(result["embeddings"]),
                        )
                    else:
                        terminal = terminal_payload(
                            service,
                            provider.scope,
                            body["recommendation_id"],
                            ("failed", "cancelled"),
                        )
                        row.update(
                            status="cancelled" if body.get("state") == "cancelled" else "failed",
                            reason=(body.get("error") or {}).get(
                                "code", "recommendation_unavailable"
                            ),
                            terminal_observation=terminal,
                            attempts=list(terminal["attempts"]),
                            embeddings=list(terminal.get("embeddings", [])),
                        )
                except RecommendationError as exc:
                    row.update(status="failed", reason=exc.code)
                except asyncio.CancelledError:
                    # Service.create commits its cancellation before propagating it.
                    # Retain that terminal observation before the temporary store closes.
                    status = "interrupted"
                    row.update(status="cancelled", reason="cancelled")
                    if cancel.recommendation_id is not None:
                        terminal = terminal_payload(
                            service, provider.scope, cancel.recommendation_id, ("cancelled",)
                        )
                        row.update(
                            terminal_observation=terminal,
                            attempts=list(terminal["attempts"]),
                            embeddings=list(terminal.get("embeddings", [])),
                        )
                finally:
                    row["observed_ms"] = budget.elapsed_ms()
                    # The gateway fences late results; drain its owned selector tasks
                    # before disposable persistence can disappear.
                    await budget.drain(0.1)
                evaluated = primary_asset_result(row) if track == "primary_only" else row["result"]
                if evaluated is not None:
                    row.update(
                        evaluated_decision=evaluated["final_decision"],
                        evaluated_candidates=list(evaluated["final_candidates"]),
                    )
                row["grade"] = grade_asset_result(label, evaluated)
                if case["id"] in choices:
                    visible = {c["asset_id"] for c in row["evaluated_candidates"][:3]}
                    if (
                        row["evaluated_decision"] != "choice_required"
                        or choices[case["id"]] not in visible
                    ):
                        raise AssetCorpusError("invalid_user_choice")
                    row["user_choice"] = choices[case["id"]]
                retained_bytes += len(canonical_json(row).encode())
                if retained_bytes > REPORT_BYTES:
                    raise AssetCorpusError("report_limit")
                rows.append(row)
        finally:
            await service.stop()
            runs.close()
    validate_asset_rows(slots, rows)
    pairs = _language_pairs(rows, inputs.catalog)
    assets = {a["record"]["asset_id"]: a for a in inputs.catalog["assets"]}
    report = dict(
        profile="source_grounded_development",
        status=status,
        qualification="provisional",
        acceptance_eligible=False,
        input_document=inputs.document,
        catalog_document=inputs.catalog,
        effective_config=effective,
        dataset_id=inputs.document["dataset_id"],
        cases_digest=inputs.document["digest"],
        catalog_digest=inputs.document["catalog_digest"],
        config_digest=canonical_digest(effective),
        policy_digest=canonical_digest(compatibility_policy(effective)),
        runner_digest=canonical_digest(runner),
        protocol_digest=canonical_digest(protocol),
        track=track,
        seed=seed,
        repetitions=repetitions,
        planned_slots=len(slots),
        slots=slots,
        index_preparation_ms=preparation_ms,
        preparation=setup,
        source_counts={
            "skill": sum(a["record"]["kind"] == "skill" for a in assets.values()),
            "mcp_tool": sum(a["record"]["kind"] == "mcp_tool" for a in assets.values()),
        },
        exclusions=list(inputs.catalog["exclusions"]),
        unicode_version=unicodedata.unidata_version,
        reference_method=(
            "Preregistered source-grounded/author-defined references; "
            "no independent human agreement."
        ),
        subgroup_totals="non-additive across acceptable target languages and kinds",
        unique_input_cases=len({input_identity(c) for c in cases.values()}),
        coverage=_reference_coverage(inputs.document, inputs.catalog),
        summary=_summary(rows),
        language_pairs=pairs,
        missing_language_pairs=_missing_pairs(pairs),
        rows=rows,
        latency=summarize_source_latency(rows),
        costs=summarize_asset_costs(rows),
        limits=["independent_annotation_not_asserted", "resource_coverage_unknown"],
    )
    if len(canonical_json(report).encode()) > REPORT_BYTES:
        raise AssetCorpusError("report_limit")
    return report


def _latin_overlap(case, catalog, unicode_version):
    """The hard subset of one case and its Latin token intersections."""
    overlap = classify_latin_overlap(
        case,
        [
            a["record"]
            for a in catalog["assets"]
            if a["record"]["asset_id"] in case["acceptable_asset_ids"]
        ],
        unicode_version=unicode_version,
    )
    hard = (
        ("no_shared_latin" if overlap.all_targets_disjoint else "shared_latin")
        if case["reference_class"] == "positive"
        else "negative_or_ambiguous"
    )
    return hard, {k: list(v) for k, v in overlap.token_intersections.items()}


def _language_pairs(rows, catalog):
    assets = {a["record"]["asset_id"]: a for a in catalog["assets"]}
    groups = {}
    for row in rows:
        targets = [assets[x]["record"] for x in row["acceptable_asset_ids"]]
        dimensions = {(source_language(a["description"]), a["kind"]) for a in targets} or {
            ("unknown", "all")
        }
        for language, kind in dimensions:
            key = (row["query_language"], {"zh-Hans": "zh"}.get(language, language), kind)
            groups.setdefault(key, []).append(row)
    return [
        dict(query_language=k[0], target_description_language=k[1], kind=k[2], **_summary(v))
        for k, v in sorted(groups.items())
    ]


def _source_binding(effective, catalog, revision, kinds):
    settings = effective["recommendations"]
    return preparation_binding(
        selector={k: settings[k] for k in ("name", "version")},
        catalog_revision=revision,
        catalog=catalog,
        scope={k: settings[k] for k in ("user_id", "workspace_id")},
        permissions=effective["permissions"],
        selection_scope="discovery",
        requested_kinds=kinds,
    )


def _reference_coverage(doc, catalog):
    assets = {a["record"]["asset_id"]: a for a in catalog["assets"]}
    targeted = {asset_id for case in doc["cases"] for asset_id in case["acceptable_asset_ids"]}
    families = {family for asset_id in targeted for family in assets[asset_id]["asset_family_ids"]}
    bodies = Counter(
        (a["record"]["source_id"], tuple(a["original_languages"])) for a in assets.values()
    )
    descriptions = Counter(
        (a["record"]["source_id"], tuple(a["record"]["language_tags"])) for a in assets.values()
    )
    return dict(
        catalog_assets=len(assets),
        targeted_assets=len(targeted),
        targeted_skills=sum(assets[x]["record"]["kind"] == "skill" for x in targeted),
        targeted_tools=sum(assets[x]["record"]["kind"] == "mcp_tool" for x in targeted),
        targeted_source_family_tags=len(families),
        family_method="Source identity tags, not independently reviewed semantic families.",
        reference_methods=dict(Counter(c["reference_kind"] for c in doc["cases"])),
        language_method=(
            "Han/ASCII character heuristic: Han>=10% is zh; remaining Han/Latin is mixed. "
            "Skill body and MCP description; no native-author assertion."
        ),
        original_languages=[
            dict(source_id=k[0], languages=list(k[1]), assets=v) for k, v in sorted(bodies.items())
        ],
        description_languages=[
            dict(source_id=k[0], languages=list(k[1]), assets=v)
            for k, v in sorted(descriptions.items())
        ],
    )


def _missing_pairs(pairs):
    present = {(p["query_language"], p["target_description_language"], p["kind"]) for p in pairs}
    return [
        dict(query_language=query, target_description_language=target, kind=kind)
        for query in ("zh", "en", "mixed")
        for target in ("zh", "en", "mixed")
        for kind in ("skill", "mcp_tool")
        if (query, target, kind) not in present
    ]


def _produced_by_admitted_attempt(result: Mapping) -> bool:
    """RM35/RM40: base output is exactly the admitted producer's own result.

    The chain admits the last attempt and copies its decision and candidates;
    an edited decision or ranking that no attempt produced is not evidence.
    """
    if result["decision"] == "empty_catalog":
        return not result["attempts"]
    if not result["attempts"]:
        return False
    producer = result["attempts"][-1]
    output = producer["result"]
    return (
        producer["outcome"] in ("ranked", "no_match")
        and output is not None
        and output["decision"] == result["base_decision"]
        and output["candidates"] == result["base_candidates"]
    )


def _verify_development_report(report: Mapping) -> None:
    try:
        doc, catalog = report["input_document"], report["catalog_document"]
        if not _VALIDATORS["diagnostic"].is_valid(doc) or not _VALIDATORS["catalog_set"].is_valid(
            catalog
        ):
            raise ValueError
        digest = canonical_digest({k: v for k, v in doc.items() if k != "digest"})
        if (
            digest != doc["digest"]
            or digest != report["cases_digest"]
            or canonical_digest(catalog) != report["catalog_digest"]
            or doc["catalog_digest"] != report["catalog_digest"]
        ):
            raise ValueError
        if report["qualification"] != "provisional" or report["acceptance_eligible"] is not False:
            raise ValueError
        if (
            canonical_digest(report["effective_config"]) != report["config_digest"]
            or canonical_digest(compatibility_policy(report["effective_config"]))
            != report["policy_digest"]
        ):
            raise ValueError
        cases = {c["id"]: c for c in doc["cases"]}
        setup = report["preparation"]
        validate_benchmark_document("report", setup, definition="PreparationRecord")
        kinds = sorted({k for c in cases.values() for k in c["requested_kinds"]})
        binding = None
        if setup["config"]["mode"] == "http":
            if report["effective_config"]["recommendations"]["primary"] != "http" or any(
                sorted(c["requested_kinds"]) != kinds for c in cases.values()
            ):
                raise ValueError
            offered = [a["record"] for a in catalog["assets"] if a["record"]["kind"] in kinds]
            binding = _source_binding(
                report["effective_config"], offered, 1 if catalog["assets"] else 0, kinds
            )
        validate_preparation_record(setup, binding=binding)
        if setup["status"] not in ("ready", "not_applicable") and any(
            r["status"] != "not_run" for r in report["rows"]
        ):
            raise ValueError
        slots = plan_asset_slots(
            cases, seed=report["seed"], repetitions=report["repetitions"], warmups=0
        )
        if slots != report["slots"] or report["planned_slots"] != len(slots):
            raise ValueError
        validate_asset_rows(slots, report["rows"])
        if report["track"] not in ("primary_only", "service"):
            raise ValueError
        if report["unicode_version"] != unicodedata.unidata_version:
            raise ValueError
        for row in report["rows"]:
            case = cases[row["slot"]["case_id"]]
            if any(
                row[key] != case[key]
                for key in ("acceptable_asset_ids", "query_language", "reference_kind")
            ):
                raise ValueError
            if row["result"] is not None:
                parse_recommendation_result(row["result"])
                if (
                    row["status"] != "ready"
                    or row["attempts"] != row["result"]["attempts"]
                    or row["embeddings"] != row["result"]["embeddings"]
                    or not _produced_by_admitted_attempt(row["result"])
                ):
                    raise ValueError
            elif row["terminal_observation"] is not None:
                terminal = row["terminal_observation"]
                if (
                    row["attempts"] != terminal["attempts"]
                    or row["embeddings"] != terminal.get("embeddings", [])
                    or row["status"] not in ("failed", "cancelled")
                ):
                    raise ValueError
            elif row["attempts"] or row["embeddings"]:
                raise ValueError
            evaluated = (
                primary_asset_result(row) if report["track"] == "primary_only" else row["result"]
            )
            decision = None if evaluated is None else evaluated["final_decision"]
            candidates = [] if evaluated is None else evaluated["final_candidates"]
            if row["evaluated_decision"] != decision or row["evaluated_candidates"] != candidates:
                raise ValueError
            if row["user_choice"] is not None and (
                decision != "choice_required"
                or row["user_choice"] not in {c["asset_id"] for c in candidates[:3]}
            ):
                raise ValueError
            if (row["hard_subset"], row["latin_token_intersections"]) != _latin_overlap(
                case, catalog, report["unicode_version"]
            ):
                raise ValueError
            expected = grade_asset_result(
                {
                    "class": case["reference_class"],
                    "acceptable_asset_ids": case["acceptable_asset_ids"],
                },
                evaluated,
            )
            if row["grade"] != expected:
                raise ValueError
            offered = [
                a["record"]
                for a in catalog["assets"]
                if a["record"]["kind"] in case["requested_kinds"]
            ]
            if row["request"] is not None:
                request = row["request"]
                if (
                    request["task_text"] != case["task_text"]
                    or request["conversation"] != case["conversation"]
                    or request["requested_kinds"] != case["requested_kinds"]
                    or request["catalog"] != offered
                ):
                    raise ValueError
                if (
                    setup["config"]["mode"] == "http"
                    and request_preparation_binding(binding["selector"], request) != binding
                ):
                    raise ValueError
            elif row["attempts"] and row["attempts"][0]["outcome"] in ("ranked", "no_match"):
                raise ValueError
            if row["attempts"] or row["result"] is not None:
                if canonical_digest(offered) != row["offered_projection_digest"]:
                    raise ValueError
            by_id = {a["asset_id"]: a for a in offered}
            retained_candidates = list(candidates)
            if row["result"] is not None:
                retained_candidates += row["result"]["base_candidates"]
                retained_candidates += row["result"]["final_candidates"]
            for ordinal, attempt in enumerate(row["attempts"]):
                if (
                    setup["config"]["mode"] == "http"
                    and ordinal == 0
                    and attempt["outcome"] in ("ranked", "no_match")
                    and attempt["result"]["index_revision"] != setup["index_revision"]
                ):
                    raise ValueError
                if attempt["outcome"] in ("ranked", "no_match") and attempt["result"] is not None:
                    retained_candidates += attempt["result"]["candidates"]
            for candidate in retained_candidates:
                record = by_id[candidate["asset_id"]]
                if any(
                    candidate[key] != record[key]
                    for key in ("kind", "content_hash", "source_version")
                ) or (
                    "availability" in candidate
                    and candidate["availability"] != record["availability"]
                ):
                    raise ValueError
        if report["summary"] != _summary(report["rows"]) or report[
            "costs"
        ] != summarize_asset_costs(report["rows"]):
            raise ValueError
        if report["latency"] != summarize_source_latency(report["rows"]):
            raise ValueError
        if report["language_pairs"] != _language_pairs(report["rows"], catalog):
            raise ValueError
        if report["missing_language_pairs"] != _missing_pairs(report["language_pairs"]) or report[
            "coverage"
        ] != _reference_coverage(doc, catalog):
            raise ValueError
    except (ValueError, TypeError, KeyError, IndexError, RecursionError):
        raise AssetCorpusError("invalid_development_report") from None


def compare_source_benchmarks(left: Mapping, right: Mapping) -> dict:
    """Pair the same frozen development workload; selector identity may differ."""
    keys = (
        "profile",
        "cases_digest",
        "catalog_digest",
        "policy_digest",
        "track",
        "seed",
        "repetitions",
        "slots",
    )
    if any(left.get(k) != right.get(k) for k in keys):
        raise AssetCorpusError("incompatible_development_runs")
    _verify_development_report(left)
    _verify_development_report(right)
    if preparation_policy_mismatches(left["preparation"]["config"], right["preparation"]["config"]):
        raise AssetCorpusError("incompatible_development_runs")
    return dict(
        profile="source_grounded_development",
        compatible=True,
        acceptance_eligible=False,
        catalog_digest=left["catalog_digest"],
        cases_digest=left["cases_digest"],
        left=left["summary"],
        right=right["summary"],
        latency={"left": left["latency"], "right": right["latency"]},
        costs={"left": left["costs"], "right": right["costs"]},
        preparation={"left": left["preparation"], "right": right["preparation"]},
        paired_slots=len(left["rows"]),
        language_pairs={"left": left["language_pairs"], "right": right["language_pairs"]},
        delta_hit3=right["summary"]["hit3"] - left["summary"]["hit3"],
    )
