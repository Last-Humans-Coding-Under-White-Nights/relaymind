"""The HTTP app for the compatible endpoints (RM19, RM20, spec 0.14 section 7.1).

``create_app`` serves GET /v1/models and POST /v1/chat/completions over
one orchestrator. The order of checks is fixed: the application token
(401), the body (400), the Idempotency-Key header (400), the
execution mode header (400, then 403), then a stored key (a replay or
409, ``relaymind.api.idempotency``). Everything is checked before a
run exists, so a rejected request has no run and stores no key. Errors
use the contract of ``relaymind.api.errors``; an unexpected exception
becomes a fixed internal_error with no detail (RM12); the operator log
names its type and where it was raised, never its text.

The native run API (RM21, spec 0.17 section 7.4) is served by the same
app: six endpoints plus /healthz and /readyz, with the same token, error
contract, and execution mode policy. A native run continues in the
background after its 202, so these routes only start, read, resume, or
cancel it.

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
from dataclasses import dataclass, replace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from relaymind.api.auth import require_bearer_token
from relaymind.api.chat import (
    ChatBackend,
    ManagedChat,
    collect_live,
    completion_body,
    open_stream,
)
from relaymind.api.chat_request import (
    CLIENT_TOOLS,
    MANAGED,
    MAX_COMPLETION_TOKENS,
    SAMPLING_OPTIONS,
    parse_chat_request,
)
from relaymind.api.errors import ApiError, error_from_exception
from relaymind.api.idempotency import (
    REPLAYED_HEADER,
    check_stored_key,
    claim_for,
    idempotency_key,
    stored_after_race,
    stored_chat_result,
)
from relaymind.api.native import (
    NativeSettings,
    event_stream,
    events_url,
    parse_cancel_request,
    parse_last_event_id,
    parse_resume_request,
    parse_run_request,
    readiness,
    run_view,
    sweep_expired_waits,
)
from relaymind.orchestrator.local_loop import LocalOrchestrator, StartedRun
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.orchestrator.run_state import RunState
from relaymind.store import IdempotencyKeyTakenError

__all__ = [
    "MIN_TOKEN_LENGTH",
    "ChatSettings",
    "NativeSettings",
    "create_app",
    "generate_application_token",
]

RUN_ID_HEADER = "X-RelayMind-Run-ID"
MODE_HEADER = "X-RelayMind-Execution-Mode"
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
    native: NativeSettings | None = None,
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
    native = native or NativeSettings()
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
        key = idempotency_key(request.headers)
        mode_header = request.headers.get(MODE_HEADER)
        mode = _execution_mode(mode_header, MODE_HEADER, chat.alias, settings)
        claim = claim_for(key, "chat", body, mode_header)
        stored = check_stored_key(orchestrator.store, claim)
        if stored is None:
            try:
                result = await backends[chat.alias].complete(replace(chat, idempotency=claim), mode)
            except IdempotencyKeyTakenError as taken:
                stored = stored_after_race(orchestrator.store, claim, taken)
        if stored is None:
            created = int(time.time())
            headers = {RUN_ID_HEADER: result.run_id}
        else:
            # A duplicate: answered from the stored run, so no model or tool runs.
            # An expired wait ends first, so it is never answered as unfinished.
            sweep_expired_waits(orchestrator)
            result, ended = stored_chat_result(
                orchestrator.store, stored.run_id, stream=chat.stream
            )
            created = int(ended.timestamp())
            headers = {RUN_ID_HEADER: result.run_id, REPLAYED_HEADER: "true"}
        if chat.stream:
            # Started here, so a failure before the first chunk still has its status.
            lines = await open_stream(chat.alias, result, created)
            return _ClosingStream(
                lines,
                media_type="text/event-stream",
                headers={**headers, "Cache-Control": "no-cache"},
            )
        result = await collect_live(result)
        return JSONResponse(completion_body(chat.alias, result, created), headers=headers)

    # -- the native run API (RM21, spec 0.17 section 7.4) ---------------------

    @app.post("/relay/v1/runs")
    @_guarded
    async def create_run(request: Request) -> Response:
        require_bearer_token(request.headers.get("authorization"), token)
        body = _decode(await request.body())
        run = parse_run_request(body)
        key = idempotency_key(request.headers)
        mode = _execution_mode(run.execution_mode, "execution_mode", None, settings)
        claim = claim_for(key, "native", body)
        stored = check_stored_key(orchestrator.store, claim)
        if stored is None:
            try:
                started = orchestrator.start(
                    run.messages,
                    execution_mode=None if run.execution_mode is None else mode.value,
                    idempotency=claim,
                )
            except IdempotencyKeyTakenError as taken:
                stored = stored_after_race(orchestrator.store, claim, taken)
            else:
                return _accepted(started)
        # A duplicate: the existing run's reference, with its current state.
        # An expired wait ends first, so a run is never shown waiting past its limit.
        sweep_expired_waits(orchestrator)
        record = orchestrator.store.get_run(stored.run_id)
        existing = StartedRun(record.run_id, record.state, record.version)
        return _accepted(existing, {REPLAYED_HEADER: "true"})

    @app.get("/relay/v1/runs/{run_id}")
    @_guarded
    async def get_run(request: Request) -> Response:
        require_bearer_token(request.headers.get("authorization"), token)
        sweep_expired_waits(orchestrator)
        view = run_view(orchestrator, request.path_params["run_id"], reveal_token=True)
        return JSONResponse(view, headers={"Cache-Control": "no-store"})

    @app.get("/relay/v1/runs/{run_id}/events")
    @_guarded
    async def get_events(request: Request) -> Response:
        require_bearer_token(request.headers.get("authorization"), token)
        run_id = request.path_params["run_id"]
        cursor = parse_last_event_id(request.headers.get("last-event-id"))
        orchestrator.store.get_run(run_id)  # 404 before the stream starts
        return _ClosingStream(
            event_stream(orchestrator, run_id, cursor, native),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.post("/relay/v1/runs/{run_id}/resume")
    @_guarded
    async def resume_run(request: Request) -> Response:
        require_bearer_token(request.headers.get("authorization"), token)
        run_id = request.path_params["run_id"]
        raw = await request.body()
        record = orchestrator.store.get_run(run_id)  # 404 first
        resume = parse_resume_request(_decode(raw))
        # An input mixed with approval fields matches no waiting state: it
        # is passed on as unusable so the terminal and version checks of
        # the waiting state still come first (section 5).
        if RunState(record.state) is RunState.WAITING_CONFIRMATION:
            started = orchestrator.start_confirmation(
                run_id,
                resume.expected_version,
                approval_id=resume.approval_id,
                token=resume.token,
                approve=None if resume.input is not None else resume.approve,
            )
        else:
            started = orchestrator.start_resume(
                run_id,
                resume.expected_version,
                None if resume.has_approval_fields else resume.input,
            )
        return _accepted(started)

    @app.post("/relay/v1/runs/{run_id}/cancel")
    @_guarded
    async def cancel_run(request: Request) -> Response:
        require_bearer_token(request.headers.get("authorization"), token)
        run_id = request.path_params["run_id"]
        raw = await request.body()
        orchestrator.store.get_run(run_id)  # 404 first
        expected = parse_cancel_request(_decode(raw))
        await orchestrator.cancel(run_id, expected, wait_seconds=native.cancel_wait_seconds)
        return JSONResponse(run_view(orchestrator, run_id), headers={"Cache-Control": "no-store"})

    @app.get("/healthz")
    @_guarded
    async def healthz(request: Request) -> Response:
        require_bearer_token(request.headers.get("authorization"), token)
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    @_guarded
    async def readyz(request: Request) -> Response:
        require_bearer_token(request.headers.get("authorization"), token)
        ready, body = await readiness(orchestrator, settings.allowed_modes)
        return JSONResponse(body, status_code=200 if ready else 503)

    return app


def _accepted(started: StartedRun, headers: dict[str, str] | None = None) -> Response:
    """The 202 body of a created or resumed run (spec 0.17 section 7.4)."""
    return JSONResponse(
        {
            "run_id": started.run_id,
            "state": started.state,
            "state_version": started.state_version,
            "events_url": events_url(started.run_id),
        },
        status_code=202,
        headers=headers,
    )


_Handler = Callable[[Request], Awaitable[Response]]


class _ClosingStream(StreamingResponse):
    """A stream whose lines are closed however the response ends.

    Starlette leaves the body iterator suspended when the client goes
    away; closing it closes a live provider stream, which is the
    cancellation request of a chat disconnect (section 7.4).
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()


