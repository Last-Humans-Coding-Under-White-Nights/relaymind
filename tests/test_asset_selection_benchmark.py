"""RM35/RM40, AC26/AC31: frozen benchmark protocol, never empirical evidence."""

import asyncio
import copy
import importlib
import math
from pathlib import Path

import pytest


def module(name):
    try:
        return importlib.import_module("relaymind.evaluation." + name)
    except ModuleNotFoundError:
        pytest.fail("RM40/AC31 benchmark module is not implemented: " + name)


def test_schedule_is_canonical_and_conserves_every_phase():
    api = module("asset_schedule")
    slots = api.plan_asset_slots(["b", "a"], seed=7, repetitions=2, warmups=1)
    assert [s["ordinal"] for s in slots] == list(range(1, 7))
    assert [s["phase"] for s in slots] == ["warmup"] * 2 + ["measured"] * 4
    assert api.plan_asset_slots(["a", "b"], seed=7, repetitions=2, warmups=1) == slots
    from relaymind.canonical import canonical_digest

    for offset, phase, rep in [(0, "warmup", 0), (2, "measured", 0), (4, "measured", 1)]:
        expected = sorted(
            ["a", "b"],
            key=lambda c: (
                canonical_digest(dict(seed=7, phase=phase, repetition=rep, case_id=c)),
                c,
            ),
        )
        assert [s["case_id"] for s in slots[offset : offset + 2]] == expected
    with pytest.raises(ValueError):
        api.plan_asset_slots(["a", "a"], seed=7, repetitions=2, warmups=1)
    with pytest.raises(ValueError):
        api.plan_asset_slots([str(i) for i in range(1001)], seed=0, repetitions=100, warmups=0)


def test_grade_false_confidence_and_failure_denominator():
    metrics = module("asset_metrics")
    result = dict(final_decision="confident", final_candidates=[dict(asset_id="a")])
    for label in ["negative", "ambiguous"]:
        grade = metrics.grade_asset_result(
            dict(**{"class": label}, acceptable_asset_ids=["a"]), result
        )
        assert grade["false_confident"] and not grade["confident_correct"]
    grade = metrics.grade_asset_result(
        dict(**{"class": "positive"}, acceptable_asset_ids=["a"]), None
    )
    assert grade["class"] == "positive" and not any(v for k, v in grade.items() if k != "class")
    rate = metrics.asset_rate(1, 2)
    assert rate["value"] == 0.5
    assert math.isclose(rate["wilson95"]["lower"], 0.09453120573423074)
    assert metrics.asset_rate(0, 0) == dict(numerator=0, denominator=0, value=None, wilson95=None)
    assert metrics.asset_rate(1, 2, independent=False)["wilson95"] is None


def test_environment_preserves_nullable_gpu_facts_and_unknown_revision(monkeypatch, tmp_path):
    """RM40/AC31: an unknown fact cannot become a fabricated match."""
    evidence = module("asset_evidence")
    original_manifest = evidence.machine_manifest
    facts = original_manifest() | dict(
        gpu=[
            dict(model="Known", cores="24"),
            dict(model=None, cores=None),
            dict(model="Known", cores="24"),
            dict(model="Other", cores=16),
        ]
    )
    monkeypatch.setattr(evidence, "machine_manifest", lambda: facts)
    environment = evidence.collect_benchmark_environment()
    assert environment["gpu"] == [
        dict(model=None, device=None, cores=None),
        dict(model="Known", device=None, cores="24"),
        dict(model="Other", device=None, cores="16"),
    ]
    assert evidence.validate_definition("Environment", environment) == environment
    unsorted = copy.deepcopy(environment)
    unsorted["gpu"].reverse()
    with pytest.raises(ValueError, match="invalid_benchmark_report"):
        evidence.validate_definition("Environment", unsorted)
    monkeypatch.setattr(evidence, "machine_manifest", lambda: facts | dict(gpu=[]))
    assert evidence.collect_benchmark_environment()["gpu"] == []
    monkeypatch.setattr(evidence, "machine_manifest", lambda: facts | dict(gpu=None))
    assert evidence.collect_benchmark_environment()["gpu"] is None
    monkeypatch.setattr(evidence, "machine_manifest", original_manifest)

    benchmark = module("asset_benchmark")
    monkeypatch.setattr(
        benchmark, "application_revision", lambda _root: dict(head=None, dirty=None)
    )
    report = asyncio.run(
        benchmark.run_asset_benchmark(fixture_config(tmp_path, repetitions=1, warmups=0))
    )
    assert report.data["implementation"]["dirty"] is None
    benchmark.validate_benchmark_document("report", report.data)


