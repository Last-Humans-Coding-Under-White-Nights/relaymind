"""Correctness corpus runner: the frozen corpus in auto, external_only, and local_only (#32).

Spec 0.28 section 12.2. The runner checks the corpus against its frozen
digest, verifies the runtime and the pinned artifact, and starts
llama-server from the manifest (it reuses the live acceptance harness,
``scripts/live_acceptance.py``). Then, for every case and every mode, it
starts a new gateway with ``relaymind serve --config`` (section 14.1): a
new store, a new sandbox working copy from the case's seed, the case's
permissions, the checked-in skills, the routing cache off, and the
configured external endpoint. So every mode of a case starts from the
same fixture snapshot. It creates one native run, answers a
clarification with the case's answer, approves a confirmation only when
the case says approve and the pending call is acceptable (it denies
otherwise), reads the trace back from the store, and grades the run
(``relaymind.evaluation.grading``).

The export holds the corpus digest, the environment, every grade and
trace, and the summary with exact numerators and denominators per mode,
language, and class. A Markdown report is written next to it. Without
an external endpoint (``--external-base-url`` and friends, with the
credential in RELAYMIND_EXTERNAL_API_KEY) only local_only runs; auto and
external_only are recorded as not run.

Two options work offline on a stored export and call no model: --regrade EXPORT
recomputes grades into a distinct --out artifact, and --regrade-draft-pass (RM31, spec 0.31,
#115) grades the same traces as if the skill-stage draft had been published
as the answer, and writes a separate export labeled as an offline
counterfactual.

Usage:
    python scripts/run_corpus.py --out docs/evidence/<date>-corpus-<machine>.json \\
        [--modes auto local_only external_only] [--cases ID ...] [--languages en zh]
    python scripts/run_corpus.py --regrade-draft-pass <export>.json \\
        --out docs/evidence/<date>-corpus-draft-pass-<machine>.json
"""

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import evaluation_session as session  # noqa: E402
import live_acceptance as harness  # noqa: E402
from evaluation_config import (  # noqa: E402
    ConfigurationError,
    RunnerConfiguration,
    add_configuration_arguments,
    configuration_from_args,
    label_report,
)

from relaymind.evaluation.corpus import (  # noqa: E402
    CLASSES,
    MODES,
    Case,
    Corpus,
    check_lock,
    demo_skill_names,
    load_corpus,
    load_skills_sidecar,
)
from relaymind.evaluation.environment import (  # noqa: E402
    application_revision,
    checkout_environment,  # noqa: E402
    machine_manifest,
    runtime_devices,
)
from relaymind.evaluation.grading import (  # noqa: E402
    RunGrade,
    call_matches,
    grade_run,
    terminal_event,
)
from relaymind.evaluation.m7_metrics import enrich_metrics  # noqa: E402
from relaymind.evaluation.metrics import render_corpus_markdown, summarize_corpus  # noqa: E402
from relaymind.evaluation.prior import byte_digest  # noqa: E402
from relaymind.orchestrator.performance import (  # noqa: E402
    draft_in_language,
    holds_tool_call_json,
    non_default_answer_options,
)
from relaymind.tools.sandbox import MUTATING_DEMO_TOOLS  # noqa: E402

REPO_ROOT = harness.REPO_ROOT
CORPUS = REPO_ROOT / "evaluation" / "corpus.v1.json"
LOCK = REPO_ROOT / "evaluation" / "corpus.v1.lock.json"
SKILLS_SIDECAR = REPO_ROOT / "evaluation" / "corpus.v1.skills.json"
SEED_DIR = REPO_ROOT / "tests" / "fixtures" / "sandbox"
# A run that does not come to rest in this time is cancelled and graded as a timeout.
RUN_TIMEOUT_SECONDS = 300.0
# Resumes per run (answers and decisions); a refused resume ends the driving.
MAX_RESUMES = 4


def load_frozen_corpus(path: Path = CORPUS) -> Corpus:
    """The corpus, refused when it no longer matches its frozen digest (section 12.2)."""
    corpus = load_corpus(path, seed_dir=SEED_DIR)
    check_lock(corpus, LOCK)
    return corpus


def expected_skills(corpus: Corpus) -> dict[str, str | None]:
    """The expected skill of every case, read once from the checked sidecar (RM28)."""
    return load_skills_sidecar(
        SKILLS_SIDECAR, corpus, skill_names=demo_skill_names(REPO_ROOT / "skills")
    )


