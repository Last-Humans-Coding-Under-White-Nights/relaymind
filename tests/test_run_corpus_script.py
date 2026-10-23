"""The correctness corpus runner, over model doubles (spec 0.28 section 12.2, #32; RM25).

scripts/run_corpus.py starts a new gateway with ``relaymind serve
--config`` for every case and mode (section 14.1), drives one native
run with the case's clarification answer and confirmation decision, and
grades it from the trace. These tests run the runner's case driver
against the app the launcher builds from the runner's own config file,
with scripted model doubles in place of the models (section 13).
"""

import argparse
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from gateway_helpers import (
    PAYMENTS_OK,
    clarify_turn,
    health_tail,
    route_turn,
    text_turn,
    tool_turn,
)
from helpers import ScriptedAdapter, StuckAdapter, load_script_module
from launcher_helpers import make_artifact
from relaymind.launcher import build_app, load_config

REPO = Path(__file__).resolve().parent.parent
DELETED = "Deleted the sandbox database demo-orders-db."


@pytest.fixture(scope="module")
def runner():
    return load_script_module("run_corpus")


@pytest.fixture(scope="module")
def corpus(runner):
    return runner.load_frozen_corpus()


def serve(runner, tmp_path, case, local, *, external_turns=None):
    """The launcher's app from the runner's config for a case; returns an Api.

    local is the local model double, or its scripted turns.
    """
    manifest, artifact = make_artifact(tmp_path)
    endpoint = runner.harness.ExternalEndpoint("https://api.example.invalid/v1", "model-x", 32768)
    path = tmp_path / "relaymind.toml"
    path.write_text(
        runner.case_config(
            case,
            work=tmp_path / "work",
            manifest=manifest,
            artifact=artifact,
            local_url="http://127.0.0.1:8033",
            port=8099,
            external=endpoint,
        ),
        "utf-8",
    )
    config = load_config(path)
    external = None
    if external_turns is not None:
        external = ScriptedAdapter(turns=external_turns, locality="external")
    launched = build_app(
        config,
        local_adapter=local if isinstance(local, ScriptedAdapter) else ScriptedAdapter(turns=local),
        external_adapter=external,
    )
    return runner.harness.Api(
        "http://127.0.0.1",
        launched.app.state.application_token,
        sandbox_dir=config.sandbox_dir,
        transport=httpx.ASGITransport(app=launched.app),
    ), config


def drive(runner, api, case, mode):
    async def go():
        try:
            record = await runner.run_case(api, case, mode, timeout=30)
            return record, await api.events(record["run_id"]), api.sandbox()["databases"]
        finally:
            await api.aclose()

    record, events, databases = asyncio.run(go())
    return runner.grade_record(case, mode, record, events, databases)


def test_the_config_carries_the_case_seed_and_permissions(runner, corpus, tmp_path):
    """Section 12.2: the case's fixture snapshot and expected permissions; cache off."""
    case = corpus.case("adversarial-03")
    _, config = serve(runner, tmp_path, case, [])
    assert (
        config.sandbox_seed == REPO / "tests" / "fixtures" / "sandbox" / "health-unreachable.json"
    )
    assert config.permissions == {"demo.read"}
    assert config.routing_cache is False
    assert {m.value for m in config.chat.allowed_modes} >= {"auto", "local_only", "external_only"}


def test_a_simple_case_completes_locally(runner, corpus, tmp_path):
    case = corpus.case("simple-01")
    turns = [route_turn("check-service-health"), *health_tail("payments-api", PAYMENTS_OK)]
    api, _ = serve(runner, tmp_path, case, turns)
    grade = drive(runner, api, case, "local_only")
    assert grade.success, grade.failures
    assert grade.local_completion is True
    assert grade.external_calls == 0


def test_a_wrong_service_is_graded_as_a_failure(runner, corpus, tmp_path):
    case = corpus.case("simple-01")
    staging = "staging-api is healthy (checked at 2026-10-20T02:58:00Z)."
    turns = [route_turn("check-service-health"), *health_tail("staging-api", staging)]
    api, _ = serve(runner, tmp_path, case, turns)
    grade = drive(runner, api, case, "local_only")
    assert grade.tool_correct is False and not grade.success


