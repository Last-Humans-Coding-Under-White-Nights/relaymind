"""Completion predicates: registered gateway validators (RM06, section 9).

RM06: completion predicates are registered gateway validators, not
executable text supplied by the model. Section 9: for health checks the
final response preserves the exact returned health state and time; for
diagnostics it distinguishes observations from hypotheses and may report
insufficient evidence. RM16: an unavailable service is never reported as
healthy, and a draft checklist is never a log-backed diagnosis. RM05:
identifiers come from tool results. Issues #9 and #13.

Each predicate has one table of (answer, tool results, expected) cases.
"""

import pytest

from relaymind.orchestrator.workflows import PARALLEL_WORKFLOWS
from relaymind.skills.predicates import COMPLETION_PREDICATES
from relaymind.tools import ToolResult, ToolStatus


def result(tool_name, data=None, *, status=ToolStatus.SUCCEEDED, op="op_1"):
    return ToolResult(
        operation_id=op,
        tool_name=tool_name,
        status=status,
        data=data,
        error=None
        if status is ToolStatus.SUCCEEDED
        else {"code": "tool_unavailable", "message": ""},
        observed_at="2026-10-20T03:00:00Z",
        duration_ms=1,
        source_ref=f"tool:{tool_name}/{op}",
        truncated=False,
    )


T = "2026-10-20T02:59:00Z"
UNAVAILABLE = result(
    "get_service_health",
    {"service": "payments-api", "status": "unavailable", "checked_at": T},
)
OLDER_HEALTHY = result(
    "get_service_health",
    {"service": "payments-api", "status": "healthy", "checked_at": "2026-10-20T01:00:00Z"},
    op="op_0",
)
FAILED_HEALTH = result("get_service_health", status=ToolStatus.FAILED)
DEPLOYED = result(
    "get_latest_deployment",
    {
        "service": "staging-api",
        "deployment_id": "dep-staging-0142",
        "deployed_at": "2026-10-19T22:10:00Z",
        "revision": "4f2c9e1",
    },
)
NO_DEPLOYMENT = result(
    "get_latest_deployment",
    {"service": "payments-api", "deployment_id": None, "deployed_at": None, "revision": None},
)
COUNTS = result(
    "get_incident_counts",
    {
        "service": "staging-api",
        "window_minutes": 1440,
        "total": 7,
        "critical": 1,
        "warning": 3,
        "info": 3,
    },
)
METRICS = result("get_metrics", {"service": "payments-api", "window_minutes": 15, "samples": []})
DELETED = result(
    "delete_demo_database", {"database_id": "demo-orders-db", "deleted": True, "revision": 4}
)
FAILED_DELETE = result("delete_demo_database", status=ToolStatus.FAILED)
LISTING = result("list_demo_databases", {"databases": []})

OBSERVED = "Observation: payments-api is unavailable (checked at 2026-10-20T02:59:00Z).\n\n"
ZH_OBSERVED = (
    "观察：payments-api 的状态是 unavailable（不可用），检查时间为 2026-10-20T02:59:00Z。\n\n"
)


def test_the_six_demo_predicates_are_registered():
    assert set(COMPLETION_PREDICATES) == {
        "health_status_preserved",
        "deployment_reported",
        "incident_counts_reported",
        "performance_evidence_separated",
        "deletion_outcome_reported",
        "health_checklist_separated",
    }


def test_the_fixed_parallel_workflow_is_registered():
    """RM15: one trusted workflow."""
    assert PARALLEL_WORKFLOWS == frozenset({"health-and-diagnostic-checklist"})


def check(predicate, answer, results, expected):
    assert COMPLETION_PREDICATES[predicate](answer, results) is expected


# --- health_status_preserved (RM05, RM16, section 9) --------------------------


