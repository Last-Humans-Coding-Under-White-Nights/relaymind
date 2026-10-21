"""Skill registry: SKILL.md packages, registry manifests, and skill roots.

RM06: a skill has a stable name, description, version, content hash, and
an instruction body. The package is a SKILL.md with Agent Skills name
and description frontmatter plus a separate RelayMind registry manifest
(version, supported languages, local eligibility, allowed tool IDs,
resource paths, completion predicate ID, optional parallel-workflow ID).
Completion predicates are registered gateway validators. Every load
emits skill.loaded with the name, version, and hash. Resource reads are
restricted to the package directory.

Section 4.2 (spec 0.4, issue #52): skill roots are operator
configuration; `skills/` is the default root, further local roots come
in a fixed order, each is scanned one level deep, a malformed package is
a startup error, names are unique across roots, roots are local paths,
and each run records the roots and the name, version, and hash of every
skill offered.

RM07: updating a skill or its tool schema changes its version or hash;
a required missing version fails explicitly with no substitution.
Issue #9.
"""

import json
import os
from pathlib import Path

import pytest

from helpers import ACCEPTED_PAYLOAD, SERVICE_INPUT_SCHEMA, make_demo_tools, make_tool_declaration
from relaymind.orchestrator.workflows import PARALLEL_WORKFLOWS
from relaymind.skills import (
    MANIFEST_FILENAME,
    SKILL_MANIFEST_SCHEMA,
    SkillConfigError,
    SkillNotFoundError,
    SkillPackageError,
    SkillRegistry,
    SkillResourceError,
    SkillVersionMissingError,
    default_skill_roots,
)
from relaymind.skills.predicates import COMPLETION_PREDICATES
from relaymind.store.trace_event import validate_trace_event
from relaymind.tools import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parent.parent

PREDICATES = frozenset({"health_status_preserved"})
WORKFLOWS = frozenset({"health-and-diagnostic-checklist"})


def make_tools(input_schema=SERVICE_INPUT_SCHEMA) -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(make_tool_declaration(name="get_service_health", input_schema=input_schema))
    return tools


def manifest(**overrides) -> dict:
    data = {
        "schema_version": 1,
        "version": "1.0.0",
        "languages": ["en", "zh"],
        "local_eligible": True,
        "allowed_tools": ["get_service_health"],
        "resources": ["examples.json"],
        "completion_predicate": "health_status_preserved",
        "parallel_workflow": None,
    }
    data.update(overrides)
    return data


SKILL_MD = """---
name: {name}
description: Check the health of one demo service and report its exact status.
---

# Check service health

Call get_service_health with the service the user named.
"""


def write_package(
    root: Path,
    name: str = "check-service-health",
    *,
    skill_md: str | None = None,
    manifest_data: dict | None = None,
    files: dict[str, str] | None = None,
) -> Path:
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        skill_md if skill_md is not None else SKILL_MD.format(name=name), "utf-8"
    )
    (package / MANIFEST_FILENAME).write_text(json.dumps(manifest_data or manifest()), "utf-8")
    for relpath, text in (files if files is not None else {"examples.json": "[]"}).items():
        target = package / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8")
    return package


def load(roots, tools=None) -> SkillRegistry:
    return SkillRegistry.load(
        roots, tools=tools or make_tools(), predicates=PREDICATES, workflows=WORKFLOWS
    )


class Events:
    def __init__(self):
        self.items = []

    def __call__(self, event_type, payload):
        self.items.append((event_type, payload))


# --- RM06: a valid package --------------------------------------------------


def test_a_valid_package_exposes_name_description_version_and_hash(tmp_path):
    write_package(tmp_path)
    registry = load([tmp_path])
    assert registry.names() == ("check-service-health",)
    [entry] = registry.catalog()
    assert entry.name == "check-service-health"
    assert entry.description.startswith("Check the health")
    skill = registry.get("check-service-health")
    assert skill.version == "1.0.0"
    assert skill.content_hash.startswith("sha256:") and len(skill.content_hash) == 71
    assert skill.languages == ("en", "zh")
    assert skill.allowed_tools == ("get_service_health",)
    assert skill.local_eligible is True
    assert skill.completion_predicate == "health_status_preserved"
    assert skill.parallel_workflow is None


