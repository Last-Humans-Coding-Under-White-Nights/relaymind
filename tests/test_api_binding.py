"""Loopback binding and the generated application token (RM19, spec 0.14 section 7.1).

Issue #22. The gateway binds only to localhost or a loopback address
literal (the rule of spec section 6, no DNS), and its token comes from a
secure random source and is long enough to resist guessing. Both are
refused at startup, never downgraded.
"""

import pytest

from api_helpers import make_app
from gateway_helpers import build_gateway
from relaymind.adapters.local_http import is_loopback_host
from relaymind.api.app import MIN_TOKEN_LENGTH, create_app, generate_application_token
from relaymind.api.server import build_server, validate_bind_host

LOOPBACK = ["127.0.0.1", "127.0.0.2", "localhost", "LOCALHOST", "::1", "[::1]"]
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


@pytest.mark.parametrize("token", ["", "short", "x" * (MIN_TOKEN_LENGTH - 1), None, 12345])
def test_a_weak_token_is_refused_at_startup(tmp_path, token):
    gateway = build_gateway(tmp_path, [])
    with pytest.raises(ValueError, match="token"):
        create_app(token=token, orchestrator=gateway.orchestrator)


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