@pytest.mark.parametrize(
    ("answer", "results", "expected"),
    [
        pytest.param(
            "payments-api is unavailable (checked at 2026-10-20T02:59:00Z).",
            [UNAVAILABLE],
            True,
            id="exact-status-and-time",
        ),
        pytest.param(
            "payments-api 当前状态为 unavailable（不可用），检查时间 2026-10-20T02:59:00Z。",
            [UNAVAILABLE],
            True,
            id="chinese",
        ),
        pytest.param(
            "payments-api 状态为unavailable，检查时间2026-10-20T02:59:00Z。",
            [UNAVAILABLE],
            True,
            id="status-right-after-chinese-text",
        ),
        pytest.param(
            "payments-api is unavailable (checked at 2026-10-20T02:59:00Z). "
            "Next, confirm ledger-db is healthy.",
            [UNAVAILABLE],
            True,
            id="other-resource-status-word",
        ),
        pytest.param(
            "payments-api is healthy (checked at 2026-10-20T02:59:00Z).",
            [UNAVAILABLE],
            False,
            id="unavailable-reported-healthy",
        ),
        pytest.param(
            "payments-api is unavailable (checked at 2026-10-20T02:59:00Z). "
            "Later payments-api is healthy again.",
            [UNAVAILABLE],
            False,
            id="service-restated-with-another-status",
        ),
        pytest.param("payments-api is unavailable.", [UNAVAILABLE], False, id="no-time"),
        pytest.param(
            "payments-api is unavailable (checked at 2026-10-20T03:00:00Z).",
            [UNAVAILABLE],
            False,
            id="wrong-time",
        ),
        pytest.param(
            "The service is unavailable (2026-10-20T02:59:00Z).",
            [UNAVAILABLE],
            False,
            id="service-not-named",
        ),
        pytest.param(
            "staging-api is unavailable (2026-10-20T02:59:00Z).",
            [UNAVAILABLE],
            False,
            id="other-service-named",
        ),
        pytest.param(
            "staging-api is healthy at 2026-10-20T02:58:00Z", [FAILED_HEALTH], False, id="failed"
        ),
        pytest.param(
            "staging-api is healthy at 2026-10-20T02:58:00Z", [], False, id="no-health-result"
        ),
        pytest.param(
            "payments-api is healthy at 2026-10-20T01:00:00Z",
            [OLDER_HEALTHY, UNAVAILABLE],
            False,
            id="uses-the-latest-result",
        ),
    ],
)
def test_health_status_preserved(answer, results, expected):
    check("health_status_preserved", answer, results, expected)


# --- deployment_reported (RM05) ------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "results", "expected"),
    [
        pytest.param(
            "Latest staging-api deployment: dep-staging-0142 at 2026-10-19T22:10:00Z.",
            [DEPLOYED],
            True,
            id="id-and-time",
        ),
        pytest.param("Latest deployment: dep-staging-0142.", [DEPLOYED], False, id="no-time"),
        pytest.param(
            "Latest deployment: dep-staging-0142 at 2026-10-19T22:10:00Z.",
            [DEPLOYED],
            False,
            id="no-service",
        ),
        pytest.param(
            "payments-api latest deployment was dep-staging-0142 at 2026-10-19T22:10:00Z.",
            [DEPLOYED],
            False,
            id="wrong-service",
        ),
        pytest.param(
            "payments-api has no recorded deployment.", [NO_DEPLOYMENT], True, id="none-and-no-id"
        ),
        pytest.param(
            "payments-api latest deployment is dep-payments-0001.",
            [NO_DEPLOYMENT],
            False,
            id="invented-id",
        ),
    ],
)
def test_deployment_reported(answer, results, expected):
    check("deployment_reported", answer, results, expected)


