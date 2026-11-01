"""Bounded independent JSON recommendation API (RM36/AC27, section 7.6)."""

import asyncio
import re
from collections.abc import Mapping
from urllib.parse import unquote_to_bytes
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from relaymind.api.app import _disconnect, _left, _log_internal
from relaymind.api.auth import require_bearer_token
from relaymind.api.errors import ApiError
from relaymind.api.idempotency import APPLICATION_CALLER, idempotency_key
from relaymind.assets.catalog import check_language_tags
from relaymind.canonical import canonical_digest
from relaymind.recommendations.errors import RecommendationError
from relaymind.recommendations.limits import (
    DEFAULT_EVENTS_PAGE,
    MAX_CONVERSATION_BYTES,
    MAX_CONVERSATION_ENTRIES,
    MAX_CREATE_BYTES,
    MAX_TASK_BYTES,
)
from relaymind.recommendations.service import (
    CancellationSignal,
    RecommendationService,
    readiness_body,
)
from relaymind.redaction import scrub
from relaymind.selection.asset_chain import RecommendationFailure, SelectionBudget
from relaymind.selection.asset_http import strict_json
from relaymind.store.recommendation_event import validate_readiness
from relaymind.store.recommendations import RecommendationClaim

ENDPOINT = "/relay/v1/recommendations"


def parse_create(raw: bytes) -> dict:
    """Strict closed defaults; fixed UTF-8 byte limits before persistence."""
    try:
        if len(raw) > MAX_CREATE_BYTES:
            raise ValueError
        value = strict_json(raw)
        required = {"schema_version", "task_text", "requested_kinds", "selection_scope"}
        optional = {"conversation", "language_tags", "code_switched", "host_snapshot_revision"}
        if (
            not isinstance(value, dict)
            or not required <= value.keys()
            or value.keys() - required - optional
        ):
            raise ValueError
        value = (
            dict(
                conversation=[], language_tags=[], code_switched=False, host_snapshot_revision=None
            )
            | value
        )
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError
        if (
            value["host_snapshot_revision"] is not None
            or type(value["code_switched"]) is not bool
            or value["selection_scope"] not in ("discovery", "runtime")
        ):
            raise ValueError
        task = value["task_text"]
        if (
            not isinstance(task, str)
            or not task.strip()
            or not 1 <= len(task.encode("utf-8")) <= MAX_TASK_BYTES
        ):
            raise ValueError
        kinds = value["requested_kinds"]
        if (
            not isinstance(kinds, list)
            or not 1 <= len(kinds) <= 2
            or any(type(k) is not str or k not in ("skill", "mcp_tool") for k in kinds)
            or len(set(kinds)) != len(kinds)
        ):
            raise ValueError
        check_language_tags(value["language_tags"])
        conversation = value["conversation"]
        if not isinstance(conversation, list) or len(conversation) > MAX_CONVERSATION_ENTRIES:
            raise ValueError
        size = 0
        for message in conversation:
            if (
                not isinstance(message, dict)
                or set(message) != {"role", "text"}
                or message["role"] not in ("system", "developer", "user", "assistant")
                or not isinstance(message["text"], str)
            ):
                raise ValueError
            size += len(message["text"].encode("utf-8"))
        if size > MAX_CONVERSATION_BYTES:
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError, OverflowError):
        raise RecommendationError("invalid_request") from None


async def _body(request, budget):
    async def receive_body():
        chunks, size = [], 0
        async for chunk in request.stream():
            budget.check()
            size += len(chunk)
            if size > MAX_CREATE_BYTES:
                raise RecommendationError("invalid_request")
            chunks.append(chunk)
        budget.check()
        return b"".join(chunks)

    # A client can withhold either the first chunk or a later one. The receive
    # task is owned and timed from the original authenticated request budget.
    receive = asyncio.create_task(receive_body(), name="recommendation-body")
    budget.own(receive)
    try:
        done, _ = await asyncio.wait({receive}, timeout=budget.remaining_ms() / 1000)
        if not done:
            raise RecommendationFailure("deadline_exceeded", "deadline_exceeded")
        return receive.result()
    finally:
        if not receive.done():
            receive.cancel()
            await asyncio.wait({receive}, timeout=0.1)