def test_an_ambiguous_case_is_resumed_with_the_case_answer(runner, corpus, tmp_path):
    case = corpus.case("ambiguous-04")
    turns = [
        clarify_turn("Which service do you mean?"),
        route_turn("check-service-health"),
        *health_tail("payments-api", PAYMENTS_OK),
    ]
    api, _ = serve(runner, tmp_path, case, turns)
    grade = drive(runner, api, case, "auto")
    assert grade.clarified and grade.success, grade.failures


def test_a_clarification_without_a_case_answer_is_cancelled(runner, corpus, tmp_path):
    case = corpus.case("simple-01")
    api, _ = serve(runner, tmp_path, case, [clarify_turn("Which service?")])
    grade = drive(runner, api, case, "local_only")
    assert grade.state == "cancelled" and not grade.success


def delete_turns(database="demo-orders-db", revision=3, answer=DELETED):
    return [
        route_turn("delete-demo-database"),
        tool_turn("list_demo_databases", call_id="call_list"),
        tool_turn(
            "delete_demo_database",
            call_id="call_delete",
            database_id=database,
            expected_revision=revision,
        ),
        text_turn(answer),
        text_turn(answer),
    ]


def test_a_mutation_case_is_approved_and_deletes_once(runner, corpus, tmp_path):
    case = corpus.case("mutation-01")
    api, _ = serve(runner, tmp_path, case, delete_turns())
    grade = drive(runner, api, case, "local_only")
    assert grade.success, grade.failures
    assert grade.confirmation_requested and grade.missed_confirmations == 0
    assert grade.databases == ["demo-analytics-db"]


def test_a_mutation_case_with_deny_keeps_the_database(runner, corpus, tmp_path):
    case = corpus.case("mutation-04")
    api, _ = serve(runner, tmp_path, case, delete_turns())
    grade = drive(runner, api, case, "local_only")
    assert grade.success, grade.failures
    assert grade.state == "cancelled"
    assert grade.databases == ["demo-analytics-db", "demo-orders-db"]


def test_a_wrong_delete_is_denied_even_when_the_case_approves(runner, corpus, tmp_path):
    """The runner approves only an acceptable call; a wrong revision is denied."""
    case = corpus.case("mutation-01")
    api, _ = serve(runner, tmp_path, case, delete_turns(revision=1))
    grade = drive(runner, api, case, "local_only")
    assert grade.state == "cancelled"
    assert grade.databases == ["demo-analytics-db", "demo-orders-db"]
    assert not grade.success and grade.tool_correct is False


def test_a_complex_case_in_external_only_calls_only_the_external_model(runner, corpus, tmp_path):
    case = corpus.case("complex-05")
    external = [
        tool_turn("get_recent_logs", service="payments-api", limit=3),
        text_turn("Observations: payments-api logs show a ledger-db timeout. Hypothesis: ..."),
    ]
    api, _ = serve(runner, tmp_path, case, [], external_turns=external)
    grade = drive(runner, api, case, "external_only")
    assert grade.success, grade.failures
    assert (grade.local_calls, grade.external_calls) == (0, 2)
    assert grade.escalation is None


def test_a_complex_case_in_local_only_fails_explicitly(runner, corpus, tmp_path):
    case = corpus.case("complex-05")
    api, _ = serve(runner, tmp_path, case, [route_turn("investigate-performance")])
    grade = drive(runner, api, case, "local_only")
    assert grade.state == "failed" and grade.explicit_failure
    assert not grade.success


def test_a_run_that_does_not_rest_is_a_timeout(runner, corpus, tmp_path):
    case = corpus.case("simple-01")
    api, _ = serve(runner, tmp_path, case, StuckAdapter())

    async def go():
        try:
            return await runner.run_case(api, case, "local_only", timeout=0.5)
        finally:
            await api.aclose()

    record = asyncio.run(go())
    assert record["timed_out"] is True
    assert record["state"] in ("cancelled", "routing")


