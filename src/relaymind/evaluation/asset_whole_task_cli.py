"""Plan and grade the section 12.6.5 host, adaptation and platform exports.

Spec 0.60 section 12.6.5 (RM16, RM38–RM44; AC29–AC37). Exit codes:
0 the gate passed or is not_applicable to acceptance (fixture platform or
adaptation report), 1 a known gate failure, 2 the export or its inputs were
rejected (contradiction), 3 blocked (missing or unverifiable required input or
evidence). Blocked is never reported as a pass. A graded result records the
qualification tier assessed from the inputs (#240), never the report's label.
"""

import argparse
import json
import sys
from pathlib import Path

from relaymind.evaluation.asset_benchmark_io import (
    REPORT_BYTES,
    atomic_bytes,
    json_bytes,
    read_json_path,
)
from relaymind.evaluation.asset_corpus_io import DOCUMENT_BYTES, AssetCorpusError
from relaymind.evaluation.asset_episode_export import (
    grade_adaptation_report,
    probe_plan,
    reset_preimages,
    verify_episodes,
)
from relaymind.evaluation.asset_evidence import OfflineResolver
from relaymind.evaluation.asset_platform_export import grade_platform_report
from relaymind.evaluation.asset_whole_task import (
    WholeTaskRejected,
    grade_host_report,
    plan_host_trial,
    validate_whole_task,
    verify_host_tasks,
)
from relaymind.evaluation.asset_whole_task_corpus import corpus_offers
from relaymind.evaluation.asset_whole_task_inputs import (
    WholeTaskBlocked,
    resolve_whole_task_inputs,
)
from relaymind.evaluation.asset_whole_task_qualification import (
    assess_inputs,
    check_claim,
    enforce_qualification,
)

_EXIT = {"pass": 0, "not_applicable": 0, "fail": 1, "blocked": 3}
_REPORTS = {
    "grade-host": "AssetHostReportV1",
    "grade-platform": "AssetPlatformReportV1",
    "grade-adaptation": "AssetAdaptationReportV1",
}


def _grade(args) -> tuple[str, dict]:
    report, _ = read_json_path(args.report, REPORT_BYTES)
    # Check the report's own shape before following any of its references.
    validate_whole_task(_REPORTS[args.command], report)
    resolver = OfflineResolver(args.resolver) if args.resolver else None
    kind = args.command.removeprefix("grade-")
    inputs = resolve_whole_task_inputs(
        "adaptation" if kind == "adaptation" else "host", report, args.inputs, resolver=resolver
    )
    # Qualification is assessed from the inputs, never taken from the label
    # (#240): a known fixture contradiction rejects before grading.
    assessment = assess_inputs(kind, report, inputs)
    check_claim(assessment)
    extra = dict(unverified=list(inputs.unverified), qualification=assessment.as_output())
    if kind == "adaptation":
        summary = grade_adaptation_report(
            report,
            inputs.root,
            inputs.freeze,
            inputs.labels,
            reset_preimages=reset_preimages(inputs.evidence),
            registry=inputs.registry,
            offers=corpus_offers(
                inputs.release,
                dict(
                    off=inputs.configs[inputs.freeze["off_config_digest"]],
                    on=inputs.configs[inputs.freeze["on_config_digest"]],
                ),
            ),
        )
    elif kind == "host":
        summary = grade_host_report(report, inputs.root, inputs.freeze, registry=inputs.registry)
    else:
        gate = grade_platform_report(report, inputs.root, inputs.freeze, registry=inputs.registry)
        return enforce_qualification(assessment, gate), extra
    return enforce_qualification(assessment, summary["gate"]), dict(summary=summary, **extra)


def _plan_host(args) -> dict:
    """The preregistered task commitments and the deterministic three-arm plan."""
    tasks, _ = read_json_path(args.tasks, DOCUMENT_BYTES)
    freeze, _ = read_json_path(args.freeze, DOCUMENT_BYTES)
    return dict(
        input_digest=verify_host_tasks(tasks, freeze),
        freeze_digest=tasks["freeze_digest"],
        planned=plan_host_trial(tasks),
    )


def _plan_episodes(args) -> dict:
    """The preregistered episode commitments and per-episode probe plan."""
    episodes, _ = read_json_path(args.episodes, DOCUMENT_BYTES)
    freeze, _ = read_json_path(args.freeze, DOCUMENT_BYTES)
    return dict(
        input_digest=verify_episodes(episodes, freeze),
        config_digest=episodes["config_digest"],
        episodes=[
            dict(
                episode_id=episode["id"],
                corrections=[c["case_id"] for c in episode["corrections"]],
                probes=[
                    dict(kind=kind, case_id=case_id, ordinal=n)
                    for n, (kind, case_id) in enumerate(probe_plan(episode), start=1)
                ],
                reset_probe_case_ids=list(episode["reset_probe_case_ids"]),
            )
            for episode in episodes["episodes"]
        ],
    )


_PLANS = {"plan-host": _plan_host, "plan-episodes": _plan_episodes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Spec 0.60 section 12.6.5 host/adaptation/platform exports. Fixture or "
            "provisional reports never pass acceptance."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan-host", help="write the deterministic three-arm plan")
    plan.add_argument("--tasks", type=Path, required=True)
    plan.add_argument("--freeze", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    episodes = sub.add_parser(
        "plan-episodes", help="verify the frozen episodes and write their probe plan"
    )
    episodes.add_argument("--episodes", type=Path, required=True)
    episodes.add_argument("--freeze", type=Path, required=True)
    episodes.add_argument("--output", type=Path, required=True)
    for name in ("grade-host", "grade-platform", "grade-adaptation"):
        grade = sub.add_parser(name)
        grade.add_argument("--report", type=Path, required=True)
        grade.add_argument("--inputs", type=Path, required=True, help="companion input manifest")
        grade.add_argument(
            "--resolver",
            type=Path,
            help=(
                "explicit OfflineResolverV1 file; required to resolve the runner and "
                "protocol inventory preimages, without it grading exits 3 "
                "(blocked: inventory_preimage_unresolved)"
            ),
        )
        grade.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in _PLANS:
            atomic_bytes(args.output, json_bytes(_PLANS[args.command](args)))
            return 0
        gate, extra = _grade(args)
        result = dict(status="graded", gate=gate, reason=None, **extra)
        code = _EXIT[gate]
    except WholeTaskBlocked as exc:
        result, code = dict(status="blocked", gate="blocked", reason=exc.reason), 3
    except (WholeTaskRejected, AssetCorpusError, ValueError, KeyError, TypeError) as exc:
        reason = str(exc) if str(exc).isidentifier() else "invalid_input"
        if args.command in _PLANS:
            print(json.dumps(dict(status="rejected", reason=reason)), file=sys.stderr)
            return 2
        result, code = dict(status="rejected", gate=None, reason=reason), 2
    atomic_bytes(args.output, json_bytes(result))
    print(json.dumps(dict(status=result["status"], gate=result["gate"], reason=result["reason"])))
    return code
