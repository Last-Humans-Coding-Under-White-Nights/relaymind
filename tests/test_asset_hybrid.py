"""RM38/RM41 AC29/AC32: deterministic full-offer fusion and compatible vectors."""

import json

from helpers import FakeClock
from test_asset_catalog import record
from test_asset_selection import request
from test_embedding_config import hybrid
from test_embedding_manifest import fixture


def api():
    from relaymind.selection import asset_hybrid

    return asset_hybrid


def setup(tmp_path, *, minimum=0.5):
    from relaymind.assets.embedding_config import load_hybrid_config
    from relaymind.assets.embedding_manifest import load_embedding_manifest

    path, _ = fixture(tmp_path)
    manifest = load_embedding_manifest(path)
    value = hybrid(manifest)
    value["fusion"]["min_cosine"] = minimum
    value["confidence"] = dict(min_score=0, min_margin=0)
    path = tmp_path / "hybrid.json"
    path.write_text(json.dumps(value))
    config = load_hybrid_config(path, manifest)
    return config, api().HybridIndex(config, {"user_id": "u", "workspace_id": "w"})


def test_metadata_same_content_and_scope_cannot_reuse_an_old_vector(tmp_path):
    config, index = setup(tmp_path)
    original = record(description="a\nb", identity={"package_id": "c", "package_version": None})
    changed = record(description="a", identity={"package_id": "b\nc", "package_version": None})
    index.publish_batch([original], [(1.0, 0.0)])
    assert index.compatible(original) == (1.0, 0.0)
    assert index.compatible(changed) is None
    foreign = api().HybridIndex(config, {"user_id": "other", "workspace_id": "w"})
    assert foreign.key(original) != index.key(original)
    assert (
        api().rank_hybrid(request(catalog=[changed]), config, index, (1.0, 0.0))["outcome"]
        == "not_ready"
    )
    index.reconcile([changed])
    assert index.compatible(original) is None


def test_partial_coverage_requires_choice_and_zero_coverage_reports_not_ready(tmp_path):
    config, index = setup(tmp_path)
    a = record(
        asset_id="a", description="中文", identity={"package_id": "甲", "package_version": None}
    )
    b = record(
        asset_id="b", description="English", identity={"package_id": "B", "package_version": None}
    )
    req = request(task_text="任务", catalog=[a, b])
    assert api().rank_hybrid(req, config, index, (1.0, 0.0))["outcome"] == "not_ready"
    index.publish_batch([a], [(1.0, 0.0)])
    partial = api().rank_hybrid(req, config, index, (1.0, 0.0))
    assert partial["decision"] == "choice_required"
    index.publish_batch([b], [(0.0, 1.0)])
    assert api().rank_hybrid(req, config, index, (1.0, 0.0))["decision"] == "confident"
    index.reconcile([])
    assert api().rank_hybrid(req, config, index, (1.0, 0.0))["outcome"] == "not_ready"


def test_exact_semantic_and_lexical_evidence_membership(tmp_path):
    config, index = setup(tmp_path)
    lexical = record(
        asset_id="lexical",
        description="search",
        identity={"package_id": "opaque", "package_version": None},
    )
    semantic = record(
        asset_id="semantic",
        description="unrelated",
        identity={"package_id": "different", "package_version": None},
    )
    index.publish_batch([lexical, semantic], [(-1.0, 0.0), (1.0, 0.0)])
    result = api().rank_hybrid(
        request(task_text="search", catalog=[lexical, semantic]), config, index, (1.0, 0.0)
    )
    assert [c["asset_id"] for c in result["candidates"]] == ["semantic", "lexical"]
    a, b = result["candidates"]
    assert a["evidence_refs"] == ["/description", "/identity/package_id"]
    assert b["evidence_refs"] == ["/description"]
    assert a["score"] == 0.6 / 61 and b["score"] == 0.4 / 61
    assert a["score_kind"] == "weighted_rrf.v1"


def test_pool_is_ten_ties_are_asset_id_order_and_unsupported_union_is_no_match(tmp_path):
    config, index = setup(tmp_path)
    records = [
        record(
            asset_id=f"a{i:02}",
            description="other",
            identity={"package_id": "other", "package_version": None},
        )
        for i in reversed(range(12))
    ]
    index.publish_batch(records, [(1.0, 0.0)] * 12)
    result = api().rank_hybrid(
        request(task_text="none", catalog=records), config, index, (1.0, 0.0)
    )
    assert [c["asset_id"] for c in result["candidates"]] == [f"a{i:02}" for i in range(10)]
    result = api().rank_hybrid(
        request(task_text="none", catalog=records), config, index, (-1.0, 0.0)
    )
    assert result["outcome"] == "no_match" and result["candidates"] == []


