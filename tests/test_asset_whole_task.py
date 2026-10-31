"""Spec 0.60 section 12.6.5 host-trial export: freeze, plan, rows and grader.

RM16, RM39, RM40, RM44; AC30, AC36, AC37. These are offline tooling tests over
explicitly fixture-qualified documents. They establish no measured result,
host qualification or acceptance gate.
"""

import hashlib

import pytest

from relaymind.canonical import canonical_digest, canonical_json
from relaymind.evaluation.asset_whole_task import (
    ARMS,
    WholeTaskRejected,
    grade_host_report,
    normalized_total_tokens,
    plan_host_trial,
    run_host_trial,
    validate_whole_task,
    verify_host_freeze,
    verify_host_tasks,
)
from whole_task_helpers import (
    ENVIRONMENT,
    H,
    attempt,
    freeze_doc,
    h,
    ordered_rows,
    report_for,
    row,
    tasks_doc,
)

# --- freeze and task documents -------------------------------------------------


def test_freeze_digest_is_the_canonical_digest_of_the_whole_validated_object():
    freeze = freeze_doc()
    assert verify_host_freeze(freeze) == canonical_digest(freeze)


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda f: f["configurations"].reverse(), "arm_order"),
        (lambda f: f["configurations"][2].update(model="other"), "arm_variable"),
        (lambda f: f["configurations"][1].update(sampling_config_digest=H), "arm_variable"),
        (
            lambda f: f["configurations"][2].update(recommendation_config_digest=None),
            "recommendation_config",
        ),
        (
            lambda f: f["configurations"][0].update(recommendation_config_digest=H),
            "recommendation_config",
        ),
        (lambda f: f["task_policies"].reverse(), "policy_order"),
        (lambda f: f["asset_bindings"].append(dict(f["asset_bindings"][0])), "binding_order"),
        (lambda f: f["packages"].append(dict(f["packages"][0])), "package_order"),
    ],
)
def test_freeze_structure_contradictions_reject(mutate, reason):
    freeze = freeze_doc()
    mutate(freeze)
    with pytest.raises(WholeTaskRejected, match=reason):
        verify_host_freeze(freeze)


def test_freeze_is_closed():
    freeze = freeze_doc()
    freeze["override"] = True
    with pytest.raises(WholeTaskRejected, match="invalid_HostFreezeV1"):
        verify_host_freeze(freeze)


def test_tasks_input_digest_and_bindings_verify():
    freeze = freeze_doc()
    tasks = tasks_doc(freeze)
    assert verify_host_tasks(tasks, freeze) == canonical_digest(tasks)


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda t: t["tasks"].reverse(), "task_order"),
        (lambda t: t.update(freeze_digest=H), "freeze_binding"),
        (lambda t: t.update(corpus_lock_digest=H), "freeze_binding"),
        (lambda t: t["tasks"][0].update(permissions_digest=H), "policy_binding"),
        (lambda t: t["tasks"][0].update(budgets_digest=H), "policy_binding"),
        (lambda t: t["tasks"][0]["asset_bindings"][0].update(content_hash=H), "asset_binding"),
        (
            lambda t: t["tasks"][0].update(success_rubric_evidence_id="rubric-other"),
            "rubric_binding",
        ),
        (lambda t: t["tasks"].pop(), "policy_coverage"),
    ],
)
def test_task_document_contradictions_reject(mutate, reason):
    freeze = freeze_doc()
    tasks = tasks_doc(freeze)
    mutate(tasks)
    with pytest.raises(WholeTaskRejected, match=reason):
        verify_host_tasks(tasks, freeze)


# --- deterministic plan ----------------------------------------------------------


def test_plan_covers_every_task_arm_once_in_task_order_with_seeded_arm_order():
    freeze = freeze_doc(task_ids=("t1", "t2", "t3"))
    tasks = tasks_doc(freeze, lanes=("en_to_en", "en_to_en", "code_switch"))
    plan = plan_host_trial(tasks)
    assert [p["ordinal"] for p in plan] == list(range(1, 10))
    assert [p["task_id"] for p in plan] == ["t1"] * 3 + ["t2"] * 3 + ["t3"] * 3
    for task_id in ("t1", "t2", "t3"):
        arms = [p["arm"] for p in plan if p["task_id"] == task_id]
        expected = sorted(
            ARMS,
            key=lambda arm: (
                hashlib.sha256(
                    canonical_json(dict(seed=7, task_id=task_id, arm=arm)).encode()
                ).hexdigest(),
                arm.encode(),
            ),
        )
        assert arms == expected
    assert plan_host_trial(tasks) == plan


