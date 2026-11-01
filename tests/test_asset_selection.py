"""RM35/AC26: isolated, closed recommendation contracts and projection-only selection."""

import asyncio
import copy
import json
from datetime import UTC, datetime

import pytest

import helpers
from relaymind.assets.catalog import CatalogSnapshot, validate_asset_record
from relaymind.canonical import canonical_json
from test_asset_catalog import record


def api():
    from relaymind.selection import asset_contract

    return asset_contract


def request(**changes):
    value = dict(
        schema_version=2,
        recommendation_id="7e46678a-d704-4027-b916-2c84de864101",
        task_id="task:1",
        task_text="Greeting 世界",
        conversation=[{"role": "user", "text": "Hello"}],
        scope={"user_id": "u", "workspace_id": "w"},
        permissions=["assets.read"],
        requested_kinds=["skill", "mcp_tool"],
        selection_scope="discovery",
        catalog_revision=1,
        host_snapshot_revision=None,
        language_tags=["en", "zh-CN"],
        code_switched=True,
        catalog=[record()],
        deadline_ms=150,
        candidate_limit=10,
    )
    value.update(changes)
    return value


def candidate(**changes):
    value = dict(
        asset_id=record()["asset_id"],
        kind="skill",
        content_hash=record()["content_hash"],
        source_version=None,
        score=1,
        score_kind="token_overlap_count",
        evidence_refs=["/description"],
    )
    value.update(changes)
    return value


def result(**changes):
    r = request()
    value = dict(
        schema_version=2,
        recommendation_id=r["recommendation_id"],
        catalog_revision=1,
        host_snapshot_revision=None,
        selector={"name": "custom", "version": "1"},
        mechanism="custom_rule",
        outcome="ranked",
        decision="choice_required",
        candidates=[candidate()],
        index_revision=None,
        elapsed_ms=0,
        usage_claims=None,
    )
    value.update(changes)
    return value


def snapshot(records=None, revision=1):
    return CatalogSnapshot(
        1,
        revision,
        "2026-10-29T00:00:00Z",
        (),
        tuple(validate_asset_record(r) for r in ([record()] if records is None else records)),
    )


def test_v2_closed_frozen_and_separate_from_v1():
    a = api()
    raw = request()
    parsed = a.parse_asset_request(raw)
    raw["catalog"][0]["description"] = "changed"
    assert parsed.to_json_object()["catalog"][0]["description"] == "Greeting skill"
    with pytest.raises(TypeError):
        parsed.data["scope"]["user_id"] = "foreign"
    from relaymind.selection.contract import SelectionError, parse_selection_request

    with pytest.raises(SelectionError):
        parse_selection_request(request())
    with pytest.raises(a.AssetSelectionError):
        a.parse_asset_request({"schema_version": 1})
    for key in request():
        raw = request()
        del raw[key]
        with pytest.raises(a.AssetSelectionError):
            a.parse_asset_request(raw)
    for key in result():
        raw = result()
        del raw[key]
        with pytest.raises(a.AssetSelectionError):
            a.parse_asset_result(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 2.0},
        {"schema_version": 1},
        {"recommendation_id": "not-uuid"},
        {"task_id": "x" * 257},
        {"task_text": "中" * 2731},
        {"task_text": ""},
        {"deadline_ms": 0},
        {"deadline_ms": True},
        {"deadline_ms": float("inf")},
        {"catalog_revision": True},
        {"scope": {"user_id": "u", "workspace_id": "w", "actor": "user"}},
        {"scope": {"user_id": "", "workspace_id": "w"}},
        {"permissions": ["x", "x"]},
        {"permissions": [""]},
        {"requested_kinds": []},
        {"requested_kinds": ["skill", "skill"]},
        {"requested_kinds": ["tool"]},
        {"host_snapshot_revision": "fake"},
        {"code_switched": 1},
        {"language_tags": ["en", "en"]},
        {"language_tags": ["中文"]},
        {"language_tags": ["e" * 65]},
        {"conversation": [{"role": "tool", "text": "x"}]},
        {"conversation": [{"role": "user", "text": "x", "content": "x"}]},
        {"conversation": [{"role": "user", "text": "中" * 10923}]},
        {"conversation": [{"role": "user", "text": "x"}] * 33},
        {"candidate_limit": 9},
        {"body": "secret"},
        {"feedback": []},
        {"catalog": [record()] * 1001},
        {"catalog": [record(), record()]},
        {
            "catalog": [
                record(identity={"package_id": "x", "package_version": None, "tool_name": "x"})
            ]
        },
    ],
)
def test_invalid_request(changes):
    a = api()
    with pytest.raises(a.AssetSelectionError):
        a.parse_asset_request(request(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 3},
        {"catalog": [record(), record()]},
        {"requested_kinds": ["mcp_tool"]},
        {"conversation": [{"role": "user", "text": "x" * 16384}] * 3},
    ],
)
def test_request_validation_reports_invalid_request_not_invalid_result(changes):
    """RM35/AC26: a malformed caller request is invalid_request; invalid_result is for selectors."""
    a = api()
    with pytest.raises(a.AssetSelectionError) as caught:
        a.parse_asset_request(request(**changes))
    assert str(caught.value) == "invalid_request"


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 2.0},
        {"catalog_revision": True},
        {"elapsed_ms": -1},
        {"elapsed_ms": float("nan")},
        {"elapsed_ms": True},
        {"outcome": "empty_catalog"},
        {"outcome": "ranked", "candidates": []},
        {"outcome": "no_match"},
        {"decision": "no_match"},
        {"candidates": [candidate(), candidate()]},
        {"candidates": [candidate(score=True)]},
        {"candidates": [candidate(score=float("inf"))]},
        {"candidates": [candidate(evidence_refs=["/description", "/description"])]},
        {"candidates": [candidate(evidence_refs=["/bad~2escape"])]},
        {"candidates": [candidate(evidence_refs=["https://example.invalid"])]},
        {"candidates": [candidate(evidence_refs=["/x" * 129])]},
        {"candidates": [candidate(tool_call={})]},
        {"permissions": []},
        {"confidence": 0.9},
        {"usage_claims": {"provenance": "observed", "value": {}}},
    ],
)
def test_invalid_result(changes):
    a = api()
    with pytest.raises(a.AssetSelectionError):
        a.parse_asset_result(result(**changes))


