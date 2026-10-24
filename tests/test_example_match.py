"""The example_match selector (RM01, RM05, RM28; spec 0.32 section 14.1).

Deterministic matching of a request against the offered skills' example
requests and descriptions in both languages, identifiers masked; no
model call. Its accuracy is measured (section 12), never assumed: the
leave-one-out figures below record what the method gives on the demo
examples, they are not a target.
"""

import asyncio
import json
import os
import subprocess
import sys

import pytest

from gateway_helpers import build_gateway, health_tail, purposes, run
from helpers import REPO_ROOT, make_demo_skills, make_demo_tools, of_type
from relaymind.orchestrator.run_state import RunState
from relaymind.selection import (
    EXAMPLE_MATCH,
    ExampleMatchSelector,
    SelectionContext,
    SelectionSettings,
    catalog_entries,
    parse_selection_request,
    parse_selection_result,
)
from relaymind.selection.example_match import example_requests

HEALTH = "check-service-health"
SCORE, MARGIN = 0.5, 0.1


@pytest.fixture
def demo(tmp_path):
    tools = make_demo_tools(tmp_path)
    skills = make_demo_skills(tools)
    return tools, skills, catalog_entries(skills, skills.names())


def request(text, catalog, language="en"):
    return parse_selection_request(
        {
            "schema_version": 1,
            "run_id": "run_1",
            "conversation": [{"role": "user", "content": text}],
            "language": language,
            "execution_mode": "auto",
            "catalog": catalog,
            "permissions": ["demo.read"],
            "deadline_ms": 2000,
        }
    )


def selector(tools, score=SCORE, margin=MARGIN):
    return ExampleMatchSelector(tools, score=score, margin=margin)


def select(matcher, text, catalog):
    raw = asyncio.run(matcher.select(request(text, catalog), SelectionContext()))
    return parse_selection_result(raw)


def language_of(text):
    return "zh" if any("一" <= ch <= "鿿" for ch in text) else "en"


def test_the_catalog_carries_the_example_requests_of_both_languages(demo):
    """RM28: examples.json requests, read through the registry, en and zh."""
    _, skills, catalog = demo
    entry = next(e for e in catalog if e["id"] == HEALTH)
    text = skills.read_resource(HEALTH, "examples.json")
    assert entry["examples"] == example_requests(text)
    assert {language_of(e) for e in entry["examples"]} == {"en", "zh"}
    assert all(e["examples"] for e in catalog)


def test_leave_one_out_on_the_demo_examples(demo):
    """RM28, section 12: each example request held out of its skill's index in turn.

    The figures are the measured result of the #118 method on the twenty
    demo example requests at the default thresholds, recorded so a change
    of the method shows here. They are not a target, and they are why the
    default chain stays model_router: with two pairs per skill the matcher
    often finds no unique skill, and it picks a wrong one twice.
    """
    tools, _, catalog = demo
    counts = {"en": {"right": 0, "wrong": 0, "none": 0}, "zh": {"right": 0, "wrong": 0, "none": 0}}
    top_first = 0
    for entry in catalog:
        for held_out in entry["examples"]:
            reduced = [
                (
                    {
                        **other,
                        "examples": [e for e in other["examples"] if e != held_out],
                        "content_hash": other["content_hash"] + "-without-one",
                    }
                    if other["id"] == entry["id"]
                    else other
                )
                for other in catalog
            ]
            found = selector(tools).match(request(held_out, reduced))
            outcome = (
                "none"
                if found.skill is None
                else ("right" if found.skill == entry["id"] else "wrong")
            )
            counts[language_of(held_out)][outcome] += 1
            top_first += found.top == entry["id"]
    assert counts == {
        "en": {"right": 4, "wrong": 1, "none": 5},
        "zh": {"right": 1, "wrong": 1, "none": 8},
    }
    assert top_first == 13


@pytest.mark.parametrize(
    "text",
    [
        "What is the weather in Paris tomorrow?",
        "Write me a poem about cats.",
        "Tell me a joke.",
        "今天天气怎么样？",
        "",
    ],
)
def test_an_unrelated_request_returns_none(demo, text):
    tools, _, catalog = demo
    result = select(selector(tools), text, catalog)
    assert result.candidate.kind == "none"
    assert result.confidence is None


def test_changing_only_the_service_name_keeps_the_pick_and_the_score(demo):
    """RM05: identifiers are masked, so the service never decides the skill."""
    tools, _, catalog = demo
    matcher = selector(tools, score=0.0, margin=0.0)
    pairs = 0
    for entry in catalog:
        for text in entry["examples"]:
            if "staging-api" not in text:
                continue
            swapped = text.replace("staging-api", "payments-api")
            assert matcher.match(request(text, catalog)) == matcher.match(request(swapped, catalog))
            pairs += 1
    assert pairs >= 8


def test_a_request_from_the_examples_selects_its_skill_with_a_confidence(demo):
    tools, _, catalog = demo
    result = select(selector(tools), "When was payments-api last deployed?", catalog)
    assert result.candidate.skill_id == "lookup-latest-deployment"
    assert result.mechanism == EXAMPLE_MATCH
    assert 0.5 <= result.confidence <= 1
    assert result.model_calls == ()
    assert result.candidate.skill_id not in result.alternatives


def test_scores_are_in_the_unit_interval_and_ranked(demo):
    """Section 14.1: normalized to [0, 1]; ranked by score, then by skill ID."""
    tools, _, catalog = demo
    for entry in catalog:
        for text in entry["examples"]:
            ranked = selector(tools).match(request(text, catalog)).ranked
            assert all(0 <= score <= 1 for _, score in ranked)
            assert list(ranked) == sorted(ranked, key=lambda item: (-item[1], item[0]))


