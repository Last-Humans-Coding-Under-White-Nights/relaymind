"""The startup warm-up of the local model (#124; RM18, RM25, RM27; spec 0.31 sections 6 and 14.1).

At startup, after restart recovery and before the server socket exists,
the gateway sends one routing-shaped and one skill-shaped generation to
the local model, so the first real request does not pay for loading the
model and for the first prefill of the constant prompt prefix (section
10). The requests are built by the gateway's own builders
(``Router.build_request`` and ``skill_stage_messages``) over a fixed
warm-up text, so they are what a run would send. They are not runs: no
run, no event, no budget, no tool call, and the model's output is
dropped. The whole warm-up is bounded by ``warmup_seconds``, and a
runtime that does not answer in time is reported, not a startup error.

The timings (duration, first token, and the token counts the runtime
reported, cached tokens among them) are kept in ``WarmupResult``. A count
the runtime did not report is None, never 0 (RM16). Nothing here touches
the pinned launch settings of the runtime (RM18).

Detection (RM26, spec 0.37 section 14.1). With placement.detect true,
``detect_local_model`` classifies the local model first: the runtime must
answer with the model loaded within warmup_seconds, or the model is
unavailable; then, with warmup true, the routing-shaped warm-up must
finish within the local per-stage active budget, or the model is
unsuitable; with placement.detect_stages true (spec 0.48) the
routing-shaped and skill-shaped warm-ups must finish within it together,
because a local stage needs both. They are the first requests after the
model loaded, so they are measured cold when the runtime has just started.
A warm-up that fails, or that is cut off by warmup_seconds before the stage
budget ran out, says the model could not generate, so it is unavailable.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

from relaymind.adapters import (
    AdapterHealth,
    FirstToken,
    GenerationFailed,
    GenerationRequest,
    Message,
    ModelAdapter,
    Role,
    TextDelta,
    ToolCall,
    Usage,
)
from relaymind.orchestrator.local_loop import LOCAL_OPTIONS, LocalOrchestrator, skill_stage_messages
from relaymind.orchestrator.local_status import LocalModelClass, LocalStatus
from relaymind.store import ModelTiming
from relaymind.store.model_timings import observed_cached_tokens

# Not a corpus or latency prompt (a repeated text would be served whole from the
# cache and inflate the cached count), and not a request any run sends.
WARMUP_TEXT = "Warm-up request from the gateway at startup. No answer is needed."
# The warm-up only needs the prompt processed; a short reply keeps it quick.
SKILL_WARMUP_MAX_TOKENS = 32
# How often detection asks a runtime that is not ready yet (RM26).
HEALTH_POLL_SECONDS = 0.5


@dataclass(frozen=True)
class WarmupSample:
    """The timings of one warm-up generation (RM25); None is a figure not reported."""

    purpose: str
    duration_ms: int
    first_token_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None

    def to_json_object(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WarmupResult:
    """What the warm-up did: the finished samples, and why it stopped early, if it did.

    ready is True when every request finished. When it is False, detail
    says why in one line, and the samples that did finish are kept.
    """

    samples: tuple[WarmupSample, ...]
    ready: bool
    detail: str | None = None
    # True when warmup_seconds cut the warm-up off (detection reads it; RM26).
    timed_out: bool = False

    def summary(self) -> str:
        """The one line the launcher prints when the warm-up finished; n/a is not reported."""

        def figure(value: int | None, unit: str = "") -> str:
            return "n/a" if value is None else f"{value}{unit}"

        parts = [
            f"{s.purpose} {s.duration_ms} ms "
            f"(first token {figure(s.first_token_ms, ' ms')}, cached {figure(s.cached_tokens)})"
            for s in self.samples
        ]
        return "Local warm-up finished: " + ", ".join(parts) + "."

    def to_json_object(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "detail": self.detail,
            "samples": [sample.to_json_object() for sample in self.samples],
        }


def warmup_requests(orchestrator: LocalOrchestrator) -> list[tuple[str, GenerationRequest]]:
    """The routing-shaped request, and a skill-shaped one when a skill has permitted tools.

    The skill is the first offered skill whose tools this caller may use
    (RM08), the same test a run applies; without one only the routing
    request is sent.
    """
    messages = [Message(role=Role.USER, content=WARMUP_TEXT)]
    requests = [("routing", orchestrator.router.build_request(messages, "warmup_routing"))]
    for name in orchestrator.router.offered:
        skill = orchestrator.skills.get(name)
        available = orchestrator.tools.available_tools(
            skill_allowlist=skill.allowed_tools,
            policy=orchestrator.policy,
            caller=orchestrator.caller,
        )
        schemas = orchestrator.tools.tool_schemas(n for n in skill.allowed_tools if n in available)
        if schemas:
            conversation = skill_stage_messages(skill, messages, [s.name for s in schemas])
            options = replace(LOCAL_OPTIONS, max_output_tokens=SKILL_WARMUP_MAX_TOKENS)
            request = GenerationRequest(
                generation_id="warmup_skill",
                messages=tuple(conversation),
                tool_schemas=tuple(schemas),
                options=options,
            )
            requests.append(("skill", request))
            break
    return requests


async def _send(
    adapter: ModelAdapter,
    purpose: str,
    request: GenerationRequest,
    orchestrator: LocalOrchestrator,
    *,
    kind: str = "warm_up",
    success_allowed: Callable[[], bool] = lambda: True,
    deadline: float | None = None,
) -> WarmupSample:
    """Send one request, drop the output, and time it with the monotonic clock (RM25)."""
    model = orchestrator.model
    if model is None:
        raise _NotReady("no local model is configured")
    clock = orchestrator.clock
    started = clock.monotonic()
    first_token: float | None = None
    usage: Usage | None = None
    succeeded = False
    try:
        try:
            # Only runtime execution belongs to the tolerant warm-up boundary.
            # Persistence below must escape unchanged, even for TimeoutError.
            async with asyncio.timeout_at(deadline):
                async with contextlib.aclosing(adapter.generate(request)) as stream:
                    async for event in stream:
                        if first_token is None and (
                            isinstance(event, FirstToken | ToolCall)
                            or (isinstance(event, TextDelta) and event.text)
                        ):
                            first_token = clock.monotonic()
                        if isinstance(event, Usage):
                            usage = event
                        elif isinstance(event, GenerationFailed):
                            raise _NotReady(f"the local runtime reported {event.code}")
        except TimeoutError as exc:
            raise _WarmupTimeout from exc
        except _NotReady:
            raise
        except Exception as exc:
            # Adapter exceptions are a not-ready result; cancellation is not.
            raise _NotReady(_one_line(f"{type(exc).__name__}: {exc}".rstrip(": "))) from exc
        task = asyncio.current_task()
        succeeded = success_allowed() and (task is None or not task.cancelling())
    finally:
        finished = clock.monotonic()
        if usage is not None:
            usage = replace(
                usage, cached_tokens=observed_cached_tokens(usage.input_tokens, usage.cached_tokens)
            )
        orchestrator.store.add_model_timing(
            ModelTiming(
                model=model.model_id,
                actor=model.actor,
                purpose=purpose,
                kind=kind,
                run_id=None,
                duration_ms=round((finished - started) * 1000),
                first_token_ms=None
                if first_token is None
                else round((first_token - started) * 1000),
                input_tokens=None if usage is None else usage.input_tokens,
                cached_tokens=None if usage is None else usage.cached_tokens,
                output_tokens=None if usage is None else usage.output_tokens,
                succeeded=succeeded,
                recorded_at=orchestrator.store.clock.utc_now(),
            )
        )
    return WarmupSample(
        purpose=purpose,
        duration_ms=round((finished - started) * 1000),
        first_token_ms=None if first_token is None else round((first_token - started) * 1000),
        input_tokens=None if usage is None else usage.input_tokens,
        output_tokens=None if usage is None else usage.output_tokens,
        cached_tokens=None if usage is None else usage.cached_tokens,
    )


class _NotReady(Exception):
    """The runtime answered, but with a failure."""


class _WarmupTimeout(Exception):
    """Runtime execution timed out, distinct from a timing-store failure."""


def _one_line(text: str, limit: int = 120) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 3] + "..."


async def warm_up(
    orchestrator: LocalOrchestrator, adapter: ModelAdapter, *, seconds: float
) -> WarmupResult:
    """Send the warm-up requests in order, bounded by seconds in all (section 6).

    Any failure of the adapter, including one a test double raises, is a
    result with ready False and a one-line detail: a runtime that is not
    ready is not a startup error. Cancelling the caller still propagates,
    as do timing validation and persistence errors.
    """
    samples: list[WarmupSample] = []
    deadline = asyncio.get_running_loop().time() + seconds
    try:
        for purpose, request in warmup_requests(orchestrator):
            samples.append(await _send(adapter, purpose, request, orchestrator, deadline=deadline))
    except _WarmupTimeout:
        return WarmupResult(
            tuple(samples), False, f"no answer within {seconds:g} s", timed_out=True
        )
    except _NotReady as exc:
        return WarmupResult(tuple(samples), False, str(exc))
    return WarmupResult(tuple(samples), True)


async def _health(adapter: ModelAdapter, seconds: float) -> AdapterHealth | str:
    """The runtime's health, or why there is none; an exception's text is not kept (RM12)."""
    try:
        async with asyncio.timeout(seconds):
            return await adapter.health()
    except TimeoutError:
        return "no answer"
    except Exception as exc:
        return f"health check failed: {type(exc).__name__}"


async def wait_until_available(
    adapter: ModelAdapter,
    *,
    seconds: float,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    poll_seconds: float = HEALTH_POLL_SECONDS,
) -> str | None:
    """None once the runtime answers with the model loaded, or the cause after seconds (RM26).

    The health check is repeated every poll_seconds; clock and sleep are
    injectable, so a test never waits on the wall clock.
    """
    deadline = clock() + seconds
    cause: str | None = None
    while True:
        remaining = deadline - clock()
        health = await _health(adapter, max(remaining, 0.001))
        if isinstance(health, AdapterHealth) and health.available and health.warm:
            return None
        if isinstance(health, AdapterHealth):
            cause = (health.detail or "").strip() or (
                "the model is loading" if health.available else "not available"
            )
        elif cause is None or health != "no answer":
            # A last check cut off by the deadline keeps the cause an earlier one gave.
            cause = health
        remaining = deadline - clock()
        if remaining <= 0:
            return (
                f"the runtime did not answer with the model loaded within {seconds:g} s;"
                f" last check: {cause}"
            )
        await sleep(min(poll_seconds, remaining))


def classify_warmup(
    result: WarmupResult, *, seconds: float, active_seconds: float, stages: bool = False
) -> LocalStatus:
    """The class the warm-up measured (RM26, spec 0.37 and 0.48).

    By default (spec 0.37) the routing-shaped warm-up alone must fit the
    per-stage active budget. With stages (placement.detect_stages, spec 0.48)
    a local stage needs a routing and a skill generation within that budget,
    so the routing-shaped and skill-shaped warm-ups together must fit it;
    without a skill-shaped sample the routing one alone decides. A
    warm-up cut off by warmup_seconds at or after that budget did not fit it
    either; a routing one cut off sooner, or one that failed, is unavailable. A
    skill-shaped warm-up that failed, or was cut off before the budget ran out,
    leaves a fitting routing one ready (the launcher still prints the warm-up
    line that says it did not finish).
    """
    limit = f"the local stage limit (active_seconds) is {active_seconds:g} s"
    budget_ms = active_seconds * 1000
    routing = next((s for s in result.samples if s.purpose == "routing"), None)
    skill = next((s for s in result.samples if s.purpose == "skill"), None)
    if routing is None:
        if result.timed_out and seconds >= active_seconds:
            return LocalStatus(
                LocalModelClass.UNSUITABLE,
                f"the routing warm-up did not finish within {seconds:g} s; {limit}",
            )
        return LocalStatus(
            LocalModelClass.UNAVAILABLE, f"the routing warm-up did not finish: {result.detail}"
        )
    if routing.duration_ms > budget_ms:
        return LocalStatus(
            LocalModelClass.UNSUITABLE,
            f"the routing warm-up took {routing.duration_ms} ms; {limit}",
        )
    if not stages:
        return LocalStatus.ready()
    if skill is not None:
        total = routing.duration_ms + skill.duration_ms
        if total > budget_ms:
            return LocalStatus(
                LocalModelClass.UNSUITABLE,
                f"the routing and skill warm-ups took {routing.duration_ms} +"
                f" {skill.duration_ms} = {total} ms; {limit}",
            )
    elif result.timed_out and seconds >= active_seconds:
        return LocalStatus(
            LocalModelClass.UNSUITABLE,
            f"the skill warm-up did not finish within {seconds:g} s after the routing one; {limit}",
        )
    return LocalStatus.ready()


async def detect_local_model(
    orchestrator: LocalOrchestrator,
    adapter: ModelAdapter,
    *,
    warmup: bool,
    seconds: float,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> tuple[LocalStatus, WarmupResult | None]:
    """Classify the local model at startup, and warm it up when warmup is true (RM26).

    Returns the class and the warm-up timings (None when no warm-up ran).
    The orchestrator's monotonic clock times both steps.
    """
    cause = await wait_until_available(
        adapter, seconds=seconds, clock=orchestrator.clock.monotonic, sleep=sleep
    )
    if cause is not None:
        return LocalStatus(LocalModelClass.UNAVAILABLE, cause), None
    if not warmup:
        return LocalStatus.ready(), None
    result = await warm_up(orchestrator, adapter, seconds=seconds)
    status = classify_warmup(
        result,
        seconds=seconds,
        active_seconds=orchestrator.budgets.active_seconds,
        stages=orchestrator.detect_stages,
    )
    return status, result


__all__ = [
    "HEALTH_POLL_SECONDS",
    "SKILL_WARMUP_MAX_TOKENS",
    "WARMUP_TEXT",
    "WarmupResult",
    "WarmupSample",
    "classify_warmup",
    "detect_local_model",
    "wait_until_available",
    "warm_up",
    "warmup_requests",
]


_LOGGER = logging.getLogger(__name__)

# While this owner's probe runs, cancellation requests are checked this often.
_ACTIVE_POLL_SECONDS = 0.05
# An idle scheduler sleeps until a probe can start, within these bounds: a run
# or another probe has no known end, and settings or clocks may change.
_IDLE_MIN_SECONDS = 1.0
_IDLE_MAX_SECONDS = 60.0
# A failed check is retried after a growing delay, never more than this.
_ERROR_BACKOFF_MAX_SECONDS = 30.0


class IdleProbe:
    """One bounded idle routing generation under the SQLite execution slot (AC21).

    Cancellation marks the attempt unsuccessful before cancelling its task. Only
    that task's final cleanup releases the reservation; a bounded stop never does.
    """

    def __init__(self, orchestrator: LocalOrchestrator, *, cancel_wait_seconds: float = 5) -> None:
        self.o = orchestrator
        self.cancel_wait_seconds = cancel_wait_seconds
        self.owner = uuid.uuid4().hex
        self._task: asyncio.Task[None] | None = None
        self._scheduler: asyncio.Task[None] | None = None
        self._closing = False
        self._cancelled = False
        self._token: str | None = None
        # The first scheduler check that failed; logged then, raised by stop().
        self._error: Exception | None = None
        self._started = 0.0
        self._wall_started = 0.0

    @property
    def enabled(self) -> bool:
        return (
            self.o.model is not None
            and self.o.external is not None
            and self.o.placement.settings.policy != "local_first"
        )

    def start(self) -> None:
        if self.enabled and self._scheduler is None:
            self._scheduler = asyncio.create_task(self._schedule())

    async def _schedule(self) -> None:
        failures = 0
        while not self._closing:
            try:
                await self.tick()
                delay = self._next_delay()
                failures = 0
            except Exception as exc:
                # One failed check must not end maintenance: a running probe still
                # needs its cancellation checks. The error is logged and kept for stop().
                failures += 1
                if self._error is None:
                    self._error = exc
                _LOGGER.exception("idle probe check failed; retrying")
                delay = min(_ERROR_BACKOFF_MAX_SECONDS, _ACTIVE_POLL_SECONDS * 2**failures)
            await asyncio.sleep(delay)

    def _next_delay(self) -> float:
        if self._task is not None:
            # Running, or finished and not yet reported by tick().
            return _ACTIVE_POLL_SECONDS
        assert self.o.model is not None
        wait = self.o.store.probe_wait_seconds(
            model=self.o.model.model_id,
            after_seconds=self.o.placement.settings.probe_after_seconds,
        )
        return min(max(wait, _IDLE_MIN_SECONDS), _IDLE_MAX_SECONDS)

    async def tick(self) -> None:
        if self._closing or not self.enabled:
            return
        settings = self.o.placement.settings
        if self._task is not None and self._task.done():
            task, self._task = self._task, None
            if not task.cancelled():
                # Report a task whose timing persistence failed, once, before replacing it.
                task.result()
        if self._task is not None:
            reservation = self.o.store.probe_reservation()
            limit = min(settings.warmup_seconds, self.o.budgets.active_seconds)
            elapsed = max(
                self.o.clock.monotonic() - self._started,
                asyncio.get_running_loop().time() - self._wall_started,
            )
            if (reservation and reservation["cancel_requested"]) or elapsed >= limit:
                self._cancel()
            return
        assert self.o.model is not None
        token = "probe_" + uuid.uuid4().hex
        if not self.o.store.try_reserve_probe(
            token,
            self.owner,
            model=self.o.model.model_id,
            after_seconds=settings.probe_after_seconds,
        ):
            return
        self._token = token
        self._cancelled = False
        self._started = self.o.clock.monotonic()
        self._wall_started = asyncio.get_running_loop().time()
        self._task = asyncio.create_task(self._execute(token))
        # Enter the task before allowing stop/cancellation; even a first-instruction
        # failure then passes through its finally and releases its own reservation.
        await asyncio.sleep(0)

    def _cancel(self) -> None:
        if self._cancelled or self._task is None or self._task.done():
            return
        self._cancelled = True
        assert self.o.model is not None and self._token is not None
        self.o.ask_provider_to_cancel(self.o.model.adapter, self._token)
        self._task.cancel()

    def _success_allowed(self) -> bool:
        limit = min(self.o.placement.settings.warmup_seconds, self.o.budgets.active_seconds)
        reservation = self.o.store.probe_reservation()
        return (
            not self._cancelled
            and not self._closing
            and reservation is not None
            and not reservation["cancel_requested"]
            and self.o.clock.monotonic() - self._started < limit
            and asyncio.get_running_loop().time() - self._wall_started < limit
        )

    async def _execute(self, token: str) -> None:
        assert self.o.model is not None
        limit = min(self.o.placement.settings.warmup_seconds, self.o.budgets.active_seconds)
        timer = asyncio.get_running_loop().call_later(limit, self._cancel)
        try:
            # Build only routing; no skill request or user run is manufactured.
            request = self.o.router.build_request(
                [Message(role=Role.USER, content=WARMUP_TEXT)], token
            )
            await _send(
                self.o.model.adapter,
                "routing",
                request,
                self.o,
                kind="probe",
                success_allowed=self._success_allowed,
            )
            if self._success_allowed() and self.o.local_status.status == LocalModelClass.UNSUITABLE:
                latest = self.o.store.latest_model_timing(
                    model=self.o.model.model_id, purpose="routing", actor="local"
                )
                if latest is None:
                    return
                prediction = self.o.placement.predict(
                    self.o.model.model_id,
                    "routing",
                    latest.input_tokens,
                    output_cap=self.o.budgets.routing_output_tokens,
                    input_source="routing_sample",
                )
                if (
                    prediction.predicted_ms is not None
                    and prediction.predicted_ms <= self.o.budgets.active_seconds * 1000
                ):
                    self.o.set_local_status(LocalStatus.ready())
        except (_NotReady, _WarmupTimeout, asyncio.CancelledError):
            # Runtime failures are recorded attempts. Evidence/storage failures
            # must remain visible, just as they do for startup warm-up.
            pass
        finally:
            timer.cancel()
            self.o.store.finish_probe(token, self.owner)

    async def stop(self) -> None:
        self._closing = True
        if self._scheduler is not None:
            self._scheduler.cancel()
            await asyncio.gather(self._scheduler, return_exceptions=True)
        self._cancel()
        if self._task is not None and not self._task.done():
            await asyncio.wait((self._task,), timeout=self.cancel_wait_seconds)
        if self._scheduler is not None and not self._scheduler.cancelled():
            self._scheduler.result()
        if self._error is not None:
            raise self._error
        if self._task is not None and self._task.done() and not self._task.cancelled():
            self._task.result()
