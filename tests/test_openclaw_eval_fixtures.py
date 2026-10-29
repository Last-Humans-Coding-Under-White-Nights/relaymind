"""The frozen OpenClaw evaluation tasks (spec 0.50 section 12.5, #139).

RM16, RM25, RM33; AC24. Section 12.5 freezes bilingual text and sandbox
host-tool tasks, their expected tool names and arguments, the host tool
policy, the prompts, the repeat count, and the sampling order before any
measurement. The tasks are one versioned file with its digest in a lock
file, apart from the managed-skill corpus of section 12.2, whose files stay
unchanged.
"""

import copy
import json
from pathlib import Path

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation import openclaw
from relaymind.evaluation.corpus import load_corpus
from relaymind.evaluation.openclaw import (
    ARMS,
    HOST_TOOLS,
    TaskSetError,
    argument_matches,
    check_lock,
    load_tasks,
    lock_document,
    parse_tasks,
    sampling_order,
)

REPO = Path(__file__).resolve().parent.parent
TASKS = REPO / "evaluation" / "openclaw_tasks.v1.json"
LOCK = REPO / "evaluation" / "openclaw_tasks.v1.lock.json"
CORPUS = REPO / "evaluation" / "corpus.v1.json"
CORPUS_LOCK = REPO / "evaluation" / "corpus.v1.lock.json"
SEEDS = REPO / "tests" / "fixtures" / "sandbox"


@pytest.fixture(scope="module")
def tasks():
    return load_tasks(TASKS)


def document():
    return json.loads(TASKS.read_text("utf-8"))


def test_the_tasks_match_their_frozen_lock(tasks):
    check_lock(tasks, LOCK)
    assert json.loads(LOCK.read_text("utf-8")) == lock_document(tasks, TASKS.name)
    assert tasks.digest == canonical_digest(document())


def test_a_changed_task_is_refused_by_the_lock(tmp_path):
    changed = document()
    changed["tasks"][0]["prompt"] += " Please."
    with pytest.raises(TaskSetError, match="new version"):
        check_lock(parse_tasks(changed), LOCK)


def test_the_file_freezes_the_arms_repeats_seed_time_limit_and_host(tasks):
    assert (
        tasks.arms
        == ARMS
        == (
            "direct",
            "relaymind_external_only",
            "relaymind_auto",
            "relaymind_no_local",
        )
    )
    assert tasks.repeats >= 1
    assert isinstance(tasks.sampling_seed, int)
    assert tasks.time_limit_seconds > 0
    assert tasks.host["release"] == "2026.10.7"
    assert tasks.host["command"] == ["agent", "exec"]


def test_every_english_task_has_a_best_effort_chinese_pair(tasks):
    english = {t.task_id: t for t in tasks.tasks if t.language == "en"}
    chinese = [t for t in tasks.tasks if t.language == "zh"]
    assert english and chinese
    assert all(not t.best_effort and t.english_task is None for t in english.values())
    paired = {t.english_task for t in chinese}
    assert paired == set(english)
    for task in chinese:
        source = english[task.english_task]
        assert task.best_effort
        assert task.kind == source.kind
        assert task.exec_policy == source.exec_policy
        assert task.expected_tools == source.expected_tools
        assert task.expected_sandbox == source.expected_sandbox
        assert task.prompt != source.prompt


def test_both_kinds_and_both_exec_policies_are_covered(tasks):
    assert {t.kind for t in tasks.tasks} == {"text", "host_tool"}
    host_tasks = [t for t in tasks.tasks if t.kind == "host_tool"]
    assert {t.exec_policy for t in host_tasks} == {"deny", "full"}
    # A denied mutation and an allowed one: the host's policy, not RelayMind, decides.
    exec_tasks = [
        t for t in host_tasks if any(c["tool"] == "exec" for g in t.expected_tools for c in g)
    ]
    assert {t.exec_policy for t in exec_tasks} == {"deny", "full"}


def test_tool_tasks_name_only_host_tools_and_act_inside_the_sandbox(tasks):
    for task in tasks.tasks:
        if task.kind == "text":
            assert task.expected_tools == ()
            continue
        assert task.expected_tools, task.task_id
        for group in task.expected_tools:
            for call in group:
                assert call["tool"] in HOST_TOOLS, task.task_id
        for name in (*task.sandbox, *task.expected_sandbox):
            assert "/" not in name and "\\" not in name and name not in (".", "..")


def test_every_task_states_its_evidence_and_rubric(tasks):
    for task in tasks.tasks:
        assert task.rubric.strip()
        assert task.required_evidence or task.expected_sandbox, task.task_id


