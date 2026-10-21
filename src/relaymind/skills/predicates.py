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

# A section label starts its own line after optional spaces and Markdown
# heading, list, or emphasis marks; a heading label ends with a colon.
_HEADING_PREFIX = r"^[ \t#*>_-]*"
_COLON = r"[ \t*_]*[:：]"

# The draft label the checklist skill's completion criteria require
# (RM16): the checklist label followed directly by its draft marker. It
# must start its own line, like the other section labels.
_DRAFT_CHECKLIST_LABEL = re.compile(
    _HEADING_PREFIX + r"(?:Diagnostic checklist\s*\(\s*draft,\s*not log-backed\s*\)"
    r"|诊断检查清单\s*[（(]\s*草稿\s*[，,]\s*未基于日志\s*[）)])",
    re.IGNORECASE | re.MULTILINE,
)

_STATUS_WORD = re.compile(r"(?<![A-Za-z])(healthy|degraded|unavailable)(?![A-Za-z])", re.IGNORECASE)
# A negator right before a status word reverses it: "not healthy",
# "no longer healthy", "不 healthy" (RM16).
_NEGATED = re.compile(
    r"(?:(?<![A-Za-z])(?:not|no longer)|n't)\s+$|(?:不是|不再|[不非未])\s*$", re.IGNORECASE
)
# The Chinese gloss a status word may carry, such as unavailable（不可用）.
_GLOSS = re.compile(r"\s*[（(]\s*(健康|降级|不可用)\s*[）)]")
_GLOSS_STATUS = {"健康": "healthy", "降级": "degraded", "不可用": "unavailable"}
# A statement about a service ends at a sentence or line break, or where
# another resource ID (such as ledger-db or web-1) starts a new subject.
# An ID-shaped token is a new subject only when followed by a predicate.
# Procedural prose such as "in step-1 and is healthy" keeps the service
# as its subject; an infrastructure suffix alone is not enough.
_STATEMENT_END = re.compile(r"[.!?](?=\s|$)|[。！？；;\n]")
_RESOURCE_ID = re.compile(
    r"(?<![A-Za-z0-9-])[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
    r"-(?:api|db|service|svc|cache|queue|worker|web|server|gateway|proxy|[a-z]*\d[a-z0-9]*)"
    r"(?![A-Za-z0-9-])"
    r"(?=(?:[ \t]+|[ \t]*[,：:][ \t]*)(?:(?:which|that)[ \t]+)?"
    r"(?:is|are|was|were|seems?|remains?|healthy|degraded|unavailable)"
    r"(?![A-Za-z])|[ \t]*(?:的状态|状态|是))",
    re.IGNORECASE,
)

# Incident count labels as the summarize-incident-counts examples phrase
# them, in English and Chinese.
_COUNT_LABELS: Mapping[str, tuple[str, ...]] = {
    "total": ("total", "incidents", "incident", "共有", "共", "总计", "事件"),
    "critical": ("critical", "严重"),
    "warning": ("warnings", "warning", "警告"),
    "info": ("info", "信息"),
}
_INCIDENT_LABELS: Mapping[str, tuple[str, ...]] = {
    "window_minutes": ("minutes", "minute", "分钟"),
    **_COUNT_LABELS,
}
# Section 9: the incident summary reports counts without inferred causes.
_CAUSE_WORDS = re.compile(
    r"(?<![A-Za-z])(?:caused by|because|due to|root cause)(?![A-Za-z])|原因|由于|导致",
    re.IGNORECASE,
)
_COUNT_NUMBER = re.compile(r"(?<![A-Za-z0-9_.-])-?\d+(?![A-Za-z0-9_]|\.\d)")

_EVIDENCE_TOOLS = ("get_latest_deployment", "get_metrics", "get_recent_logs")


def _latest(
    results: Sequence[ToolResult], tool_name: str, *, require_complete: bool = True
) -> dict[str, Any] | None:
    """Use the latest outcome; complete-result skills cannot fall back to stale data (RM22)."""
    for result in reversed(results):
        if result.tool_name == tool_name:
            if (
                result.status is ToolStatus.SUCCEEDED
                and result.data is not None
                and (not require_complete or not result.truncated)
            ):
                return result.data
            return None
    return None


def _alternation(items: Sequence[str]) -> str:
    return "|".join(re.escape(item) for item in items)


# A number followed by a label ("3 warning", "7 个事件") belongs to that
# label; otherwise a label right before it ("total 7", "警告 3 个")
# names it.
_LABEL_AFTER = {
    key: re.compile(
        rf"[ \t]*(?:[个起次]|-)?[ \t]*(?:{_alternation(items)})(?![A-Za-z])", re.IGNORECASE
    )
    for key, items in _INCIDENT_LABELS.items()
}
_LABEL_BEFORE = {
    key: re.compile(
        rf"(?<![A-Za-z])(?:{_alternation(items)})(?![A-Za-z])"
        r"[ \t]*(?:incidents?(?![A-Za-z])|事件)?[ \t]*(?:[:：]|of)?[ \t]*$",
        re.IGNORECASE,
    )
    for key, items in _INCIDENT_LABELS.items()
}


