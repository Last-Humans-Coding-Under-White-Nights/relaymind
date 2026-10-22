"""Skill parse and template cache by content hash (RM07, RM24; spec 0.26 section 10)."""

import json
import shutil

import pytest

from helpers import BASELINE, DEMO_SKILLS_ROOT
from relaymind.orchestrator.workflows import PARALLEL_WORKFLOWS
from relaymind.skills import ContentCache, SkillPackageError, SkillRegistry
from relaymind.skills.predicates import COMPLETION_PREDICATES
from relaymind.tools import ToolRegistry
from relaymind.tools.sandbox import SandboxDataService, register_demo_tools


@pytest.fixture
def tools(tmp_path):
    registry = ToolRegistry()
    register_demo_tools(registry, SandboxDataService(BASELINE, work_dir=tmp_path / "sandbox"))
    return registry


@pytest.fixture
def root(tmp_path):
    target = tmp_path / "skills"
    shutil.copytree(DEMO_SKILLS_ROOT, target)
    return target


def load(root, tools, **kwargs):
    return SkillRegistry.load(
        [root],
        tools=tools,
        predicates=COMPLETION_PREDICATES.keys(),
        workflows=PARALLEL_WORKFLOWS,
        **kwargs,
    )


def test_content_cache_parses_equal_bytes_once():
    """RM24: the same bytes are parsed once; changed bytes are a new key."""
    cache = ContentCache()
    calls = []

    def parse(data):
        calls.append(data)
        return data.upper()

    assert cache.get_or_parse("k", b"abc", parse) == b"ABC"
    assert cache.get_or_parse("k", b"abc", parse) == b"ABC"
    assert cache.get_or_parse("k", b"abd", parse) == b"ABD"
    assert len(calls) == 2
    assert (cache.hits, cache.misses) == (1, 2)


def test_content_cache_never_keeps_a_failed_parse():
    """RM24: an invalid file is refused on every load."""
    cache = ContentCache()

    def parse(data):
        raise ValueError("bad")

    for _ in range(2):
        with pytest.raises(ValueError):
            cache.get_or_parse("k", b"x", parse)
    assert cache.misses == 2 and len(cache) == 0


def test_content_cache_is_bounded():
    cache = ContentCache(max_entries=2)
    for data in (b"a", b"b", b"c"):
        cache.get_or_parse("k", data, lambda d: d)
    assert len(cache) == 2


def test_reload_of_unchanged_packages_parses_nothing_again(root, tools):
    """RM24: a second load hits the cache for every file and yields equal skills."""
    first = load(root, tools)
    cache = first.parse_cache
    misses = cache.misses
    assert misses > 0 and cache.hits == 0
    second = first.reloaded()
    assert cache.misses == misses
    assert cache.hits == misses
    for name in first.names():
        assert second.get(name).content_hash == first.get(name).content_hash
        assert second.get(name).body == first.get(name).body


def test_changed_skill_is_parsed_again_and_gets_a_new_hash(root, tools):
    """RM07, RM24: edited bytes miss the cache; the other packages still hit."""
    first = load(root, tools)
    path = root / "check-service-health" / "SKILL.md"
    path.write_text(path.read_text("utf-8") + "\nAlso mention the time.\n", "utf-8")
    cache = first.parse_cache
    before = (cache.hits, cache.misses)
    second = first.reloaded()
    assert cache.misses == before[1] + 1
    assert cache.hits > before[0]
    name = "check-service-health"
    assert second.get(name).content_hash != first.get(name).content_hash
    assert "Also mention the time." in second.get(name).body
    assert "Also mention the time." not in first.get(name).body


def test_changed_resource_text_is_loaded_again(root, tools):
    """RM24: template and example text is cached by content hash too."""
    first = load(root, tools)
    path = root / "check-service-health" / "examples.json"
    path.write_text(path.read_text("utf-8") + " ", "utf-8")
    second = first.reloaded()
    name = "check-service-health"
    assert second.read_resource(name, "examples.json") != first.read_resource(name, "examples.json")
    assert second.get(name).content_hash != first.get(name).content_hash


def test_invalid_edit_is_refused_after_a_cached_load(root, tools):
    """RM24: the cache never lets a broken file through."""
    first = load(root, tools)
    manifest = root / "check-service-health" / "skill.manifest.json"
    data = json.loads(manifest.read_text("utf-8"))
    data["allowed_tools"] = ["no_such_tool"]
    manifest.write_text(json.dumps(data), "utf-8")
    with pytest.raises(SkillPackageError):
        first.reloaded()


def test_a_tool_schema_check_runs_on_every_load(root, tools):
    """RM24: a cached parse is still validated against the registered tools."""
    first = load(root, tools)
    empty = ToolRegistry()
    with pytest.raises(SkillPackageError):
        SkillRegistry.load(
            [root],
            tools=empty,
            predicates=COMPLETION_PREDICATES.keys(),
            workflows=PARALLEL_WORKFLOWS,
            parse_cache=first.parse_cache,
        )


def test_reload_reads_the_directories_first_scanned(root, tools, tmp_path, monkeypatch):
    """RM07, spec 0.26 section 10: reloaded() re-reads the same roots, not a new cwd's."""
    monkeypatch.chdir(root.parent)
    first = SkillRegistry.load(
        ["skills"],
        tools=tools,
        predicates=COMPLETION_PREDICATES.keys(),
        workflows=PARALLEL_WORKFLOWS,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    second = first.reloaded()
    assert second.names() == first.names()
    assert second.roots == first.roots
    # run.accepted records the operator's strings, unchanged by a reload (section 8).
    assert second.configured_roots == first.configured_roots == ("skills",)


def test_reload_keeps_the_registry_class(root, tools):
    """Spec 0.26 section 10: a reload gives a registry of the same kind."""

    class Custom(SkillRegistry):
        pass

    first = Custom.load(
        [root], tools=tools, predicates=COMPLETION_PREDICATES.keys(), workflows=PARALLEL_WORKFLOWS
    )
    assert type(first.reloaded()) is Custom
