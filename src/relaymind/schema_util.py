"""Shared boundary checks for published, versioned schemas (spec section 8).

Each normalized object publishes a JSON Schema under its package's
``schemas/`` directory and is validated at the boundary. RouteDecision
is the first; the objects that follow it (ToolProposal, TraceEvent, and
the rest of the section 8 table) share these helpers, which hold the
checks every such validator starts with: a closed set of fields and an
exact schema version.
"""

import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from importlib import resources
from typing import Any, NoReturn


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


def first_schema_error(validator: Any, instance: object) -> Any:
    """The first validation error in instance-path order, or None.

    validator is a jsonschema validator; the error is a ValidationError.
    """
    errors = sorted(validator.iter_errors(instance), key=lambda error: _path_key(error.path))
    return errors[0] if errors else None


def _path_key(path: Iterable[object]) -> tuple[tuple[int, object], ...]:
    """A sort key for a JSON path whose parts may mix integers and strings.

    Each part is tagged by type, so array indexes sort as numbers among
    themselves, names sort as text, and an index never meets a name in a
    comparison (a mapping with a non-string key would otherwise raise
    TypeError).
    """
    return tuple(
        (0, part) if isinstance(part, int) and not isinstance(part, bool) else (1, str(part))
        for part in path
    )


class FrozenJSONObject(dict[str, Any]):
    """A read-only JSON object from freeze_json.

    It is a dict, so json and jsonschema accept it as they are, but every
    change raises TypeError. copy.deepcopy returns a plain, editable copy.
    """

    __slots__ = ()

    def _read_only(self, *args: object, **kwargs: object) -> NoReturn:
        raise TypeError("a frozen JSON object is read-only")

    __setitem__ = __delitem__ = __ior__ = _read_only
    clear = pop = popitem = setdefault = update = _read_only

    def __copy__(self) -> "FrozenJSONObject":
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> dict[str, Any]:
        return {key: copy.deepcopy(value, memo) for key, value in self.items()}

    def __reduce__(self) -> tuple[type, tuple[dict[str, Any]]]:
        return (type(self), (dict(self),))


class FrozenJSONArray(list[Any]):
    """A read-only JSON array from freeze_json; see FrozenJSONObject."""

    __slots__ = ()

    def _read_only(self, *args: object, **kwargs: object) -> NoReturn:
        raise TypeError("a frozen JSON array is read-only")

    __setitem__ = __delitem__ = __iadd__ = __imul__ = _read_only
    append = clear = extend = insert = pop = remove = reverse = sort = _read_only

    def __copy__(self) -> "FrozenJSONArray":
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> list[Any]:
        return [copy.deepcopy(item, memo) for item in self]

    def __reduce__(self) -> tuple[type, tuple[list[Any]]]:
        return (type(self), (list(self),))


def freeze_json(value: object) -> Any:
    """A deep, read-only copy of a JSON value (RM07, RM08).

    Objects become FrozenJSONObject and arrays FrozenJSONArray, at every
    level; scalars are kept. A registered schema frozen this way cannot
    drift from the hash taken at registration, and it still serializes
    and validates like the plain value.
    """
    if isinstance(value, Mapping):
        return FrozenJSONObject({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return FrozenJSONArray(freeze_json(item) for item in value)
    return value
