"""Owned bounded HTTP inside the sole capture worker (RM03/RM12/RM38/RM43)."""

import asyncio
import hashlib
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Self
from urllib.parse import unquote, urlsplit

import httpx

from relaymind.assets.catalog import MAX_JOB_RECEIVED_BYTES, READ_BUFFER_BYTES
from relaymind.assets.remote_contracts import RemoteSourceProfile, validate_secret_ref
from relaymind.assets.sources import CaptureContext, SourceFailure
from relaymind.schema_util import freeze_json


@dataclass(frozen=True)
class RemoteResponse:
    status: int
    body: bytes
    sha256: str
    headers: Mapping[str, str]
    not_before: datetime | None


class RemoteTransport:
    """A job-local connection owner; constructor doubles never come from launch config."""

    def __init__(
        self,
        profile: RemoteSourceProfile,
        *,
        secret_ref: str | None = None,
        credential_lookup: Callable[[str], str | None] = os.environ.get,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        validate_secret_ref(secret_ref)
        self.profile, self.secret_ref, self.lookup = profile, secret_ref, credential_lookup
        self.http_transport = http_transport
        self._blocked_until = None
        self._ref_not_before = None
        self._last_context = None

    def __enter__(self) -> Self:
        self._runner = asyncio.Runner()
        self._client = httpx.AsyncClient(
            transport=self.http_transport,
            trust_env=False,
            follow_redirects=False,
            timeout=10.0,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": self.profile.api_version,
                "Accept-Encoding": "identity",
                "User-Agent": "RelayMind-qualified-source",
            },
        )
        return self

    def __exit__(self, *args: object) -> None:
        try:
            if self._last_context is None:
                self._runner.run(self._client.aclose())
            else:
                self._last_context.io(self._runner.run, self._client.aclose(), resume=False)
        finally:
            self._runner.close()

    def _url(self, url: str, api_auth: bool) -> bool:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            raise SourceFailure("invalid_data")
        base = "/repos/" + self.profile.repository
        ref = parsed.path == base + "/git/ref/" + self.profile.ref
        valid_api = ref or re.fullmatch(
            re.escape(base) + r"/git/(commits|trees)/[0-9a-f]{40}", parsed.path
        )
        if parsed.hostname == "api.github.com" and valid_api:
            if parsed.query and not (
                parsed.path.startswith(base + "/git/trees/") and parsed.query == "recursive=1"
            ):
                raise SourceFailure("invalid_data")
            return bool(ref)
        prefix = "/" + self.profile.repository + "/"
        if (
            parsed.hostname == "raw.githubusercontent.com"
            and parsed.path.startswith(prefix)
            and not api_auth
        ):
            tail = parsed.path[len(prefix) :]
            commit, separator, path = tail.partition("/")
            decoded = unquote(path)
            if (
                separator
                and re.fullmatch("[0-9a-f]{40}", commit)
                and not parsed.query
                and "\\" not in decoded
                and not any(p in ("", ".", "..") for p in decoded.split("/"))
                and (decoded == "LICENSE" or decoded.startswith("skills/"))
            ):
                return False
        raise SourceFailure("invalid_data")

    @staticmethod
    def _defer(context: CaptureContext, when: datetime) -> None:
        if context.remote_not_before_at is None or when > context.remote_not_before_at:
            context.remote_not_before_at = when
            if context.defer_remote is not None:
                context.defer_remote(when)

    def _headers(self, response, context):
        now = context.utc_now()

        def seconds(value):
            if not re.fullmatch(r"[0-9]{1,18}", value):
                raise ValueError
            return int(value)

        try:
            interval = response.headers.get("x-poll-interval")
            if interval is not None:
                self._defer(context, now + timedelta(seconds=seconds(interval)))
            retry = response.headers.get("retry-after")
            cooldown = None
            if retry is not None:
                cooldown = (
                    now + timedelta(seconds=seconds(retry))
                    if retry.isdecimal()
                    else parsedate_to_datetime(retry)
                )
                if cooldown.tzinfo is None:
                    raise ValueError
            remaining = response.headers.get("x-ratelimit-remaining")
            if remaining is not None and seconds(remaining) == 0:
                reset = response.headers.get("x-ratelimit-reset")
                if reset is None:
                    raise ValueError
                instant = datetime.fromtimestamp(seconds(reset), UTC)
                cooldown = instant if cooldown is None else max(cooldown, instant)
            if cooldown is not None:
                self._defer(context, cooldown)
                self._blocked_until = (
                    cooldown if self._blocked_until is None else max(self._blocked_until, cooldown)
                )
        except (ValueError, TypeError, OverflowError, OSError):
            raise SourceFailure("invalid_data") from None
        allowed = (
            "etag",
            "x-poll-interval",
            "retry-after",
            "x-ratelimit-remaining",
            "x-ratelimit-reset",
        )
        result = {key: response.headers[key] for key in allowed if key in response.headers}
        for key, value in result.items():
            for secret in context.secrets:
                value = value.replace(secret, "[redacted]")
            result[key] = value
        return freeze_json(result)

    @staticmethod
    async def _await_io(awaitable, context, exchange_deadline):
        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                # Native waiting does not acquire a CPU window or mark foreground
                # contention. Only stopped/deadline facts apply until I/O returns.
                if context.cancelled():
                    raise SourceFailure("cancelled", rejected_count=0)
                now = context.monotonic()
                if now >= context.deadline:
                    raise SourceFailure("deadline")
                if now >= exchange_deadline:
                    raise SourceFailure("source_unavailable")
                if task.done():
                    return task.result()
                await asyncio.wait((task,), timeout=min(0.025, exchange_deadline - now))
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _network(self, awaitable, context, deadline):
        return context.io(
            self._runner.run, self._await_io(awaitable, context, deadline), resume=False
        )

    def get(
        self, url: str, *, context: CaptureContext, max_bytes: int, api_auth: bool
    ) -> RemoteResponse:
        is_ref = self._url(url, api_auth)
        context.check()
        if type(max_bytes) is not int or not 0 < max_bytes <= 10 * READ_BUFFER_BYTES:
            raise SourceFailure("size_limit")
        if context.remote_request_count >= 1000:
            raise SourceFailure("request_limit")
        if max_bytes > MAX_JOB_RECEIVED_BYTES - context.received_bytes:
            raise SourceFailure("size_limit")
        now = context.utc_now()
        if (self._blocked_until is not None and now < self._blocked_until) or (
            is_ref and self._ref_not_before is not None and now < self._ref_not_before
        ):
            raise SourceFailure("source_unavailable")
        headers = {}
        if self.secret_ref is not None:
            secret = self.lookup(self.secret_ref[4:])
            if not isinstance(secret, str) or not secret or "\r" in secret or "\n" in secret:
                raise SourceFailure("source_unavailable")
            context.secrets = context.secrets | {secret}
            if api_auth and urlsplit(url).hostname == "api.github.com":
                headers["Authorization"] = "Bearer " + secret
        try:
            request = self._client.build_request("GET", url, headers=headers)
        except (UnicodeError, ValueError):
            raise SourceFailure("source_unavailable") from None
        if is_ref:
            self._ref_not_before = now + timedelta(seconds=self.profile.minimum_poll_seconds)
            self._defer(context, self._ref_not_before)
        context.remote_request_count += 1
        self._last_context = context
        deadline = min(context.deadline, context.monotonic() + 10)
        response = None
        try:
            response = self._network(self._client.send(request, stream=True), context, deadline)
            # Ordinary header parsing/persistence reacquires the same CPU gate
            # after native waiting; foreground work must not race a new batch.
            try:
                context.check()
            except SourceFailure:
                # Available mandatory hints still need a bounded cleanup flush
                # after a stop. This admits no HTTP or projection/publication.
                try:
                    self._headers(response, context)
                except SourceFailure:
                    pass  # Preserve the original cancellation/deadline reason.
                raise
            safe_headers = self._headers(response, context)
            context.check()
            if context.monotonic() >= deadline:
                raise SourceFailure("source_unavailable")
            if response.status_code != 200:
                raise SourceFailure("source_unavailable")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise SourceFailure("invalid_data")
            announced = response.headers.get("content-length")
            if announced is not None:
                if re.fullmatch(r"[0-9]{1,18}", announced) is None:
                    raise SourceFailure("invalid_data")
                if int(announced) > min(max_bytes, MAX_JOB_RECEIVED_BYTES - context.received_bytes):
                    raise SourceFailure("size_limit")
            body = bytearray()
            digest = hashlib.sha256()
            # Preserve each native read immediately, including small partial
            # bodies. Filling a 1MiB application chunk can hide received bytes
            # when cancellation arrives before that chunk fills.
            iterator = response.aiter_raw()
            while True:
                try:
                    chunk = self._network(iterator.__anext__(), context, deadline)
                except StopAsyncIteration:
                    break
                # Receipt is an observed scalar even when the owner has just
                # stopped. Do not admit parsing/hash work after its fence.
                context.received_bytes += len(chunk)
                context.check()
                if (
                    len(chunk) > READ_BUFFER_BYTES
                    or context.received_bytes > MAX_JOB_RECEIVED_BYTES
                    or len(body) + len(chunk) > max_bytes
                ):
                    raise SourceFailure("size_limit")
                body.extend(chunk)
                digest.update(chunk)
            context.check()
            if context.monotonic() >= deadline:
                raise SourceFailure("source_unavailable")
            return RemoteResponse(
                response.status_code,
                bytes(body),
                digest.hexdigest(),
                safe_headers,
                context.remote_not_before_at,
            )
        except httpx.HTTPError:
            raise SourceFailure("source_unavailable") from None
        finally:
            if response is not None:
                context.io(self._runner.run, response.aclose(), resume=False)