def test_no_task_reuses_a_corpus_prompt_and_the_corpus_stays_frozen(tasks):
    """Corpus scores are never host task results (section 12.5)."""
    corpus = load_corpus(CORPUS, seed_dir=SEEDS)
    corpus_prompts = {case.prompt for case in corpus.cases}
    assert not corpus_prompts & {t.prompt for t in tasks.tasks}
    assert json.loads(CORPUS_LOCK.read_text("utf-8"))["sha256"] == corpus.digest


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d.update(extra=1), "unknown"),
        (lambda d: d["tasks"][0].update(extra=1), "unknown"),
        (lambda d: d.update(arms=["direct"]), "arms"),
        (lambda d: d["tasks"][0].update(exec_policy="ask"), "exec_policy"),
        (lambda d: d["tasks"][0].update(language="fr"), "language"),
        (lambda d: d["tasks"].append(copy.deepcopy(d["tasks"][0])), "duplicate"),
        (lambda d: d.update(repeats=0), "repeats"),
        (lambda d: d.update(schema_version=2), "schema_version"),
    ],
)
def test_a_malformed_task_file_is_refused(mutate, match):
    changed = document()
    mutate(changed)
    with pytest.raises(TaskSetError, match=match):
        parse_tasks(changed)


def test_a_tool_outside_the_host_catalog_is_refused():
    changed = document()
    task = next(t for t in changed["tasks"] if t["kind"] == "host_tool")
    task["expected_tools"][0][0]["tool"] = "get_service_health"
    with pytest.raises(TaskSetError, match="host tool"):
        parse_tasks(changed)


def test_a_sandbox_path_outside_the_working_directory_is_refused():
    changed = document()
    task = next(t for t in changed["tasks"] if t["sandbox"])
    task["sandbox"]["../escape.txt"] = "x"
    with pytest.raises(TaskSetError, match="sandbox"):
        parse_tasks(changed)


def test_a_chinese_task_must_name_an_english_task():
    changed = document()
    task = next(t for t in changed["tasks"] if t["language"] == "zh")
    task["english_task"] = "missing-en"
    with pytest.raises(TaskSetError, match="english_task"):
        parse_tasks(changed)


# -- the sampling order ----------------------------------------------------------------


def test_the_sampling_order_holds_every_slot_once_with_a_tasks_arms_adjacent(tasks):
    order = sampling_order(tasks)
    slots = [(s.repeat, s.task_id, s.arm) for s in order]
    assert len(slots) == len(set(slots)) == tasks.repeats * len(tasks.tasks) * len(ARMS)
    assert [s.index for s in order] == list(range(len(order)))
    for start in range(0, len(order), len(ARMS)):
        block = order[start : start + len(ARMS)]
        assert len({(s.repeat, s.task_id) for s in block}) == 1
        assert {s.arm for s in block} == set(ARMS)


def test_the_sampling_order_is_frozen_by_the_seed(tasks):
    """The same file gives the same order on any machine; another seed another order."""
    first = [(s.repeat, s.task_id, s.arm) for s in sampling_order(tasks)]
    assert first == [(s.repeat, s.task_id, s.arm) for s in sampling_order(load_tasks(TASKS))]
    reseeded = document()
    reseeded["sampling_seed"] += 1
    other = [(s.repeat, s.task_id, s.arm) for s in sampling_order(parse_tasks(reseeded))]
    assert sorted(other) == sorted(first) and other != first
    # Not Python's random module: its algorithms may change between versions.
    assert canonical_digest(first) == json.loads(LOCK.read_text("utf-8"))["order_sha256"]


def test_the_arms_are_shuffled_within_a_task_not_always_in_one_order(tasks):
    blocks = [
        tuple(s.arm for s in sampling_order(tasks)[i : i + len(ARMS)])
        for i in range(0, len(sampling_order(tasks)), len(ARMS))
    ]
    assert len(set(blocks)) > 1


# -- argument patterns -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "value", "expected"),
    [
        ("*", {"anything": 1}, True),
        ({"exact": "notes.txt"}, "notes.txt", True),
        ({"exact": "notes.txt"}, "./notes.txt", False),
        ({"suffix": "notes.txt"}, "/tmp/sandbox/notes.txt", True),
        ({"suffix": "notes.txt"}, "notes.txt.bak", False),
        ({"contains": "log.txt"}, "echo ready >> log.txt", True),
        ({"contains": "log.txt"}, None, False),
        ({"suffix": "x"}, 3, False),
    ],
)
def test_argument_patterns(pattern, value, expected):
    assert argument_matches(pattern, value) is expected


def test_the_module_names_the_observed_host_tool_catalog():
    """The eleven tools the pinned host sent on the macOS probe (section 12.5)."""
    assert HOST_TOOLS == frozenset(
        {
            "apply_patch",
            "edit",
            "exec",
            "ls",
            "process",
            "read",
            "sessions_yield",
            "tool_call",
            "tool_describe",
            "tool_search",
            "write",
        }
    )
    assert openclaw.TASKS_FILE == "evaluation/openclaw_tasks.v1.json"
