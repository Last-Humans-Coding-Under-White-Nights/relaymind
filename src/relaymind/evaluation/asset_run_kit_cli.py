"""Run-kit commands of ``scripts/asset_whole_task.py`` (#236, #237).

Spec 0.53 section 12.6.1 and 0.65 section 12.6.5 (RM16, RM40–RM44; AC31–AC36):
blind label templates, the adjudication worksheet and label freeze;
preregistration checks for host tasks and adaptation episodes; the resumable
host runner over an operator executor program; and external cost capture from
RelayMind trace events. Exit codes follow the grading commands: 0 done, 2
rejected input, 3 blocked or incomplete (an interrupted run). A first SIGINT or
SIGTERM to ``run-host`` stops before the next row; a second stops at once,
killing the executor's process group and writing no report. Nothing here
grades acceptance; ``grade-host`` and ``grade-adaptation`` do, offline.
"""

import argparse
import json
import signal
import sys
from pathlib import Path

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_benchmark_io import (
    CONFIG_BYTES,
    atomic_bytes,
    json_bytes,
    read_json_path,
)
from relaymind.evaluation.asset_corpus_io import (
    DOCUMENT_BYTES,
    AssetCorpusError,
    check_strict_value,
    read_local,
    strict_loads,
)
from relaymind.evaluation.asset_host_runner import (
    RUN_QUALIFICATIONS,
    TOKEN_RELATIONSHIPS,
    attempts_from_trace,
    command_executor,
    host_preregistration,
    run_host_plan,
)

COMMANDS = (
    "label-template",
    "label-worksheet",
    "label-freeze",
    "preregister-host",
    "preregister-episodes",
    "run-host",
    "capture-costs",
)