def test_cost_unknowns_closed_and_nonfinite():
    a = api()
    cost = dict.fromkeys(
        [
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "calls",
            "cpu_ms",
            "peak_bytes",
        ]
    )
    raw = result(usage_claims={"provenance": "reported", "value": cost})
    assert a.parse_asset_result(raw).to_json_object() == raw
    for key in cost:
        for bad in (True, -1, float("inf")):
            other = copy.deepcopy(raw)
            other["usage_claims"]["value"][key] = bad
            with pytest.raises(a.AssetSelectionError):
                a.parse_asset_result(other)
    raw["usage_claims"]["value"]["secret"] = "x"
    with pytest.raises(a.AssetSelectionError):
        a.parse_asset_result(raw)


def test_offer_validation_retains_syntactically_valid_rejections():
    a = api()
    from relaymind.selection.asset_admission import validate_against_offer

    req = a.parse_asset_request(request())
    for changes, outcome in [
        ({"selector": {"name": "wrong", "version": "1"}}, "invalid"),
        ({"recommendation_id": "7e46678a-d704-4027-b916-2c84de864102"}, "stale"),
        ({"catalog_revision": 2}, "stale"),
        ({"candidates": [candidate(content_hash="a" * 64)]}, "invalid"),
        ({"candidates": [candidate(evidence_refs=["/body"])]}, "invalid"),
        ({"candidates": [candidate(availability="available")]}, "invalid"),
        ({"mechanism": "gateway_lexical_fallback"}, "invalid"),
        ({"mechanism": "native_function_calling"}, "invalid"),
        ({"mechanism": "lexical_overlap"}, "invalid"),
    ]:
        parsed = a.parse_asset_result(result(**changes))
        with pytest.raises(a.AttemptFailure) as caught:
            validate_against_offer(parsed, req, a.SelectorIdentity("custom", "1"), http=True)
        assert caught.value.outcome == outcome
        assert caught.value.result is parsed
    validate_against_offer(
        a.parse_asset_result(result()), req, a.SelectorIdentity("custom", "1"), http=True
    )


def ready_value():
    return dict(
        schema_version=1,
        decision="empty_catalog",
        catalog_revision=1,
        host_snapshot_revision=None,
        attempts=[],
        primary_attempt=None,
        base_candidates=[],
        base_decision="empty_catalog",
        final_candidates=[],
        final_decision="empty_catalog",
        fallback_reason=None,
        adaptation=None,
        admission={
            "outcome": "accepted",
            "rechecked_catalog_revision": 1,
            "checked_at": "2026-10-29T00:00:00Z",
        },
        observed_ms=0,
        embeddings=[],
        host_run_ids=[],
    )


def test_final_shape_and_obsolete_duplicate_fields_rejected():
    a = api()
    value = ready_value()
    assert a.parse_recommendation_result(value).to_json_object() == value
    for changes in (
        {"primary_result": result()},
        {"primary_attempt": 1},
        {
            "adaptation": {
                "identity": {"name": "x", "version": "1"},
                "feedback_revision": 1,
                "changed": False,
                "before_candidates": [],
            }
        },
        {"final_decision": "no_match"},
        {"host_run_ids": ["fake"]},
    ):
        with pytest.raises(a.AssetSelectionError):
            a.parse_recommendation_result(value | changes)


def test_tokenizer_nfc_casefold_han_boundaries():
    from relaymind.selection.asset_lexical import tokenize

    assert tokenize("HELLO Café Cafe\u0301 世界你好 中 abc_123 北京!") == frozenset(
        {"hello", "café", "世界", "界你", "你好", "中", "abc", "123", "北京"}
    )
    assert tokenize("世，界") == frozenset({"世", "界"})


def test_lexical_projection_only_stable_ties_cap_and_no_confidence():
    from relaymind.selection.asset_lexical import LexicalAssetSelector, build_lexical_index

    a = api()
    records = [
        record(asset_id=f"opaque:{i:02}", description="世界 greeting") for i in reversed(range(12))
    ]

    async def run():
        context = a.SelectorContext(lambda: 0, lambda: None)
        index = await build_lexical_index(
            1, tuple(validate_asset_record(r) for r in records), context=context
        )
        selector = LexicalAssetSelector(index)
        req = a.parse_asset_request(request(catalog=records, task_text="世界 greeting"))
        data = a.parse_asset_result(await selector.select(req, context)).data
        assert [c["asset_id"] for c in data["candidates"]] == [f"opaque:{i:02}" for i in range(10)]
        assert data["decision"] == "choice_required"
        assert data["candidates"][0]["score"] == 2
        assert data["candidates"][0]["evidence_refs"] == ["/description"]
        wire_req = a.parse_asset_request(json.loads(json.dumps(req.to_json_object())))
        assert await selector.select(wire_req, context) == await selector.select(req, context)
        no_match = a.parse_asset_request(request(catalog=records, task_text="secretbodyterm"))
        assert (await selector.select(no_match, context))["outcome"] == "no_match"
        stale = a.parse_asset_request(request(catalog=records, catalog_revision=2))
        assert (await selector.select(stale, context))["outcome"] == "not_ready"
        records[0]["stale"] = True
        changed = a.parse_asset_request(request(catalog=records))
        assert (await selector.select(changed, context))["outcome"] == "not_ready"

    asyncio.run(run())