def test_external_modes_are_not_run_without_an_endpoint(runner):
    modes, not_run = runner.select_modes(("auto", "external_only", "local_only"), external=None)
    assert modes == ("local_only",)
    assert set(not_run) == {"auto", "external_only"}
    assert all("external" in reason for reason in not_run.values())


def test_the_runner_refuses_a_changed_corpus(runner, tmp_path):
    from relaymind.evaluation.corpus import CorpusError

    changed = json.loads((REPO / "evaluation" / "corpus.v1.json").read_text("utf-8"))
    changed["cases"][0]["prompt"] = "Is payments-api up?"
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(changed), "utf-8")
    with pytest.raises(CorpusError):
        runner.load_frozen_corpus(path)


def test_cases_can_be_selected_and_the_selection_is_recorded(runner, corpus):
    selected = runner.select_cases(corpus, ids=["simple-01", "complex-02"], languages=None)
    assert [c.case_id for c in selected] == ["simple-01", "complex-02"]
    assert len(runner.select_cases(corpus, ids=None, languages=["en"])) == 40
    with pytest.raises(SystemExit):
        runner.select_cases(corpus, ids=["nope"], languages=None)


class RefusingApi:
    """An Api stand-in whose run waits for confirmation and refuses every resume."""

    def __init__(self):
        self.resumes = 0
        self.cancelled = False

    async def start(self, prompt, mode=None):
        return httpx.Response(202, json={"run_id": "run_1"}, request=httpx.Request("POST", "x"))

    def _view(self):
        state = "cancelled" if self.cancelled else "waiting_confirmation"
        pending = {
            "approval_id": "approval_1",
            "token": None,
            "tool_name": "delete_demo_database",
            "arguments": {"database_id": "demo-orders-db", "expected_revision": 3},
        }
        return {
            "state": state,
            "state_version": 4,
            "pending_approval": None if self.cancelled else pending,
            "error_code": None,
            "reason_code": "cancel_requested" if self.cancelled else None,
        }

    async def settle(self, run_id, states=None, timeout=None):
        return self._view()

    async def view(self, run_id):
        return self._view()

    async def resume(self, run_id, body):
        self.resumes += 1
        return httpx.Response(409)

    async def cancel(self, run_id, version):
        self.cancelled = True
        return httpx.Response(200)


def test_a_refused_decision_ends_the_driving(runner, corpus):
    """A resume that is refused is not retried forever; the run is cancelled."""
    api = RefusingApi()
    record = asyncio.run(runner.run_case(api, corpus.case("mutation-01"), "auto", timeout=5))
    assert api.resumes == 1
    assert record["state"] == "cancelled"
    assert record["actions"][0]["status"] == 409


def test_an_export_is_regraded_from_its_stored_traces(runner, corpus, tmp_path):
    """The grades of a recorded run can be recomputed from its traces, with no model call."""
    case = corpus.case("simple-01")
    turns = [route_turn("check-service-health"), *health_tail("payments-api", PAYMENTS_OK)]
    api, _ = serve(runner, tmp_path, case, turns)

    async def go():
        try:
            record = await runner.run_case(api, case, "local_only", timeout=30)
            return record, await api.events(record["run_id"]), api.sandbox()["databases"]
        finally:
            await api.aclose()

    record, events, databases = asyncio.run(go())
    stale = runner.grade_record(case, "local_only", record, events, databases).to_json_object()
    stale["success"] = False
    document = {
        "corpus": {"sha256": corpus.digest},
        "grades": [stale],
        "records": {"simple-01/local_only": record},
        "traces": {record["run_id"]: events},
    }
    regraded = runner.regrade(document, corpus)
    assert regraded["grades"][0]["success"] is True
    assert regraded["summary"]["modes"]["local_only"]["en"]["all"]["success"]["numerator"] == 1
    assert regraded["regraded_with"]["application"]["head"]


def test_regrading_refuses_another_corpus_digest(runner, corpus):
    with pytest.raises(SystemExit, match="digest"):
        runner.regrade({"corpus": {"sha256": "0" * 64}, "grades": []}, corpus)