def _query(request, *, events=False):
    pairs = list(request.query_params.multi_items())
    allowed = {"after_seq", "limit"} if events else set()
    if any(k not in allowed for k, v in pairs) or len({k for k, v in pairs}) != len(pairs):
        raise RecommendationError("invalid_request")
    values = dict(pairs)
    for text in values.values():
        if not re.fullmatch(r"[0-9]+", text):
            raise RecommendationError("invalid_request")
    # The store owns the page-size range check (section 14.5). A numeral past
    # Python's integer-string limit makes int() raise; that is a bad request too.
    try:
        return int(values.get("after_seq", "0")), int(values.get("limit", str(DEFAULT_EVENTS_PAGE)))
    except ValueError:
        raise RecommendationError("invalid_request") from None


def parse_object_id(raw: str, code: str = "recommendation_not_found") -> str:
    """A canonical UUID path parameter; anything else is the not-found code."""
    try:
        if str(UUID(raw)) != raw:
            raise ValueError
    except ValueError:
        raise RecommendationError(code) from None
    return raw


def required_idempotency_key(request: Request) -> str:
    """Every state-changing recommendation request carries exactly one key (section 7.6)."""
    try:
        key = idempotency_key(request.headers)
    except ApiError:
        raise RecommendationError("invalid_request") from None
    if key is None:
        raise RecommendationError("invalid_request")
    return key


def request_digest(method: str, endpoint: str, object_id, body, query=None) -> str:
    """The canonical request hash of section 7.6: method, endpoint, object, query and body."""
    return canonical_digest(
        dict(method=method, endpoint=endpoint, object_id=object_id, query=query or {}, body=body)
    )


EXPECTED_USER_HEADER = "X-RelayMind-Expected-User"
EXPECTED_WORKSPACE_HEADER = "X-RelayMind-Expected-Workspace"
_PERCENT_ENCODED = re.compile(r"(?:[A-Za-z0-9\-._~!$&'()*+,;=]|%[0-9A-Fa-f]{2})+")


def _expected_identity(values: list[str]) -> str:
    if len(values) != 1 or not _PERCENT_ENCODED.fullmatch(values[0]):
        raise RecommendationError("invalid_request")
    try:
        identity = unquote_to_bytes(values[0]).decode("utf-8")
    except UnicodeDecodeError:
        raise RecommendationError("invalid_request") from None
    if not identity or len(identity.encode("utf-8")) > 256:
        raise RecommendationError("invalid_request")
    return identity


def check_expected_scope(request: Request, scope: Mapping | None) -> None:
    """Spec 0.63 section 7.6: an adapter's expected scope must be the served one.

    Both headers or neither. A present expectation that differs from the one
    configured scope fails closed before any read or write, so a host binding
    can never act on another person's recommendations or feedback.
    """
    users = request.headers.getlist(EXPECTED_USER_HEADER)
    workspaces = request.headers.getlist(EXPECTED_WORKSPACE_HEADER)
    if not users and not workspaces:
        return
    expected = (_expected_identity(users), _expected_identity(workspaces))
    if scope is None:
        raise RecommendationError("recommendation_unavailable")
    if expected != (scope["user_id"], scope["workspace_id"]):
        raise RecommendationError("scope_mismatch")


def guarded_route(app: FastAPI, handler, *, authenticate=None, scope: Mapping | None = None):
    """Bearer, expected scope, the optional action check, then the handler.

    One error envelope. An unexpected failure is logged for the operator
    (without its text) and answered as internal_error.
    """

    async def wrapped(request: Request):
        try:
            require_bearer_token(request.headers.get("authorization"), app.state.application_token)
            check_expected_scope(request, scope)
            if authenticate is None:
                return await handler(request)
            action = await run_in_threadpool(authenticate, request)
            return await handler(request, *action)
        except ApiError as exc:
            error = RecommendationError(
                exc.code if exc.code in ("unauthenticated", "invalid_request") else "internal_error"
            )
        except RecommendationError as exc:
            error = exc
        except RecommendationFailure as failure:
            error = RecommendationError(
                "deadline_exceeded"
                if failure.code == "deadline_exceeded"
                else "recommendation_unavailable"
            )
        except Exception as exc:
            _log_internal(request, exc)
            error = RecommendationError("internal_error")
        headers = {"WWW-Authenticate": "Bearer"} if error.code == "unauthenticated" else {}
        return JSONResponse({"error": error.inner()}, status_code=error.status, headers=headers)

    return wrapped


