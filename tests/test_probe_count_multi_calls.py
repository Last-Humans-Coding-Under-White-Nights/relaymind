"""The multiple-calls probe (#119; RM09, RM11, spec 0.31 sections 4.3 and 12.1).

scripts/probes/count_multi_calls.py reads a stored corpus export and
counts the round trips that admitting several read-only calls per
generation would have saved (finding F4 of the 2026-10-23 analysis). The
tests use small exports written by hand, never the 5 MB checked-in
export: the probe is deterministic, calls no model, and changes nothing.
"""

import json

import pytest

from helpers import load_script_module

LOCAL, EXTERNAL = "qwen3-0.6b", "glm-4.6"
ALL_TOOLS = [
    "delete_demo_database",
    "get_incident_counts",
    "get_latest_deployment",
    "get_metrics",
    "get_recent_logs",
    "get_service_health",
    "list_demo_databases",
]
HEALTH = ("get_service_health", {"service": "payments-api"})
LOGS = ("get_recent_logs", {"service": "payments-api", "limit": 20})
DEPLOY = ("get_latest_deployment", {"service": "payments-api"})
DELETE = ("delete_demo_database", {"database_id": "demo-orders-db", "expected_revision": 3})
LABEL = "offline analysis of recorded traces, not a measured run"


@pytest.fixture(scope="module")
def probe():
    return load_script_module("probes/count_multi_calls")


class Trace:
    """A stored trace written by hand; each generation records its proposals like the gateway."""

    def __init__(self, run_id, prompt, *, tool_calls=2, run_tool_executions=6):
        self.run_id, self.events, self.generations = run_id, [], 0
        self.add(
            "run.accepted",
            original_messages=[{"role": "user", "content": prompt}],
            budgets={"tool_calls": tool_calls, "run_tool_executions": run_tool_executions},
        )

    def add(self, kind, who="gateway", **payload):
        seq = len(self.events) + 1
        self.events.append(
            {
                "run_id": self.run_id,
                "seq": seq,
                "elapsed_ms": seq * 10,
                "event_type": kind,
                "actor": who,
                "payload": payload,
            }
        )

    def generation(self, who, calls, *, offered=ALL_TOOLS, text=None):
        """A model call that proposes calls; more than one is refused, as in the baseline."""
        self.generations += 1
        model = LOCAL if who == "local" else EXTERNAL
        call_id = f"{self.run_id}_gen{self.generations}"
        self.add("model.started", who, call_id=call_id, model=model, purpose="skill", actor=who)
        self.add("model.finished", who, call_id=call_id, model=model, purpose="skill", text=text)
        self.events[-1]["payload"]["proposed_calls"] = [
            {"tool_name": name, "arguments": arguments} for name, arguments in calls
        ]
        self.events[-2]["payload"]["tools_offered"] = list(offered)
        for name, arguments in calls:
            rejection = {"code": "multiple_tool_calls"} if len(calls) > 1 else None
            self.add(
                "tool.proposed",
                who,
                tool_name=name,
                arguments=arguments,
                rejection=rejection,
                admissible=rejection is None,
            )

    def execute(self, name, data=None):
        self.add("tool.started", tool_name=name)
        self.add("tool.finished", tool_name=name, result={"data": data or {}})

    def answered(self, text):
        self.add("clarification.answered", input=text, missing="service", waited_ms=1)


def export_of(*runs):
    """runs: (trace, case_id, class, language, mode)."""
    grades = [
        {
            "case_id": case_id,
            "case_class": case_class,
            "language": language,
            "mode": mode,
            "run_id": trace.run_id,
        }
        for trace, case_id, case_class, language, mode in runs
    ]
    return {
        "corpus": {"sha256": "0" * 64},
        "grades": grades,
        "traces": {trace.run_id: trace.events for trace, *_ in runs},
    }


def complex_run(run_id, mode="external_only", *, calls, later, **budgets):
    trace = Trace(run_id, "Why is payments-api slow?", **budgets)
    trace.generation("external", calls)
    for call in later:
        trace.generation("external", [call])
        trace.execute(call[0])
    trace.generation("external", [], text="answer")
    return trace, run_id, "complex", "en", mode


def only_generation(probe, document):
    result = probe.analyse_export(document)
    assert len(result["generations"]) == 1
    return result["generations"][0], result


def test_a_read_only_set_within_the_limit_is_admitted_and_the_later_generations_are_saved(probe):
    """Section 4.3: the set runs together, so the one-at-a-time re-proposals are not needed."""
    document = export_of(complex_run("run_a", calls=[HEALTH, LOGS], later=[HEALTH, LOGS]))
    generation, result = only_generation(probe, document)
    assert generation["admitted"] is True and generation["reason"] is None
    assert [c["class"] for c in generation["calls"]] == ["read", "read"]
    assert generation["limit"] == 6
    assert generation["saved_generations"] == 2
    assert result["totals"]["saved_generations"] == 2


