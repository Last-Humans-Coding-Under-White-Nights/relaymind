"""Closed direct-input action routes (RM37/AC28)."""

import json
import re
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from relaymind.api.auth import require_bearer_token
from relaymind.api.errors import ApiError
from relaymind.api.idempotency import idempotency_key
from relaymind.canonical import canonical_digest
from relaymind.recommendations.errors import RecommendationError
from relaymind.recommendations.limits import DEFAULT_EVENTS_PAGE, MAX_CREATE_BYTES, MAX_EVENTS_PAGE
from relaymind.store.recommendations import RecommendationClaim


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _json(raw: bytes, secrets=()):
    try:
        if len(raw) > MAX_CREATE_BYTES:
            raise ValueError
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )

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
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise RecommendationError("invalid_request")


def _decimal(value: str, *, maximum: int) -> int:
    if len(value) > 19 or not re.fullmatch(r"[0-9]+", value):
        raise RecommendationError("invalid_request")
    number = int(value)
    if number > maximum:
        raise RecommendationError("invalid_request")
    return number


def _choice(raw, secrets=()):
    value = _json(raw, secrets)
    _versioned(value, ("expected_version", "choice"))
    _nonnegative(value["expected_version"])
    choice = value["choice"]
    if type(choice) is not dict:
        raise RecommendationError("invalid_request")
    if choice.get("kind") == "asset":
        _closed(choice, ("kind", "asset_id", "content_hash"))
        if (
            type(choice["asset_id"]) is not str
            or not 1 <= len(choice["asset_id"].encode("utf-8")) <= 256
            or type(choice["content_hash"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", choice["content_hash"])
        ):
            raise RecommendationError("invalid_request")
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
        _closed(feedback, ("kind", "asset_id", "content_hash"))
        if (
            type(feedback["asset_id"]) is not str
            or not 1 <= len(feedback["asset_id"].encode("utf-8")) <= 256
            or type(feedback["content_hash"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", feedback["content_hash"])
        ):
            raise RecommendationError("invalid_request")
    elif kind == "usefulness":
        _closed(feedback, ("kind", "value"))
        if feedback["value"] not in ("useful", "not_useful"):
            raise RecommendationError("invalid_request")
    else:
        raise RecommendationError("invalid_request")
    return value


def _id(value: str, code="recommendation_not_found") -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise RecommendationError(code) from None
    return value


def _key(request):
    try:
        key = idempotency_key(request.headers)
    except ApiError:
        raise RecommendationError("invalid_request") from None
    if key is None:
        raise RecommendationError("invalid_request")
    return key


def _claim(action, method, endpoint, object_id, key, body, query=None):
    return RecommendationClaim(
        action.scope,
        action.adapter_id,
        method,
        endpoint,
        object_id,
        key,
        canonical_digest(
            dict(
                method=method,
                endpoint=endpoint,
                object_id=object_id,
                query=query or {},
                body=body,
            )
        ),
    )


def install_action_routes(app: FastAPI, service) -> None:
    """Register the published private routes; absent registration denies action."""

    def secrets(request):
        return (app.state.application_token, request.headers.get("X-RelayMind-User-Action-Token"))

    def guarded(handler):
        async def wrapped(request: Request):
            try:
                require_bearer_token(
                    request.headers.get("authorization"), app.state.application_token
                )
                registrations = getattr(app.state, "action_registrations", None)
                if registrations is None or service is None:
                    raise RecommendationError("user_action_required")
                tokens = [
                    value.decode("latin-1")
                    for key, value in request.scope["headers"]
                    if key.lower() == b"x-relaymind-user-action-token"
                ]
                if len(tokens) != 1:
                    raise RecommendationError("user_action_required")
                action = registrations.authenticate(tokens[0], service.scope)
                return await handler(request, action, registrations)
            except ApiError as exc:
                error = RecommendationError(
                    exc.code
                    if exc.code in ("unauthenticated", "invalid_request")
                    else "internal_error"
                )
            except RecommendationError as exc:
                error = exc
            except Exception:
                error = RecommendationError("internal_error")
            headers = {"WWW-Authenticate": "Bearer"} if error.code == "unauthenticated" else {}
            return JSONResponse({"error": error.inner()}, status_code=error.status, headers=headers)

        return wrapped

    async def choose(request, action, registrations):
        if request.query_params:
            raise RecommendationError("invalid_request")
        body = _choice(await _body(request), secrets(request))
        key = _key(request)
        rid = _id(request.path_params["recommendation_id"])
        endpoint = "/relay/v1/recommendations/{id}/choice"
        claim = _claim(action, "POST", endpoint, rid, key, body)
        if service is None:
            raise RecommendationError("recommendation_not_found")
        response = service.store.choose(
            action,
            rid,
            body,
            claim,
            now=service.clock.utc_now(),
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
        feedback_store = getattr(app.state, "feedback_store", None)
        if feedback_store is None:
            raise RecommendationError("invalid_state")
        response = feedback_store.create(action, rid, body, claim, now=service.clock.utc_now())
        return JSONResponse(response.body, status_code=response.status)

    async def list_feedback(request, action, registrations):
        pairs = list(request.query_params.multi_items())
        if any(key not in ("after_id", "limit") for key, _ in pairs) or len(
            {key for key, _ in pairs}
        ) != len(pairs):
            raise RecommendationError("invalid_request")
        query = dict(pairs)
        limit_text = query.get("limit", str(DEFAULT_EVENTS_PAGE))
        limit = _decimal(limit_text, maximum=MAX_EVENTS_PAGE)
        feedback_store = getattr(app.state, "feedback_store", None)
        if feedback_store is None:
            raise RecommendationError("invalid_state")
        page = feedback_store.list(
            action,
            after_id=query.get("after_id"),
            limit=limit,
            now=service.clock.utc_now(),
        )
        return JSONResponse(page)

    async def delete_feedback(request, action, registrations):
        pairs = list(request.query_params.multi_items())
        if len(pairs) != 1 or pairs[0][0] != "expected_feedback_revision":
            raise RecommendationError("invalid_request")
        raw_revision = pairs[0][1]
        revision = _decimal(raw_revision, maximum=2**63 - 1)
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
        feedback_store = getattr(app.state, "feedback_store", None)
        if feedback_store is None:
            raise RecommendationError("invalid_state")
        response = feedback_store.delete(action, fid, revision, claim, now=service.clock.utc_now())
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
        feedback_store = getattr(app.state, "feedback_store", None)
        if feedback_store is None:
            raise RecommendationError("invalid_state")
        response = feedback_store.reset(
            action,
            body["expected_feedback_revision"],
            claim,
            now=service.clock.utc_now(),
        )
        return JSONResponse(response.body, status_code=response.status)

    async def unavailable(request, action, registrations):
        raise RecommendationError("invalid_state")

    base = "/relay/v1/recommendations/{recommendation_id}"
    app.add_api_route(base + "/choice", guarded(choose), methods=["POST"])
    app.add_api_route(base + "/feedback", guarded(create_feedback), methods=["POST"])
    app.add_api_route("/relay/v1/feedback", guarded(list_feedback), methods=["GET"])
    app.add_api_route("/relay/v1/feedback/reset", guarded(reset_feedback), methods=["POST"])
    app.add_api_route(
        "/relay/v1/feedback/{feedback_id}", guarded(delete_feedback), methods=["DELETE"]
    )