def test_cost_unknown_keeps_owned_known_subtotal():
    costs = module("asset_costs")
    rows = [
        dict(
            observed_costs=[
                dict(
                    provenance="observed",
                    value=dict(
                        input_tokens=7,
                        output_tokens=None,
                        cached_tokens=None,
                        reasoning_tokens=None,
                        calls=1,
                        cpu_ms=None,
                        peak_bytes=None,
                    ),
                )
            ],
            attempts=[],
            embeddings=[],
            resources=dict(cpu_ms=None, peak_bytes=None, coverage="unknown"),
        ),
        dict(
            observed_costs=[],
            attempts=[],
            embeddings=[],
            resources=dict(cpu_ms=None, peak_bytes=None, coverage="unknown"),
        ),
    ]
    summary = costs.summarize_asset_costs(rows)
    item = next(
        c for c in summary if c["metric"] == "input_tokens" and c["provenance"] == "observed"
    )
    assert item == dict(
        metric="input_tokens",
        provenance="observed",
        known=1,
        unknown=1,
        total=None,
        known_subtotal=7,
    )


def test_config_schema_is_closed_strict_and_bounded():
    api = module("asset_benchmark_io")
    config = dict(
        schema_version=1,
        lock="lock.json",
        lock_digest="a" * 64,
        production_config="production.toml",
        production_config_digest="b" * 64,
        split="development",
        track="primary_only",
        seed=0,
        repetitions=1,
        warmups=0,
        environment_lock="environment.json",
        environment_lock_digest="c" * 64,
    )
    api.validate_benchmark_document("config", config)
    for patch in [
        dict(extra=1),
        dict(schema_version=True),
        dict(seed=2**32),
        dict(repetitions=0),
        dict(lock="../lock.json"),
    ]:
        with pytest.raises(ValueError):
            api.validate_benchmark_document("config", config | patch)


def test_resolver_uses_complete_identity_triple(tmp_path):
    api = module("asset_evidence")
    import json

    from relaymind.canonical import canonical_digest

    entries = []
    for i in [1, 2]:
        value = dict(n=i)
        payload = json.dumps(value).encode()
        path = tmp_path / f"{i}.json"
        path.write_bytes(payload)
        entries.append(
            dict(
                id="effective_config",
                hash_kind="canonical",
                hash=canonical_digest(value),
                bytes=len(payload),
                path=path.name,
            )
        )
    entries.sort(key=lambda e: (e["id"], e["hash_kind"], e["hash"]))
    resolver_path = tmp_path / "resolver.json"
    resolver_path.write_text(json.dumps(dict(schema_version=1, entries=entries)))
    resolver = api.OfflineResolver(resolver_path)
    for entry in entries:
        assert (
            resolver.read(entry["id"], "canonical", entry["hash"], entry["bytes"])
            == (tmp_path / entry["path"]).read_bytes()
        )
    with pytest.raises(ValueError):
        resolver.read("effective_config", "canonical", "f" * 64, 2)
    resolver_path.write_text(json.dumps(dict(schema_version=1, entries=[entries[0], entries[0]])))
    with pytest.raises(ValueError):
        api.OfflineResolver(resolver_path)