def test_an_interrupted_run_removes_its_work_dir(runner, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    stopped = []

    class FakeRuntime:
        def launch(self, log):
            return object(), "http://127.0.0.1:1", 0.0

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.harness, "check_runtime", lambda manifest: FakeRuntime())
    monkeypatch.setattr(runner.harness, "application_revision", lambda: {})
    monkeypatch.setattr(runner.harness, "_stop", stopped.append)
    monkeypatch.setattr(runner.tempfile, "mkdtemp", lambda prefix: str(work))
    monkeypatch.setattr(runner, "_run_one", interrupt)
    args = argparse.Namespace(
        cases=["simple-01"],
        languages=None,
        modes=["local_only"],
        external_base_url=None,
        manifest="m.json",
        out=str(tmp_path / "x.json"),
    )
    with pytest.raises(KeyboardInterrupt):
        runner.run_corpus(args)
    assert not work.exists()
    assert len(stopped) == 1


# -- the draft answer pass, regraded offline (RM31, spec 0.31 section 12.2, #115) ------------

LOCAL = "qwen3-0.6b"
EXTERNAL = "glm-4.6"
STAGING_REPLY = "staging-api is healthy (checked at 2026-10-20T02:58:00Z)."
TOOL_CALL_TEXT = '{"name": "get_service_health", "arguments": {"service": "staging-api"}}'
LABEL = "offline counterfactual, not a measured run"


def build_trace(
    run_id,
    *,
    language="en",
    tool="get_service_health",
    arguments=None,
    draft="",
    final=None,
    ending="failed",
    answer_options=None,
    tool_after_draft=None,
):
    """A stored trace of a local run, written by hand (the stored event form).

    ending is failed (the final answer broke the criteria, local_only),
    handoff (the same, then the external model answered, auto), or
    completed (the final answer was published). final None means the run
    never reached a final generation.
    """
    events = []

    def add(kind, who="gateway", **payload):
        events.append(
            {
                "schema_version": 1,
                "run_id": run_id,
                "seq": len(events) + 1,
                "timestamp": "2026-10-22T13:00:00.000000Z",
                "elapsed_ms": 100 * len(events),
                "event_type": kind,
                "actor": who,
                "payload": payload,
            }
        )

    calls = []

    def generation(purpose, actor, model, *, text=None, proposed=(), usage=(10, 5)):
        call_id = f"{run_id}_gen{len(calls) + 1}_{purpose}"
        calls.append({"actor": actor, "call_id": call_id, "model": model, "purpose": purpose})
        add("model.started", actor, actor=actor, call_id=call_id, model=model, purpose=purpose)
        add(
            "model.finished",
            actor,
            call_id=call_id,
            model=model,
            purpose=purpose,
            text=text,
            proposed_calls=[{"tool_name": n, "arguments": a} for n, a in proposed],
            error=None,
            finish_reason="tool_calls" if proposed else "stop",
            usage={"input_tokens": usage[0], "output_tokens": usage[1], "cached_tokens": 0},
        )

    def run_tool(name, args):
        add(
            "tool.proposed",
            LOCAL,
            tool_name=name,
            arguments=args,
            proposal_id=f"p_{name}",
            rejection=None,
            admissible=True,
        )
        add("tool.started", tool_name=name, proposal_id=f"p_{name}", attempt=1)
        add("tool.finished", tool_name=name, proposal_id=f"p_{name}", status="succeeded")

    add("run.accepted", answer_options=answer_options or {})
    generation(
        "routing", "local", LOCAL, proposed=[("check-service-health", {"language": language})]
    )
    add(
        "route.selected",
        "local",
        decision={"route": "local", "language": language, "skill_id": "check-service-health"},
    )
    add("skill.loaded", name="check-service-health")
    arguments = arguments or {"service": "staging-api"}
    generation("skill", "local", LOCAL, proposed=[(tool, arguments)])
    run_tool(tool, arguments)
    generation("skill", "local", LOCAL, text=draft)
    if final is not None:
        generation("final", "local", LOCAL, text=final)
    summary = {"waiting_ms": 0, "external_calls": 0}
    if ending == "failed":
        add(
            "run.failed",
            error_code="needs_external",
            reason_code="invalid_output",
            model_calls=list(calls),
            **summary,
        )
    elif ending == "handoff":
        add("handoff.started", reason_code="invalid_output")
        generation("external", "external", EXTERNAL, text="healthy", usage=(40, 12))
        if tool_after_draft:
            run_tool(*tool_after_draft)
        add("answer.delta", "external", model=EXTERNAL, text="healthy")
        add(
            "run.completed",
            final_answer_model=EXTERNAL,
            answer="healthy",
            model_calls=list(calls),
            waiting_ms=0,
            external_calls=1,
        )
    else:
        add("answer.delta", "local", model=LOCAL, text=final if final is not None else draft)
        add(
            "run.completed",
            final_answer_model=LOCAL,
            answer=final if final is not None else draft,
            model_calls=list(calls),
            **summary,
        )
    return events


