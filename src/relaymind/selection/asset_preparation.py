"""Optional explicit index preparation; never query execution authority (RM35/RM40)."""

import asyncio
import copy
import math
import time
import uuid
from collections.abc import Callable, Mapping, Sequence

from relaymind.assets.catalog import (
    MAX_OFFER_BYTES,
    acquisition_budgets,
    text,
    validate_asset_record,
    validate_document,
)
from relaymind.canonical import canonical_digest, canonical_json
from relaymind.schema_util import StrictValidator, load_packaged_schema
from relaymind.selection.asset_contract import AttemptFailure, SelectorContext, parse_asset_result
from relaymind.selection.asset_http import post_json
from relaymind.selection.http import validate_url
from relaymind.store.index_job_event import validate_job_event

DEFAULT_PREPARATION = dict(
    mode="none", url=None, profile=None, total_ms=60000, request_ms=2000, poll_ms=250, max_polls=240
)
_REASONS = frozenset(
    (
        "build_failed",
        "deadline_exceeded",
        "cancelled",
        "catalog_stale",
        "unknown_preparation",
        "identity_conflict",
        "unsupported_profile",
        "transport_error",
        "http_status",
        "response_media_type",
        "response_too_large",
        "invalid_result",
        "invalid_response",
        "poll_limit",
    )
)
_VALIDATORS = {
    kind: StrictValidator(
        load_packaged_schema("relaymind.selection", f"asset_preparation_{kind}.v1.json")
    )
    for kind in ("request", "response")
}
_BINDING_VALIDATOR = StrictValidator(_VALIDATORS["request"].schema["properties"]["binding"])
_READINESS_VALIDATOR = StrictValidator(
    load_packaged_schema("relaymind.store", "recommendation_readiness.v1.json")
)
_COST_SCHEMA = load_packaged_schema("relaymind.store", "recommendation_result.v1.json")
_COST_VALIDATOR = StrictValidator({"$ref": "#/$defs/CostV1", "$defs": _COST_SCHEMA["$defs"]})


def normalize_preparation(
    value: Mapping | None, *, http_primary: bool = True, local_primary: bool = False
) -> dict:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError("invalid_preparation_config")
    value = {} if value is None else dict(value)
    if set(value) - set(DEFAULT_PREPARATION):
        raise ValueError("invalid_preparation_config")
    value = DEFAULT_PREPARATION | value
    for key, low, high in (
        ("total_ms", 1, 600000),
        ("request_ms", 1, 10000),
        ("poll_ms", 50, 5000),
        ("max_polls", 1, 1000),
    ):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError("invalid_preparation_config")
    if value["mode"] != "local" and value["request_ms"] > value["total_ms"]:
        raise ValueError("invalid_preparation_config")
    if value["mode"] == "none":
        if value["url"] is not None or value["profile"] is not None:
            raise ValueError("invalid_preparation_config")
    elif value["mode"] == "http" and http_primary and value["profile"] in ("cold", "reuse"):
        value["url"] = validate_url(value["url"])
        if len(value["url"].encode()) > 2048:
            raise ValueError("invalid_preparation_config")
    elif value["mode"] == "local" and local_primary:
        if (
            value["url"] is not None
            or value["profile"] != "cold"
            or value["total_ms"] > 60000
            or any(
                value[key] != DEFAULT_PREPARATION[key]
                for key in ("request_ms", "poll_ms", "max_polls")
            )
        ):
            raise ValueError("invalid_preparation_config")
    else:
        raise ValueError("invalid_preparation_config")
    return value


def preparation_policy_mismatches(left: Mapping, right: Mapping) -> tuple[str, ...]:
    """Compare setup policy while allowing declared stateless/indexed arms."""
    modes = {left["mode"], right["mode"]}
    if modes == {"http"}:
        keys = ("profile", "total_ms", "request_ms", "poll_ms", "max_polls")
    elif modes == {"local"}:
        keys = ("total_ms",)
    elif modes == {"local", "http"}:
        # HTTP polling limits are inapplicable to local execution. The shared
        # cold profile and preparation deadline still have to match.
        keys = ("profile", "total_ms")
    else:
        return ()
    return tuple(key for key in keys if left[key] != right[key])


