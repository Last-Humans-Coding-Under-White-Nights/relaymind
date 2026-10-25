"""Validated immutable placement priors from measured terminal exports (RM17/RM27)."""

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema

from relaymind.adapters import GenerationOptions
from relaymind.canonical import canonical_digest
from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.store import ModelTiming, RunStore
from relaymind.store.outcomes import project_outcome
from relaymind.store.trace_event import TRACE_EVENT_SCHEMA, validate_trace_event

TERMINAL = {"run.completed": "completed", "run.failed": "failed", "run.cancelled": "cancelled"}


def byte_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _closed(value: object, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label}: expected exactly {sorted(fields)}")
    return value


def _generation_options(value: object) -> dict:
    options = _closed(value, {"max_output_tokens", "temperature", "top_p"}, "generation options")
    for name, number in options.items():
        if number is not None and (
            isinstance(number, bool)
            or not isinstance(number, int | float)
            or not math.isfinite(number)
            or (name == "max_output_tokens" and type(number) is not int)
        ):
            raise ValueError("invalid generation option")
    GenerationOptions(**options)
    return options


def validate_configuration(value: object) -> dict:
    """Validate the closed runner partition, including unselected partitions."""
    config = _closed(
        value, {"local", "external", "local_generation_options", "budgets"}, "configuration"
    )
    local = config["local"]
    if local is not None:
        _closed(
            local,
            {"model_id", "artifact_sha256", "manifest_sha256", "runtime_banner", "runtime_argv"},
            "local configuration",
        )
        if (
            not all(
                isinstance(local[k], str) and local[k].strip()
                for k in ("model_id", "runtime_banner")
            )
            or not all(
                isinstance(local[k], str) and re.fullmatch(r"[0-9a-f]{64}", local[k])
                for k in ("artifact_sha256", "manifest_sha256")
            )
            or not isinstance(local["runtime_argv"], list)
            or not local["runtime_argv"]
            or not all(isinstance(x, str) for x in local["runtime_argv"])
        ):
            raise ValueError("invalid local configuration")
    external = config["external"]
    if external is not None:
        _closed(
            external,
            {"base_url", "model", "context_limit", "request_options", "generation_options"},
            "external configuration",
        )
        if (
            not all(
                isinstance(external[k], str) and external[k].strip() for k in ("base_url", "model")
            )
            or type(external["context_limit"]) is not int
            or external["context_limit"] <= 0
            or not isinstance(external["request_options"], dict)
        ):
            raise ValueError("invalid external configuration")
        # Reject non-JSON and nonfinite provider option values before digesting them.
        json.dumps(external["request_options"], allow_nan=False)
        _generation_options(external["generation_options"])
    _generation_options(config["local_generation_options"])
    budgets = _closed(config["budgets"], set(LocalBudgets().to_json_object()), "budgets")
    LocalBudgets(**budgets)
    return config


def bind_trace_configuration(events: list[dict], config: dict) -> None:
    """Bind observable models and options; an absent legacy model uses actual local calls."""
    accepted = events[0]["payload"]
    local_model = (config["local"] or {}).get("model_id")
    if "local_model" in accepted and accepted["local_model"] != local_model:
        raise ValueError("source accepted local model differs from configuration")
    requested = accepted.get("answer_options", {})
    option_names = {
        "max_completion_tokens": "max_output_tokens",
        "temperature": "temperature",
        "top_p": "top_p",
    }
    for event in events:
        if event["event_type"] not in ("model.started", "model.finished"):
            continue
        payload, actor = event["payload"], event["actor"]
        if actor == "local":
            model = local_model
            options = config["local_generation_options"]
            request_options = {}
        elif actor == "external":
            external = config["external"] or {}
            model = external.get("model")
            options = external.get("generation_options", {})
            request_options = external.get("request_options", {})
        else:
            raise ValueError("source model actor is not local or external")
        if model is None or payload["model"] != model:
            raise ValueError("source call model differs from configuration")
        if event["event_type"] != "model.started":
            continue
        # Since 0.33 omitted request_options means no declared provider options.
        if payload.get("request_options", {}) != request_options:
            raise ValueError("source provider request options differ from configuration")
        if "generation_options" in payload and payload["generation_options"] != options:
            raise ValueError("source generation options differ from configuration")
        if any(
            requested[key] != options[name]
            for key, name in option_names.items()
            if key in requested
        ):
            raise ValueError("source answer generation options differ from configuration")