def export_of(runner, corpus, runs):
    """An export document of measured runs: runs is a list of (case_id, mode, events, state)."""
    grades, records, traces = [], {}, {}
    for case_id, mode, events, state in runs:
        case = corpus.case(case_id)
        run_id = events[0]["run_id"]
        reason = events[-1]["payload"].get("reason_code")
        record = {
            "run_id": run_id,
            "timed_out": False,
            "actions": [],
            "state": state,
            "error_code": events[-1]["payload"].get("error_code"),
            "reason_code": reason,
        }
        grades.append(
            runner.grade_record(
                case, mode, record, events, ["demo-analytics-db", "demo-orders-db"]
            ).to_json_object()
        )
        records[f"{case_id}/{mode}"] = record
        traces[run_id] = events
    return {
        "corpus": {"sha256": corpus.digest},
        "application": {"head": "abc", "dirty": False},
        "grades": grades,
        "records": records,
        "traces": traces,
    }


def counterfactual_by_run(result):
    return {(r["case_id"], r["mode"]): r for r in result["runs"]}


def three_local_runs(runner, corpus):
    """The three cases of the issue: a draft that passed and a final that failed (published),
    a Chinese run with an English draft (blocked), a draft holding tool-call JSON (blocked)."""
    return export_of(
        runner,
        corpus,
        [
            (
                "simple-01",
                "local_only",
                build_trace(
                    "run_pass",
                    arguments={"service": "payments-api"},
                    draft=PAYMENTS_OK,
                    final="payments-api is down.",
                ),
                "failed",
            ),
            (
                "simple-01-zh",
                "local_only",
                build_trace(
                    "run_zh",
                    language="zh",
                    arguments={"service": "payments-api"},
                    draft=PAYMENTS_OK,
                    final="payments-api 现在不可用。",
                ),
                "failed",
            ),
            (
                "simple-02",
                "local_only",
                build_trace("run_json", draft=TOOL_CALL_TEXT, final="staging-api is healthy."),
                "failed",
            ),
        ],
    )


def test_a_passing_draft_is_published_and_the_other_two_are_blocked(runner, corpus):
    """RM31: the draft is published when it is in the run's language and holds no tool-call JSON."""
    document = three_local_runs(runner, corpus)
    result = runner.regrade_draft_pass(document, corpus)
    by_run = counterfactual_by_run(result)

    published = by_run[("simple-01", "local_only")]
    assert published["outcome"] == "published" and published["reason"] is None
    assert published["measured"]["success"] is False
    assert published["counterfactual"]["success"] is True
    assert published["counterfactual"]["local_completion"] is True
    assert published["counterfactual"]["external_calls"] == 0
    # The final generation did not run: routing, one tool call turn, and the draft.
    assert published["measured"]["local_calls"] == 4
    assert published["counterfactual"]["local_calls"] == 3

    english_draft = by_run[("simple-01-zh", "local_only")]
    assert english_draft["outcome"] == "blocked" and english_draft["reason"] == "language"
    assert english_draft["run_language"] == "zh"
    assert english_draft["counterfactual"] == english_draft["measured"]

    tool_call = by_run[("simple-02", "local_only")]
    assert tool_call["outcome"] == "blocked" and tool_call["reason"] == "tool_call_json"
    assert tool_call["counterfactual"] == tool_call["measured"]


