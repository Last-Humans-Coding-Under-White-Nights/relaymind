"""Unauthenticated loopback selector transport (RM12, RM28, spec 0.40)."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from relaymind.adapters.local_http import is_loopback_host
from relaymind.selection.chain import _check_identity
from relaymind.selection.contract import (
    SelectionContext,
    SelectionError,
    SelectionRequest,
    SelectionResult,
    SelectorError,
    SelectorTimeout,
    parse_selection_request,
    parse_selection_result,
)


def validate_url(url: object) -> str:
    """Reject credentials and non-loopback URLs without exposing their text."""
    try:
        if not isinstance(url, str) or any(c.isspace() for c in url):
            raise ValueError
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or not is_loopback_host(parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or "?" in url
            or "#" in url
            or parsed.netloc.endswith(":")
            or (parsed.port is not None and parsed.port < 1)
        ):
            raise ValueError
        httpx.URL(url)
    except (ValueError, httpx.InvalidURL):
        raise ValueError(
            "selection HTTP url must be absolute loopback HTTP(S), with a valid port"
            " and no credentials, query or fragment"
        ) from None
    return url


@dataclass(frozen=True)
class HttpSelectorConfig:
    name: str
    version: str
    url: str
    transport: str = "http"

    def __post_init__(self) -> None:
        if self.transport != "http":
            raise ValueError("selection transport must be http")
        if any(not isinstance(v, str) or not v.strip() for v in (self.name, self.version)):
            raise ValueError("selection HTTP name and version must be nonblank")
        if self.name in ("model_router", "example_match"):
            raise ValueError("selection HTTP name is reserved")
        validate_url(self.url)


class HttpResultError(SelectorError):
    """A schema-valid, untrusted response failed identity; retain only its claims."""

    def __init__(self, result: SelectionResult) -> None:
        super().__init__("HTTP selector identity or mechanism rejected")
        self.result = result


class HttpSelector:
    """One fresh client per POST; no resource or cookie crosses attempts/event loops."""

    model_based = False
    transport = "http"

    def __init__(self, *, name: str, version: str, url: str) -> None:
        self.config = HttpSelectorConfig(name=name, version=version, url=url)
        self.name, self.version = name, version

    async def select(self, request: SelectionRequest, context: SelectionContext) -> dict[str, Any]:
        payload = parse_selection_request(request.to_json_object()).to_json_object()
        try:
            # The chain/benchmark owns the total monotonic deadline. No automatic retries.
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=None
            ) as client:
                response = await client.post(self.config.url, json=payload)
                if response.status_code != 200:
                    raise SelectorError("HTTP selector non-200 response")
                try:
                    result = parse_selection_result(response.json())
                except (ValueError, SelectionError):
                    raise SelectorError("HTTP selector invalid response") from None
        except httpx.TimeoutException:
            raise SelectorTimeout("HTTP selector transport timeout") from None
        except httpx.HTTPError:
            raise SelectorError("HTTP selector transport failure") from None
        try:
            _check_identity(self, result)
        except SelectionError:
            raise HttpResultError(result) from None
        return result.to_json_object()
