"""Whole-host trial freeze, deterministic plan, driver and offline grader.

Spec 0.60 section 12.6.5 (RM16, RM39, RM40, RM44; AC30, AC36, AC37). The same
driver and grader serve the early installed-skill trial (#198) and the paired
acceptance tasks (#205). Nothing here activates a host adapter, executes a
tool, grants an approval or establishes a measured result: the executor is
supplied by the caller and only the host executes tools. Summaries are always
recomputed from verified rows; a stored summary is compared, never trusted.
"""

import copy
import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from relaymind.canonical import canonical_digest, canonical_json
from relaymind.evaluation.asset_benchmark_io import validate_closed_document
from relaymind.schema_util import load_packaged_schema

ARMS = ("native", "provider_only", "recommendations")
ACCEPTANCE_TASKS = 100


class WholeTaskRejected(ValueError):
    """A structural, binding or digest contradiction; the export is rejected."""


class WholeTaskBlocked(Exception):
    """Required input or evidence is missing: unverifiable, never a pass."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _schema() -> dict:
    own = load_packaged_schema("relaymind.evaluation", "asset_whole_task.v1.json")
    shared = load_packaged_schema("relaymind.evaluation", "asset_benchmark_report.v1.json")
    defs = dict(shared["$defs"])
    # The retrieval Row is the published results item; reuse it, never a copy.
    defs["Row"] = shared["properties"]["results"]["items"]
    manifest = copy.deepcopy(defs["InputManifestV1"])
    kinds = manifest["properties"]["documents"]["items"]["properties"]["kind"]["enum"]
    kinds.extend(["host_tasks", "host_freeze", "episodes", "episode_freeze"])
    defs["S3InputManifestV1"] = manifest
    clash = set(defs) & set(own["$defs"])
    if clash:
        raise RuntimeError("schema_definition_clash")
    defs.update(own["$defs"])
    return defs


_DEFS = _schema()


def validate_whole_task(definition: str, value: Any) -> dict:
    """Validate one closed S3 document against its published definition."""
    try:
        validate_closed_document(
            {"$ref": "#/$defs/" + definition, "$defs": _DEFS}, value, "invalid_" + definition
        )
    except ValueError as exc:
        raise WholeTaskRejected(str(exc)) from None
    return dict(value)


def _utf8(value: str) -> bytes:
    return value.encode("utf-8")


def _strictly_sorted(keys: Sequence) -> bool:
    return all(a < b for a, b in zip(keys, keys[1:], strict=False))


def _binding_key(binding: Mapping) -> tuple:
    return (binding["asset_id"], binding["content_hash"], binding["schema_hash"])


# --- freeze and task documents --------------------------------------------------


def verify_host_freeze(freeze: Mapping) -> str:
    """Check the closed HostFreezeV1 and return its freeze_digest."""
    validate_whole_task("HostFreezeV1", freeze)
    configs = freeze["configurations"]
    if [c["arm"] for c in configs] != list(ARMS):
        raise WholeTaskRejected("arm_order")
    shared = {(c["provider"], c["model"], c["sampling_config_digest"]) for c in configs}
    if len({c["provider_config_digest"] for c in configs}) != 1 or len(shared) != 1:
        raise WholeTaskRejected("arm_variable")
    for config in configs:
        uses = config["arm"] == "recommendations"
        if (config["recommendation_config_digest"] is not None) != uses:
            raise WholeTaskRejected("recommendation_config")
    if not _strictly_sorted([_utf8(p["task_id"]) for p in freeze["task_policies"]]):
        raise WholeTaskRejected("policy_order")
    if not _strictly_sorted([_utf8(b["asset_id"]) for b in freeze["asset_bindings"]]):
        raise WholeTaskRejected("binding_order")
    package_keys = [
        (_utf8(p["name"]), _utf8(p["version"]), p["sha256"]) for p in freeze["packages"]
    ]
    if not _strictly_sorted(package_keys):
        raise WholeTaskRejected("package_order")
    return canonical_digest(freeze)


def verify_host_tasks(tasks: Mapping, freeze: Mapping) -> str:
    """Check AssetHostTasksV1 against its verified freeze; return input_digest."""
    freeze_digest = verify_host_freeze(freeze)
    validate_whole_task("AssetHostTasksV1", tasks)
    if (
        tasks["freeze_digest"] != freeze_digest
        or tasks["corpus_lock_digest"] != freeze["corpus_lock_digest"]
    ):
        raise WholeTaskRejected("freeze_binding")
    rows = tasks["tasks"]
    # Unsorted input is rejected, never silently executed in another order.
    if not _strictly_sorted([_utf8(t["id"]) for t in rows]):
        raise WholeTaskRejected("task_order")
    policies = {p["task_id"]: p for p in freeze["task_policies"]}
    if set(policies) != {t["id"] for t in rows}:
        raise WholeTaskRejected("policy_coverage")
    frozen = {_binding_key(b) for b in freeze["asset_bindings"]}
    for task in rows:
        policy = policies[task["id"]]
        if (
            task["permissions_digest"] != canonical_digest(policy["permissions"])
            or task["budgets_digest"] != policy["budgets_digest"]
        ):
            raise WholeTaskRejected("policy_binding")
        bindings = task["asset_bindings"]
        if (
            not _strictly_sorted([_utf8(b["asset_id"]) for b in bindings])
            or not {_binding_key(b) for b in bindings} <= frozen
        ):
            raise WholeTaskRejected("asset_binding")
    if (
        sorted({t["success_rubric_evidence_id"] for t in rows}, key=_utf8)
        != freeze["rubric_evidence_ids"]
    ):
        raise WholeTaskRejected("rubric_binding")
    return canonical_digest(tasks)


def frozen_evidence_ids(tasks: Mapping, freeze: Mapping) -> set[str]:
    """Evidence the frozen inputs name; each must resolve in the registry."""
    ids = {p["evidence_id"] for p in freeze["packages"]}
    ids.update(freeze["host"]["distribution_evidence_ids"])
    ids.update(freeze["rubric_evidence_ids"])
    for task in tasks["tasks"]:
        ids.update(task["setup_evidence_ids"])
    return ids


# --- deterministic plan -----------------------------------------------------------


def _arm_key(seed: int, task_id: str, arm: str) -> tuple:
    payload = canonical_json(dict(seed=seed, task_id=task_id, arm=arm)).encode("utf-8")
    return (hashlib.sha256(payload).hexdigest(), _utf8(arm))


def plan_host_trial(tasks: Mapping) -> list[dict]:
    """Plan all three arms of every task before setup, in frozen task order."""
    plan = []
    for task in tasks["tasks"]:
        for arm in sorted(ARMS, key=lambda a: _arm_key(tasks["seed"], task["id"], a)):
            plan.append(dict(task_id=task["id"], arm=arm, ordinal=len(plan) + 1))
    return plan


# --- attempts and rows ----------------------------------------------------------


def normalized_total_tokens(attempt: Mapping) -> int | None:
    """Normalize a total only from evidenced component relationships."""
    parts = [
        attempt[k] for k in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")
    ]
    relation = attempt["token_relationship"]
    inp, out, cached, reasoning = parts
    total = None
    if relation == "component_disjoint":
        if all(p is not None for p in parts):
            total = sum(parts)
    elif relation == "cached_within_input_reasoning_within_output":
        if (cached is not None and inp is not None and cached > inp) or (
            reasoning is not None and out is not None and reasoning > out
        ):
            raise WholeTaskRejected("token_subset")
        if inp is not None and out is not None:
            total = inp + out
    if (
        total is not None
        and attempt["total_tokens"] is not None
        and attempt["total_tokens"] != total
    ):
        raise WholeTaskRejected("token_total")
    return total


_NOT_RUN_NULLS = (
    "success",
    "elapsed_ms",
    "active_ms",
    "human_wait_ms",
    "interactions",
    "local_turns",
    "cpu_ms",
    "peak_bytes",
    "host_session_id",
)


def _instant(value: str) -> datetime:
    """A UTC RFC3339 time as an instant; text order differs across precisions."""
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise WholeTaskRejected("attempt_timing") from None


def _check_attempts(row: Mapping, seen: set[str]) -> None:
    for index, attempt in enumerate(row["attempts"], start=1):
        if attempt["ordinal"] != index:
            raise WholeTaskRejected("attempt_ordinal")
        if attempt["id"] in seen:
            raise WholeTaskRejected("attempt_identity")
        seen.add(attempt["id"])
        if attempt["calls"] and not attempt["evidence_ids"]:
            raise WholeTaskRejected("dispatch_evidence")
        if attempt["usage_provenance"] == "unknown" and any(
            attempt[k] is not None
            for k in (
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "reasoning_tokens",
                "total_tokens",
            )
        ):
            raise WholeTaskRejected("usage_provenance")
        start, end = attempt["started_at"], attempt["ended_at"]
        if start is not None and end is not None and _instant(end) < _instant(start):
            raise WholeTaskRejected("attempt_timing")
        normalized_total_tokens(attempt)


def _check_row(row: Mapping, config: Mapping, seen: set[str]) -> None:
    status = row["status"]
    if status == "not_run":
        if (
            any(row[k] is not None for k in _NOT_RUN_NULLS)
            or row["attempts"]
            or row["recommendation_ids"]
        ):
            raise WholeTaskRejected("not_run_work")
    if status in ("failed", "timeout", "cancelled") and row["success"] is True:
        raise WholeTaskRejected("row_verdict")
    if row["success"] is True and (row["host_session_id"] is None or not row["evidence_ids"]):
        # A proposed tool or gateway trace alone is not task success.
        raise WholeTaskRejected("success_evidence")
    elapsed = row["elapsed_ms"]
    if elapsed is not None and any(
        row[k] is not None and row[k] > elapsed for k in ("active_ms", "human_wait_ms")
    ):
        raise WholeTaskRejected("row_timing")
    if config["recommendation_config_digest"] is None and row["recommendation_ids"]:
        raise WholeTaskRejected("recommendation_arm")
    _check_attempts(row, seen)


def _row_cost(row: Mapping) -> tuple[int | None, int | None]:
    """External tokens and calls of one row; None when coverage is incomplete."""
    if row["status"] == "not_run":
        return None, None
    external = [a for a in row["attempts"] if a["external"]]
    if not external:
        # Zero needs complete no-dispatch evidence, which an offline grader
        # cannot establish from evidence IDs; the cost stays unknown.
        return None, None
    totals = [normalized_total_tokens(a) for a in external]
    calls = [a["calls"] for a in external]
    tokens = None if any(t is None for t in totals) else sum(totals)
    count = None if any(c is None for c in calls) else sum(calls)
    return tokens, count


def _verdict(row: Mapping) -> bool | None:
    """The row's own verdict; counting it failed does not make a null known."""
    if row["status"] == "not_run":
        return None
    return row["success"]


