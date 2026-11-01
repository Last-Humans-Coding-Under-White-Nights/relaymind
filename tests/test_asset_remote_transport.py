"""RM03/RM12/RM38/RM43: bounded owned HTTP, fixed origins and secret isolation."""

import asyncio
from datetime import timedelta

import httpx
import pytest

from helpers import FakeClock
from relaymind.assets.sources import CaptureContext, SourceFailure
from test_asset_remote_contracts import PROFILE

API = "https://api.github.com/repos/LHCWN/relaymind"
RAW = "https://raw.githubusercontent.com/LHCWN/relaymind"


def make(handler, *, secret_ref=None, lookup=lambda key: None):
    from relaymind.assets.remote_contracts import resolve_remote_profile
    from relaymind.assets.remote_transport import RemoteTransport

    return RemoteTransport(
        resolve_remote_profile(PROFILE),
        secret_ref=secret_ref,
        credential_lookup=lookup,
        http_transport=httpx.MockTransport(handler),
    )


def context(clock=None):
    clock = clock or FakeClock()
    return CaptureContext(clock.utc_now(), monotonic=clock.monotonic, utc_now=clock.utc_now)


def test_public_original_bytes_and_api_only_auth_with_sanitized_headers():
    def handler(request):
        if request.url.host == "api.github.com":
            assert request.headers["Authorization"] == "Bearer PRIVATE-secret"
        else:
            assert "Authorization" not in request.headers
        return httpx.Response(
            200, stream=httpx.ByteStream(b"public bytes"), headers={"ETag": "PRIVATE-secret"}
        )

    ctx = context()
    with make(
        handler, secret_ref="env:FIXTURE_TOKEN", lookup=lambda key: "PRIVATE-secret"
    ) as transport:
        one = transport.get(
            API + "/git/ref/heads/master", context=ctx, max_bytes=100, api_auth=True
        )
        two = transport.get(
            RAW + "/" + "a" * 40 + "/skills/a/SKILL.md", context=ctx, max_bytes=100, api_auth=False
        )
    assert one.body == two.body == b"public bytes"
    assert one.headers["etag"] == "[redacted]"
    assert ctx.received_bytes == 24 and ctx.remote_request_count == 2
    assert ctx.remote_not_before_at == ctx.captured_at + timedelta(seconds=300)


@pytest.mark.parametrize("value", [None, "", "private\nsecret", "private\rsecret"])
def test_bad_credential_refuses_before_any_network(value):
    def forbidden(request):
        raise AssertionError("credential failure dispatched HTTP")

    ctx = context()
    with make(forbidden, secret_ref="env:FIXTURE_TOKEN", lookup=lambda _: value) as transport:
        with pytest.raises(SourceFailure, match="source_unavailable"):
            transport.get(API + "/git/ref/heads/master", context=ctx, max_bytes=100, api_auth=True)
    assert ctx.remote_request_count == 0


@pytest.mark.parametrize(
    "url",
    [
        API + "/../../elsewhere",
        API + "/contents/x",
        "http://api.github.com/repos/LHCWN/relaymind/git/ref/heads/master",
        "https://evil.test/a",
        "https://u:p@api.github.com/a",
        RAW + "/master/skills/a/SKILL.md",
        RAW + "/" + "a" * 40 + "/../private",
        API + "/git/trees/" + "a" * 40 + "?recursive=2",
    ],
)
def test_wrong_origin_or_protocol_path_never_dispatches(url):
    def forbidden(request):
        raise AssertionError("unsafe path dispatched")

    ctx = context()
    with make(forbidden) as transport:
        with pytest.raises(SourceFailure):
            transport.get(url, context=ctx, max_bytes=100, api_auth=True)
    assert ctx.remote_request_count == 0


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 404, 429, 500])
def test_http_failure_keeps_fixed_reason_and_never_retries(status):
    ctx = context()

    def handler(request):
        return httpx.Response(
            status, content=b"PRIVATE-error", headers={"Location": "https://evil.test/"}
        )

    with make(handler) as transport:
        with pytest.raises(SourceFailure, match="source_unavailable") as caught:
            transport.get(API + "/git/ref/heads/master", context=ctx, max_bytes=100, api_auth=True)
    assert ctx.remote_request_count == 1 and "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("limit", ["requests", "bytes"])
def test_whole_job_exhaustion_refuses_before_dispatch(limit):
    ctx = context()
    if limit == "requests":
        ctx.remote_request_count = 1000
    else:
        ctx.received_bytes = 100 * 1024 * 1024

    def forbidden(request):
        raise AssertionError("exhausted job dispatched request")

    with make(forbidden) as transport:
        with pytest.raises(SourceFailure):
            transport.get(API + "/git/ref/heads/master", context=ctx, max_bytes=100, api_auth=True)
    assert ctx.remote_request_count == (1000 if limit == "requests" else 0)


@pytest.mark.parametrize("announced", [False, True])
def test_announced_and_chunked_body_limits_are_enforced(announced):
    def handler(request):
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"x" * 101),
            headers={"Content-Length": "101"} if announced else {},
        )

    ctx = context()
    with make(handler) as transport:
        with pytest.raises(SourceFailure, match="size_limit"):
            transport.get(API + "/git/ref/heads/master", context=ctx, max_bytes=100, api_auth=True)
    assert ctx.remote_request_count == 1