# --- incident_counts_reported (section 9) -------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(
            "In the last 1440 minutes staging-api had 7 incidents: 1 critical, 3 warning, 3 info.",
            True,
            id="every-count-and-window",
        ),
        pytest.param(
            "过去 1440 分钟内，staging-api 共有 7 个事件：严重 1 个，警告 3 个，信息 3 个。",
            True,
            id="chinese",
        ),
        pytest.param(
            "staging-api, last 1440 minutes: total 7, critical 1, warning 3, info 3.",
            True,
            id="labeled-total",
        ),
        pytest.param(
            "In the last 1440 minutes staging-api had 8 incidents: 1 critical, 3 warning, 3 info.",
            False,
            id="wrong-count",
        ),
        pytest.param(
            "In the last 1440 minutes: 17 incidents, 11 critical, 13 warning, 13 info.",
            False,
            id="digits-inside-other-numbers",
        ),
        pytest.param(
            "In the last 1440 minutes staging-api had 7 incidents: 3 critical, 1 warning, 3 info.",
            False,
            id="numbers-on-the-wrong-labels",
        ),
        pytest.param(
            "staging-api had 7 incidents over 1440 checks: 1 critical, 3 warning, 3 info.",
            False,
            id="window-not-labeled",
        ),
        pytest.param(
            "过去 1440 分钟内，staging-api 共有 7 个事件：严重 3 个，警告 1 个，信息 3 个。",
            False,
            id="chinese-labels-swapped",
        ),
    ],
)
def test_incident_counts_reported(answer, expected):
    check("incident_counts_reported", answer, [COUNTS], expected)


# --- performance_evidence_separated (section 9) -------------------------------


@pytest.mark.parametrize(
    ("answer", "results", "expected"),
    [
        pytest.param(
            "Observations:\n- p95 rose.\n\nHypotheses:\n- ledger-db is slow.",
            [METRICS],
            True,
            id="observations-and-hypotheses",
        ),
        pytest.param(
            "Observations:\n- p95 rose.\n\nInsufficient evidence to name a cause.",
            [METRICS],
            True,
            id="insufficient-evidence",
        ),
        pytest.param(
            "观察：\n- p95 延迟上升。\n\n假设：\n- ledger-db 变慢。", [METRICS], True, id="chinese"
        ),
        pytest.param("The cause is ledger-db being slow.", [METRICS], False, id="mixed"),
        pytest.param(
            "Observations:\n- none.\n\nHypotheses:\n- ledger-db.", [], False, id="no-evidence"
        ),
    ],
)
def test_performance_evidence_separated(answer, results, expected):
    check("performance_evidence_separated", answer, results, expected)


# --- deletion_outcome_reported -------------------------------------------------


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        pytest.param([DELETED], True, id="deleted"),
        pytest.param([LISTING], False, id="no-delete-result"),
        pytest.param([FAILED_DELETE], False, id="delete-failed"),
    ],
)
def test_deletion_outcome_reported(results, expected):
    answer = "Deleted sandbox database demo-orders-db."
    check("deletion_outcome_reported", answer, results, expected)


# --- health_checklist_separated (RM16) -----------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(
            OBSERVED + "Diagnostic checklist (draft, not log-backed):\n- Check ledger-db.",
            True,
            id="preserved-health-and-draft-label",
        ),
        pytest.param(
            ZH_OBSERVED + "诊断检查清单（草稿，未基于日志）：\n- 检查上游依赖。",
            True,
            id="chinese-draft-marker",
        ),
        pytest.param(
            OBSERVED + "Diagnostic checklist (draft, not log-backed):\n"
            "- Confirm payments-api is healthy after a restart.",
            True,
            id="checklist-item-about-recovery",
        ),
        pytest.param(
            OBSERVED.replace("unavailable", "healthy")
            + "Diagnostic checklist (draft, not log-backed):\n- Check ledger-db.",
            False,
            id="health-state-changed",
        ),
        pytest.param(
            "payments-api is unavailable (2026-10-20T02:59:00Z). The cause is ledger-db.",
            False,
            id="no-checklist-label",
        ),
        pytest.param(
            OBSERVED + "Diagnostic checklist:\n- Check ledger-db.", False, id="no-draft-marker"
        ),
        pytest.param(
            ZH_OBSERVED + "诊断检查清单：\n- 检查上游依赖。", False, id="chinese-no-draft-marker"
        ),
    ],
)
def test_health_checklist_separated(answer, expected):
    check("health_checklist_separated", answer, [UNAVAILABLE], expected)