# --- summary --------------------------------------------------------------------


def _difference(by_arm: Mapping, key: str, planned_tasks: int) -> float | int | None:
    a, b = by_arm["recommendations"][key], by_arm["native"][key]
    if not planned_tasks or a is None or b is None:
        return None
    difference = (a - b) / planned_tasks
    return int(difference) if difference == int(difference) else difference


def _summarize(rows: Sequence[Mapping], task_ids: Sequence[str], acceptance_ok: bool) -> dict:
    arms = []
    for arm in ARMS:
        own = [r for r in rows if r["arm"] == arm]
        costs = [_row_cost(r) for r in own]
        success = sum(r["status"] == "completed" and r["success"] is True for r in own)
        not_run = sum(r["status"] == "not_run" for r in own)
        tokens = [c[0] for c in costs]
        calls = [c[1] for c in costs]
        arms.append(
            dict(
                arm=arm,
                planned=len(own),
                success=success,
                failed=len(own) - success - not_run,
                not_run=not_run,
                external_tokens=None if None in tokens else sum(tokens),
                external_calls=None if None in calls else sum(calls),
                unknown_cost_rows=sum(t is None or c is None for t, c in costs),
            )
        )
    by_arm = {a["arm"]: a for a in arms}
    verdicts = {(r["task_id"], r["arm"]): _verdict(r) for r in rows}
    gains = losses = 0
    unknown = False
    for task_id in task_ids:
        native = verdicts[(task_id, "native")]
        recommended = verdicts[(task_id, "recommendations")]
        if native is None or recommended is None:
            unknown = True
        elif recommended and not native:
            gains += 1
        elif native and not recommended:
            losses += 1
    planned_tasks = len(task_ids)
    if not planned_tasks or unknown:
        success_gate = "blocked"
    else:
        success_gate = "pass" if gains >= losses else "fail"
    token_difference = _difference(by_arm, "external_tokens", planned_tasks)
    call_difference = _difference(by_arm, "external_calls", planned_tasks)
    known = [d for d in (token_difference, call_difference) if d is not None]
    if not planned_tasks:
        cost_gate = "blocked"
    elif any(d > 0 for d in known):
        cost_gate = "fail"
    elif len(known) < 2:
        cost_gate = "blocked"
    else:
        cost_gate = "pass"
    if "fail" in (success_gate, cost_gate):
        gate = "fail"
    elif "blocked" in (success_gate, cost_gate) or not acceptance_ok:
        gate = "blocked"
    else:
        gate = "pass"
    return dict(
        planned_tasks=planned_tasks,
        arms=arms,
        gains=gains,
        losses=losses,
        external_token_difference_per_task=token_difference,
        external_call_difference_per_task=call_difference,
        success_gate=success_gate,
        cost_gate=cost_gate,
        gate=gate,
    )


