"""Bounded independent JSON recommendation API (RM36/AC27, section 7.6)."""

import asyncio
import json
import math
import re
from collections.abc import Mapping
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from relaymind.api.auth import require_bearer_token
from relaymind.api.errors import ApiError
from relaymind.api.idempotency import APPLICATION_CALLER, idempotency_key
from relaymind.assets.catalog import check_language_tags
from relaymind.canonical import canonical_digest
from relaymind.recommendations.service import CancellationSignal, RecommendationService
from relaymind.redaction import scrub
from relaymind.selection.asset_chain import RecommendationFailure, SelectionBudget
from relaymind.store.recommendations import RecommendationClaim, RecommendationError

ENDPOINT = "/relay/v1/recommendations"
MAX_CREATE_BYTES = 512 * 1024


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError
    return result


def parse_create(raw: bytes) -> dict:
    """Strict closed defaults; fixed UTF-8 byte limits before persistence."""
    try:
        if len(raw) > MAX_CREATE_BYTES:
            raise ValueError
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            parse_float=_float,
        )
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
            or not 1 <= len(task.encode("utf-8")) <= 8192
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
        if not isinstance(conversation, list) or len(conversation) > 32:
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
        if size > 32768:
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
    try:
        after = int(values.get("after_seq", "0"))
        limit = int(values.get("limit", "100"))
    except ValueError:
        raise RecommendationError("invalid_request") from None
    if not 1 <= limit <= 1000:
        raise RecommendationError("invalid_request")
    return after, limit


async def _disconnect(request):
    while (await request.receive())["type"] != "http.disconnect":
        pass


def _disabled_readiness():
    return dict(
        schema_version=1,
        state="unavailable",
        reason="disabled",
        catalog_revision=None,
        catalog_size=0,
        lexical_ready=False,
        semantic_coverage=dict(ready=0, total=0),
        pending_jobs=0,
        failed_jobs=0,
    )


def install_recommendation_routes(
    app: FastAPI, service: RecommendationService | None, *, configured_scope: Mapping | None
) -> None:
    """Install only the four RM36 routes. #195 direct actions remain unwired."""

    def guarded(handler):
        async def wrapped(request: Request):
            try:
                require_bearer_token(
                    request.headers.get("authorization"), app.state.application_token
                )
                return await handler(request)
            except ApiError as exc:
                error = RecommendationError(
                    exc.code
                    if exc.code in ("unauthenticated", "invalid_request")
                    else "internal_error"
                )
            except RecommendationError as exc:
                error = exc
            except RecommendationFailure as failure:
                error = RecommendationError(
                    "deadline_exceeded"
                    if failure.code == "deadline_exceeded"
                    else "recommendation_unavailable"
                )
            except Exception:
                error = RecommendationError("internal_error")
            return JSONResponse({"error": error.inner()}, status_code=error.status)

        return wrapped

    async def create(request):
        budget = service.new_budget() if service else SelectionBudget()
        _query(request)
        body = parse_create(await _body(request, budget))
        try:
            key = idempotency_key(request.headers)
        except ApiError:
            raise RecommendationError("invalid_request") from None
        if key is None:
            raise RecommendationError("invalid_request")
        if service is None:
            raise RecommendationError("recommendation_unavailable")
        body = service.redactor.redact_with_repeats(scrub(body, [app.state.application_token]))
        digest = canonical_digest(
            dict(method="POST", endpoint=ENDPOINT, object_id=None, query={}, body=body)
        )
        claim = RecommendationClaim(
            configured_scope, APPLICATION_CALLER, "POST", ENDPOINT, None, key, digest
        )
        signal = CancellationSignal()
        task = asyncio.create_task(service.create(body, claim, budget, signal))
        watcher = asyncio.create_task(_disconnect(request))
        try:
            done, _ = await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
            if (
                watcher in done
                and not watcher.cancelled()
                and watcher.exception() is None
                and not task.done()
            ):
                signal.reason = "disconnect"
                budget.cancel()
                if signal.recommendation_id:
                    await service.cancel_computing(signal.recommendation_id, reason="disconnect")
                else:
                    task.cancel()
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
            recorded = service.store.lookup_claim(claim, now=service.clock.utc_now())
            if recorded is not None and recorded.response is not None:
                return JSONResponse(recorded.response.body, status_code=recorded.response.status)
            return Response(status_code=200)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def readiness(request):
        _query(request)
        return JSONResponse(service.readiness() if service else _disabled_readiness())

    def object_id(request):
        raw = request.path_params["recommendation_id"]
        try:
            if str(UUID(raw)) != raw:
                raise ValueError
        except ValueError:
            raise RecommendationError("recommendation_not_found") from None
        return raw

    async def get(request):
        _query(request)
        if service is None:
            raise RecommendationError("recommendation_not_found")
        return JSONResponse(
            service.store.get(
                configured_scope, object_id(request), now=service.clock.utc_now()
            ).view
        )

    async def events(request):
        after, limit = _query(request, events=True)
        if service is None:
            raise RecommendationError("recommendation_not_found")
        return JSONResponse(
            service.store.events(
                configured_scope,
                object_id(request),
                after_seq=after,
                limit=limit,
                now=service.clock.utc_now(),
            )
        )

    app.add_api_route(ENDPOINT, guarded(create), methods=["POST"])
    app.add_api_route(ENDPOINT + "/readiness", guarded(readiness), methods=["GET"])
    app.add_api_route(ENDPOINT + "/{recommendation_id}", guarded(get), methods=["GET"])
    app.add_api_route(ENDPOINT + "/{recommendation_id}/events", guarded(events), methods=["GET"])