def select_cases(
    corpus: Corpus, *, ids: Sequence[str] | None, languages: Sequence[str] | None
) -> list[Case]:
    known = {case.case_id for case in corpus.cases}
    if ids and (unknown := sorted(set(ids) - known)):
        raise SystemExit(f"unknown case id(s): {', '.join(unknown)}")
    return [
        case
        for case in corpus.cases
        if (not ids or case.case_id in ids) and (not languages or case.language in languages)
    ]


def select_modes(
    modes: Sequence[str], *, external: harness.ExternalEndpoint | None, local_model: bool = True
) -> tuple[tuple[str, ...], dict[str, str]]:
    """The modes that can run, and the reason each other mode was not run."""
    not_run = {}
    if not local_model:
        not_run.update(
            {mode: "no local model is configured" for mode in modes if mode == "local_only"}
        )
    if external is None or not os.environ.get(harness.EXTERNAL_KEY_ENV):
        reason = (
            "no external endpoint was configured"
            if external is None
            else f"{harness.EXTERNAL_KEY_ENV} is not set, so the external endpoint cannot be called"
        )
        not_run.update({mode: reason for mode in modes if mode != "local_only"})
    return tuple(mode for mode in modes if mode not in not_run), not_run


def case_config(case: Case, *, external: harness.ExternalEndpoint | None = None, **options) -> str:
    """The operator file for one case: its seed and permissions, checked-in skills, cache off."""
    return harness.gateway_config(
        **options,
        external=external,
        modes=(
            ("auto", "external_only")
            if not options.get("local_model", True)
            else MODES
            if external is not None
            else ("auto", "local_only")
        ),
        seed=SEED_DIR / f"{case.fixture}.json",
        permissions=case.permissions,
    )


def _acceptable(case: Case, pending: Mapping[str, Any]) -> bool:
    return any(
        call_matches(p, pending["tool_name"], pending["arguments"]) for p in case.acceptable_calls
    )


async def run_case(api: harness.Api, case: Case, mode: str, *, timeout: float) -> dict[str, Any]:
    """Create one native run of the case and drive it to rest (spec 0.28 section 12.2)."""
    response = await api.start(case.prompt, mode=mode)
    response.raise_for_status()
    run_id = response.json()["run_id"]
    record: dict[str, Any] = {"run_id": run_id, "timed_out": False, "actions": []}
    deadline = time.monotonic() + timeout
    resumes = 0
    view = None
    try:
        while True:
            view = await api.settle(run_id, timeout=max(deadline - time.monotonic(), 0.01))
            state = view["state"]
            if resumes >= MAX_RESUMES:
                break
            if state == "waiting_input" and case.clarification_answer:
                resumes += 1
                body = {
                    "expected_version": view["state_version"],
                    "input": case.clarification_answer,
                }
                answered = await api.resume(run_id, body)
                record["actions"].append(
                    {"answer": case.clarification_answer, "status": answered.status_code}
                )
                if answered.status_code != 202:
                    break
                continue
            if state == "waiting_confirmation" and view["pending_approval"] is not None:
                pending = view["pending_approval"]
                resumes += 1
                approve = case.confirmation == "approve" and _acceptable(case, pending)
                decision = {
                    "expected_version": view["state_version"],
                    "approval_id": pending["approval_id"],
                    "token": pending["token"],
                    "approve": approve,
                }
                decided = await api.resume(run_id, decision)
                record["actions"].append(
                    {
                        "confirmation": "approve" if approve else "deny",
                        "tool_name": pending["tool_name"],
                        "arguments": pending["arguments"],
                        "status": decided.status_code,
                    }
                )
                if decided.status_code != 202:
                    break
                continue
            break
    except TimeoutError:
        record["timed_out"] = True
        view = await api.view(run_id)
    # A run left waiting (or still running after a timeout) is cancelled, so it rests.
    if view["state"] not in harness.TERMINAL:
        await api.cancel(run_id, view["state_version"])
        try:
            view = await api.settle(run_id, harness.TERMINAL, timeout=30)
        except TimeoutError:
            view = await api.view(run_id)
    record.update(
        state=view["state"], error_code=view["error_code"], reason_code=view["reason_code"]
    )
    return record


def grade_record(
    case: Case,
    mode: str,
    record: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    databases: Sequence[str] | None,
    *,
    local_model_configured: bool | None = None,
) -> RunGrade:
    return grade_run(
        case,
        mode,
        events,
        state=record["state"],
        databases=databases,
        run_id=record["run_id"],
        error_code=record["error_code"],
        reason_code=record["reason_code"],
        timed_out=record["timed_out"],
        local_model_configured=local_model_configured,
    )


