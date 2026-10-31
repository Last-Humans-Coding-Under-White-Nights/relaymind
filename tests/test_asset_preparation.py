"""RM35/RM40, AC26/AC31: explicit bounded preparation preserves query identity."""

import asyncio
import copy
import importlib
import json
from pathlib import Path

import httpx
import pytest

from relaymind.canonical import canonical_digest


class Wire(httpx.AsyncByteStream):
    def __init__(self, payload):
        self.payload = payload

    async def __aiter__(self):
        yield self.payload


def reply(status, *, json=None, content=None, headers=None):
    payload = content if content is not None else __import__("json").dumps(json).encode()
    return httpx.Response(
        status, stream=Wire(payload), headers=headers or {"content-type": "application/json"}
    )


def api():
    try:
        return importlib.import_module("relaymind.selection.asset_preparation")
    except ModuleNotFoundError:
        pytest.fail("RM35/AC26 preparation client is not implemented")


def inputs():
    m = api()
    catalog = [
        json.loads(Path("tests/fixtures/asset-benchmark/catalog.json").read_text())["assets"][0][
            "record"
        ]
    ]
    binding = m.preparation_binding(
        selector={"name": "team", "version": "1"},
        catalog_revision=1,
        catalog=catalog,
        scope={"user_id": "u", "workspace_id": "w"},
        permissions=[],
        selection_scope="discovery",
        requested_kinds=["skill", "mcp_tool"],
    )
    config = m.normalize_preparation(
        dict(mode="http", url="http://127.0.0.1:8913/index", profile="cold", poll_ms=50)
    )
    return m, catalog, binding, config


def response(request, *, state="ready", **changes):
    return (
        dict(
            schema_version=1,
            preparation_id=request["preparation_id"],
            binding=request["binding"],
            profile=request["profile"],
            state=state,
            index_revision="index-1" if state == "ready" else None,
            reused=False if state == "ready" else None,
            elapsed_ms=None,
            usage_claims=None,
            reason=None if state in ("ready", "indexing") else "build_failed",
        )
        | changes
    )


def test_none_is_explicit_unknown_setup_and_sends_no_traffic():
    m = api()
    value = asyncio.run(m.prepare_asset_index(m.normalize_preparation(None), None, []))
    assert value["status"] == "not_applicable"
    assert value["observed_ms"] is value["cleanup_ms"] is None
    assert value["exchanges"] == value["observed_costs"] == []
    assert value["binding"] is value["preparation_id"] is None


@pytest.mark.parametrize(
    "change",
    [
        dict(mode="http"),
        dict(mode="none", url="http://127.0.0.1/index"),
        dict(mode="http", url="http://example.com/index", profile="cold"),
        dict(total_ms=True),
        dict(request_ms=10001),
        dict(poll_ms=1),
        dict(total_ms=1, request_ms=2),
        dict(extra=1),
    ],
)
def test_config_rejects_unsafe_or_unbounded_settings(change):
    with pytest.raises(ValueError):
        api().normalize_preparation(change)


def test_indexing_polls_preserve_cumulative_claims_and_exact_offer():
    m, catalog, binding, config = inputs()
    requests = []

    def handle(req):
        value = json.loads(req.content)
        requests.append(value)
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
        return reply(
            200,
            json=response(
                value, state="indexing" if len(requests) == 1 else "ready", usage_claims=claim
            ),
        )

    record = asyncio.run(
        m.prepare_asset_index(
            config, binding, catalog, transport_factory=lambda: httpx.MockTransport(handle)
        )
    )
    assert record["status"] == "ready" and record["index_revision"] == "index-1"
    assert [q["operation"] for q in requests] == ["prepare", "status"]
    assert requests[0]["catalog"] == catalog and requests[1]["catalog"] is None
    assert binding["catalog_digest"] == canonical_digest(catalog)
    assert [e["response"]["usage_claims"]["value"]["calls"] for e in record["exchanges"]] == [1, 2]
    assert record["observed_costs"] == []
    assert record["observed_ms"] >= 50 and record["cleanup_ms"] is None


@pytest.mark.parametrize(
    "mutation",
    ["binding", "profile", "id", "cold_reused", "extra", "duplicate", "oversize", "redirect"],
)
def test_invalid_prepare_is_not_readiness_and_cleanup_is_single_bounded_exchange(mutation):
    m, catalog, binding, config = inputs()
    requests = []

    def handle(req):
        value = json.loads(req.content)
        requests.append(value)
        result = response(value)
        if value["operation"] == "cancel":
            return reply(200, json=response(value, state="cancelled", reason="cancelled"))
        if mutation == "binding":
            result["binding"]["catalog_revision"] = 2
        elif mutation == "profile":
            result["profile"] = "reuse"
        elif mutation == "id":
            result["preparation_id"] = "00000000-0000-0000-0000-000000000000"
        elif mutation == "cold_reused":
            result["reused"] = True
        elif mutation == "extra":
            result["extra"] = "http://example.com/never-follow"
        elif mutation == "duplicate":
            return reply(
                200,
                content=b'{"schema_version":1,"schema_version":1}',
                headers={"content-type": "application/json"},
            )
        elif mutation == "oversize":
            return reply(
                200, content=b" " * (1024 * 1024 + 1), headers={"content-type": "application/json"}
            )
        elif mutation == "redirect":
            return reply(302, headers={"location": "http://example.com/never-follow"})
        return reply(200, json=result)

    record = asyncio.run(
        m.prepare_asset_index(
            config, binding, catalog, transport_factory=lambda: httpx.MockTransport(handle)
        )
    )
    assert record["status"] == "failed" and record["index_revision"] is None
    assert [q["operation"] for q in requests] == ["prepare", "cancel"]
    assert record["cleanup_ms"] is not None


