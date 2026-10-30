"""Deterministic RM36 recommendation fixtures; no model or host."""

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


def setup(tmp_path, *, populate=True, settings=None, primary=None, clock=None):
    from fastapi import FastAPI

    from relaymind.api.recommendations import install_recommendation_routes
    from relaymind.recommendations.service import CatalogProvider, RecommendationService
    from relaymind.store.recommendations import RecommendationStore

    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    if populate:
        skill(source)
    runs = RunStore(tmp_path / "db", **({"clock": clock} if clock else {}))
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