def test_offer_cache_coherent_variants_and_no_dispatch_catalog_work(monkeypatch):
    from relaymind.selection.asset_lexical import LexicalAssetSelector, OfferCache

    a = api()

    async def run():
        cache = OfferCache()
        context = a.SelectorContext(lambda: 0, lambda: None)
        await cache.publish(
            snapshot(),
            scope=request()["scope"],
            permissions=["assets.read"],
            policy=lambda r: True,
            catalog_revision=1,
            context=context,
        )
        offer = cache.get("discovery", ["skill", "mcp_tool"], catalog_revision=1)
        assert (
            offer.catalog_bytes
            == json.dumps(
                request()["catalog"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        )
        assert not cache.get("discovery", ["mcp_tool"], catalog_revision=1).index.records

        def forbidden(*args, **kwargs):
            pytest.fail("dispatch revalidated/reserialized the catalog")

        def envelope_only(value):
            # Only the small envelope may be serialized; the catalog bytes are reused.
            if isinstance(value, list) or "catalog" in value:
                forbidden()
            return canonical_json(value)

        monkeypatch.setattr("relaymind.selection.asset_contract.validate_asset_record", forbidden)
        monkeypatch.setattr("relaymind.selection.asset_contract.canonical_json", envelope_only)
        for _ in range(2):
            req = offer.request(
                {k: v for k, v in request().items() if k != "catalog"}, context=context
            )
            assert req.catalog_bytes is offer.catalog_bytes
            assert (await LexicalAssetSelector(offer.index).select(req, context))[
                "outcome"
            ] == "ranked"
            assert json.loads(req.to_bytes(context)) == request()
        cache.invalidate()
        with pytest.raises(a.AttemptFailure):
            cache.get("discovery", ["skill"], catalog_revision=1)
        monkeypatch.undo()
        await cache.publish(
            snapshot([record(stale=True)], revision=2),
            scope=request()["scope"],
            permissions=["assets.read"],
            policy=lambda r: True,
            catalog_revision=2,
            context=context,
        )
        new = cache.get("discovery", ["skill", "mcp_tool"], catalog_revision=2)
        assert new.catalog_bytes != offer.catalog_bytes
        with pytest.raises(a.AttemptFailure):
            cache.get("discovery", ["skill"], catalog_revision=1)

    asyncio.run(run())


def test_lexical_batches_poll_budget_and_cancel_publication():
    from relaymind.selection.asset_lexical import build_lexical_index

    a = api()
    checks = 0

    def check():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise asyncio.CancelledError

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await build_lexical_index(
                1,
                [validate_asset_record(record(asset_id=str(i))) for i in range(100)],
                context=a.SelectorContext(lambda: 0, check),
            )

    asyncio.run(run())


class FakeClock:
    def __init__(self):
        self.seconds = 0.0

    def monotonic(self):
        return self.seconds

    def utc_now(self):
        return datetime(2026, 10, 29, tzinfo=UTC)


class Primary:
    name, version, mechanism = "custom", "1", "custom_rule"

    def __init__(self, value=None, callback=None):
        self.value = result() if value is None else value
        self.callback = callback
        self.deadlines = []

    async def select(self, req, context):
        self.deadlines.append(req.data["deadline_ms"])
        if self.callback:
            self.callback()
        return self.value


async def coordinate(
    primary=None,
    *,
    raw=None,
    clock=None,
    budget=None,
    current=None,
    policy=None,
    fallback=True,
    observe=None,
):
    a = api()
    from relaymind.selection.asset_chain import SelectionBudget, select_assets
    from relaymind.selection.asset_lexical import LexicalAssetSelector, build_lexical_index

    clock = clock or FakeClock()
    budget = budget or SelectionBudget(total_ms=500, clock=clock)
    req = a.parse_asset_request(request() if raw is None else raw)
    index = await build_lexical_index(
        req.data["catalog_revision"],
        [validate_asset_record(r) for r in req.data["catalog"]],
        context=a.SelectorContext(clock.monotonic, lambda: None),
    )
    floor = LexicalAssetSelector(index)
    got = await select_assets(
        req,
        primary=primary or Primary(),
        lexical_floor=floor,
        budget=budget,
        primary_ms=150,
        fallback_enabled=fallback,
        current_catalog=current or (lambda: snapshot()),
        policy=policy or (lambda r, req: True),
        trusted_scope={"user_id": "u", "workspace_id": "w"},
        trusted_permissions=["assets.read"],
        observe=observe,
    )
    return got.to_json_object(), budget


def test_coordinator_ranked_no_match_empty_and_zero_budget():
    from relaymind.selection.asset_chain import RecommendationFailure, SelectionBudget

    got, _ = asyncio.run(coordinate())
    assert got["primary_attempt"] == 0 and got["attempts"][0]["attempt"] == 1
    assert got["final_candidates"] == [candidate()]
    got, _ = asyncio.run(
        coordinate(Primary(result(outcome="no_match", decision="no_match", candidates=[])))
    )
    assert got["decision"] == "no_match" and len(got["attempts"]) == 1
    p = Primary()
    got, _ = asyncio.run(coordinate(p, raw=request(catalog=[]), current=lambda: snapshot([])))
    assert got["decision"] == "empty_catalog" and got["primary_attempt"] is None and not p.deadlines
    clock = FakeClock()
    budget = SelectionBudget(total_ms=500, clock=clock)
    clock.seconds = 0.5
    with pytest.raises(RecommendationFailure) as caught:
        asyncio.run(coordinate(p, budget=budget, clock=clock))
    assert caught.value.code == "deadline_exceeded" and not p.deadlines


@pytest.mark.parametrize(
    "changes,outcome,reason",
    [
        (
            {"outcome": "unavailable", "decision": None, "candidates": []},
            "unavailable",
            "selector_unavailable",
        ),
        (
            {"outcome": "not_ready", "decision": None, "candidates": []},
            "not_ready",
            "selector_not_ready",
        ),
        ({"catalog_revision": 2}, "stale", "stale_result"),
        ({"selector": {"name": "wrong", "version": "1"}}, "invalid", "invalid_result"),
        ({"candidates": [candidate(content_hash="b" * 64)]}, "invalid", "invalid_result"),
        ({"elapsed_ms": float("nan")}, "invalid", "invalid_result"),
    ],
)
def test_only_failed_primary_falls_back_with_retained_result(changes, outcome, reason):
    got, _ = asyncio.run(coordinate(Primary(result(**changes))))
    assert got["primary_attempt"] == 0 and len(got["attempts"]) == 2
    first, floor = got["attempts"]
    assert (first["outcome"], first["reason"]) == (outcome, reason)
    assert (first["result"] is None) == ("elapsed_ms" in changes)
    assert floor["selector"] == {"name": "lexical", "version": "1"}
    assert (
        floor["mechanism"] == "gateway_lexical_fallback"
        and floor["result"]["mechanism"] == "lexical_overlap"
    )
    assert floor["cost_observations"][0] == {
        "provenance": "observed",
        "value": {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "cpu_ms": None,
            "peak_bytes": None,
        },
    }
    assert first["cost_observations"] == []
    assert got["fallback_reason"] == reason and got["decision"] == "choice_required"


def test_primary_cap_fallback_remaining_total_and_total_exhaustion():
    from relaymind.selection.asset_chain import RecommendationFailure

    clock = FakeClock()
    primary = Primary(callback=lambda: setattr(clock, "seconds", 0.160))
    got, _ = asyncio.run(coordinate(primary, clock=clock))
    assert primary.deadlines == [150]
    assert got["attempts"][0]["outcome"] == "timeout"
    assert got["observed_ms"] == 160
    clock = FakeClock()
    with pytest.raises(RecommendationFailure) as caught:
        asyncio.run(
            coordinate(Primary(callback=lambda: setattr(clock, "seconds", 0.5)), clock=clock)
        )
    assert caught.value.code == "deadline_exceeded"
    assert len(caught.value.attempts) == 1


def test_cancel_before_during_result_and_commit_fence_wins():
    from relaymind.selection.asset_chain import RecommendationFailure, SelectionBudget

    async def run():
        clock = FakeClock()
        budget = SelectionBudget(clock=clock)
        budget.cancel()
        primary = Primary()
        with pytest.raises(RecommendationFailure) as caught:
            await coordinate(primary, budget=budget, clock=clock)
        assert caught.value.code == "cancelled" and not primary.deadlines
        budget = SelectionBudget(clock=clock)
        primary = Primary(callback=budget.cancel)
        with pytest.raises(RecommendationFailure) as caught:
            await coordinate(primary, budget=budget, clock=clock)
        assert caught.value.code == "cancelled" and len(caught.value.attempts) == 1
        budget = SelectionBudget(clock=clock)
        _, budget = await coordinate(budget=budget, clock=clock)
        budget.cancel()
        with pytest.raises(RecommendationFailure) as caught:
            budget.check()
        assert caught.value.code == "cancelled"

    asyncio.run(run())


def test_cancel_resistant_late_work_cannot_publish_or_block():
    from relaymind.selection.asset_chain import RecommendationFailure, SelectionBudget

    async def run():
        budget = SelectionBudget(clock=helpers.FakeClock())
        entered, release = asyncio.Event(), asyncio.Event()
        events = []

        class Resistant(Primary):
            async def select(self, req, ctx):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                return result()

        task = asyncio.create_task(coordinate(Resistant(), budget=budget, observe=events.append))
        await entered.wait()
        budget.cancel()
        with pytest.raises(RecommendationFailure) as caught:
            await asyncio.wait_for(task, 0.2)
        assert caught.value.code == "cancelled"
        count = len(events)
        release.set()
        await budget.drain(0.2)
        assert len(events) == count

    asyncio.run(run())


@pytest.mark.parametrize(
    "records,revision,outcome",
    [
        # Spec 0.52: any revision change makes a computing result stale.
        ([record(content_hash="a" * 64)], 2, "stale"),
        ([], 2, "stale"),
        ([record(identity={"package_id": "changed", "package_version": None})], 2, "stale"),
        ([record(), record(asset_id="unrelated")], 2, "stale"),
        # At the pinned revision admission still rechecks each candidate's exact content.
        ([record(content_hash="a" * 64)], 1, "stale"),
        ([], 1, "stale"),
        ([record(identity={"package_id": "changed", "package_version": None})], 1, "stale"),
        ([record(), record(asset_id="unrelated")], 1, "ranked"),
    ],
)
def test_current_catalog_requires_the_pinned_revision_and_exact_content(records, revision, outcome):
    """RM35/AC26, spec 0.52 section 4.6.2: revision equality, then exact candidate content."""
    from relaymind.selection.asset_chain import RecommendationFailure

    if outcome == "ranked":
        got, _ = asyncio.run(coordinate(current=lambda: snapshot(records, revision)))
        assert got["admission"]["rechecked_catalog_revision"] == revision
    else:
        with pytest.raises(RecommendationFailure) as caught:
            asyncio.run(coordinate(current=lambda: snapshot(records, revision)))
        assert caught.value.code == "recommendation_unavailable"
        assert all(attempt["outcome"] == outcome for attempt in caught.value.attempts)


def test_policy_and_observer_errors_do_not_hide_as_selector_fallback():
    from relaymind.selection.asset_chain import RecommendationFailure

    with pytest.raises(RecommendationFailure) as caught:
        asyncio.run(coordinate(policy=lambda r, req: False))
    assert all(attempt["outcome"] == "invalid" for attempt in caught.value.attempts)

    def broken(*args):
        raise RuntimeError("secret infrastructure")

    with pytest.raises(RecommendationFailure) as caught:
        asyncio.run(coordinate(observe=broken))
    assert caught.value.code == "internal_error" and "secret" not in str(caught.value)
    with pytest.raises(RecommendationFailure) as caught:
        asyncio.run(coordinate(policy=broken))
    assert caught.value.code == "internal_error"


def test_runtime_unavailable_and_no_lexical_self_fallback():
    from relaymind.selection.asset_chain import (
        RecommendationFailure,
        SelectionBudget,
        select_assets,
    )
    from relaymind.selection.asset_lexical import LexicalAssetSelector

    with pytest.raises(RecommendationFailure) as caught:
        asyncio.run(coordinate(raw=request(selection_scope="runtime")))
    assert caught.value.code == "recommendation_unavailable" and not caught.value.attempts

    async def run():
        with pytest.raises(RecommendationFailure) as caught:
            await select_assets(
                api().parse_asset_request(request()),
                primary=LexicalAssetSelector(None),
                lexical_floor=LexicalAssetSelector(None),
                budget=SelectionBudget(clock=helpers.FakeClock()),
                primary_ms=150,
                fallback_enabled=True,
                current_catalog=lambda: snapshot(),
                policy=lambda r, q: True,
                trusted_scope={"user_id": "u", "workspace_id": "w"},
                trusted_permissions=["assets.read"],
            )
        assert len(caught.value.attempts) == 1

    asyncio.run(run())


def test_cache_invalidation_fences_inflight_publication():
    from relaymind.selection.asset_lexical import OfferCache

    a = api()

    async def run():
        cache = OfferCache()
        checks = 0

        def check():
            nonlocal checks
            checks += 1
            if checks == 3:
                cache.invalidate()

        with pytest.raises(a.AttemptFailure):
            await cache.publish(
                snapshot(),
                scope=request()["scope"],
                permissions=[],
                catalog_revision=1,
                policy=lambda r: True,
                context=a.SelectorContext(lambda: 0, check),
            )
        with pytest.raises(a.AttemptFailure):
            cache.get("discovery", ["skill"], catalog_revision=1)

    asyncio.run(run())


def test_configured_factory_and_launcher_preserve_v1(tmp_path):
    from helpers import ScriptedAdapter
    from launcher_helpers import write_config
    from relaymind.assets.settings import RecommendationSettings
    from relaymind.launcher import build_app, load_config
    from relaymind.selection.asset_factory import build_asset_selector
    from relaymind.selection.asset_http import HttpAssetSelector
    from relaymind.selection.asset_lexical import LexicalAssetSelector

    assert isinstance(build_asset_selector(RecommendationSettings()), LexicalAssetSelector)
    http = build_asset_selector(
        RecommendationSettings(
            primary="http", name="lexical", version="1", url="http://localhost:8913/select"
        )
    )
    assert isinstance(http, HttpAssetSelector)
    with pytest.raises(ValueError):
        build_asset_selector(RecommendationSettings(primary="hybrid"))
    path = write_config(tmp_path)
    launched = build_app(load_config(path), local_adapter=ScriptedAdapter(turns=[]))
    assert launched.asset_selector is None
    launched.orchestrator.store.close()
    path.write_text(
        path.read_text() + '\n[recommendations]\nenabled=true\nuser_id="u"\nworkspace_id="w"\n'
        '\n[[assets.sources]]\nsource_id="example"\nkind="local_directory"\npath="ordinary"\n'
    )
    launched = build_app(load_config(path), local_adapter=ScriptedAdapter(turns=[]))
    assert isinstance(launched.asset_selector, LexicalAssetSelector)
    assert launched.asset_selector.index is None
    assert load_config(path).selection.chain == ("model_router",)
    launched.orchestrator.store.close()


def test_builtin_dependency_failure_is_unavailable():
    """RM35/AC26: a broken local index is unavailable without exposing its exception."""
    from relaymind.selection.asset_chain import (
        RecommendationFailure,
        SelectionBudget,
        select_assets,
    )
    from relaymind.selection.asset_lexical import LexicalAssetSelector

    class Broken(LexicalAssetSelector):
        async def select(self, req, ctx):
            raise OSError("secret index failure")

    async def run():
        with pytest.raises(RecommendationFailure) as caught:
            await select_assets(
                api().parse_asset_request(request()),
                primary=Broken(None),
                lexical_floor=None,
                budget=SelectionBudget(clock=helpers.FakeClock()),
                primary_ms=150,
                fallback_enabled=True,
                current_catalog=lambda: snapshot(),
                policy=lambda r, q: True,
                trusted_scope={"user_id": "u", "workspace_id": "w"},
                trusted_permissions=["assets.read"],
            )
        assert caught.value.code == "recommendation_unavailable"
        assert caught.value.attempts[0]["outcome"] == "unavailable"

    asyncio.run(run())


def test_cancellation_during_lexical_batches_stops_result():
    """RM35/AC26: bounded lexical batches observe cancellation before publishing."""
    from relaymind.selection.asset_lexical import LexicalAssetSelector, build_lexical_index

    async def run():
        records = [validate_asset_record(record(asset_id=str(i))) for i in range(100)]
        index = await build_lexical_index(
            1, records, context=api().SelectorContext(lambda: 0, lambda: None)
        )
        calls = 0

        def check():
            nonlocal calls
            calls += 1
            if calls == 27:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await LexicalAssetSelector(index).select(
                api().parse_asset_request(request(catalog=[r.to_json_object() for r in records])),
                api().SelectorContext(lambda: 0, check),
            )

    asyncio.run(run())


def test_observer_work_and_result_parsing_spend_original_total_budget(monkeypatch):
    """RM35/AC26: observer/persistence and parsing time cannot reset service time."""
    from relaymind.selection import asset_chain
    from relaymind.selection.asset_chain import RecommendationFailure

    clock = FakeClock()

    def observer(event):
        if isinstance(event, asset_chain.AttemptFinished):
            clock.seconds = 0.5

    with pytest.raises(RecommendationFailure) as caught:
        asyncio.run(coordinate(clock=clock, observe=observer))
    assert caught.value.code == "deadline_exceeded" and len(caught.value.attempts) == 1
    clock = FakeClock()
    original = asset_chain.parse_asset_result

    def slow(value):
        parsed = original(value)
        clock.seconds = 0.5
        return parsed

    monkeypatch.setattr(asset_chain, "parse_asset_result", slow)
    with pytest.raises(RecommendationFailure) as caught:
        asyncio.run(coordinate(clock=clock))
    assert caught.value.code == "deadline_exceeded"
    assert caught.value.attempts[0]["result"] is not None
    assert caught.value.attempts[0]["outcome"] == "timeout"


def test_parent052_revision_guard_includes_no_match_and_empty():
    """RM35/AC26: ANY new published revision invalidates the computing offer."""
    from relaymind.selection.asset_chain import RecommendationFailure

    for value, raw in (
        (result(outcome="no_match", decision="no_match", candidates=[]), request()),
        (result(), request(catalog=[])),
    ):
        with pytest.raises(RecommendationFailure) as caught:
            asyncio.run(coordinate(Primary(value), raw=raw, current=lambda: snapshot(revision=2)))
        assert caught.value.code == "recommendation_unavailable"
        assert all(a["outcome"] == "stale" for a in caught.value.attempts)


def test_same_revision_changed_policy_or_availability_cannot_publish():
    """RM34/RM35/AC25/AC26: no different effective offer under one revision."""
    from relaymind.selection.asset_lexical import OfferCache

    async def run():
        ctx = api().SelectorContext(lambda: 0, lambda: None)
        for alternate, policy in (
            (snapshot(), lambda r: False),
            (snapshot([record(availability="unavailable")]), lambda r: True),
        ):
            cache = OfferCache()
            await cache.publish(
                snapshot(),
                scope=request()["scope"],
                permissions=[],
                policy=lambda r: True,
                context=ctx,
            )
            with pytest.raises(api().AttemptFailure):
                await cache.publish(
                    alternate, scope=request()["scope"], permissions=[], policy=policy, context=ctx
                )
            with pytest.raises(api().AttemptFailure):
                cache.get("discovery", ["skill"], catalog_revision=1)

    asyncio.run(run())


def test_publication_serializes_record_once_not_once_per_kind(monkeypatch):
    """RM35/AC26: all kind variants are byte assemblies, with set semantics."""
    from relaymind.selection import asset_lexical

    original = asset_lexical.canonical_json
    serialized = []

    def count(value):
        assert isinstance(value, dict) and value.get("schema_version") == 1
        serialized.append(value["asset_id"])
        return original(value)

    monkeypatch.setattr(asset_lexical, "canonical_json", count)

    async def run():
        cache = asset_lexical.OfferCache()
        await cache.publish(
            snapshot(),
            scope=request()["scope"],
            permissions=[],
            policy=lambda r: True,
            context=api().SelectorContext(lambda: 0, lambda: None),
        )
        one = cache.get("discovery", ["skill", "mcp_tool"], catalog_revision=1)
        two = cache.get("discovery", ["mcp_tool", "skill"], catalog_revision=1)
        assert one is two and serialized == ["example:hello"]

    asyncio.run(run())


def test_coordinator_rejects_untrusted_scope_or_permissions_before_dispatch():
    """RM35/AC26: wire labels never establish scope or policy grants."""
    from relaymind.selection.asset_chain import (
        RecommendationFailure,
        SelectionBudget,
        select_assets,
    )

    async def run(scope, permissions, empty=False, abstain=False):
        primary = (
            Primary(result(outcome="no_match", decision="no_match", candidates=[]))
            if abstain
            else Primary()
        )
        with pytest.raises(RecommendationFailure) as caught:
            await select_assets(
                api().parse_asset_request(request(catalog=[] if empty else [record()])),
                primary=primary,
                lexical_floor=None,
                budget=SelectionBudget(clock=helpers.FakeClock()),
                primary_ms=150,
                fallback_enabled=False,
                current_catalog=lambda: snapshot(),
                policy=lambda r, q: True,
                trusted_scope=scope,
                trusted_permissions=permissions,
            )
        assert caught.value.code == "invalid_request" and not primary.deadlines

    asyncio.run(run({"user_id": "foreign", "workspace_id": "w"}, ["assets.read"]))
    asyncio.run(run(request()["scope"], ["new.grant"]))
    asyncio.run(run({"user_id": "foreign", "workspace_id": "w"}, ["assets.read"], empty=True))
    asyncio.run(run(request()["scope"], ["new.grant"], abstain=True))


def test_mcp_lexical_fields_and_exact_schema_admission():
    """RM35/AC26: genuine MCP identity fields match; schema/content remains exact."""
    from relaymind.canonical import canonical_digest
    from relaymind.selection.asset_lexical import LexicalAssetSelector, build_lexical_index

    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    tool = record(
        asset_id="mcp:opaque",
        kind="mcp_tool",
        description="Different description",
        identity={
            "server_id": "Weather",
            "tool_name": "forecast",
            "exposed_name": "weather_forecast",
            "input_schema": schema,
            "schema_hash": canonical_digest(schema),
        },
    )

    async def run():
        context = api().SelectorContext(lambda: 0, lambda: None)
        req = api().parse_asset_request(request(catalog=[tool], task_text="weather forecast"))
        index = await build_lexical_index(1, [validate_asset_record(tool)], context=context)
        got = await LexicalAssetSelector(index).select(req, context)
        assert got["candidates"][0]["score"] == 2
        assert got["candidates"][0]["evidence_refs"] == [
            "/identity/server_id",
            "/identity/tool_name",
            "/identity/exposed_name",
        ]

    asyncio.run(run())


def test_http_builtin_mechanism_spoof_is_rejected_even_colliding_display_name():
    """RM35/AC26: mechanism ownership follows implementation, not display identity."""

    class HttpDouble(Primary):
        name = "lexical"
        transport = "http"

    got, _ = asyncio.run(
        coordinate(
            HttpDouble(
                result(selector={"name": "lexical", "version": "1"}, mechanism="lexical_overlap")
            )
        )
    )
    assert got["attempts"][0]["outcome"] == "invalid"
    assert len(got["attempts"]) == 2
    got, _ = asyncio.run(
        coordinate(HttpDouble(result(selector={"name": "lexical", "version": "1"})))
    )
    assert len(got["attempts"]) == 1


def test_corrupt_current_catalog_is_unavailable_with_retained_attempt():
    """RM35/AC26: corrupt catalog never becomes empty or an inference failure."""
    from relaymind.selection.asset_chain import RecommendationFailure
    from relaymind.store.asset_catalog import CatalogUnavailable

    def corrupt():
        raise CatalogUnavailable("catalog_corrupt")

    with pytest.raises(RecommendationFailure) as caught:
        asyncio.run(coordinate(current=corrupt))
    assert caught.value.code == "recommendation_unavailable"
    assert len(caught.value.attempts) == 1
    assert caught.value.attempts[0]["result"] is not None


def test_precommit_fence_can_close_budget_before_late_attempt_event():
    """RM35/AC26: terminal owner fences forbid subsequent dispatch/ready."""
    from relaymind.selection.asset_chain import (
        AttemptStarted,
        RecommendationFailure,
        SelectionBudget,
    )

    budget = SelectionBudget(clock=helpers.FakeClock())
    primary = Primary()

    def observe(event):
        if isinstance(event, AttemptStarted):
            budget.fence_terminal()

    with pytest.raises(RecommendationFailure) as caught:
        asyncio.run(coordinate(primary, budget=budget, observe=observe))
    assert caught.value.code == "cancelled" and not primary.deadlines


def test_http_finished_attempt_uses_admitted_algorithm_mechanism():
    """RM35/AC26: HTTP transport is not itself the selector's algorithm identity."""
    import httpx

    from relaymind.selection.asset_http import HttpAssetSelector

    # MockTransport response must be a streaming raw response for production's
    # bounded decoder, rather than httpx's pre-consumed json convenience body.
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps(result()).encode()

    async def response(req):
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Body())

    primary = HttpAssetSelector(
        name="custom",
        version="1",
        url="http://localhost:8913/select",
        transport_factory=lambda: httpx.MockTransport(response),
    )
    got, _ = asyncio.run(coordinate(primary))
    assert got["attempts"][0]["mechanism"] == "custom_rule"


def test_failed_new_preparation_retains_last_compatible_cache():
    """RM34/RM35 AC25/AC26: a failed rebuild does not erase a compatible offer."""
    from relaymind.selection.asset_lexical import OfferCache

    async def run():
        cache = OfferCache(current_revision=lambda: 1)
        context = api().SelectorContext(lambda: 0, lambda: None)
        await cache.publish(
            snapshot(),
            scope=request()["scope"],
            permissions=[],
            policy=lambda r: True,
            context=context,
        )
        original = cache.get("discovery", ["skill"], catalog_revision=1)

        def exhausted():
            raise TimeoutError

        with pytest.raises(TimeoutError):
            await cache.publish(
                snapshot(revision=2),
                scope=request()["scope"],
                permissions=[],
                policy=lambda r: True,
                context=api().SelectorContext(lambda: 0, exhausted),
            )
        assert cache.get("discovery", ["skill"], catalog_revision=1) is original

    asyncio.run(run())


@pytest.mark.parametrize(
    "outcome,where", [("ranked", "current"), ("no_match", "current"), ("ranked", "policy")]
)
@pytest.mark.parametrize("fallback", [False, True])
def test_primary_cap_after_admission_discards_success(outcome, where, fallback):
    """RM35/AC26 C1: admitted intermediate output cannot survive a failed attempt fence."""
    from relaymind.selection.asset_chain import RecommendationFailure

    clock = FakeClock()
    value = (
        result()
        if outcome == "ranked"
        else result(outcome="no_match", decision="no_match", candidates=[])
    )

    def current():
        if where == "current":
            clock.seconds = 0.160
        return snapshot()

    def policy(record, req):
        if where == "policy":
            clock.seconds = 0.160
        return True

    if not fallback:
        with pytest.raises(RecommendationFailure) as caught:
            asyncio.run(
                coordinate(
                    Primary(value), clock=clock, current=current, policy=policy, fallback=False
                )
            )
        assert caught.value.code == "recommendation_unavailable"
        assert len(caught.value.attempts) == 1
        attempt = caught.value.attempts[0]
    else:
        got, _ = asyncio.run(
            coordinate(Primary(value), clock=clock, current=current, policy=policy, fallback=True)
        )
        assert len(got["attempts"]) == 2
        attempt, floor = got["attempts"]
        assert got["primary_attempt"] == 0
        assert got["fallback_reason"] == "deadline_exceeded"
        assert got["decision"] == "choice_required"
        assert floor["mechanism"] == "gateway_lexical_fallback"
        assert floor["result"]["catalog_revision"] == 1
        assert floor["result"]["recommendation_id"] == request()["recommendation_id"]
        assert [c["asset_id"] for c in floor["result"]["candidates"]] == [record()["asset_id"]]
        assert got["base_candidates"] == floor["result"]["candidates"]
    assert attempt["outcome"] == "timeout" and attempt["reason"] == "deadline_exceeded"
    assert attempt["result"] == value


def test_pointer_schema_rejection_has_external_watchdog():
    """RM35/AC26 C2: bounded untrusted pointers cannot stall synchronous schema work."""
    import subprocess
    import sys
    from pathlib import Path

    paths = [
        Path("src/relaymind/selection/schemas/selection_result.v2.json"),
        Path("src/relaymind/store/schemas/recommendation_result.v1.json"),
    ]
    patterns = []

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence_refs":
                    patterns.append(item["items"]["pattern"])
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for path in paths:
        collect(json.loads(path.read_text()))
    # Every evidence_refs pattern, inline or behind a shared $defs entry.
    assert patterns
    script = """
import json, sys
from jsonschema import Draft202012Validator
patterns = json.loads(sys.stdin.read())
for pattern in patterns:
    validator = Draft202012Validator({"type": "string", "pattern": pattern})
    assert not validator.is_valid("/" * 28 + "~2")
    for pointer in ["", "/", "/~0", "/~1", "/~01", "/items/0", "/" * 256]:
        assert validator.is_valid(pointer)
print("validated", len(patterns))
"""
    worker = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = worker.communicate(json.dumps(patterns), timeout=2)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.communicate()
        pytest.fail("bounded JSON Pointer schema validation exceeded external watchdog")
    assert worker.returncode == 0, stderr
    assert stdout.strip() == f"validated {len(patterns)}"


def test_pointer_empty_root_escaped_and_array_semantics():
    """RM35/AC26 C2: syntax and offered-field resolution remain separate boundaries."""
    from relaymind.canonical import canonical_digest
    from relaymind.selection.asset_admission import validate_against_offer

    a = api()
    schema = {
        "type": "object",
        "properties": {"a/b~c": {"type": "string"}, "~1": {"type": "string"}},
        "additionalProperties": False,
    }
    tool = record(
        kind="mcp_tool",
        identity={
            "server_id": "server",
            "tool_name": "tool",
            "exposed_name": "exposed",
            "input_schema": schema,
            "schema_hash": canonical_digest(schema),
        },
    )
    req = a.parse_asset_request(request(catalog=[tool]))
    for pointer in [
        "",
        "/description",
        "/language_tags/0",
        "/identity/input_schema/properties/a~1b~0c",
        "/identity/input_schema/properties/~01",
    ]:
        parsed = a.parse_asset_result(
            result(candidates=[candidate(kind="mcp_tool", evidence_refs=[pointer])])
        )
        validate_against_offer(parsed, req, a.SelectorIdentity("custom", "1"))
    for pointer in ["/", "/language_tags/00", "/language_tags/-", "/language_tags/99"]:
        parsed = a.parse_asset_result(
            result(candidates=[candidate(kind="mcp_tool", evidence_refs=[pointer])])
        )
        with pytest.raises(a.AttemptFailure):
            validate_against_offer(parsed, req, a.SelectorIdentity("custom", "1"))


class SlowPrimary(Primary):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def select(self, req, context):
        self.started.set()
        await asyncio.sleep(10)


def test_task_cancellation_propagates_as_cancelled_error():
    """RM35/AC26: a caller's task cancel is not turned into a domain failure."""

    async def cancel_task():
        primary = SlowPrimary()
        task = asyncio.create_task(coordinate(primary))
        await primary.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def timeout_scope():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await coordinate(SlowPrimary())

    asyncio.run(cancel_task())
    asyncio.run(timeout_scope())


def test_request_own_cancel_signal_stays_a_recommendation_failure():
    """RM35/AC26: the budget's cancel is a lifecycle failure, not a task cancel."""
    from relaymind.selection.asset_chain import RecommendationFailure, SelectionBudget

    async def run():
        primary = SlowPrimary()
        budget = SelectionBudget(clock=helpers.FakeClock())
        task = asyncio.create_task(coordinate(primary, budget=budget))
        await primary.started.wait()
        budget.cancel()
        with pytest.raises(RecommendationFailure) as caught:
            await task
        assert caught.value.code == "cancelled"

    asyncio.run(run())


def test_exhausted_attempt_time_is_a_timeout_never_internal_error():
    """RM35/AC26: remaining time at or below zero maps to an attempt timeout."""
    from relaymind.selection.asset_chain import _attempt_request

    clock = FakeClock()
    req = api().parse_asset_request(request())
    clock.seconds = 1.0
    for deadline in (1.0, 0.999):
        with pytest.raises(api().AttemptFailure) as caught:
            _attempt_request(req, deadline, clock)
        assert (caught.value.outcome, caught.value.reason) == ("timeout", "deadline_exceeded")
    assert _attempt_request(req, 1.1, clock).data["deadline_ms"] == pytest.approx(100)


def test_offer_permissions_compare_as_a_set():
    """RM35/AC26: permission order is not identity at publication or dispatch."""
    from relaymind.selection.asset_lexical import OfferCache

    a = api()

    async def run():
        cache = OfferCache()
        context = a.SelectorContext(lambda: 0, lambda: None)
        await cache.publish(
            snapshot(),
            scope=request()["scope"],
            permissions=["b.read", "a.read"],
            policy=lambda r: True,
            catalog_revision=1,
            context=context,
        )
        offer = cache.get("discovery", ["skill", "mcp_tool"], catalog_revision=1)
        assert offer.permissions == ("a.read", "b.read")
        envelope = {k: v for k, v in request().items() if k != "catalog"}
        for order in (["a.read", "b.read"], ["b.read", "a.read"]):
            assert offer.request({**envelope, "permissions": order}, context=context)
        with pytest.raises(a.AssetSelectionError):
            offer.request({**envelope, "permissions": ["a.read"]}, context=context)

    asyncio.run(run())


def test_published_schema_copies_stay_identical():
    """RM35/AC26: embedded definitions cannot drift from their source schemas."""
    from relaymind.schema_util import load_packaged_schema

    a = api()
    result_schema = {k: v for k, v in a.RESULT_SCHEMA.items() if k != "$schema"}
    assert a.RECOMMENDATION_SCHEMA["$defs"]["SelectionResultV2"] == result_schema
    record_schema = load_packaged_schema("relaymind.store", "asset_record.v1.json")
    assert a.REQUEST_SCHEMA["properties"]["catalog"]["items"] == record_schema
    properties = a.RECOMMENDATION_SCHEMA["properties"]
    for name in ("base_candidates", "final_candidates"):
        assert properties[name]["items"] == {"$ref": "#/$defs/CandidateV2"}


@pytest.mark.parametrize(
    "path,value",
    [
        (("base_candidates", 0, "score"), True),
        (("final_candidates", 0, "kind"), "tool"),
        (("attempts", 1, "cost_observations", 0, "value", "calls"), 1.0),
        (("attempts", 1, "cost_observations", 0, "value", "cpu_ms"), "fast"),
    ],
)
def test_referenced_schema_parts_still_reject_invalid_values(path, value):
    """RM35/AC26: moving to $ref keeps candidate and cost checks identical."""
    raw, _ = asyncio.run(coordinate(Primary(result(catalog_revision=2))))
    api().parse_recommendation_result(copy.deepcopy(raw))
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(api().AssetSelectionError):
        api().parse_recommendation_result(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"task_text": "   "},
        {"task_text": "x" * 8193},
        {"conversation": [{"role": "user", "text": "\ud800"}]},
    ],
)
def test_cached_offer_request_reports_bad_queries_as_invalid_request(changes):
    """RM35/AC26: the cached-offer entry point classifies caller input like parse_asset_request."""
    from relaymind.selection.asset_lexical import OfferCache

    a = api()

    async def run():
        cache = OfferCache()
        context = a.SelectorContext(lambda: 0, lambda: None)
        await cache.publish(
            snapshot(),
            scope=request()["scope"],
            permissions=request()["permissions"],
            policy=lambda r: True,
            catalog_revision=1,
            context=context,
        )
        offer = cache.get("discovery", ["skill", "mcp_tool"], catalog_revision=1)
        envelope = {k: v for k, v in request().items() if k != "catalog"}
        with pytest.raises(a.AssetSelectionError) as caught:
            offer.request({**envelope, **changes}, context=context)
        assert str(caught.value) == "invalid_request"

    asyncio.run(run())
