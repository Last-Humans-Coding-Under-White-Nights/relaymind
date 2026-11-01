"""Admission of a user-named asset against the offer and the current catalog (RM37/AC28).

A choice or a rejection names an offered asset; a correction may name any
current in-scope asset. Both are checked against the record as it stands now.
A catalog failure is never reported as a changed asset: it propagates.
"""

from collections.abc import Mapping

# The source-owned fields that must be unchanged for an offered asset to stay
# the same asset (spec section 5.1): a changed value is asset_changed.
IDENTITY_FIELDS = (
    "schema_version",
    "kind",
    "source_id",
    "source_locator",
    "source_version",
    "content_hash",
    "identity",
)


def current_asset(catalog, asset_id: str):
    """The current catalog record for asset_id, or None when it is gone."""
    if catalog is None:
        raise RuntimeError("asset admission needs the current catalog")
    return next((asset for asset in catalog.snapshot().assets if asset.asset_id == asset_id), None)


def offered_record(accepted: Mapping, asset_id: str) -> Mapping | None:
    """The record as it was offered, from the internal accepted_json offer payload."""
    return next((asset for asset in accepted["catalog"] if asset["asset_id"] == asset_id), None)


def same_identity(original: Mapping, current) -> bool:
    return all(getattr(current, name) == original[name] for name in IDENTITY_FIELDS)
