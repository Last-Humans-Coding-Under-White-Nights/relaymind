"""Starting the gateway on a loopback address only (RM19, spec 0.14 section 7.1, section 3).

The bind address is checked before a server object exists, so a
public address is a startup error and never reaches a socket. The
rule is the one of spec section 6: ``localhost`` or an IPv4 or IPv6
loopback literal, with no DNS lookup.
"""

import uvicorn
from fastapi import FastAPI

from relaymind.adapters.local_http import is_loopback_host

__all__ = ["build_server", "serve", "validate_bind_host"]


def validate_bind_host(host: str) -> str:
    """Return host when it is a loopback address; otherwise refuse it."""
    if not is_loopback_host(host):
        raise ValueError(
            f"refusing to bind {host!r}: the gateway binds only to localhost or a loopback address"
        )
    return host


def build_server(app: FastAPI, *, host: str = "127.0.0.1", port: int = 8080) -> uvicorn.Server:
    """A uvicorn server for app on a validated loopback host."""
    config = uvicorn.Config(app, host=validate_bind_host(host), port=port, log_level="warning")
    return uvicorn.Server(config)


def serve(app: FastAPI, *, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run the gateway until it is stopped."""
    build_server(app, host=host, port=port).run()
