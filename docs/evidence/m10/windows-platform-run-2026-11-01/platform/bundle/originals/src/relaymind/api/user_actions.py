"""Closed direct-input action routes (RM37/AC28)."""

import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from relaymind.api.recommendations import (
    guarded_route,
    parse_object_id,
    request_digest,
    required_idempotency_key,
)
from relaymind.recommendations.errors import RecommendationError
from relaymind.recommendations.limits import (
    DEFAULT_EVENTS_PAGE,
    MAX_CREATE_BYTES,
    MAX_EVENTS_PAGE,
    MAX_SEQUENCE,
)
from relaymind.selection.asset_http import strict_json
from relaymind.store.recommendations import RecommendationClaim

ACTION_TOKEN_HEADER = "X-RelayMind-User-Action-Token"


def _json(raw: bytes, secrets=()):
    """Strict JSON that also refuses any string carrying a current secret (RM12)."""
    try:
        value = strict_json(raw)

        def check(item):
            if isinstance(item, str):
                item.encode("utf-8")
                if any(secret and secret in item for secret in secrets):
                    raise ValueError
            elif isinstance(item, dict):
                for key, child in item.items():
                    check(key)
                    check(child)
            elif isinstance(item, list):
                for child in item:
                    check(child)

        check(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RecommendationError("invalid_request") from None


async def _body(request: Request) -> bytes:
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_CREATE_BYTES:
            raise RecommendationError("invalid_request")
        chunks.append(chunk)
    return b"".join(chunks)


def _closed(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise RecommendationError("invalid_request")


def _versioned(value, fields):
    _closed(value, ("schema_version", *fields))
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise RecommendationError("invalid_request")


def _nonnegative(value):
    if type(value) is not int or value < 0 or value > MAX_SEQUENCE:
        raise RecommendationError("invalid_request")


def _decimal(value: str, *, maximum: int) -> int:
    if len(value) > 19 or not re.fullmatch(r"[0-9]+", value):
        raise RecommendationError("invalid_request")
    number = int(value)
    if number > maximum:
        raise RecommendationError("invalid_request")
    return number


def _asset_reference(value):
    """{kind, asset_id, content_hash}: the shape shared by asset choices and targeted feedback."""
    _closed(value, ("kind", "asset_id", "content_hash"))
    if (
        type(value["asset_id"]) is not str
        or not 1 <= len(value["asset_id"].encode("utf-8")) <= 256
        or type(value["content_hash"]) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", value["content_hash"])
    ):
        raise RecommendationError("invalid_request")


def _choice(raw, secrets=()):
    value = _json(raw, secrets)
    _versioned(value, ("expected_version", "choice"))
    _nonnegative(value["expected_version"])
    choice = value["choice"]
    if type(choice) is not dict:
        raise RecommendationError("invalid_request")
    if choice.get("kind") == "asset":
        _asset_reference(choice)
    elif choice.get("kind") in ("none", "cancel"):
        _closed(choice, ("kind",))
    else:
        raise RecommendationError("invalid_request")
    return value


def _feedback(raw, secrets=()):
    value = _json(raw, secrets)
    _versioned(value, ("event_id", "expected_feedback_revision", "feedback"))
    _nonnegative(value["expected_feedback_revision"])
    if type(value["event_id"]) is not str or not 1 <= len(value["event_id"].encode("utf-8")) <= 256:
        raise RecommendationError("invalid_request")
    feedback = value["feedback"]
    if type(feedback) is not dict:
        raise RecommendationError("invalid_request")
    kind = feedback.get("kind")
    if kind in ("correction", "rejection"):
        _asset_reference(feedback)
    elif kind == "usefulness":
        _closed(feedback, ("kind", "value"))
        if feedback["value"] not in ("useful", "not_useful"):
            raise RecommendationError("invalid_request")
    else:
        raise RecommendationError("invalid_request")
    return value


# Kept under these names for callers and tests that build claims directly.
_id = parse_object_id
_key = required_idempotency_key


def _claim(action, method, endpoint, object_id, key, body, query=None):
    return RecommendationClaim(
        action.scope,
        action.adapter_id,
        method,
        endpoint,
        object_id,
        key,
        request_digest(method, endpoint, object_id, body, query),
    )


def install_action_routes(app: FastAPI, service) -> None:
    """Register the published private routes; absent registration denies action."""

    def secrets(request):
        return (app.state.application_token, *request.headers.getlist(ACTION_TOKEN_HEADER))

    def authenticate(request):
        # Section 7.6 step 2: a registered direct-input credential, after the bearer.
        registrations = getattr(app.state, "action_registrations", None)
        if registrations is None or service is None:
            raise RecommendationError("user_action_required")
        tokens = request.headers.getlist(ACTION_TOKEN_HEADER)
        if len(tokens) != 1:
            raise RecommendationError("user_action_required")
        return registrations.authenticate(tokens[0], service.scope), registrations

    def feedback_store():
        store = getattr(app.state, "feedback_store", None)
        if store is None:
            raise RecommendationError("internal_error")
        return store

    def guarded(handler):
        return guarded_route(
            app, handler, authenticate=authenticate, scope=service.scope if service else None
        )

    async def run_storage(operation, *args, **kwargs):
        # Take the time in the worker: waiting for dispatch cannot extend expiry.
        def invoke():
            return operation(*args, now=service.clock.utc_now(), **kwargs)

        return await run_in_threadpool(invoke)

    async def choose(request, action, registrations):
        if request.query_params:
            raise RecommendationError("invalid_request")
        body = _choice(await _body(request), secrets(request))
        key = _key(request)
        rid = _id(request.path_params["recommendation_id"])
        endpoint = "/relay/v1/recommendations/{id}/choice"
        claim = _claim(action, "POST", endpoint, rid, key, body)
        response = await run_storage(
            service.store.choose,
            action,
            rid,
            body,
            claim,
            registrations=registrations,
            catalog=service.provider.store,
            policy=service.provider.policy,
        )
        return JSONResponse(response.body, status_code=response.status)

    async def create_feedback(request, action, registrations):
        if request.query_params:
            raise RecommendationError("invalid_request")
        body = _feedback(await _body(request), secrets(request))
        key = _key(request)
        rid = _id(request.path_params["recommendation_id"])
        endpoint = "/relay/v1/recommendations/{id}/feedback"
        claim = _claim(action, "POST", endpoint, rid, key, body)
        response = await run_storage(feedback_store().create, action, rid, body, claim)
        return JSONResponse(response.body, status_code=response.status)

    async def list_feedback(request, action, registrations):
        pairs = list(request.query_params.multi_items())
        if any(key not in ("after_id", "limit") for key, _ in pairs) or len(
            {key for key, _ in pairs}
        ) != len(pairs):
            raise RecommendationError("invalid_request")
        query = dict(pairs)
        limit = _decimal(query.get("limit", str(DEFAULT_EVENTS_PAGE)), maximum=MAX_EVENTS_PAGE)
        page = await run_storage(
            feedback_store().list,
            action,
            after_id=query.get("after_id"),
            limit=limit,
        )
        return JSONResponse(page)

    async def delete_feedback(request, action, registrations):
        pairs = list(request.query_params.multi_items())
        if len(pairs) != 1 or pairs[0][0] != "expected_feedback_revision":
            raise RecommendationError("invalid_request")
        revision = _decimal(pairs[0][1], maximum=MAX_SEQUENCE)
        if await _body(request):
            raise RecommendationError("invalid_request")
        fid = _id(request.path_params["feedback_id"], "feedback_not_found")
        key = _key(request)
        endpoint = "/relay/v1/feedback/{id}"
        claim = _claim(
            action,
            "DELETE",
            endpoint,
            fid,
            key,
            None,
            query={"expected_feedback_revision": revision},
        )
        response = await run_storage(feedback_store().delete, action, fid, revision, claim)
        return JSONResponse(response.body, status_code=response.status)

    async def reset_feedback(request, action, registrations):
        if request.query_params:
            raise RecommendationError("invalid_request")
        body = _json(await _body(request), secrets(request))
        _versioned(body, ("expected_feedback_revision",))
        _nonnegative(body["expected_feedback_revision"])
        key = _key(request)
        endpoint = "/relay/v1/feedback/reset"
        claim = _claim(action, "POST", endpoint, None, key, body)
        response = await run_storage(
            feedback_store().reset,
            action,
            body["expected_feedback_revision"],
            claim,
        )
        return JSONResponse(response.body, status_code=response.status)

    base = "/relay/v1/recommendations/{recommendation_id}"
    app.add_api_route(base + "/choice", guarded(choose), methods=["POST"])
    app.add_api_route(base + "/feedback", guarded(create_feedback), methods=["POST"])
    app.add_api_route("/relay/v1/feedback", guarded(list_feedback), methods=["GET"])
    app.add_api_route("/relay/v1/feedback/reset", guarded(reset_feedback), methods=["POST"])
    app.add_api_route(
        "/relay/v1/feedback/{feedback_id}", guarded(delete_feedback), methods=["DELETE"]
    )