def test_cancelled_batch_cannot_publish_partial_vectors(tmp_path):
    import pytest

    config, index = setup(tmp_path)
    records = [record(asset_id="a"), record(asset_id="b")]
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError):
        index.publish_batch(records, [(1.0, 0.0), (0.0, 1.0)], check=check)
    assert index.compatible(records[0]) is None


def test_builtin_selector_embeds_only_after_compatible_index_and_preserves_failures(tmp_path):
    import asyncio
    import time

    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.selection.asset_contract import (
        SelectorContext,
        parse_asset_request,
        parse_asset_result,
    )
    from test_embedding_backend import NativeDouble

    config, index = setup(tmp_path)
    engine = EmbeddingEngine(config.manifest, NativeDouble())
    selector = api().HybridAssetSelector(config, index, engine)
    req = parse_asset_request(request())
    context = SelectorContext(time.monotonic, lambda: None)
    initial = asyncio.run(selector.select(req, context))
    assert initial["outcome"] == "not_ready"
    assert selector.observations == ()
    index.publish_batch(req.data["catalog"], [(0.6, 0.8)])
    result = asyncio.run(selector.select(req, context))
    assert (
        parse_asset_result(result).data["candidates"][0]["asset_id"]
        == req.data["catalog"][0]["asset_id"]
    )
    assert selector.observations[0]["outcome"] == "succeeded"
    assert selector.observations[0]["cost_observations"][0]["value"]["calls"] == 1


def test_query_pins_one_vector_publication_across_awaited_inference(tmp_path):
    import asyncio
    import threading
    import time

    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.selection.asset_contract import SelectorContext, parse_asset_request
    from test_embedding_backend import NativeDouble

    class Blocking(NativeDouble):
        def __init__(self):
            self.started, self.release = threading.Event(), threading.Event()

        def infer(self, tokens, invoked):
            invoked()
            self.started.set()
            self.release.wait(1)
            return [[3, 4]]

    config, index = setup(tmp_path)
    records = [record(asset_id="a", description="other"), record(asset_id="b", description="other")]
    index.publish_batch(records, [(1.0, 0.0), (0.0, 1.0)])
    pinned = index.revision
    native = Blocking()
    selector = api().HybridAssetSelector(config, index, EmbeddingEngine(config.manifest, native))

    async def run():
        pending = asyncio.create_task(
            selector.select(
                parse_asset_request(
                    request(task_text="unmatched", catalog=records, deadline_ms=500)
                ),
                SelectorContext(time.monotonic, lambda: None),
            )
        )
        while not native.started.is_set():
            await asyncio.sleep(0.001)
        index.publish_batch(records, [(0.0, 1.0), (1.0, 0.0)])
        native.release.set()
        result = await pending
        assert result["index_revision"] == pinned
        assert result["candidates"][0]["asset_id"] == "b"

    asyncio.run(run())


def test_valid_large_offer_allows_peer_cancellation_during_real_selector_ranking(
    tmp_path, monkeypatch
):
    """RM03/RM35/RM41 AC32: a valid 200x7800-byte offer remains cooperative."""
    import asyncio

    import pytest

    from relaymind.assets.catalog import CatalogSnapshot, validate_asset_record
    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.selection.asset_chain import (
        RecommendationFailure,
        SelectionBudget,
        select_assets,
    )
    from relaymind.selection.asset_contract import parse_asset_request
    from test_embedding_backend import NativeDouble

    config, index = setup(tmp_path)
    records = [record(asset_id=f"asset{i:03}", description="token " * 1300) for i in range(200)]
    req = parse_asset_request(request(catalog=records, task_text="token"))
    assert len(req.to_json_object()["catalog"]) == 200
    for start in range(0, 200, 25):
        index.publish_batch(records[start : start + 25], [(0.6, 0.8)] * 25)
    catalog = CatalogSnapshot(
        1, 1, "2026-10-30T00:00:00Z", (), tuple(validate_asset_record(r) for r in records)
    )

    async def run():
        budget = SelectionBudget(500, clock=FakeClock())
        selector = api().HybridAssetSelector(
            config, index, EmbeddingEngine(config.manifest, NativeDouble())
        )
        started = asyncio.Event()
        scored = 0
        peer_count = None
        real_cosine = api().cosine

        def observe_cosine(*args):
            nonlocal scored
            scored += 1
            started.set()
            return real_cosine(*args)

        monkeypatch.setattr(api(), "cosine", observe_cosine)

        async def peer():
            nonlocal peer_count
            await started.wait()
            await asyncio.sleep(0)
            peer_count = scored
            budget.cancel()

        task = asyncio.create_task(peer())
        with pytest.raises(RecommendationFailure) as failure:
            await select_assets(
                req,
                primary=selector,
                lexical_floor=None,
                budget=budget,
                primary_ms=150,
                fallback_enabled=False,
                current_catalog=lambda: catalog,
                policy=lambda r, q: True,
                trusted_scope=req.data["scope"],
                trusted_permissions=req.data["permissions"],
            )
        await task
        assert 0 < peer_count < 200
        assert failure.value.code == "cancelled"
        stopped = scored
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert scored == stopped

    asyncio.run(run())