def _run_one(
    case: Case,
    mode: str,
    *,
    runtime: harness.Runtime | None,
    local_url: str | None,
    configuration: RunnerConfiguration,
    external: harness.ExternalEndpoint | None,
    work: Path,
    prior=None,
) -> tuple[RunGrade, dict[str, Any], list[dict[str, Any]]]:
    """One gateway start for one case and mode; returns the grade, the record, and the trace."""
    plan_dir = work / f"{case.case_id}-{mode}"
    plan_dir.mkdir()
    config = plan_dir / "relaymind.toml"
    config.write_text(
        case_config(
            case,
            work=plan_dir,
            manifest=runtime.manifest_path.resolve() if runtime else None,
            artifact=runtime.artifact if runtime else None,
            **configuration.gateway_options(),
            local_url=local_url,
            port=harness._free_port(),
            external=external,
        ),
        encoding="utf-8",
    )
    prior_report = session.seed_store(plan_dir, prior)
    gateway = harness.GatewayProcess(
        config, plan_dir / "gateway.log", checkout_environment(REPO_ROOT, dict(os.environ))
    )
    gateway.start()
    api = harness.Api(gateway.output.url, gateway.output.token, sandbox_dir=plan_dir / "sandbox")

    async def drive():
        try:
            return await run_case(api, case, mode, timeout=RUN_TIMEOUT_SECONDS)
        finally:
            await api.aclose()

    try:
        record = asyncio.run(drive())
        databases = api.sandbox()["databases"]
    finally:
        gateway.stop()
    record["timing_prior"] = prior_report
    record["timing_snapshot"] = session.snapshot_store(plan_dir)
    events = harness._traces(plan_dir / "runs.sqlite3", [record["run_id"]])[record["run_id"]]
    return (
        grade_record(
            case,
            mode,
            record,
            events,
            databases,
            local_model_configured=configuration.local_model_configured,
        ),
        record,
        events,
    )


