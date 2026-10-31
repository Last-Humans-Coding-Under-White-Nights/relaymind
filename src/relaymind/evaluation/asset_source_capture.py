"""Explicit pinned public-source acquisition, outside runtime ingestion (RM34/RM40)."""

import asyncio
import ipaddress
import os
import re
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from relaymind.evaluation.asset_corpus_io import MIB, AssetCorpusError, matches_original, read_local


def _public_url(value: str) -> str:
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.fragment
            or url.port not in (None, 443)
        ):
            raise ValueError
        host = url.hostname.lower()
        if re.fullmatch(r"0[xX][0-9a-fA-F]+", host):
            raise ValueError
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise ValueError
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError
        except ValueError:
            if ":" in host or re.fullmatch(r"[0-9.]+", host):
                raise
        if len(value.encode()) > 2048:
            raise ValueError
    except (ValueError, UnicodeError):
        raise AssetCorpusError("unsafe_source_url") from None
    return value


def acquire_recipe(
    recipe: Mapping, cache_root: Path, *, client: httpx.AsyncClient | None = None
) -> list[dict]:
    """One <=60s/100MiB serial acquisition job; resume reuses verified cache bytes."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise AssetCorpusError("capture_requires_sync_context")
    return asyncio.run(_acquire_recipe(recipe, cache_root, client=client))


async def _acquire_recipe(recipe: Mapping, cache_root: Path, *, client=None) -> list[dict]:
    if recipe.get("schema_version") != 1 or type(recipe.get("schema_version")) is not int:
        raise AssetCorpusError("invalid_source_recipe")
    files = recipe.get("files")
    if not isinstance(files, list):
        raise AssetCorpusError("invalid_source_recipe")
    if len(files) > 1000:
        raise AssetCorpusError("source_limit")
    redirect_hosts = recipe.get("redirect_hosts", [])
    if not isinstance(redirect_hosts, list) or any(
        not isinstance(host, str) or not host for host in redirect_hosts
    ):
        raise AssetCorpusError("invalid_source_recipe")
    ids = set()
    total = 0
    for row in files:
        if (
            not isinstance(row, Mapping)
            or set(row) - {"id", "url", "sha256", "bytes", "kind"}
            or not {"id", "url", "sha256", "bytes"} <= set(row)
            or not isinstance(row["url"], str)
            or not isinstance(row["id"], str)
            or not row["id"]
            or len(row["id"].encode()) > 256
            or row["id"] in ids
            or not isinstance(row["sha256"], str)
            or not re.fullmatch("[0-9a-f]{64}", row["sha256"])
            or type(row["bytes"]) is not int
            or row["bytes"] < 0
            or row.get("kind", "original") not in ("original", "metadata")
        ):
            raise AssetCorpusError("invalid_source_recipe")
        ids.add(row["id"])
        total += row["bytes"]
    if total > 100 * MIB:
        raise AssetCorpusError("source_limit")
    cache_root = Path(cache_root)
    if cache_root.is_symlink():
        raise AssetCorpusError("unsafe_cache")
    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    own = client is None
    if client is None:
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=10)
    # Redirects may reach only hosts of rows whose own URL is safe; an unsafe
    # row is excluded by itself below and never blocks the other rows.
    # Host names compare case-insensitively; urlsplit().hostname is lower-case.
    origins = {host.lower() for host in redirect_hosts}
    for other in files:
        try:
            origins.add(urlsplit(_public_url(other["url"])).hostname)
        except AssetCorpusError:
            pass
    started = time.monotonic()
    received = 0
    requests = 0
    results = []
    try:
        for row in files:
            result = dict(
                id=row["id"],
                sha256=row["sha256"],
                bytes=row["bytes"],
                status="excluded",
                captured_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
            path = cache_root / (row["sha256"] + ".original")
            try:
                if time.monotonic() - started >= 60:
                    raise AssetCorpusError("capture_job_limit")
                url = _public_url(row["url"])
                limit = 4 * MIB if row.get("kind") == "metadata" else 10 * MIB
                if row["bytes"] > limit:
                    raise AssetCorpusError("source_limit")
                if path.exists() or path.is_symlink():
                    raw = read_local(cache_root, path.name, limit)
                    if time.monotonic() - started >= 60:
                        raise AssetCorpusError("capture_job_limit")
                    if not matches_original(raw, row["sha256"], row["bytes"]):
                        raise AssetCorpusError("cached_digest_mismatch")
                    result.update(status="reused", final_url=row["url"])
                    results.append(result)
                    continue
                payload = None
                for redirect in range(4):
                    remaining = 60 - (time.monotonic() - started)
                    if remaining <= 0 or requests >= 1000:
                        raise AssetCorpusError("capture_job_limit")
                    requests += 1
                    exchange_deadline = time.monotonic() + min(10, remaining)

                    def check_exchange(deadline=exchange_deadline):
                        now = time.monotonic()
                        if now - started >= 60:
                            raise AssetCorpusError("capture_job_limit")
                        if now >= deadline:
                            raise AssetCorpusError("capture_exchange_limit")

                    try:
                        # Async cancellation bounds connect/headers AND a pending body read;
                        # an inactivity timeout alone cannot stop an endlessly trickling peer.
                        async with asyncio.timeout(min(10, remaining)):
                            async with client.stream(
                                "GET",
                                url,
                                headers={"Accept-Encoding": "identity"},
                                timeout=min(10, remaining),
                            ) as response:
                                check_exchange()
                                if response.status_code in (301, 302, 303, 307, 308):
                                    if redirect == 3 or not response.headers.get("location"):
                                        raise AssetCorpusError("source_redirect_limit")
                                    url = _public_url(urljoin(url, response.headers["location"]))
                                    if urlsplit(url).hostname not in origins:
                                        raise AssetCorpusError("unsafe_source_redirect")
                                    continue
                                if response.status_code != 200:
                                    raise AssetCorpusError("source_http_error")
                                if (
                                    response.headers.get("content-encoding", "identity").lower()
                                    != "identity"
                                ):
                                    raise AssetCorpusError("source_encoding")
                                length = response.headers.get("content-length")
                                if length is not None and (
                                    not length.isascii()
                                    or not length.isdigit()
                                    or int(length) > limit
                                ):
                                    raise AssetCorpusError("source_limit")
                                chunks, count = [], 0
                                iterator = response.aiter_raw(chunk_size=65536).__aiter__()
                                while True:
                                    check_exchange()
                                    try:
                                        chunk = await anext(iterator)
                                    except StopAsyncIteration:
                                        check_exchange()
                                        break
                                    check_exchange()
                                    count += len(chunk)
                                    received += len(chunk)
                                    if count > limit or received > 100 * MIB:
                                        raise AssetCorpusError("source_limit")
                                    chunks.append(chunk)
                                payload = b"".join(chunks)
                            check_exchange()
                    except TimeoutError:
                        raise AssetCorpusError(
                            "capture_job_limit" if remaining <= 10 else "capture_exchange_limit"
                        ) from None
                    break
                if payload is None:
                    raise AssetCorpusError("source_redirect_limit")
                if not matches_original(payload, row["sha256"], row["bytes"]):
                    raise AssetCorpusError("source_digest_mismatch")
                descriptor, temporary = tempfile.mkstemp(prefix=".capture-", dir=cache_root)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(payload)
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
                result.update(status="captured", final_url=url)
            except AssetCorpusError as exc:
                result["reason"] = str(exc)
            except (httpx.HTTPError, OSError, ValueError):
                result["reason"] = "source_transport_error"
            results.append(result)
    finally:
        if own:
            await client.aclose()
    return results