@pytest.mark.parametrize("ending", ["poll_limit", "cancelled", "timeout"])
def test_polling_stops_and_records_cleanup_without_fabricating_acknowledgement(ending):
    m, catalog, binding, config = inputs()
    requests = []
    config |= {"max_polls": 1}
    if ending == "timeout":
        config |= {"total_ms": 20, "request_ms": 10}

    def handle(req):
        value = json.loads(req.content)
        requests.append(value)
        if value["operation"] == "cancel":
            raise httpx.ConnectError("fixture")
        return reply(200, json=response(value, state="indexing"))

    record = asyncio.run(
        m.prepare_asset_index(
            config,
            binding,
            catalog,
            cancelled=lambda: ending == "cancelled" and bool(requests),
            transport_factory=lambda: httpx.MockTransport(handle),
        )
    )
    assert (
        record["status"]
        == {"poll_limit": "failed", "cancelled": "cancelled", "timeout": "timeout"}[ending]
    )
    assert (
        record["reason"]
        == {"poll_limit": "poll_limit", "cancelled": "cancelled", "timeout": "deadline_exceeded"}[
            ending
        ]
    )
    assert requests[-1]["operation"] == "cancel"
    assert record["exchanges"][-1]["response"] is None
    assert record["exchanges"][-1]["reason"] == "transport_error"
    assert record["observed_ms"] < 1000 and record["cleanup_ms"] < 1000


def test_preparation_response_strict_closed_state_rules():
    m, catalog, binding, config = inputs()
    request = dict(
        schema_version=1,
        preparation_id="00000000-0000-4000-8000-000000000001",
        operation="prepare",
        binding=binding,
        profile="cold",
        catalog=catalog,
        budget_ms=100,
    )
    assert m.parse_preparation_request(request) == request
    for change in (
        dict(index_revision=None),
        dict(reused=None),
        dict(reason="secret error"),
        dict(elapsed_ms=float("nan")),
    ):
        with pytest.raises(ValueError):
            m.parse_preparation_response(response(request) | change, request)


@pytest.mark.parametrize("result_kind", ["ready", "wrong_index", "not_ready", "wrong_scope"])
def test_query_wrapper_pins_index_and_scope_without_hiding_actual_not_ready(result_kind):
    import time

    from relaymind.selection.asset_contract import AttemptFailure, SelectorContext
    from test_asset_index_preparation_integration import query

    m, catalog, binding, config = inputs()
    preparation = dict(status="ready", binding=binding, index_revision="index-1")

    class Primary:
        name = "team"
        version = "1"

        async def select(self, request, context):
            return dict(
                schema_version=2,
                recommendation_id=request.data["recommendation_id"],
                catalog_revision=1,
                host_snapshot_revision=None,
                selector=binding["selector"],
                mechanism="team-rank",
                outcome="not_ready" if result_kind == "not_ready" else "no_match",
                decision=None if result_kind == "not_ready" else "no_match",
                candidates=[],
                index_revision="wrong" if result_kind == "wrong_index" else "index-1",
                elapsed_ms=0,
                usage_claims=None,
            )

    request_binding = copy.deepcopy(binding)
    if result_kind == "wrong_scope":
        request_binding["scope"]["user_id"] = "foreign"
    wrapper = m.PreparedAssetSelector(Primary(), preparation)

    async def go():
        return await wrapper.select(
            query(request_binding, catalog), SelectorContext(time.monotonic, lambda: None)
        )

    if result_kind in ("wrong_index", "wrong_scope"):
        with pytest.raises(AttemptFailure) as failure:
            asyncio.run(go())
        assert failure.value.outcome == "stale"
    else:
        assert asyncio.run(go())["outcome"] == (
            "not_ready" if result_kind == "not_ready" else "no_match"
        )


def test_predispatch_cancellation_is_a_valid_retained_record_without_fake_exchange():
    m, catalog, binding, config = inputs()
    record = asyncio.run(m.prepare_asset_index(config, binding, catalog, cancelled=lambda: True))
    assert record["status"] == "cancelled" and record["reason"] == "cancelled"
    assert record["exchanges"] == [] and record["cleanup_ms"] is None
    m.validate_preparation_record(record, config=config, binding=binding)