def test_a_mutating_call_in_the_set_keeps_the_rejection_and_the_set_counts_as_mixed(probe):
    """RM11: a mutating call is admitted only as the generation's single call."""
    document = export_of(complex_run("run_b", calls=[HEALTH, DELETE], later=[HEALTH]))
    generation, result = only_generation(probe, document)
    assert generation["admitted"] is False and generation["reason"] == "mutating"
    assert generation["mixed"] is True
    assert generation["saved_generations"] == 0
    assert (result["totals"]["mixed"], result["totals"]["saved_generations"]) == (1, 0)


def test_more_calls_than_the_remaining_run_limit_keep_the_rejection(probe):
    """External: the entire-run tool limit less the tools started before the generation."""
    trace = Trace("run_c", "Why is payments-api slow?", run_tool_executions=6)
    for _ in range(3):
        trace.execute("get_service_health")
    trace.generation("external", [HEALTH, LOGS, DEPLOY, HEALTH])
    over, _ = only_generation(probe, export_of((trace, "complex-01", "complex", "en", "auto")))
    assert (over["limit"], over["admitted"], over["reason"]) == (3, False, "over_limit")

    trace = Trace("run_c2", "Why is payments-api slow?", run_tool_executions=6)
    for _ in range(3):
        trace.execute("get_service_health")
    trace.generation("external", [HEALTH, LOGS, DEPLOY])
    fits, _ = only_generation(probe, export_of((trace, "complex-01", "complex", "en", "auto")))
    assert (fits["limit"], fits["admitted"]) == (3, True)


def test_a_local_generation_is_limited_by_the_per_stage_tool_limit(probe):
    """Local: the per-stage limit; the entire-run limit does not apply to it."""
    trace = Trace("run_d", "Why is payments-api slow?", tool_calls=2, run_tool_executions=6)
    trace.generation("local", [HEALTH, LOGS, DEPLOY])
    over, _ = only_generation(probe, export_of((trace, "complex-01", "complex", "en", "auto")))
    assert (over["actor"], over["limit"], over["reason"]) == ("local", 2, "over_limit")

    trace = Trace("run_d2", "Why is payments-api slow?", tool_calls=2)
    trace.generation("local", [HEALTH, LOGS])
    fits, _ = only_generation(probe, export_of((trace, "complex-01", "complex", "en", "auto")))
    assert fits["admitted"] is True


@pytest.mark.parametrize(
    ("bad", "offered", "problem"),
    [
        (("get_service_health", {"service": "orders-api"}), ALL_TOOLS, "invalid_arguments"),
        (
            ("get_recent_logs", {"service": "payments-api", "limit": 500}),
            ALL_TOOLS,
            "invalid_arguments",
        ),
        (("get_secret", {}), ALL_TOOLS, "unknown_tool"),
        (LOGS, ["get_service_health"], "tool_not_offered"),
    ],
)
def test_a_call_that_fails_its_own_validation_keeps_the_rejection(probe, bad, offered, problem):
    """Section 4.3: a partly valid set never runs in part."""
    trace = Trace("run_e", "Why is payments-api slow?")
    trace.generation("external", [HEALTH, bad], offered=offered)
    generation, _ = only_generation(
        probe, export_of((trace, "complex-01", "complex", "en", "auto"))
    )
    assert generation["admitted"] is False and generation["reason"] == "invalid_call"
    assert [c["problem"] for c in generation["calls"]] == [None, problem]


def test_an_identifier_the_request_never_named_is_not_admitted(probe):
    """RM05: the ambiguous prompt names no service, so the model's guess is not provenance."""
    ambiguous = Trace("run_f", "Is the API healthy?")
    ambiguous.generation(
        "external",
        [("get_service_health", {"service": "staging-api"}), HEALTH],
    )
    generation, _ = only_generation(
        probe, export_of((ambiguous, "ambiguous-01", "ambiguous", "en", "external_only"))
    )
    assert generation["admitted"] is False and generation["reason"] == "invalid_call"
    assert {c["problem"] for c in generation["calls"]} == {"unsupported_identifier"}


def test_a_clarification_answer_and_an_earlier_tool_result_are_provenance(probe):
    answered = Trace("run_g", "Is the API healthy?")
    answered.answered("payments-api")
    answered.generation("external", [HEALTH, DEPLOY])
    generation, _ = only_generation(
        probe, export_of((answered, "ambiguous-01", "ambiguous", "en", "auto"))
    )
    assert generation["admitted"] is True

    chained = Trace("run_g2", "List my databases then delete one.")
    chained.execute("list_demo_databases", {"databases": [{"id": "demo-orders-db"}]})
    chained.generation("external", [("get_service_health", {"service": "payments-api"})] * 2)
    # payments-api appears nowhere in the request or the results.
    generation, _ = only_generation(
        probe, export_of((chained, "mutation-05", "mutation", "en", "auto"))
    )
    assert generation["reason"] == "invalid_call"