def test_failed_response_rate_limit_is_retained_and_stops_another_request():
    clock = FakeClock()
    ctx = context(clock)
    reset = int(clock.utc_now().timestamp()) + 7200

    def handler(request):
        return httpx.Response(
            429,
            headers={
                "Retry-After": "4000",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset),
            },
        )

    with make(handler) as transport:
        for _ in range(2):
            with pytest.raises(SourceFailure):
                transport.get(
                    API + "/git/ref/heads/master", context=ctx, max_bytes=100, api_auth=True
                )
    assert ctx.remote_request_count == 1
    assert ctx.remote_not_before_at == clock.utc_now() + timedelta(seconds=7200)


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Poll-Interval": "-1"},
        {"Retry-After": "tomorrow"},
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "invalid"},
    ],
)
def test_malformed_mandatory_headers_fail_closed(headers):
    ctx = context()
    with make(lambda request: httpx.Response(200, content=b"ok", headers=headers)) as transport:
        with pytest.raises(SourceFailure, match="invalid_data"):
            transport.get(API + "/git/ref/heads/master", context=ctx, max_bytes=100, api_auth=True)


def test_cancellation_during_body_closes_native_response_and_returns_no_partial_body():
    closed = []
    cancelled = False

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal cancelled
            yield b"first"
            cancelled = True
            await asyncio.sleep(1)
            yield b"late"

        async def aclose(self):
            closed.append(True)

    ctx = context()
    ctx.cancelled = lambda: cancelled
    with make(lambda request: httpx.Response(200, stream=Stream())) as transport:
        with pytest.raises(SourceFailure, match="cancelled"):
            transport.get(API + "/git/ref/heads/master", context=ctx, max_bytes=100, api_auth=True)
    assert closed and ctx.received_bytes <= 5


def test_cancelled_small_body_retains_already_received_bytes_exactly():
    """RM16/RM43: a cancelled partial body is not an observed zero-byte exchange."""
    cancelled = False

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal cancelled
            yield b"first"
            cancelled = True
            await asyncio.sleep(1)

    ctx = context()
    ctx.cancelled = lambda: cancelled
    with make(lambda request: httpx.Response(200, stream=Stream())) as transport:
        with pytest.raises(SourceFailure, match="cancelled"):
            transport.get(API + "/git/ref/heads/master", context=ctx, max_bytes=100, api_auth=True)
    assert ctx.received_bytes == 5


def test_actual_socket_slow_headers_and_body_keep_job_deadline_and_close():
    """RM03/RM38: real socket waits consume the unchanged owned deadline."""
    import time
    from datetime import UTC, datetime

    from relaymind.assets.remote_contracts import resolve_remote_profile
    from relaymind.assets.remote_transport import RemoteTransport
    from remote_source_fixture import ForwardToFixture, server

    for slow_headers in (False, True):

        def reply(path, slow_headers=slow_headers):
            if slow_headers:
                time.sleep(0.2)

            def chunks():
                yield b"first"
                time.sleep(0.2)
                yield b"late"

            return 200, {}, chunks()

        with server(reply) as port:
            ctx = CaptureContext(datetime.now(UTC))
            ctx.deadline = ctx.monotonic() + 0.05
            with RemoteTransport(
                resolve_remote_profile(PROFILE), http_transport=ForwardToFixture(port)
            ) as transport:
                with pytest.raises(SourceFailure, match="deadline"):
                    transport.get(
                        API + "/git/ref/heads/master", context=ctx, max_bytes=100, api_auth=True
                    )
            # A loaded host may hit its deadline before the first socket read;
            # never invent bytes merely because the fixture sent them. The
            # deterministic stream test separately pins observed partial bytes.
            assert ctx.received_bytes in (0, 5)
            if slow_headers:
                assert ctx.received_bytes == 0


def test_unencodable_bearer_is_safe_and_does_not_consume_a_dispatch():
    """RM12/RM43: invalid credential header preparation never reaches HTTP or public errors."""
    ctx = context()

    def forbidden(request):
        raise AssertionError("invalid header reached transport")

    with make(
        forbidden, secret_ref="env:FIXTURE_TOKEN", lookup=lambda _: "秘密-token"
    ) as transport:
        with pytest.raises(SourceFailure, match="source_unavailable"):
            transport.get(API + "/git/ref/heads/master", context=ctx, max_bytes=100, api_auth=True)
    assert ctx.remote_request_count == 0


def test_remaining_job_bytes_must_admit_response_budget_before_dispatch():
    """RM03/RM43: a positive remaining balance does not authorize an over-budget exchange."""
    ctx = context()
    ctx.received_bytes = 100 * 1024 * 1024 - 50
    with make(lambda request: httpx.Response(200, stream=httpx.ByteStream(b"ok"))) as transport:
        with pytest.raises(SourceFailure, match="size_limit"):
            transport.get(API + "/git/ref/heads/master", context=ctx, max_bytes=100, api_auth=True)
    assert ctx.remote_request_count == 0