def test_zero_compatible_vector_scan_checks_after_yield_and_stops_at_deadline(
    tmp_path, monkeypatch
):
    """RM03/RM41: not_ready discovery cannot bypass cooperative budget checks."""
    import asyncio

    import pytest

    from relaymind.selection.asset_contract import (
        AttemptFailure,
        SelectorContext,
        parse_asset_request,
    )

    config, index = setup(tmp_path)
    req = parse_asset_request(
        request(catalog=[record(asset_id=str(i), description="token " * 1300) for i in range(200)])
    )
    checks = 0
    scanned = 0
    original = index.compatible

    def compatible(*args, **kwargs):
        nonlocal scanned
        scanned += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(index, "compatible", compatible)

    def check():
        if checks >= 3:
            raise AttemptFailure("timeout", "deadline_exceeded")

    async def yield_control():
        nonlocal checks
        checks += 1
        await asyncio.sleep(0)

    async def run():
        with pytest.raises(AttemptFailure, match="deadline_exceeded"):
            await (
                api()
                .HybridAssetSelector(config, index, None)
                .select(req, SelectorContext(lambda: 0, check, yield_control))
            )
        assert scanned < 200 and checks == 3

    asyncio.run(run())


def test_cooperative_cached_and_unprepared_paths_preserve_exact_rrf_and_evidence(tmp_path):
    """RM41 AC32: execution scheduling/cache reuse cannot alter numerical or evidence bytes."""
    import asyncio

    from relaymind.selection.asset_contract import SelectorContext, parse_asset_request

    config, index = setup(tmp_path)
    records = [
        record(
            asset_id="a", description="a\nb", identity={"package_id": "c", "package_version": None}
        ),
        record(
            asset_id="b",
            description="中文 search",
            identity={"package_id": "b", "package_version": None},
        ),
    ]
    index.publish_batch(records, [(1.0, 0.0), (0.0, 1.0)])
    req = parse_asset_request(request(catalog=records, task_text="search"))
    pinned = index.pin()
    uncached = (pinned[0], pinned[1], {})
    expected = api().rank_hybrid(req, config, index, (1.0, 0.0), publication=pinned)
    assert expected == api().rank_hybrid(req, config, index, (1.0, 0.0), publication=uncached)

    async def run():
        context = SelectorContext(lambda: 0, lambda: None)
        for publication in (pinned, uncached):
            result = await api()._cooperative(
                api()._rank_steps(req, config, index, (1.0, 0.0), publication=publication), context
            )
            assert result == expected

    asyncio.run(run())


def test_cooperative_rank_stops_after_its_original_deadline_without_extra_scoring(
    tmp_path, monkeypatch
):
    """RM03/RM41: a live scoring deadline halts the computation in its owning task."""
    import asyncio

    import pytest

    from relaymind.selection.asset_contract import (
        AttemptFailure,
        SelectorContext,
        parse_asset_request,
    )

    config, index = setup(tmp_path)
    records = [record(asset_id=str(i), description="token " * 1300) for i in range(200)]
    req = parse_asset_request(request(catalog=records, task_text="token"))
    for start in range(0, 200, 25):
        index.publish_batch(records[start : start + 25], [(1.0, 0.0)] * 25)
    scored = 0
    now = 0.0
    checks = 0
    original = api().cosine

    def cosine(*args):
        nonlocal scored
        scored += 1
        return original(*args)

    monkeypatch.setattr(api(), "cosine", cosine)

    def check():
        nonlocal checks
        checks += 1
        if now >= 0.15:
            raise AttemptFailure("timeout", "deadline_exceeded")

    async def checkpoint():
        nonlocal now
        if scored:
            now = 0.15
        await asyncio.sleep(0)

    async def run():
        with pytest.raises(AttemptFailure, match="deadline_exceeded"):
            await api()._cooperative(
                api()._rank_steps(req, config, index, (1.0, 0.0)),
                SelectorContext(lambda: now, check, checkpoint),
            )
        assert scored == 1 and now == 0.15 and checks > 200
        await asyncio.sleep(0)
        assert scored == 1

    asyncio.run(run())
