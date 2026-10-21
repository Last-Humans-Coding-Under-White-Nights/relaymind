"""PR #66 regressions: preserve evidence without rejecting valid phrasing (RM05, RM16)."""

from dataclasses import replace

import pytest

from relaymind.skills.predicates import COMPLETION_PREDICATES
from test_completion_predicates import (
    COUNTS,
    DELETED,
    DEPLOYED,
    HEALTHY,
    METRICS,
    NO_DEPLOYMENT,
    UNAVAILABLE,
    T,
)


@pytest.mark.parametrize("negator", ["不是", "不再", "未", "尚未", "并非"])
@pytest.mark.parametrize("evidence", [HEALTHY, UNAVAILABLE])
def test_chinese_negation_cannot_affirm_the_returned_health(negator, evidence):
    """RM16, review 4336239659: negating the returned status is not confirmation."""
    answer = f"payments-api 的状态{negator} {evidence.data['status']}，检查时间为 {T}。"
    assert not COMPLETION_PREDICATES["health_status_preserved"](answer, [evidence])


@pytest.mark.parametrize(
    "description",
    [
        "was verified in step-1 and is",
        "was verified in phase-1 and is",
        "was verified in check-1 and is",
        "was verified in item-1 and is",
        "was verified in tier-1 and is",
        "is in-service and",
        "is out-of-service and",
    ],
)
def test_procedural_words_do_not_start_a_new_resource_statement(description):
    """RM16, review 4336239666: status after ordinary hyphenated prose stays attributable."""
    answer = f"payments-api {description} healthy (checked at {T})."
    assert COMPLETION_PREDICATES["health_status_preserved"](answer, [HEALTHY])
    assert not COMPLETION_PREDICATES["health_status_preserved"](answer, [UNAVAILABLE])


def test_another_resource_status_does_not_affirm_the_service_status():
    """RM16: resource boundaries still prevent using a dependency's health as service health."""
    answer = f"payments-api depends on ledger-db which is healthy (checked at {T})."
    assert not COMPLETION_PREDICATES["health_status_preserved"](answer, [HEALTHY])


@pytest.mark.parametrize("node", ["web-1", "worker-2", "node-13"])
def test_identifier_digits_are_not_incident_totals(node):
    """RM16, review 4336239671: an ID beside an incident noun is not a count."""
    answer = (
        f"In the last 1440 minutes, staging-api had 7 incidents on {node} incident queue: "
        "1 critical, 3 warning, 3 info."
    )
    assert COMPLETION_PREDICATES["incident_counts_reported"](answer, [COUNTS])


@pytest.mark.parametrize("classifier", ["起", "次", "个"])
def test_chinese_incident_classifiers_are_accepted(classifier):
    """RM16, review 4336239677: a standard classifier preserves the labelled count."""
    answer = (
        f"staging-api 在 1440 分钟内共 7 {classifier}事件："
        f"1 {classifier}严重，3 {classifier}警告，3 {classifier}信息。"
    )
    assert COMPLETION_PREDICATES["incident_counts_reported"](answer, [COUNTS])


@pytest.mark.parametrize(
    "breakdown",
    [
        "Total: 7 incidents\nCritical: 1 incident\nWarning: 3 incidents\nInfo: 3 incidents",
        "Total incidents: 7\nCritical incidents: 1\nWarning incidents: 3\nInfo incidents: 3",
        "总计：7 个事件\n严重：1 个事件\n警告：3 个事件\n信息：3 个事件",
        "总计事件：7\n严重事件：1\n警告事件：3\n信息事件：3",
    ],
)
def test_incident_unit_nouns_do_not_override_severity_labels(breakdown):
    """RM16, review 4336239683: category counts with units must not become totals."""
    answer = f"staging-api, last 1440 minutes:\n{breakdown}"
    assert COMPLETION_PREDICATES["incident_counts_reported"](answer, [COUNTS])
    wrong = replace(COUNTS, data={**COUNTS.data, "critical": 2, "warning": 2})
    assert not COMPLETION_PREDICATES["incident_counts_reported"](answer, [wrong])


@pytest.mark.parametrize(
    "section",
    [
        "证据不足\n仅凭这些样本，无法判断原因。",
        "## 证据不足\n仅凭这些样本，无法判断原因。",
        "证据不足。\n仅凭这些样本，无法判断原因。",
        "## Insufficient Evidence\nOne sample cannot establish a cause.",
        "Insufficient evidence.\nOne sample cannot establish a cause.",
    ],
)
def test_insufficient_evidence_sections_do_not_require_a_colon(section):
    """RM16, review 4336239686: the skill permits bare labels and Markdown headings."""
    answer = "Observations:\n- get_metrics returned one sample.\n" + section
    assert COMPLETION_PREDICATES["performance_evidence_separated"](answer, [METRICS])


@pytest.mark.parametrize(
    "answer",
    [
        "payments-api 没有部署编号（共查询了 1 次）。",
        "payments-api 没有部署编号，耗时 1 秒。",
        "payments-api 暂时没有部署编号，这是第 1 次检查。",
    ],
)
def test_unrelated_chinese_numbers_are_not_deployment_ids(answer):
    """RM05, review 4336239693: only a number assigned to the deployment label is an ID."""
    assert COMPLETION_PREDICATES["deployment_reported"](answer, [NO_DEPLOYMENT])


