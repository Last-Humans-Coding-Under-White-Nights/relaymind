"""RM35/RM40 AC26/AC31: offline paired comparison cannot trust saved grades."""

import asyncio
import copy
import json

import pytest

from test_asset_selection_benchmark import fixture_config, module
from test_asset_selection_benchmark import http_example as http_example


@pytest.fixture(scope="module")
def saved_path(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("benchmark-comparison")
    api = module("asset_benchmark")
    config = fixture_config(tmp_path)
    report = asyncio.run(api.run_asset_benchmark(config))
    path = tmp_path / "a.json"
    api.write_asset_benchmark_report(report, path)
    return path


@pytest.fixture(scope="module")
def saved(saved_path):
    return module("asset_benchmark").load_asset_benchmark_report(
        saved_path, resolver=saved_path.with_name(saved_path.name + ".resolver.json")
    )


def comparison(a, b, **kwargs):
    return module("asset_comparison").compare_asset_reports(a, b, **kwargs).to_json_object()


def test_quality_only_recomputes_and_renders_self_contained(saved):
    api = module("asset_comparison")
    result = api.compare_asset_reports(saved, saved, quality_only=True)
    data = result.to_json_object()
    assert data["status"] == "compatible" and data["performance_comparable"] is False
    overall = next(
        p
        for p in data["paired"]
        if p["dimensions"]["lane"] == "all"
        and p["dimensions"]["kind"] == "all"
        and p["dimensions"]["availability"] == "all"
        and p["dimensions"]["stale"] is None
        and p["dimensions"]["retrieval_mode"] == "service"
    )
    assert overall["cases"] == 1 and overall["both_hit"] == 1
    assert overall["a"]["planned"] == 2 and overall["a"]["positive"] == 1
    assert data["gate_status"] != "pass"
    # Renderer receives only the closed comparison, never source reports.
    markdown = api.render_asset_comparison(copy.deepcopy(data))
    assert "lexical" in markdown and "not comparable" in markdown
    assert "known subtotal" in markdown.lower() and "0" in markdown


@pytest.mark.parametrize(
    "mutation", ["grade", "summary", "missing_row", "digest", "offered", "artifact", "slot"]
)
def test_rejects_tampered_report_even_quality_only(saved, mutation):
    other = copy.copy(saved)
    other.data = copy.deepcopy(saved.data)
    if mutation == "grade":
        other.data["results"][0]["grade"]["hit3"] = False
    if mutation == "summary":
        other.data["summaries"][0]["hit3"]["numerator"] = 0
    if mutation == "missing_row":
        other.data["results"].pop()
    if mutation == "digest":
        other.data["config_digest"] = "f" * 64
    if mutation == "offered":
        other.data["results"][0]["offered_projection_digest"] = "f" * 64
    if mutation == "artifact":
        other.data["artifacts"] = [
            dict(
                role="primary_model",
                name="invented",
                version="1",
                digest=None,
                provenance="unknown",
            )
        ]
    if mutation == "slot":
        other.data["planned_slots"][0]["ordinal"] = 9
    result = comparison(saved, other, quality_only=True)
    assert result["status"] == "incompatible" and result["paired"] == [] and result["reasons"]


def test_missing_required_evidence_refuses_offline_and_quality_only(saved, tmp_path):
    api = module("asset_benchmark")
    # A saved summary alone is never a comparison input.
    path = tmp_path / "missing.json"
    path.write_text(json.dumps(saved.data))
    unavailable = api.load_asset_benchmark_report(path)
    assert unavailable.inputs is None
    result = comparison(unavailable, unavailable, quality_only=True)
    assert result["status"] == "incompatible"
    assert any(r["reason"] == "input_unresolved" for r in result["reasons"])


def test_unknown_machine_does_not_establish_performance_match(saved):
    result = comparison(saved, saved)
    assert result["status"] == "incompatible"
    assert any(r["field"].endswith("/gpu") for r in result["reasons"])


def test_primary_quality_does_not_inherit_floor_success(saved):
    metrics = module("asset_metrics")
    row = copy.deepcopy(saved.data["results"][0])
    row["attempts"][0]["outcome"] = "timeout"
    row["attempts"][0]["result"] = None
    assert metrics.primary_asset_result(row) is None
    assert row["result"]["final_candidates"]


def test_cli_writes_rejection_with_nonzero_exit_and_exact_options(saved, tmp_path):
    api = module("asset_benchmark_cli")
    # Use the real report and exact private resolver. No implicit root lookup.
    source = tmp_path / "a.json"
    source.write_text(json.dumps(saved.data))
    output = tmp_path / "comparison.json"
    markdown = tmp_path / "comparison.md"
    code = api.main(
        [
            "compare",
            str(source),
            str(source),
            "--output",
            str(output),
            "--markdown",
            str(markdown),
            "--quality-only",
        ]
    )
    assert code != 0 and json.loads(output.read_text())["status"] == "incompatible"
    assert "input_unresolved" in markdown.read_text()


@pytest.mark.parametrize(
    "field",
    [
        "seed",
        "repetitions",
        "warmups",
        "scope",
        "permissions",
        "budgets",
        "track",
        "fallback",
        "selection_scope",
        "requested_kinds",
    ],
)
def test_rejects_changed_protocol_or_permission_invariants(saved, field):
    from relaymind.canonical import canonical_digest

    other = copy.copy(saved)
    other.data = copy.deepcopy(saved.data)
    values = dict(
        seed=8,
        repetitions=3,
        warmups=2,
        scope=dict(user_id="other", workspace_id="fixture-workspace"),
        permissions=["read"],
        budgets=dict(saved.data["config"]["budgets"], primary_ms=100),
        track="service",
        fallback=True,
        selection_scope="runtime",
        requested_kinds=["skill"],
    )
    other.data["config"][field] = values[field]
    other.data["config_digest"] = canonical_digest(other.data["config"])
    result = comparison(saved, other, quality_only=True)
    assert result["status"] == "incompatible"
    assert any(r["field"] == "/config/" + field for r in result["reasons"])


@pytest.mark.parametrize(
    "field",
    ["python", "dependencies_digest", "runtime", "runtime_version", "runtime_config_digest"],
)
def test_quality_only_never_relaxes_shared_harness_runtime(saved, field):
    from relaymind.canonical import canonical_digest

    other = copy.copy(saved)
    other.data = copy.deepcopy(saved.data)
    other.data["environment"][field] = "e" * 64 if field.endswith("digest") else "changed"
    other.data["environment_digest"] = canonical_digest(other.data["environment"])
    result = comparison(saved, other, quality_only=True)
    assert result["status"] == "incompatible"
    assert any(r["field"] == "/environment/" + field for r in result["reasons"])


def test_source_policy_review_is_tied_to_exact_effective_and_proof_bytes(saved, tmp_path):
    evidence = module("asset_evidence")
    review = evidence.SourcePolicyReview(
        effective_config_digest="f" * 64,
        proof_sha256="e" * 64,
        custodian_id="reviewer",
        review_record_evidence_ids=("access_control",),
    )
    # Trusted application assertions are scoped to exact bytes; JSON reviewer
    # strings and an unrelated review cannot qualify another source policy.
    with pytest.raises(ValueError):
        evidence.verify_source_policy_review(
            saved.inputs.effective,
            saved.inputs.source_proof,
            saved.inputs.manifest,
            saved.inputs.evidence,
            review,
        )


def test_quality_only_allows_explicit_different_machine_facts(saved):
    from dataclasses import replace

    from relaymind.canonical import canonical_digest
    from relaymind.evaluation.asset_benchmark_io import json_bytes

    other = copy.copy(saved)
    other.data = copy.deepcopy(saved.data)
    documents = copy.deepcopy(saved.inputs.documents)
    manifest = copy.deepcopy(saved.inputs.manifest)
    machine = dict(
        other.data["environment"], ram_bytes=other.data["environment"]["ram_bytes"] + 1024
    )
    other.data["environment"] = machine
    other.data["environment_digest"] = canonical_digest(machine)
    documents["environment_lock"] = machine
    documents["benchmark_config"]["environment_lock_digest"] = canonical_digest(machine)
    for entry in manifest["documents"]:
        if entry["id"] in ("environment_lock", "benchmark_config"):
            entry["digest"] = canonical_digest(documents[entry["id"]])
            entry["bytes"] = len(json_bytes(documents[entry["id"]]))
    other.inputs = replace(saved.inputs, documents=documents, manifest=manifest)
    other.data["input_manifest"].update(
        digest=canonical_digest(manifest), bytes=len(json_bytes(manifest))
    )
    assert comparison(saved, other, quality_only=True)["status"] == "compatible"
    normal = comparison(saved, other)
    assert normal["status"] == "incompatible"
    assert any(reason["field"] == "/environment/ram_bytes" for reason in normal["reasons"])


def test_same_verified_source_alias_survives_private_directory_relocation(saved, tmp_path):
    api = module("asset_benchmark")
    config = fixture_config(tmp_path, repetitions=2, warmups=1)
    report = asyncio.run(api.run_asset_benchmark(config))
    path = tmp_path / "b.json"
    api.write_asset_benchmark_report(report, path)
    relocated = api.load_asset_benchmark_report(
        path, resolver=path.with_name(path.name + ".resolver.json")
    )
    assert saved.inputs.source_proof["bindings"] != relocated.inputs.source_proof["bindings"]
    assert comparison(saved, relocated, quality_only=True)["status"] == "compatible"


def rewrite_fixture_bundle(path, mutation):
    """Recommit authentic fixture bytes after one adversarial semantic mutation.

    This is test construction only: production readers must independently reject
    contradictory content even when all attacker-controlled hashes agree.
    """
    import hashlib

    from relaymind.canonical import canonical_digest
    from relaymind.evaluation.asset_benchmark_io import json_bytes
    from relaymind.evaluation.asset_evidence import OfflineResolver, resolve_reference

    root = path.parent
    report = json.loads(path.read_bytes())
    manifest_path = root / report["input_manifest"]["path"]
    manifest = json.loads(manifest_path.read_bytes())
    resolver_path = path.with_name(path.name + ".resolver.json")
    resolver = OfflineResolver(resolver_path)
    entries = dict(resolver.entries)
    documents = {
        r["id"]: json.loads(resolve_reference(r, root, resolver)) for r in manifest["documents"]
    }
    evidence = {
        r["id"]: resolve_reference(r, root, resolver, evidence=True) for r in manifest["evidence"]
    }
    overrides = {}
    mutation(documents, report, overrides)
    for inventory_name in ("runner_inventory", "protocol_inventory"):
        for item in documents[inventory_name]["files"]:
            if item["path"] not in overrides:
                continue
            id = "file:" + hashlib.sha256(item["path"].encode()).hexdigest()
            entries.pop((id, "original", item["sha256"]))
            payload = overrides[item["path"]]
            item.update(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
            relative = "rewritten/files/" + item["sha256"]
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            entries[(id, "original", item["sha256"])] = dict(
                id=id, hash_kind="original", hash=item["sha256"], bytes=len(payload), path=relative
            )
    effective = documents["effective_config"]
    for field, kind in [
        ("runner_digest", "runner_inventory"),
        ("protocol_digest", "protocol_inventory"),
    ]:
        digest = canonical_digest(documents[kind])
        effective["generator"][field] = digest
        report["implementation"][field] = digest
    effective_digest = canonical_digest(effective)
    report["config"]["production_config_digest"] = effective_digest
    report["config_digest"] = canonical_digest(report["config"])
    report["environment_digest"] = canonical_digest(report["environment"])
    documents["benchmark_config"]["production_config_digest"] = effective_digest
    documents["benchmark_config"]["environment_lock_digest"] = canonical_digest(
        documents["environment_lock"]
    )
    for id, payload in list(evidence.items()):
        try:
            value = json.loads(payload)
        except ValueError:
            continue
        if "effective_config_digest" in value:
            value["effective_config_digest"] = effective_digest
            if "generator" in value:
                value["generator"] = effective["generator"]
            evidence[id] = json_bytes(value)
    committed = {reference["id"] for reference in manifest["documents"]}
    for id, value in documents.items():
        if id not in committed:
            manifest["documents"].append(
                dict(
                    id=id,
                    kind=id.split(":", 1)[0],
                    path="placeholder.json",
                    digest=canonical_digest(value),
                    bytes=0,
                    format="json",
                )
            )
    manifest["documents"].sort(key=lambda reference: reference["id"])
    for reference in manifest["documents"]:
        old_key = (reference["id"], "canonical", reference["digest"])
        payload = json_bytes(documents[reference["id"]])
        digest = canonical_digest(documents[reference["id"]])
        relative = "rewritten/documents/" + reference["id"].replace(":", "-") + ".json"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        reference.update(digest=digest, bytes=len(payload))
        if reference["path"] is None:
            entries.pop(old_key, None)
            entries[(reference["id"], "canonical", digest)] = dict(
                id=reference["id"],
                hash_kind="canonical",
                hash=digest,
                bytes=len(payload),
                path=relative,
            )
        else:
            reference["path"] = relative
    for reference in manifest["evidence"]:
        payload = evidence[reference["id"]]
        old_key = (reference["id"], "original", reference["sha256"])
        digest = hashlib.sha256(payload).hexdigest()
        relative = "rewritten/evidence/" + hashlib.sha256(reference["id"].encode()).hexdigest()
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        entries.pop(old_key, None)
        reference.update(sha256=digest, bytes=len(payload))
        entries[(reference["id"], "original", digest)] = dict(
            id=reference["id"], hash_kind="original", hash=digest, bytes=len(payload), path=relative
        )
    resolver_path.write_bytes(
        json_bytes(dict(schema_version=1, entries=[entries[k] for k in sorted(entries)]))
    )
    manifest_path.write_bytes(json_bytes(manifest))
    report["input_manifest"].update(
        digest=canonical_digest(manifest), bytes=manifest_path.stat().st_size
    )
    path.write_bytes(json_bytes(report))
    return module("asset_benchmark").load_asset_benchmark_report(path, resolver=resolver_path)


@pytest.fixture
def disk_bundle(saved_path, tmp_path):
    import shutil

    root = tmp_path / "bundle"
    shutil.copytree(saved_path.parent, root)
    return root / saved_path.name


@pytest.mark.parametrize("quality_only", [False, True])
@pytest.mark.parametrize(
    "mutation", ["missing_python", "missing_schema", "unknown_python", "unknown_protocol"]
)
def test_exact_compiled_inventory_rejects_recommitted_unknown_generator(
    disk_bundle, mutation, quality_only
):
    def change(documents, report, overrides):
        inventory = documents[
            "protocol_inventory"
            if mutation in ("missing_schema", "unknown_protocol")
            else "runner_inventory"
        ]["files"]
        target = (
            "src/relaymind/evaluation/schemas/asset_benchmark_config.v1.json"
            if mutation == "missing_schema"
            else "docs/SPECIFICATION.md"
            if mutation == "unknown_protocol"
            else "src/relaymind/evaluation/asset_comparison.py"
        )
        if mutation.startswith("missing"):
            inventory[:] = [item for item in inventory if item["path"] != target]
        else:
            from pathlib import Path

            overrides[target] = (
                Path(__file__).parents[1] / target
            ).read_bytes() + b"\n# Unsupported fixture generator bytes\n"

    report = rewrite_fixture_bundle(disk_bundle, change)
    result = comparison(report, report, quality_only=quality_only)
    assert result["status"] == "incompatible" and result["paired"] == []
    assert any(reason["reason"] == "evidence_unverified" for reason in result["reasons"])


@pytest.mark.parametrize("identity", ["runner_digest", "protocol_digest"])
def test_cached_report_rechecks_compiled_generator_binding(saved, identity):
    report = copy.copy(saved)
    report.data = copy.deepcopy(saved.data)
    report.data["implementation"][identity] = "e" * 64
    assert comparison(report, report, quality_only=True)["status"] == "incompatible"


def mutate_admitted_identity(report, mutation):
    for row in report["results"]:
        raw = row["attempts"][0]["result"]
        if mutation == "content":
            raw["candidates"][0]["content_hash"] = "f" * 64
            for field in ("base_candidates", "final_candidates"):
                row["result"][field][0]["content_hash"] = "f" * 64
        elif mutation == "selector":
            raw["selector"]["name"] = "unconfigured-selector"
        else:
            raw["catalog_revision"] += 1
        row["result"]["attempts"] = copy.deepcopy(row["attempts"])


@pytest.mark.parametrize("mutation", ["content", "selector", "stale_revision"])
@pytest.mark.parametrize("entry", ["library", "cli"])
def test_admission_failure_emits_incompatible_object_and_cli_artifacts(
    disk_bundle, mutation, entry
):
    from relaymind.canonical import canonical_digest

    report = json.loads(disk_bundle.read_bytes())
    mutate_admitted_identity(report, mutation)
    disk_bundle.write_text(json.dumps(report))
    if entry == "library":
        loaded = module("asset_benchmark").load_asset_benchmark_report(
            disk_bundle, resolver=disk_bundle.with_name(disk_bundle.name + ".resolver.json")
        )
        result = comparison(loaded, loaded, quality_only=True)
    else:
        output = disk_bundle.parent / "rejected.json"
        markdown = disk_bundle.parent / "rejected.md"
        code = module("asset_benchmark_cli").main(
            [
                "compare",
                str(disk_bundle),
                str(disk_bundle),
                "--resolver",
                str(disk_bundle.with_name(disk_bundle.name + ".resolver.json")),
                "--quality-only",
                "--output",
                str(output),
                "--markdown",
                str(markdown),
            ]
        )
        assert code == 2 and output.exists() and markdown.exists()
        assert "incompatible" in markdown.read_text()
        result = json.loads(output.read_bytes())
    assert result["status"] == "incompatible" and result["paired"] == []
    assert result["reports"]["a"] == canonical_digest(report)
    assert all(reason["reason"] == "evidence_unverified" for reason in result["reasons"])


@pytest.mark.parametrize("quality_only", [False, True])
@pytest.mark.parametrize(
    "mutation",
    [
        "missing_runtime",
        "unsupported_runtime",
        "dangling_effective",
        "unsupported_effective",
        "unused_config",
    ],
)
def test_runtime_and_effective_config_closure_is_required_offline(
    disk_bundle, mutation, quality_only
):
    from relaymind.canonical import canonical_digest

    def change(documents, report, overrides):
        configuration = dict(schema_version=1, threads=2)
        digest = canonical_digest(configuration)
        id = "referenced_config:" + digest
        if mutation in ("unsupported_runtime", "unsupported_effective", "unused_config"):
            documents[id] = configuration
        if mutation in ("missing_runtime", "unsupported_runtime"):
            documents["environment_lock"].update(
                runtime="fabricated-runtime",
                runtime_version="1",
                runtime_config_digest="f" * 64 if mutation == "missing_runtime" else digest,
            )
            report["environment"] = copy.deepcopy(documents["environment_lock"])
        if mutation in ("dangling_effective", "unsupported_effective"):
            documents["effective_config"]["references"] = [
                dict(
                    role="index_config",
                    document_id="referenced_config:" + "f" * 64
                    if mutation == "dangling_effective"
                    else id,
                )
            ]

    report = rewrite_fixture_bundle(disk_bundle, change)
    assert report.inputs is None and report.input_error in (
        "input_unresolved",
        "evidence_unverified",
    )
    output = disk_bundle.parent / "config-rejected.json"
    markdown = disk_bundle.parent / "config-rejected.md"
    argv = [
        "compare",
        str(disk_bundle),
        str(disk_bundle),
        "--resolver",
        str(disk_bundle.with_name(disk_bundle.name + ".resolver.json")),
        "--output",
        str(output),
        "--markdown",
        str(markdown),
    ]
    if quality_only:
        argv.append("--quality-only")
    assert module("asset_benchmark_cli").main(argv) == 2
    result = json.loads(output.read_bytes())
    assert result["status"] == "incompatible" and result["paired"] == [] and markdown.exists()
    assert any(
        reason["reason"] in ("input_unresolved", "evidence_unverified")
        for reason in result["reasons"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "admission_revision",
        "primary_floor",
        "host_revision",
        "reported_ms",
        "index_revision",
        "fallback_reason",
        "accepted_reason",
    ],
)
def test_rejects_coordinator_contradictions_even_when_grades_match(saved, mutation):
    report = copy.copy(saved)
    report.data = copy.deepcopy(saved.data)
    for row in report.data["results"]:
        result = row["result"]
        attempt = row["attempts"][0]
        if mutation == "admission_revision":
            result["admission"]["rechecked_catalog_revision"] = 999
        if mutation == "primary_floor":
            attempt["mechanism"] = "gateway_lexical_fallback"
        if mutation == "host_revision":
            result["host_snapshot_revision"] = 999
        if mutation == "reported_ms":
            attempt["reported_ms"] += 1
        if mutation == "index_revision":
            attempt["index_revision"] = "invented-index"
        if mutation == "fallback_reason":
            result["fallback_reason"] = "selector_unavailable"
        if mutation == "accepted_reason":
            attempt["reason"] = "selector_unavailable"
        result["attempts"] = copy.deepcopy(row["attempts"])
    if mutation == "host_revision":
        # RecommendationResultV1 already requires null for the current
        # discovery service. Malformed product input is rejected before a
        # structurally valid comparison object can be formed.
        with pytest.raises(ValueError, match="invalid_benchmark_report"):
            comparison(report, report, quality_only=True)
        return
    result = comparison(report, report, quality_only=True)
    assert result["status"] == "incompatible" and result["paired"] == []


@pytest.mark.parametrize("mode", ["catalog", "identity", "timeout"])
def test_actual_failed_primary_floor_and_rejected_raw_result_remain_comparable(
    tmp_path, http_example, mode
):
    api = module("asset_benchmark")
    url, captured = http_example
    captured.mode = mode
    report = asyncio.run(
        api.run_asset_benchmark(
            fixture_config(
                tmp_path, primary="http", url=url, fallback=True, repetitions=1, warmups=0
            )
        )
    )
    assert report.data["results"][0]["status"] == "ready"
    assert len(report.data["results"][0]["attempts"]) == 2
    result = comparison(report, report, quality_only=True)
    assert result["status"] == "compatible", result["reasons"]


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_reported",
        "mismatched_claim",
        "missing_claim_observation",
        "duplicate_observed",
        "unbacked_observed",
    ],
)
def test_unbacked_attempt_costs_do_not_become_complete_totals(tmp_path, http_example, mutation):
    api = module("asset_benchmark")
    url, _ = http_example
    observed = mutation in ("duplicate_observed", "unbacked_observed")
    config = fixture_config(
        tmp_path,
        primary="lexical" if observed else "http",
        url=None if observed else url,
        repetitions=2,
        warmups=1,
    )
    report = asyncio.run(api.run_asset_benchmark(config))
    report.data = copy.deepcopy(report.data)
    value = dict(
        input_tokens=4,
        output_tokens=2,
        cached_tokens=0,
        reasoning_tokens=0,
        calls=1,
        cpu_ms=None,
        peak_bytes=None,
    )
    claimed = dict(provenance="reported", value=value)
    for row in report.data["results"]:
        attempt = row["attempts"][0]
        if mutation == "duplicate_reported":
            attempt["cost_observations"] = [copy.deepcopy(claimed), copy.deepcopy(claimed)]
        if mutation == "mismatched_claim":
            attempt["result"]["usage_claims"] = dict(
                provenance="reported", value=dict(value, calls=9)
            )
            attempt["cost_observations"] = [copy.deepcopy(claimed)]
        if mutation == "missing_claim_observation":
            attempt["result"]["usage_claims"] = copy.deepcopy(claimed)
        if mutation == "duplicate_observed":
            attempt["cost_observations"] *= 2
        if mutation == "unbacked_observed":
            attempt["cost_observations"][0]["value"]["calls"] = 1
        row["result"]["attempts"] = copy.deepcopy(row["attempts"])
    # Recommit the self-consistent totals the vulnerable flattened counter
    # would compute. They are fixture expectations, not measured observations.
    observations = report.data["results"][0]["attempts"][0]["cost_observations"]
    for summary in report.data["summaries"]:
        planned = summary["planned"]
        for coverage in summary["costs"]:
            values = [
                o["value"][coverage["metric"]]
                for o in observations
                if o["provenance"] == coverage["provenance"]
            ]
            known = bool(values) and all(v is not None for v in values)
            components = [v for v in values if v is not None]
            subtotal = (
                (
                    max(components)
                    if coverage["metric"] == "peak_bytes"
                    else sum(components) * planned
                )
                if components
                else None
            )
            coverage.update(
                known=planned if known else 0,
                unknown=0 if known else planned,
                total=subtotal if known else None,
                known_subtotal=subtotal,
            )
    result = comparison(report, report, quality_only=True)
    assert result["status"] == "incompatible" and result["paired"] == []


