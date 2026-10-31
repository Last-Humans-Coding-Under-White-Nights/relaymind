"""Builders for the spec 0.60 section 12.6.5 export tests (fixture data only)."""

import copy
import hashlib

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_whole_task import ARMS, host_summary, plan_host_trial

H = "a" * 64


def h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def freeze_doc(task_ids=("t1", "t2"), bindings=None) -> dict:
    bindings = (("asset-a", h("a")),) if bindings is None else bindings
    configs = [
        dict(
            arm=arm,
            provider="provider-x",
            model="model-x",
            provider_config_digest=h("provider"),
            sampling_config_digest=h("sampling"),
            host_config_digest=h("host-" + arm),
            recommendation_config_digest=h("rec") if arm == "recommendations" else None,
        )
        for arm in ARMS
    ]
    return dict(
        schema_version=1,
        corpus_lock_digest=h("lock"),
        configurations=configs,
        task_policies=[
            dict(task_id=t, permissions=["demo.read"], budgets_digest=h("budget-" + t))
            for t in task_ids
        ],
        native_inventory_digest=h("inventory"),
        packages=[
            dict(
                name="relaymind-openclaw",
                version="1.0.0",
                sha256=h("pkg"),
                bytes=10,
                evidence_id="ev-package",
            )
        ],
        host=dict(
            name="openclaw",
            version="2026.10.7",
            sdk_version="2026.10.7",
            distribution_evidence_ids=["ev-host"],
        ),
        asset_bindings=[dict(asset_id=a, content_hash=c, schema_hash=None) for a, c in bindings],
        rubric_evidence_ids=sorted("rubric-" + t for t in task_ids),
    )


def tasks_doc(freeze: dict, lanes=("en_to_en", "zh_hans_to_zh_hans"), kind="early_trial") -> dict:
    tasks = []
    for policy, lane in zip(freeze["task_policies"], lanes, strict=True):
        t = policy["task_id"]
        tasks.append(
            dict(
                id=t,
                lane=lane,
                family_ids=["family-" + t],
                prompt_case_id="case-" + t,
                setup_evidence_ids=["setup-" + t],
                success_rubric_evidence_id="rubric-" + t,
                permissions_digest=canonical_digest(policy["permissions"]),
                budgets_digest=policy["budgets_digest"],
                asset_bindings=copy.deepcopy(freeze["asset_bindings"]),
                mutation=False,
            )
        )
    return dict(
        schema_version=1,
        kind=kind,
        corpus_lock_digest=freeze["corpus_lock_digest"],
        seed=7,
        freeze_digest=canonical_digest(freeze),
        tasks=tasks,
    )


def attempt(
    id: str,
    *,
    external=True,
    tokens=(10, 5, 0, 0),
    relation="component_disjoint",
    calls=1,
    total=None,
) -> dict:
    inp, out, cached, reasoning = tokens
    return dict(
        id=id,
        ordinal=1,
        model="model-x",
        provider="provider-x",
        external=external,
        started_at="2026-10-31T00:00:00Z",
        ended_at="2026-10-31T00:00:01Z",
        input_tokens=inp,
        output_tokens=out,
        cached_tokens=cached,
        reasoning_tokens=reasoning,
        total_tokens=total,
        token_relationship=relation,
        calls=calls,
        usage_provenance="observed",
        evidence_ids=["ev-" + id],
        outcome="success",
    )


def row(task_id: str, arm: str, *, success=True, status="completed", attempts=None) -> dict:
    if status == "not_run":
        return dict(
            task_id=task_id,
            arm=arm,
            status="not_run",
            success=None,
            attempts=[],
            elapsed_ms=None,
            active_ms=None,
            human_wait_ms=None,
            interactions=None,
            local_turns=None,
            cpu_ms=None,
            peak_bytes=None,
            recommendation_ids=[],
            host_session_id=None,
            evidence_ids=[],
            reason="interrupted",
        )
    if attempts is None:
        attempts = [attempt(f"{task_id}-{arm}-1")]
    return dict(
        task_id=task_id,
        arm=arm,
        status=status,
        success=success,
        attempts=attempts,
        elapsed_ms=100.0,
        active_ms=90.0,
        human_wait_ms=10.0,
        interactions=1,
        local_turns=0,
        cpu_ms=None,
        peak_bytes=None,
        recommendation_ids=["rec-" + task_id] if arm == "recommendations" else [],
        host_session_id="session-" + task_id + "-" + arm,
        evidence_ids=["run-" + task_id + "-" + arm],
        reason=None if status == "completed" else "host_failed",
    )


ENVIRONMENT = dict(
    os="Linux",
    os_version=None,
    architecture="x86_64",
    cpu=None,
    ram_bytes=None,
    gpu=None,
    python="3.12.0",
    dependencies_digest=H,
    runtime=None,
    runtime_version=None,
    runtime_config_digest=None,
)


def report_for(freeze, tasks, rows, *, qualification="fixture") -> dict:
    ids = sorted(
        {i for r in rows for i in r["evidence_ids"]}
        | {i for r in rows for a in r["attempts"] for i in a["evidence_ids"]}
    )
    report = dict(
        schema_version=1,
        kind=tasks["kind"],
        qualification=qualification,
        input_digest=canonical_digest(tasks),
        freeze_digest=canonical_digest(freeze),
        input_manifest=dict(path="inputs.json", digest=H, bytes=1),
        environment=ENVIRONMENT,
        artifacts=[],
        planned=plan_host_trial(tasks),
        rows=rows,
        summary=None,
        evidence=[dict(id=i, path=None, sha256=h(i), bytes=1, provenance="fixture") for i in ids],
    )
    report["summary"] = host_summary(report, tasks, freeze)
    return report


def ordered_rows(tasks, make) -> list:
    return [make(p["task_id"], p["arm"]) for p in plan_host_trial(tasks)]