def test_loading_a_skill_returns_its_body_and_emits_skill_loaded(tmp_path):
    """RM06: every load emits skill.loaded with name, version, and hash."""
    write_package(tmp_path)
    registry = load([tmp_path])
    events = Events()
    loaded = registry.load_skill("check-service-health", emit=events)
    assert loaded.body.startswith("# Check service health")
    assert "---" not in loaded.body.splitlines()[0]
    assert events.items == [
        (
            "skill.loaded",
            {
                "name": "check-service-health",
                "version": "1.0.0",
                "content_hash": loaded.content_hash,
            },
        )
    ]
    registry.load_skill("check-service-health", emit=events)
    assert len(events.items) == 2


def test_the_skill_loaded_payload_fits_the_trace_event_schema(tmp_path):
    write_package(tmp_path)
    events = Events()
    load([tmp_path]).load_skill("check-service-health", emit=events)
    event_type, payload = events.items[0]
    validate_trace_event(
        {
            "schema_version": 1,
            "run_id": "run_1",
            "seq": 2,
            "timestamp": "2026-10-20T03:00:00Z",
            "elapsed_ms": 0,
            "event_type": event_type,
            "actor": "gateway",
            "payload": payload,
        }
    )


def test_the_manifest_schema_is_published_and_rejects_unknown_fields():
    assert SKILL_MANIFEST_SCHEMA["$id"].endswith("skill_manifest.v1.json")
    assert SKILL_MANIFEST_SCHEMA["additionalProperties"] is False
    published = json.loads(
        (REPO_ROOT / "src/relaymind/skills/schemas/skill_manifest.v1.json").read_text("utf-8")
    )
    assert published == SKILL_MANIFEST_SCHEMA


def test_frontmatter_allowed_tools_never_grants_permissions(tmp_path):
    """S7: allowed-tools is experimental; permissions come from the manifest."""
    skill_md = SKILL_MD.format(name="check-service-health").replace(
        "description:", "allowed-tools: delete_demo_database run_shell\ndescription:"
    )
    write_package(tmp_path, skill_md=skill_md)
    assert load([tmp_path]).get("check-service-health").allowed_tools == ("get_service_health",)


# --- RM06: malformed frontmatter and manifests -----------------------------


@pytest.mark.parametrize(
    ("skill_md", "message"),
    [
        ("# No frontmatter\n\nBody.\n", "frontmatter"),
        ("---\nname: check-service-health\ndescription: x\n", "frontmatter"),
        ("---\n- just\n- a list\n---\nBody\n", "frontmatter"),
        ("---\nname: [unclosed\n---\nBody\n", "frontmatter"),
        ("---\ndescription: Something.\n---\nBody\n", "name"),
        ("---\nname: check-service-health\n---\nBody\n", "description"),
        ("---\nname: check-service-health\ndescription: '  '\n---\nBody\n", "description"),
        ("---\nname: other-skill\ndescription: Something.\n---\nBody\n", "directory"),
        ("---\nname: Check_Health\ndescription: Something.\n---\nBody\n", "name"),
        ("---\nname: check-service-health\ndescription: Something.\n---\n\n  \n", "body"),
        (
            "---\nname: check-service-health\ndescription: Something.\nmodel: gpt-x\n---\nBody\n",
            "model",
        ),
    ],
)
def test_malformed_frontmatter_is_a_startup_error(tmp_path, skill_md, message):
    write_package(tmp_path, skill_md=skill_md)
    with pytest.raises(SkillPackageError, match=message):
        load([tmp_path])


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"version": ""}, "version"),
        ({"version": "latest"}, "version"),
        ({"languages": []}, "languages"),
        ({"languages": ["en", "fr"]}, "languages"),
        ({"local_eligible": "yes"}, "local_eligible"),
        ({"allowed_tools": ["get_service_health", "get_service_health"]}, "allowed_tools"),
        ({"completion_predicate": None}, "completion_predicate"),
        ({"schema_version": 2}, "schema_version"),
        ({"install": "pip install requests"}, "install"),
    ],
)
def test_malformed_manifests_are_startup_errors(tmp_path, overrides, message):
    write_package(tmp_path, manifest_data=manifest(**overrides))
    with pytest.raises(SkillPackageError, match=message):
        load([tmp_path])


def test_a_manifest_without_a_version_fails_explicitly(tmp_path):
    """RM07: a missing required version is an explicit failure."""
    data = manifest()
    del data["version"]
    write_package(tmp_path, manifest_data=data)
    with pytest.raises(SkillPackageError, match="version"):
        load([tmp_path])


def test_a_manifest_that_is_not_json_is_a_startup_error(tmp_path):
    package = write_package(tmp_path)
    (package / MANIFEST_FILENAME).write_text("version: 1.0.0", "utf-8")
    with pytest.raises(SkillPackageError, match="JSON"):
        load([tmp_path])