def fixture_config(
    tmp_path,
    *,
    primary="lexical",
    url=None,
    fallback=False,
    source="fixture",
    repetitions=2,
    warmups=1,
    total_ms=60_000,
    primary_ms=60_000,
):
    """Only synthetic locked metadata is injected into a disposable service.

    The recommendation budget is a deterministic test value, not the
    production 500/150 ms pair: these tests measure protocol and evidence, so
    runner scheduling must not be able to expire a primary attempt. Tests that
    deliberately exercise a timeout pass their own small budget.
    """
    from relaymind.canonical import canonical_digest
    from relaymind.launcher import load_config
    from test_asset_corpus import read, stage_fixture

    api = module("asset_benchmark")
    evidence = module("asset_evidence")
    root = tmp_path / "inputs"
    stage_fixture(root)
    production_path = root / "production.toml"
    production_path.write_text(
        '[external_model]\nbase_url="http://127.0.0.1:1/v1"\nmodel="unused-fixture"\ncontext_limit=1024\n[policy]\npermissions=[]\n[recommendations]\nenabled=true\nuser_id="fixture-user"\nworkspace_id="fixture-workspace"\nprimary="'
        + primary
        + '"\nfallback='
        + str(fallback).lower()
        + f"\ntotal_ms={total_ms}\nprimary_ms={primary_ms}\n"
        + (f'name="example_lexical"\nversion="1"\nurl="{url}"\n' if url else "")
        + f'[[assets.sources]]\nsource_id="{source}"\nkind="local_directory"\npath="originals"\n'
    )
    release = module("asset_corpus").load_asset_release(
        root / "lock.json", evidence_root=root, related_roots={}
    )
    inventories = api.benchmark_inventories()
    effective = evidence.effective_asset_config(
        load_config(production_path),
        release,
        runner_digest=canonical_digest(inventories["runner_inventory"]),
        protocol_digest=canonical_digest(inventories["protocol_inventory"]),
    )
    import json

    environment = evidence.collect_benchmark_environment()
    (root / "environment.json").write_text(json.dumps(environment))
    config = dict(
        schema_version=1,
        lock="lock.json",
        lock_digest=read(root, "lock")["digest"],
        production_config="production.toml",
        production_config_digest=canonical_digest(effective),
        split="development",
        track="service" if fallback else "primary_only",
        seed=7,
        repetitions=repetitions,
        warmups=warmups,
        environment_lock="environment.json",
        environment_lock_digest=canonical_digest(environment),
    )
    (root / "bench.toml").write_text("\n".join(f"{k}={json.dumps(v)}" for k, v in config.items()))
    return api.load_benchmark_config(root / "bench.toml")


def test_service_runner_retains_warmups_and_independent_denominator(tmp_path):
    api = module("asset_benchmark")
    config = fixture_config(tmp_path)
    report = asyncio.run(api.run_asset_benchmark(config))
    data = report.to_json_object()
    assert data["qualification"] == "fixture"
    assert data["status"] == "completed" and len(data["results"]) == 3
    assert all(
        r["status"] == "ready" and r["request_digest"] and r["offered_projection_digest"]
        for r in data["results"]
    )
    overall = next(
        s
        for s in data["summaries"]
        if s["dimensions"]["lane"] == "all"
        and s["dimensions"]["kind"] == "all"
        and s["dimensions"]["availability"] == "all"
        and s["dimensions"]["stale"] is None
        and s["dimensions"]["retrieval_mode"] == "service"
    )
    assert overall["planned"] == 2 and overall["positive"] == 1
    assert overall["ready"] == 2 and "completed" not in overall
    assert overall["planned"] == sum(
        overall[key] for key in ("ready", "failed", "cancelled", "not_run")
    )
    measured = next(row for row in data["results"] if row["slot"]["phase"] == "measured")
    rows = [next(row for row in data["results"] if row["slot"]["phase"] == "warmup")]
    for repetition, status in enumerate(("ready", "failed", "cancelled", "not_run")):
        row = copy.deepcopy(measured)
        row["slot"]["repetition"] = repetition
        row["status"] = status
        if status != "ready":
            row["result"] = None
        rows.append(row)
    mixed = module("asset_metrics").summarize_asset_rows(
        config.release,
        data["config"] | dict(repetitions=4),
        rows,
        qualification="fixture",
        effective=config.effective,
    )
    mixed_overall = next(s for s in mixed if s["dimensions"] == overall["dimensions"])
    assert [
        mixed_overall[key] for key in ("planned", "ready", "failed", "cancelled", "not_run")
    ] == [4, 1, 1, 1, 1]
    assert overall["hit3"]["denominator"] == 1 and overall["hit3"]["numerator"] == 1
    assert data["artifacts"] == []
    path = tmp_path / "report.json"
    api.write_asset_benchmark_report(report, path)
    assert path.exists() and path.with_name(path.name + ".inputs.json").exists()
    saved = api.load_asset_benchmark_report(
        path, resolver=path.with_name(path.name + ".resolver.json")
    )
    assert saved.to_json_object() == report.to_json_object()
    assert saved.inputs is not None, saved.input_error