# --- Review fixes: labelled sections and deployment-like IDs -------------------


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(
            "I made no observations and have no hypotheses. Root cause: bad deploy.",
            id="label-words-in-prose",
        ),
        pytest.param(
            "Observations: p95 rose. Hypotheses: ledger-db is slow.", id="one-line-not-sections"
        ),
        pytest.param("Observations:\n- p95 rose.\n\nThe cause is a bad deploy.", id="no-second"),
        pytest.param("我没有观察，也没有假设。原因是部署失败。", id="chinese-prose"),
    ],
)
def test_performance_labels_must_start_their_own_sections(answer):
    """Section 9: observations and hypotheses are separate labelled
    sections; the label words inside prose do not count."""
    check("performance_evidence_separated", answer, [METRICS], False)


def test_a_labelled_section_may_use_markdown_emphasis():
    answer = "**Observations:**\n- p95 rose.\n\n## Hypotheses:\n- ledger-db is slow."
    check("performance_evidence_separated", answer, [METRICS], True)


@pytest.mark.parametrize(
    "answer",
    [
        "payments-api's latest deployment was DEP-9 on Monday.",
        "payments-api deployment dep_0142 shipped.",
        "payments-api ran Deploy-77 yesterday.",
        "payments-api 的最新部署是 dep-payments-0001。",
    ],
)
def test_no_deployment_like_id_is_claimed_when_none_exists(answer):
    """RM05: with no recorded deployment, any deployment-like ID in the
    answer is invented, whatever its case or separator."""
    check("deployment_reported", answer, [NO_DEPLOYMENT], False)


@pytest.mark.parametrize(
    "answer",
    [
        "payments-api has no recorded deployment; check depth2 of the queue.",
        "payments-api has no recorded deployment. The deposit-1 job is unrelated.",
        "payments-api has no recorded deployment since departure3.",
        "payments-api has no recorded deployment; dependent0 services are fine.",
        "payments-api has no recorded deployment, so the deploy step 2 never ran.",
    ],
)
def test_words_that_start_with_dep_are_not_deployment_ids(answer):
    """RM05: only dep, deploy, or deployment followed by a separator and a
    digit-bearing token, or by digits, looks like an ID. Ordinary words
    such as depth2, deposit-1, or departure3 do not."""
    check("deployment_reported", answer, [NO_DEPLOYMENT], True)


@pytest.mark.parametrize(
    "answer",
    [
        "payments-api's latest deployment was dep9.",
        "payments-api ran deployment-2026-10 on Monday.",
        "payments-api ran deploy#12 yesterday.",
    ],
)
def test_other_deployment_like_ids_are_still_invented(answer):
    """RM05: deployment-like IDs in other forms still count as invented."""
    check("deployment_reported", answer, [NO_DEPLOYMENT], False)


def test_a_draft_item_that_mentions_an_observation_stays_in_the_checklist():
    """RM16: only a labelled observation section ends the checklist, so a
    draft item that uses the word is never read as the observation."""
    answer = (
        "Diagnostic checklist (draft, not log-backed):\n"
        "- Record an observation: payments-api is unavailable (2026-10-20T02:59:00Z).\n"
    )
    check("health_checklist_separated", answer, [UNAVAILABLE], False)


# --- identifier case (RM05, RM16, section 9) -----------------------------------


