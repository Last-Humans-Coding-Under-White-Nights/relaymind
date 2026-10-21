"""The six demo skill packages under skills/ (RM23, sections 4.2 and 9).

Section 9: the skill table names six skills and their tools exactly;
each skill includes English examples and completion criteria. Chinese
examples are best-effort under spec 0.7; the demo packages keep theirs,
so these tests still check both languages. Section 4.2: packages pass registry validation from the
default root. The examples are illustrative; each one names the tool
calls it assumes, and its responses must satisfy the skill's registered
completion predicate against the real sandbox tool outputs, so an
example never states a value the sandbox did not return (RM16).
The Chinese examples have not been natively reviewed; spec 0.7
section 9 no longer requires that review, and the evidence says so.
Issue #13.
"""

import json
import re

import pytest

from helpers import make_demo_skills, make_demo_tools
from relaymind.skills.predicates import COMPLETION_PREDICATES
from relaymind.tools import ToolStatus
from relaymind.tools.sandbox import DEMO_TOOL_NAMES

SECTION_9_SKILLS = {
    "check-service-health": ("get_service_health",),
    "lookup-latest-deployment": ("get_latest_deployment",),
    "summarize-incident-counts": ("get_incident_counts",),
    "investigate-performance": ("get_latest_deployment", "get_metrics", "get_recent_logs"),
    "delete-demo-database": ("list_demo_databases", "delete_demo_database"),
    "health-and-diagnostic-checklist": ("get_service_health",),
}

CJK = re.compile(r"[一-鿿]")


@pytest.fixture
def tools(tmp_path):
    return make_demo_tools(tmp_path)


@pytest.fixture
def skills(tools):
    return make_demo_skills(tools)


def examples(skills, name):
    return json.loads(skills.read_resource(name, "examples.json"))


def test_the_six_packages_pass_registry_validation(skills):
    assert set(skills.names()) == set(SECTION_9_SKILLS)


@pytest.mark.parametrize("name", sorted(SECTION_9_SKILLS))
def test_allowed_tools_match_the_section_9_table_exactly(skills, name):
    assert skills.get(name).allowed_tools == SECTION_9_SKILLS[name]


def test_every_demo_tool_is_used_by_some_skill():
    used = {tool for tools in SECTION_9_SKILLS.values() for tool in tools}
    assert used == DEMO_TOOL_NAMES


@pytest.mark.parametrize("name", sorted(SECTION_9_SKILLS))
def test_each_skill_supports_english_and_chinese(skills, name):
    assert skills.get(name).languages == ("en", "zh")


@pytest.mark.parametrize("name", sorted(SECTION_9_SKILLS))
def test_each_skill_has_a_registered_completion_predicate(skills, name):
    assert skills.get(name).completion_predicate in COMPLETION_PREDICATES


def test_each_skill_has_its_own_predicate(skills):
    predicates = [skills.get(name).completion_predicate for name in SECTION_9_SKILLS]
    assert len(set(predicates)) == len(predicates)


def test_only_the_checklist_skill_names_the_parallel_workflow(skills):
    workflows = {name: skills.get(name).parallel_workflow for name in SECTION_9_SKILLS}
    assert workflows == {
        name: (
            "health-and-diagnostic-checklist" if name == "health-and-diagnostic-checklist" else None
        )
        for name in SECTION_9_SKILLS
    }


@pytest.mark.parametrize("name", sorted(SECTION_9_SKILLS))
def test_each_body_states_completion_criteria(skills, name):
    body = skills.get(name).body
    assert "## Completion criteria" in body
    assert "## Examples" in body


@pytest.mark.parametrize("name", sorted(SECTION_9_SKILLS))
def test_each_skill_has_paired_english_and_chinese_examples(skills, name):
    pairs = examples(skills, name)["pairs"]
    assert pairs
    for pair in pairs:
        for language in ("en", "zh"):
            assert pair[language]["request"].strip()
            assert pair[language]["response"].strip()
        assert CJK.search(pair["zh"]["request"])
        assert CJK.search(pair["zh"]["response"])
        assert not CJK.search(pair["en"]["request"])