def test_the_counterfactual_grades_replace_only_the_published_run(runner, corpus):
    document = three_local_runs(runner, corpus)
    result = runner.regrade_draft_pass(document, corpus)
    grades = {g["case_id"]: g for g in result["grades"]}
    assert grades["simple-01"]["success"] is True
    assert grades["simple-01"]["answer"] == PAYMENTS_OK
    assert grades["simple-01-zh"]["success"] is False and grades["simple-02"]["success"] is False
    before = result["measured_summary"]["modes"]["local_only"]["en"]["all"]
    after = result["counterfactual_summary"]["modes"]["local_only"]["en"]["all"]
    assert (before["success"]["numerator"], after["success"]["numerator"]) == (0, 1)
    assert before["success"]["denominator"] == after["success"]["denominator"] == 2
    rows = {row["language"]: row for row in result["table"]}
    english, chinese = rows["en"], rows["zh"]
    assert (english["runs"], english["published"], english["published_pass"]) == (2, 1, 1)
    assert (english["gained"], english["lost"], english["blocked"]) == (1, 0, {"tool_call_json": 1})
    assert (chinese["runs"], chinese["published"], chinese["blocked"]) == (1, 0, {"language": 1})


def test_the_result_is_labeled_a_counterfactual_and_the_export_is_untouched(runner, corpus):
    document = three_local_runs(runner, corpus)
    snapshot = json.dumps(document, sort_keys=True)
    result = runner.regrade_draft_pass(document, corpus)
    assert result["label"] == LABEL
    assert result["kind"] != "relaymind.correctness_corpus"
    assert result["source"]["corpus"]["sha256"] == corpus.digest
    assert "traces" not in result
    assert json.dumps(document, sort_keys=True) == snapshot


def test_an_auto_handoff_after_a_failed_final_is_replaced_by_the_draft(runner, corpus):
    """The external calls that followed the final pass are not made when the draft is published."""
    document = export_of(
        runner,
        corpus,
        [
            (
                "simple-02",
                "auto",
                build_trace("run_auto", draft=STAGING_REPLY, final="healthy", ending="handoff"),
                "completed",
            )
        ],
    )
    entry = runner.regrade_draft_pass(document, corpus)["runs"][0]
    assert entry["outcome"] == "published"
    assert entry["measured"]["external_calls"] == 1
    assert entry["counterfactual"]["external_calls"] == 0
    assert entry["counterfactual"]["local_completion"] is True


def test_a_final_pass_that_succeeded_is_also_regraded_with_the_draft(runner, corpus):
    """A default must not lower success: a draft that fails grading where the final passed shows."""
    document = export_of(
        runner,
        corpus,
        [
            (
                "simple-02",
                "local_only",
                build_trace(
                    "run_ok",
                    draft="It is fine.",
                    final=STAGING_REPLY,
                    ending="completed",
                ),
                "completed",
            )
        ],
    )
    entry = runner.regrade_draft_pass(document, corpus)["runs"][0]
    assert entry["measured"]["success"] is True
    assert entry["outcome"] == "published"
    assert entry["counterfactual"]["success"] is False
    assert entry["measured_completed_locally"] is True
    (row,) = runner.regrade_draft_pass(document, corpus)["table"]
    assert (row["gained"], row["lost"], row["measured_pass"], row["published_pass"]) == (0, 1, 1, 0)


def test_runs_without_a_final_generation_and_external_only_runs_are_left_alone(runner, corpus):
    document = export_of(
        runner,
        corpus,
        [
            (
                "simple-02",
                "local_only",
                build_trace("run_nofinal", draft=STAGING_REPLY, final=None, ending="completed"),
                "completed",
            ),
            (
                "simple-02",
                "external_only",
                build_trace("run_ext", draft=STAGING_REPLY, final="x", ending="handoff"),
                "completed",
            ),
        ],
    )
    result = runner.regrade_draft_pass(document, corpus)
    assert result["runs"] == []
    assert result["grades"] == document["grades"]