def test_empty_catalog_revision_zero_has_no_attempt_and_no_inference(tmp_path):
    api = module("asset_benchmark")
    report = asyncio.run(
        api.run_asset_benchmark(
            fixture_config(tmp_path, source="excluded", repetitions=1, warmups=0)
        )
    )
    row = report.to_json_object()["results"][0]
    assert row["status"] == "ready" and row["catalog_revision"] == 0
    assert row["result"]["decision"] == "empty_catalog" and row["attempts"] == []
    assert row["observed_costs"][0]["value"]["calls"] == 0
    assert all(s["dimensions"]["catalog_size"] == 0 for s in report.data["summaries"])


def test_interruption_conserves_all_remaining_slots(tmp_path):
    api = module("asset_benchmark")
    calls = 0

    def interrupted():
        nonlocal calls
        calls += 1
        return calls > 1

    report = asyncio.run(api.run_asset_benchmark(fixture_config(tmp_path), interrupted=interrupted))
    data = report.to_json_object()
    assert data["status"] == "interrupted"
    assert [r["status"] for r in data["results"]] == ["ready", "not_run", "not_run"]
    assert all(
        r["request_digest"] is None and r["attempts"] == [] and r["observed_ms"] is None
        for r in data["results"][1:]
    )


@pytest.fixture
def http_example():
    import importlib.util
    import threading

    path = Path(__file__).resolve().parents[1] / "scripts/example_asset_selector.py"
    if not path.exists():
        pytest.fail("RM35/AC26 HTTP v2 replacement example is not implemented")
    spec = importlib.util.spec_from_file_location("asset_selector_example", path)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)

    class Captures(list):
        mode = "normal"
        # Explicit modeled latency, never the host schedule (RM40/AC31).
        delay = 0

    captured = Captures()
    original = example.selection

    async def record(value):
        captured.append(value)
        if captured.delay:
            await asyncio.sleep(captured.delay)
        if captured.mode == "timeout":
            await asyncio.sleep(0.3)
        result = await original(value)
        if captured.mode == "reported":
            result["usage_claims"] = {
                "provenance": "reported",
                "value": dict(
                    input_tokens=0,
                    output_tokens=0,
                    cached_tokens=0,
                    reasoning_tokens=0,
                    calls=0,
                    cpu_ms=None,
                    peak_bytes=None,
                ),
            }
        if captured.mode == "version":
            result["schema_version"] = 1
        if captured.mode == "identity":
            result["selector"]["name"] = "wrong"
        if captured.mode == "catalog":
            result["catalog_revision"] += 1
        if captured.mode == "content":
            result["candidates"][0]["content_hash"] = "f" * 64
        if captured.mode == "no_match":
            result.update(outcome="no_match", decision="no_match", candidates=[])
        if captured.mode == "confident":
            result.update(outcome="ranked", decision="confident")
        return result

    example.selection = record
    handler = example.Handler

    class Server(example.LoopbackServer):
        def handle_error(self, request, client_address):
            import sys

            if not isinstance(sys.exception(), (BrokenPipeError, ConnectionResetError)):
                super().handle_error(request, client_address)

    server = Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/select", captured
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_http_uses_identical_full_offer_and_does_not_receive_labels(tmp_path, http_example):
    api = module("asset_benchmark")
    url, captured = http_example
    builtin_config = fixture_config(tmp_path)
    builtin = asyncio.run(api.run_asset_benchmark(builtin_config))
    production = builtin_config.path.parent / "production.toml"
    text = production.read_text().replace(
        'primary="lexical"', f'primary="http"\nname="example_lexical"\nversion="1"\nurl="{url}"'
    )
    production.write_text(text)
    from relaymind.canonical import canonical_digest
    from relaymind.launcher import load_config

    effective = module("asset_evidence").effective_asset_config(
        load_config(production),
        builtin_config.release,
        runner_digest=canonical_digest(builtin_config.inventories["runner_inventory"]),
        protocol_digest=canonical_digest(builtin_config.inventories["protocol_inventory"]),
    )
    config_text = builtin_config.path.read_text().replace(
        builtin_config.data["production_config_digest"], canonical_digest(effective)
    )
    builtin_config.path.write_text(config_text)
    http_config = api.load_benchmark_config(builtin_config.path)
    remote = asyncio.run(api.run_asset_benchmark(http_config))
    assert len(captured) == 3
    assert len(captured[0]["catalog"]) == len(builtin_config.release.catalog["assets"])
    assert not any(
        key in str(captured[0])
        for key in ["acceptable_asset_ids", "adjudications", "original_evidence_id", "private_body"]
    )
    for a, b in zip(builtin.data["results"], remote.data["results"], strict=True):
        assert a["request_digest"] == b["request_digest"]
        assert a["offered_projection_digest"] == b["offered_projection_digest"]
        assert b["status"] == "ready", b
    for name, report in [("a", builtin), ("b", remote)]:
        api.write_asset_benchmark_report(report, tmp_path / (name + ".json"))
    # One explicit resolver may contain same document ID with different hashes.
    import json

    entries = {}
    for side in ["a", "b"]:
        for e in json.loads((tmp_path / (side + ".json.resolver.json")).read_text())["entries"]:
            entries[(e["id"], e["hash_kind"], e["hash"])] = e
    resolver = tmp_path / "both.resolver.json"
    resolver.write_text(
        json.dumps(dict(schema_version=1, entries=[entries[k] for k in sorted(entries)]))
    )
    a = api.load_asset_benchmark_report(tmp_path / "a.json", resolver=resolver)
    b = api.load_asset_benchmark_report(tmp_path / "b.json", resolver=resolver)
    compared = (
        module("asset_comparison").compare_asset_reports(a, b, quality_only=True).to_json_object()
    )
    assert compared["status"] == "compatible", compared["reasons"]
    assert compared["arms"]["a"]["selector"] != compared["arms"]["b"]["selector"]