def test_a_re_proposal_with_changed_arguments_is_not_counted_as_saved(probe):
    """Only the same tool with the same canonical arguments counts."""
    changed = ("get_recent_logs", {"limit": 50, "service": "payments-api"})
    document = export_of(complex_run("run_h", calls=[HEALTH, changed], later=[HEALTH, LOGS]))
    generation, _ = only_generation(probe, document)
    assert generation["admitted"] is True
    assert generation["saved_generations"] == 1
    assert generation["reproposed_changed"] == 1


def test_a_refused_call_is_matched_once_by_a_later_generation(probe):
    document = export_of(
        complex_run("run_i", calls=[HEALTH, HEALTH], later=[HEALTH, HEALTH, HEALTH])
    )
    generation, _ = only_generation(probe, document)
    assert generation["saved_generations"] == 2


def test_single_call_generations_and_runs_without_one_are_ignored(probe):
    trace = Trace("run_j", "Check payments-api health")
    trace.generation("external", [HEALTH])
    trace.execute("get_service_health")
    result = probe.analyse_export(export_of((trace, "simple-01", "simple", "en", "auto")))
    assert result["generations"] == [] and result["totals"]["generations"] == 0


def test_the_counts_are_cross_checked_against_the_refusals_in_the_trace(probe):
    document = export_of(
        complex_run("run_k", calls=[HEALTH, LOGS, DEPLOY], later=[HEALTH]),
        complex_run("run_l", "auto", calls=[HEALTH, DELETE], later=[]),
    )
    result = probe.analyse_export(document)
    check = result["cross_check"]
    assert check["generations"] == 2
    assert check["proposals_in_multi_call_generations"] == 5
    assert check["proposals_refused_multiple_tool_calls"] == 5
    assert check["agree"] is True


def test_a_disagreeing_cross_check_is_reported(probe):
    trace, *rest = complex_run("run_m", calls=[HEALTH, LOGS], later=[])
    for event in trace.events:
        if event["event_type"] == "tool.proposed":
            event["payload"]["rejection"] = None
    result = probe.analyse_export(export_of((trace, *rest)))
    assert result["cross_check"]["agree"] is False


def test_the_table_is_per_language_mode_and_class_with_exact_counts(probe):
    document = export_of(
        complex_run("run_n", "auto", calls=[HEALTH, LOGS], later=[HEALTH, LOGS]),
        complex_run("run_o", "external_only", calls=[HEALTH, DELETE], later=[HEALTH]),
        complex_run("run_p", "external_only", calls=[HEALTH, LOGS, DEPLOY], later=[HEALTH, LOGS]),
    )
    rows = {(r["mode"], r["case_class"]): r for r in probe.analyse_export(document)["table"]}
    auto = rows[("auto", "complex")]
    assert (
        auto["generations"],
        auto["proposals"],
        auto["admitted"],
        auto["saved_generations"],
    ) == (
        1,
        2,
        1,
        2,
    )
    external = rows[("external_only", "complex")]
    assert (external["generations"], external["proposals"], external["admitted"]) == (2, 5, 1)
    assert (external["kept_rejected"], external["mixed"]) == ({"mutating": 1}, 1)
    assert external["saved_generations"] == 2


def test_the_result_is_labeled_and_the_export_is_untouched(probe):
    document = export_of(complex_run("run_q", calls=[HEALTH, LOGS], later=[HEALTH, LOGS]))
    snapshot = json.dumps(document, sort_keys=True)
    result = probe.analyse_export(document)
    assert result["label"] == LABEL
    assert json.dumps(document, sort_keys=True) == snapshot


def test_the_cli_writes_the_analysis_and_a_report(probe, tmp_path):
    document = export_of(complex_run("run_r", calls=[HEALTH, LOGS], later=[HEALTH, LOGS]))
    source = tmp_path / "export.json"
    source.write_text(json.dumps(document), "utf-8")
    out = tmp_path / "multi-calls.json"
    probe.main(["--export", str(source), "--out", str(out)])
    assert json.loads(out.read_text("utf-8"))["label"] == LABEL
    report = out.with_suffix(".md").read_text("utf-8")
    assert LABEL in report.splitlines()[0]
    assert "complex" in report and "external_only" in report


def test_the_cli_refuses_to_overwrite_the_export(probe, tmp_path, capsys):
    source = tmp_path / "export.json"
    source.write_text("{}", "utf-8")
    with pytest.raises(SystemExit):
        probe.main(["--export", str(source), "--out", str(source)])
    assert "must differ" in capsys.readouterr().err