def test_seed_changes_arm_order_but_not_coverage():
    freeze = freeze_doc(task_ids=tuple(f"t{i}" for i in range(10)))
    tasks = tasks_doc(freeze, lanes=("en_to_en",) * 10)
    other = dict(tasks, seed=8)
    a, b = plan_host_trial(tasks), plan_host_trial(other)
    assert [p["arm"] for p in a] != [p["arm"] for p in b]
    assert sorted((p["task_id"], p["arm"]) for p in a) == sorted(
        (p["task_id"], p["arm"]) for p in b
    )


def test_empty_task_set_plans_nothing_and_cannot_pass():
    freeze = freeze_doc(task_ids=())
    freeze["rubric_evidence_ids"] = []
    tasks = tasks_doc(freeze, lanes=())
    report = report_for(freeze, tasks, [])
    assert report["planned"] == []
    summary = grade_host_report(report, tasks, freeze)
    assert summary["external_token_difference_per_task"] is None
    assert summary["success_gate"] == summary["cost_gate"] == summary["gate"] == "blocked"


# --- attempts and token arithmetic ---------------------------------------------


def test_component_disjoint_sums_all_four_components():
    assert normalized_total_tokens(attempt("a", tokens=(10, 5, 3, 2))) == 20


def test_nested_relationship_sums_input_and_output_only():
    a = attempt("a", tokens=(10, 5, 3, 2), relation="cached_within_input_reasoning_within_output")
    assert normalized_total_tokens(a) == 15


def test_nested_relationship_with_a_subset_larger_than_its_parent_rejects():
    a = attempt("a", tokens=(10, 5, 11, 0), relation="cached_within_input_reasoning_within_output")
    with pytest.raises(WholeTaskRejected, match="token_subset"):
        normalized_total_tokens(a)


@pytest.mark.parametrize(
    "tokens, relation",
    [
        ((10, 5, None, 0), "component_disjoint"),
        ((None, 5, 0, 0), "cached_within_input_reasoning_within_output"),
        ((10, 5, 0, 0), "unknown"),
    ],
)
def test_missing_components_or_relationship_stay_unknown(tokens, relation):
    assert normalized_total_tokens(attempt("a", tokens=tokens, relation=relation)) is None


def test_a_reported_total_that_contradicts_the_evidenced_components_rejects():
    with pytest.raises(WholeTaskRejected, match="token_total"):
        normalized_total_tokens(attempt("a", tokens=(10, 5, 0, 0), total=16))


def test_source_total_with_unknown_coverage_is_never_promoted_to_complete():
    a = attempt("a", tokens=(None, None, None, None), relation="unknown", total=99)
    assert normalized_total_tokens(a) is None


# --- rows, summary and gates -----------------------------------------------------


def two_task_setup(**freeze_kwargs):
    freeze = freeze_doc(**freeze_kwargs)
    tasks = tasks_doc(freeze)
    return freeze, tasks


def test_all_success_equal_cost_has_passing_metrics_but_fixture_gate_stays_blocked():
    freeze, tasks = two_task_setup()
    report = report_for(freeze, tasks, ordered_rows(tasks, row))
    summary = grade_host_report(report, tasks, freeze)
    assert summary["gains"] == summary["losses"] == 0
    assert summary["success_gate"] == "pass" and summary["cost_gate"] == "pass"
    assert summary["external_token_difference_per_task"] == 0
    assert summary["gate"] == "blocked"  # fixture/early reports never pass acceptance
    by_arm = {a["arm"]: a for a in summary["arms"]}
    assert by_arm["native"] == dict(
        arm="native",
        planned=2,
        success=2,
        failed=0,
        not_run=0,
        external_tokens=30,
        external_calls=2,
        unknown_cost_rows=0,
    )
    assert [lane["lane"] for lane in summary["lanes"]] == ["en_to_en", "zh_hans_to_zh_hans"]