def test_an_unregistered_tool_id_is_a_startup_error(tmp_path):
    write_package(tmp_path, manifest_data=manifest(allowed_tools=["run_shell"]))
    with pytest.raises(SkillPackageError, match="run_shell"):
        load([tmp_path])


def test_an_unregistered_completion_predicate_is_a_startup_error(tmp_path):
    """RM06: predicates are registered gateway validators, not package text."""
    write_package(tmp_path, manifest_data=manifest(completion_predicate="eval_this_code"))
    with pytest.raises(SkillPackageError, match="eval_this_code"):
        load([tmp_path])


def test_an_unregistered_parallel_workflow_is_a_startup_error(tmp_path):
    write_package(tmp_path, manifest_data=manifest(parallel_workflow="free-form-workflow"))
    with pytest.raises(SkillPackageError, match="free-form-workflow"):
        load([tmp_path])


def test_a_registered_parallel_workflow_is_recorded(tmp_path):
    write_package(
        tmp_path, manifest_data=manifest(parallel_workflow="health-and-diagnostic-checklist")
    )
    skill = load([tmp_path]).get("check-service-health")
    assert skill.parallel_workflow == "health-and-diagnostic-checklist"


def test_a_package_missing_its_manifest_is_a_startup_error(tmp_path):
    """Section 4.2: a malformed package is never skipped silently."""
    package = write_package(tmp_path)
    (package / MANIFEST_FILENAME).unlink()
    with pytest.raises(SkillPackageError, match=MANIFEST_FILENAME):
        load([tmp_path])


def test_a_package_missing_skill_md_is_a_startup_error(tmp_path):
    package = write_package(tmp_path)
    (package / "SKILL.md").unlink()
    with pytest.raises(SkillPackageError, match="SKILL.md"):
        load([tmp_path])


def test_one_malformed_package_fails_the_whole_load(tmp_path):
    write_package(tmp_path, "check-service-health")
    write_package(tmp_path, "lookup-latest-deployment", manifest_data=manifest(version="x"))
    with pytest.raises(SkillPackageError, match="lookup-latest-deployment"):
        load([tmp_path])


# --- Section 4.2: resources stay inside the package --------------------------


def test_a_declared_resource_can_be_read(tmp_path):
    write_package(tmp_path, files={"examples.json": '[{"id": "a"}]'})
    registry = load([tmp_path])
    assert registry.read_resource("check-service-health", "examples.json") == '[{"id": "a"}]'


@pytest.mark.parametrize(
    "path", ["../other/SKILL.md", "/etc/passwd", "sub/../../x", "SKILL.md", "undeclared.txt"]
)
def test_reads_outside_the_declared_package_resources_are_rejected(tmp_path, path):
    root = tmp_path / "root"
    write_package(root, files={"examples.json": "[]", "undeclared.txt": "secret"})
    (root / "check-service-health" / "sub").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "SKILL.md").write_text("outside", "utf-8")
    registry = load([root])
    with pytest.raises(SkillResourceError):
        registry.read_resource("check-service-health", path)


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/hosts", "missing.json", ""])
def test_a_manifest_resource_outside_the_package_is_a_startup_error(tmp_path, path):
    (tmp_path / "outside.txt").write_text("outside", "utf-8")
    write_package(tmp_path / "root", manifest_data=manifest(resources=[path]))
    with pytest.raises(SkillPackageError, match="resource"):
        load([tmp_path / "root"])