def validated_trace(events: object, run_id: str, record: dict) -> list[dict]:
    """Require a complete consistent terminal trace, not merely valid individual events."""
    if not isinstance(events, list) or not events:
        raise ValueError("missing source trace")
    starts, finishes, elapsed = {}, set(), -1
    for seq, raw in enumerate(events, 1):
        event = validate_trace_event(raw)
        try:
            jsonschema.Draft202012Validator(TRACE_EVENT_SCHEMA).validate(raw)
        except jsonschema.ValidationError as exc:
            raise ValueError("source event violates published TraceEvent schema") from exc
        if event.seq != seq or event.run_id != run_id or event.elapsed_ms < elapsed:
            raise ValueError("inconsistent source trace sequence/identity/time")
        elapsed = event.elapsed_ms
        kind, payload = raw["event_type"], raw["payload"]
        if kind in TERMINAL and seq != len(events):
            raise ValueError("source trace has a nonfinal terminal event")
        if kind == "model.started":
            call_id = payload["call_id"]
            if call_id in starts:
                raise ValueError("duplicate source model call")
            starts[call_id] = {k: payload.get(k) for k in ("call_id", "model", "purpose")}
            starts[call_id]["actor"] = raw["actor"]
        if kind == "model.finished":
            call_id = payload["call_id"]
            if call_id not in starts or call_id in finishes:
                raise ValueError("unmatched source model completion")
            if (
                any(starts[call_id][k] != payload.get(k) for k in ("model", "purpose"))
                or starts[call_id]["actor"] != raw["actor"]
            ):
                raise ValueError("inconsistent source model completion")
            finishes.add(call_id)
    terminal = events[-1]
    if (
        terminal["event_type"] not in TERMINAL
        or record.get("state") != TERMINAL[terminal["event_type"]]
        or record.get("run_id") != run_id
    ):
        raise ValueError("source record and terminal state disagree")
    payload = terminal["payload"]
    if Counter(tuple(sorted(c.items())) for c in payload.get("model_calls", [])) != Counter(
        tuple(sorted(c.items())) for c in starts.values()
    ):
        raise ValueError("source terminal model calls disagree")
    if payload.get("external_calls", 0) != sum(c["actor"] == "external" for c in starts.values()):
        raise ValueError("source terminal external call count disagrees")
    if set(starts) != finishes:
        raise ValueError("source trace has unfinished model calls")
    for name in ("error_code", "reason_code"):
        if name in record and record[name] != payload.get(name):
            raise ValueError("source terminal metadata disagrees")
    return events


def _observations(events: list[dict]) -> tuple[list[ModelTiming], dict | None]:
    progress = dict(
        local_model=None,
        skill_id=None,
        execution_mode=None,
        local_attempt=0,
        handoff=0,
        external_model=None,
        external_generations=0,
    )
    timings = []
    for event in events:
        kind, p, actor = event["event_type"], event["payload"], event["actor"]
        if kind == "run.accepted":
            progress.update(
                local_model=p.get("local_model"), execution_mode=p["effective_execution_mode"]
            )
        elif kind == "skill.loaded":
            progress["skill_id"] = p["name"]
        elif kind == "model.started":
            if actor == "local":
                progress["local_model"] = progress["local_model"] or p["model"]
                progress["local_attempt"] |= p.get("purpose") not in (None, "routing")
            elif actor == "external":
                if progress["external_model"] not in (None, p["model"]):
                    raise ValueError("mixed external models within one source run are unsupported")
                progress["external_model"] = p["model"]
                progress["external_generations"] += 1
        elif kind == "handoff.started":
            progress["handoff"] = 1
        elif kind == "model.finished":
            usage = p.get("usage") or {}
            timings.append(
                ModelTiming.from_json_object(
                    dict(
                        schema_version=1,
                        model=p["model"],
                        actor=actor,
                        purpose=p["purpose"],
                        kind="run",
                        input_tokens=usage.get("input_tokens"),
                        cached_tokens=usage.get("cached_tokens"),
                        first_token_ms=p.get("first_token_ms"),
                        duration_ms=p["duration_ms"],
                        output_tokens=usage.get("output_tokens"),
                        succeeded=p.get("error") is None,
                        run_id=None,
                        recorded_at=event["timestamp"],
                    )
                )
            )
    terminal = events[-1]
    return timings, project_outcome(progress, terminal["event_type"], terminal["payload"])


@dataclass(frozen=True)
class PlacementPrior:
    """Fully validated partition; seeding writes atomically before gateway startup."""

    bundle_digest: str
    configuration_digest: str
    timings: tuple[ModelTiming, ...]
    outcomes: tuple[dict, ...]
    provenance: dict[str, Any]

    def seed(self, store: RunStore) -> dict[str, Any]:
        return store.import_placement_prior(
            bundle_digest=self.bundle_digest,
            configuration_digest=self.configuration_digest,
            timings=list(self.timings),
            outcomes=list(self.outcomes),
            report=self.provenance,
        )