def add_commands(sub) -> None:
    template = sub.add_parser("label-template", help="one annotator's blind label template")
    worksheet = sub.add_parser("label-worksheet", help="adjudication worksheet from raw judgments")
    freeze = sub.add_parser("label-freeze", help="assemble and validate AssetLabelsV1")
    for parser in (template, worksheet, freeze):
        parser.add_argument("--catalog", type=Path, required=True)
        parser.add_argument("--cases", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument(
            "--labelling",
            choices=("independent", "owner_machine", "machine_dual_owner_approved"),
            default="independent",
            help="spec 12.6.1 independent or a 12.6.6 owner-run labelling profile",
        )
    template.add_argument("--annotator", required=True)
    for parser in (worksheet, freeze):
        parser.add_argument("--judgments", type=Path, nargs="+", required=True)
    freeze.add_argument("--worksheet", type=Path, required=True)

    host = sub.add_parser("preregister-host", help="check and bind a frozen host task set")
    host.add_argument("--tasks", type=Path, required=True)
    host.add_argument("--freeze", type=Path, required=True)
    host.add_argument("--output", type=Path, required=True)
    episodes = sub.add_parser("preregister-episodes", help="check and bind frozen episodes")
    episodes.add_argument("--episodes", type=Path, required=True)
    episodes.add_argument("--freeze", type=Path, required=True)
    episodes.add_argument("--lock", type=Path, help="the frozen corpus lock")
    episodes.add_argument("--evidence-root", type=Path, help="the corpus evidence root")
    episodes.add_argument("--output", type=Path, required=True)

    run = sub.add_parser(
        "run-host",
        help="execute the frozen plan through an executor program, resumably",
        description="Everything after -- is the executor program and its arguments.",
    )
    run.add_argument("--tasks", type=Path, required=True)
    run.add_argument("--freeze", type=Path, required=True)
    run.add_argument("--journal", type=Path, required=True)
    run.add_argument("--inputs", type=Path, required=True, help="companion input manifest")
    run.add_argument("--environment", type=Path, required=True)
    run.add_argument("--artifacts", type=Path, help="JSON array of ArtifactIdentity")
    run.add_argument("--qualification", choices=RUN_QUALIFICATIONS, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--executor-timeout",
        type=float,
        help="seconds before a hung executor program is killed (harness bound, not a budget)",
    )
    run.add_argument("executor", nargs=argparse.REMAINDER)

    costs = sub.add_parser("capture-costs", help="HostAttempts from RelayMind trace events")
    costs.add_argument("--events", type=Path, required=True, help="JSON array of TraceEvent")
    costs.add_argument("--provider", required=True)
    costs.add_argument("--attempt-prefix", required=True)
    costs.add_argument("--evidence-id", required=True, help="registry ID of the events export")
    costs.add_argument(
        "--token-relationship",
        choices=TOKEN_RELATIONSHIPS,
        default="unknown",
    )
    costs.add_argument("--relationship-evidence", help="registry ID evidencing the relationship")
    costs.add_argument("--first-ordinal", type=int, default=1)
    costs.add_argument("--output", type=Path, required=True)


def _read(path: Path) -> dict:
    return read_json_path(path, DOCUMENT_BYTES)[0]


def _read_array(path: Path, limit: int, reason: str) -> list:
    """A bounded strict JSON array, under the same rules as every other input."""
    path = Path(path).absolute()
    value = strict_loads(read_local(path.parent, path.name, limit))
    if not isinstance(value, list):
        raise ValueError(reason)
    check_strict_value(value)
    return value


def _labels(args) -> dict:
    from relaymind.evaluation import asset_labelling

    catalog, cases = _read(args.catalog), _read(args.cases)
    labelling = args.labelling
    if args.command == "label-template":
        return asset_labelling.label_template(catalog, cases, args.annotator, labelling=labelling)
    judgments = asset_labelling.raw_judgments(
        catalog, cases, [_read(path) for path in args.judgments], labelling=labelling
    )
    if args.command == "label-worksheet":
        return asset_labelling.adjudication_worksheet(
            catalog, cases, judgments, labelling=labelling
        )
    return asset_labelling.freeze_labels(
        catalog, cases, judgments, _read(args.worksheet), labelling=labelling
    )


def _episodes(args) -> dict:
    from relaymind.evaluation.asset_corpus import load_asset_release
    from relaymind.evaluation.asset_episode_export import episode_preregistration

    release = None
    if args.evidence_root is not None and args.lock is None:
        raise ValueError("lock")
    if args.lock is not None:
        if args.evidence_root is None:
            raise ValueError("evidence_root")
        release = load_asset_release(
            args.lock.absolute(), evidence_root=args.evidence_root.absolute(), related_roots={}
        )
    return episode_preregistration(_read(args.episodes), _read(args.freeze), release)


def _run_host(args) -> tuple[dict, int]:
    executor = list(args.executor)
    if executor[:1] == ["--"]:
        executor = executor[1:]
    if not executor:
        raise ValueError("executor")
    tasks, freeze = _read(args.tasks), _read(args.freeze)
    manifest, payload = read_json_path(args.inputs)
    artifacts = _read_array(args.artifacts, CONFIG_BYTES, "artifacts") if args.artifacts else []
    stop = False

    def interrupt(signum, frame):
        # The first signal stops before the next row; a second one stops at
        # once, even inside a hung executor (its started row is kept).
        nonlocal stop
        if stop:
            raise KeyboardInterrupt
        stop = True

    prior = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            prior[sig] = signal.signal(sig, interrupt)
        report = run_host_plan(
            tasks,
            freeze,
            command_executor(executor, tasks, timeout=args.executor_timeout),
            args.journal,
            environment=_read(args.environment),
            artifacts=artifacts,
            qualification=args.qualification,
            input_manifest=dict(
                path=args.inputs.name, digest=canonical_digest(manifest), bytes=len(payload)
            ),
            interrupted=lambda: stop,
        )
    finally:
        for sig, handler in prior.items():
            signal.signal(sig, handler)
    incomplete = any(
        r["status"] == "not_run" and r["reason"] == "interrupted" for r in report["rows"]
    )
    return report, 3 if incomplete else 0


def _costs(args) -> dict:
    events = _read_array(args.events, DOCUMENT_BYTES, "events")
    return dict(
        attempts=attempts_from_trace(
            events,
            provider=args.provider,
            attempt_prefix=args.attempt_prefix,
            evidence_id=args.evidence_id,
            token_relationship=args.token_relationship,
            relationship_evidence_id=args.relationship_evidence,
            first_ordinal=args.first_ordinal,
        )
    )


def run(args) -> int:
    """Run one run-kit command; print a fixed safe status line."""
    from relaymind.evaluation.asset_run_journal import JournalError
    from relaymind.evaluation.asset_whole_task import WholeTaskBlocked, WholeTaskRejected

    code = 0
    try:
        if args.command.startswith("label-"):
            result = _labels(args)
        elif args.command == "preregister-host":
            result = host_preregistration(_read(args.tasks), _read(args.freeze))
        elif args.command == "preregister-episodes":
            result = _episodes(args)
        elif args.command == "run-host":
            result, code = _run_host(args)
        else:
            result = _costs(args)
        atomic_bytes(args.output, json_bytes(result))
    except WholeTaskBlocked as exc:
        print(json.dumps(dict(status="blocked", reason=exc.reason)), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        # Stopped inside a row: nothing is written; the journal keeps it started.
        print(json.dumps(dict(status="interrupted")), file=sys.stderr)
        return 3
    except (
        WholeTaskRejected,
        JournalError,
        AssetCorpusError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
    ) as exc:
        reason = str(exc) if str(exc).isidentifier() else "invalid_input"
        print(json.dumps(dict(status="rejected", reason=reason)), file=sys.stderr)
        return 2
    print(json.dumps(dict(status="incomplete" if code else "written", output=str(args.output))))
    return code
