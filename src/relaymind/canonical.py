"""One canonical JSON text form for hashing and comparison.

Tool schema hashes (RM07), operation IDs (RM10), and approval argument
binding (RM11) must agree on one text form, so they all use this module,
and so do idempotency request hashes (spec 0.18 section 7.4). It imports
nothing from the rest of RelayMind.
"""

import hashlib
import json


def canonical_json(value: object) -> str:
    """Stable JSON text: sorted keys, no spaces, non-ASCII kept as is.

    NaN and infinities are not JSON (RFC 8259), so they raise ValueError
    instead of becoming NaN or Infinity in a hash or an operation ID.
    """
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except ValueError as exc:
        if "not JSON compliant" not in str(exc):
            raise
        raise ValueError("canonical JSON cannot hold NaN or infinite numbers") from exc


def canonical_digest(value: object) -> str:
    """The SHA-256 hex digest of the canonical JSON of value; ValueError as canonical_json."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
