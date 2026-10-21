"""One canonical JSON text form for hashing and comparison.

Tool schema hashes (RM07), operation IDs (RM10), and approval argument
binding (RM11) must agree on one text form, so they all use this module.
It imports nothing from the rest of RelayMind.
"""

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
