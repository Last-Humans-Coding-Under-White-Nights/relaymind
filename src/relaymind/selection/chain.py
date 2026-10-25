"""The selector chain: each selector in order, with its time limit and its outcome (RM28).

Spec 0.31 RM28 and section 11. The chain tries the configured selectors
in order. A result of none, a timeout, an error, or an inadmissible
result falls through to the next selector and is recorded as an
attempt; the first admitted result ends the chain. A selector that is
not a model gets selection.timeout_seconds (two seconds by default) and
is cancelled when it runs out; a model-based selector is bounded by its
generation's limits instead, and only it gets the routing generation
callback.

A failure of the gateway's own routing generation (a budget stop, an
unavailable model, a cancel) is not a selector error: it leaves the
chain unchanged, so the run stops exactly as it did before the chain
existed (RM03, RM04).

The time limit uses an injectable sleep and the elapsed times an
injectable clock, so tests never wait on the wall clock.
"""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from relaymind.orchestrator.placement import PlacementDeclined
from relaymind.selection.contract import (
    CandidateKind,
    SelectionContext,
    SelectionError,
    SelectionRequest,
    SelectionResult,
    Selector,
    parse_selection_result,
)

__all__ = [
    "EXHAUSTED_MECHANISM",
    "Attempt",
    "ChainResult",
    "InadmissibleError",
    "Outcome",
    "SelectorChain",
]


# The route.selected mechanism of the gateway's own decision when the chain chose
# nothing (route external, reason selection_failed; spec 0.36 section 8).
EXHAUSTED_MECHANISM = "selector_chain"

# Mechanism labels a selector may not claim (section 8): the gateway's own, and those
# of the built-in selectors, which only they may use. A result that claims one is an error.
GATEWAY_MECHANISMS = frozenset(
    {
        "routing_cache",
        "bypass",
        "execution_mode",
        "degraded_mode",
        "clarification_resume",
        "client_owned",
        EXHAUSTED_MECHANISM,
    }
)
BUILT_IN_MECHANISMS: Mapping[str, str] = {
    "native_function_calling": "model_router",
    "example_match": "example_match",
}


class Outcome(StrEnum):
    """How one selector attempt ended (section 8, route.selected attempts)."""

    SELECTED = "selected"
    NONE = "none"
    TIMEOUT = "timeout"
    ERROR = "error"
    INADMISSIBLE = "inadmissible"
    # Spec 0.31 RM27: the placement policy did not run a model-based selector (#130).
    NOT_PLACED = "not_placed"


class InadmissibleError(Exception):
    """The gateway refused a valid result: an unknown skill, a mode, or a rule (RM28)."""


@dataclass(frozen=True)
class Attempt:
    """One selector tried. detail says why it fell through; it is not traced."""

    name: str
    outcome: Outcome
    elapsed_ms: int
    model_based: bool = False
    detail: str | None = None

    def to_json_object(self) -> dict[str, str]:
        return {"name": self.name, "outcome": self.outcome.value}


@dataclass(frozen=True)
class ChainResult[T]:
    """The attempts, and the selector, result, and admission of the one that was admitted."""

    attempts: tuple[Attempt, ...]
    elapsed_ms: int
    selector: Selector | None = None
    result: SelectionResult | None = None
    admitted: T | None = None

    def rejected_model_output(self) -> Attempt | None:
        """The first model-based attempt whose output was rejected (RM28: invalid_output)."""
        return next(
            (
                attempt
                for attempt in self.attempts
                if attempt.model_based and attempt.outcome in (Outcome.ERROR, Outcome.INADMISSIBLE)
            ),
            None,
        )

    def selector_record(self) -> dict[str, Any]:
        """The selector field of route.selected (section 8).

        name, version, and mechanism are those of the admitted selector,
        or null when the chain chose nothing; elapsed_ms is its time, or
        the whole chain's then.
        """
        if self.selector is None or self.result is None:
            name = version = mechanism = None
            elapsed = self.elapsed_ms
        else:
            name, version = self.selector.name, self.selector.version
            mechanism = self.result.mechanism
            elapsed = self.attempts[-1].elapsed_ms
        return {
            "name": name,
            "version": version,
            "mechanism": mechanism,
            "elapsed_ms": elapsed,
            "attempts": [attempt.to_json_object() for attempt in self.attempts],
        }


class _GatewayStop(Exception):
    def __init__(self, cause: Exception | asyncio.CancelledError) -> None:
        super().__init__(str(cause))
        self.cause = cause


_TIMED_OUT = object()