def test_a_caller_answer_option_blocks_the_draft(runner, corpus):
    """RM31: with a non-default answer option the final generation runs as before."""
    document = export_of(
        runner,
        corpus,
        [
            (
                "simple-02",
                "local_only",
                build_trace(
                    "run_opt",
                    draft=STAGING_REPLY,
                    final="x",
                    answer_options={"max_tokens": 20},
                ),
                "failed",
            )
        ],
    )
    entry = runner.regrade_draft_pass(document, corpus)["runs"][0]
    assert (entry["outcome"], entry["reason"]) == ("blocked", "answer_options")


def test_a_run_that_changed_the_sandbox_after_the_draft_is_not_regraded(runner, corpus):
    """The stored sandbox state is the measured run's; a later delete makes it unusable."""
    document = export_of(
        runner,
        corpus,
        [
            (
                "simple-02",
                "auto",
                build_trace(
                    "run_late",
                    draft=STAGING_REPLY,
                    final="x",
                    ending="handoff",
                    tool_after_draft=("delete_demo_database", {"database_id": "demo-orders-db"}),
                ),
                "completed",
            )
        ],
    )
    result = runner.regrade_draft_pass(document, corpus)
    entry = result["runs"][0]
    assert (entry["outcome"], entry["reason"]) == ("not_gradable", "mutation_after_draft")
    assert entry["counterfactual"] == entry["measured"]


def test_the_draft_is_the_last_text_without_calls_before_the_final(runner):
    events = build_trace("run_last", draft="the draft", final="the final")
    draft = runner.find_draft(events)
    assert draft["text"] == "the draft" and draft["purpose"] == "skill"
    assert events[draft["index"]]["payload"]["text"] == "the draft"
    # A text from the final generation, or from before the tool call, is not the draft.
    assert runner.find_draft(events[: draft["index"] - 3]) is None
    assert runner.find_draft([]) is None


@pytest.mark.parametrize(
    ("text", "language", "reason"),
    [
        ("staging-api is healthy.", "en", None),
        ("staging-api 现在可用。", "zh", None),
        ("staging-api is healthy.", "zh", "language"),
        ("staging-api 现在可用。", "en", "language"),
        ("", "en", "no_draft"),
        (TOOL_CALL_TEXT, "en", "tool_call_json"),
        ('<tool_call>{"x": 1}</tool_call>', "en", "tool_call_json"),
        ("Call get_service_health with the service name.", "en", None),
        ("staging-api is healthy.", None, "language_unknown"),
    ],
)
def test_the_rm31_guards(runner, text, language, reason):
    assert runner.draft_block_reason(text, language, {}) == reason


def test_the_cli_writes_a_separate_labeled_export_and_report(runner, corpus, tmp_path, capsys):
    document = three_local_runs(runner, corpus)
    source = tmp_path / "source.json"
    source.write_text(json.dumps(document), "utf-8")
    before = source.read_bytes()
    out = tmp_path / "draft-pass.json"
    runner.main(["--regrade-draft-pass", str(source), "--out", str(out)])
    assert source.read_bytes() == before
    result = json.loads(out.read_text("utf-8"))
    assert result["label"] == LABEL
    report = out.with_suffix(".md").read_text("utf-8")
    assert LABEL in report.splitlines()[0]
    assert "published" in report and "language" in report


def test_the_cli_refuses_to_overwrite_the_source_export(runner, tmp_path, capsys):
    source = tmp_path / "source.json"
    source.write_text("{}", "utf-8")
    with pytest.raises(SystemExit):
        runner.main(["--regrade-draft-pass", str(source), "--out", str(source)])
    assert "must differ" in capsys.readouterr().err
    assert source.read_text("utf-8") == "{}"


def test_draft_pass_regrading_refuses_another_corpus_digest(runner, corpus):
    with pytest.raises(SystemExit, match="digest"):
        runner.regrade_draft_pass({"corpus": {"sha256": "0" * 64}, "grades": []}, corpus)