@pytest.mark.parametrize("container", ["attempt", "whole_slot"])
def test_duplicate_cost_provenance_cannot_cover_another_owned_component(container):
    value = dict(
        input_tokens=7,
        output_tokens=0,
        cached_tokens=0,
        reasoning_tokens=0,
        calls=1,
        cpu_ms=None,
        peak_bytes=None,
    )
    observation = dict(provenance="observed", value=value)
    row = dict(
        observed_costs=[observation, observation] if container == "whole_slot" else [],
        attempts=[dict(cost_observations=[observation, observation]), dict(cost_observations=[])],
        embeddings=[],
        resources=dict(cpu_ms=None, peak_bytes=None, coverage="unknown"),
    )
    with pytest.raises(ValueError):
        module("asset_costs").summarize_asset_costs([row])


def test_legitimate_reported_claims_remain_separate_from_observed_costs(tmp_path, http_example):
    api = module("asset_benchmark")
    url, captured = http_example
    captured.mode = "reported"
    report = asyncio.run(
        api.run_asset_benchmark(
            fixture_config(tmp_path, primary="http", url=url, repetitions=2, warmups=0)
        )
    )
    assert comparison(report, report, quality_only=True)["status"] == "compatible"
    summary = next(
        s
        for s in report.data["summaries"]
        if s["dimensions"]["lane"] == "all"
        and s["dimensions"]["kind"] == "all"
        and s["dimensions"]["availability"] == "all"
        and s["dimensions"]["stale"] is None
        and s["dimensions"]["retrieval_mode"] == "service"
    )
    costs = {c["provenance"]: c for c in summary["costs"] if c["metric"] == "calls"}
    assert costs["reported"]["known"] == 2 and costs["reported"]["total"] == 0
    assert costs["observed"]["known"] == 0 and costs["observed"]["total"] is None
    assert report.data["results"][0]["attempts"][0]["cost_observations"] == [
        report.data["results"][0]["attempts"][0]["result"]["usage_claims"]
    ]