@pytest.mark.parametrize("mutation", ["duplicate_asset", "wrong_kind", "oversized_binding"])
def test_preparation_offer_obeys_existing_identity_kind_and_byte_limits(mutation):
    m, catalog, binding, config = inputs()
    if mutation == "duplicate_asset":
        catalog *= 2
        binding["catalog_digest"] = canonical_digest(catalog)
    elif mutation == "wrong_kind":
        binding["requested_kinds"] = ["mcp_tool"]
    else:
        binding["scope"]["user_id"] = "用" * 100
    request = dict(
        schema_version=1,
        preparation_id="00000000-0000-4000-8000-000000000001",
        operation="prepare",
        binding=binding,
        profile="cold",
        catalog=catalog,
        budget_ms=100,
    )
    with pytest.raises(ValueError):
        m.parse_preparation_request(request)


@pytest.mark.parametrize(
    "mutation", ["status_catalog", "ready_index", "cold_reused", "indexing_index"]
)
def test_published_companion_schemas_enforce_operation_and_state_shapes(mutation):
    from relaymind.schema_util import StrictValidator, load_packaged_schema

    m, catalog, binding, config = inputs()
    request = dict(
        schema_version=1,
        preparation_id="00000000-0000-4000-8000-000000000001",
        operation="prepare",
        binding=binding,
        profile="cold",
        catalog=catalog,
        budget_ms=100,
    )
    value = request if mutation == "status_catalog" else response(request)
    kind = "request" if mutation == "status_catalog" else "response"
    if mutation == "status_catalog":
        value["operation"] = "status"
    elif mutation == "ready_index":
        value["index_revision"] = None
    elif mutation == "cold_reused":
        value["reused"] = True
    else:
        value["state"] = "indexing"
    validator = StrictValidator(
        load_packaged_schema("relaymind.selection", f"asset_preparation_{kind}.v1.json")
    )
    assert next(validator.iter_errors(value), None) is not None


@pytest.mark.parametrize("mutation", ["reason", "cost", "repeated_prepare", "elapsed"])
def test_transcript_cannot_fabricate_state_cost_or_extra_dispatch(mutation):
    m, catalog, binding, config = inputs()
    tick = [100.0]

    def handle(req):
        if mutation == "elapsed":
            tick[0] += 0.025
        return reply(200, json=response(json.loads(req.content)))

    if mutation in ("reason", "cost"):
        record = asyncio.run(
            m.prepare_asset_index(config, binding, catalog, cancelled=lambda: True)
        )
        if mutation == "reason":
            record["reason"] = "invented-secret-reason"
        else:
            record["observed_costs"] = [dict(provenance="reported", value={})]
    else:
        record = asyncio.run(
            m.prepare_asset_index(
                config,
                binding,
                catalog,
                clock=lambda: tick[0] if mutation == "elapsed" else 100.0,
                transport_factory=lambda: httpx.MockTransport(handle),
            )
        )
        if mutation == "repeated_prepare":
            record["exchanges"].append(copy.deepcopy(record["exchanges"][0]))
        else:
            assert record["exchanges"][0]["observed_ms"] > 0
            assert record["observed_ms"] >= record["exchanges"][0]["observed_ms"]
            m.validate_preparation_record(record, config=config, binding=binding)
            record["observed_ms"] = 0
    with pytest.raises(
        ValueError, match="invalid_preparation_time" if mutation == "elapsed" else None
    ):
        m.validate_preparation_record(record, config=config, binding=binding)


def test_zero_clock_ready_preparation_is_valid():
    """RM35/AC26: coarse clocks may honestly record zero elapsed work."""
    m, catalog, binding, config = inputs()

    def handle(req):
        return reply(200, json=response(json.loads(req.content)))

    record = asyncio.run(
        m.prepare_asset_index(
            config,
            binding,
            catalog,
            clock=lambda: 100.0,
            transport_factory=lambda: httpx.MockTransport(handle),
        )
    )
    assert record["status"] == "ready"
    assert record["observed_ms"] == record["exchanges"][0]["observed_ms"] == 0
    m.validate_preparation_record(record, config=config, binding=binding)


def test_coarse_clock_tolerance_covers_the_cleanup_exchange_and_record():
    # RM35/AC26: a coarse clock can report just over the request window for an
    # on-time cleanup; the exchange check and the record allow the same 1 ms.
    m, catalog, binding, config = inputs()
    config |= {"max_polls": 1}

    def handle(req):
        value = json.loads(req.content)
        if value["operation"] == "cancel":
            return reply(200, json=response(value, state="cancelled", reason="cancelled"))
        return reply(200, json=response(value, state="indexing"))

    record = asyncio.run(
        m.prepare_asset_index(
            config, binding, catalog, transport_factory=lambda: httpx.MockTransport(handle)
        )
    )
    assert record["status"] == "failed"
    cleanup = record["exchanges"][-1]
    assert cleanup["operation"] == "cancel" and cleanup["response"] is not None
    record["cleanup_ms"] = cleanup["observed_ms"] = 1000.0005
    m.validate_preparation_record(record, config=config, binding=binding)
    record["cleanup_ms"] = cleanup["observed_ms"] = 1001
    with pytest.raises(ValueError, match="invalid_preparation_exchange"):
        m.validate_preparation_record(record, config=config, binding=binding)