@pytest.mark.parametrize("mode", ["version", "identity", "catalog", "content", "timeout"])
def test_production_http_rejection_retains_primary_failure_and_distinct_floor(
    tmp_path, http_example, mode
):
    api = module("asset_benchmark")
    url, captured = http_example
    captured.mode = mode
    # Only the timeout case needs a modeled primary deadline; it is explicit
    # so host scheduling cannot turn the other modes into timeouts.
    budget = {"total_ms": 500, "primary_ms": 150} if mode == "timeout" else {}
    config = fixture_config(
        tmp_path,
        primary="http",
        url=url,
        fallback=True,
        repetitions=1,
        warmups=0,
        **budget,
    )
    report = asyncio.run(api.run_asset_benchmark(config))
    row = report.data["results"][0]
    assert row["status"] == "ready", row
    assert len(row["attempts"]) == 2
    assert row["attempts"][0]["outcome"] in ("invalid", "stale", "timeout")
    assert row["attempts"][1]["mechanism"] == "gateway_lexical_fallback"
    assert module("asset_metrics").primary_asset_result(row) is None
    assert row["result"]["final_candidates"][0]["asset_id"] == "calendar"


def test_valid_no_match_is_terminal_and_has_unknown_remote_usage(tmp_path, http_example):
    api = module("asset_benchmark")
    url, captured = http_example
    captured.mode = "no_match"
    report = asyncio.run(
        api.run_asset_benchmark(
            fixture_config(
                tmp_path, primary="http", url=url, fallback=True, repetitions=1, warmups=0
            )
        )
    )
    row = report.data["results"][0]
    assert row["status"] == "ready" and row["result"]["decision"] == "no_match"
    assert len(row["attempts"]) == 1 and row["attempts"][0]["cost_observations"] == []
    summary = next(
        s
        for s in report.data["summaries"]
        if s["dimensions"]["lane"] == "all"
        and s["dimensions"]["kind"] == "all"
        and s["dimensions"]["availability"] == "all"
        and s["dimensions"]["stale"] is None
        and s["dimensions"]["retrieval_mode"] == "service"
    )
    cost = next(
        c for c in summary["costs"] if c["metric"] == "calls" and c["provenance"] == "observed"
    )
    assert cost["total"] is None and cost["unknown"] == 1
    assert "cost_coverage_unknown" in report.data["limits"]


