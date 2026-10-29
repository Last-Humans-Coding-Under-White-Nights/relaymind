"""One isolated strict HTTP v2 attempt; no retries or credentials (RM35/AC26)."""

import json
import zlib
from collections.abc import Callable

import httpx

from relaymind.assets.catalog import text
from relaymind.selection.asset_contract import (
    AssetSelectionError,
    AssetSelectionRequest,
    AttemptFailure,
    SelectorContext,
    parse_asset_result,
)
from relaymind.selection.http import validate_url

MAX_RESPONSE_BYTES = 1024 * 1024
READ_BUFFER_BYTES = 64 * 1024


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def strict_json(body: bytes) -> object:
    def constant(value):
        raise ValueError

    return json.loads(body.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=constant)


def _media_type(value: str | None) -> bool:
    if value is None:
        return False
    parts = [part.strip(" \t").lower() for part in value.split(";")]
    return parts[0] == "application/json" and (
        len(parts) == 1 or (len(parts) == 2 and parts[1] in ("charset=utf-8", 'charset="utf-8"'))
    )


async def _read_body(response: httpx.Response, context: SelectorContext) -> bytes:
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("identity", "gzip", "deflate"):
        raise AttemptFailure("unavailable", "transport_error")
    decoder = (
        None if encoding == "identity" else zlib.decompressobj(31 if encoding == "gzip" else 15)
    )
    body = bytearray()
    # Never use httpx.aiter_bytes: a compressed chunk may allocate arbitrary
    # decoded output before a length check. max_length bounds each decode here.
    async for chunk in response.aiter_raw(chunk_size=READ_BUFFER_BYTES):
        context.check()
        if decoder is None:
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise AttemptFailure("invalid", "response_too_large")
            body.extend(chunk)
            continue
        while chunk:
            decoded = decoder.decompress(chunk, MAX_RESPONSE_BYTES + 1 - len(body))
            body.extend(decoded)
            if len(body) > MAX_RESPONSE_BYTES:
                raise AttemptFailure("invalid", "response_too_large")
            chunk = decoder.unconsumed_tail
            if decoder.eof and decoder.unused_data:
                # Concatenated gzip members are legal and share the same cap.
                if encoding != "gzip":
                    raise AttemptFailure("unavailable", "transport_error")
                chunk = decoder.unused_data
                decoder = zlib.decompressobj(31)
            context.check()
    if decoder is not None and not decoder.eof:
        raise AttemptFailure("unavailable", "transport_error")
    context.check()
    return bytes(body)


class HttpAssetSelector:
    transport = "http"
    mechanism = "http"

    def __init__(
        self,
        *,
        name: str,
        version: str,
        url: str,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ):
        text(name, 256)
        text(version, 256)
        self.name, self.version, self.url = name, version, validate_url(url)
        self._transport_factory = transport_factory

    async def select(self, request: AssetSelectionRequest, context: SelectorContext) -> object:
        payload = request.to_bytes(context)
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=None,
                transport=None if self._transport_factory is None else self._transport_factory(),
            ) as client:
                # Setup and client entry consume the original attempt clock.
                # Fence before sending bytes; the client still closes on failure.
                context.check()
                async with client.stream(
                    "POST",
                    self.url,
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip, deflate",
                    },
                ) as response:
                    context.check()
                    if response.status_code != 200:
                        raise AttemptFailure("unavailable", "http_status")
                    if not _media_type(response.headers.get("content-type")):
                        raise AttemptFailure("invalid", "response_media_type")
                    body = await _read_body(response, context)
            context.check()
            try:
                parsed = parse_asset_result(strict_json(body))
            except (AssetSelectionError, ValueError, UnicodeError, RecursionError, OverflowError):
                raise AttemptFailure("invalid", "invalid_result") from None
            context.check()
            return parsed.to_json_object()
        except httpx.TimeoutException:
            raise AttemptFailure("timeout", "deadline_exceeded") from None
        except (httpx.HTTPError, zlib.error):
            raise AttemptFailure("unavailable", "transport_error") from None