def _qualified_acceptance(report: Mapping) -> bool:
    """Only verified qualified acceptance could pass; no review path exists yet.

    Qualification needs resolved actual corpus, execution, human/platform and
    artifact evidence, never a caller flag. This revision supplies no reviewed
    verifier for that evidence, so every report is blocked rather than passed.
    """
    return False


def host_summary(report: Mapping, tasks: Mapping, freeze: Mapping) -> dict:
    """Recompute HostSummary, with per-lane summaries, from the report rows."""
    acceptance_ok = (
        report["kind"] == "acceptance"
        and report["qualification"] == "qualified"
        and _qualified_acceptance(report)
    )
    task_ids = [t["id"] for t in tasks["tasks"]]
    summary = _summarize(
        report["rows"], task_ids, acceptance_ok and len(task_ids) >= ACCEPTANCE_TASKS
    )
    lanes = []
    for lane in sorted({t["lane"] for t in tasks["tasks"]}, key=_utf8):
        ids = [t["id"] for t in tasks["tasks"] if t["lane"] == lane]
        lane_rows = [r for r in report["rows"] if r["task_id"] in set(ids)]
        own = _summarize(lane_rows, ids, acceptance_ok)
        own["lane"] = lane
        own["planned_tasks"] = own.pop("planned_tasks")
        lanes.append(
            {
                k: own[k]
                for k in (
                    "lane",
                    "planned_tasks",
                    "arms",
                    "gains",
                    "losses",
                    "external_token_difference_per_task",
                    "external_call_difference_per_task",
                    "success_gate",
                    "cost_gate",
                    "gate",
                )
            }
        )
    summary["lanes"] = lanes
    return summary


