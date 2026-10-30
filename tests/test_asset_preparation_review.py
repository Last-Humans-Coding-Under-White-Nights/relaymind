"""RM35/RM40 AC26/AC31: lifecycle review evidence cannot promote cleanup or costs."""

import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from helpers import FakeClock
from test_asset_index_preparation_integration import example
from test_asset_selection_benchmark import fixture_config


@pytest.fixture(scope="module")
def prepared_bundle(tmp_path_factory):
    from relaymind.evaluation.asset_benchmark import (
        load_benchmark_config,
        refresh_fixture_benchmark_config,
        run_asset_benchmark,
        write_asset_benchmark_report,
    )

    root = tmp_path_factory.mktemp("prepared-review")
    module = example()
    server = module.make_server("127.0.0.1", 0, preparation_path="/index")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        config = fixture_config(root, primary="http", url=url + "/select", repetitions=1, warmups=0)
        with config.path.open("a") as out:
            out.write(
                '\n[preparation]\nmode="http"\nurl="' + url + '/index"\nprofile="cold"\n'
                "max_polls=1\npoll_ms=50\ntotal_ms=5000\nrequest_ms=1000\n"
            )
        refresh_fixture_benchmark_config(config.path)
        report = asyncio.run(run_asset_benchmark(load_benchmark_config(config.path)))
        path = root / "original.json"
        write_asset_benchmark_report(report, path)
        assert report.data["preparation"]["status"] == "ready"
        return path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def corrupt(preparation, mutation):
    ready = copy.deepcopy(preparation["exchanges"][-1]["response"])

    def exchange(operation, state, reason=None):
        response = copy.deepcopy(ready)
        response.update(state=state, reason=reason)
        if state != "ready":
            response.update(index_revision=None, reused=None)
        return dict(operation=operation, observed_ms=0.1, response=response, reason=None)

    if mutation == "failed_then_ready_cleanup":
        preparation["exchanges"] = [
            exchange("prepare", "failed", "build_failed"),
            exchange("cancel", "ready"),
        ]
        preparation["cleanup_ms"] = 0.2
    elif mutation == "extra_poll":
        preparation["exchanges"] = [
            exchange("prepare", "indexing"),
            exchange("status", "indexing"),
            exchange("status", "ready"),
        ]
    elif mutation == "phantom_cleanup":
        preparation["cleanup_ms"] = 999999
    elif mutation == "missing_poll_wait":
        preparation["observed_ms"] = sum(e["observed_ms"] for e in preparation["exchanges"])
    elif mutation == "ready_total_deadline":
        preparation["observed_ms"] = 5001
    elif mutation == "ready_request_deadline":
        preparation["exchanges"][-1]["observed_ms"] = 1001
        preparation["observed_ms"] = 2000
    else:
        value = 0 if mutation == "observed_zero" else 17
        preparation["observed_costs"] = [
            dict(
                provenance="observed",
                value={
                    k: value
                    for k in (
                        "input_tokens",
                        "output_tokens",
                        "cached_tokens",
                        "reasoning_tokens",
                        "calls",
                        "cpu_ms",
                        "peak_bytes",
                    )
                },
            )
        ]


@pytest.mark.parametrize(
    "mutation",
    [
        "failed_then_ready_cleanup",
        "extra_poll",
        "phantom_cleanup",
        "missing_poll_wait",
        "ready_total_deadline",
        "ready_request_deadline",
        "observed_zero",
        "observed_nonzero",
    ],
)
def test_offline_and_real_cli_refuse_impossible_preparation(prepared_bundle, tmp_path, mutation):
    from relaymind.evaluation.asset_benchmark import load_asset_benchmark_report
    from relaymind.evaluation.asset_benchmark_cli import main
    from relaymind.evaluation.asset_comparison import compare_asset_reports

    path = prepared_bundle
    raw = json.loads(path.read_text())
    corrupt(raw["preparation"], mutation)
    forged_path = path.with_name("forged-" + mutation + ".json")
    forged_path.write_text(json.dumps(raw))
    resolver = path.with_name(path.name + ".resolver.json")
    original = load_asset_benchmark_report(path, resolver=resolver)
    forged = load_asset_benchmark_report(
        forged_path, inputs=path.with_name(path.name + ".inputs.json"), resolver=resolver
    )
    library = compare_asset_reports(original, forged, quality_only=True).data
    output, markdown = tmp_path / "comparison.json", tmp_path / "comparison.md"
    code = main(
        [
            "compare",
            str(path),
            str(forged_path),
            "--quality-only",
            "--resolver",
            str(resolver),
            "--inputs-a",
            str(path.with_name(path.name + ".inputs.json")),
            "--inputs-b",
            str(path.with_name(path.name + ".inputs.json")),
            "--output",
            str(output),
            "--markdown",
            str(markdown),
        ]
    )
    assert code == 2, f"impossible transcript accepted by real CLI: {mutation}"
    assert library["status"] == "incompatible"
    assert json.loads(output.read_text())["paired"] == []
    assert "incompatible" in markdown.read_text()