def load_prior(path: Path, configuration: dict) -> PlacementPrior:
    """Validate every partition/source before selecting the exact configuration."""
    validate_configuration(configuration)
    raw = path.read_bytes()
    bundle = _closed(json.loads(raw), {"schema_version", "kind", "partitions"}, "prior")
    if (
        type(bundle["schema_version"]) is not int
        or bundle["schema_version"] != 1
        or bundle["kind"] != "relaymind.placement_prior"
    ):
        raise ValueError("unsupported prior kind/version")
    if not isinstance(bundle["partitions"], list) or not bundle["partitions"]:
        raise ValueError("prior requires partitions")
    digest = hashlib.sha256(raw).hexdigest()
    partitions = {}
    for part in bundle["partitions"]:
        _closed(part, {"configuration", "include_outcomes", "sources"}, "partition")
        if (
            not isinstance(part["configuration"], dict)
            or type(part["include_outcomes"]) is not bool
            or not isinstance(part["sources"], list)
        ):
            raise ValueError("invalid prior partition")
        validate_configuration(part["configuration"])
        key = canonical_digest(part["configuration"])
        if key in partitions:
            raise ValueError("duplicate configuration partition")
        timings, outcomes, sources, ineligible, identities = [], [], [], [], set()
        timing_sources = []
        for ref in part["sources"]:
            _closed(ref, {"file", "sha256", "run_ids"}, "source")
            if (
                not isinstance(ref["file"], str)
                or not ref["file"]
                or not isinstance(ref["run_ids"], list)
                or not all(isinstance(r, str) and r for r in ref["run_ids"])
            ):
                raise ValueError("invalid source reference")
            source_path = path.parent / ref["file"]
            source_raw = source_path.read_bytes()
            if hashlib.sha256(source_raw).hexdigest() != ref["sha256"]:
                raise ValueError("source byte digest mismatch")
            source = json.loads(source_raw)
            if (
                source.get("kind") != "relaymind.correctness_corpus"
                or source.get("counterfactual")
                or source.get("prior_configuration") != part["configuration"]
            ):
                raise ValueError("source is counterfactual or configuration differs")
            if not source.get("application", {}).get("head"):
                raise ValueError("source application revision missing")
            records = source.get("records", {})
            for run_id in ref["run_ids"]:
                identity = (ref["sha256"], run_id)
                if identity in identities:
                    raise ValueError("duplicate source run identity")
                identities.add(identity)
                matching = [r for r in records.values() if r.get("run_id") == run_id]
                if len(matching) != 1:
                    raise ValueError("source run must have one actual record")
                events = validated_trace(source.get("traces", {}).get(run_id), run_id, matching[0])
                bind_trace_configuration(events, part["configuration"])
                observed, outcome = _observations(events)
                timings.extend(observed)
                timing_sources.extend(
                    {
                        "source_digest": ref["sha256"],
                        "source_run_id": run_id,
                        "call_id": e["payload"]["call_id"],
                    }
                    for e in events
                    if e["event_type"] == "model.finished"
                )
                if part["include_outcomes"] and outcome is not None:
                    outcomes.append(
                        dict(
                            source_digest=ref["sha256"],
                            source_run_id=run_id,
                            recorded_at=datetime.fromisoformat(events[-1]["timestamp"])
                            .isoformat(timespec="microseconds")
                            .replace("+00:00", "Z"),
                            **outcome,
                        )
                    )
                else:
                    ineligible.append(
                        dict(
                            source_digest=ref["sha256"],
                            source_run_id=run_id,
                            reason="timing_only"
                            if not part["include_outcomes"]
                            else "no_selected_skill",
                        )
                    )
            sources.append({**ref, "application": source["application"]})
        report = dict(
            bundle_sha256=digest,
            configuration_sha256=key,
            configuration=part["configuration"],
            projector_version=1,
            timings=len(timings),
            outcomes=len(outcomes),
            retained_timings=len(timings),
            retained_outcomes=len(outcomes),
            timing_sources=timing_sources,
            sources=sources,
            ineligible=ineligible,
        )
        partitions[key] = PlacementPrior(digest, key, tuple(timings), tuple(outcomes), report)
    try:
        return partitions[canonical_digest(configuration)]
    except KeyError as exc:
        raise ValueError("no exact prior configuration partition") from exc