@pytest.mark.parametrize("name", sorted(SECTION_9_SKILLS))
def test_examples_are_labeled_illustrative(skills, name):
    """Section 12: examples are not measured or live results."""
    assert examples(skills, name)["illustrative"] is True


@pytest.mark.parametrize("name", sorted(SECTION_9_SKILLS))
def test_example_responses_satisfy_the_predicate_against_sandbox_outputs(skills, tools, name):
    """RM16: every value an example states comes from a sandbox tool result."""
    skill = skills.get(name)
    predicate = COMPLETION_PREDICATES[skill.completion_predicate]
    for index, pair in enumerate(examples(skills, name)["pairs"]):
        results = []
        for number, call in enumerate(pair["tool_calls"]):
            assert call["tool"] in skill.allowed_tools
            result = tools.invoke(
                call["tool"],
                call["arguments"],
                available=set(skill.allowed_tools),
                operation_id=f"{name}-{index}-{number}",
            )
            assert result.status is ToolStatus.SUCCEEDED
            results.append(result)
        for language in ("en", "zh"):
            response = pair[language]["response"]
            assert predicate(response, results), f"{name} pair {index} {language}"


# ASCII word characters only: CJK counts as \w in Python, and a number
# written right next to Chinese text ("共5个") must still be found.
NUMBER = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?(?![A-Za-z0-9_.])")


def _stated_numbers(value):
    if isinstance(value, bool):
        return set()
    if isinstance(value, int | float):
        return {repr(float(value))}
    if isinstance(value, str):
        return {repr(float(n)) for n in NUMBER.findall(value)}
    if isinstance(value, dict):
        return set().union(*(_stated_numbers(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_stated_numbers(v) for v in value))
    return set()


@pytest.mark.parametrize("name", sorted(SECTION_9_SKILLS))
def test_example_responses_state_no_number_the_tools_did_not_return(skills, tools, name):
    """RM16: every number in an example response comes from a tool call or its result."""
    skill = skills.get(name)
    for index, pair in enumerate(examples(skills, name)["pairs"]):
        known: set[str] = set()
        for number, call in enumerate(pair["tool_calls"]):
            result = tools.invoke(
                call["tool"],
                call["arguments"],
                available=set(skill.allowed_tools),
                operation_id=f"{name}-{index}-{number}",
            )
            known |= _stated_numbers(call["arguments"]) | _stated_numbers(result.data)
            known |= {repr(float(len(result.data.get("entries", []))))}
        for language in ("en", "zh"):
            stated = _stated_numbers(pair[language]["response"])
            assert stated <= known, f"{name} pair {index} {language}: {sorted(stated - known)}"


def test_local_eligibility_matches_the_section_9_routes(skills):
    """Section 9, RM04: investigate-performance ends in an external
    evidence-based assessment and the checklist workflow in an external
    synthesis, so neither is local-eligible; the other four complete
    locally."""
    eligible = {name: skills.get(name).local_eligible for name in SECTION_9_SKILLS}
    assert eligible == {
        name: name not in {"investigate-performance", "health-and-diagnostic-checklist"}
        for name in SECTION_9_SKILLS
    }


@pytest.mark.parametrize(
    ("text", "numbers"),
    [
        ("共5个事件，其中严重1个", {5.0, 1.0}),
        ("错误率为0.41。", {0.41}),
        ("window 60, total 7", {60.0, 7.0}),
        ("p95 and ch_demo_1 and v2", set()),
    ],
)
def test_stated_numbers_are_found_next_to_chinese_text(text, numbers):
    """RM16: a number written right next to CJK text is still a stated number."""
    assert _stated_numbers(text) == {repr(n) for n in numbers}