def test_queued_expiry_is_terminal_before_worker_start_and_replay_cancel(monkeypatch):
    from relaymind.selection.asset_preparation import parse_preparation_request
    from test_asset_preparation import inputs

    module = example()
    clock = FakeClock()
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=clock.monotonic))
    entered, release = threading.Event(), threading.Event()
    original = module.build_lexical_index
    builds = []

    async def held(*args, **kwargs):
        builds.append(args[0])
        entered.set()
        assert release.wait(2)
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "build_lexical_index", held)
    _, catalog, binding, _ = inputs()

    def request(identity, budget):
        return parse_preparation_request(
            dict(
                schema_version=1,
                preparation_id=identity,
                operation="prepare",
                binding=binding,
                profile="cold",
                catalog=catalog,
                budget_ms=budget,
            )
        )

    registry = module.IndexedExample()
    first = request("00000000-0000-4000-8000-000000000001", 1000)
    second = request("00000000-0000-4000-8000-000000000002", 1)
    try:
        registry.exchange(first)
        assert entered.wait(1)
        registry.exchange(second)
        clock.advance(0.03)
        status = dict(second, operation="status", catalog=None, budget_ms=None)
        expired = registry.exchange(status)
        assert expired["state"] == "failed" and expired["reason"] == "deadline_exceeded"
        assert registry.exchange(second) == expired
        assert registry.exchange(dict(status, operation="cancel")) == expired
        release.set()
        registry.executor.shutdown(wait=True)
        assert registry.exchange(status) == expired
        assert len(builds) == 1, "expired queued work must never enter the CPU builder"
    finally:
        release.set()
        registry.stop()


@pytest.mark.parametrize("kind", ["late_ready", "terminal_restart", "native_cancel"])
def test_client_records_deadline_and_invalid_cleanup_without_false_readiness(monkeypatch, kind):
    import httpx

    from relaymind.selection import asset_preparation as module
    from test_asset_preparation import inputs, reply, response

    _, catalog, binding, config = inputs()
    config |= dict(total_ms=100, request_ms=100)
    clock = FakeClock()
    original = module.parse_preparation_response

    def parse(value, request):
        parsed = original(value, request)
        if kind == "late_ready" and request.get("operation") == "prepare":
            clock.advance(0.2)
        return parsed

    monkeypatch.setattr(module, "parse_preparation_response", parse)
    entered = asyncio.Event()

    async def handler(request):
        raw = json.loads(request.content)
        if raw["operation"] == "cancel":
            return reply(200, json=response(raw))
        if kind == "native_cancel":
            entered.set()
            await asyncio.sleep(10)
        return reply(
            200,
            json=response(raw, state="failed", reason="build_failed")
            if kind == "terminal_restart"
            else response(raw),
        )

    async def go():
        task = asyncio.create_task(
            module.prepare_asset_index(
                config,
                binding,
                catalog,
                clock=clock.monotonic,
                transport_factory=lambda: httpx.MockTransport(handler),
            )
        )
        if kind == "native_cancel":
            await entered.wait()
            task.cancel()
        return await task

    record = asyncio.run(go())
    expected = {
        "late_ready": "timeout",
        "terminal_restart": "failed",
        "native_cancel": "cancelled",
    }[kind]
    assert record["status"] == expected
    if kind == "terminal_restart":
        assert record["reason"] == "build_failed"
        assert record["exchanges"][-1]["reason"] == "invalid_response"
        assert record["exchanges"][-1]["response"] is None
    elif kind == "native_cancel":
        assert record["exchanges"][0]["reason"] == "cancelled"
    module.validate_preparation_record(record, config=config, binding=binding)