def test_cost_coverage_is_checked_per_owned_attempt():
    value = dict(
        input_tokens=7,
        output_tokens=0,
        cached_tokens=0,
        reasoning_tokens=0,
        calls=1,
        cpu_ms=None,
        peak_bytes=None,
    )
    observation = dict(provenance="observed", value=value)
    row = dict(
        observed_costs=[],
        attempts=[dict(cost_observations=[observation]), dict(cost_observations=[])],
        embeddings=[],
        resources=dict(cpu_ms=None, peak_bytes=None, coverage="unknown"),
    )
    incomplete = next(
        c
        for c in module("asset_costs").summarize_asset_costs([row])
        if c["metric"] == "input_tokens" and c["provenance"] == "observed"
    )
    assert (
        incomplete["known"] == 0
        and incomplete["total"] is None
        and incomplete["known_subtotal"] == 7
    )
    row["attempts"][1]["cost_observations"] = [
        dict(provenance="observed", value=dict(value, input_tokens=3))
    ]
    complete = next(
        c
        for c in module("asset_costs").summarize_asset_costs([row])
        if c["metric"] == "input_tokens" and c["provenance"] == "observed"
    )
    assert (
        complete["known"] == 1
        and complete["unknown"] == 0
        and complete["total"] == 10
        and complete["known_subtotal"] == 10
    )


