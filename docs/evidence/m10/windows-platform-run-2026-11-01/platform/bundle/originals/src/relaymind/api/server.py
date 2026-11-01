"""Starting the gateway on a loopback address only (RM19, spec 0.14 section 7.1, section 3).

The bind address is checked before a server object exists, so a
public address is a startup error and never reaches a socket. The
rule is the one of spec section 6: ``localhost`` or an IPv4 or IPv6
loopback literal, with no DNS lookup.

A stream that fails after its first chunk ends with ``StreamAborted``,
the only way to close a started response. The gateway has already
logged that failure in one line (RM12), so the server does not log it
again as a crash with a traceback.
"""

import logging
from collections.abc import Callable

import uvicorn
from starlette.types import ASGIApp

from relaymind.adapters.local_http import is_loopback_host
from relaymind.api.chat import StreamAborted

__all__ = ["build_server", "serve", "validate_bind_host"]


def validate_bind_host(host: str) -> str:
    """Return the bindable host when it is a loopback address; otherwise refuse it.

    The brackets of an IPv6 literal belong to URL syntax, so ``[::1]``
    is returned as ``::1``, the form the socket layer binds.
    """
    if not is_loopback_host(host):
        raise ValueError(
            f"refusing to bind {host!r}: the gateway binds only to localhost or a loopback address"
        )
    return host.removeprefix("[").removesuffix("]")


class _ExpectedAborts(logging.Filter):
    """Drops the server's crash record of an aborted stream; every other record passes."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.exc_info and isinstance(record.exc_info[1], StreamAborted))


_EXPECTED_ABORTS = _ExpectedAborts()


class _StartedServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, on_started: Callable[[], None]):
        super().__init__(config)
        self._on_started = on_started

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            self._on_started()


def build_server(
    app: ASGIApp,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    on_started: Callable[[], None] | None = None,
) -> uvicorn.Server:
    """A uvicorn server for app on a validated loopback host.

    The app-owned warm-up starts after the listener is ready unless the caller
    supplies its own startup callback. Apps without an owner serve normally.
    """
    if on_started is None:
        startup_warmup = getattr(getattr(app, "state", None), "startup_warmup", None)
        if startup_warmup is not None:
            on_started = startup_warmup.start
    config = uvicorn.Config(app, host=validate_bind_host(host), port=port, log_level="warning")
    # After the config, which sets up the server loggers; adding the one filter twice is a no-op.
    logging.getLogger("uvicorn.error").addFilter(_EXPECTED_ABORTS)
    return uvicorn.Server(config) if on_started is None else _StartedServer(config, on_started)


def serve(
    app: ASGIApp,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    on_started: Callable[[], None] | None = None,
) -> None:
    """Run the gateway until it is stopped."""
    build_server(app, host=host, port=port, on_started=on_started).run()