@pytest.mark.parametrize(
    ("predicate", "answer", "results", "expected"),
    [
        pytest.param(
            "health_status_preserved",
            "Payments-api is unavailable (checked at 2026-10-20T02:59:00Z).",
            [UNAVAILABLE],
            True,
            id="health-capital-service",
        ),
        pytest.param(
            "health_status_preserved",
            "PAYMENTS-API: Unavailable, checked at 2026-10-20T02:59:00Z.",
            [UNAVAILABLE],
            True,
            id="health-upper-service-and-status",
        ),
        pytest.param(
            "health_status_preserved",
            "Payments-api is Healthy (checked at 2026-10-20T02:59:00Z).",
            [UNAVAILABLE],
            False,
            id="health-capital-service-wrong-status",
        ),
        pytest.param(
            "health_status_preserved",
            "Payments-api-v2 is unavailable (checked at 2026-10-20T02:59:00Z).",
            [UNAVAILABLE],
            False,
            id="health-longer-id-is-another-service",
        ),
        pytest.param(
            "deployment_reported",
            "Staging-api latest deployment: dep-staging-0142 at 2026-10-19T22:10:00Z.",
            [DEPLOYED],
            True,
            id="deployment-capital-service",
        ),
        pytest.param(
            "deployment_reported",
            "Latest STAGING-API deployment: dep-staging-0142 at 2026-10-19T22:10:00Z.",
            [DEPLOYED],
            True,
            id="deployment-upper-case-service",
        ),
        pytest.param(
            "deployment_reported",
            "Latest staging-api deployment: DEP-STAGING-0142 at 2026-10-19T22:10:00Z.",
            [DEPLOYED],
            False,
            id="deployment-id-stays-exact",
        ),
        pytest.param(
            "deployment_reported",
            "Payments-api has no recorded deployment.",
            [NO_DEPLOYMENT],
            True,
            id="no-deployment-capital-service",
        ),
        pytest.param(
            "deployment_reported",
            "payments-api-v2 has no recorded deployment.",
            [NO_DEPLOYMENT],
            False,
            id="no-deployment-longer-id-is-another-service",
        ),
        pytest.param(
            "deletion_outcome_reported",
            "Deleted sandbox database Demo-Orders-DB.",
            [DELETED],
            True,
            id="deletion-capital-database",
        ),
        pytest.param(
            "deletion_outcome_reported",
            "Deleted sandbox database demo-orders-db-2.",
            [DELETED],
            False,
            id="deletion-longer-id-is-another-database",
        ),
    ],
)
def test_lowercase_identifiers_match_in_any_case(predicate, answer, results, expected):
    """Service and database IDs are lowercase identifiers by schema (enum
    or ^[a-z0-9]... pattern), so the answer may name them in any case and
    still name exactly one resource ("Staging-api is healthy" at the start
    of a sentence; live AC01). The match still needs ID boundaries, so a
    longer ID is another resource. Deployment IDs have no case rule in the
    output schema, so a quoted deployment ID must stay exact (RM05)."""
    check(predicate, answer, results, expected)


# --- Audit fixes: wrong passes (RM05, RM16, section 9; issue #13) --------------

HEALTHY = result(
    "get_service_health", {"service": "payments-api", "status": "healthy", "checked_at": T}
)


