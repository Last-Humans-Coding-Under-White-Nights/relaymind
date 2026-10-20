"""Per-generation timing and statistics (RM25, spec section 12.1).

Engineering instrumentation for comparing adapters on the same prompts
(issue #37). Durations come from a monotonic clock and timestamps from
UTC. This is working data for development, not the formal M6 benchmark:
numbers from here are never published as measured results (spec
section 12.3).
"""

import json
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from relaymind.adapters.base import (
    AdapterCapabilities,
    AdapterEvent,
    AdapterHealth,
    Completed,
    GenerationFailed,
    GenerationRequest,
    Message,
    ModelAdapter,
    Role,
    TextDelta,
    ToolCall,
    Usage,
)


@dataclass(frozen=True)
class AdapterIdentity:
    """What was measured: the artifact manifest fields that name it.

    For a local artifact these come from the model manifest (model id,
    quantization, runtime version); an external endpoint carries its
    configured model identifier.
    """

    adapter: str
    model_id: str
    quantization: str | None = None
    runtime_version: str | None = None


@dataclass
class GenerationStats:
    """Spec 12.1 metrics for one generation.

    Durations are seconds from generation start on a monotonic clock;
    the start itself is stamped in UTC (RM25). A metric the generation
    never reached stays None; completion is recorded only for a
    successful final answer, never for a tool proposal, a truncation,
    or a failure.
    """

    generation_id: str
    # The current turn's user message, so exports from different
    # adapter configs align row by row.
    prompt: str | None = None
    started_at_utc: str | None = None
    first_transport_event_seconds: float | None = None
    first_model_action_seconds: float | None = None
    first_answer_token_seconds: float | None = None
    completion_seconds: float | None = None
    finish_reason: str | None = None
    failure_code: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None


# The only finish that is a successful final answer (spec 12.1).
_ANSWER_FINISH_REASON = "stop"


class TimingCollector(ModelAdapter):
    """Wrap any ModelAdapter and record spec 12.1 metrics per generation.

    Events pass through unchanged (RM18: the rest of the gateway sees
    the same normalized stream); one GenerationStats record is appended
    per generate() call. A transport event is anything the provider
    sent; a GenerationFailed the adapter synthesized (timeout,
    unreachable, missing credentials) is not one.
    """

    def __init__(
        self,
        adapter: ModelAdapter,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._adapter = adapter
        self._clock = clock
        self.records: list[GenerationStats] = []

    async def capabilities(self) -> AdapterCapabilities:
        return await self._adapter.capabilities()

    async def generate(self, request: GenerationRequest) -> AsyncIterator[AdapterEvent]:
        record = GenerationStats(
            generation_id=request.generation_id,
            prompt=next(
                (
                    m.content
                    for m in reversed(request.messages)
                    if m.role is Role.USER and m.content
                ),
                None,
            ),
            started_at_utc=datetime.now(UTC).isoformat(),
        )
        self.records.append(record)
        started = self._clock()
        try:
            async for event in self._adapter.generate(request):
                elapsed = self._clock() - started
                if (
                    not isinstance(event, GenerationFailed)
                    and record.first_transport_event_seconds is None
                ):
                    record.first_transport_event_seconds = elapsed
                # Event types are mutually exclusive; at most one branch runs.
                if isinstance(event, TextDelta):
                    if event.text and record.first_answer_token_seconds is None:
                        record.first_answer_token_seconds = elapsed
                elif isinstance(event, ToolCall):
                    if record.first_model_action_seconds is None:
                        record.first_model_action_seconds = elapsed
                elif isinstance(event, Usage):
                    record.input_tokens = event.input_tokens
                    record.output_tokens = event.output_tokens
                    record.cached_tokens = event.cached_tokens
                elif isinstance(event, Completed):
                    record.finish_reason = event.finish_reason
                    if event.finish_reason == _ANSWER_FINISH_REASON:
                        record.completion_seconds = elapsed
                elif isinstance(event, GenerationFailed):
                    record.failure_code = event.code
                yield event
        except GeneratorExit:
            # The consumer walked away before a terminal event.
            if record.finish_reason is None and record.failure_code is None:
                record.failure_code = "abandoned"
            raise
        except Exception:
            record.failure_code = "adapter_error"
            raise

    async def cancel(self, generation_id: str) -> None:
        await self._adapter.cancel(generation_id)

    async def health(self) -> AdapterHealth:
        return await self._adapter.health()

    async def aclose(self) -> None:
        aclose = getattr(self._adapter, "aclose", None)
        if aclose is not None:
            await aclose()


# Config names become export filenames: one plain path segment.
_EXPORT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def check_export_names(names: Sequence[str]) -> None:
    """Reject config names that are not a safe, distinct filename.

    Names are compared case-insensitively so two exports cannot
    overwrite each other on a case-insensitive filesystem.
    """
    seen: dict[str, str] = {}
    for name in names:
        if not isinstance(name, str) or not _EXPORT_NAME.fullmatch(name):
            raise ValueError(
                f"adapter config name {name!r} must be letters, digits, '.', '_' or '-'"
            )
        folded = name.casefold()
        if folded in seen:
            raise ValueError(
                f"adapter config names {seen[folded]!r} and {name!r} collide as filenames"
            )
        seen[folded] = name


def export_stats(
    path: Path | str,
    *,
    identity: AdapterIdentity,
    records: Sequence[GenerationStats],
) -> None:
    """Write one machine-readable JSON export for one adapter config.

    Token counts stay per call and are never summed across different
    tokenizers (spec 12.1). The status field marks the numbers as
    working data, never publishable results (spec 12.3).
    """
    document = {
        "schema_version": 1,
        "status": "engineering_instrumentation",
        "exported_at_utc": datetime.now(UTC).isoformat(),
        "adapter": asdict(identity),
        "generations": [asdict(record) for record in records],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


async def run_comparison(
    adapters: Mapping[str, tuple[ModelAdapter, AdapterIdentity]],
    prompts: Sequence[str],
    out_dir: Path,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Path]:
    """Run the same prompts against every adapter config; export each.

    Sequential and unwarmed by design: this compares adapters during
    development (issue #37), it is not the formal M6 experiment.
    Generation ids are shared across configs (gen_000, gen_001, ...)
    so exports join row by row. An adapter that raises is recorded as
    a failed generation and the run continues, so one broken config
    never costs the others their exports. Returns the export path per
    config name.
    """
    check_export_names(list(adapters))
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, (adapter, identity) in adapters.items():
        collector = TimingCollector(adapter, clock=clock)
        for index, prompt in enumerate(prompts):
            request = GenerationRequest(
                generation_id=f"gen_{index:03d}",
                messages=(Message(role=Role.USER, content=prompt),),
            )
            try:
                async for _event in collector.generate(request):
                    pass
            except Exception:  # noqa: BLE001 - recorded as adapter_error by the collector
                continue
        path = out_dir / f"{name}.json"
        export_stats(path, identity=identity, records=collector.records)
        paths[name] = path
    return paths
