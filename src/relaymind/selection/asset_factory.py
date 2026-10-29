"""Construct the configured recommendation primary without contacting it (RM35)."""

from relaymind.assets.settings import RecommendationSettings
from relaymind.selection.asset_contract import AssetSelector
from relaymind.selection.asset_http import HttpAssetSelector
from relaymind.selection.asset_lexical import LexicalAssetSelector


def build_asset_selector(settings: RecommendationSettings) -> AssetSelector:
    """The separate v2 primary; a lexical primary starts without a generation.

    It reports not_ready until a compatible publication exists. #194/#196 own
    API attachment and operational compatible-publication readiness.
    """
    if settings.primary == "lexical":
        return LexicalAssetSelector(None)
    if settings.primary == "http":
        return HttpAssetSelector(name=settings.name, version=settings.version, url=settings.url)
    raise ValueError(f"recommendations primary {settings.primary!r} is not available")