def _labeled_numbers(answer: str) -> dict[str, list[int]]:
    """Each number written right next to an incident label, by label key.

    So in "7 incidents: 1 critical" the 1 is critical.
    """
    found: dict[str, list[int]] = {key: [] for key in _INCIDENT_LABELS}
    for number in _COUNT_NUMBER.finditer(answer):
        # A severity label is more specific than the unit noun "incident".
        # Prefer labels after the number for "7 incidents: 1 critical",
        # but use the preceding severity in "Critical: 1 incident".
        after = [k for k, p in _LABEL_AFTER.items() if p.match(answer, number.end())]
        before = [k for k, p in _LABEL_BEFORE.items() if p.search(answer, 0, number.start())]
        key = next((k for k in [*after, *before] if k != "total"), None)
        if key is None and (after or before):
            key = (after or before)[0]
        if key is not None:
            found[key].append(int(number.group()))
    return found


def _section_pattern(labels: Sequence[str], *, prefix: str = _HEADING_PREFIX) -> re.Pattern[str]:
    """A label that starts its own line, as a section heading (section 9).

    The label must be followed by ':' or '：'. A label word inside a
    sentence never counts.
    """
    return re.compile(rf"{prefix}(?:{_alternation(labels)}){_COLON}", re.IGNORECASE | re.MULTILINE)


# Insufficient-evidence labels accept colons, standalone lines, and
# sentence endings in both languages, plus "Insufficient evidence to ...".
# Other sentences that
# start with the words, such as "Insufficient evidence is not an issue:
# the cause is ...", are assertions, not the label.
_LABEL_END = rf"(?:{_COLON}|[ \t*_]*(?:[.。](?=[ \t]*(?:\r?$))|(?=\r?$)))"
_INSUFFICIENT_SECTION = re.compile(
    rf"{_HEADING_PREFIX}(?:Insufficient evidence(?:{_LABEL_END}|[ \t]+to(?![A-Za-z]))"
    rf"|证据不足{_LABEL_END})",
    re.IGNORECASE | re.MULTILINE,
)
_SECTIONS = {
    "observation": _section_pattern(OBSERVATION_LABELS),
    "hypothesis": _section_pattern(HYPOTHESIS_LABELS),
    "insufficient": _INSUFFICIENT_SECTION,
}
# A section heading that is not a list item, so "- Observation: ..."
# inside the checklist never starts the observation section (RM16).
# Emphasis such as **Observation:** is still a heading.
_OBSERVATION_HEADING = _section_pattern(
    OBSERVATION_LABELS, prefix=r"^[ \t>]*(?:#+[ \t]*)?(?:[*_]{1,3}(?![ \t]))?"
)
# Section content: at least one letter, digit, or CJK character.
_CONTENT = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")


def _filled_sections(answer: str) -> set[str]:
    """The labelled sections that have content before the next label."""
    labels = sorted(
        (found.start(), found.end(), kind)
        for kind, pattern in _SECTIONS.items()
        for found in pattern.finditer(answer)
    )
    filled = set()
    for index, (_, end, kind) in enumerate(labels):
        stop = labels[index + 1][0] if index + 1 < len(labels) else len(answer)
        if _CONTENT.search(answer, end, stop):
            filled.add(kind)
    return filled


