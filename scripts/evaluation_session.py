"""Shared runner provenance, frozen-prior startup and durable work directories (RM25)."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from relaymind.evaluation.checkpoint import Checkpoint
from relaymind.evaluation.environment import source_fingerprint
from relaymind.evaluation.prior import PlacementPrior, byte_digest, load_prior
from relaymind.orchestrator.local_loop import EXTERNAL_OPTIONS, LOCAL_OPTIONS
from relaymind.store import RunStore


def prior_configuration(runtime: Any, external: Any, configuration: Any) -> dict[str, Any]:
    """Exact throughput/cost partition; policy/chain/streaming are independent factors."""
    return {
        "local": None
        if runtime is None
        else {
            "model_id": runtime.manifest.model_id,
            "artifact_sha256": runtime.manifest.file_sha256,
            "manifest_sha256": byte_digest(runtime.manifest_path),
            "runtime_banner": runtime.banner,
            "runtime_argv": runtime.server_argv,
        },
        "external": None
        if external is None
        else {
            "base_url": external.base_url,
            "model": external.model,
            "context_limit": external.context_limit,
            "request_options": dict(getattr(external, "request_options", None) or {}),
            "generation_options": asdict(EXTERNAL_OPTIONS),
        },
        "local_generation_options": asdict(LOCAL_OPTIONS),
        "budgets": configuration.budgets.to_json_object(),
    }


def prepare(
    args: argparse.Namespace, root: Path, runtime: Any, external: Any, configuration: Any
) -> tuple[dict, PlacementPrior | None, dict, Checkpoint | None]:
    partition = prior_configuration(runtime, external, configuration)
    prior = (
        load_prior(Path(args.timing_samples), partition)
        if getattr(args, "timing_samples", None)
        else None
    )
    # Validate retention and row limits before any runtime launch or gateway startup.
    if prior:
        with RunStore(":memory:") as store:
            prior.seed(store)
    source = source_fingerprint(root)
    checkpoint = None
    if getattr(args, "checkpoint", None):
        identity = {
            "application": source,
            "configuration": configuration.to_json_object(),
            "prior_configuration": partition,
            "prior": None if prior is None else prior.provenance,
            "arguments": vars(args),
        }
        checkpoint = Checkpoint(Path(args.checkpoint), json.loads(json.dumps(identity)))
    return partition, prior, source, checkpoint


def seed_store(work: Path, prior: PlacementPrior | None) -> dict | None:
    if prior is None:
        return None
    with RunStore(work / "runs.sqlite3") as store:
        report = prior.seed(store)
        return {**report, "initial_expected_wait_prerequisites": prerequisites(prior, store)}


def snapshot_store(work: Path) -> list[dict]:
    with RunStore(work / "runs.sqlite3") as store:
        return [s.to_json_object() for s in store.model_timings()]


def durable_work(args: argparse.Namespace, prefix: str, temporary: Path | None) -> Path | None:
    if not getattr(args, "checkpoint", None):
        return temporary
    work = Path(args.checkpoint).with_suffix(".work")
    work.mkdir(parents=True, exist_ok=False)
    return work


def existing_result(checkpoint: Checkpoint | None, out: str | Path) -> dict | None:
    if checkpoint is None or not checkpoint.document["completed"]:
        return None
    path = Path(out)
    if not path.exists() or byte_digest(path) != checkpoint.document["result"]["export_sha256"]:
        raise ValueError("finalized checkpoint export digest differs")
    return json.loads(path.read_text("utf-8"))


def prerequisites(prior: PlacementPrior, store: RunStore) -> list[dict]:
    """Initial history availability, separate from actual per-generation inequality evidence."""
    partition = prior.provenance["configuration"]
    local_model = (partition.get("local") or {}).get("model_id")
    external_model = (partition.get("external") or {}).get("model")
    skills = {row["skill_id"] for row in prior.outcomes}
    skills.update(
        path.parent.name
        for path in (Path(__file__).resolve().parent.parent / "skills").glob(
            "*/skill.manifest.json"
        )
    )
    timings = (
        []
        if external_model is None
        else [
            sample
            for sample in store.model_timings(model=external_model, actor="external")
            if sample.succeeded and sample.kind == "run"
        ]
    )
    rows = []
    for skill in sorted(skills):
        estimate = store.outcome_estimate(local_model, skill, external_model=external_model)
        missing = []
        if local_model is None:
            missing.append("local_model")
        if external_model is None:
            missing.append("external_model")
        if estimate.local_success_rate is None:
            missing.append("local_outcomes")
        if not timings:
            missing.append("external_timings")
        if estimate.median_external_generations is None:
            missing.append("external_completed_outcomes")
        rows.append(
            {
                "skill_id": skill,
                "local_model": local_model,
                "external_model": external_model,
                "local_completed": estimate.local_completed,
                "local_escalated": estimate.local_escalated,
                "external_completed": estimate.external_completed,
                "median_external_generations": estimate.median_external_generations,
                "external_timing_count": len(timings),
                "missing": missing,
            }
        )
    return rows
