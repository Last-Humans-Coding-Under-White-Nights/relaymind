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
unsuitable. That warm-up is the first request after the model loaded, so
it is measured cold. A warm-up that fails, or that is cut off by
warmup_seconds before the stage budget ran out, says the model could not
generate, so it is unavailable.
"""

import asyncio
import contextlib
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
    adapter: ModelAdapter, purpose: str, request: GenerationRequest, orchestrator: LocalOrchestrator
) -> WarmupSample:
    """Send one request, drop the output, and time it with the monotonic clock (RM25)."""
    clock = orchestrator.clock
    started = clock.monotonic()
    first_token: float | None = None
    usage: Usage | None = None
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
    finished = clock.monotonic()
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


def _one_line(text: str, limit: int = 120) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 3] + "..."


async def warm_up(
    orchestrator: LocalOrchestrator, adapter: ModelAdapter, *, seconds: float
) -> WarmupResult:
    """Send the warm-up requests in order, bounded by seconds in all (section 6).

    Any failure of the adapter, including one a test double raises, is a
    result with ready False and a one-line detail: a runtime that is not
    ready is not a startup error. Cancelling the caller still propagates.
    """
    samples: list[WarmupSample] = []
    try:
        async with asyncio.timeout(seconds):
            for purpose, request in warmup_requests(orchestrator):
                samples.append(await _send(adapter, purpose, request, orchestrator))
    except TimeoutError:
        return WarmupResult(
            tuple(samples), False, f"no answer within {seconds:g} s", timed_out=True
        )
    except _NotReady as exc:
        return WarmupResult(tuple(samples), False, str(exc))
    except Exception as exc:
        # Any adapter failure, a double's included, is the one printed line.
        return WarmupResult(
            tuple(samples), False, _one_line(f"{type(exc).__name__}: {exc}".rstrip(": "))
        )
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


def classify_warmup(result: WarmupResult, *, seconds: float, active_seconds: float) -> LocalStatus:
    """The class the routing-shaped warm-up measured (RM26, spec 0.37).

    Over the per-stage active budget is unsuitable. A warm-up cut off by
    warmup_seconds at or after that budget did not fit it either; one cut
    off sooner, or one that failed, is unavailable. Only the routing sample
    decides the class: a skill-shaped warm-up that fails or is cut off after
    a routing one that fits leaves the model ready (the launcher still
    prints the warm-up line that says it did not finish).
    """
    limit = f"the local stage limit (active_seconds) is {active_seconds:g} s"
    routing = next((s for s in result.samples if s.purpose == "routing"), None)
    if routing is not None:
        if routing.duration_ms > active_seconds * 1000:
            return LocalStatus(
                LocalModelClass.UNSUITABLE,
                f"the routing warm-up took {routing.duration_ms} ms; {limit}",
            )
        return LocalStatus.ready()
    if result.timed_out and seconds >= active_seconds:
        return LocalStatus(
            LocalModelClass.UNSUITABLE,
            f"the routing warm-up did not finish within {seconds:g} s; {limit}",
        )
    return LocalStatus(
        LocalModelClass.UNAVAILABLE, f"the routing warm-up did not finish: {result.detail}"
    )


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
        result, seconds=seconds, active_seconds=orchestrator.budgets.active_seconds
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