def run_corpus(args: argparse.Namespace) -> dict[str, Any]:
    configuration = configuration_from_args(args)
    corpus = load_frozen_corpus()
    skills = expected_skills(corpus)
    cases = select_cases(corpus, ids=args.cases, languages=args.languages)
    external = None
    if configuration.external is not None:
        external = harness.ExternalEndpoint(
            args.external_base_url,
            args.external_model,
            args.external_context_limit,
            request_options=configuration.external_request_options,
        )
    if not configuration.local_model_configured and (
        external is None or not os.environ.get(harness.EXTERNAL_KEY_ENV)
    ):
        raise ConfigurationError("--no-local-model requires an external endpoint and its API key")
    modes, not_run = select_modes(
        args.modes, external=external, local_model=configuration.local_model_configured
    )
    if external is not None and not os.environ.get(harness.EXTERNAL_KEY_ENV):
        # Without a usable endpoint the gateways are started without one, and the
        # configuration label names none.
        external = None
        configuration = replace(configuration, external=None)
    runtime = (
        harness.check_runtime(Path(args.manifest), extra_args=args.runtime_extra_args)
        if configuration.local_model_configured
        else None
    )
    partition, prior, application, checkpoint = session.prepare(
        args, REPO_ROOT, runtime, external, configuration
    )
    if (previous := session.existing_result(checkpoint, args.out)) is not None:
        return previous
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    work = (
        Path(tempfile.mkdtemp(prefix="relaymind-corpus-"))
        if checkpoint is None
        else session.durable_work(args, "corpus", None)
    )
    # The work dir goes on any exit, an interrupt included.
    with ExitStack() as cleanup:
        if checkpoint is None:
            cleanup.callback(shutil.rmtree, work, ignore_errors=True)
        llama, local_url, cold_start = (
            runtime.launch(work / "llama-server.log") if runtime else (None, None, None)
        )
        grades, records, traces = [], {}, {}
        try:
            for case in cases:
                for mode in modes:
                    identity = f"{case.case_id}/{mode}"
                    if checkpoint:
                        checkpoint.start(identity)
                    print(f"{case.case_id} {mode}", flush=True)
                    try:
                        grade, record, events = _run_one(
                            case,
                            mode,
                            runtime=runtime,
                            local_url=local_url,
                            configuration=configuration,
                            external=external,
                            work=work,
                            **({"prior": prior} if prior is not None else {}),
                        )
                    except Exception as exc:  # noqa: BLE001 - recorded and graded, never dropped
                        if checkpoint:
                            checkpoint.document["infrastructure_error"] = type(exc).__name__
                            checkpoint._save()
                            raise
                        record = {
                            "run_id": None,
                            "timed_out": False,
                            "state": "error",
                            "error_code": None,
                            "reason_code": None,
                            "runner_error": f"{type(exc).__name__}: {exc}",
                        }
                        events = []
                        grade = grade_record(
                            case,
                            mode,
                            record,
                            events,
                            None,
                            local_model_configured=configuration.local_model_configured,
                        )
                    grade_json = grade.to_json_object()
                    grade_json = enrich_grade(
                        grade_json,
                        case,
                        events,
                        skills[case.case_id],
                        configuration.to_json_object(),
                        modern=True,
                    )
                    grades.append(grade_json)
                    if checkpoint:
                        checkpoint.finish(
                            identity, {"grade": grade_json, "record": record, "events": events}
                        )
                    records[f"{case.case_id}/{mode}"] = record
                    if record["run_id"] is not None:
                        traces[record["run_id"]] = events
                    print(f"  {'pass' if grade.success else 'fail'} {grade.failures}", flush=True)
        finally:
            if llama is not None:
                harness._stop(llama)
        summary = _summary(grades, corpus)
        document = {
            "kind": "relaymind.correctness_corpus",
            "configuration": configuration.to_json_object(),
            "prior_configuration": partition,
            "timing_prior": None if prior is None else prior.provenance,
            "selection_sidecar_sha256": byte_digest(SKILLS_SIDECAR),
            "note": "Live runs of the frozen corpus (spec 0.28 section 12.2). Each case and mode "
            "ran in a new gateway from the case's seed. A failed grade is a finding, never "
            "dropped.",
            "command": " ".join(["python", "scripts/run_corpus.py", *sys.argv[1:]]),
            "corpus": {
                "file": "evaluation/corpus.v1.json",
                "version": corpus.document["version"],
                "sha256": corpus.digest,
                "chinese_review": corpus.document["chinese_review"],
            },
            "selection": {
                "cases": [case.case_id for case in cases],
                "full_corpus": len(cases) == len(corpus.cases),
                "modes": list(modes),
                "not_run": not_run,
            },
            "started_at_utc": started_at,
            "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "machine": machine_manifest(),
            "application": application,
            "runtime_banner": runtime.banner if runtime else None,
            "runtime_devices": runtime_devices() if runtime else None,
            "llama_server": runtime.server_record(cold_start) if runtime else None,
            **(
                runtime.model_record()
                if runtime
                else {
                    "model_id": None,
                    "manifest": None,
                    "model_file": None,
                    "model_sha256": None,
                    "quantization": None,
                }
            ),
            "external_endpoint": None
            if external is None
            else {
                "base_url": external.base_url,
                "model": external.model,
                "context_limit": external.context_limit,
                "region": configuration.external.region,
            },
            "summary": summary,
            "grades": grades,
            "records": records,
            "traces": traces,
        }
        out = Path(args.out)
        harness.write_json(out, document, work=work)
        report = render_corpus_markdown(summary, title=f"Correctness corpus: {out.stem}")
        report = label_report(report, document.get("configuration"))
        out.with_suffix(".md").write_text(report, encoding="utf-8")
        if checkpoint:
            checkpoint.complete({"export_sha256": byte_digest(out), "grades": len(grades)})
        return document


def _summary(grades: Sequence[Mapping[str, Any]], corpus: Corpus) -> dict[str, Any]:
    routes = {case.case_id: case.expected_route for case in corpus.cases}
    return summarize_corpus(grades, expected_routes=routes)


def enrich_grade(
    grade: dict,
    case: Case,
    events: Sequence[Mapping[str, Any]],
    expected_skill: str | None,
    configuration: dict,
    *,
    modern: bool,
) -> dict:
    grade["metrics"] = enrich_metrics(
        case,
        events,
        expected_skill=expected_skill,
        selector_chain=configuration.get("selection", {}).get("chain", []),
    )
    if modern and grade.get("local_completion") and grade.get("state") == "completed":
        grade["local_completion"] = grade["metrics"]["answer_owner"] == "local"
    grade["local_owner_evidence"] = "explicit" if modern else "legacy compatibility"
    return grade