def test_a_resource_symlink_that_escapes_the_package_is_a_startup_error(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", "utf-8")
    package = write_package(tmp_path / "root", files={})
    try:
        os.symlink(outside, package / "examples.json")
    except OSError:
        pytest.skip("symlinks are not available on this platform")
    with pytest.raises(SkillPackageError, match="resource"):
        load([tmp_path / "root"])


# --- Section 4.2: skill roots ------------------------------------------------


def test_roots_are_scanned_one_level_deep(tmp_path):
    """Section 4.2: a package's own subdirectories are not scanned, and
    plain files in a root, such as a README, are ignored."""
    package = write_package(tmp_path / "root")
    write_package(package / "nested", "lookup-latest-deployment")
    (tmp_path / "root" / "README.md").write_text("not a package", "utf-8")
    assert load([tmp_path / "root"]).names() == ("check-service-health",)


@pytest.mark.parametrize("directory", ["notes", ".hidden"])
def test_every_subdirectory_of_a_root_is_a_candidate_package(tmp_path, directory):
    """Section 4.2 (spec 0.4): every subdirectory of a root is a candidate
    package; one without SKILL.md and skill.manifest.json is a startup
    error, never skipped silently."""
    write_package(tmp_path / "root")
    (tmp_path / "root" / directory).mkdir()
    with pytest.raises(SkillPackageError, match="missing SKILL.md"):
        load([tmp_path / "root"])


def test_the_manifest_file_is_skill_manifest_json():
    """Section 4.2 (spec 0.4): the registry manifest is skill.manifest.json."""
    assert MANIFEST_FILENAME == "skill.manifest.json"


def test_a_manifest_under_another_name_is_a_startup_error(tmp_path):
    """Section 4.2: the manifest has one name; another file does not count."""
    package = write_package(tmp_path)
    (package / MANIFEST_FILENAME).rename(package / "relaymind.json")
    with pytest.raises(SkillPackageError, match="missing skill.manifest.json"):
        load([tmp_path])


@pytest.mark.parametrize("value", [1.0, True, "1"])
def test_the_manifest_schema_version_must_be_the_integer_1(tmp_path, value):
    """Section 4.2, section 8: an exact integer schema_version."""
    write_package(tmp_path, manifest_data=manifest(schema_version=value))
    with pytest.raises(SkillPackageError, match="schema_version"):
        load([tmp_path])


def test_without_configured_roots_the_default_root_is_loaded(tmp_path, monkeypatch):
    """Section 4.2: the default root list is ["skills/"], the checkout's
    skills directory when the gateway starts in the checkout root."""
    monkeypatch.chdir(REPO_ROOT)
    registry = SkillRegistry.load(
        tools=make_demo_tools(tmp_path),
        predicates=COMPLETION_PREDICATES.keys(),
        workflows=PARALLEL_WORKFLOWS,
    )
    assert registry.roots == (REPO_ROOT / "skills",)
    assert "check-service-health" in registry.names()


def test_a_configured_root_list_replaces_the_default(tmp_path):
    """Section 4.2: a configured list replaces the default, it does not add to it."""
    write_package(tmp_path / "only", "summarize-incident-counts")
    registry = load([tmp_path / "only"])
    assert registry.names() == ("summarize-incident-counts",)
    assert registry.roots == ((tmp_path / "only").resolve(),)


def test_several_roots_load_in_their_configured_order(tmp_path):
    write_package(tmp_path / "team", "summarize-incident-counts")
    write_package(tmp_path / "default", "check-service-health")
    registry = load([tmp_path / "default", tmp_path / "team"])
    assert registry.names() == ("check-service-health", "summarize-incident-counts")
    assert registry.roots == ((tmp_path / "default").resolve(), (tmp_path / "team").resolve())
    reordered = load([tmp_path / "team", tmp_path / "default"])
    assert reordered.names() == ("summarize-incident-counts", "check-service-health")


def test_a_duplicate_name_across_roots_is_a_configuration_error(tmp_path):
    """Section 4.2: a duplicate is a configuration error, not a precedence rule."""
    write_package(tmp_path / "default")
    write_package(tmp_path / "team")
    with pytest.raises(SkillConfigError, match="check-service-health"):
        load([tmp_path / "default", tmp_path / "team"])


def test_a_missing_root_is_a_configuration_error(tmp_path):
    with pytest.raises(SkillConfigError, match="does-not-exist"):
        load([tmp_path / "does-not-exist"])


@pytest.mark.parametrize(
    "root", ["https://skills.example.com/registry", "file://server/share", "git@host:skills"]
)
def test_a_remote_root_is_rejected(root):
    """Section 4.2: fetching from a URL is automatic skill installation."""
    with pytest.raises(SkillConfigError, match="local"):
        load([root])


def test_no_roots_is_a_configuration_error():
    with pytest.raises(SkillConfigError):
        load([])


def test_the_run_snapshot_records_roots_and_every_offered_skill(tmp_path):
    """Section 4.2: each run records the roots and name, version, and hash
    of every skill offered to the router."""
    write_package(tmp_path / "a", "check-service-health")
    write_package(tmp_path / "b", "lookup-latest-deployment")
    registry = load([tmp_path / "a", tmp_path / "b"])
    snapshot = registry.offered_snapshot(["lookup-latest-deployment"])
    assert snapshot == {
        "skill_roots": [str(tmp_path / "a"), str(tmp_path / "b")],
        "skills_offered": [
            {
                "name": "lookup-latest-deployment",
                "version": "1.0.0",
                "content_hash": registry.get("lookup-latest-deployment").content_hash,
            }
        ],
    }
    with pytest.raises(SkillNotFoundError):
        registry.offered_snapshot(["invented-skill"])


def test_the_run_snapshot_fits_the_run_accepted_payload(tmp_path):
    """Section 8, RM22: the snapshot supplies skill_roots and skills_offered
    of the seq-1 run.accepted event."""
    write_package(tmp_path)
    snapshot = load([tmp_path]).offered_snapshot(["check-service-health"])
    validate_trace_event(
        {
            "schema_version": 1,
            "run_id": "run_1",
            "seq": 1,
            "timestamp": "2026-10-20T03:00:00Z",
            "elapsed_ms": 0,
            "event_type": "run.accepted",
            "actor": "gateway",
            "payload": {
                "requested_execution_mode": ACCEPTED_PAYLOAD["requested_execution_mode"],
                "effective_execution_mode": ACCEPTED_PAYLOAD["effective_execution_mode"],
                "model_alias": ACCEPTED_PAYLOAD["model_alias"],
                "budgets": ACCEPTED_PAYLOAD["budgets"],
                "original_messages": ACCEPTED_PAYLOAD["original_messages"],
                **snapshot,
            },
        }
    )


# --- RM07: versions and hashes ------------------------------------------------


def test_editing_the_instruction_body_changes_the_hash(tmp_path):
    package = write_package(tmp_path)
    before = load([tmp_path]).get("check-service-health").content_hash
    (package / "SKILL.md").write_text(
        SKILL_MD.format(name="check-service-health") + "\nAlso say hello.\n", "utf-8"
    )
    assert load([tmp_path]).get("check-service-health").content_hash != before


def test_editing_a_resource_changes_the_hash(tmp_path):
    package = write_package(tmp_path)
    before = load([tmp_path]).get("check-service-health").content_hash
    (package / "examples.json").write_text('[{"id": "new"}]', "utf-8")
    assert load([tmp_path]).get("check-service-health").content_hash != before


def test_changing_an_allowed_tool_schema_changes_the_skill_hash(tmp_path):
    """RM07: updating a skill's tool schema changes its content hash."""
    write_package(tmp_path)
    before = load([tmp_path]).get("check-service-health").content_hash
    narrowed = {**SERVICE_INPUT_SCHEMA, "properties": {"service": {"enum": ["staging-api"]}}}
    after = load([tmp_path], tools=make_tools(narrowed)).get("check-service-health").content_hash
    assert before != after


def test_the_hash_is_stable_across_loads(tmp_path):
    write_package(tmp_path)
    assert (
        load([tmp_path]).get("check-service-health").content_hash
        == load([tmp_path]).get("check-service-health").content_hash
    )


def test_a_required_version_that_matches_loads(tmp_path):
    write_package(tmp_path)
    registry = load([tmp_path])
    current = registry.get("check-service-health")
    loaded = registry.load_skill(
        "check-service-health",
        emit=Events(),
        version=current.version,
        content_hash=current.content_hash,
    )
    assert loaded.version == "1.0.0"


@pytest.mark.parametrize("required", [{"version": "0.9.0"}, {"content_hash": "sha256:" + "0" * 64}])
def test_a_required_missing_version_fails_without_substitution(tmp_path, required):
    """RM07: existing runs keep their snapshot; a missing one fails explicitly."""
    write_package(tmp_path)
    events = Events()
    with pytest.raises(SkillVersionMissingError):
        load([tmp_path]).load_skill("check-service-health", emit=events, **required)
    assert events.items == []


def test_loading_an_unknown_skill_fails_without_an_event(tmp_path):
    write_package(tmp_path)
    events = Events()
    with pytest.raises(SkillNotFoundError):
        load([tmp_path]).load_skill("invented-skill", emit=events)
    assert events.items == []


def test_a_loaded_skill_keeps_the_startup_snapshot(tmp_path):
    """RM07: the registry serves the version it validated, not later edits."""
    package = write_package(tmp_path)
    registry = load([tmp_path])
    snapshot = registry.get("check-service-health").content_hash
    (package / "SKILL.md").write_text(
        SKILL_MD.format(name="check-service-health") + "\nInjected later.\n", "utf-8"
    )
    loaded = registry.load_skill("check-service-health", emit=Events())
    assert loaded.content_hash == snapshot
    assert "Injected later" not in loaded.body


# --- Review fixes: containment, normalized resources, default root -----------


def _symlink(target, link):
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlinks are not available on this platform")


@pytest.mark.parametrize("filename", ["SKILL.md", MANIFEST_FILENAME])
def test_a_package_file_symlinked_outside_the_package_is_a_startup_error(tmp_path, filename):
    """Section 4.2: reads are restricted to the package directory, so
    SKILL.md and the manifest are resolved and must stay inside it."""
    package = write_package(tmp_path / "root")
    outside = tmp_path / f"outside-{filename}"
    (package / filename).rename(outside)
    _symlink(outside, package / filename)
    with pytest.raises(SkillPackageError, match="outside the package"):
        load([tmp_path / "root"])


def test_a_package_directory_symlinked_outside_the_root_is_a_startup_error(tmp_path):
    """Section 4.2: a candidate package resolves inside its root."""
    write_package(tmp_path / "elsewhere")
    (tmp_path / "root").mkdir()
    _symlink(tmp_path / "elsewhere" / "check-service-health", tmp_path / "root" / "linked")
    with pytest.raises(SkillPackageError, match="outside the skill root"):
        load([tmp_path / "root"])


def test_a_symlink_that_stays_inside_the_package_is_allowed(tmp_path):
    package = write_package(tmp_path / "root", files={"data/examples.json": "[]"})
    _symlink(package / "data" / "examples.json", package / "examples.json")
    registry = load([tmp_path / "root"])
    assert registry.read_resource("check-service-health", "examples.json") == "[]"


@pytest.mark.parametrize("path", ["./examples.json", "data//examples.json", "data/./examples.json"])
def test_a_resource_path_that_is_not_normalized_is_a_startup_error(tmp_path, path):
    """Section 4.2: a declared resource must be readable by the exact path
    the manifest names, so only normalized paths are accepted."""
    write_package(
        tmp_path,
        manifest_data=manifest(resources=[path]),
        files={"examples.json": "[]", "data/examples.json": "[]"},
    )
    with pytest.raises(SkillPackageError, match="normalized"):
        load([tmp_path])


def test_two_resource_paths_for_one_file_are_a_startup_error(tmp_path):
    """Section 4.2: duplicates are found after resolving the paths."""
    package = write_package(tmp_path / "root", files={"examples.json": "[]"})
    _symlink(package / "examples.json", package / "alias.json")
    data = manifest(resources=["examples.json", "alias.json"])
    (package / MANIFEST_FILENAME).write_text(json.dumps(data), "utf-8")
    with pytest.raises(SkillPackageError, match="more than once"):
        load([tmp_path / "root"])


def test_the_default_root_is_skills_relative_to_the_working_directory(tmp_path, monkeypatch):
    """Section 4.2: the default root list is ["skills/"], a path relative to
    the working directory the gateway starts in (the checkout root). It
    does not depend on where the relaymind package is installed."""
    assert default_skill_roots() == ("skills/",)
    write_package(tmp_path / "skills")
    monkeypatch.chdir(tmp_path)
    registry = SkillRegistry.load(tools=make_tools(), predicates=PREDICATES, workflows=WORKFLOWS)
    assert registry.names() == ("check-service-health",)
    assert registry.roots == ((tmp_path / "skills").resolve(),)


def test_the_run_snapshot_records_the_configured_root_strings(tmp_path, monkeypatch):
    """Section 4.2 and section 8: run.accepted records "the configured
    roots in order", as the operator wrote them. A relative root stays
    relative and a symlinked root keeps its own name, so the snapshot
    holds no host-specific resolved path; the registry still scans the
    resolved directories internally."""
    write_package(tmp_path / "real", "check-service-health")
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
    write_package(tmp_path / "team", "lookup-latest-deployment")
    monkeypatch.chdir(tmp_path)
    registry = load(["linked", "./team/"])
    assert registry.offered_snapshot([])["skill_roots"] == ["linked", "./team/"]
    assert registry.configured_roots == ("linked", "./team/")
    assert registry.roots == ((tmp_path / "real").resolve(), (tmp_path / "team").resolve())


def test_the_default_root_is_recorded_as_configured(tmp_path, monkeypatch):
    """Section 4.2: the default list is ["skills/"], and that is what a run
    records when no list is configured."""
    write_package(tmp_path / "skills")
    monkeypatch.chdir(tmp_path)
    registry = SkillRegistry.load(tools=make_tools(), predicates=PREDICATES, workflows=WORKFLOWS)
    assert registry.offered_snapshot([])["skill_roots"] == ["skills/"]
