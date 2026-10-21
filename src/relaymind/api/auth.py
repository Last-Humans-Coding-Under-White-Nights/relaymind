"""Application token check for the API layer (RM19, spec section 7.1).

The gateway requires a generated application token on every request. A
missing or invalid token is 401 with code unauthenticated; the response
never repeats the token.
"""

import hmac

from relaymind.api.errors import ApiError

__all__ = ["require_bearer_token"]

_SCHEME = "bearer"


def require_bearer_token(authorization: str | None, token: str) -> None:
    """Accept the request only when it carries ``Authorization: Bearer <token>``."""
    scheme, _, supplied = (authorization or "").partition(" ")
    supplied = supplied.strip()
    if (
        scheme.lower() != _SCHEME
        or not supplied
        or not hmac.compare_digest(supplied.encode(), token.encode())
    ):
        raise ApiError.of("unauthenticated", "a valid application token is required")