@pytest.mark.parametrize(
    ("answer", "results", "expected"),
    [
        pytest.param(f"payments-api is not healthy (checked at {T}).", [HEALTHY], False, id="not"),
        pytest.param(
            f"payments-api is no longer healthy (checked at {T}).",
            [HEALTHY],
            False,
            id="no-longer",
        ),
        pytest.param(
            f"payments-api 的状态不 healthy，检查时间为 {T}。", [HEALTHY], False, id="zh-bu"
        ),
        pytest.param(
            f"payments-api 的状态非 healthy，检查时间为 {T}。", [HEALTHY], False, id="zh-fei"
        ),
        pytest.param(
            f"payments-api is unavailable or healthy (checked at {T}).",
            [UNAVAILABLE],
            False,
            id="second-status-same-statement",
        ),
        pytest.param(
            f"payments-api is unavailable (checked at {T}), but payments-api is "
            "self-healing and healthy now.",
            [UNAVAILABLE],
            False,
            id="hyphenated-word-is-not-a-resource",
        ),
        pytest.param(
            f"payments-api 的状态是 unavailable（健康），检查时间为 {T}。",
            [UNAVAILABLE],
            False,
            id="zh-gloss-contradicts",
        ),
        pytest.param(
            f"payments-api 的状态是 healthy（不可用），检查时间为 {T}。",
            [HEALTHY],
            False,
            id="zh-gloss-contradicts-healthy",
        ),
        pytest.param(
            f"payments-api 的状态是 healthy（健康），检查时间为 {T}。",
            [HEALTHY],
            True,
            id="zh-gloss-matches",
        ),
        pytest.param(
            f"payments-api is unavailable (checked at {T}). Next, check whether web-1 "
            "and ledger-db are healthy.",
            [UNAVAILABLE],
            True,
            id="other-resources-still-end-the-statement",
        ),
        pytest.param(
            f"payments-api is unavailable (checked at {T}); the self-test is not done.",
            [UNAVAILABLE],
            True,
            id="negator-elsewhere-is-fine",
        ),
    ],
)
def test_health_status_rejects_negated_or_mixed_statuses(answer, results, expected):
    """RM16: an unavailable service is never reported as healthy, by
    negation, by a second status word, or by a contradicting gloss."""
    check("health_status_preserved", answer, results, expected)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(
            "Diagnostic checklist (draft, not log-backed):\n"
            f"- Observation: payments-api is unavailable (checked at {T}).",
            False,
            id="list-item-is-not-the-observation-section",
        ),
        pytest.param(
            f"Observation: payments-api is unavailable (checked at {T}). This is not a "
            "Diagnostic checklist (draft, not log-backed); root cause is ledger-db.",
            False,
            id="draft-label-mid-sentence",
        ),
        pytest.param(
            "Diagnostic checklist (draft, not log-backed):\n- Check ledger-db.\n\n"
            f"**Observation:** payments-api is unavailable (checked at {T}).",
            True,
            id="observation-heading-after-checklist",
        ),
        pytest.param(
            OBSERVED + "## Diagnostic checklist (draft, not log-backed):\n- Check ledger-db.",
            True,
            id="draft-label-as-markdown-heading",
        ),
    ],
)
def test_health_checklist_structure(answer, expected):
    """RM16: the draft label starts its own line, and a checklist item
    never stands in for the observation section."""
    check("health_checklist_separated", answer, [UNAVAILABLE], expected)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(
            "Observations:\n- p95 rose.\n"
            "Insufficient evidence is not an issue: the root cause is ledger-db.",
            False,
            id="insufficient-words-in-an-assertion",
        ),
        pytest.param(
            "观察：\n- p95 上升。\n证据不足以说明其他原因，根因就是 ledger-db 宕机。",
            False,
            id="zh-insufficient-words-in-an-assertion",
        ),
        pytest.param(
            "Observations:\n- The root cause is ledger-db.\nHypotheses:",
            False,
            id="empty-hypotheses",
        ),
        pytest.param(
            "Observations:\n\nHypotheses:\n- ledger-db is slow.", False, id="empty-observations"
        ),
        pytest.param(
            "Observations:\n- p95 rose.\n\nInsufficient evidence:", False, id="empty-insufficient"
        ),
        pytest.param(
            "Observations:\n- p95 rose.\n\nInsufficient evidence: one sample is not enough.",
            True,
            id="insufficient-label-with-colon",
        ),
        pytest.param(
            "观察：\n- p95 为 175 ms。\n\n证据不足：仅凭这些样本，无法判断是否存在性能问题。",
            True,
            id="zh-insufficient-label",
        ),
    ],
)
def test_performance_sections_are_real_and_non_empty(answer, expected):
    """Section 9: observations and hypotheses (or insufficient evidence)
    are labelled, non-empty sections; the insufficient-evidence label is
    a label, not the start of an assertion."""
    check("performance_evidence_separated", answer, [METRICS], expected)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(
            "In the last 1440 minutes staging-api had 7 incidents: 1 critical, 3 warning, "
            "3 info. They were caused by the 22:10 deployment.",
            False,
            id="inferred-cause",
        ),
        pytest.param(
            "In the last 1440 minutes staging-api had 7 incidents: 1 critical, 3 warning, "
            "3 info, because of a bad deploy.",
            False,
            id="because",
        ),
        pytest.param(
            "过去 1440 分钟内，staging-api 共有 7 个事件：严重 1 个，警告 3 个，信息 3 个。"
            "原因是最近一次部署。",
            False,
            id="zh-inferred-cause",
        ),
        pytest.param(
            "过去 1440 分钟内，staging-api 共有 7 个事件：严重 1 个，警告 3 个，信息 3 个，"
            "由于部署导致。",
            False,
            id="zh-due-to",
        ),
        pytest.param(
            "In the last 1440 minutes staging-api had 7 incidents: 1 critical, 3 warning, "
            "3 info. Correction: 9 incidents, 2 critical.",
            False,
            id="second-number-for-a-label",
        ),
        pytest.param(
            "In the last 1440 minutes staging-api had 7 incidents: 3 critical 1 warning 3 info.",
            False,
            id="label-ambiguity",
        ),
        pytest.param(
            "In the last 1440 minutes: 7 incidents, 1 critical, 3 warning, 3 info.",
            False,
            id="service-not-named",
        ),
        pytest.param(
            "In the last 1440 minutes, staging-api had 7 incidents: 1 critical, 3 warning, "
            "and 3 info.",
            True,
            id="comma-after-window",
        ),
        pytest.param(
            "最近 1440 分钟内，staging-api 共有 7 个事件：严重 1 个，警告 3 个，信息 3 个。",
            True,
            id="zh-zuijin",
        ),
    ],
)
def test_incident_counts_without_causes_or_conflicts(answer, expected):
    """Section 9: the summary reports the returned counts for the named
    service, without inferred causes and without a second number for a
    count."""
    check("incident_counts_reported", answer, [COUNTS], expected)