@pytest.mark.parametrize("ending", ["ready", "failed", "timeout_ready_cleanup"])
def test_genuine_main_outcomes_and_reported_claims_stay_valid(ending):
    import httpx

    from relaymind.selection import asset_preparation as module
    from test_asset_preparation import inputs, reply, response

    _, catalog, binding, config = inputs()
    config |= dict(total_ms=80, request_ms=60, max_polls=5)
    requests = []

    def handler(request):
        raw = json.loads(request.content)
        requests.append(raw)
        if raw["operation"] == "cancel":
            state, reason = (
                ("ready", None) if ending == "timeout_ready_cleanup" else ("failed", "build_failed")
            )
        elif ending == "ready":
            state, reason = "ready", None
        elif ending == "failed":
            state, reason = "failed", "build_failed"
        else:
            state, reason = "indexing", None
        claim = dict(
            provenance="reported",
            value={
                k: None
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cached_tokens",
                    "reasoning_tokens",
                    "calls",
                    "cpu_ms",
                    "peak_bytes",
                )
            },
        )
        claim["value"]["calls"] = len(requests)
        return reply(200, json=response(raw, state=state, reason=reason, usage_claims=claim))

    record = asyncio.run(
        module.prepare_asset_index(
            config, binding, catalog, transport_factory=lambda: httpx.MockTransport(handler)
        )
    )
    module.validate_preparation_record(record, config=config, binding=binding)
    assert record["observed_costs"] == []
    assert (
        record["status"]
        == {"ready": "ready", "failed": "failed", "timeout_ready_cleanup": "timeout"}[ending]
    )
    if ending == "timeout_ready_cleanup":
        assert record["exchanges"][-1]["response"]["state"] == "ready"
        assert record["index_revision"] is None
    claims = [
        e["response"]["usage_claims"]["value"]["calls"]
        for e in record["exchanges"]
        if e["response"] is not None
    ]
    assert claims == list(range(1, len(claims) + 1))


def test_real_cli_keeps_valid_ready_setup_with_unknown_costs(prepared_bundle, tmp_path):
    """RM40/AC31: hostile transcript checks must not reject valid original evidence."""
    from relaymind.evaluation.asset_benchmark_cli import main

    original = json.loads(prepared_bundle.read_text())
    assert original["preparation"]["observed_costs"] == []
    assert all(e["response"]["usage_claims"] is None for e in original["preparation"]["exchanges"])
    output, markdown = tmp_path / "valid.json", tmp_path / "valid.md"
    assert (
        main(
            [
                "compare",
                str(prepared_bundle),
                str(prepared_bundle),
                "--quality-only",
                "--resolver",
                str(prepared_bundle.with_name(prepared_bundle.name + ".resolver.json")),
                "--output",
                str(output),
                "--markdown",
                str(markdown),
            ]
        )
        == 0
    )
    result = json.loads(output.read_text())
    assert result["status"] == "compatible" and result["preparation"]["a"]["status"] == "ready"


def test_known_cleanup_ack_cannot_claim_time_beyond_cleanup_budget():
    import httpx

    from relaymind.selection import asset_preparation as module
    from test_asset_preparation import inputs, reply, response

    _, catalog, binding, config = inputs()

    def handle(request):
        raw = json.loads(request.content)
        return reply(200, json=response(raw, state="failed", reason="build_failed"))

    record = asyncio.run(
        module.prepare_asset_index(
            config, binding, catalog, transport_factory=lambda: httpx.MockTransport(handle)
        )
    )
    module.validate_preparation_record(record, config=config, binding=binding)
    record["cleanup_ms"] = 999999
    with pytest.raises(ValueError):
        module.validate_preparation_record(record, config=config, binding=binding)
    # Slow unsuccessful cleanup is retained as an actual failed exchange,
    # rather than clipped or presented as a deadline-bound acknowledgement.
    record["exchanges"][-1].update(response=None, reason="deadline_exceeded", observed_ms=999999)
    module.validate_preparation_record(record, config=config, binding=binding)
