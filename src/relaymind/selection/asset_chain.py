"""One primary and one distinct lexical floor under one absolute clock (RM35)."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from relaymind.assets.catalog import CatalogSnapshot, CatalogUnavailable
from relaymind.schema_util import freeze_json
from relaymind.selection.asset_admission import AssetPolicy, admit_current, validate_against_offer
from relaymind.selection.asset_contract import (
    AssetSelectionError,
    AssetSelectionRequest,
    AssetSelector,
    AttemptFailure,
    RecommendationSelectionResult,
    SelectorContext,
    SelectorIdentity,
    parse_asset_result,
    parse_recommendation_result,
)
from relaymind.store.run_store import TraceClock, utc_iso


class RecommendationFailure(Exception):
    """An internal lifecycle failure, with immutable prior attempt evidence."""

    def __init__(self, code: str, reason: str, attempts=()):
        super().__init__(reason)
        self.code, self.reason = code, reason
        self.attempts = tuple(freeze_json(a) for a in attempts)


class SelectionBudget:
    def __init__(
        self,
        total_ms: int = 500,
        *,
        clock: TraceClock | None = None,
        cancelled: Callable[[], bool] | None = None,
        sleep: Callable[[float], Awaitable] = asyncio.sleep,
    ):
        if type(total_ms) is not int or total_ms <= 0:
            raise ValueError("total_ms must be a positive integer")
        self.clock = clock or TraceClock()
        self.started = self.clock.monotonic()
        self.deadline = self.started + total_ms / 1000
        self._cancelled, self.sleep = cancelled, sleep
        self._cancel_event = asyncio.Event()
        self._terminal = False
        self._owned: set[asyncio.Task] = set()

    def cancel(self) -> None:
        self._cancel_event.set()

    def _stopped(self) -> bool:
        return (
            self._cancel_event.is_set()
            or self._terminal
            or (self._cancelled is not None and self._cancelled())
        )

    def check(self) -> None:
        if self._stopped():
            raise RecommendationFailure("cancelled", "cancelled")
        if self.clock.monotonic() >= self.deadline:
            raise RecommendationFailure("deadline_exceeded", "deadline_exceeded")

    def remaining_ms(self) -> float:
        self.check()
        return max(0, (self.deadline - self.clock.monotonic()) * 1000)

    def fence_terminal(self) -> None:
        """The lifecycle owner calls this after a terminal atomic transition."""
        self._terminal = True
        self.cancel()

    def elapsed_ms(self) -> float:
        return max(0, (self.clock.monotonic() - self.started) * 1000)

    def own(self, task: asyncio.Task) -> None:
        self._owned.add(task)

        def finished(done):
            self._owned.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)

    async def drain(self, timeout: float) -> None:
        """Bounded owner cleanup; resistant tasks stay tracked until completion."""
        if self._owned:
            await asyncio.wait(tuple(self._owned), timeout=timeout)

    async def wait_cancelled(self) -> None:
        if self._cancelled is None:
            # Every stop goes through the event: no polling.
            await self._cancel_event.wait()
            return
        # An external cancelled() callable cannot signal, so it is polled.
        while not self._stopped():
            try:
                await asyncio.wait_for(self._cancel_event.wait(), timeout=0.01)
            except TimeoutError:
                pass


@dataclass(frozen=True)
class AttemptStarted:
    attempt: int
    selector: SelectorIdentity
    mechanism: str
    remaining_ms: float


@dataclass(frozen=True)
class AttemptFinished:
    attempt_record: object


AttemptObserver = Callable[[AttemptStarted | AttemptFinished], object]


def _capability(selector: AssetSelector, name: str, default: object = None) -> Any:
    """A capability the selector class declares (see AssetSelector)."""
    return getattr(selector, name, default)


def _task_cancelled() -> bool:
    """The running task itself is being cancelled, not the request's budget."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _attempt_request(
    request: AssetSelectionRequest, deadline: float, clock: TraceClock
) -> AssetSelectionRequest:
    """The request with this attempt's remaining time, from one clock read."""
    remaining = deadline - clock.monotonic()
    if remaining <= 0:
        raise AttemptFailure("timeout", "deadline_exceeded")
    return request.with_deadline(remaining * 1000)


async def _invoke(selector, request, context):
    try:
        return await selector.select(request, context)
    except (AttemptFailure, RecommendationFailure, asyncio.CancelledError):
        raise
    except Exception:
        if _capability(selector, "builtin", False):
            raise AttemptFailure("unavailable", "selector_unavailable") from None
        raise