class SelectorChain:
    """The configured selectors, in order, with the selection time limit."""

    def __init__(
        self,
        selectors: Sequence[Selector],
        *,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        names = [selector.name for selector in selectors]
        if not names:
            raise ValueError("the selector chain needs at least one selector")
        if len(set(names)) != len(names):
            raise ValueError("a selector appears more than once in the chain")
        self.selectors = tuple(selectors)
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._sleep = sleep

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(selector.name for selector in self.selectors)

    @property
    def identity(self) -> tuple[tuple[str, str], ...]:
        """Each selector's name and version, in order (the routing cache key, section 10)."""
        return tuple((selector.name, selector.version) for selector in self.selectors)

    def _elapsed_ms(self, started: float) -> int:
        return max(0, round((self._clock() - started) * 1000))

    async def run[T](
        self,
        request: SelectionRequest,
        context: SelectionContext,
        admit: Callable[[SelectionResult], T],
    ) -> ChainResult[T]:
        """Try each selector until one result is admitted; admit raises InadmissibleError."""
        attempts: list[Attempt] = []
        started = self._clock()
        for selector in self.selectors:
            began = self._clock()
            outcome, detail, parsed, admitted = await self._attempt(
                selector, request, context, admit
            )
            attempts.append(
                Attempt(
                    selector.name,
                    outcome,
                    self._elapsed_ms(began),
                    selector.model_based,
                    detail,
                )
            )
            if outcome == Outcome.SELECTED:
                return ChainResult(
                    tuple(attempts), self._elapsed_ms(started), selector, parsed, admitted
                )
        return ChainResult(tuple(attempts), self._elapsed_ms(started))

    async def _attempt[T](
        self,
        selector: Selector,
        request: SelectionRequest,
        context: SelectionContext,
        admit: Callable[[SelectionResult], T],
    ) -> tuple[Outcome, str | None, SelectionResult | None, T | None]:
        stops: list[Exception | asyncio.CancelledError] = []
        try:
            raw = await self._call(selector, request, context, stops)
        except _GatewayStop as stop:
            if isinstance(stop.cause, PlacementDeclined):
                return Outcome.NOT_PLACED, str(stop.cause), None, None
            raise stop.cause from None
        except Exception as exc:  # a selector's own failure falls through (RM28)
            if stops:
                if isinstance(stops[0], PlacementDeclined):
                    return Outcome.NOT_PLACED, str(stops[0]), None, None
                raise stops[0] from None
            return Outcome.ERROR, f"{type(exc).__name__}: {exc}", None, None
        if stops:
            # A selector cannot swallow a callback stop.
            if isinstance(stops[0], PlacementDeclined):
                return Outcome.NOT_PLACED, str(stops[0]), None, None
            raise stops[0]
        if raw is _TIMED_OUT:
            return Outcome.TIMEOUT, f"no result within {self.timeout_seconds} seconds", None, None
        try:
            result = parse_selection_result(raw)
            _check_identity(selector, result)
        except SelectionError as exc:
            return Outcome.ERROR, str(exc), None, None
        if result.candidate.kind == CandidateKind.NONE:
            return Outcome.NONE, None, result, None
        try:
            admitted = admit(result)
        except InadmissibleError as exc:
            return Outcome.INADMISSIBLE, str(exc), result, None
        return Outcome.SELECTED, None, result, admitted

    async def _call(
        self,
        selector: Selector,
        request: SelectionRequest,
        context: SelectionContext,
        stops: list[Exception | asyncio.CancelledError],
    ) -> Any:
        if selector.model_based:
            guarded = SelectionContext(context.messages, _guard(context, stops))
            return await selector.select(request.with_deadline(None), guarded)
        deadline_ms = max(1, round(self.timeout_seconds * 1000))
        work = asyncio.ensure_future(
            selector.select(request.with_deadline(deadline_ms), context.without_generation())
        )
        timer = asyncio.ensure_future(self._sleep(self.timeout_seconds))
        try:
            done, _ = await asyncio.wait((work, timer), return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Neither task outlives the attempt, also when the run is cancelled here.
            for task in (work, timer):
                task.cancel()
            await asyncio.gather(work, timer, return_exceptions=True)
        if work in done and not work.cancelled():
            return work.result()
        return _TIMED_OUT


def _guard(context: SelectionContext, stops: list[Exception | asyncio.CancelledError]) -> Any:
    """The routing generation callback, with the gateway's own failures marked."""
    generate = context.generate
    if generate is None:
        return None

    async def guarded(messages: Any, functions: Any, options: Any) -> Any:
        try:
            return await generate(messages, functions, options)
        except (Exception, asyncio.CancelledError) as exc:
            # Cancellation is a gateway stop too; a selector cannot swallow it.
            stops.append(exc)
            raise _GatewayStop(exc) from exc

    return guarded


def _check_identity(selector: Selector, result: SelectionResult) -> None:
    """A result names its own selector and claims no mechanism it does not own (section 8)."""
    if (result.selector_name, result.selector_version) != (selector.name, selector.version):
        raise SelectionError(
            f"the result names selector {result.selector_name} {result.selector_version},"
            f" not {selector.name} {selector.version}"
        )
    owner = BUILT_IN_MECHANISMS.get(result.mechanism)
    if result.mechanism in GATEWAY_MECHANISMS or (owner is not None and owner != selector.name):
        raise SelectionError(f"mechanism {result.mechanism!r} is reserved")