# --- evidence namespace ----------------------------------------------------------


def verify_evidence(
    evidence: Sequence[Mapping],
    nested_ids: set[str],
    registry: Mapping[str, Mapping] | None,
    frozen_ids: set[str] = frozenset(),
    packages: Sequence[Mapping] = (),
) -> None:
    """One report-wide evidence namespace bound to the companion registry.

    ``registry`` maps an evidence ID to ``{sha256, bytes, path}`` from the
    verified input manifest. Without it, references are only checked for
    closure inside the report; frozen-input evidence cannot be resolved.
    """
    ids = [e["id"] for e in evidence]
    if not _strictly_sorted([_utf8(i) for i in ids]):
        raise WholeTaskRejected("evidence_order")
    if not nested_ids <= set(ids):
        raise WholeTaskRejected("evidence_dangling")
    if registry is None:
        return
    if not frozen_ids <= set(registry):
        raise WholeTaskRejected("evidence_dangling")
    for package in packages:
        entry = registry[package["evidence_id"]]
        if (entry["sha256"], entry["bytes"]) != (package["sha256"], package["bytes"]):
            raise WholeTaskRejected("package_binding")
    for item in evidence:
        entry = registry.get(item["id"])
        if entry is None:
            raise WholeTaskRejected("evidence_dangling")
        if (entry["sha256"], entry["bytes"], entry["path"]) != (
            item["sha256"],
            item["bytes"],
            item["path"],
        ):
            raise WholeTaskRejected("evidence_conflict")


