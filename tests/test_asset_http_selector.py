"""RM35/AC26: strict v2 HTTP boundaries; no credential/transport state inheritance."""

import asyncio
import gzip
import json
import threading

import httpx
import pytest

from helpers import load_script_module
from test_asset_selection import api, request, result


class Bytes(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks, self.error = chunks, error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


def run_http(*, body=None, headers=None, status=200, error=None):
    from relaymind.selection.asset_http import HttpAssetSelector

    a = api()
    body = json.dumps(result()).encode() if body is None else body
    stream = Bytes([body], error)
    seen = []

    def factory():
        async def respond(req):
            seen.append(req)
            assert req.headers["Content-Type"] == "application/json"
            assert req.headers["Accept"] == "application/json"
            assert "authorization" not in req.headers and "cookie" not in req.headers
            assert json.loads(req.content) == request()
            return httpx.Response(
                status,
                headers=headers if headers is not None else {"Content-Type": "application/json"},
                stream=stream,
            )

        return httpx.MockTransport(respond)

    selector = HttpAssetSelector(
        name="custom", version="1", url="http://127.0.0.1:8123/select", transport_factory=factory
    )
    context = a.SelectorContext(lambda: 0, lambda: None)
    return selector, a.parse_asset_request(request()), context, stream, seen


@pytest.mark.parametrize(
    "media",
    [
        "Application/JSON; Charset=UTF-8",
        'application/json; charset="UTF-8"',
        'application/json ; \tcharset="utf-8"\t',
    ],
)
def test_success_and_exact_full_offer_http_payload(monkeypatch, media):
    monkeypatch.setenv("HTTP_PROXY", "http://secret@outside.invalid")
    selector, req, ctx, stream, seen = run_http(headers={"Content-Type": media})
    assert asyncio.run(selector.select(req, ctx)) == result()
    assert stream.closed and len(seen) == 1


@pytest.mark.parametrize(
    "headers,body,status,outcome,reason",
    [
        ({}, b"{}", 200, "invalid", "response_media_type"),
        ({"Content-Type": "text/event-stream"}, b"{}", 200, "invalid", "response_media_type"),
        ({"Content-Type": "application/x-ndjson"}, b"{}", 200, "invalid", "response_media_type"),
        (
            {"Content-Type": "application/json; charset=latin-1"},
            b"{}",
            200,
            "invalid",
            "response_media_type",
        ),
        ({"Content-Type": "application/json; x=1"}, b"{}", 200, "invalid", "response_media_type"),
        (
            {"Content-Type": 'application/json; charset = "utf-8"'},
            json.dumps(result()).encode(),
            200,
            "invalid",
            "response_media_type",
        ),
        (
            {"Content-Type": "application/json; charset\t=\tutf-8"},
            json.dumps(result()).encode(),
            200,
            "invalid",
            "response_media_type",
        ),
        (
            {"Content-Type": 'application/json; charset="u tf-8"'},
            json.dumps(result()).encode(),
            200,
            "invalid",
            "response_media_type",
        ),
        (
            {"Content-Type": "application/json; char set=utf-8"},
            json.dumps(result()).encode(),
            200,
            "invalid",
            "response_media_type",
        ),
        (
            {"Content-Type": "application/json; charset=utf-8; charset=utf-8"},
            b"{}",
            200,
            "invalid",
            "response_media_type",
        ),
        ({"Content-Type": "application/json"}, b"\xff", 200, "invalid", "invalid_result"),
        ({"Content-Type": "application/json"}, b"{}{}", 200, "invalid", "invalid_result"),
        ({"Content-Type": "application/json"}, b'{"a":1,"a":2}', 200, "invalid", "invalid_result"),
        (
            {"Content-Type": "application/json"},
            b'{"elapsed_ms":NaN}',
            200,
            "invalid",
            "invalid_result",
        ),
        ({"Location": "http://outside.invalid"}, b"credential", 302, "unavailable", "http_status"),
        ({}, b"credential", 429, "unavailable", "http_status"),
        ({}, b"credential", 503, "unavailable", "http_status"),
        (
            {"Content-Type": "application/json"},
            b"x" * (1024 * 1024 + 1),
            200,
            "invalid",
            "response_too_large",
        ),
        (
            {"Content-Type": "application/json", "Content-Encoding": "gzip"},
            gzip.compress(b"x" * (1024 * 1024 + 1)),
            200,
            "invalid",
            "response_too_large",
        ),
    ],
)
def test_fixed_safe_http_classifications(headers, body, status, outcome, reason):
    selector, req, ctx, stream, seen = run_http(body=body, headers=headers, status=status)
    with pytest.raises(api().AttemptFailure) as caught:
        asyncio.run(selector.select(req, ctx))
    assert (caught.value.outcome, caught.value.reason, caught.value.result) == (
        outcome,
        reason,
        None,
    )
    assert "credential" not in str(caught.value)
    assert stream.closed and len(seen) == 1


@pytest.mark.parametrize(
    "error", [httpx.ReadError("secret reset"), httpx.RemoteProtocolError("secret incomplete")]
)
def test_reset_during_body_is_transport_error_not_malformed_json(error):
    selector, req, ctx, stream, _ = run_http(body=b'{"unfinished":', error=error)
    with pytest.raises(api().AttemptFailure) as caught:
        asyncio.run(selector.select(req, ctx))
    assert (caught.value.outcome, caught.value.reason) == ("unavailable", "transport_error")
    assert stream.closed


def test_fresh_clients_do_not_replay_cookies_or_retry():
    from relaymind.selection.asset_http import HttpAssetSelector

    made = []

    def factory():
        async def respond(req):
            assert "cookie" not in req.headers
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json", "Set-Cookie": "secret=1"},
                stream=Bytes([json.dumps(result()).encode()]),
            )

        transport = httpx.MockTransport(respond)
        made.append(transport)
        return transport

    async def run():
        selector = HttpAssetSelector(
            name="custom",
            version="1",
            url="http://localhost:8123/select",
            transport_factory=factory,
        )
        for _ in range(2):
            await selector.select(
                api().parse_asset_request(request()), api().SelectorContext(lambda: 0, lambda: None)
            )

    asyncio.run(run())
    assert len(made) == 2 and made[0] is not made[1]


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/select",
        "http://outside.invalid/select",
        "http://u:p@127.0.0.1/select",
        "http://127.0.0.1:0/select",
        "http://localhost:99999/select",
        "http://localhost/select?q=x",
        "http://localhost/#x",
    ],
)
def test_static_url_refusal_does_not_connect(url):
    from relaymind.selection.asset_http import HttpAssetSelector

    with pytest.raises(ValueError):
        HttpAssetSelector(name="custom", version="1", url=url)