async def _bounded(work: Awaitable, budget: SelectionBudget, deadline: float) -> Any:
    task = asyncio.create_task(work)
    budget.own(task)
    timer = asyncio.create_task(budget.sleep(max(0, deadline - budget.clock.monotonic())))
    cancel = asyncio.create_task(budget.wait_cancelled())
    try:
        while True:
            done, _ = await asyncio.wait((task, timer, cancel), return_when=asyncio.FIRST_COMPLETED)
            budget.check()  # Cancellation always wins ties with result/timeout.
            remaining = deadline - budget.clock.monotonic()
            if remaining <= 0:
                raise AttemptFailure("timeout", "deadline_exceeded")
            if task in done:
                return task.result()
            # A platform timer can wake before the absolute clock deadline.
            # Keep the same attempt alive for its remaining allowance.
            if timer in done:
                timer = asyncio.create_task(budget.sleep(remaining))
    finally:
        # Do not await cancellation-resistant selector cleanup past service time.
        for pending in (task, timer, cancel):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(timer, cancel, return_exceptions=True)


async def _observe(observer, event, budget):
    if observer is not None:
        value = observer(event)
        if inspect.isawaitable(value):
            await _bounded(value, budget, budget.deadline)


def _attempt(selector, ordinal, mechanism, outcome, reason, started, budget, parsed):
    data = None if parsed is None else parsed.to_json_object()
    costs = []
    observed = _capability(selector, "observed_cost")
    if observed is not None:
        costs.append({"provenance": "observed", "value": dict(observed)})
    if data is not None and data["usage_claims"] is not None:
        costs.append(data["usage_claims"])
    return dict(
        attempt=ordinal,
        selector={"name": selector.name, "version": selector.version},
        mechanism=mechanism,
        outcome=outcome,
        reason=reason,
        observed_ms=max(0, (budget.clock.monotonic() - started) * 1000),
        reported_ms=None if data is None else data["elapsed_ms"],
        index_revision=None if data is None else data["index_revision"],
        cost_observations=costs,
        result=data,
    )


