"""Completion predicates: registered gateway validators (RM06, section 9).

A skill manifest names one of these IDs. Each predicate takes the final
answer text and the run's tool results and returns whether the answer
meets the skill's completion criteria. They check grounding, not style:
values the answer must preserve come from the latest successful tool
result, never from the model (RM05, RM16).

The diagnostic predicates also check that the answer keeps observations
apart from hypotheses or checklist items, using the section labels the
skill instructions require in English or Chinese. A label counts only
where it starts its own line as a section heading. A label check cannot
judge whether a hypothesis is sound; that stays with evaluation
(section 12).
"""

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from relaymind.tools.registry import ToolResult, ToolStatus

Predicate = Callable[[str, Sequence[ToolResult]], bool]

OBSERVATION_LABELS = ("Observations", "Observation", "观察")
HYPOTHESIS_LABELS = ("Hypotheses", "Hypothesis", "假设")
INSUFFICIENT_LABELS = ("Insufficient evidence", "证据不足")

# The draft label the checklist skill's completion criteria require
# (RM16): the checklist label followed directly by its draft marker.
_DRAFT_CHECKLIST_LABEL = re.compile(
    r"Diagnostic checklist\s*\(\s*draft,\s*not log-backed\s*\)"
    r"|诊断检查清单\s*[（(]\s*草稿\s*[，,]\s*未基于日志\s*[）)]",
    re.IGNORECASE,
)

_STATUS_WORD = re.compile(r"(?<![A-Za-z])(healthy|degraded|unavailable)(?![A-Za-z])", re.IGNORECASE)
# A statement about a service ends at a sentence or line break, or where
# another resource ID (such as ledger-db) starts a new subject.
_STATEMENT_END = re.compile(r"[.!?](?=\s|$)|[。！？；;\n]")
_RESOURCE_ID = re.compile(
    r"(?<![A-Za-z0-9-])[a-z][a-z0-9]*(?:-[a-z0-9]+)+(?![A-Za-z0-9-])", re.IGNORECASE
)

# Incident count labels as the summarize-incident-counts examples phrase
# them, in English and Chinese.
_COUNT_LABELS: Mapping[str, tuple[str, ...]] = {
    "total": ("total", "incidents", "incident", "共有", "共", "总计", "事件"),
    "critical": ("critical", "严重"),
    "warning": ("warnings", "warning", "警告"),
    "info": ("info", "信息"),
}
_WINDOW_LABELS = ("minutes", "minute", "分钟")

_EVIDENCE_TOOLS = ("get_latest_deployment", "get_metrics", "get_recent_logs")


def _latest(results: Sequence[ToolResult], tool_name: str) -> dict[str, Any] | None:
    for result in reversed(results):
        if result.tool_name == tool_name:
            if result.status is ToolStatus.SUCCEEDED and result.data is not None:
                return result.data
            return None
    return None


def _has_labeled_number(answer: str, value: int, labels: Sequence[str]) -> bool:
    """value is written right next to one of its labels: "3 warning", "警告 3 个"."""
    number = rf"(?<!\d)(?<!\d\.){value}(?!\.?\d)"
    label = "|".join(re.escape(item) for item in labels)
    before = rf"(?<![A-Za-z])(?:{label})\s*(?:[:：]|of)?\s*{number}"
    after = rf"{number}\s*(?:个|-)?\s*(?:{label})(?![A-Za-z])"
    return re.search(rf"{before}|{after}", answer, re.IGNORECASE) is not None


def _section_pattern(labels: Sequence[str], *, colon: bool) -> re.Pattern[str]:
    """A label that starts its own line, as a section heading (section 9).

    Leading spaces and Markdown heading, list, or emphasis marks are
    allowed. With colon, the label must be followed by ':' or '：';
    otherwise it must end at a word boundary ("Insufficient evidence to
    name a cause."). A label word inside a sentence never counts.
    """
    label = "|".join(re.escape(item) for item in labels)
    tail = r"[ \t*_]*[:：]" if colon else r"(?![A-Za-z])"
    return re.compile(rf"^[ \t#*>_-]*(?:{label}){tail}", re.IGNORECASE | re.MULTILINE)


_OBSERVATION_SECTION = _section_pattern(OBSERVATION_LABELS, colon=True)
_HYPOTHESIS_SECTION = _section_pattern(HYPOTHESIS_LABELS, colon=True)
_INSUFFICIENT_SECTION = _section_pattern(INSUFFICIENT_LABELS, colon=False)

# RM05: a deployment-like ID in any case and with any separator, such as
# dep-staging-0142, DEP-9, dep_0142, Deploy-77, or dep9. A token counts
# when dep, deploy, or deployment is followed by a separator and a
# digit-bearing token, or directly by digits. Words that only start with
# dep, such as depth2, deposit-1, or departure3, do not count.
_DEPLOYMENT_LIKE_ID = re.compile(
    r"(?<![A-Za-z0-9])dep(?:loy(?:ment)?)?(?:[-_#:]+[A-Za-z0-9_-]*\d|\d)", re.IGNORECASE
)


