"""Identifier provenance (RM05, section 5), shared by the router and the loop.

RM05: resource names such as database IDs come from the request,
established conversation context, or tool results; the gateway never
invents one to make a tool call possible. Spec 0.6 (section 5) narrows
the provenance: only user turns and tool results count. The gateway's
own messages (a clarification question that lists the valid values,
repair notes) and the model's turns are never provenance.

An argument is identifier-like when its schema property names a closed
set of string values (``enum``) or constrains a string with a
``pattern``, such as ``service`` and ``database_id`` in section 9.
Bounded numbers (a time window, a log limit) are not identifiers.

Clarification labels come from the schema, in the run's language
(section 5): the property's ``x-relaymind-label`` entry for that
language, else its ``title``, else its name. The demo tool schemas
carry English and Chinese labels for each identifier (#55).
"""

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from relaymind.adapters import Message, Role
from relaymind.tools.registry import ToolResult

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
# An identifier token: word parts joined by -, _, ., :, or /, or a number.
_IDENTIFIER_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_.:/][A-Za-z0-9]+)+|\d+")
_LETTER = re.compile(r"[^\W\d_]")
# One Latin word or ID with optional surrounding spaces and punctuation.
_BARE_WORD = re.compile(r"\s*[^\w\s]*[A-Za-z0-9][A-Za-z0-9_.:/-]*[^\w\s]*\s*")


def mentions(text: str, value: str) -> bool:
    """Whether value appears in text as a whole token, not inside a longer ID.

    The separators ., :, and / join identifier parts when a letter or
    digit follows (staging-api.backup is another resource), but a
    sentence's trailing punctuation still ends the token (RM05).
    """
    pattern = (
        rf"(?<![A-Za-z0-9_-])(?<![A-Za-z0-9][.:/]){re.escape(value)}"
        rf"(?![A-Za-z0-9_-])(?![.:/][A-Za-z0-9])"
    )
    return re.search(pattern, text) is not None


def provenance_text(messages: Sequence[Message], results: Iterable[ToolResult] = ()) -> str:
    """User turns and gateway tool result data: the RM05 provenance (section 5)."""
    parts = [m.content for m in messages if m.role is Role.USER and m.content]
    parts += [json.dumps(r.data, ensure_ascii=False) for r in results if r.data]
    return "\n".join(parts)


def _properties(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    properties = schema.get("properties", {})
    return properties if isinstance(properties, Mapping) else {}


def is_identifier_property(spec: object) -> bool:
    """A closed set of string values, or a pattern-constrained string."""
    if not isinstance(spec, Mapping):
        return False
    values = spec.get("enum")
    if values:
        return all(isinstance(value, str) for value in values)
    return spec.get("type") == "string" and isinstance(spec.get("pattern"), str)


def required_enum_identifiers(schema: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Required properties whose values are a closed list of string IDs."""
    properties = _properties(schema)
    found: dict[str, tuple[str, ...]] = {}
    for name in schema.get("required", ()):
        spec = properties.get(name, {})
        if is_identifier_property(spec) and spec.get("enum"):
            found[name] = tuple(spec["enum"])
    return found


def unsupported_identifiers(
    arguments: Mapping[str, Any], schema: Mapping[str, Any], provenance: str
) -> list[str]:
    """Names of identifier-like arguments whose value the provenance never names."""
    properties = _properties(schema)
    return [
        name
        for name, value in arguments.items()
        if isinstance(value, str)
        and is_identifier_property(properties.get(name))
        and not mentions(provenance, value)
    ]


LABEL_KEYWORD = "x-relaymind-label"


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def label(schema: Mapping[str, Any], name: str, language: str = "en") -> str:
    """The human label for a property in a language (en or zh).

    The schema's x-relaymind-label entry for the language, else the
    property title, else the property name.
    """
    spec = _properties(schema).get(name, {})
    if not isinstance(spec, Mapping):
        return name
    labels = spec.get(LABEL_KEYWORD)
    by_language = _text(labels.get(language)) if isinstance(labels, Mapping) else None
    return by_language or _text(spec.get("title")) or name


def label_in(schemas: Sequence[Mapping[str, Any]], name: str, language: str = "en") -> str:
    """The label for a property shared by several tool schemas.

    A language label in any schema wins, then a title in any schema,
    then the name, so a first schema without a label does not hide one
    that another allowed tool declares (section 5).
    """
    specs = [_properties(schema).get(name) for schema in schemas]
    specs = [spec for spec in specs if isinstance(spec, Mapping)]
    for spec in specs:
        labels = spec.get(LABEL_KEYWORD)
        found = _text(labels.get(language)) if isinstance(labels, Mapping) else None
        if found:
            return found
    for spec in specs:
        if title := _text(spec.get("title")):
            return title
    return name


def is_identifier_only(text: str) -> bool:
    """True when text holds no words besides identifiers, numbers, and punctuation.

    Such a reply, for example ``staging-api``, says nothing about the
    language of the conversation. A single bare Latin word, such as
    ``auth`` or ``redis``, counts too: it may be a resource name, so it
    is not language evidence (section 5).
    """
    if _BARE_WORD.fullmatch(text):
        return True
    rest = _IDENTIFIER_TOKEN.sub(" ", text)
    return not (_CJK.search(rest) or _LETTER.search(rest))


def has_cjk(text: str) -> bool:
    return _CJK.search(text) is not None
