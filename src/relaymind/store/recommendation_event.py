"""Closed independent recommendation/job evidence (RM36/AC27, section 8.1)."""

from collections.abc import Mapping
from typing import Any

from jsonschema import FormatChecker
from referencing import Registry, Resource

from relaymind.assets.catalog import validate_document
from relaymind.schema_util import StrictValidator, freeze_json, load_packaged_schema
from relaymind.selection.asset_contract import parse_recommendation_result

_NAMES = (
    "recommendation_view",
    "recommendation_event",
    "index_job_event",
    "recommendation_result",
    "asset_record",
)
_SCHEMAS = {name: load_packaged_schema("relaymind.store", name + ".v1.json") for name in _NAMES}
_REGISTRY = Registry().with_resources(
    (f"https://relaymind.local/schemas/{name}.v1.json", Resource.from_contents(schema))
    for name, schema in _SCHEMAS.items()
)
_VALIDATORS = {
    name: StrictValidator(schema, registry=_REGISTRY, format_checker=FormatChecker())
    for name, schema in _SCHEMAS.items()
}


def _validate(name: str, value: object) -> dict[str, Any]:
    return validate_document(value, _VALIDATORS[name])


def validate_view(value: object) -> Mapping:
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


def validate_event(value: object, *, allow_erased: bool = False) -> Mapping:
    raw = _validate("recommendation_event", value)
    if raw["erased"] and not allow_erased:
        raise ValueError("invalid_erasure")
    if raw["event_type"] == "ready" and not raw["erased"]:
        parse_recommendation_result(raw["payload"]["result"])
    return freeze_json(raw)


def validate_job_event(value: object) -> Mapping:
    return freeze_json(_validate("index_job_event", value))
