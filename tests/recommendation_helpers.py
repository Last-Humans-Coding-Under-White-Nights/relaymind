"""Deterministic RM36 recommendation fixtures; no model or host."""

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import httpx

from helpers import FakeClock
from relaymind.assets.settings import AssetSettings, RecommendationSettings, SourceConfig
from relaymind.store import RunStore
from relaymind.store.asset_catalog import AssetCatalogStore
from test_asset_sources import capture, skill

SCOPE = {"user_id": "u", "workspace_id": "w"}
BODY = dict(
    schema_version=1,
    task_text="hello greeting",
    requested_kinds=["skill"],
    selection_scope="discovery",
)


def build_app(config, **options):
    """RM36/RM38: exercise launcher wiring with a deterministic store clock."""
    from relaymind import launcher

    with patch.object(launcher, "RunStore", lambda *a, **kw: RunStore(*a, clock=FakeClock(), **kw)):
        return launcher.build_app(config, **options)


def setup(tmp_path, *, populate=True, settings=None, primary=None, clock=None):
    from fastapi import FastAPI

    from relaymind.api.recommendations import install_recommendation_routes
    from relaymind.recommendations.service import CatalogProvider, RecommendationService
    from relaymind.store.recommendations import RecommendationStore

    # RM36/AC27: lifecycle assertions must not consume budgets while CI is descheduled.
    # Tests of real deadline expiry explicitly supply TraceClock instead.
    if clock is None:
        clock = FakeClock()
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    if populate:
        skill(source)
    runs = RunStore(tmp_path / "db", clock=clock)
    catalog = AssetCatalogStore(runs)
    configured = SourceConfig("example", "local_directory", source)
    if populate:
        catalog.publish(capture(source), expected_revision=0)
    provider = CatalogProvider(catalog, AssetSettings((configured,)), scope=SCOPE, permissions=())
    config = settings or RecommendationSettings(
        enabled=True, user_id="u", workspace_id="w", total_ms=500, primary_ms=150
    )
    service = RecommendationService(
        RecommendationStore(runs), provider, config, primary=primary, clock=clock
    )
    app = FastAPI()
    app.state.application_token = "a" * 40
    install_recommendation_routes(app, service, configured_scope=SCOPE)
    return app, service, runs, catalog


NOW = datetime(2026, 10, 29, tzinfo=UTC)


def accepted(**changes):
    from relaymind.store.recommendations import recommendation_budgets

    value = dict(
        recommendation_id=str(uuid.uuid4()),
        task_id="a" * 64,
        task_text="greet",
        conversation=[],
        scope=SCOPE,
        budgets=recommendation_budgets(),
        configured_primary={"name": "lexical", "version": "1"},
        config_digest="b" * 64,
        catalog_revision=1,
        host_snapshot_revision=None,
        requested_kinds=["skill"],
        selection_scope="discovery",
        language_tags=[],
        code_switched=False,
        catalog=[],
        created_at=NOW,
    )
    return value | changes


def claim(key="key", digest="c" * 64, scope=None):
    from relaymind.store.recommendations import RecommendationClaim

    return RecommendationClaim(
        scope or SCOPE, "application", "POST", "/relay/v1/recommendations", None, key, digest
    )


def empty_result():
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
            "checked_at": NOW.isoformat(),
        },
        observed_ms=1,
        embeddings=[],
        host_run_ids=[],
    )


async def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers={
            "Authorization": "Bearer " + app.state.application_token,
            "Idempotency-Key": "key",
        },
    )