def specified_dimension_order(dimensions):
    return (
        dimensions["lane"].encode(),
        dimensions["kind"].encode(),
        dimensions["availability"].encode(),
        0 if dimensions["stale"] is None else 2 if dimensions["stale"] else 1,
        dimensions["selection_scope"].encode(),
        dimensions["catalog_size"],
        dimensions["retrieval_mode"].encode(),
        dimensions["subset"].encode(),
    )


@pytest.mark.parametrize("export", ["summaries", "paired"])
def test_exports_follow_reviewed_659b3052_dimension_tuple_order(saved, export):
    rows = (
        saved.data["summaries"]
        if export == "summaries"
        else comparison(saved, saved, quality_only=True)["paired"]
    )
    assert rows == sorted(rows, key=lambda row: specified_dimension_order(row["dimensions"]))


def test_named_id_and_permission_sets_are_sorted_before_hashing(saved):
    io = module("asset_benchmark_io")
    report = copy.deepcopy(saved.data)
    report["qualification_evidence_ids"] = ["z", "a"]
    with pytest.raises(ValueError):
        io.validate_benchmark_document("report", report)
    effective = copy.deepcopy(saved.inputs.effective)
    effective["permissions"] = ["z", "a"]
    with pytest.raises(ValueError):
        io.validate_benchmark_document("report", effective, definition="EffectiveConfigV1")
    proof = copy.deepcopy(saved.inputs.source_proof)
    proof["reviewer_ids"] = ["z", "a"]
    with pytest.raises(ValueError):
        io.validate_benchmark_document("report", proof, definition="SourceProofV1")


def test_unsorted_but_structurally_valid_summary_is_an_incompatible_report(saved):
    report = copy.copy(saved)
    report.data = copy.deepcopy(saved.data)
    report.data["summaries"].reverse()
    result = comparison(report, report, quality_only=True)
    assert result["status"] == "incompatible" and result["paired"] == []


def test_language_pair_keeps_skill_and_tool_diagnostics_before_aggregation(saved):
    # RM40/AC31: even an EN→EN-only fixture retains its populated kind cohort;
    # CN→EN and other actual lanes use the same frozen membership function.
    summaries = saved.data["summaries"]
    for mode in ("primary", "service"):
        cohort = [
            s
            for s in summaries
            if s["dimensions"]["lane"] == "en_to_en"
            and s["dimensions"]["kind"] == "skill"
            and s["dimensions"]["availability"] == "all"
            and s["dimensions"]["stale"] is None
            and s["dimensions"]["retrieval_mode"] == mode
        ]
        assert len(cohort) == 1
        assert cohort[0]["positive"] == 1 and cohort[0]["planned"] == 2
        assert cohort[0]["hit3"]["denominator"] == 1 and cohort[0]["gate"] == "not_applicable"