def test_inclusive_attempt_cost_does_not_double_count_embedding():
    api = module("asset_costs")
    cost = dict(
        provenance="reported",
        value=dict(
            input_tokens=7,
            output_tokens=None,
            cached_tokens=None,
            reasoning_tokens=None,
            calls=1,
            cpu_ms=None,
            peak_bytes=None,
        ),
    )
    row = dict(
        observed_costs=[],
        attempts=[dict(cost_observations=[cost]), dict(cost_observations=[])],
        embeddings=[dict(cost_observations=[cost])],
        resources=dict(cpu_ms=None, peak_bytes=None, coverage="unknown"),
    )
    summary = api.summarize_asset_costs([row])
    tokens = next(
        c for c in summary if c["metric"] == "input_tokens" and c["provenance"] == "reported"
    )
    assert tokens == dict(
        metric="input_tokens",
        provenance="reported",
        known=0,
        unknown=1,
        total=None,
        known_subtotal=7,
    )


def test_failed_primary_only_keeps_actual_rejected_attempt(tmp_path, http_example):
    api = module("asset_benchmark")
    url, captured = http_example
    captured.mode = "identity"
    report = asyncio.run(
        api.run_asset_benchmark(
            fixture_config(
                tmp_path, primary="http", url=url, fallback=False, repetitions=1, warmups=0
            )
        )
    )
    row = report.data["results"][0]
    assert row["status"] == "failed" and row["result"] is None
    assert len(row["attempts"]) == 1 and row["attempts"][0]["outcome"] == "invalid"
    assert row["attempts"][0]["result"]["selector"]["name"] == "wrong"


def test_unavailable_predispatch_has_no_invented_attempt_or_zero_cost(tmp_path, monkeypatch):
    api = module("asset_benchmark")
    config = fixture_config(tmp_path, repetitions=1, warmups=0)

    def unavailable(self, kinds):
        from relaymind.recommendations.errors import RecommendationError

        raise RecommendationError("recommendation_unavailable")

    monkeypatch.setattr(api._LockedProjectionProvider, "pin", unavailable)
    report = asyncio.run(api.run_asset_benchmark(config))
    row = report.data["results"][0]
    assert row["status"] == "failed" and row["attempts"] == [] and row["observed_costs"] == []
    assert row["request_digest"] is None and row["offered_projection_digest"] is None
    assert "offer_observation_incomplete" in report.data["limits"]


def test_async_cancel_keeps_current_attempt_and_marks_rest_not_run(tmp_path, http_example):
    api = module("asset_benchmark")
    url, captured = http_example
    captured.mode = "timeout"
    config = fixture_config(tmp_path, primary="http", url=url, repetitions=2, warmups=1)

    async def run():
        task = asyncio.create_task(api.run_asset_benchmark(config))
        for _ in range(200):
            if captured:
                task.cancel()
                return await task
            await asyncio.sleep(0.001)
        pytest.fail("HTTP benchmark did not dispatch within bounded rendezvous")

    report = asyncio.run(run())
    assert report.data["status"] == "interrupted"
    assert [r["status"] for r in report.data["results"]] == ["cancelled", "not_run", "not_run"]
    assert len(report.data["results"][0]["attempts"]) == 1
    assert report.data["results"][0]["attempts"][0]["outcome"] == "cancelled"