async def select_assets(
    request: AssetSelectionRequest,
    *,
    primary: AssetSelector,
    lexical_floor: AssetSelector | None,
    budget: SelectionBudget,
    primary_ms: int,
    fallback_enabled: bool,
    current_catalog: Callable[[], CatalogSnapshot],
    policy: AssetPolicy,
    trusted_scope: Mapping[str, str],
    trusted_permissions: Sequence[str],
    observe: AttemptObserver | None = None,
) -> RecommendationSelectionResult:
    attempts = []
    fallback_reason = None
    try:
        budget.check()
        if request.data["scope"] != dict(trusted_scope) or set(request.data["permissions"]) != set(
            trusted_permissions
        ):
            raise RecommendationFailure("invalid_request", "invalid_request")
        if type(primary_ms) is not int or primary_ms <= 0:
            raise ValueError
        if request.data["selection_scope"] != "discovery":
            raise RecommendationFailure("recommendation_unavailable", "selector_unavailable")
        if not request.data["catalog"]:
            current = current_catalog()
            budget.check()
            if current.catalog_revision != request.data["catalog_revision"]:
                raise RecommendationFailure("recommendation_unavailable", "stale_result")
            return _ready(
                request, attempts, [], "empty_catalog", None, current.catalog_revision, budget
            )
        selectors = [primary]
        # Lexical is never repeated as its own fallback.
        if (
            fallback_enabled
            and lexical_floor is not None
            and _capability(primary, "mechanism") != _capability(lexical_floor, "mechanism")
        ):
            selectors.append(lexical_floor)
        for ordinal, selector in enumerate(selectors, 1):
            budget.check()
            started = budget.clock.monotonic()
            deadline = (
                min(budget.deadline, started + primary_ms / 1000)
                if ordinal == 1
                else budget.deadline
            )

            def check_attempt(deadline=deadline):
                budget.check()
                if budget.clock.monotonic() >= deadline:
                    raise AttemptFailure("timeout", "deadline_exceeded")

            context = SelectorContext(budget.clock.monotonic, check_attempt)
            identity = SelectorIdentity(selector.name, selector.version)
            mechanism = (
                "gateway_lexical_fallback"
                if ordinal == 2
                else _capability(selector, "mechanism", "selector")
            )
            parsed = None
            outcome = reason = admission = None
            terminal_failure = None
            # Observers/persistence belong to the gateway, never selector fallback.
            await _observe(
                observe,
                AttemptStarted(
                    ordinal,
                    identity,
                    mechanism,
                    max(0, (deadline - started) * 1000),
                ),
                budget,
            )
            budget.check()
            try:
                budget.check()
                raw = await _bounded(
                    _invoke(selector, _attempt_request(request, deadline, budget.clock), context),
                    budget,
                    deadline,
                )
                check_attempt()
                try:
                    parsed = parse_asset_result(raw)
                except AssetSelectionError:
                    raise AttemptFailure("invalid", "invalid_result") from None
                check_attempt()
                validate_against_offer(
                    parsed,
                    request,
                    identity,
                    http=_capability(selector, "transport") == "http",
                )
                check_attempt()
                if ordinal == 1:
                    mechanism = parsed.data["mechanism"]
                outcome = parsed.data["outcome"]
                if outcome in ("not_ready", "unavailable"):
                    reason = (
                        "selector_not_ready" if outcome == "not_ready" else "selector_unavailable"
                    )
                else:
                    checked_admission = admit_current(parsed, request, current_catalog(), policy)
                    check_attempt()
                    # Only a fully fenced attempt may supply the ready result.
                    admission = checked_admission
            except CatalogUnavailable:
                outcome, reason = "unavailable", "selector_unavailable"
                terminal_failure = ("recommendation_unavailable", "corrupt_catalog")
            except AttemptFailure as failure:
                outcome, reason = failure.outcome, failure.reason
                if failure.result is not None:
                    parsed = failure.result
            except RecommendationFailure as failure:
                outcome = "cancelled" if failure.code == "cancelled" else "timeout"
                reason = "cancelled" if outcome == "cancelled" else "deadline_exceeded"
            except asyncio.CancelledError:
                budget.cancel()
                if _task_cancelled():
                    raise
                # A selector's own cancellation is a cancelled attempt.
                outcome, reason = "cancelled", "cancelled"
            except Exception:
                # Only a built-in dependency failure is a declared unavailable
                # selector. Policy/store/admission infrastructure must be terminal.
                raise RecommendationFailure("internal_error", "internal_error", attempts) from None
            record = _attempt(
                selector, ordinal, mechanism, outcome, reason, started, budget, parsed
            )
            attempts.append(record)
            await _observe(observe, AttemptFinished(freeze_json(record)), budget)
            budget.check()
            if terminal_failure is not None:
                raise RecommendationFailure(*terminal_failure, attempts)
            if admission is not None:
                return _ready(
                    request,
                    attempts,
                    parsed.data["candidates"],
                    parsed.data["decision"],
                    fallback_reason,
                    admission.rechecked_catalog_revision,
                    budget,
                )
            if ordinal == 1:
                fallback_reason = reason
        raise RecommendationFailure(
            "recommendation_unavailable", fallback_reason or "selector_unavailable", attempts
        )
    except CatalogUnavailable:
        raise RecommendationFailure(
            "recommendation_unavailable", "corrupt_catalog", attempts
        ) from None
    except RecommendationFailure as failure:
        raise RecommendationFailure(failure.code, failure.reason, attempts) from None
    except asyncio.CancelledError:
        budget.cancel()
        if _task_cancelled():
            raise
        raise RecommendationFailure("cancelled", "cancelled", attempts) from None
    except Exception:
        raise RecommendationFailure("internal_error", "internal_error", attempts) from None


def _ready(request, attempts, candidates, decision, fallback_reason, revision, budget):
    budget.check()
    value = dict(
        schema_version=1,
        decision=decision,
        catalog_revision=request.data["catalog_revision"],
        host_snapshot_revision=None,
        attempts=attempts,
        # The primary is always the first attempt; empty_catalog dispatches none.
        primary_attempt=0 if attempts else None,
        base_candidates=candidates,
        base_decision=decision,
        final_candidates=candidates,
        final_decision=decision,
        fallback_reason=fallback_reason,
        adaptation=None,
        admission={
            "outcome": "accepted",
            "rechecked_catalog_revision": revision,
            "checked_at": utc_iso(budget.clock.utc_now()),
        },
        observed_ms=budget.elapsed_ms(),
        embeddings=[],
        host_run_ids=[],
    )
    result = parse_recommendation_result(value)
    budget.check()
    return result
