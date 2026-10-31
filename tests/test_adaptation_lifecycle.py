"""RM37/RM41/RM42 AC28/AC32/AC33: store-ordered actual finish/applied/terminal evidence."""

import asyncio
import threading

from recommendation_helpers import BODY
from relaymind.assets.embedding_backends import EmbeddingEngine
from relaymind.selection.adaptation_stage import AdaptationStage
from test_asset_adaptation_stage import config
from test_asset_adaptation_store import correct
from test_asset_user_actions import action_client
from test_embedding_backend import NativeDouble


def test_ready_persists_actual_stage_evidence_then_applied_then_ready(tmp_path):
    async def run():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, _):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            await correct(client, parent, 0, "first")
            semantic = config(tmp_path)
            service.adaptation = AdaptationStage(
                app.state.feedback_store,
                semantic,
                EmbeddingEngine(semantic.manifest, NativeDouble()),
            )
            response = await client.post(
                "/relay/v1/recommendations", json=BODY, headers={"Idempotency-Key": "adapted"}
            )
            assert response.status_code == 200, response.text
            body = response.json()
            result = body["result"]
            assert result["adaptation"]["feedback_revision"] == 1
            assert len(result["embeddings"]) == 1
            events = (
                await client.get(f"/relay/v1/recommendations/{body['recommendation_id']}/events")
            ).json()["events"]
            assert [e["event_type"] for e in events][-3:] == [
                "selector.finished",
                "adaptation.applied",
                "ready",
            ]
            assert events[-3]["payload"]["embeddings"] == result["embeddings"]
            assert events[-3]["payload"]["attempt_record"]["outcome"] == "ranked"

    asyncio.run(run())


def test_failed_similarity_keeps_primary_success_and_cost_before_failed(tmp_path):
    class Invalid(NativeDouble):
        def infer(self, tokens, invoked):
            invoked()
            return [[0, 0] for _ in tokens]

    async def run():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, _):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            await correct(client, parent, 0, "first")
            semantic = config(tmp_path)
            service.adaptation = AdaptationStage(
                app.state.feedback_store, semantic, EmbeddingEngine(semantic.manifest, Invalid())
            )
            response = await client.post(
                "/relay/v1/recommendations", json=BODY, headers={"Idempotency-Key": "failed-stage"}
            )
            assert response.status_code == 503, response.text
            rid = response.json()["error"]["recommendation_id"]
            events = (await client.get(f"/relay/v1/recommendations/{rid}/events")).json()["events"]
            assert [e["event_type"] for e in events][-2:] == ["selector.finished", "failed"]
            assert not any(e["event_type"] == "adaptation.applied" for e in events)
            finish = events[-2]["payload"]
            assert finish["attempt_record"]["outcome"] == "ranked"
            assert finish["embeddings"][0]["outcome"] == "failed"
            assert finish["embeddings"][0]["cost_observations"][0]["value"]["calls"] == 1

    asyncio.run(run())


def test_disconnect_flushes_live_stage_cost_before_cancel_without_relabeling_primary(tmp_path):
    class Slow(NativeDouble):
        def __init__(self):
            self.started, self.release = threading.Event(), threading.Event()

        def infer(self, tokens, invoked):
            invoked()
            self.started.set()
            self.release.wait(1)
            return [[3, 4] for _ in tokens]

        def cancel(self):
            self.release.set()

    async def run():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, _):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            await correct(client, parent, 0, "first")
            semantic = config(tmp_path)
            native = Slow()
            service.adaptation = AdaptationStage(
                app.state.feedback_store, semantic, EmbeddingEngine(semantic.manifest, native)
            )
            pending = asyncio.create_task(
                client.post(
                    "/relay/v1/recommendations",
                    json=BODY,
                    headers={"Idempotency-Key": "cancel-stage"},
                )
            )
            for _ in range(100):
                if native.started.is_set():
                    break
                await asyncio.sleep(0.001)
            assert native.started.is_set()
            rid = next(iter(service._active))
            await service.cancel_computing(rid, reason="disconnect")
            cancelled = await pending
            assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
            events = (await client.get(f"/relay/v1/recommendations/{rid}/events")).json()["events"]
            assert [e["event_type"] for e in events][-2:] == ["selector.finished", "cancelled"]
            finish = events[-2]["payload"]
            assert finish["attempt_record"]["outcome"] == "ranked"
            assert finish["embeddings"][0]["outcome"] == "cancelled"
            assert finish["embeddings"][0]["cost_observations"][0]["value"]["calls"] == 1
            assert not any(e["event_type"] == "adaptation.applied" for e in events)

    asyncio.run(run())