def test_fixture_config_refresh_only_authors_explicit_fixture_inputs(tmp_path):
    api = module("asset_benchmark")
    config = fixture_config(tmp_path, repetitions=1, warmups=0)
    api.refresh_fixture_benchmark_config(config.path)
    refreshed = api.load_benchmark_config(config.path)
    assert refreshed.data["lock_digest"] == config.data["lock_digest"]
    assert refreshed.release.lock["purpose"] == "fixture"


def test_export_never_writes_corpus_chosen_paths_under_output_root(tmp_path):
    api = module("asset_benchmark")
    config = fixture_config(tmp_path, repetitions=1, warmups=0)
    report = asyncio.run(api.run_asset_benchmark(config))
    original = next(e for e in config.release.catalog["evidence"] if e["kind"] == "original")
    sentinel = tmp_path / original["path"]
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("operator file; must stay unchanged")
    api.write_asset_benchmark_report(report, tmp_path / "report.json")
    assert sentinel.read_text() == "operator file; must stay unchanged"


def test_run_and_offline_compare_preserve_related_lock_closure(tmp_path):
    api = module("asset_benchmark")
    from test_asset_corpus import read, relock, revise_fixture_release, stage_fixture, write

    config = fixture_config(tmp_path, repetitions=1, warmups=0)
    root = config.path.parent
    stage_fixture(root / "baseline")
    revise_fixture_release(root / "baseline", 2)
    lock = read(root, "lock")
    lock["related_locks"] = [
        dict(
            id="baseline",
            path="baseline/lock.json",
            digest=read(root / "baseline", "lock")["digest"],
        )
    ]
    write(root, "lock", lock)
    relock(root)
    raw = config.path.read_text().replace(config.data["lock_digest"], read(root, "lock")["digest"])
    config.path.write_text(raw)
    linked = api.load_benchmark_config(config.path)
    assert len(linked.release.related_releases) == 1
    report = asyncio.run(api.run_asset_benchmark(linked))
    path = tmp_path / "linked.json"
    api.write_asset_benchmark_report(report, path)
    saved = api.load_asset_benchmark_report(
        path, resolver=path.with_name(path.name + ".resolver.json")
    )
    assert saved.inputs is not None, saved.input_error
    assert len(saved.inputs.release.related_releases) == 1
    assert (
        module("asset_comparison")
        .compare_asset_reports(saved, saved, quality_only=True)
        .data["status"]
        == "compatible"
    )


def test_production_validator_reads_bounded_snapshot_and_preserves_source_root(
    tmp_path, monkeypatch
):
    api = module("asset_benchmark")
    config = fixture_config(tmp_path, repetitions=1, warmups=0)
    original = api.parse_launcher_config
    actual = config.path.parent / config.data["production_config"]
    seen = []

    def validate(raw, *, config_dir):
        assert config_dir == actual.parent
        seen.append(raw)
        # The validator must use the bounded captured values, even when the
        # original file changes after capture. Semantic paths keep this root.
        actual.write_text("invalid input after capture", encoding="utf-8")
        return original(raw, config_dir=config_dir)

    monkeypatch.setattr(api, "parse_launcher_config", validate)
    loaded = api.load_benchmark_config(config.path)
    assert (
        seen
        and loaded.production.assets.sources[0].path == config.production.assets.sources[0].path
    )


def test_infrastructure_failure_preserves_full_plan_as_blocked_rows(tmp_path, monkeypatch):
    api = module("asset_benchmark")
    config = fixture_config(tmp_path)

    def unavailable(self, kinds):
        raise RuntimeError("private infrastructure detail")

    monkeypatch.setattr(api._LockedProjectionProvider, "pin", unavailable)
    report = asyncio.run(api.run_asset_benchmark(config))
    assert report.data["status"] == "blocked"
    assert [r["status"] for r in report.data["results"]] == ["failed", "not_run", "not_run"]
    assert all(r["reason"] in ("internal_error", "blocked") for r in report.data["results"])
    assert "private infrastructure detail" not in str(report.to_json_object())


