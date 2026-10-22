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
