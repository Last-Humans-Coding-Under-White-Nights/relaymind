"""Measured per-generation placement (RM27; specification 0.39 section 14.2)."""

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from statistics import median
from types import MappingProxyType
from typing import Any

from relaymind.store import RunStore


@dataclass(frozen=True)
class PlacementSettings:
    policy: str = "local_first"
    margin: float = 2
    probe_after_seconds: float = 600
    warmup_seconds: float = 30

    def __post_init__(self) -> None:
        if self.policy not in ("local_first", "device_aware", "expected_wait"):
            raise ValueError("placement policy must be local_first, device_aware or expected_wait")
        for name in ("margin", "probe_after_seconds", "warmup_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"placement {name} must be a positive finite number")


class PlacementDeclined(Exception):
    """Only the gateway callback may turn this pre-dispatch stop into not_placed."""

    def __init__(self, placement: "Prediction") -> None:
        super().__init__("local generation declined by placement")
        self.placement = placement


@dataclass(frozen=True)
class Prediction:
    predicted_ms: int | None
    inputs: Mapping[str, Any]
    role: str = "local"
    reason: str = "prediction_unavailable"
    deadline_ms: int | None = None
    # Unrounded limit used for comparisons; never serialized as a measurement.
    deadline_seconds: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))

    def to_json_object(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "reason": self.reason,
            "predicted_ms": self.predicted_ms,
            "deadline_ms": self.deadline_ms,
            "inputs": dict(self.inputs),
        }


def marker(external: bool = False) -> Prediction:
    return Prediction(
        None,
        {
            "model": None,
            "purpose": None,
            "input_tokens": None,
            "input_source": "none",
            "expected_output_tokens": None,
            "output_source": "unavailable",
            "cache_class": "unknown",
            "sample_count": 0,
            "sample_recorded_at": None,
            "prefill_tokens_per_ms": None,
            "decode_tokens_per_ms": None,
            "local_success_rate": None,
            "expected_external_ms": None,
        },
        role="external" if external else "none",
        reason="external_path" if external else "no_generation",
    )


class Placement:
    def __init__(self, store: RunStore, settings: PlacementSettings | None = None) -> None:
        self.store = store
        self.settings = settings or PlacementSettings()

    def predict(
        self,
        model: str,
        purpose: str,
        input_tokens: int | None,
        *,
        output_cap: int | None = None,
        input_source: str = "tokenizer",
    ) -> Prediction:
        for name, value in (("input_tokens", input_tokens), ("output_cap", output_cap)):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer or None")
        if not model.strip() or not purpose.strip():
            raise ValueError("model and purpose must be nonblank")
        if input_source not in ("tokenizer", "routing_sample", "none"):
            raise ValueError("invalid input source")
        estimate = self.store.timing_estimate(model, purpose)
        outputs = [
            s.output_tokens
            for s in self.store.model_timings(model=model, purpose=purpose, actor="local")
            if s.succeeded and s.kind == "run" and s.output_tokens is not None
        ]
        output = median(outputs) if outputs else output_cap
        if output is not None and output_cap is not None:
            output = min(output, output_cap)
        rates = getattr(estimate, estimate.latest_cache_class, None)
        prefill = rates.prefill_tokens_per_ms if rates else None
        decode = rates.decode_tokens_per_ms if rates else None
        predicted = None
        if input_tokens is not None and output is not None and prefill and decode:
            try:
                duration = input_tokens / prefill + output / decode
            except OverflowError:
                duration = math.inf
            if math.isfinite(duration):
                predicted = max(1, math.ceil(duration))
        return Prediction(
            predicted,
            {
                "model": model,
                "purpose": purpose,
                "input_tokens": input_tokens,
                "input_source": input_source if input_tokens is not None else "none",
                "expected_output_tokens": output,
                "output_source": "run_samples"
                if outputs
                else ("purpose_cap" if output_cap is not None else "unavailable"),
                "cache_class": estimate.latest_cache_class,
                "sample_count": rates.sample_count if rates else 0,
                "sample_recorded_at": estimate.recorded_at.isoformat().replace("+00:00", "Z")
                if estimate.recorded_at
                else None,
                "prefill_tokens_per_ms": prefill,
                "decode_tokens_per_ms": decode,
                "local_success_rate": None,
                "expected_external_ms": None,
            },
        )

    def admit(
        self,
        prediction: Prediction,
        *,
        mode: str,
        external_model: str | None,
        stage_seconds: float,
        run_seconds: float,
        skill_id: str | None = None,
    ) -> Prediction:
        p = prediction
        reason = (
            "local_only"
            if mode == "local_only"
            else "no_external_path"
            if external_model is None
            else "local_first"
            if self.settings.policy == "local_first"
            else None
        )
        if reason:
            return replace(p, reason=reason)
        if p.predicted_ms is None:
            return p
        if p.predicted_ms > stage_seconds * 1000:
            return replace(p, role="none", reason="local_slow")
        if self.settings.policy == "expected_wait" and skill_id is not None:
            outcome = self.store.outcome_estimate(
                p.inputs["model"], skill_id, external_model=external_model
            )
            costs = [
                s.duration_ms
                for s in self.store.model_timings(model=external_model, actor="external")
                if s.succeeded and s.kind == "run"
            ]
            if (
                costs
                and outcome.local_success_rate is not None
                and outcome.median_external_generations is not None
            ):
                expected = median(costs) * max(1, outcome.median_external_generations)
                p = replace(
                    p,
                    inputs={
                        **p.inputs,
                        "local_success_rate": outcome.local_success_rate,
                        "expected_external_ms": expected,
                    },
                )
                if not p.predicted_ms < outcome.local_success_rate * expected:
                    return replace(p, role="none", reason="expected_wait")
        deadline = min(p.predicted_ms * self.settings.margin / 1000, stage_seconds, run_seconds)
        return replace(
            p,
            reason="within_budget",
            deadline_ms=max(1, math.ceil(deadline * 1000)),
            deadline_seconds=deadline,
        )

    def readiness(
        self,
        *,
        local_model: str | None,
        external_model: str | None,
        usable: bool,
        stage_seconds: float,
        output_cap: int,
    ) -> dict[str, Any]:
        p = marker()
        fits = None
        chosen = external_model
        # An absent, unavailable or unsuitable class keeps the RM26 external choice:
        # no local routing is predicted, so neither a placement nor a fit is reported.
        if local_model is not None and usable:
            latest = self.store.latest_model_timing(
                model=local_model, purpose="routing", actor="local"
            )
            tokens = None if latest is None else latest.input_tokens
            p = self.predict(
                local_model, "routing", tokens, output_cap=output_cap, input_source="routing_sample"
            )
            fits = None if p.predicted_ms is None else p.predicted_ms <= stage_seconds * 1000
            p = self.admit(
                p,
                mode="auto",
                external_model=external_model,
                stage_seconds=stage_seconds,
                run_seconds=stage_seconds,
            )
            chosen = local_model if p.role == "local" else external_model
        return {
            "policy": self.settings.policy,
            "representative": True,
            "routing": p.to_json_object(),
            "routing_fits": fits,
            "auto_model": chosen,
        }