def preparation_binding(
    *,
    selector: Mapping,
    catalog_revision: int,
    catalog: Sequence[Mapping],
    scope: Mapping,
    permissions: Sequence[str],
    selection_scope: str,
    requested_kinds: Sequence[str],
) -> dict:
    return dict(
        selector=dict(selector),
        catalog_revision=catalog_revision,
        catalog_digest=canonical_digest(catalog),
        scope=dict(scope),
        permissions=sorted(set(permissions)),
        selection_scope=selection_scope,
        requested_kinds=sorted(set(requested_kinds)),
    )


def _binding(value):
    validate_document(value, _BINDING_VALIDATOR)
    for item in (*value["selector"].values(), *value["scope"].values(), *value["permissions"]):
        text(item, 256)
    for key in ("permissions", "requested_kinds"):
        if value[key] != sorted(set(value[key])):
            raise ValueError("invalid_preparation_binding")


def _validate_local_preparation(record, *, binding=None):
    required = {
        "config",
        "binding",
        "preparation_id",
        "status",
        "reason",
        "index_revision",
        "observed_ms",
        "cleanup_ms",
        "exchanges",
        "observed_costs",
        "local",
    }
    if set(record) != required or record["binding"] is None:
        raise ValueError("invalid_preparation_record")
    _binding(record["binding"])
    if binding is not None and record["binding"] != binding:
        raise ValueError("preparation_binding")
    if record["preparation_id"] is not None or record["exchanges"] != []:
        raise ValueError("invalid_preparation_record")
    local = record["local"]
    if not isinstance(local, Mapping) or set(local) != {"job_events", "readiness"}:
        raise ValueError("invalid_local_preparation")
    events = local["job_events"]
    if not isinstance(events, (list, tuple)) or len(events) > 100000:
        raise ValueError("job_evidence_limit")
    states, batches, kinds = {}, {}, {}
    accepted, failed = 0, 0
    for value in events:
        event = validate_job_event(value)
        job_id = event["job_id"]
        text(job_id, 256)
        state, seq = states.get(job_id, ("new", 0))
        kind, payload = event["event_type"], event["payload"]
        if event["seq"] != seq + 1 or state in ("published", "failed", "cancelled"):
            raise ValueError("invalid_local_job_sequence")
        if kind == "queued":
            if state != "new" or payload["budgets"] != acquisition_budgets():
                raise ValueError("invalid_local_job_sequence")
            if payload["source_id"] is not None or payload["job_kind"] not in (
                "lexical",
                "embedding",
            ):
                raise ValueError("invalid_local_job_kind")
            kinds[job_id] = payload["job_kind"]
        elif kind == "started":
            if state != "queued":
                raise ValueError("invalid_local_job_sequence")
        elif kind == "batch.finished":
            if state != "started" or payload["batch"] != batches.get(job_id, 0) + 1:
                raise ValueError("invalid_local_job_sequence")
            batches[job_id] = payload["batch"]
            if kinds[job_id] == "embedding":
                accepted += payload["accepted_count"]
            elif payload["embeddings"]:
                raise ValueError("invalid_local_job_kind")
            for observation in payload["embeddings"]:
                for item in (
                    *observation["model"].values(),
                    observation["device"],
                    observation["representation_revision"],
                ):
                    text(item, 256)
                if observation["representation_revision"] != "asset_fields.v1":
                    raise ValueError("invalid_local_embedding")
        elif kind in ("published", "failed", "cancelled"):
            if state != "started":
                raise ValueError("invalid_local_job_sequence")
            failed += int(kind == "failed")
            if (
                kind == "published"
                and kinds[job_id] == "embedding"
                and record["status"] == "ready"
                and payload["index_revision"] != record["index_revision"]
            ):
                raise ValueError("preparation_index_binding")
        else:
            raise ValueError("invalid_local_job_sequence")
        if (
            kind in ("queued", "started", "published")
            and payload["catalog_revision"] != record["binding"]["catalog_revision"]
        ):
            raise ValueError("preparation_binding")
        states[job_id] = (state if kind == "batch.finished" else kind, event["seq"])
    readiness = local["readiness"]
    if readiness is not None:
        validate_document(readiness, _READINESS_VALIDATOR)
        coverage = readiness["semantic_coverage"]
        if (
            readiness["catalog_revision"] != record["binding"]["catalog_revision"]
            or not 0 <= coverage["ready"] <= coverage["total"] == readiness["catalog_size"] <= 1000
        ):
            raise ValueError("preparation_readiness_binding")
    reasons = {
        "ready": None,
        "not_applicable": None,
        "failed": "build_failed",
        "timeout": "deadline_exceeded",
        "cancelled": "cancelled",
        "stale_catalog": "catalog_stale",
    }
    status = record["status"]
    if status not in reasons or record["reason"] != reasons[status]:
        raise ValueError("invalid_preparation_reason")
    for key in ("observed_ms", "cleanup_ms"):
        value = record[key]
        if value is not None and (
            type(value) not in (int, float) or not math.isfinite(value) or value < 0
        ):
            raise ValueError("invalid_preparation_time")
    if status in ("ready", "not_applicable"):
        if (
            readiness is None
            or readiness["pending_jobs"]
            or record["observed_ms"] is None
            or record["observed_ms"] >= record["config"]["total_ms"]
        ):
            raise ValueError("invalid_preparation_deadline")
        if (
            any(state not in ("published", "failed", "cancelled") for state, seq in states.values())
            or readiness["failed_jobs"] != failed
        ):
            raise ValueError("invalid_local_job_sequence")
        if status == "ready":
            if (
                readiness["state"] != "ready"
                or not readiness["lexical_ready"]
                or readiness["semantic_coverage"]["ready"] <= 0
                or accepted < readiness["semantic_coverage"]["ready"]
                or record["index_revision"] is None
            ):
                raise ValueError("invalid_preparation_readiness")
            text(record["index_revision"], 256)
        elif (
            readiness["state"] != "empty"
            or readiness["catalog_size"] != 0
            or record["index_revision"] is not None
            or record["binding"]["catalog_digest"] != canonical_digest([])
        ):
            raise ValueError("invalid_preparation_readiness")
    elif record["index_revision"] is not None:
        raise ValueError("invalid_preparation_outcome")
    costs = record["observed_costs"]
    if not isinstance(costs, (list, tuple)) or len(costs) > 1:
        raise ValueError("invalid_preparation_costs")
    for observation in costs:
        if set(observation) != {"provenance", "value"} or observation["provenance"] != "observed":
            raise ValueError("invalid_preparation_costs")
        validate_document(observation["value"], _COST_VALIDATOR)
    if len(canonical_json(record).encode("utf-8")) > 256 * 1024 * 1024:
        raise ValueError("preparation_size_limit")