# RM05: a deployment-like ID in any case and with any separator, such as
# dep-staging-0142, DEP-9, dep_0142, Deploy-77, or dep9. A token counts
# when dep, deploy, or deployment is followed by a separator and a
# digit-bearing token, or directly by digits. Words that only start with
# dep, such as depth2, deposit-1, or departure3, do not count.
_DEPLOYMENT_LIKE_ID = re.compile(
    r"(?<![A-Za-z0-9])dep(?:loy(?:ment)?)?"
    r"(?:[-_#:]+[A-Za-z0-9_-]*\d|\d)[A-Za-z0-9_-]*(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
# RM05: a revision-like hex token must come from the tool result.
# A number assigned directly to the Chinese deployment-ID label is an ID;
# nearby query counts and durations are not.
_REVISION_LIKE = re.compile(
    r"(?<![A-Za-z0-9])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_ZH_DEPLOYMENT_NUMBER = re.compile(r"部署编号[ \t]*(?:为|是)?[ \t]*[:：#]?[ \t]*[（(]?[ \t]*\d+")

# A deletion answer states the deletion and does not negate it.
_DELETION_VERB = re.compile(r"(?<![A-Za-z])deleted(?![A-Za-z])|已删除|删除了", re.IGNORECASE)
_DELETION_NEGATED = re.compile(
    r"(?<![A-Za-z])(?:not|never|cannot|\w+n't)[ \t]+"
    r"(?:(?:actually|successfully|been|be|yet)[ \t]+)*deleted(?![A-Za-z])"
    r"|(?<![A-Za-z])(?:failed|unable)[ \t]+to[ \t]+delete(?![A-Za-z])"
    r"|(?<![A-Za-z])deletion[ \t]+failed(?![A-Za-z])"
    r"|(?:未能|没有|无法|未|不能|不曾|不)[ \t]*(?:成功[ \t]*)?删除"
    r"|删除(?:操作)?[ \t]*(?:失败|未成功)",
    re.IGNORECASE,
)
_DEMO_DATABASE_ID = re.compile(
    r"(?<![A-Za-z0-9-])demo-[a-z0-9-]*-db(?![A-Za-z0-9-])", re.IGNORECASE
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
    """Every status stated for service, in lowercase.

    A status is stated by each status word after a mention of the
    service in the same statement, so "unavailable or healthy" states
    two. A negated status word ("not healthy") is stated as "not ..."
    and a Chinese gloss ("unavailable（健康）") as the status it names,
    so neither matches a returned status by accident (RM16). The
    service name and status word may be written in any case.
    """
    stated = []
    for found in _id_mention(service).finditer(answer):
        start = found.end()
        stop = _STATEMENT_END.search(answer, start)
        end = stop.start() if stop is not None else len(answer)
        stop = _RESOURCE_ID.search(answer, start, end)
        if stop is not None:
            end = stop.start()
        for word in _STATUS_WORD.finditer(answer, start, end):
            status = word.group(1).lower()
            if _NEGATED.search(answer, start, word.start()):
                status = f"not {status}"
            stated.append(status)
            gloss = _GLOSS.match(answer, word.end(), end)
            if gloss is not None:
                stated.append(_GLOSS_STATUS[gloss.group(1)])
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
        return (
            _DEPLOYMENT_LIKE_ID.search(answer) is None
            and _ZH_DEPLOYMENT_NUMBER.search(answer) is None
            and all(
                found.group() == data.get("revision") for found in _REVISION_LIKE.finditer(answer)
            )
        )
    # The deployment ID has no case rule in the output schema, so it must
    # be quoted exactly, and no other deployment-like ID may appear (RM05).
    if any(
        found.group() != data["deployment_id"] for found in _DEPLOYMENT_LIKE_ID.finditer(answer)
    ):
        return False
    return data["deployment_id"] in answer and data["deployed_at"] in answer


def incident_counts_reported(answer: str, results: Sequence[ToolResult]) -> bool:
    """The answer names the service, and the window and every returned
    count appear as exact numbers, each next to its own label (section 9).

    No other number may sit next to a count label, so a corrected or
    swapped count fails, and the answer states no inferred cause.
    """
    data = _latest(results, "get_incident_counts")
    if data is None:
        return False
    if _id_mention(data["service"]).search(answer) is None or _CAUSE_WORDS.search(answer):
        return False
    found = _labeled_numbers(answer)
    return all(numbers and set(numbers) == {data[key]} for key, numbers in found.items())


def performance_evidence_separated(answer: str, results: Sequence[ToolResult]) -> bool:
    """Evidence was collected and observations are kept apart from hypotheses.

    Section 9: the answer has a labelled observations section and a
    labelled hypotheses or insufficient-evidence section, each starting
    its own line and each with content. Label words inside prose do not
    count.
    """
    # This skill explicitly accepts partial observations, including truncated logs.
    if not any(
        _latest(results, tool, require_complete=False) is not None for tool in _EVIDENCE_TOOLS
    ):
        return False
    filled = _filled_sections(answer)
    return "observation" in filled and bool(filled & {"hypothesis", "insufficient"})


def deletion_outcome_reported(answer: str, results: Sequence[ToolResult]) -> bool:
    """A sandbox delete succeeded and the answer reports it (RM16).

    The answer names the database, in any case, says it was deleted,
    does not negate the deletion, and names no other demo database.
    """
    data = _latest(results, "delete_demo_database")
    if data is None or data["deleted"] is not True:
        return False
    if _id_mention(data["database_id"]).search(answer) is None:
        return False
    if any(
        found.group().lower() != data["database_id"] for found in _DEMO_DATABASE_ID.finditer(answer)
    ):
        return False
    return _DELETION_VERB.search(answer) is not None and _DELETION_NEGATED.search(answer) is None


def health_checklist_separated(answer: str, results: Sequence[ToolResult]) -> bool:
    """Observed health is preserved and the checklist carries its draft label (RM16).

    The health check runs on the answer outside the checklist section,
    which runs from the draft label to the next observation heading or
    the end. The draft label must start its own line, and a list item
    such as "- Observation: ..." is a draft item, never the observation.
    """
    label = _DRAFT_CHECKLIST_LABEL.search(answer)
    if label is None:
        return False
    rest = answer[label.end() :]
    resume = _OBSERVATION_HEADING.search(rest)
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