def test_the_thresholds_come_from_the_configuration(demo):
    """Section 14.1: a unique best skill at or above both thresholds, otherwise none."""
    tools, _, catalog = demo
    # Not an example request itself, so its score is under the cap of 1.
    text = "What was the most recent deployment of payments-api?"
    found = selector(tools).match(request(text, catalog))
    assert found.score < 1
    assert found.skill == "lookup-latest-deployment"
    assert selector(tools, score=found.score).match(request(text, catalog)).skill is not None
    assert (
        selector(tools, score=min(1.0, found.score + 0.01)).match(request(text, catalog)).skill
        is None
    )
    assert selector(tools, margin=found.margin).match(request(text, catalog)).skill is not None
    assert (
        selector(tools, margin=min(1.0, found.margin + 0.01)).match(request(text, catalog)).skill
        is None
    )


def test_a_one_skill_catalog_compares_with_zero(demo):
    """Section 14.1: the second-best score of a one-skill catalog is 0."""
    tools, _, catalog = demo
    only = [e for e in catalog if e["id"] == HEALTH]
    found = selector(tools).match(request("Check the health of staging-api.", only))
    assert found.skill == HEALTH
    assert found.margin == found.score


def test_a_tie_for_the_top_score_is_none(demo):
    tools, _, catalog = demo
    entry = next(e for e in catalog if e["id"] == HEALTH)
    twin = {**entry, "id": "check-service-health-twin"}
    found = selector(tools, score=0.0, margin=0.0).match(
        request("Check the health of staging-api.", [entry, twin])
    )
    assert found.margin == 0 and found.skill is None


def test_the_index_is_rebuilt_only_when_a_content_hash_changes(demo):
    """RM07, RM24: the index follows the catalog's skill IDs and content hashes."""
    tools, _, catalog = demo
    matcher = selector(tools)
    matcher.match(request("Is staging-api healthy right now?", catalog))
    matcher.match(request("When was payments-api last deployed?", catalog))
    assert matcher.index_builds == 1
    changed = [
        {**e, "content_hash": e["content_hash"] + "x", "examples": ["Ping the fleet."]}
        if e["id"] == HEALTH
        else e
        for e in catalog
    ]
    assert matcher.match(request("Ping the fleet.", changed)).skill == HEALTH
    assert matcher.index_builds == 2


def test_the_match_is_deterministic_across_processes(demo):
    """The score sums run in sorted order, so the hash seed changes nothing."""
    script = (
        "import json, sys\n"
        "sys.path.insert(0, 'tests')\n"
        "from pathlib import Path\n"
        "import tempfile\n"
        "from helpers import make_demo_skills, make_demo_tools\n"
        "from relaymind.selection import ExampleMatchSelector, catalog_entries, "
        "parse_selection_request\n"
        "tools = make_demo_tools(Path(tempfile.mkdtemp()))\n"
        "skills = make_demo_skills(tools)\n"
        "catalog = catalog_entries(skills, skills.names())\n"
        "matcher = ExampleMatchSelector(tools, score=0.5, margin=0.1)\n"
        "out = []\n"
        "for entry in catalog:\n"
        "    for text in entry['examples']:\n"
        "        request = parse_selection_request({'schema_version': 1, 'run_id': 'r',"
        " 'conversation': [{'role': 'user', 'content': text}], 'language': 'en',"
        " 'execution_mode': 'auto', 'catalog': catalog, 'permissions': [],"
        " 'deadline_ms': None})\n"
        "        out.append([list(map(list, matcher.match(request).ranked))])\n"
        "print(json.dumps(out))\n"
    )
    outputs = set()
    for seed in ("0", "1", "2"):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.add(completed.stdout)
    assert len(outputs) == 1
    assert json.loads(outputs.pop())


def test_example_match_in_the_gateway_makes_no_model_call(tmp_path):
    """RM01, RM28: chain ["example_match"]: the gateway admits its pick; no routing generation."""
    gateway = build_gateway(
        tmp_path,
        health_tail(),
        external_turns=[],
        selection=SelectionSettings(chain=(EXAMPLE_MATCH,)),
    )
    outcome, events = run(gateway, "Is staging-api healthy right now?")
    assert outcome.state is RunState.COMPLETED
    assert "routing" not in purposes(events)
    (route,) = of_type(events, "route.selected")
    assert route.actor == "gateway"
    assert route.payload["mechanism"] == EXAMPLE_MATCH
    assert route.payload["decision"]["skill_id"] == "check-service-health"
    assert route.payload["selector"]["name"] == EXAMPLE_MATCH
    assert route.payload["selector"]["attempts"] == [
        {
            "name": EXAMPLE_MATCH,
            "outcome": "selected",
            "version": "1",
            "transport": "in_process",
            "elapsed_ms": 0,
            "model_calls_claim": None,
        }
    ]
    (accepted,) = of_type(events, "run.accepted")
    assert accepted.payload["selection"] == [EXAMPLE_MATCH]


def test_the_gateway_uses_the_configured_thresholds(tmp_path):
    """Section 14.1: a threshold no score reaches leaves the chain with nothing."""
    gateway = build_gateway(
        tmp_path,
        [],
        selection=SelectionSettings(chain=(EXAMPLE_MATCH,), example_match_score=1.0),
    )
    outcome, events = run(gateway, "What was the most recent deployment of payments-api?")
    assert outcome.state is RunState.FAILED
    (route,) = of_type(events, "route.selected")
    assert route.payload["decision"]["reason_code"] == "selection_failed"
    (accepted,) = of_type(events, "run.accepted")
    assert accepted.payload["example_match_score"] == 1.0
