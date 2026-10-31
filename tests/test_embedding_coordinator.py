"""RM35/RM41/RM42 AC26/AC32/AC33: actual cost/stage evidence belongs to gateway."""

import asyncio

import pytest

from relaymind.selection.asset_chain import (
    AttemptFinished,
    RecommendationFailure,
    SelectionBudget,
    select_assets,
)
from relaymind.selection.asset_contract import parse_asset_request
from test_asset_hybrid import setup
from test_asset_selection import request, snapshot


def test_hybrid_query_observations_reach_ready_and_primary_attempt_once(tmp_path):
    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.selection.asset_hybrid import HybridAssetSelector
    from test_embedding_backend import NativeDouble

    config, index = setup(tmp_path)
    req = parse_asset_request(request())
    index.publish_batch(req.data["catalog"], [(0.6, 0.8)])
    primary = HybridAssetSelector(config, index, EmbeddingEngine(config.manifest, NativeDouble()))
    events = []
    result = asyncio.run(
        select_assets(
            req,
            primary=primary,
            lexical_floor=None,
            budget=SelectionBudget(500),
            primary_ms=150,
            fallback_enabled=False,
            current_catalog=snapshot,
            policy=lambda r, q: True,
            trusted_scope=req.data["scope"],
            trusted_permissions=req.data["permissions"],
            observe=events.append,
        )
    )
    result = result.to_json_object()
    assert len(result["embeddings"]) == 1
    assert result["embeddings"][0]["representation_revision"] == "asset_fields.v1"
    finished = next(e for e in events if isinstance(e, AttemptFinished))
    assert list(finished.embeddings) == result["embeddings"]
    assert (
        result["attempts"][0]["cost_observations"] == result["embeddings"][0]["cost_observations"]
    )
    assert result["attempts"][0]["cost_observations"][0]["value"]["calls"] == 1


def test_adaptation_failure_preserves_valid_primary_and_failed_similarity_evidence(tmp_path):
    from recommendation_helpers import BODY
    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.selection.adaptation_stage import AdaptationStage
    from relaymind.selection.asset_lexical import LexicalAssetSelector
    from test_asset_adaptation_stage import config, request_for
    from test_asset_adaptation_store import correct
    from test_asset_user_actions import action_client
    from test_embedding_backend import NativeDouble

    class Invalid(NativeDouble):
        def infer(self, tokens, invoked):
            invoked()
            return [[0, 0] for _ in tokens]

    async def run():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, _):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            await correct(client, parent, 0, "first")
            semantic = config(tmp_path)
            stage = AdaptationStage(
                app.state.feedback_store, semantic, EmbeddingEngine(semantic.manifest, Invalid())
            )
            req = request_for(service, catalog)
            offer = service.provider.pin(tuple(req.data["requested_kinds"]))
            primary = LexicalAssetSelector(offer.index)
            events = []
            with pytest.raises(RecommendationFailure) as caught:
                await select_assets(
                    req,
                    primary=primary,
                    lexical_floor=primary,
                    budget=service.new_budget(),
                    primary_ms=150,
                    fallback_enabled=True,
                    current_catalog=service.provider.current,
                    policy=lambda r, q: True,
                    trusted_scope=service.scope,
                    trusted_permissions=(),
                    observe=events.append,
                    adaptation=stage,
                )
            assert caught.value.code == "recommendation_unavailable"
            assert len(caught.value.attempts) == 1
            assert caught.value.attempts[0]["outcome"] == "ranked"
            assert caught.value.attempts[0]["result"]["outcome"] == "ranked"
            finished = next(e for e in events if isinstance(e, AttemptFinished))
            assert finished.embeddings[0]["representation_revision"] == "task_text.v1"
            assert finished.embeddings[0]["outcome"] == "failed"
            assert finished.embeddings[0]["cost_observations"][0]["value"]["calls"] == 1
            stage.close()

    asyncio.run(run())
