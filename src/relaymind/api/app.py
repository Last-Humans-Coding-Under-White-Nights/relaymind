"""The HTTP app for the compatible endpoints (RM19, RM20, spec 0.14 section 7.1).

``create_app`` serves GET /v1/models and POST /v1/chat/completions over
one orchestrator. The order of checks is fixed: the application token
(401), the body (400), the Idempotency-Key header (400), then the
execution mode header (400, then 403). Everything is checked before a
run exists, so a rejected request has no run. Errors use the contract of
``relaymind.api.errors``; an unexpected exception becomes a fixed
internal_error with no detail (RM12).

The app holds no model, tool, or store logic: the orchestrator owns it
(section 3).
"""

import functools
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from relaymind.api.auth import require_bearer_token
from relaymind.api.chat import ChatBackend, ManagedChat, completion_body, stream_chunks
from relaymind.api.chat_request import (
    CLIENT_TOOLS,
    MANAGED,
    SAMPLING_OPTIONS,
    parse_chat_request,
)
from relaymind.api.errors import ApiError, error_from_exception
from relaymind.orchestrator.local_loop import LocalOrchestrator
from relaymind.orchestrator.route_decision import ExecutionMode

__all__ = ["MIN_TOKEN_LENGTH", "ChatSettings", "create_app", "generate_application_token"]

RUN_ID_HEADER = "X-RelayMind-Run-ID"
MODE_HEADER = "X-RelayMind-Execution-Mode"
IDEMPOTENCY_HEADER = "Idempotency-Key"
MIN_TOKEN_LENGTH = 32


def generate_application_token() -> str:
    """A fresh application token from a cryptographically secure source (section 3)."""
    return secrets.token_urlsafe(MIN_TOKEN_LENGTH)


@dataclass(frozen=True)
class ChatSettings:
    """Operator policy for the chat endpoint.

    allowed_modes are the execution modes the operator permits (the
    same policy as the native run API); supported_options declares
    which of max_output_tokens, temperature, and top_p the configured
    adapters accept (spec 0.14 section 7.1).
    """

    allowed_modes: frozenset[ExecutionMode] = frozenset(ExecutionMode)
    supported_options: frozenset[str] = SAMPLING_OPTIONS


def create_app(
    *,
    token: str,
    orchestrator: LocalOrchestrator,
    client_tools: ChatBackend | None = None,
    settings: ChatSettings | None = None,
) -> FastAPI:
    """The compatible API over one orchestrator.

    client_tools serves the relaymind-client-tools alias; without it the
    alias is neither listed nor accepted, so the gateway never offers
    what it cannot run. A token shorter than MIN_TOKEN_LENGTH is
    refused at startup.
    """
    if not isinstance(token, str) or len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"the application token needs at least {MIN_TOKEN_LENGTH} characters")
    settings = settings or ChatSettings()
    backends: dict[str, ChatBackend] = {MANAGED: ManagedChat(orchestrator)}
    if client_tools is not None:
        backends[CLIENT_TOOLS] = client_tools
    started = int(time.time())
    # No interactive docs: they would be an unauthenticated surface.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/v1/models")
    @_guarded
    async def models(request: Request) -> Response:
        require_bearer_token(request.headers.get("authorization"), token)
        data = [
            {"id": alias, "object": "model", "created": started, "owned_by": "relaymind"}
            for alias in backends
        ]
        return JSONResponse({"object": "list", "data": data})

    @app.post("/v1/chat/completions")
    @_guarded
    async def completions(request: Request) -> Response:
        require_bearer_token(request.headers.get("authorization"), token)
        body = _decode(await request.body())
        chat = parse_chat_request(
            body, served=backends, supported_options=settings.supported_options
        )
        if IDEMPOTENCY_HEADER in request.headers:
            raise ApiError(
                "unsupported_parameter", f"the {IDEMPOTENCY_HEADER} header is not supported yet"
            )
        mode = _execution_mode(request.headers.get(MODE_HEADER), chat.alias, settings)
        result = await backends[chat.alias].complete(chat, mode)
        headers = {RUN_ID_HEADER: result.run_id}
        created = int(time.time())
        if chat.stream:
            return StreamingResponse(
                stream_chunks(chat.alias, result, created),
                media_type="text/event-stream",
                headers={**headers, "Cache-Control": "no-cache"},
            )
        return JSONResponse(completion_body(chat.alias, result, created), headers=headers)

    return app


_Handler = Callable[[Request], Awaitable[Response]]


def _guarded(handler: _Handler) -> _Handler:
    """Every response of the route follows the error contract (section 11)."""

    @functools.wraps(handler)
    async def wrapper(request: Request) -> Response:
        try:
            return await handler(request)
        except Exception as exc:
            return _error_response(error_from_exception(exc))

    return wrapper


def _decode(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        raise ApiError("invalid_request", "the request body must be valid JSON") from None


def _execution_mode(header: str | None, alias: str, settings: ChatSettings) -> ExecutionMode:
    """The effective mode from the header: absent is auto, never downgraded (section 7.1)."""
    if header is None:
        mode = ExecutionMode.AUTO
    else:
        try:
            mode = ExecutionMode(header)
        except ValueError:
            raise ApiError(
                "invalid_execution_mode",
                f"{MODE_HEADER} must be one of: {', '.join(m.value for m in ExecutionMode)}",
            ) from None
    if mode is ExecutionMode.PARALLEL_DEMO and alias == CLIENT_TOOLS:
        raise ApiError(
            "invalid_execution_mode",
            f"parallel_demo is not admissible with the {CLIENT_TOOLS} alias",
        )
    if mode not in settings.allowed_modes:
        raise ApiError(
            "execution_mode_forbidden", f"the execution mode {mode.value} is not permitted"
        )
    return mode


def _error_response(error: ApiError) -> JSONResponse:
    headers: dict[str, str] = {}
    if error.run_id is not None:
        headers[RUN_ID_HEADER] = error.run_id
    if error.code == "unauthenticated":
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(error.body(), status_code=error.status, headers=headers)
