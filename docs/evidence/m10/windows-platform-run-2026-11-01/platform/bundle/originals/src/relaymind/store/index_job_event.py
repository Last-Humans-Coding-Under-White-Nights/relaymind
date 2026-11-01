"""Closed index job evidence for the asset catalog (RM34, spec section 8.1)."""

from collections.abc import Mapping

from jsonschema import FormatChecker
from referencing import Registry, Resource

from relaymind.assets.catalog import validate_document
from relaymind.schema_util import StrictValidator, freeze_json, load_packaged_schema

_SCHEMA = load_packaged_schema("relaymind.store", "index_job_event.v1.json")
_REGISTRY = Registry().with_resource(
    "https://relaymind.local/schemas/recommendation_result.v1.json",
    Resource.from_contents(
        load_packaged_schema("relaymind.store", "recommendation_result.v1.json")
    ),
)
_VALIDATOR = StrictValidator(_SCHEMA, registry=_REGISTRY, format_checker=FormatChecker())


def _reasons(event_type: str) -> frozenset[str]:
    """The closed reason enum the schema declares for one event type."""
    for rule in _SCHEMA["allOf"]:
        if (
            rule.get("if", {}).get("properties", {}).get("event_type", {}).get("const")
            == event_type
        ):
            return frozenset(rule["then"]["properties"]["payload"]["properties"]["reason"]["enum"])
    raise LookupError(event_type)


FAILED_REASONS = _reasons("failed")
CANCELLED_REASONS = _reasons("cancelled")


def validate_job_event(value: object) -> Mapping:
    return freeze_json(validate_document(value, _VALIDATOR))