def regrade(
    document: dict[str, Any], corpus: Corpus, *, source_sha256: str | None = None
) -> dict[str, Any]:
    """Recompute the grades and summary of an export from its stored traces (no model call).

    The sandbox state after each run is taken from the stored grade.
    The export records the revision that regraded it.
    """
    if document["corpus"]["sha256"] != corpus.digest:
        raise SystemExit("the export was made with another corpus digest; it cannot be regraded")
    skills = expected_skills(corpus)
    grades = []
    for old in document["grades"]:
        case, mode = corpus.case(old["case_id"]), old["mode"]
        record = document["records"][f"{case.case_id}/{mode}"]
        events = document["traces"].get(record["run_id"], [])
        grade = grade_record(
            case,
            mode,
            record,
            events,
            old["databases"],
            local_model_configured=document.get("configuration", {}).get("local_model_configured"),
        )
        grades.append(
            enrich_grade(
                grade.to_json_object(),
                case,
                events,
                skills[case.case_id],
                document.get("configuration", {}),
                modern="selection_sidecar_sha256" in document,
            )
        )
    return {
        **document,
        "grades": grades,
        "summary": _summary(grades, corpus),
        "analysis_history": [
            *document.get("analysis_history", []),
            *([document["regraded_with"]] if "regraded_with" in document else []),
        ],
        "regraded_with": {
            "application": harness.application_revision(),
            "source_sha256": source_sha256,
        },
    }


# -- the draft answer pass, regraded offline (RM31, spec 0.31 section 12.2, #115) ------------

DRAFT_PASS_LABEL = "offline counterfactual, not a measured run"
DRAFT_PASS_KIND = "relaymind.correctness_corpus.draft_pass_counterfactual"
# external_only never runs a local final generation.
DRAFT_PASS_MODES = ("auto", "local_only")
# The skill stage's generations: a text answer from either can be the draft.
DRAFT_PURPOSES = ("skill", "repair")
# Every code draft_block_reason returns, explained in the export (its "guards").
DRAFT_BLOCK_REASONS = {
    "no_draft": "no skill or repair generation gave text without calls before the final one",
    "answer_options": "the caller sent a non-default answer option (section 7.1)",
    "language_unknown": "the run's first route.selected decision names no language (en or zh)",
    "language": "the draft is not in the run's language (identifiers.has_cjk)",
    "tool_call_json": "the draft holds a tool-call tag or JSON naming a tool and arguments",
}


def run_language(events: Sequence[Mapping[str, Any]]) -> str | None:
    """The run's language: the first route.selected decision (RM02, section 8)."""
    for event in events:
        if event["event_type"] == "route.selected":
            language = (event["payload"].get("decision") or {}).get("language")
            return language if isinstance(language, str) else None
    return None


def draft_block_reason(
    text: str, language: str | None, answer_options: Mapping[str, Any] | None
) -> str | None:
    """Why the gateway would not publish a draft (RM31), or None when it would.

    The final generation then runs as before. The guards are: a caller
    answer option that is not its API default (section 7.1), the run's
    language (a Chinese run needs Chinese text, an English run none, as
    the router reads a message, identifiers.has_cjk), and tool-call JSON
    in the text. A run with no draft text, or with no
    language in its first decision, is blocked too (no_draft,
    language_unknown).
    """
    if not text.strip():
        return "no_draft"
    if non_default_answer_options(answer_options or {}):
        return "answer_options"
    if language not in ("en", "zh"):
        return "language_unknown"
    if not draft_in_language(text, language):
        return "language"
    if holds_tool_call_json(text):
        return "tool_call_json"
    return None


