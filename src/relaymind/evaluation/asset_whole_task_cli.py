"""Plan and grade the section 12.6.5 host, adaptation and platform exports.

Spec 0.60 section 12.6.5 (RM16, RM38–RM44; AC29–AC37). Exit codes:
0 the gate passed or is not_applicable to acceptance (fixture platform or
adaptation report), 1 a known gate failure, 2 the export or its inputs were
rejected (contradiction), 3 blocked (missing or unverifiable required input or
evidence). Blocked is never reported as a pass. A graded result records the
qualification tier assessed from the inputs (#240), never the report's label.

The ``trial-*`` commands are the #234 early installed-skill trial kit
(``asset_trial_kit``): a target machine/host/release preflight, the packaged
plugin installed with the pinned host CLI, and the frozen plan run over
operator-captured host observations, bundled and graded with the grader above.
They invoke the host CLI only for ``--version``, ``plugins install`` and
``plugins inspect``; they never execute a task, a tool or an approval.
"""

import argparse
import json
import signal
import sys
from pathlib import Path

from relaymind.evaluation import asset_trial_kit
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
from relaymind.evaluation.asset_run_kit_cli import COMMANDS as RUN_KIT_COMMANDS
from relaymind.evaluation.asset_run_kit_cli import add_commands as add_run_kit_commands
from relaymind.evaluation.asset_run_kit_cli import run as run_kit
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


def _host(args) -> list[str]:
    host = list(args.host)
    if host[:1] == ["--"]:
        host = host[1:]
    if not host:
        raise SystemExit("the OpenClaw command follows --, e.g. -- openclaw")
    return host


def _trial_preflight(args, environment: dict) -> dict:
    frozen = asset_trial_kit.load_frozen(args.inputs)
    return asset_trial_kit.preflight(
        environment=environment,
        lock=frozen["lock"],
        host_command=_host(args),
        tarball=args.tarball,
        freeze=frozen["freeze"],
    )


_TRIAL_COMMANDS = ("trial-preflight", "trial-install", "trial", "trial-run")


def _trial(args) -> int:
    """#234: the early-trial kit commands (spec 0.65 section 12.6.5).

    Output paths are resolved once (macOS ``/tmp`` is a symlink), so the
    no-follow write fence applies to the real directories.
    """
    if args.command not in _TRIAL_COMMANDS:
        # A programming error, not an operator input: never fall through to trial-run.
        raise ValueError(f"trial_command: {args.command}")
    try:
        if args.command == "trial-preflight":
            record = _trial_preflight(args, asset_trial_kit.observed_environment())
            atomic_bytes(asset_trial_kit.real_path(args.output), json_bytes(record))
            print(json.dumps(dict(passed=record["passed"], checks=record["checks"])))
            return 0 if record["passed"] else 3
        if args.command == "trial-install":
            record = asset_trial_kit.install_plugin(_host(args), args.tarball, args.work)
            atomic_bytes(asset_trial_kit.real_path(args.output), json_bytes(record))
            print(json.dumps(dict(passed=record["passed"])))
            return 0 if record["passed"] else 3
        if args.command == "trial":
            work = asset_trial_kit.real_path(args.work)
            work.mkdir(mode=0o700, parents=True, exist_ok=False)
            environment = asset_trial_kit.observed_environment()
            preflight = _trial_preflight(args, environment)
            atomic_bytes(work / "preflight.json", json_bytes(preflight))
            install = None
            # A provisional trial stops at a failed preflight; nothing is installed.
            if preflight["passed"] or args.qualification == "fixture":
                install = asset_trial_kit.install_plugin(
                    _host(args), args.tarball, work / "install"
                )
            tarball = args.tarball
        elif args.command == "trial-run":
            preflight, _ = read_json_path(args.preflight)
            # Offline bundling may run elsewhere: the report keeps the target
            # machine's environment as the preflight recorded it.
            environment = asset_trial_kit.preflight_environment(preflight)
            install = read_json_path(args.install)[0] if args.install else None
            tarball = args.tarball
    except (WholeTaskBlocked, WholeTaskRejected, AssetCorpusError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", str(exc))
        blocked = isinstance(exc, WholeTaskBlocked)
        status = "blocked" if blocked else "rejected"
        print(json.dumps(dict(status=status, reason=reason)), file=sys.stderr)
        return 3 if blocked else 2
    stop = []
    previous = signal.signal(signal.SIGINT, lambda *_: stop.append(True))
    try:
        code, result = asset_trial_kit.trial_run(
            inputs=args.inputs,
            observations=args.observations,
            bundle=args.bundle,
            resolver=args.resolver,
            qualification=args.qualification,
            environment=environment,
            preflight_record=preflight,
            install_record=install,
            tarball=tarball,
            ask=input if args.interactive else None,
            interrupted=lambda: bool(stop),
        )
    finally:
        signal.signal(signal.SIGINT, previous)
    print(json.dumps({k: result.get(k) for k in ("status", "gate", "reason", "bundle")}))
    return code


def _trial_parsers(sub) -> None:
    def host(parser):
        parser.add_argument(
            "host", nargs=argparse.REMAINDER, help="-- then the OpenClaw command, e.g. openclaw"
        )

    def run(parser):
        parser.add_argument("--inputs", type=Path, required=True, help="frozen input manifest")
        parser.add_argument("--observations", type=Path, required=True)
        parser.add_argument("--bundle", type=Path, required=True, help="new output directory")
        parser.add_argument("--resolver", type=Path)
        parser.add_argument(
            "--qualification", choices=asset_trial_kit.QUALIFICATIONS, required=True
        )
        parser.add_argument("--interactive", action="store_true")

    preflight = sub.add_parser("trial-preflight", help="#234 target machine/host/release check")
    preflight.add_argument("--inputs", type=Path, required=True)
    preflight.add_argument("--tarball", type=Path, required=True)
    preflight.add_argument("--output", type=Path, required=True)
    host(preflight)
    install = sub.add_parser("trial-install", help="install the packaged plugin in the host")
    install.add_argument("--tarball", type=Path, required=True)
    install.add_argument("--work", type=Path, required=True)
    install.add_argument("--output", type=Path, required=True)
    host(install)
    trial_run = sub.add_parser("trial-run", help="run, bundle and grade the early trial")
    run(trial_run)
    trial_run.add_argument("--preflight", type=Path, required=True)
    trial_run.add_argument("--install", type=Path)
    trial_run.add_argument("--tarball", type=Path)
    trial = sub.add_parser("trial", help="preflight, install, run, bundle and grade")
    run(trial)
    trial.add_argument("--tarball", type=Path, required=True)
    trial.add_argument("--work", type=Path, required=True, help="new private work directory")
    host(trial)


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
    _trial_parsers(sub)
    add_run_kit_commands(sub)
    args = parser.parse_args(argv)
    if args.command.startswith("trial"):
        return _trial(args)
    if args.command in RUN_KIT_COMMANDS:
        return run_kit(args)
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