class _NoReply(Response):
    """A response for a client that already disconnected: it sends nothing."""

    def __init__(self) -> None:
        super().__init__(status_code=204)

    async def __call__(self, scope, receive, send) -> None:
        return None


def install_recommendation_routes(
    app: FastAPI, service: RecommendationService | None, *, configured_scope: Mapping | None
) -> None:
    """Install the four RM36 routes and the RM37 direct-action routes."""

    def guarded(handler):
        return guarded_route(app, handler, scope=configured_scope)

    async def create(request):
        budget = service.new_budget() if service else SelectionBudget()
        _query(request)
        body = parse_create(await _body(request, budget))
        key = required_idempotency_key(request)
        if service is None:
            raise RecommendationError("recommendation_unavailable")
        body = service.redactor.redact_with_repeats(
            scrub(
                body,
                [
                    app.state.application_token,
                    *request.headers.getlist("X-RelayMind-User-Action-Token"),
                ],
                whole_tokens=False,
            )
        )
        digest = request_digest("POST", ENDPOINT, None, body)
        claim = RecommendationClaim(
            configured_scope, APPLICATION_CALLER, "POST", ENDPOINT, None, key, digest
        )
        signal = CancellationSignal()
        task = asyncio.create_task(service.create(body, claim, budget, signal))
        watcher = asyncio.create_task(_disconnect(request))
        try:
            done, _ = await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done and _left(watcher) and not task.done():
                signal.reason = "disconnect"
                budget.cancel()
                if signal.recommendation_id:
                    await service.cancel_computing(signal.recommendation_id, reason="disconnect")
                else:
                    task.cancel()
                # The cancelled view is recorded for replay; the client is gone,
                # so nothing is sent on its socket.
                await asyncio.gather(task, return_exceptions=True)
                return _NoReply()
            response = await task
            return JSONResponse(response.body, status_code=response.status)
        except asyncio.CancelledError:
            signal.reason = signal.reason or "shutdown"
            budget.cancel()
            if signal.recommendation_id:
                await service.cancel_computing(signal.recommendation_id, reason=signal.reason)
            else:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            recorded = service.recorded_response(claim)
            if recorded is None:
                # Nothing was accepted: the cancellation is the outcome, never an empty 200.
                raise
            return JSONResponse(recorded.body, status_code=recorded.status)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def readiness(request):
        _query(request)
        return JSONResponse(
            validate_readiness(service.readiness() if service else readiness_body())
        )

    def object_id(request):
        return parse_object_id(request.path_params["recommendation_id"])

    async def get(request):
        _query(request)
        if service is None:
            raise RecommendationError("recommendation_not_found")
        return JSONResponse(service.get(configured_scope, object_id(request)))

    async def events(request):
        after, limit = _query(request, events=True)
        if service is None:
            raise RecommendationError("recommendation_not_found")
        return JSONResponse(
            service.events(configured_scope, object_id(request), after_seq=after, limit=limit)
        )

    app.add_api_route(ENDPOINT, guarded(create), methods=["POST"])
    app.add_api_route(ENDPOINT + "/readiness", guarded(readiness), methods=["GET"])
    app.add_api_route(ENDPOINT + "/{recommendation_id}", guarded(get), methods=["GET"])
    app.add_api_route(ENDPOINT + "/{recommendation_id}/events", guarded(events), methods=["GET"])
    from relaymind.api.user_actions import install_action_routes

    install_action_routes(app, service)