# --- grader ---------------------------------------------------------------------


def grade_host_report(
    report: Mapping,
    tasks: Mapping,
    freeze: Mapping,
    *,
    registry: Mapping[str, Mapping] | None = None,
) -> dict:
    """Verify an AssetHostReportV1 against its frozen inputs; recompute summary.

    Contradictions raise WholeTaskRejected. The returned summary is the
    recomputed one; a stored summary that differs is a contradiction.
    """
    input_digest = verify_host_tasks(tasks, freeze)
    validate_whole_task("AssetHostReportV1", report)
    if report["input_digest"] != input_digest or report["kind"] != tasks["kind"]:
        raise WholeTaskRejected("input_binding")
    if report["freeze_digest"] != tasks["freeze_digest"]:
        raise WholeTaskRejected("freeze_binding")
    plan = plan_host_trial(tasks)
    if report["planned"] != plan:
        raise WholeTaskRejected("plan_mismatch")
    if [(r["task_id"], r["arm"]) for r in report["rows"]] != [
        (p["task_id"], p["arm"]) for p in plan
    ]:
        raise WholeTaskRejected("row_plan")
    configs = {c["arm"]: c for c in freeze["configurations"]}
    seen: set[str] = set()
    nested: set[str] = set()
    for row in report["rows"]:
        _check_row(row, configs[row["arm"]], seen)
        nested.update(row["evidence_ids"])
        for attempt in row["attempts"]:
            nested.update(attempt["evidence_ids"])
    verify_evidence(
        report["evidence"],
        nested,
        registry,
        frozen_evidence_ids(tasks, freeze),
        freeze["packages"],
    )
    summary = host_summary(report, tasks, freeze)
    if canonical_json(report["summary"]) != canonical_json(summary):
        raise WholeTaskRejected("summary_mismatch")
    return summary


# --- driver ---------------------------------------------------------------------

Executor = Callable[[Mapping, str, Mapping], tuple[Mapping, Sequence[Mapping]]]


def _not_run(task_id: str, arm: str, reason: str) -> dict:
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
        reason=reason,
    )


def run_host_trial(
    tasks: Mapping,
    freeze: Mapping,
    execute: Executor,
    *,
    environment: Mapping,
    artifacts: Sequence[Mapping],
    qualification: str,
    input_manifest: Mapping,
    interrupted: Callable[[], bool],
) -> dict:
    """Execute the frozen plan through a host executor and export every row.

    ``execute(task, arm, arm_config)`` runs one planned task/arm on the host
    and returns ``(HostRow, [EvidenceRef])`` for the work it actually started,
    retaining every consumed attempt. After an interruption the remaining
    planned rows are exported as not_run, never dropped or invented.
    """
    verify_host_tasks(tasks, freeze)
    by_id = {t["id"]: t for t in tasks["tasks"]}
    configs = {c["arm"]: c for c in freeze["configurations"]}
    plan = plan_host_trial(tasks)
    rows, evidence = [], {}
    for planned in plan:
        task_id, arm = planned["task_id"], planned["arm"]
        if interrupted():
            rows.append(_not_run(task_id, arm, "interrupted"))
            continue
        row, refs = execute(by_id[task_id], arm, copy.deepcopy(configs[arm]))
        if (row["task_id"], row["arm"]) != (task_id, arm):
            raise WholeTaskRejected("row_plan")
        rows.append(copy.deepcopy(dict(row)))
        for ref in refs:
            if evidence.setdefault(ref["id"], dict(ref)) != dict(ref):
                raise WholeTaskRejected("evidence_conflict")
    report = dict(
        schema_version=1,
        kind=tasks["kind"],
        qualification=qualification,
        input_digest=canonical_digest(tasks),
        freeze_digest=tasks["freeze_digest"],
        input_manifest=dict(input_manifest),
        environment=dict(environment),
        artifacts=[dict(a) for a in artifacts],
        planned=plan,
        rows=rows,
        summary=None,
        evidence=[evidence[i] for i in sorted(evidence, key=_utf8)],
    )
    report["summary"] = host_summary(report, tasks, freeze)
    grade_host_report(report, tasks, freeze)
    return report