def find_draft(events: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The draft: the last skill or repair generation without calls or error before the final.

    The final generation starts only after a text answer met the skill's
    completion predicate, so that text is the draft (spec 0.31, RM31).
    """
    draft = None
    for index, event in enumerate(events):
        kind, payload = event["event_type"], event["payload"]
        if kind == "model.started" and payload["purpose"] == "final":
            break
        if (
            kind == "model.finished"
            and payload["purpose"] in DRAFT_PURPOSES
            and not payload.get("proposed_calls")
            and not payload.get("error")
        ):
            draft = {
                "index": index,
                "call_id": payload["call_id"],
                "purpose": payload["purpose"],
                "model": payload["model"],
                "text": payload.get("text") or "",
                "elapsed_ms": event.get("elapsed_ms"),
            }
    return draft


def _ran_final(events: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        e["event_type"] == "model.started" and e["payload"]["purpose"] == "final" for e in events
    )


def _completed_locally(events: Sequence[Mapping[str, Any]]) -> bool:
    terminal = terminal_event(events)
    return (
        terminal is not None
        and terminal["event_type"] == "run.completed"
        and terminal["payload"].get("external_calls") == 0
    )


def _waits_before(events: Sequence[Mapping[str, Any]], draft: Mapping[str, Any]) -> int:
    """The measured waiting_ms less the clarification waits recorded after the draft."""
    terminal = terminal_event(events)
    waiting = int(terminal["payload"].get("waiting_ms", 0)) if terminal else 0
    later = sum(
        int(e["payload"].get("waited_ms") or 0)
        for e in events[draft["index"] + 1 :]
        if e["event_type"] == "clarification.answered"
    )
    return max(waiting - later, 0)


def draft_trace(events: Sequence[Mapping[str, Any]], draft: Mapping[str, Any]) -> list[dict]:
    """The counterfactual trace: the events up to the draft, then the draft as the local answer.

    The run completes with the draft's model as the final answer model and
    no external call; the generations and tool calls after the draft are
    not part of it. run.completed has every field section 8 requires
    (RM22): the model calls up to the draft, waiting_ms the measured waits
    before the draft, active_ms the draft's elapsed time less those waits,
    and answer_pass_applied draft (RM31, spec 0.32).
    """
    kept = [dict(e) for e in events[: draft["index"] + 1]]
    last = kept[-1]
    base = {
        k: last[k] for k in ("schema_version", "run_id", "timestamp", "elapsed_ms") if k in last
    }
    seq = last.get("seq", len(kept))
    waiting = _waits_before(events, draft)
    calls = [
        {
            "actor": e["actor"],
            "call_id": e["payload"]["call_id"],
            "model": e["payload"]["model"],
            "purpose": e["payload"]["purpose"],
        }
        for e in kept
        if e["event_type"] == "model.started"
    ]
    kept.append(
        {
            **base,
            "seq": seq + 1,
            "event_type": "answer.delta",
            "actor": "local",
            "payload": {"model": draft["model"], "text": draft["text"]},
        }
    )
    kept.append(
        {
            **base,
            "seq": seq + 2,
            "event_type": "run.completed",
            "actor": "gateway",
            "payload": {
                "answer": draft["text"],
                "final_answer_model": draft["model"],
                "external_calls": 0,
                "model_calls": calls,
                "active_ms": max(int(draft.get("elapsed_ms") or 0) - waiting, 0),
                "waiting_ms": waiting,
                "answer_pass_applied": "draft",
                "counterfactual": DRAFT_PASS_LABEL,
            },
        }
    )
    return kept


def _outcome(grade: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: grade[key]
        for key in ("state", "success", "local_completion", "local_calls", "external_calls")
    }


def _draft_entry(
    case: Case,
    old: Mapping[str, Any],
    record: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One run's counterfactual: its entry for the report and the grade to use."""
    draft = find_draft(events)
    accepted = next((e for e in events if e["event_type"] == "run.accepted"), None)
    options = (accepted["payload"].get("answer_options") if accepted else None) or {}
    language = run_language(events)
    reason = draft_block_reason(draft["text"] if draft else "", language, options)
    outcome = "blocked" if reason else "published"
    if (
        draft is not None
        and reason is None
        and any(
            e["event_type"] == "tool.started" and e["payload"]["tool_name"] in MUTATING_DEMO_TOOLS
            for e in events[draft["index"] + 1 :]
        )
    ):
        # The stored sandbox state includes a change that the draft would have prevented.
        outcome, reason = "not_gradable", "mutation_after_draft"
    entry: dict[str, Any] = {
        "case_id": case.case_id,
        "mode": old["mode"],
        "language": case.language,
        "case_class": case.case_class,
        "run_id": record["run_id"],
        "run_language": language,
        "measured_completed_locally": _completed_locally(events),
        "outcome": outcome,
        "reason": reason,
        "draft": draft["text"] if draft else None,
        "draft_call_id": draft["call_id"] if draft else None,
        "draft_ready_ms": draft["elapsed_ms"] if draft else None,
        "measured": _outcome(old),
    }
    if outcome != "published":
        entry["counterfactual"] = entry["measured"]
        return entry, dict(old)
    counterfactual = {
        **record,
        "state": "completed",
        "error_code": None,
        "reason_code": None,
        "timed_out": False,
    }
    grade = grade_record(
        case, old["mode"], counterfactual, draft_trace(events, draft), old["databases"]
    ).to_json_object()
    entry["counterfactual"] = _outcome(grade)
    return entry, grade


def regrade_draft_pass(document: dict[str, Any], corpus: Corpus) -> dict[str, Any]:
    """Grade the stored traces as if the skill-stage draft had been published (RM31, #115).

    For each auto and local_only run in which a final generation ran: take
    the draft, apply the RM31 guards, and, when it would be published,
    grade a counterfactual trace that ends with the draft as the local
    answer. A blocked run keeps its measured grade, because the final
    generation then runs as before. A run that changed the sandbox after
    the draft keeps its measured grade too and is marked not gradable,
    because its stored sandbox state holds a change that the draft would
    have prevented. No model is called, and the document is not changed;
    the result is a separate record, labeled as an offline
    counterfactual. Its runs say whether the measured run completed
    locally, so the runs the final pass broke (finding F1 of the
    2026-10-23 analysis) are apart from the runs it did not.
    """
    if document["corpus"]["sha256"] != corpus.digest:
        raise SystemExit("the export was made with another corpus digest; it cannot be regraded")
    grades, runs = [], []
    for old in document["grades"]:
        case, mode = corpus.case(old["case_id"]), old["mode"]
        record = document["records"][f"{case.case_id}/{mode}"]
        events = document["traces"].get(record["run_id"], [])
        if mode not in DRAFT_PASS_MODES or not _ran_final(events):
            grades.append(old)
            continue
        entry, grade = _draft_entry(case, old, record, events)
        runs.append(entry)
        grades.append(grade)
    return {
        "kind": DRAFT_PASS_KIND,
        "label": DRAFT_PASS_LABEL,
        "note": "The stored traces of a measured corpus run, graded as if the skill-stage draft "
        "had been published as the answer (RM31). No model was called and the source export is "
        "not changed. A blocked run keeps its measured grade. This is not a measured run: the "
        "draft pass was never run, so the figures are a prediction of it.",
        "guards": dict(DRAFT_BLOCK_REASONS),
        "source": {
            key: document[key]
            for key in (
                "corpus",
                "application",
                "regraded_with",
                "started_at_utc",
                "finished_at_utc",
                "machine",
                "model_id",
                "quantization",
                "external_endpoint",
                "configuration",
            )
            if key in document
        },
        "regraded_with": {"application": application_revision(REPO_ROOT)},
        "runs": runs,
        "table": draft_pass_table(runs),
        "measured_summary": _summary(document["grades"], corpus),
        "counterfactual_summary": _summary(grades, corpus),
        "grades": grades,
    }


def draft_pass_table(runs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per mode, language, class, and whether the measured run completed locally: the outcomes.

    Each row counts the runs with a final generation, the drafts
    published and blocked (by reason), the runs not gradable, how many
    published drafts pass grading, how many measured runs passed, and the
    runs that the draft turned from failing to passing (gained) or from
    passing to failing (lost).
    """
    rows: dict[tuple, dict[str, Any]] = {}
    for run in runs:
        key = (run["mode"], run["language"], run["case_class"], run["measured_completed_locally"])
        row = rows.setdefault(
            key,
            {
                "mode": run["mode"],
                "language": run["language"],
                "case_class": run["case_class"],
                "measured_completed_locally": run["measured_completed_locally"],
                "runs": 0,
                "published": 0,
                "published_pass": 0,
                "gained": 0,
                "lost": 0,
                "blocked": {},
                "not_gradable": 0,
                "measured_pass": 0,
            },
        )
        measured, counterfactual = (
            bool(run["measured"]["success"]),
            bool(run["counterfactual"]["success"]),
        )
        row["runs"] += 1
        row["measured_pass"] += measured
        if run["outcome"] == "published":
            row["published"] += 1
            row["published_pass"] += counterfactual
            # A run that fails measured and passes with the draft, and the reverse.
            row["gained"] += counterfactual > measured
            row["lost"] += measured > counterfactual
        elif run["outcome"] == "blocked":
            row["blocked"][run["reason"]] = row["blocked"].get(run["reason"], 0) + 1
        else:
            row["not_gradable"] += 1
    return sorted(
        rows.values(),
        key=lambda r: (
            MODES.index(r["mode"]),
            r["language"],
            CLASSES.index(r["case_class"]),
            r["measured_completed_locally"],
        ),
    )


def _fraction(value: Mapping[str, Any] | None) -> str:
    return "-" if value is None else f"{value['numerator']}/{value['denominator']}"


def render_draft_pass_markdown(result: Mapping[str, Any]) -> str:
    """The report of a draft-pass counterfactual: published and blocked, then the two summaries."""
    source = result["source"]
    application = (source.get("application") or {}).get("head", "unknown")
    lines = [
        f"# Draft answer pass: {DRAFT_PASS_LABEL}",
        "",
        result["note"],
        "",
        f"Source export: corpus digest `{source['corpus']['sha256']}`, "
        f"application `{application}`.",
        "",
        "## Drafts published and blocked",
        "",
        "Runs of auto and local_only in which a final generation ran. A measured run that "
        "did not complete locally is the case of finding F1; one that completed locally is "
        "checked so that the draft does not lower success.",
        "",
        "| mode | language | class | measured run | runs | published | published and passing "
        "| blocked | reasons | not gradable | measured passing | gained | lost |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in result["table"]:
        reasons = "; ".join(f"{k} {v}" for k, v in sorted(row["blocked"].items())) or "-"
        measured = "completed locally" if row["measured_completed_locally"] else "not local"
        lines.append(
            f"| {row['mode']} | {row['language']} | {row['case_class']} | {measured} "
            f"| {row['runs']} | {row['published']} | {row['published_pass']}/{row['published']} "
            f"| {sum(row['blocked'].values())} | {reasons} | {row['not_gradable']} "
            f"| {row['measured_pass']}/{row['runs']} | {row['gained']} | {row['lost']} |"
        )
    lines += [
        "",
        "## Task success and local completion: measured and counterfactual",
        "",
        "| mode | language | class | success measured | success counterfactual "
        "| local completion measured | local completion counterfactual "
        "| local calls | external calls |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    before, after = result["measured_summary"]["modes"], result["counterfactual_summary"]["modes"]
    for mode, languages in before.items():
        if mode not in DRAFT_PASS_MODES:
            continue
        for language, data in languages.items():
            others = {**after[mode][language]["classes"], "all": after[mode][language]["all"]}
            for name, block in [*data["classes"].items(), ("all", data["all"])]:
                other = others[name]
                calls = block["provider_calls"], other["provider_calls"]
                lines.append(
                    f"| {mode} | {language} | {name} "
                    f"| {_fraction(block['success'])} | {_fraction(other['success'])} "
                    f"| {_fraction(block.get('local_completion'))} "
                    f"| {_fraction(other.get('local_completion'))} "
                    f"| {calls[0]['local']} to {calls[1]['local']} "
                    f"| {calls[0]['external']} to {calls[1]['external']} |"
                )
    lines.append("")
    return label_report("\n".join(lines), source.get("configuration"))


def argument_parser() -> argparse.ArgumentParser:
    """The same CLI schema is used for invocation and checkpoint reconciliation."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="the export file (JSON); the report is .md")
    parser.add_argument(
        "--manifest", default=os.fspath(harness.DEFAULT_MANIFEST.relative_to(REPO_ROOT))
    )
    harness.add_runtime_extra_args(parser)
    add_configuration_arguments(parser)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--cases", nargs="+", help="run only these case IDs (a subset)")
    parser.add_argument("--languages", nargs="+", choices=("en", "zh"))
    parser.add_argument(
        "--regrade",
        metavar="EXPORT",
        default=False,
        help="regrade immutable EXPORT into a distinct --out file from its stored traces",
    )
    parser.add_argument(
        "--regrade-draft-pass",
        metavar="EXPORT",
        help="grade the stored traces of EXPORT as if the skill-stage draft had been published "
        "(RM31): an offline counterfactual, written to --out and its .md, never to EXPORT",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = argument_parser()
    args = parser.parse_args(argv)
    if (args.regrade or args.regrade_draft_pass) and (
        args.performance
        or args.budgets
        or args.selection_chain
        or args.no_local_model
        or args.placement
        or args.timing_samples
        or args.checkpoint
    ):
        parser.error("offline regrading keeps the source configuration; omit configuration flags")
    if args.regrade_draft_pass:
        if args.regrade:
            parser.error("--regrade and --regrade-draft-pass do not go together")
        source, out = Path(args.regrade_draft_pass), Path(args.out)
        if out.resolve() == source.resolve():
            parser.error("--out must differ from the source export: the original is never changed")
        result = regrade_draft_pass(json.loads(source.read_text("utf-8")), load_frozen_corpus())
        harness.write_json(out, result)
        out.with_suffix(".md").write_text(render_draft_pass_markdown(result), encoding="utf-8")
        return
    if args.regrade:
        source, out = Path(args.regrade), Path(args.out)
        if source.resolve() == out.resolve():
            parser.error("--out must differ from the source export: the original is never changed")
        document = regrade(
            json.loads(source.read_text("utf-8")),
            load_frozen_corpus(),
            source_sha256=byte_digest(source),
        )
        harness.write_json(out, document)
        report = render_corpus_markdown(
            document["summary"], title=f"Correctness corpus: {out.stem}"
        )
        report = label_report(report, document.get("configuration"))
        out.with_suffix(".md").write_text(report, encoding="utf-8")
        return
    # Only option checks raise ConfigurationError, before anything starts; any other
    # failure keeps its traceback and is never reported as a usage error.
    try:
        document = run_corpus(args)
    except ConfigurationError as exc:
        parser.error(str(exc))
    print(
        label_report(
            render_corpus_markdown(document["summary"], title="Correctness corpus"),
            document.get("configuration"),
        )
    )


if __name__ == "__main__":
    main()