def parse_preparation_request(value: object) -> dict:
    raw = validate_document(value, _VALIDATORS["request"])
    _binding(raw["binding"])
    if raw["operation"] == "prepare":
        if raw["catalog"] is None or raw["budget_ms"] is None:
            raise ValueError("invalid_preparation_request")
        seen = set()
        for record in raw["catalog"]:
            validate_asset_record(record)
            if (
                record["asset_id"] in seen
                or record["kind"] not in raw["binding"]["requested_kinds"]
            ):
                raise ValueError("invalid_preparation_catalog")
            seen.add(record["asset_id"])
        if canonical_digest(raw["catalog"]) != raw["binding"]["catalog_digest"]:
            raise ValueError("invalid_preparation_binding")
        if len(canonical_json(raw["catalog"]).encode()) > MAX_OFFER_BYTES:
            raise ValueError("size_limit")
    elif raw["catalog"] is not None or raw["budget_ms"] is not None:
        raise ValueError("invalid_preparation_request")
    if len(canonical_json(raw).encode()) > 33 * 1024 * 1024:
        raise ValueError("size_limit")
    return raw


def parse_preparation_response(value: object, request: Mapping) -> dict:
    raw = validate_document(value, _VALIDATORS["response"])
    _binding(raw["binding"])
    if any(raw[k] != request[k] for k in ("preparation_id", "binding", "profile")):
        raise ValueError("invalid_preparation_binding")
    if raw["index_revision"] is not None:
        text(raw["index_revision"], 256)
    if raw["state"] == "ready":
        if raw["index_revision"] is None or raw["reused"] is None or raw["reason"] is not None:
            raise ValueError("invalid_preparation_response")
        if raw["profile"] == "cold" and raw["reused"]:
            raise ValueError("invalid_preparation_response")
    elif raw["index_revision"] is not None or raw["reused"] is not None:
        raise ValueError("invalid_preparation_response")
    elif (raw["state"] == "indexing") != (raw["reason"] is None):
        raise ValueError("invalid_preparation_response")
    return raw


