"""Platform lifecycle export: closed steps, dedicated evidence and gate.

Spec 0.60 section 12.6.5 (RM38, RM39, RM44; AC29, AC30, AC35, AC37). The
export binds the same frozen host tasks, freeze and installed package as the
host trial. Fixture reports are not_applicable to measured acceptance. A
measured report cannot pass here: the operational workload grading of
``asset_operations`` and the actual Apple Silicon macOS / native x86-64 Windows
review are not bound by this revision, so a measured report without a known
failure is blocked, never passed by a caller-supplied flag.
"""

from collections.abc import Mapping

from relaymind.evaluation.asset_whole_task import (
    WholeTaskRejected,
    validate_whole_task,
    verify_evidence,
    verify_host_tasks,
)

PLATFORM_STEPS = (
    "install",
    "configure",
    "recommend",
    "choose",
    "skill_use",
    "mcp_use",
    "feedback",
    "delete",
    "reset",
    "restart",
    "uninstall",
)


def platform_gate(report: Mapping) -> str:
    """Known failure first, then missing evidence; fixture is not_applicable."""
    if report["qualification"] == "fixture":
        return "not_applicable"
    statuses = [s["status"] for s in report["steps"]]
    if "failed" in statuses:
        return "fail"
    return "blocked"


def grade_platform_report(
    report: Mapping,
    tasks: Mapping,
    freeze: Mapping,
    *,
    registry: Mapping[str, Mapping] | None = None,
) -> str:
    """Verify an AssetPlatformReportV1 against its host inputs; return the gate."""
    input_digest = verify_host_tasks(tasks, freeze)
    validate_whole_task("AssetPlatformReportV1", report)
    if report["input_digest"] != input_digest:
        raise WholeTaskRejected("input_binding")
    if tuple(s["name"] for s in report["steps"]) != PLATFORM_STEPS:
        raise WholeTaskRejected("step_order")
    nested = set()
    for step in report["steps"]:
        passed = step["status"] == "passed"
        if passed and not step["evidence_ids"]:
            raise WholeTaskRejected("step_receipt")
        if passed != (step["reason"] is None):
            raise WholeTaskRejected("step_reason")
        nested.update(step["evidence_ids"])
    registered = {e["id"]: e for e in report["evidence"]}
    dedicated = list(report["approvals_evidence"])
    if report["operations_evidence"] is not None:
        dedicated.append(report["operations_evidence"])
    for item in dedicated:
        entry = registered.get(item["id"])
        if entry is None:
            raise WholeTaskRejected("evidence_dangling")
        if entry != item:
            raise WholeTaskRejected("evidence_conflict")
    verify_evidence(report["evidence"], nested, registry)
    gate = platform_gate(report)
    if report["gate"] != gate:
        raise WholeTaskRejected("gate_mismatch")
    return gate
