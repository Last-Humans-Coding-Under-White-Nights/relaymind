"""Loopback binding and the generated application token (RM19, spec 0.14 section 7.1).

Issue #22. The gateway binds only to localhost or a loopback address
literal (the rule of spec section 6, no DNS), and its token comes from a
secure random source and is long enough to resist guessing. Both are
refused at startup, never downgraded.
"""

import httpx
import pytest
from fastapi import FastAPI

from api_helpers import RunningServer, make_app, token_of
from gateway_helpers import build_gateway
from relaymind.adapters.local_http import is_loopback_host
from relaymind.api.app import MIN_TOKEN_LENGTH, create_app, generate_application_token
from relaymind.api.server import build_server, validate_bind_host

LOOPBACK = ["127.0.0.1", "127.0.0.2", "localhost", "LOCALHOST", "::1"]
NOT_LOOPBACK = [
    "0.0.0.0",
    "::",
    "192.168.1.5",
    "10.0.0.1",
    "example.com",
    "",
    "localhost.example.com",
    "127.0.0.1.nip.io",
    "local host",
]


@pytest.mark.parametrize("host", LOOPBACK)
def test_loopback_hosts_are_accepted(host):
    assert is_loopback_host(host)
    assert validate_bind_host(host) == host


def test_a_bracketed_ipv6_loopback_binds_without_its_brackets():
    """RM19: a host the validator accepts can be bound (spec 0.14 section 7.1).

    The brackets belong to URL syntax; the socket layer needs the bare
    address, so the validator returns it.
    """
    assert is_loopback_host("[::1]")
    assert validate_bind_host("[::1]") == "::1"


@pytest.mark.parametrize("host", NOT_LOOPBACK)
def test_other_hosts_are_refused_without_dns(host):
    assert not is_loopback_host(host)
    with pytest.raises(ValueError, match="loopback"):
        validate_bind_host(host)


def test_a_generated_token_is_long_unique_and_url_safe():
    tokens = {generate_application_token() for _ in range(50)}
    assert len(tokens) == 50
    for token in tokens:
        assert len(token) >= MIN_TOKEN_LENGTH
        assert token.replace("-", "").replace("_", "").isalnum()


def test_each_app_generates_its_own_token_at_startup(tmp_path):
    """RM19: the gateway generates the token; no caller supplies one (section 7.1)."""
    gateway = build_gateway(tmp_path, [])
    first = create_app(orchestrator=gateway.orchestrator)
    second = create_app(orchestrator=gateway.orchestrator)
    assert len(token_of(first)) >= MIN_TOKEN_LENGTH
    assert token_of(first) != token_of(second)


def test_a_caller_cannot_supply_the_token(tmp_path):
    """RM19: a low-entropy operator token never reaches the endpoints (section 7.1)."""
    gateway = build_gateway(tmp_path, [])
    with pytest.raises(TypeError):
        create_app(token="a" * MIN_TOKEN_LENGTH, orchestrator=gateway.orchestrator)


def test_the_server_binds_the_validated_loopback_host(tmp_path):
    app = make_app(build_gateway(tmp_path, []))
    server = build_server(app, host="127.0.0.1", port=0)
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 0


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "example.com"])
def test_the_server_refuses_a_public_bind_address(tmp_path, host):
    app = make_app(build_gateway(tmp_path, []))
    with pytest.raises(ValueError, match="loopback"):
        build_server(app, host=host, port=8080)


def test_asgi_wrapper_without_state_serves_normally():
    """RM19, RM33/AC24, RM38/AC29: optional warm-up preserves ASGI wrappers."""
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ready": True}

    async def wrapped(scope, receive, send):
        await app(scope, receive, send)

    with RunningServer(wrapped) as running:
        response = httpx.get(running.origin + "/health")
        assert response.status_code == 200
        assert response.json() == {"ready": True}