def test_gains_losses_and_counts_partition_planned():
    freeze, tasks = two_task_setup()

    def make(task_id, arm):
        if arm == "native" and task_id == "t1":
            return row(task_id, arm, success=False)
        if arm == "recommendations" and task_id == "t2":
            return row(task_id, arm, status="timeout", success=False)
        return row(task_id, arm)

    summary = grade_host_report(report_for(freeze, tasks, ordered_rows(tasks, make)), tasks, freeze)
    assert summary["gains"] == 1 and summary["losses"] == 1
    assert summary["success_gate"] == "pass"
    rec = next(a for a in summary["arms"] if a["arm"] == "recommendations")
    assert (rec["success"], rec["failed"], rec["not_run"]) == (1, 1, 0)


def test_an_unknown_completed_verdict_counts_failed_and_blocks_the_success_gate():
    freeze, tasks = two_task_setup()

    def make(task_id, arm):
        if arm == "native" and task_id == "t1":
            return row(task_id, arm, success=None)
        if arm == "recommendations" and task_id == "t2":
            return row(task_id, arm, success=False)
        return row(task_id, arm)

    summary = grade_host_report(report_for(freeze, tasks, ordered_rows(tasks, make)), tasks, freeze)
    native = next(a for a in summary["arms"] if a["arm"] == "native")
    assert native["failed"] == 1
    # One known loss is visible, but the comparison is undecidable, not failed.
    assert summary["losses"] == 1 and summary["success_gate"] == "blocked"


def test_more_losses_than_gains_fails():
    freeze, tasks = two_task_setup()

    def make(task_id, arm):
        return row(task_id, arm, success=not (arm == "recommendations" and task_id == "t1"))

    summary = grade_host_report(report_for(freeze, tasks, ordered_rows(tasks, make)), tasks, freeze)
    assert summary["losses"] == 1 and summary["success_gate"] == "fail"
    assert summary["gate"] == "fail"


def test_known_positive_token_difference_fails_even_when_calls_are_unknown():
    freeze, tasks = two_task_setup()

    def make(task_id, arm):
        if arm == "recommendations":
            a = attempt(f"{task_id}-{arm}-1", tokens=(50, 5, 0, 0), calls=None)
            return row(task_id, arm, attempts=[a])
        return row(task_id, arm)

    summary = grade_host_report(report_for(freeze, tasks, ordered_rows(tasks, make)), tasks, freeze)
    assert summary["external_token_difference_per_task"] == 40
    assert summary["external_call_difference_per_task"] is None
    assert summary["cost_gate"] == "fail" and summary["gate"] == "fail"


def test_not_run_rows_cannot_manufacture_zero_cost_and_block():
    freeze, tasks = two_task_setup()

    def make(task_id, arm):
        if task_id == "t2":
            return row(task_id, arm, status="not_run")
        return row(task_id, arm)

    summary = grade_host_report(report_for(freeze, tasks, ordered_rows(tasks, make)), tasks, freeze)
    native = next(a for a in summary["arms"] if a["arm"] == "native")
    assert native["external_tokens"] is None and native["unknown_cost_rows"] == 1
    assert native["not_run"] == 1
    assert summary["cost_gate"] == "blocked" and summary["success_gate"] == "blocked"