class _Stopped(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


async def prepare_asset_index(
    config: Mapping,
    binding: Mapping | None,
    catalog: list[dict],
    *,
    cancelled: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = time.monotonic,
    transport_factory: Callable | None = None,
) -> dict:
    """Perform one finite lifecycle, retaining ordered exchanges and unknown costs."""
    config = normalize_preparation(config)
    record = dict(
        config=config,
        binding=None,
        preparation_id=None,
        status="not_applicable",
        reason=None,
        index_revision=None,
        observed_ms=None,
        cleanup_ms=None,
        exchanges=[],
        observed_costs=[],
    )
    if config["mode"] == "none":
        return record
    started = clock()
    deadline = started + config["total_ms"] / 1000
    record.update(binding=copy.deepcopy(binding), preparation_id=str(uuid.uuid4()), status="failed")
    dispatched = False

    def check():
        if cancelled():
            raise _Stopped("cancelled", "cancelled")
        if clock() >= deadline:
            raise _Stopped("timeout", "deadline_exceeded")

    async def exchange(operation, *, cleanup=False):
        nonlocal dispatched
        now = clock()
        end = cleanup_started + 1.0 if cleanup else min(deadline, now + config["request_ms"] / 1000)
        request = dict(
            schema_version=1,
            preparation_id=record["preparation_id"],
            operation=operation,
            binding=binding,
            profile=config["profile"],
            catalog=catalog if operation == "prepare" else None,
            budget_ms=max(1, math.ceil((deadline - now) * 1000))
            if operation == "prepare"
            else None,
        )
        request = parse_preparation_request(request)
        observation = dict(operation=operation, observed_ms=0, response=None, reason=None)
        record["exchanges"].append(observation)

        def exchange_check():
            if not cleanup:
                check()
            if clock() >= end:
                raise TimeoutError

        try:
            exchange_check()
            if operation == "prepare":
                dispatched = True
            async with asyncio.timeout(max(0, end - clock())):
                raw = await post_json(
                    config["url"],
                    canonical_json(request).encode(),
                    SelectorContext(clock, exchange_check),
                    transport_factory=transport_factory,
                )
                try:
                    result = parse_preparation_response(raw, request)
                    if cleanup and len(record["exchanges"]) > 1:
                        terminal = record["exchanges"][-2]["response"]
                        if (
                            terminal is not None
                            and terminal["state"] in ("failed", "cancelled", "stale_catalog")
                            and result["state"] != terminal["state"]
                        ):
                            raise ValueError("invalid_preparation_terminal_transition")
                    exchange_check()
                except ValueError:
                    raise AttemptFailure("invalid", "invalid_response") from None
                observation["response"] = result
                return result
        except AttemptFailure as exc:
            observation["reason"] = exc.reason
            raise
        except (TimeoutError, _Stopped, asyncio.CancelledError) as exc:
            is_cancelled = isinstance(exc, asyncio.CancelledError) or (
                isinstance(exc, _Stopped) and exc.status == "cancelled"
            )
            observation["reason"] = "cancelled" if is_cancelled else "deadline_exceeded"
            raise
        finally:
            observation["observed_ms"] = max(0, (clock() - now) * 1000)

    try:
        check()
        result = await exchange("prepare")
        polls = 0
        while result["state"] == "indexing":
            if polls >= config["max_polls"]:
                raise _Stopped("failed", "poll_limit")
            until = min(deadline, clock() + config["poll_ms"] / 1000)
            while clock() < until:
                check()
                await asyncio.sleep(min(0.05, until - clock()))
            check()
            result = await exchange("status")
            polls += 1
        record.update(
            status=result["state"], reason=result["reason"], index_revision=result["index_revision"]
        )
    except _Stopped as exc:
        record.update(status=exc.status, reason=exc.reason)
    except AttemptFailure as exc:
        record.update(status="timeout" if exc.outcome == "timeout" else "failed", reason=exc.reason)
    except TimeoutError:
        record.update(status="timeout", reason="deadline_exceeded")
    except asyncio.CancelledError:
        record.update(status="cancelled", reason="cancelled")
    finally:
        record["observed_ms"] = max(0, (clock() - started) * 1000)
        if dispatched and record["status"] != "ready":
            cleanup_started = clock()
            try:
                await exchange("cancel", cleanup=True)
            except (AttemptFailure, TimeoutError, _Stopped, asyncio.CancelledError):
                pass
            record["cleanup_ms"] = max(0, (clock() - cleanup_started) * 1000)
    return record


def validate_preparation_record(
    record: Mapping, *, config: Mapping | None = None, binding: Mapping | None = None
) -> None:
    """Recheck transcript semantics before an offline report can earn grades."""
    normalized = normalize_preparation(record["config"], local_primary=True)
    if normalized != record["config"] or config is not None and normalized != config:
        raise ValueError("preparation_config_binding")
    if normalized["mode"] == "none":
        expected = dict(
            config=normalized,
            binding=None,
            preparation_id=None,
            status="not_applicable",
            reason=None,
            index_revision=None,
            observed_ms=None,
            cleanup_ms=None,
            exchanges=[],
            observed_costs=[],
        )
        if record != expected:
            raise ValueError("invalid_preparation_record")
        return
    if normalized["mode"] == "local":
        _validate_local_preparation(record, binding=binding)
        return
    if binding is not None and record["binding"] != binding:
        raise ValueError("preparation_binding")
    if (
        record["binding"] is None
        or record["preparation_id"] is None
        or record["status"] == "not_applicable"
    ):
        raise ValueError("invalid_preparation_record")
    if record["reason"] is not None and record["reason"] not in _REASONS:
        raise ValueError("invalid_preparation_reason")
    # This compiled client has no preparation collector. Provenance text is
    # not an observation; all server costs remain reported inside responses.
    if record["observed_costs"]:
        raise ValueError("unsupported_preparation_costs")
    exchanges = record["exchanges"]
    if not exchanges:
        if (
            (record["status"], record["reason"])
            not in (("cancelled", "cancelled"), ("timeout", "deadline_exceeded"))
            or record["index_revision"] is not None
            or record["cleanup_ms"] is not None
        ):
            raise ValueError("invalid_preparation_record")
        return
    cleanup = exchanges[-1] if exchanges[-1]["operation"] == "cancel" else None
    main = exchanges[:-1] if cleanup is not None else exchanges
    if (
        not main
        or main[0]["operation"] != "prepare"
        or any(e["operation"] != "status" for e in main[1:])
        or len(main) - 1 > normalized["max_polls"]
    ):
        raise ValueError("invalid_preparation_sequence")
    if cleanup is None:
        if record["cleanup_ms"] is not None:
            raise ValueError("invalid_preparation_cleanup")
    elif (
        record["status"] == "ready"
        or record["cleanup_ms"] is None
        or record["cleanup_ms"] + 1e-6 < cleanup["observed_ms"]
    ):
        raise ValueError("invalid_preparation_cleanup")
    measured = sum(e["observed_ms"] for e in main) + (len(main) - 1) * normalized["poll_ms"]
    if record["observed_ms"] is not None and record["observed_ms"] + 1e-6 < measured:
        raise ValueError("invalid_preparation_time")
    request = dict(
        preparation_id=record["preparation_id"],
        binding=record["binding"],
        profile=normalized["profile"],
    )
    previous = None
    for position, exchange in enumerate(main):
        if position and (previous is None or previous["state"] != "indexing"):
            raise ValueError("invalid_preparation_transition")
        previous = _verified_exchange(exchange, request, normalized["request_ms"])
    # Only the main prepare/status result can establish readiness or a terminal
    # server outcome. A cleanup ready acknowledgement never upgrades the run.
    if previous is not None and previous["state"] != "indexing":
        if (record["status"], record["reason"], record["index_revision"]) != (
            previous["state"],
            previous["reason"],
            previous["index_revision"],
        ):
            raise ValueError("invalid_preparation_outcome")
    else:
        if record["index_revision"] is not None:
            raise ValueError("invalid_preparation_outcome")
        if previous is None:
            reason = main[-1]["reason"]
            status = (
                "cancelled"
                if reason == "cancelled"
                else ("timeout" if reason == "deadline_exceeded" else "failed")
            )
            if (record["status"], record["reason"]) != (status, reason):
                raise ValueError("invalid_preparation_outcome")
        elif (record["status"], record["reason"]) not in (
            ("cancelled", "cancelled"),
            ("timeout", "deadline_exceeded"),
            ("failed", "poll_limit"),
        ):
            raise ValueError("invalid_preparation_outcome")
        if record["reason"] == "poll_limit" and len(main) - 1 != normalized["max_polls"]:
            raise ValueError("invalid_preparation_poll_limit")
    if record["status"] == "ready":
        if record["observed_ms"] is not None and record["observed_ms"] > normalized["total_ms"]:
            raise ValueError("invalid_preparation_deadline")
    elif cleanup is None and (
        previous is not None or main[-1]["reason"] not in ("cancelled", "deadline_exceeded")
    ):
        raise ValueError("missing_preparation_cleanup")
    if cleanup is not None:
        acknowledgement = _verified_exchange(cleanup, request, 1000)
        if acknowledgement is not None and record["cleanup_ms"] > 1000 + 1e-3:
            raise ValueError("invalid_preparation_cleanup_deadline")
        if (
            previous is not None
            and previous["state"] != "indexing"
            and acknowledgement is not None
            and acknowledgement["state"] != previous["state"]
        ):
            raise ValueError("invalid_preparation_terminal_transition")


def _verified_exchange(exchange: Mapping, request: Mapping, request_ms: int) -> dict | None:
    reason = exchange["reason"]
    if reason is not None and reason not in _REASONS:
        raise ValueError("invalid_preparation_reason")
    if exchange["response"] is None:
        if reason is None:
            raise ValueError("invalid_preparation_record")
        # Retain actual slow failures, rather than treating them as successful
        # deadline-bound responses or discarding their measured duration.
        return None
    if reason is not None or exchange["observed_ms"] > request_ms + 1e-3:
        raise ValueError("invalid_preparation_exchange")
    return parse_preparation_response(exchange["response"], request)


class PreparedAssetSelector:
    """Validate pinned preparation identity before ordinary v2 coordinator admission."""

    def __init__(self, primary, preparation: Mapping) -> None:
        self.primary, self.preparation = primary, copy.deepcopy(preparation)
        self.name, self.version = primary.name, primary.version
        self.builtin = getattr(primary, "builtin", False)
        self.transport = getattr(primary, "transport", "in_process" if self.builtin else "http")
        self.mechanism = getattr(primary, "mechanism", self.transport)

    def __getattr__(self, name):
        # Actual request observations stay owned by the underlying selector.
        return getattr(self.primary, name)

    async def select(self, request, context: SelectorContext) -> object:
        binding = preparation_binding(
            selector=dict(name=self.name, version=self.version),
            catalog_revision=request.data["catalog_revision"],
            catalog=list(request.data["catalog"]),
            scope=request.data["scope"],
            permissions=request.data["permissions"],
            selection_scope=request.data["selection_scope"],
            requested_kinds=request.data["requested_kinds"],
        )
        if self.preparation["status"] != "ready" or binding != self.preparation["binding"]:
            raise AttemptFailure("stale", "stale_result")
        raw = await self.primary.select(request, context)
        result = parse_asset_result(raw).to_json_object()
        if (
            result["outcome"] in ("ranked", "no_match")
            and result["index_revision"] != self.preparation["index_revision"]
        ):
            raise AttemptFailure("stale", "stale_result", parse_asset_result(raw))
        return raw