@pytest.mark.parametrize("label", ["部署编号为 0142", "部署编号：0142", "部署编号（0142）"])
def test_assigned_chinese_deployment_numbers_remain_rejected(label):
    """RM05: narrowing the number detector must still reject invented deployment IDs."""
    assert not COMPLETION_PREDICATES["deployment_reported"](
        f"payments-api {label}。", [NO_DEPLOYMENT]
    )


def test_returned_revision_is_not_an_invented_identifier_without_a_deployment_id():
    """RM05, review 4336239698: the predicate accepts provenance from the result.

    The current sandbox schema only emits null revisions without a deployment;
    this checks the predicate's handling of independently supplied ToolResults.
    """
    evidence = replace(NO_DEPLOYMENT, data={**NO_DEPLOYMENT.data, "revision": "4f2c9e1"})
    check = COMPLETION_PREDICATES["deployment_reported"]
    assert check("payments-api has no deployment ID. Returned revision: 4f2c9e1.", [evidence])
    assert not check("payments-api has no deployment ID. Revision: 9a8b7c6.", [evidence])


@pytest.mark.parametrize("identifier", ["dep-staging-0142a", "dep-staging-0142-blue", "dep12blue"])
def test_deployment_ids_include_letters_after_the_last_digit(identifier):
    """RM05, review 4336239704: compare the whole returned ID, not its numeric prefix."""
    evidence = replace(DEPLOYED, data={**DEPLOYED.data, "deployment_id": identifier})
    check = COMPLETION_PREDICATES["deployment_reported"]
    assert check(f"staging-api deployment {identifier} at 2026-10-19T22:10:00Z.", [evidence])
    assert not check(f"staging-api deployment {identifier}x at 2026-10-19T22:10:00Z.", [evidence])


@pytest.mark.parametrize(
    "answer",
    [
        "Deleted the sandbox database demo-orders-db. Please note this action cannot be undone.",
        "Deleted the sandbox database demo-orders-db without failure.",
        "Deleted demo-orders-db. The database is not listed in the catalog anymore.",
        "已删除沙箱数据库 demo-orders-db。该操作无法撤销。",
        "已删除沙箱数据库 demo-orders-db，没有遗留数据。",
    ],
)
def test_deletion_followups_can_use_unrelated_negation(answer):
    """RM16, review 4336239711: negation of a follow-up does not negate the deletion."""
    assert COMPLETION_PREDICATES["deletion_outcome_reported"](answer, [DELETED])


@pytest.mark.parametrize(
    "denial",
    [
        "It was not deleted.",
        "It was not actually deleted.",
        "Failed to delete it.",
        "It wasn't deleted.",
        "未能删除。",
        "没有删除。",
        "删除失败。",
    ],
)
def test_a_deletion_denial_cannot_be_hidden_after_a_success_claim(denial):
    """RM16: scope negation to deletion, while retaining contradictory-outcome rejection."""
    assert not COMPLETION_PREDICATES["deletion_outcome_reported"](
        "Deleted demo-orders-db. " + denial, [DELETED]
    )


@pytest.mark.parametrize(
    "claim",
    [
        "could not be deleted",
        "cannot be deleted",
        "has not yet been deleted",
    ],
)
def test_deletion_negation_allows_auxiliaries_between_the_negator_and_verb(claim):
    """RM16: passive forms must not turn a deletion denial into a success claim."""
    assert not COMPLETION_PREDICATES["deletion_outcome_reported"](
        f"demo-orders-db {claim}.", [DELETED]
    )


@pytest.mark.parametrize("dependency", ["ledger-db, which is", "ledger-db:"])
def test_punctuated_dependency_statuses_are_not_service_evidence(dependency):
    """RM16: punctuation before a dependency predicate does not change its subject."""
    assert not COMPLETION_PREDICATES["health_status_preserved"](
        f"payments-api depends on {dependency} healthy (checked at {T}).", [HEALTHY]
    )
    assert COMPLETION_PREDICATES["health_status_preserved"](
        f"payments-api is unavailable (checked at {T}), while {dependency} healthy.",
        [UNAVAILABLE],
    )


@pytest.mark.parametrize("assignment", ["为：", "是：", "为: "])
def test_chinese_deployment_assignments_may_use_both_copula_and_colon(assignment):
    """RM05: punctuation must not hide an invented deployment ID."""
    assert not COMPLETION_PREDICATES["deployment_reported"](
        f"payments-api 最近一次部署编号{assignment}0142。", [NO_DEPLOYMENT]
    )


def test_negative_count_corrections_are_not_ignored_as_identifier_digits():
    """RM16: excluding identifier suffixes must retain contradictory signed counts."""
    answer = (
        "In the last 1440 minutes, staging-api had 7 incidents: "
        "1 critical, 3 warning, 3 info. Correction: -2 critical."
    )
    assert not COMPLETION_PREDICATES["incident_counts_reported"](answer, [COUNTS])