def test_retries_and_local_attempts_are_counted_separately():
    freeze, tasks = two_task_setup()

    def make(task_id, arm):
        attempts = [
            attempt(f"{task_id}-{arm}-1"),
            dict(attempt(f"{task_id}-{arm}-2"), ordinal=2),
            dict(
                attempt(f"{task_id}-{arm}-3", external=False, tokens=(1000, 1000, 0, 0)), ordinal=3
            ),
        ]
        return row(task_id, arm, attempts=attempts)

    summary = grade_host_report(report_for(freeze, tasks, ordered_rows(tasks, make)), tasks, freeze)
    native = next(a for a in summary["arms"] if a["arm"] == "native")
    assert native["external_tokens"] == 60 and native["external_calls"] == 4


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda r: r["rows"].reverse(), "row_plan"),
        (lambda r: r["rows"].pop(), "row_plan"),
        (lambda r: r["planned"][0].update(ordinal=9), "plan_mismatch"),
        (lambda r: r.update(input_digest=H), "input_binding"),
        (lambda r: r.update(freeze_digest=H), "freeze_binding"),
        (lambda r: r.update(kind="acceptance"), "input_binding"),
        (lambda r: r["rows"][0].update(status="failed", success=True, reason="x"), "row_verdict"),
        (lambda r: r["rows"][0]["attempts"][0].update(ordinal=2), "attempt_ordinal"),
        (
            lambda r: r["rows"][1]["attempts"][0].update(id=r["rows"][0]["attempts"][0]["id"]),
            "attempt_identity",
        ),
        (lambda r: r["rows"][0]["attempts"][0].update(evidence_ids=[]), "dispatch_evidence"),
        (lambda r: r["rows"][0].update(active_ms=1000.0), "row_timing"),
        (lambda r: r["evidence"].pop(), "evidence_dangling"),
        (lambda r: r["evidence"].reverse(), "evidence_order"),
        (lambda r: r["summary"].update(gains=5), "summary_mismatch"),
    ],
)
def test_report_contradictions_reject(mutate, reason):
    freeze, tasks = two_task_setup()
    report = report_for(freeze, tasks, ordered_rows(tasks, row))
    mutate(report)
    with pytest.raises(WholeTaskRejected, match=reason):
        grade_host_report(report, tasks, freeze)


def test_success_requires_host_execution_evidence():
    freeze, tasks = two_task_setup()
    report = report_for(freeze, tasks, ordered_rows(tasks, row))
    report["rows"][0]["host_session_id"] = None
    with pytest.raises(WholeTaskRejected, match="success_evidence"):
        grade_host_report(report, tasks, freeze)


def test_not_run_rows_carry_no_work():
    freeze, tasks = two_task_setup()
    report = report_for(
        freeze, tasks, ordered_rows(tasks, lambda t, a: row(t, a, status="not_run"))
    )
    report["rows"][0]["elapsed_ms"] = 1.0
    with pytest.raises(WholeTaskRejected, match="not_run_work"):
        grade_host_report(report, tasks, freeze)


def test_arms_without_recommendation_config_carry_no_recommendation_ids():
    freeze, tasks = two_task_setup()
    report = report_for(freeze, tasks, ordered_rows(tasks, row))
    native = next(r for r in report["rows"] if r["arm"] == "native")
    native["recommendation_ids"] = ["rec-x"]
    with pytest.raises(WholeTaskRejected, match="recommendation_arm"):
        grade_host_report(report, tasks, freeze)


def test_registry_mismatch_rejects_and_matching_registry_verifies():
    freeze, tasks = two_task_setup()
    report = report_for(freeze, tasks, ordered_rows(tasks, row))
    registry = {
        e["id"]: dict(sha256=e["sha256"], bytes=e["bytes"], path=e["path"])
        for e in report["evidence"]
    }
    for task in tasks["tasks"]:
        for i in (*task["setup_evidence_ids"], task["success_rubric_evidence_id"]):
            registry[i] = dict(sha256=h(i), bytes=1, path=None)
    registry["ev-host"] = dict(sha256=h("ev-host"), bytes=1, path=None)
    package = freeze["packages"][0]
    registry["ev-package"] = dict(sha256=package["sha256"], bytes=package["bytes"], path=None)
    grade_host_report(report, tasks, freeze, registry=registry)
    first = report["evidence"][0]["id"]
    registry[first] = dict(registry[first], sha256=H)
    with pytest.raises(WholeTaskRejected, match="evidence_conflict"):
        grade_host_report(report, tasks, freeze, registry=registry)


def test_registry_missing_frozen_evidence_rejects():
    freeze, tasks = two_task_setup()
    report = report_for(freeze, tasks, ordered_rows(tasks, row))
    registry = {
        e["id"]: dict(sha256=e["sha256"], bytes=e["bytes"], path=e["path"])
        for e in report["evidence"]
    }
    with pytest.raises(WholeTaskRejected, match="evidence_dangling"):
        grade_host_report(report, tasks, freeze, registry=registry)


