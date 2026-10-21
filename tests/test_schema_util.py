"""Shared boundary checks for published, versioned schemas (spec section 8).

Section 8: each normalized object has a versioned JSON Schema and is
validated at the boundary; unknown fields are rejected. RouteDecision
(RM02) uses these checks first; the later section 8 objects (TraceEvent,
RM22, among them) share them, so all report the same field-naming
errors.
"""

import pytest

from relaymind.schema_util import check_closed_fields, check_schema_version, load_packaged_schema

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