def _guarded(handler: _Handler) -> _Handler:
    """Every response of the route follows the error contract (section 11)."""

    @functools.wraps(handler)
    async def wrapper(request: Request) -> Response:
        try:
            return await handler(request)
        except Exception as exc:
            error = error_from_exception(exc)
            run_id = request.path_params.get("run_id")
            if error.run_id is None and error.code != "unauthenticated" and _header_safe(run_id):
                # A run endpoint names the run its request was about, when
                # the path text can be a header value at all.
                error.run_id = run_id
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


def _header_safe(value: object) -> bool:
    """True for text that can be an HTTP header value as it is (printable ASCII)."""
    return isinstance(value, str) and value.isascii() and value.isprintable()


def _decode(raw: bytes) -> object:
    # A body nested too deeply for the decoder is not valid JSON to the gateway.
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise ApiError("invalid_request", "the request body must be valid JSON") from None


def _execution_mode(
    value: object, source: str, alias: str | None, settings: ChatSettings
) -> ExecutionMode:
    """The effective mode from a header or a native field: absent is auto, never downgraded.

    One enumeration and one operator policy serve both surfaces (section 7.4).
    """
    if value is None:
        mode = ExecutionMode.AUTO
    else:
        try:
            mode = ExecutionMode(value)
        except ValueError:
            raise ApiError(
                "invalid_execution_mode",
                f"{source} must be one of: {', '.join(m.value for m in ExecutionMode)}",
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
