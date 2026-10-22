"""The HTTP app for the compatible endpoints (RM19, RM20, spec 0.14 section 7.1).

``create_app`` serves GET /v1/models and POST /v1/chat/completions over
one orchestrator. The order of checks is fixed: the application token
(401), the body (400), the Idempotency-Key header (400), then the
execution mode header (400, then 403). Everything is checked before a
run exists, so a rejected request has no run. Errors use the contract of
``relaymind.api.errors``; an unexpected exception becomes a fixed
internal_error with no detail (RM12); the operator log names its type
and where it was raised, never its text.

The app holds no model, tool, or store logic: the orchestrator owns it
(section 3).
"""

import functools
import json
import logging
import secrets
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from relaymind.api.auth import require_bearer_token
from relaymind.api.chat import ChatBackend, ManagedChat, completion_body, stream_chunks
from relaymind.api.chat_request import (
    CLIENT_TOOLS,
    MANAGED,
    MAX_COMPLETION_TOKENS,
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

logger = logging.getLogger(__name__)


def generate_application_token() -> str:
    """A fresh application token from a cryptographically secure source (section 3)."""
    return secrets.token_urlsafe(MIN_TOKEN_LENGTH)


@dataclass(frozen=True)
class ChatSettings:
    """Operator policy for the chat endpoint.

    allowed_modes are the execution modes the operator permits (the
    same policy as the native run API); supported_options declares
    which of max_output_tokens, temperature, and top_p the configured
    adapters accept; max_completion_tokens is the largest token budget
    a request may ask for (spec 0.14 section 7.1, RM03).
    """

    allowed_modes: frozenset[ExecutionMode] = frozenset(ExecutionMode)
    supported_options: frozenset[str] = SAMPLING_OPTIONS
    max_completion_tokens: int = MAX_COMPLETION_TOKENS


def create_app(
    *,
    orchestrator: LocalOrchestrator,
    client_tools: ChatBackend | None = None,
    settings: ChatSettings | None = None,
) -> FastAPI:
    """The compatible API over one orchestrator.

    client_tools serves the relaymind-client-tools alias; without it the
    alias is neither listed nor accepted, so the gateway never offers
    what it cannot run. The application token is generated here, at
    startup, from a secure random source; no caller supplies it (spec
    0.14 section 7.1). The operator hands app.state.application_token
    to the clients.
    """
    token = generate_application_token()
    settings = settings or ChatSettings()
    backends: dict[str, ChatBackend] = {MANAGED: ManagedChat(orchestrator)}
    if client_tools is not None:
        backends[CLIENT_TOOLS] = client_tools
    started = int(time.time())
    # No interactive docs: they would be an unauthenticated surface.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.application_token = token

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
            body,
            served=backends,
            supported_options=settings.supported_options,
            max_completion_tokens=settings.max_completion_tokens,
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
            error = error_from_exception(exc)
            if error.code == "internal_error":
                _log_internal(request, exc)
            return _error_response(error)

    return wrapper


def _log_internal(request: Request, exc: Exception) -> None:
    """Log an unexpected failure for the operator without its text (RM12).

    The exception message, its values, and even a source line can hold
    secrets, so the log has the type and the frames (file, line, and
    function) that raised it, never text.
    """
    frames = " <- ".join(
        f"{frame.filename}:{frame.lineno} in {frame.name}"
        for frame in reversed(traceback.extract_tb(exc.__traceback__))
    )
    logger.error(
        "internal_error in %s %s: %s at %s",
        request.method,
        request.url.path,
        type(exc).__name__,
        frames,
    )


def _decode(raw: bytes) -> object:
    # A body nested too deeply for the decoder is not valid JSON to the gateway.
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
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