def test_qualified_acceptance_without_a_review_path_is_blocked_not_passed():
    ids = tuple(f"t{i:03d}" for i in range(100))
    freeze = freeze_doc(task_ids=ids)
    tasks = tasks_doc(freeze, lanes=("en_to_en",) * 100, kind="acceptance")
    report = report_for(freeze, tasks, ordered_rows(tasks, row), qualification="qualified")
    summary = grade_host_report(report, tasks, freeze)
    assert summary["success_gate"] == summary["cost_gate"] == "pass"
    assert summary["gate"] == "blocked"


# --- driver ----------------------------------------------------------------------


def captured(r: dict) -> tuple[dict, list]:
    ids = sorted({*r["evidence_ids"], *(i for a in r["attempts"] for i in a["evidence_ids"])})
    return r, [dict(id=i, path=None, sha256=h(i), bytes=1, provenance="fixture") for i in ids]


def test_driver_conserves_every_planned_row_after_interruption():
    freeze, tasks = two_task_setup()
    calls = []

    def execute(task, arm, config):
        calls.append((task["id"], arm))
        return captured(row(task["id"], arm))

    report = run_host_trial(
        tasks,
        freeze,
        execute,
        environment=ENVIRONMENT,
        artifacts=[],
        qualification="fixture",
        input_manifest=dict(path="inputs.json", digest=H, bytes=1),
        interrupted=lambda: len(calls) >= 2,
    )
    validate_whole_task("AssetHostReportV1", report)
    assert [(r["task_id"], r["arm"]) for r in report["rows"]] == [
        (p["task_id"], p["arm"]) for p in report["planned"]
    ]
    assert calls == [(p["task_id"], p["arm"]) for p in report["planned"][:2]]
    assert [r["status"] for r in report["rows"][2:]] == ["not_run"] * 4
    assert all(r["reason"] == "interrupted" for r in report["rows"][2:])
    assert report["summary"] == grade_host_report(report, tasks, freeze)


def test_driver_rejects_a_row_for_another_task_or_arm():
    freeze, tasks = two_task_setup()

    def execute(task, arm, config):
        return captured(row(task["id"], "native" if arm != "native" else "provider_only"))

    with pytest.raises(WholeTaskRejected, match="row_plan"):
        run_host_trial(
            tasks,
            freeze,
            execute,
            environment=ENVIRONMENT,
            artifacts=[],
            qualification="fixture",
            input_manifest=dict(path="inputs.json", digest=H, bytes=1),
            interrupted=lambda: False,
        )


def test_driver_passes_the_frozen_arm_configuration():
    freeze, tasks = two_task_setup()
    seen = {}

    def execute(task, arm, config):
        seen[arm] = config
        return captured(row(task["id"], arm))

    run_host_trial(
        tasks,
        freeze,
        execute,
        environment=ENVIRONMENT,
        artifacts=[],
        qualification="fixture",
        input_manifest=dict(path="inputs.json", digest=H, bytes=1),
        interrupted=lambda: False,
    )
    assert seen == {c["arm"]: c for c in freeze["configurations"]}


# --- review follow-ups (spec 0.60 section 12.6.5) ------------------------------


@pytest.mark.parametrize("status", ["failed", "timeout", "cancelled"])
def test_a_started_failure_with_a_null_verdict_stays_unknown(status):
    """Counting a row failed does not establish a known verdict; it blocks."""
    freeze, tasks = two_task_setup()

    def make(task_id, arm):
        if arm == "recommendations":
            return row(task_id, arm, status=status, success=None)
        return row(task_id, arm)

    summary = grade_host_report(report_for(freeze, tasks, ordered_rows(tasks, make)), tasks, freeze)
    rec = next(a for a in summary["arms"] if a["arm"] == "recommendations")
    assert rec["failed"] == 2
    assert summary["losses"] == 0 and summary["success_gate"] == "blocked"


@pytest.mark.parametrize("status", ["failed", "timeout", "cancelled"])
def test_a_started_failure_with_a_false_verdict_is_a_known_loss(status):
    freeze, tasks = two_task_setup()

    def make(task_id, arm):
        if arm == "recommendations":
            return row(task_id, arm, status=status, success=False)
        return row(task_id, arm)

    summary = grade_host_report(report_for(freeze, tasks, ordered_rows(tasks, make)), tasks, freeze)
    assert summary["losses"] == 2 and summary["success_gate"] == "fail"