def test_reset_during_similarity_fences_private_work_and_restores_baseline(tmp_path):
    """RM37/RM42 AC28/AC33: reset acknowledges only after access invalidation."""

    class Slow(NativeDouble):
        def __init__(self):
            self.started, self.release = threading.Event(), threading.Event()

        def infer(self, tokens, invoked):
            invoked()
            self.started.set()
            self.release.wait(1)
            return [[3, 4] for _ in tokens]

        def cancel(self):
            self.release.set()

    async def run():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, _):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            await correct(client, parent, 0, "first")
            semantic = config(tmp_path)
            native = Slow()
            service.adaptation = AdaptationStage(
                app.state.feedback_store, semantic, EmbeddingEngine(semantic.manifest, native)
            )
            pending = asyncio.create_task(
                client.post(
                    "/relay/v1/recommendations",
                    json=BODY,
                    headers={"Idempotency-Key": "reset-stage"},
                )
            )
            for _ in range(200):
                if native.started.is_set():
                    break
                await asyncio.sleep(0.001)
            assert native.started.is_set()
            reset = await client.post(
                "/relay/v1/feedback/reset",
                json={"schema_version": 1, "expected_feedback_revision": 1},
                headers={"Idempotency-Key": "reset-in-flight"},
            )
            assert reset.status_code == 200
            failed = await pending
            assert failed.status_code == 503
            rid = failed.json()["error"]["recommendation_id"]
            events = (await client.get(f"/relay/v1/recommendations/{rid}/events")).json()["events"]
            assert [e["event_type"] for e in events][-2:] == ["selector.finished", "failed"]
            assert events[-2]["payload"]["attempt_record"]["outcome"] == "ranked"
            assert len(events[-2]["payload"]["embeddings"]) == 1
            assert not any(e["event_type"] == "adaptation.applied" for e in events)
            assert not service.adaptation.buffers._leases
            restored = await client.post(
                "/relay/v1/recommendations", json=BODY, headers={"Idempotency-Key": "after-reset"}
            )
            assert restored.status_code == 200
            result = restored.json()["result"]
            for key in ("final_candidates", "decision", "base_candidates", "base_decision"):
                assert result[key] == parent["result"][key]
            assert result["adaptation"]["changed"] is False and result["embeddings"] == []

    asyncio.run(run())


def test_admission_infrastructure_failure_retains_inferred_primary_cost_before_failed(tmp_path):
    """RM16/RM35/RM41: usable storage must retain actual cost on infrastructure failure."""
    from relaymind.selection.asset_hybrid import HybridAssetSelector
    from test_asset_hybrid import setup

    class Counted(NativeDouble):
        calls = 0

        def infer(self, tokens, invoked):
            self.calls += 1
            return super().infer(tokens, invoked)

    async def run():
        async with action_client(tmp_path) as (client, app, service, runs, catalog, _):
            config, index = setup(tmp_path)
            offer = service.provider.pin(("skill", "mcp_tool"))
            index.publish_batch(offer.catalog, [(0.6, 0.8)] * len(offer.catalog))
            native = Counted()
            service.primary = HybridAssetSelector(
                config, index, EmbeddingEngine(config.manifest, native)
            )

            def broken_policy(record):
                raise RuntimeError("fixture admission infrastructure")

            service.provider._policy = broken_policy
            response = await client.post(
                "/relay/v1/recommendations",
                json=BODY,
                headers={"Idempotency-Key": "admission-error"},
            )
            assert (
                response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
            )
            assert native.calls == 1
            rid = response.json()["error"]["recommendation_id"]
            events = (await client.get(f"/relay/v1/recommendations/{rid}/events")).json()["events"]
            assert [e["event_type"] for e in events][-2:] == ["selector.finished", "failed"]
            finished = [e["payload"] for e in events if e["event_type"] == "selector.finished"]
            assert len(finished) == 1
            assert finished[0]["attempt_record"]["outcome"] == "ranked"
            assert finished[0]["attempt_record"]["result"]["outcome"] == "ranked"
            assert finished[0]["embeddings"][0]["outcome"] == "succeeded"
            assert finished[0]["embeddings"][0]["cost_observations"][0]["value"]["calls"] == 1
            assert events[-1]["payload"]["attempts"] == [finished[0]["attempt_record"]]
            assert not any(e["event_type"] in ("ready", "adaptation.applied") for e in events)

    asyncio.run(run())
