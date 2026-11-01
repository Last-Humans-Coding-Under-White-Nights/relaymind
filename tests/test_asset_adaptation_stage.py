"""RM37/RM42 AC28/AC33: real scoped feedback, no-op inference and fail-closed guards.

Each test runs a real selection under the recommendation deadline, so the
fixture uses a frozen clock: a runner descheduled for longer than the 500 ms
budget would receive a 504 with no result before the stage is applied.
"""

import asyncio
import json

import pytest

from helpers import FakeClock
from recommendation_helpers import BODY
from test_asset_adaptation_store import correct
from test_asset_user_actions import action_client
from test_embedding_manifest import fixture


def api():
    from relaymind.selection import adaptation_stage

    return adaptation_stage


def config(tmp_path):
    from relaymind.assets.embedding_config import load_adaptation_config
    from relaymind.assets.embedding_manifest import load_embedding_manifest

    model = tmp_path / "model"
    model.mkdir()
    path, _ = fixture(model)
    manifest = load_embedding_manifest(path)
    value = dict(
        schema_version=1,
        identity={"name": "fixture", "version": "1"},
        manifest="manifest.json",
        manifest_digest=manifest.manifest_digest,
        representation=dict(
            version="task_text.v1",
            normalization="NFC",
            query_prefix="",
            max_tokens=100,
            overflow="unavailable",
        ),
        similarity=dict(version="exact_cosine.v1", threshold=0.9),
        ranking_version="bounded_votes.v1",
    )
    path = model / "adaptation.json"
    path.write_text(json.dumps(value))
    return load_adaptation_config(path)


def request_for(service, catalog):
    from relaymind.selection.asset_contract import parse_asset_request
    from test_asset_selection import request

    return parse_asset_request(
        request(
            scope=service.scope,
            permissions=[],
            catalog=[a.to_json_object() for a in catalog.snapshot().assets],
            catalog_revision=catalog.revision(),
        )
    )


def test_ranked_history_invokes_actual_stage_and_revision_change_blocks_ready(tmp_path):
    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.selection.asset_chain import RecommendationFailure
    from test_embedding_backend import NativeDouble

    async def run():
        async with action_client(tmp_path, clock=FakeClock()) as (
            client,
            app,
            service,
            runs,
            catalog,
            _,
        ):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            await correct(client, parent, 0, "first")
            semantic = config(tmp_path)
            stage = api().AdaptationStage(
                app.state.feedback_store,
                semantic,
                EmbeddingEngine(semantic.manifest, NativeDouble()),
            )
            primary = parent["result"]["attempts"][0]["result"]
            outcome = await stage.apply(
                primary,
                request_for(service, catalog),
                budget=service.new_budget(),
                current_catalog=service.provider.current,
            )
            assert outcome.adaptation["feedback_revision"] == 1
            assert (
                len(outcome.embeddings) == 1
                and outcome.embeddings[0]["representation_revision"] == "task_text.v1"
            )
            assert outcome.embeddings[0]["cost_observations"][0]["value"]["calls"] == 1
            response = await client.post(
                "/relay/v1/feedback/reset",
                json={"schema_version": 1, "expected_feedback_revision": 1},
                headers={"Idempotency-Key": "reset"},
            )
            assert response.status_code == 200
            with pytest.raises(RecommendationFailure) as caught:
                outcome.guard()
            assert caught.value.code == "recommendation_unavailable"
            outcome.close()
            stage.close()

    asyncio.run(run())


def test_no_history_and_valid_no_match_have_no_similarity_calls_but_recheck_revision(tmp_path):
    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.selection.asset_chain import RecommendationFailure
    from test_embedding_backend import NativeDouble

    class Refuse(NativeDouble):
        def infer(self, *args):
            raise AssertionError("No inference allowed on no-op stage")

    async def run():
        async with action_client(tmp_path, clock=FakeClock()) as (
            client,
            app,
            service,
            runs,
            catalog,
            _,
        ):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            semantic = config(tmp_path)
            stage = api().AdaptationStage(
                app.state.feedback_store, semantic, EmbeddingEngine(semantic.manifest, Refuse())
            )
            primary = parent["result"]["attempts"][0]["result"]
            outcome = await stage.apply(
                primary,
                request_for(service, catalog),
                budget=service.new_budget(),
                current_catalog=service.provider.current,
            )
            assert outcome.embeddings == () and outcome.adaptation["changed"] is False
            await correct(client, parent, 0, "first")
            with pytest.raises(RecommendationFailure):
                outcome.guard()
            outcome.close()
            primary = dict(primary, outcome="no_match", decision="no_match", candidates=[])
            outcome = await stage.apply(
                primary,
                request_for(service, catalog),
                budget=service.new_budget(),
                current_catalog=service.provider.current,
            )
            assert outcome.embeddings == () and outcome.decision == "no_match"
            outcome.guard()
            outcome.close()
            stage.close()

    asyncio.run(run())


def test_similarity_failure_retains_actual_work_and_is_unavailable(tmp_path):
    from relaymind.assets.embedding_backends import EmbeddingEngine
    from test_embedding_backend import NativeDouble

    class Invalid(NativeDouble):
        def infer(self, batch, invoked):
            invoked()
            return [[0, 0] for _ in batch]

    async def run():
        async with action_client(tmp_path, clock=FakeClock()) as (
            client,
            app,
            service,
            runs,
            catalog,
            _,
        ):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            await correct(client, parent, 0, "first")
            semantic = config(tmp_path)
            stage = api().AdaptationStage(
                app.state.feedback_store, semantic, EmbeddingEngine(semantic.manifest, Invalid())
            )
            with pytest.raises(api().AdaptationFailure) as caught:
                await stage.apply(
                    parent["result"]["attempts"][0]["result"],
                    request_for(service, catalog),
                    budget=service.new_budget(),
                    current_catalog=service.provider.current,
                )
            assert caught.value.code == "recommendation_unavailable"
            assert caught.value.embeddings[0]["outcome"] == "failed"
            assert caught.value.embeddings[0]["cost_observations"][0]["value"]["calls"] == 1
            assert stage.buffers.active == 0
            stage.close()

    asyncio.run(run())
