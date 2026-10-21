"""Shared boundary checks for published, versioned schemas (spec section 8).

Section 8: each normalized object has a versioned JSON Schema and is
validated at the boundary; unknown fields are rejected. RouteDecision
(RM02) uses these checks first; the later section 8 objects (TraceEvent,
RM22, among them) share them, so all report the same field-naming
errors.
"""

import copy
import json
import pickle

import pytest
from jsonschema import Draft202012Validator

from relaymind.schema_util import (
    check_closed_fields,
    check_schema_version,
    first_schema_error,
    freeze_json,
    load_packaged_schema,
)

FIELDS = ("schema_version", "route")


class ShapeError(ValueError):
    pass


def test_a_packaged_schema_loads_as_json():
    schema = load_packaged_schema("relaymind.orchestrator", "route_decision.v1.json")
    assert schema["$id"].endswith("route_decision.v1.json")


def test_closed_fields_return_the_mapping():
    data = {"schema_version": 1, "route": "local"}
    assert check_closed_fields(data, FIELDS, ShapeError, kind="Thing") is data


@pytest.mark.parametrize("data", [None, [], "local", 1])
def test_a_non_object_is_rejected_by_kind(data):
    with pytest.raises(ShapeError, match="^a Thing must be a JSON object$"):
        check_closed_fields(data, FIELDS, ShapeError, kind="Thing")


def test_unknown_fields_are_named_in_sorted_order():
    data = {"schema_version": 1, "route": "local", "zeta": 1, "alpha": 2}
    with pytest.raises(ShapeError, match="^unknown fields: alpha, zeta$"):
        check_closed_fields(data, FIELDS, ShapeError, kind="Thing")


def test_missing_fields_are_named_in_field_order():
    with pytest.raises(ShapeError, match="^missing fields: schema_version, route$"):
        check_closed_fields({}, FIELDS, ShapeError, kind="Thing")


def test_the_expected_schema_version_passes():
    check_schema_version(1, 1, ShapeError)


@pytest.mark.parametrize("value", [2, "1", True, 1.0, None])
def test_any_other_schema_version_is_rejected(value):
    # bool is an int subclass and True == 1, so the exact type is checked.
    with pytest.raises(ShapeError, match="^'schema_version' must be 1$"):
        check_schema_version(value, 1, ShapeError)


# --- first_schema_error (section 8, RM09) -----------------------------------

MIXED_KEYS = {
    "type": "object",
    "properties": {"service": {"type": "string"}},
    "additionalProperties": {"type": "string"},
}


def test_the_first_error_survives_paths_that_mix_integers_and_strings():
    """Section 8, RM09: a mapping with a non-string key gives error paths
    that mix int and str at one depth. Picking the first error must not
    raise TypeError, so the caller can still reject the value."""
    validator = Draft202012Validator(MIXED_KEYS)
    error = first_schema_error(validator, {0: 5, "service": 3})
    assert error is not None
    assert list(error.path) in ([0], ["service"])


def test_the_first_error_follows_instance_path_order():
    """Section 8: errors at one depth keep their natural order; integer
    indexes sort as numbers, not as text (index 2 before index 10)."""
    validator = Draft202012Validator({"type": "array", "items": {"type": "string"}})
    instance = ["ok"] * 11
    instance[10] = 1
    instance[2] = 1
    assert list(first_schema_error(validator, instance).path) == [2]
    named = Draft202012Validator(
        {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    )
    assert list(first_schema_error(named, {"b": 1, "a": 1}).path) == ["a"]
    assert first_schema_error(named, {"a": "x"}) is None


# --- freeze_json (RM07, RM08) -----------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {"service": {"type": "string", "enum": ["staging-api"]}},
    "required": ["service"],
    "additionalProperties": False,
}


def test_a_frozen_json_value_is_a_deep_copy_that_cannot_change():
    """RM07, RM08: a registered schema must not drift from its hash, so the
    frozen form copies the input and refuses every change at every level."""
    source = copy.deepcopy(SCHEMA)
    frozen = freeze_json(source)
    source["properties"]["service"]["enum"].append("payments-api")
    assert frozen == SCHEMA
    for change in (
        lambda: frozen.__setitem__("type", "array"),
        lambda: frozen.pop("type"),
        lambda: frozen.update(type="array"),
        lambda: frozen["properties"].clear(),
        lambda: frozen["properties"]["service"].setdefault("x", 1),
        lambda: frozen["required"].append("other"),
        lambda: frozen["required"].__setitem__(0, "other"),
        lambda: frozen["properties"]["service"]["enum"].sort(),
    ):
        with pytest.raises(TypeError, match="read-only"):
            change()
    assert frozen == SCHEMA


def test_a_frozen_schema_still_works_as_json_and_as_a_json_schema():
    """Section 8: the frozen form serializes as the same JSON text and
    validates with jsonschema exactly like the plain schema."""
    frozen = freeze_json(SCHEMA)
    assert json.dumps(frozen, sort_keys=True) == json.dumps(SCHEMA, sort_keys=True)
    Draft202012Validator.check_schema(frozen)
    validator = Draft202012Validator(frozen)
    assert validator.is_valid({"service": "staging-api"})
    assert not validator.is_valid({"service": "staging-api", "extra": 1})
    assert pickle.loads(pickle.dumps(frozen)) == SCHEMA


def test_a_deep_copy_of_a_frozen_value_is_plain_and_editable():
    thawed = copy.deepcopy(freeze_json(SCHEMA))
    thawed["properties"]["service"]["enum"].append("payments-api")
    assert type(thawed) is dict and type(thawed["required"]) is list
    assert thawed["properties"]["service"]["enum"] == ["staging-api", "payments-api"]