def test_unlinked_embedding_cost_cannot_establish_complete_owned_total():
    api = module("asset_costs")
    cost = dict(
        provenance="observed",
        value=dict(
            input_tokens=7,
            output_tokens=0,
            cached_tokens=0,
            reasoning_tokens=0,
            calls=1,
            cpu_ms=None,
            peak_bytes=None,
        ),
    )
    row = dict(
        observed_costs=[],
        attempts=[dict(cost_observations=[cost])],
        embeddings=[dict(cost_observations=[cost])],
        resources=dict(cpu_ms=None, peak_bytes=None, coverage="unknown"),
    )
    summary = api.summarize_asset_costs([row])
    tokens = next(
        c for c in summary if c["metric"] == "input_tokens" and c["provenance"] == "observed"
    )
    assert tokens["total"] is None and tokens["known"] == 0 and tokens["known_subtotal"] == 7


def test_run_returns_real_manifest_commitment_and_verified_in_memory_comparison(tmp_path):
    api = module("asset_benchmark")
    report = asyncio.run(
        api.run_asset_benchmark(fixture_config(tmp_path, repetitions=1, warmups=0))
    )
    assert report.data["input_manifest"]["digest"] != "0" * 64
    assert report.data["input_manifest"]["bytes"] > 0
    assert report.inputs is not None
    assert (
        module("asset_comparison")
        .compare_asset_reports(report, report, quality_only=True)
        .data["status"]
        == "compatible"
    )


def test_unresolved_restricted_evidence_keeps_report_and_blocks_comparison(tmp_path):
    api = module("asset_benchmark")
    from test_asset_corpus import read, relock, write

    config = fixture_config(tmp_path, repetitions=1, warmups=0)
    root = config.path.parent
    catalog = read(root, "catalog")
    original = next(e for e in catalog["evidence"] if e["kind"] == "original")
    original.update(access="restricted", path=None)
    write(root, "catalog", catalog)
    relock(root)
    config.path.write_text(
        config.path.read_text().replace(config.data["lock_digest"], read(root, "lock")["digest"])
    )
    loaded = api.load_benchmark_config(config.path)
    report = asyncio.run(api.run_asset_benchmark(loaded))
    assert len(report.data["results"]) == 1 and report.data["input_manifest"]["bytes"] > 0
    compared = (
        module("asset_comparison").compare_asset_reports(report, report, quality_only=True).data
    )
    assert compared["status"] == "incompatible" and compared["paired"] == []


def test_single_kind_diagnostic_cohort_preserves_locked_request_envelope(tmp_path, http_example):
    api = module("asset_benchmark")
    from test_asset_corpus import read, relock, write

    url, captured = http_example
    config = fixture_config(tmp_path, primary="http", url=url, repetitions=1, warmups=0)
    cases = read(config.path.parent, "cases")
    cases["cases"][0].update(split="diagnostic", requested_kinds=["skill"])
    write(config.path.parent, "cases", cases)
    relock(config.path.parent)
    config.path.write_text(config.path.read_text().replace('"development"', '"diagnostic"'))
    api.refresh_fixture_benchmark_config(config.path)
    loaded = api.load_benchmark_config(config.path)
    report = asyncio.run(api.run_asset_benchmark(loaded))
    assert report.data["config"]["requested_kinds"] == ["skill"]
    assert captured[0]["requested_kinds"] == ["skill"]
    assert all(s["gate"] == "not_applicable" for s in report.data["summaries"])
    assert (
        module("asset_comparison")
        .compare_asset_reports(report, report, quality_only=True)
        .data["status"]
        == "compatible"
    )


@pytest.mark.parametrize(
    "locator,absolute",
    [(r"C:\evaluation\assets", True), ("/evaluation/assets", True), ("evaluation/assets", False)],
)
def test_private_source_locator_validation_is_platform_neutral(locator, absolute):
    # RM40/AC31: proof locators are opaque data; cross-device quality-only
    # comparison cannot reinterpret an absolute Windows path as a Linux path.
    assert module("asset_evidence").source_locator_is_absolute(locator) is absolute
