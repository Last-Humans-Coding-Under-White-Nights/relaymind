"""Closed independent recommendation evidence (RM36/AC27, section 8.1)."""

from collections.abc import Mapping
from typing import Any

from jsonschema import FormatChecker
from referencing import Registry, Resource

from relaymind.assets.catalog import validate_document
from relaymind.schema_util import StrictValidator, freeze_json, load_packaged_schema
from relaymind.selection.asset_contract import parse_recommendation_result

_SCHEMAS = {
    name: load_packaged_schema("relaymind.store", name + ".v1.json")
    for name in (
        "recommendation_view",
        "recommendation_event",
        "recommendation_readiness",
        "recommendation_result",
        "asset_record",
    )
}
_REGISTRY = Registry().with_resources(
    (f"https://relaymind.local/schemas/{name}.v1.json", Resource.from_contents(schema))
    for name, schema in _SCHEMAS.items()
)
# Result and asset schemas are reference targets for the view and event.
_VALIDATORS = {
    name: StrictValidator(_SCHEMAS[name], registry=_REGISTRY, format_checker=FormatChecker())
    for name in ("recommendation_view", "recommendation_event", "recommendation_readiness")
}


def _validate(name: str, value: object) -> dict[str, Any]:
    return validate_document(value, _VALIDATORS[name])


def validate_view(value: object, *, result_checked: bool = False) -> Mapping:
    """result_checked: the embedded result was already parsed in this transaction."""
    raw = _validate("recommendation_view", value)
    state = raw["state"]
    derived = (
        state in ("ready", "expired", "chosen")
        or state == "cancelled"
        and raw["ready_at"] is not None
    )
    if derived:
        if raw["result"] is None or raw["ready_at"] is None or raw["choice_expires_at"] is None:
            raise ValueError("invalid_view")
        if not result_checked:
            parse_recommendation_result(raw["result"])
        if raw["decision"] != raw["result"]["decision"]:
            raise ValueError("invalid_view")
    elif (
        any(raw[k] is not None for k in ("result", "decision", "ready_at", "choice_expires_at"))
        or raw["displayed_candidates"]
    ):
        raise ValueError("invalid_view")
    if (raw["error"] is not None) != (state == "failed"):
        raise ValueError("invalid_view")
    return freeze_json(raw)


def validate_event(
    value: object, *, allow_erased: bool = False, result_checked: bool = False
) -> Mapping:
    raw = _validate("recommendation_event", value)
    if raw["erased"] and not allow_erased:
        raise ValueError("invalid_erasure")
    if raw["event_type"] == "ready" and not raw["erased"] and not result_checked:
        parse_recommendation_result(raw["payload"]["result"])
    return freeze_json(raw)


def validate_readiness(value: object) -> Mapping:
    """Validate the closed public readiness body (RM36/AC27, section 7.6)."""
    raw = _validate("recommendation_readiness", value)
    coverage = raw["semantic_coverage"]
    if coverage["ready"] > coverage["total"]:
        raise ValueError("invalid_readiness")
    return freeze_json(raw)
