"""Independent, recommendation-only asset discovery (RM34)."""

from relaymind.assets.catalog import (
    AssetRecord,
    AssetValidationError,
    CatalogSnapshot,
    SearchDocument,
    discovery_records,
    validate_asset_record,
    validate_catalog_snapshot,
)

__all__ = [
    "AssetRecord",
    "AssetValidationError",
    "CatalogSnapshot",
    "SearchDocument",
    "discovery_records",
    "validate_asset_record",
    "validate_catalog_snapshot",
]
