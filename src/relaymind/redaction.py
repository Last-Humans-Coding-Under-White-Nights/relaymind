"""Redaction of configured secret fields (RM12, sections 5 and 8).

Logs and traces must redact configured secret fields, and events that
contain tool results are redacted before persistence and rendering
(section 8). The secret fields are operator configuration: a set of
JSON object keys, matched without regard to case. A value under such a
key is replaced with ``[redacted]`` wherever it appears, at any depth.

Free text, such as model output, has no keys. The values that were
found under secret keys during a run can be scrubbed from it with
``redact_text``, so a model that repeats a secret it was shown does not
write it into the trace either; ``scrub`` does the same for every string
of a JSON value. It replaces whole tokens only, in one pass, so a short
secret value never corrupts unrelated words or numbers, and a
replacement is never scrubbed again.

This module imports nothing from the rest of RelayMind.
"""

import json
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

REDACTED = "[redacted]"

# The default configuration: key names that hold credentials in common
# APIs. An operator replaces the set; it is not a claim of completeness.
DEFAULT_SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "connection_string",
        "password",
        "secret",
        "token",
    }
)


@dataclass(frozen=True)
class Redactor:
    """Replaces the values of configured secret fields (RM12)."""

    secret_fields: Collection[str] = field(default=DEFAULT_SECRET_FIELDS)

    def __post_init__(self) -> None:
        fields = self.secret_fields
        if isinstance(fields, str) or not all(
            isinstance(name, str) and name.strip() for name in fields
        ):
            raise ValueError("secret_fields must be a collection of non-blank key names")
        object.__setattr__(self, "secret_fields", frozenset(name.lower() for name in fields))

    def is_secret(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self.secret_fields

    def redact(self, value: Any) -> Any:
        """A copy of a JSON value with every secret field's value replaced."""
        return self._redact(value, None)

    def redact_with_repeats(self, value: Any, known: Iterable[str] = ()) -> Any:
        """redact(), and the secret values replaced in every other string.

        A secret that sits in a field may also be repeated in free text
        next to it, such as a log message. The values found under secret
        fields of this same value, and the known values a run has
        already seen, are scrubbed (spec 0.12, section 8), as whole
        tokens (see redact_text).
        """
        return self._redact(value, _pattern(self.secret_values(value) | set(known)))

    def _redact(self, value: Any, pattern: re.Pattern[str] | None) -> Any:
        if isinstance(value, Mapping):
            return {
                key: REDACTED if self.is_secret(key) else self._redact(item, pattern)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple):
            return [self._redact(item, pattern) for item in value]
        if pattern is not None and isinstance(value, str):
            return pattern.sub(REDACTED, value)
        return value

    def secret_values(self, value: Any) -> frozenset[str]:
        """The non-blank strings and numbers found under secret fields, at any depth.

        A number is kept as its JSON text, so a numeric PIN repeated in a
        log message is scrubbed too. A boolean is not a secret value.
        """
        found: set[str] = set()
        self._collect(value, inside_secret=False, found=found)
        return frozenset(found)

    def _collect(self, value: Any, *, inside_secret: bool, found: set[str]) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                self._collect(item, inside_secret=inside_secret or self.is_secret(key), found=found)
        elif isinstance(value, list | tuple):
            for item in value:
                self._collect(item, inside_secret=inside_secret, found=found)
        elif not inside_secret or isinstance(value, bool):
            return
        elif isinstance(value, str) and value.strip() and value != REDACTED:
            found.add(value)
        elif isinstance(value, int | float):
            found.add(json.dumps(value))


def scrub(value: Any, secrets: Iterable[str]) -> Any:
    """A copy of a JSON value with the secret values replaced in every string.

    Keys are left alone; this is for free text whose secret fields were
    already redacted, such as the data of a tool result.
    """
    pattern = _pattern(secrets)
    return value if pattern is None else _scrub(value, pattern)


def _scrub(value: Any, pattern: re.Pattern[str]) -> Any:
    if isinstance(value, Mapping):
        return {key: _scrub(item, pattern) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(item, pattern) for item in value]
    if isinstance(value, str):
        return pattern.sub(REDACTED, value)
    return value


def redact_text(text: str, secrets: Iterable[str]) -> str:
    """text with every known secret value replaced, longest first.

    A value is replaced as a whole token: it must not continue a longer
    run of letters or digits, so a short value such as "1" or "true"
    leaves 10 and untrue alone. All values are replaced in one pass, so
    a replacement is never scrubbed again.
    """
    pattern = _pattern(secrets)
    return text if pattern is None else pattern.sub(REDACTED, text)


def _pattern(secrets: Iterable[str]) -> re.Pattern[str] | None:
    """One whole-token pattern for all values, longest first; None when there are none."""
    values = sorted({s for s in secrets if s}, key=len, reverse=True)
    if not values:
        return None
    alternatives = "|".join(re.escape(value) for value in values)
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alternatives})(?![A-Za-z0-9])")
