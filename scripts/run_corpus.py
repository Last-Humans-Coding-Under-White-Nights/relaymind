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

Usage:
    python scripts/run_corpus.py --out docs/evidence/<date>-corpus-<machine>.json \\
        [--modes auto local_only external_only] [--cases ID ...] [--languages en zh]
"""

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_acceptance as harness  # noqa: E402

from relaymind.evaluation.corpus import (  # noqa: E402
    MODES,
    Case,
    Corpus,
    check_lock,
    load_corpus,
)
from relaymind.evaluation.environment import machine_manifest, runtime_devices  # noqa: E402
from relaymind.evaluation.grading import RunGrade, call_matches, grade_run  # noqa: E402
from relaymind.evaluation.metrics import render_corpus_markdown, summarize_corpus  # noqa: E402

REPO_ROOT = harness.REPO_ROOT
CORPUS = REPO_ROOT / "evaluation" / "corpus.v1.json"
LOCK = REPO_ROOT / "evaluation" / "corpus.v1.lock.json"
SEED_DIR = REPO_ROOT / "tests" / "fixtures" / "sandbox"
# A run that does not come to rest in this time is cancelled and graded as a timeout.
RUN_TIMEOUT_SECONDS = 300.0
MAX_CLARIFICATIONS = 3


def load_frozen_corpus(path: Path = CORPUS) -> Corpus:
    """The corpus, refused when it no longer matches its frozen digest (section 12.2)."""
    corpus = load_corpus(path, seed_dir=SEED_DIR)
    check_lock(corpus, LOCK)
    return corpus


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
    modes: Sequence[str], *, external: harness.ExternalEndpoint | None
) -> tuple[tuple[str, ...], dict[str, str]]:
    """The modes that can run, and the reason each other mode was not run."""
    if external is not None and os.environ.get(harness.EXTERNAL_KEY_ENV):
        return tuple(modes), {}
    reason = (
        "no external endpoint was configured"
        if external is None
        else f"{harness.EXTERNAL_KEY_ENV} is not set, so the external endpoint cannot be called"
    )
    runnable = tuple(mode for mode in modes if mode == "local_only")
    return runnable, {mode: reason for mode in modes if mode != "local_only"}


def case_config(case: Case, *, external: harness.ExternalEndpoint | None = None, **options) -> str:
    """The operator file for one case: its seed and permissions, checked-in skills, cache off."""
    return harness.gateway_config(
        **options,
        external=external,
        modes=MODES if external is not None else ("auto", "local_only"),
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
    clarifications = 0
    view = None
    try:
        while True:
            view = await api.settle(run_id, timeout=max(deadline - time.monotonic(), 0.01))
            state = view["state"]
            if (
                state == "waiting_input"
                and case.clarification_answer
                and (clarifications < MAX_CLARIFICATIONS)
            ):
                clarifications += 1
                body = {
                    "expected_version": view["state_version"],
                    "input": case.clarification_answer,
                }
                answered = await api.resume(run_id, body)
                record["actions"].append(
                    {"answer": case.clarification_answer, "status": answered.status_code}
                )
                continue
            if state == "waiting_confirmation" and view["pending_approval"] is not None:
                pending = view["pending_approval"]
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
    )


def _run_one(
    case: Case,
    mode: str,
    *,
    runtime: harness.Runtime,
    local_url: str,
    external: harness.ExternalEndpoint | None,
    work: Path,
) -> tuple[RunGrade, dict[str, Any], list[dict[str, Any]]]:
    """One gateway start for one case and mode; returns the grade, the record, and the trace."""
    plan_dir = work / f"{case.case_id}-{mode}"
    plan_dir.mkdir()
    config = plan_dir / "relaymind.toml"
    config.write_text(
        case_config(
            case,
            work=plan_dir,
            manifest=runtime.manifest_path.resolve(),
            artifact=runtime.artifact,
            local_url=local_url,
            port=harness._free_port(),
            external=external,
        ),
        encoding="utf-8",
    )
    gateway = harness.GatewayProcess(config, plan_dir / "gateway.log", os.environ)
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
    events = harness._traces(plan_dir / "runs.sqlite3", [record["run_id"]])[record["run_id"]]
    return grade_record(case, mode, record, events, databases), record, events


def run_corpus(args: argparse.Namespace) -> dict[str, Any]:
    corpus = load_frozen_corpus()
    cases = select_cases(corpus, ids=args.cases, languages=args.languages)
    external = None
    if args.external_base_url:
        external = harness.ExternalEndpoint(
            args.external_base_url, args.external_model, args.external_context_limit
        )
    modes, not_run = select_modes(args.modes, external=external)
    runtime = harness.check_runtime(Path(args.manifest))
    application = harness.application_revision()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    work = Path(tempfile.mkdtemp(prefix="relaymind-corpus-"))
    llama, local_url, cold_start = runtime.launch(work / "llama-server.log")
    grades, records, traces = [], {}, {}
    try:
        for case in cases:
            for mode in modes:
                print(f"{case.case_id} {mode}", flush=True)
                grade, record, events = _run_one(
                    case, mode, runtime=runtime, local_url=local_url, external=external, work=work
                )
                grades.append(grade.to_json_object())
                records[f"{case.case_id}/{mode}"] = record
                traces[record["run_id"]] = events
                print(f"  {'pass' if grade.success else 'fail'} {grade.failures}", flush=True)
    finally:
        harness._stop(llama)
    routes = {case.case_id: case.expected_route for case in corpus.cases}
    summary = summarize_corpus(grades, expected_routes=routes)
    document = {
        "kind": "relaymind.correctness_corpus",
        "note": "Live runs of the frozen corpus (spec 0.28 section 12.2). Each case and mode ran "
        "in a new gateway from the case's seed. A failed grade is a finding, never dropped.",
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
        "runtime_banner": runtime.banner,
        "runtime_devices": runtime_devices(),
        "llama_server": {"argv": runtime.server_argv, "cold_start_seconds": round(cold_start, 3)},
        **runtime.model_record(),
        "external_endpoint": None
        if external is None
        else {
            "base_url": external.base_url,
            "model": external.model,
            "context_limit": external.context_limit,
            "region": args.external_region,
        },
        "summary": summary,
        "grades": grades,
        "records": records,
        "traces": traces,
    }
    out = Path(args.out)
    harness.write_json(out, document, work=work)
    report = render_corpus_markdown(summary, title=f"Correctness corpus: {out.stem}")
    out.with_suffix(".md").write_text(report, encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return document


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="the export file (JSON); the report is .md")
    parser.add_argument(
        "--manifest", default=os.fspath(harness.DEFAULT_MANIFEST.relative_to(REPO_ROOT))
    )
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--cases", nargs="+", help="run only these case IDs (a subset)")
    parser.add_argument("--languages", nargs="+", choices=("en", "zh"))
    parser.add_argument("--external-base-url")
    parser.add_argument("--external-model")
    parser.add_argument("--external-context-limit", type=int)
    parser.add_argument("--external-region", help="the endpoint region, if known")
    args = parser.parse_args(argv)
    external_args = (args.external_base_url, args.external_model, args.external_context_limit)
    if any(external_args) and not all(external_args):
        parser.error(
            "--external-base-url, --external-model, and --external-context-limit go together"
        )
    document = run_corpus(args)
    print(render_corpus_markdown(document["summary"], title="Correctness corpus"))


if __name__ == "__main__":
    main()