@pytest.mark.parametrize(
    ("answer", "results", "expected"),
    [
        pytest.param(
            "staging-api latest deployment is dep-staging-0199; the one before was "
            "dep-staging-0142 at 2026-10-19T22:10:00Z.",
            [DEPLOYED],
            False,
            id="another-deployment-id",
        ),
        pytest.param(
            "The latest staging-api deployment is dep-staging-0142, deployed at "
            "2026-10-19T22:10:00Z (revision 4f2c9e1).",
            [DEPLOYED],
            True,
            id="returned-revision",
        ),
        pytest.param(
            "staging-api 最近一次部署是 dep-staging-0142，部署时间为 2026-10-19T22:10:00Z"
            "（修订号 4f2c9e1）。",
            [DEPLOYED],
            True,
            id="zh-returned-revision",
        ),
        pytest.param(
            "payments-api has no recorded deployment ID, but revision 4f2c9e1 is running.",
            [NO_DEPLOYMENT],
            False,
            id="invented-revision",
        ),
        pytest.param(
            "payments-api 最近一次部署编号为 0142。", [NO_DEPLOYMENT], False, id="zh-invented-id"
        ),
        pytest.param("payments-api 没有部署记录。", [NO_DEPLOYMENT], True, id="zh-none"),
    ],
)
def test_deployment_ids_come_from_the_tool_result(answer, results, expected):
    """RM05: the answer names no deployment ID or revision the tool did
    not return."""
    check("deployment_reported", answer, results, expected)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param("Failed to delete demo-orders-db; it still exists.", False, id="failed"),
        pytest.param("demo-orders-db was not deleted.", False, id="not-deleted"),
        pytest.param("未能删除 demo-orders-db。", False, id="zh-failed"),
        pytest.param("没有删除 demo-orders-db。", False, id="zh-not"),
        pytest.param("Deleted demo-orders-db and demo-users-db.", False, id="another-database"),
        pytest.param("demo-orders-db is listed.", False, id="no-deletion-verb"),
        pytest.param(
            "Deleted the sandbox database demo-orders-db. The sandbox fixture revision is now 4.",
            True,
            id="deleted-with-revision",
        ),
        pytest.param(
            "已删除沙箱数据库 demo-orders-db。沙箱数据的修订号现为 4。", True, id="zh-deleted"
        ),
    ],
)
def test_deletion_answer_reports_the_returned_outcome(answer, expected):
    """RM16: the answer states the deletion that happened, for the
    returned database only, without negating it."""
    check("deletion_outcome_reported", answer, [DELETED], expected)