def test_disposable_loopback_example_uses_same_lexical_contract():
    module = load_script_module("example_asset_selector")
    server = module.make_server("127.0.0.1", 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        from relaymind.selection.asset_http import HttpAssetSelector

        selector = HttpAssetSelector(
            name="example_lexical", version="1", url=f"http://127.0.0.1:{server.server_port}/select"
        )
        data = asyncio.run(
            selector.select(
                api().parse_asset_request(request()), api().SelectorContext(lambda: 0, lambda: None)
            )
        )
        assert (
            data["decision"] == "choice_required"
            and data["candidates"][0]["asset_id"] == "example:hello"
        )
        assert data["mechanism"] == "example_lexical_overlap"
        with httpx.Client(trust_env=False) as client:
            assert client.get(f"http://127.0.0.1:{server.server_port}/select").status_code == 405
            assert (
                client.post(
                    f"http://127.0.0.1:{server.server_port}/wrong", content=b"{}"
                ).status_code
                == 404
            )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_cancel_pending_headers_and_body_closes_owned_transport():
    """RM35/AC26: HTTP aborts during either phase; no fallback is dispatched."""
    from relaymind.selection.asset_chain import (
        RecommendationFailure,
        SelectionBudget,
        select_assets,
    )
    from relaymind.selection.asset_http import HttpAssetSelector
    from test_asset_selection import snapshot

    async def run(body_phase):
        entered = asyncio.Event()
        closed = asyncio.Event()

        class Pending(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b"{}"

            async def aclose(self):
                closed.set()

        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, req):
                if not body_phase:
                    entered.set()
                    await asyncio.Event().wait()
                return httpx.Response(
                    200, headers={"Content-Type": "application/json"}, stream=Pending()
                )

            async def aclose(self):
                closed.set()

        budget = SelectionBudget()
        selector = HttpAssetSelector(
            name="custom",
            version="1",
            url="http://localhost:8913/select",
            transport_factory=Transport,
        )
        task = asyncio.create_task(
            select_assets(
                api().parse_asset_request(request()),
                primary=selector,
                lexical_floor=None,
                budget=budget,
                primary_ms=150,
                fallback_enabled=True,
                current_catalog=lambda: snapshot(),
                policy=lambda r, q: True,
                trusted_scope={"user_id": "u", "workspace_id": "w"},
                trusted_permissions=["assets.read"],
            )
        )
        await entered.wait()
        budget.cancel()
        with pytest.raises(RecommendationFailure) as caught:
            await asyncio.wait_for(task, 0.3)
        await asyncio.wait_for(closed.wait(), 0.3)
        assert caught.value.code == "cancelled" and len(caught.value.attempts) == 1
        assert caught.value.attempts[0]["outcome"] == "cancelled"

    asyncio.run(run(False))
    asyncio.run(run(True))


def test_connect_and_timeout_phase_classification():
    """RM35/AC26: transport text is never exposed; phase timeout has owned outcome."""
    from relaymind.selection.asset_http import HttpAssetSelector

    async def run(error, outcome, reason):
        async def fail(req):
            raise error

        selector = HttpAssetSelector(
            name="custom",
            version="1",
            url="http://localhost:8913/select",
            transport_factory=lambda: httpx.MockTransport(fail),
        )
        with pytest.raises(api().AttemptFailure) as caught:
            await selector.select(
                api().parse_asset_request(request()), api().SelectorContext(lambda: 0, lambda: None)
            )
        assert (caught.value.outcome, caught.value.reason) == (outcome, reason)

    asyncio.run(run(httpx.ConnectError("secret refused"), "unavailable", "transport_error"))
    asyncio.run(run(httpx.ReadTimeout("secret timeout"), "timeout", "deadline_exceeded"))


def test_example_binding_never_performs_reverse_hostname_lookup(monkeypatch):
    """RM35/AC26: loopback example binding is local, with no reverse DNS lookup."""
    import socket

    module = load_script_module("example_asset_selector")

    def forbidden(*args):
        pytest.fail("loopback binding performed hostname lookup")

    monkeypatch.setattr(socket, "getfqdn", forbidden)
    server = module.make_server("127.0.0.1", 0)
    server.server_close()


@pytest.mark.parametrize("stop", ["primary_deadline", "total_deadline", "cancel"])
def test_client_setup_stop_prevents_post_and_closes_transport(stop):
    """RM35/AC26: entering a fresh client consumes the same clock and cancellation."""
    from relaymind.selection.asset_chain import RecommendationFailure, SelectionBudget
    from relaymind.selection.asset_http import HttpAssetSelector
    from test_asset_selection import FakeClock, coordinate

    async def run():
        clock = FakeClock()
        budget = SelectionBudget(total_ms=500, clock=clock)
        seen, closed = [], []

        async def respond(req):
            seen.append(req)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                stream=Bytes([json.dumps(result()).encode()]),
            )

        class CountedTransport(httpx.MockTransport):
            async def aclose(self):
                closed.append(True)
                await super().aclose()

        def factory():
            if stop == "cancel":
                budget.cancel()
            else:
                clock.seconds = 0.160 if stop == "primary_deadline" else 0.510
            return CountedTransport(respond)

        selector = HttpAssetSelector(
            name="custom",
            version="1",
            url="http://127.0.0.1:8123/select",
            transport_factory=factory,
        )
        with pytest.raises(RecommendationFailure) as caught:
            await coordinate(selector, clock=clock, budget=budget, fallback=stop == "cancel")
        assert seen == []
        assert closed == [True]
        expected_code = {
            "primary_deadline": "recommendation_unavailable",
            "total_deadline": "deadline_exceeded",
            "cancel": "cancelled",
        }[stop]
        assert caught.value.code == expected_code
        assert len(caught.value.attempts) == 1
        assert caught.value.attempts[0]["outcome"] == (
            "cancelled" if stop == "cancel" else "timeout"
        )
        assert caught.value.attempts[0]["result"] is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "error",
    [httpx.InvalidURL("secret url"), httpx.StreamConsumed(), httpx.CookieConflict("secret")],
)
def test_every_httpx_failure_is_a_transport_attempt_failure(error):
    """RM35/AC26: httpx errors outside HTTPError still allow lexical fallback."""
    from relaymind.selection.asset_http import HttpAssetSelector

    a = api()

    def factory():
        async def respond(req):
            raise error

        return httpx.MockTransport(respond)

    selector = HttpAssetSelector(
        name="custom", version="1", url="http://127.0.0.1:8123/select", transport_factory=factory
    )
    with pytest.raises(a.AttemptFailure) as caught:
        asyncio.run(
            selector.select(
                a.parse_asset_request(request()), a.SelectorContext(lambda: 0, lambda: None)
            )
        )
    assert (caught.value.outcome, caught.value.reason) == ("unavailable", "transport_error")


def test_http_selector_decodes_and_leaves_schema_admission_to_the_gateway():
    """RM35/AC26: the selector proposes; the chain's single parse decides validity."""
    for body, decoded in ((b"[]", []), (b"{}", {})):
        selector, req, ctx, _, _ = run_http(body=body)
        assert asyncio.run(selector.select(req, ctx)) == decoded
        with pytest.raises(api().AssetSelectionError):
            api().parse_asset_result(decoded)
