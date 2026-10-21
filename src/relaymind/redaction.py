"""Redaction of configured secret fields (RM12, sections 5 and 8).

Logs and traces must redact configured secret fields, and events that
contain tool results are redacted before persistence and rendering
(section 8). The secret fields are operator configuration: a set of
JSON object keys, matched without regard to case. A value under such a
key is replaced with ``[redacted]`` wherever it appears, at any depth.

Free text, such as model output, has no keys. The values that were
found under secret keys during a run can be scrubbed from it with
``redact_text``, so a model that repeats a secret it was shown does not
write it into the trace either. It replaces whole tokens only, so a
short secret value never corrupts unrelated words or numbers.

This module imports nothing from the rest of RelayMind.
"""

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
        if isinstance(value, Mapping):
            return {
                key: REDACTED if self.is_secret(key) else self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.redact(item) for item in value]
        return value

    def secret_values(self, value: Any) -> frozenset[str]:
        """The non-blank strings found under secret fields, at any depth."""
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
        elif inside_secret and isinstance(value, str) and value.strip() and value != REDACTED:
            found.add(value)


def redact_text(text: str, secrets: Iterable[str]) -> str:
    """text with every known secret value replaced, longest first.

    A value is replaced as a whole token: it must not continue a longer
    run of letters or digits, so a short value such as "1" or "true"
    leaves 10 and untrue alone.
    """
    for secret in sorted(set(secrets), key=len, reverse=True):
        pattern = rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])"
        text = re.sub(pattern, lambda _: REDACTED, text)
    return text
