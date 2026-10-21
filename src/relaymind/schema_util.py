"""Shared boundary checks for published, versioned schemas (spec section 8).

Each normalized object publishes a JSON Schema under its package's
``schemas/`` directory and is validated at the boundary. RouteDecision
is the first; the objects that follow it (ToolProposal, TraceEvent, and
the rest of the section 8 table) share these helpers, which hold the
checks every such validator starts with: a closed set of fields and an
exact schema version.
"""

import json
from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Any


def load_packaged_schema(package: str, name: str) -> dict[str, Any]:
    """Load ``schemas/<name>`` published inside a package."""
    return json.loads(resources.files(package).joinpath("schemas", name).read_text("utf-8"))


def check_closed_fields(
    data: object,
    fields: Sequence[str],
    error_cls: type[Exception],
    *,
    kind: str,
) -> Mapping[str, Any]:
    """Require a mapping with exactly these fields; return it.

    Unknown fields are named in sorted order, missing fields in schema
    order.
    """
    if not isinstance(data, Mapping):
        raise error_cls(f"a {kind} must be a JSON object")
    unknown = sorted(str(key) for key in data if key not in fields)
    if unknown:
        raise error_cls(f"unknown fields: {', '.join(unknown)}")
    missing = [name for name in fields if name not in data]
    if missing:
        raise error_cls(f"missing fields: {', '.join(missing)}")
    return data


def check_schema_version(value: object, expected: int, error_cls: type[Exception]) -> None:
    """Require schema_version to be exactly the expected integer."""
    # bool is a subclass of int and True == 1, so check the exact type.
    if type(value) is not int or value != expected:
        raise error_cls(f"'schema_version' must be {expected}")