def test_reasons_are_nullable_as_published():
    freeze, tasks = two_task_setup()

    def make(task_id, arm):
        if arm == "native":
            return dict(row(task_id, arm, success=False), reason="rubric_not_met")
        if arm == "provider_only":
            return dict(row(task_id, arm, status="failed", success=False), reason=None)
        return row(task_id, arm)

    grade_host_report(report_for(freeze, tasks, ordered_rows(tasks, make)), tasks, freeze)


def test_a_started_row_without_external_attempts_has_unknown_cost():
    """Complete no-dispatch evidence cannot be verified offline, so zero is unknown."""
    freeze, tasks = two_task_setup()

    def make(task_id, arm):
        if arm == "native":
            local = attempt(f"{task_id}-{arm}-1", external=False)
            return row(task_id, arm, attempts=[local])
        return row(task_id, arm)

    summary = grade_host_report(report_for(freeze, tasks, ordered_rows(tasks, make)), tasks, freeze)
    native = next(a for a in summary["arms"] if a["arm"] == "native")
    assert native["external_tokens"] is None and native["unknown_cost_rows"] == 2
    assert summary["cost_gate"] == "blocked"


def test_attempt_times_compare_as_instants_not_text():
    freeze, tasks = two_task_setup()

    def make(start, end):
        def build(task_id, arm):
            a = dict(attempt(f"{task_id}-{arm}-1"), started_at=start, ended_at=end)
            return row(task_id, arm, attempts=[a])

        return build

    forward = make("2026-10-31T00:00:00Z", "2026-10-31T00:00:00.1Z")
    grade_host_report(report_for(freeze, tasks, ordered_rows(tasks, forward)), tasks, freeze)
    backward = make("2026-10-31T00:00:00.1Z", "2026-10-31T00:00:00Z")
    report = report_for(freeze, tasks, ordered_rows(tasks, backward))
    with pytest.raises(WholeTaskRejected, match="attempt_timing"):
        grade_host_report(report, tasks, freeze)


def test_frozen_package_bytes_must_match_their_registry_evidence():
    freeze, tasks = two_task_setup()
    report = report_for(freeze, tasks, ordered_rows(tasks, row))
    registry = {
        e["id"]: dict(sha256=e["sha256"], bytes=e["bytes"], path=e["path"])
        for e in report["evidence"]
    }
    for task in tasks["tasks"]:
        for i in (*task["setup_evidence_ids"], task["success_rubric_evidence_id"]):
            registry[i] = dict(sha256=h(i), bytes=1, path=None)
    registry["ev-host"] = dict(sha256=h("ev-host"), bytes=1, path=None)
    package = freeze["packages"][0]
    registry["ev-package"] = dict(sha256=package["sha256"], bytes=package["bytes"], path=None)
    grade_host_report(report, tasks, freeze, registry=registry)
    registry["ev-package"] = dict(sha256=h("other"), bytes=package["bytes"], path=None)
    with pytest.raises(WholeTaskRejected, match="package_binding"):
        grade_host_report(report, tasks, freeze, registry=registry)


@pytest.mark.parametrize(
    "start, end, ok",
    [
        ("2026-10-31T00:00:00.1234561Z", "2026-10-31T00:00:00.1234569Z", True),
        ("2026-10-31T00:00:00.1234569Z", "2026-10-31T00:00:00.1234561Z", False),
        ("2026-10-31T00:00:00.10Z", "2026-10-31T00:00:00.1Z", True),
    ],
)
def test_attempt_times_keep_full_fractional_precision(start, end, ok):
    freeze, tasks = two_task_setup()

    def build(task_id, arm):
        a = dict(attempt(f"{task_id}-{arm}-1"), started_at=start, ended_at=end)
        return row(task_id, arm, attempts=[a])

    report = report_for(freeze, tasks, ordered_rows(tasks, build))
    if ok:
        grade_host_report(report, tasks, freeze)
    else:
        with pytest.raises(WholeTaskRejected, match="attempt_timing"):
            grade_host_report(report, tasks, freeze)