def _id_mention(resource_id: str) -> re.Pattern[str]:
    """A mention of a lowercase resource ID, in any case (RM05, section 9).

    Service and database IDs are lowercase identifiers by schema (an enum
    or a ``^[a-z0-9]`` pattern), so case folding never joins two IDs and
    "Staging-api" at the start of a sentence still names staging-api.
    The mention must end at an ID boundary: a longer ID such as
    staging-api-v2 is another resource. IDs with no case rule, such as
    deployment IDs, are matched exactly instead.
    """
    return re.compile(rf"(?<![A-Za-z0-9-]){re.escape(resource_id)}(?![A-Za-z0-9-])", re.IGNORECASE)


def _stated_statuses(answer: str, service: str) -> list[str]:
    """The status word stated for each mention of service.

    The stated status is the first status word after the service name in
    the same statement, in lowercase. Mentions without one are skipped.
    The service name and status word may be written in any case.
    """
    stated = []
    for found in _id_mention(service).finditer(answer):
        rest = answer[found.end() :]
        end = len(rest)
        for pattern in (_STATEMENT_END, _RESOURCE_ID):
            stop = pattern.search(rest)
            if stop is not None:
                end = min(end, stop.start())
        word = _STATUS_WORD.search(rest[:end])
        if word is not None:
            stated.append(word.group(1).lower())
    return stated


def health_status_preserved(answer: str, results: Sequence[ToolResult]) -> bool:
    """The answer names the returned service, states exactly the returned
    status for it, and repeats the check time (RM16, section 9).

    Status words about other resources, such as a dependency to check,
    do not count against the answer.
    """
    data = _latest(results, "get_service_health")
    if data is None:
        return False
    stated = _stated_statuses(answer, data["service"])
    if not stated or any(status != data["status"] for status in stated):
        return False
    return data["checked_at"] in answer


def deployment_reported(answer: str, results: Sequence[ToolResult]) -> bool:
    """The answer names the service, and its deployment ID and time appear,
    or no ID is claimed when none exists.

    The service is matched in any case, like the other predicates. With
    no recorded deployment, any deployment-like ID in the answer is
    invented (RM05), whatever its case or separator.
    """
    data = _latest(results, "get_latest_deployment")
    if data is None:
        return False
    if _id_mention(data["service"]).search(answer) is None:
        return False
    if data["deployment_id"] is None:
        return _DEPLOYMENT_LIKE_ID.search(answer) is None
    # The deployment ID has no case rule in the output schema, so it must
    # be quoted exactly (RM05).
    return data["deployment_id"] in answer and data["deployed_at"] in answer


def incident_counts_reported(answer: str, results: Sequence[ToolResult]) -> bool:
    """The window and every returned count appear as exact numbers, each
    next to its own label (section 9)."""
    data = _latest(results, "get_incident_counts")
    if data is None:
        return False
    if not _has_labeled_number(answer, data["window_minutes"], _WINDOW_LABELS):
        return False
    return all(
        _has_labeled_number(answer, data[key], labels) for key, labels in _COUNT_LABELS.items()
    )


def performance_evidence_separated(answer: str, results: Sequence[ToolResult]) -> bool:
    """Evidence was collected and observations are kept apart from hypotheses.

    Section 9: the answer has a labelled observations section and a
    labelled hypotheses or insufficient-evidence section, each starting
    its own line. Label words inside prose do not count.
    """
    if not any(_latest(results, tool) is not None for tool in _EVIDENCE_TOOLS):
        return False
    return _OBSERVATION_SECTION.search(answer) is not None and (
        _HYPOTHESIS_SECTION.search(answer) is not None
        or _INSUFFICIENT_SECTION.search(answer) is not None
    )


def deletion_outcome_reported(answer: str, results: Sequence[ToolResult]) -> bool:
    """A sandbox delete succeeded and the answer names the database, in any case."""
    data = _latest(results, "delete_demo_database")
    return (
        data is not None
        and data["deleted"] is True
        and _id_mention(data["database_id"]).search(answer) is not None
    )


def health_checklist_separated(answer: str, results: Sequence[ToolResult]) -> bool:
    """Observed health is preserved and the checklist carries its draft label (RM16).

    The health check runs on the answer outside the checklist section,
    which runs from the draft label to the next labelled observation
    section or the end, so draft items, even ones that use the word
    observation, are never read as the observation.
    """
    label = _DRAFT_CHECKLIST_LABEL.search(answer)
    if label is None:
        return False
    rest = answer[label.end() :]
    resume = _OBSERVATION_SECTION.search(rest)
    outside = answer[: label.start()] + (rest[resume.start() :] if resume else "")
    return health_status_preserved(outside, results)


COMPLETION_PREDICATES: Mapping[str, Predicate] = {
    "health_status_preserved": health_status_preserved,
    "deployment_reported": deployment_reported,
    "incident_counts_reported": incident_counts_reported,
    "performance_evidence_separated": performance_evidence_separated,
    "deletion_outcome_reported": deletion_outcome_reported,
    "health_checklist_separated": health_checklist_separated,
}
