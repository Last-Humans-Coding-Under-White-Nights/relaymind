"""Measured model calls and placement statistics (RM25, RM27, spec 0.38.1 section 8)."""

import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Any, Literal, Self

import jsonschema

from relaymind.schema_util import check_schema_version, first_schema_error, load_packaged_schema

MODEL_TIMING_SCHEMA = load_packaged_schema("relaymind.store", "model_timing.v1.json")
_VALIDATOR = jsonschema.Draft202012Validator(MODEL_TIMING_SCHEMA)
CacheClass = Literal["warm", "cold", "unknown"]
_LOGGER = logging.getLogger(__name__)


def observed_cached_tokens(input_tokens: int | None, cached_tokens: int | None) -> int | None:
    """Reject an impossible optional observation, never repair imported samples (RM27)."""
    if input_tokens is not None and cached_tokens is not None and cached_tokens > input_tokens:
        _LOGGER.warning(
            "invalid_cached_tokens: cached_tokens exceeds input_tokens; recorded as unknown"
        )
        return None
    return cached_tokens


@dataclass(frozen=True)
class ModelTiming:
    """One observed generation; unknown provider measurements stay None."""

    model: str
    actor: Literal["local", "external"]
    purpose: str
    kind: Literal["run", "warm_up", "probe"]
    input_tokens: int | None
    cached_tokens: int | None
    first_token_ms: int | None
    duration_ms: int
    output_tokens: int | None
    succeeded: bool
    run_id: str | None
    recorded_at: datetime

    def to_json_object(self) -> dict[str, Any]:
        """Return the published versioned sample shape, validated for seeding."""
        if not isinstance(self.recorded_at, datetime) or self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be a timezone-aware datetime")
        data = asdict(self)
        data["schema_version"] = 1
        data["recorded_at"] = (
            self.recorded_at.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        _validate(data)
        return data

    @classmethod
    def from_json_object(cls, data: object) -> Self:
        """Validate an imported sample, including its exact schema version."""
        _validate(data)
        fields = dict(data)
        fields.pop("schema_version")
        fields["recorded_at"] = datetime.fromisoformat(fields["recorded_at"])
        return cls(**fields)


def _validate(data: object) -> None:
    if not isinstance(data, dict):
        raise ValueError("a model timing must be a JSON object")
    check_schema_version(data.get("schema_version"), 1, ValueError)
    error = first_schema_error(_VALIDATOR, data)
    if error is not None:
        raise ValueError(f"model timing: {error.message}")
    # JSON Schema considers 1.0 an integer; the store's numeric boundary is exact.
    for name in ("input_tokens", "cached_tokens", "first_token_ms", "duration_ms", "output_tokens"):
        value = data[name]
        if value is not None and type(value) is not int:
            raise ValueError(f"{name} must be an integer or null")
    try:
        datetime.fromisoformat(data["recorded_at"])
    except ValueError as exc:
        raise ValueError("recorded_at must be a UTC timestamp") from exc
    if data["first_token_ms"] is not None and data["first_token_ms"] > data["duration_ms"]:
        raise ValueError("first_token_ms cannot exceed duration_ms")
    if data["cached_tokens"] is not None and data["input_tokens"] is not None:
        if data["cached_tokens"] > data["input_tokens"]:
            raise ValueError("cached_tokens cannot exceed input_tokens")
    if data["kind"] != "run" and data["run_id"] is not None:
        raise ValueError("warm-up and probe samples cannot belong to a run")


@dataclass(frozen=True)
class ThroughputEstimate:
    """Medians of effective rates, in tokens/ms; missing intervals give no rate."""

    sample_count: int
    recorded_at: datetime | None
    prefill_tokens_per_ms: float | None
    decode_tokens_per_ms: float | None
    prefill_sample_count: int
    decode_sample_count: int


@dataclass(frozen=True)
class TimingEstimate:
    """A deterministic snapshot of successful observations for one actor/model/purpose."""

    model: str
    purpose: str
    actor: str
    sample_count: int
    recorded_at: datetime | None
    latest_cache_class: CacheClass
    median_duration_ms: float | None
    median_output_tokens: float | None
    median_first_token_ms: float | None
    warm: ThroughputEstimate
    cold: ThroughputEstimate


@dataclass(frozen=True)
class SkillOutcomeEstimate:
    """Terminal local attempts and completed external runs of a selected skill."""

    local_completed: int
    local_escalated: int
    local_success_rate: float | None
    external_completed: int
    median_external_generations: float | None


def _median(values: Sequence[int | float]) -> float | None:
    return float(median(values)) if values else None


def cache_class(sample: ModelTiming) -> CacheClass:
    if sample.cached_tokens is None:
        return "unknown"
    return "warm" if sample.cached_tokens > 0 else "cold"


def _throughput(samples: Sequence[ModelTiming]) -> ThroughputEstimate:
    prefill = [
        s.input_tokens / s.first_token_ms
        for s in samples
        if s.input_tokens is not None
        and s.input_tokens > 0
        and s.first_token_ms is not None
        and s.first_token_ms > 0
    ]
    decode = [
        s.output_tokens / (s.duration_ms - s.first_token_ms)
        for s in samples
        if s.output_tokens is not None
        and s.output_tokens > 0
        and s.first_token_ms is not None
        and s.duration_ms > s.first_token_ms
    ]
    return ThroughputEstimate(
        len(samples),
        samples[-1].recorded_at if samples else None,
        _median(prefill),
        _median(decode),
        len(prefill),
        len(decode),
    )


def timing_estimate(
    model: str, purpose: str, actor: str, samples: Sequence[ModelTiming]
) -> TimingEstimate:
    """Summarize filtered samples in ascending timestamp, then insertion order.

    The store supplies that order, including ties; perform no inference.
    """
    successful = [s for s in samples if s.succeeded]
    return TimingEstimate(
        model=model,
        purpose=purpose,
        actor=actor,
        sample_count=len(successful),
        recorded_at=successful[-1].recorded_at if successful else None,
        latest_cache_class=cache_class(successful[-1]) if successful else "unknown",
        median_duration_ms=_median([s.duration_ms for s in successful]),
        median_output_tokens=_median(
            [s.output_tokens for s in successful if s.output_tokens is not None]
        ),
        median_first_token_ms=_median(
            [s.first_token_ms for s in successful if s.first_token_ms is not None]
        ),
        warm=_throughput([s for s in successful if cache_class(s) == "warm"]),
        cold=_throughput([s for s in successful if cache_class(s) == "cold"]),
    )
