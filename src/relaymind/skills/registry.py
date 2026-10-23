"""Skill registry: SKILL.md packages and RelayMind manifests (RM06, RM07).

A package is a directory holding a SKILL.md (Agent Skills ``name`` and
``description`` frontmatter, then the instruction body) and a RelayMind
registry manifest, ``skill.manifest.json``, validated against the
published ``schemas/skill_manifest.v1.json``.

Skill roots are operator configuration (spec 0.4, section 4.2): an
ordered list, ``skills/`` by default, which a configured list replaces.
Relative roots, the default among them, resolve against the working
directory, which is the checkout root when the gateway starts there.
The registry scans each root one level deep, in order. Every
subdirectory of a root is a candidate package; one that lacks either
file or fails validation stops the load. Plain files in a root are
ignored. Names are unique across all roots. Package directories,
SKILL.md, the manifest, and resources are resolved through symlinks and
must stay inside their root and package.

The registry reads everything once, at load: the body, the manifest,
and every declared resource. The content hash covers those bytes and the
schema hash of each allowed tool, so a changed skill or tool schema
changes the hash (RM07). Later edits on disk do not change a loaded
registry. Skills never run code: the registry only reads text.
"""

import copy
import hashlib
import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

import yaml

from relaymind.orchestrator import identifiers
from relaymind.schema_util import (
    check_schema_version,
    first_schema_error,
    load_packaged_schema,
)
from relaymind.skills.cache import ContentCache
from relaymind.tools.registry import (
    SideEffect,
    StrictValidator,
    ToolRegistry,
    parameters_validator,
)

MANIFEST_FILENAME = "skill.manifest.json"
SKILL_FILENAME = "SKILL.md"
MANIFEST_SCHEMA_VERSION = 1

SKILL_MANIFEST_SCHEMA: dict[str, Any] = load_packaged_schema(__package__, "skill_manifest.v1.json")
_MANIFEST_VALIDATOR = StrictValidator(SKILL_MANIFEST_SCHEMA)

# Agent Skills frontmatter fields (S7). allowed-tools is accepted but
# never grants anything: permissions come from the manifest.
_FRONTMATTER_KEYS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)
_SKILL_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_MAX_NAME = 64
_MAX_DESCRIPTION = 1024
_REMOTE_ROOT = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://|^[\w.-]+@[\w.-]+:")

Emit = Callable[[str, dict[str, Any]], None]


class SkillConfigError(ValueError):
    """The configured skill roots cannot be loaded (section 4.2)."""


class SkillPackageError(SkillConfigError):
    """One skill package is malformed; the gateway must not start."""


class SkillNotFoundError(LookupError):
    """The named skill is not in the registry."""


class SkillVersionMissingError(LookupError):
    """A run needs a skill version or hash the registry does not hold (RM07)."""


class SkillResourceError(PermissionError):
    """A resource read outside the package's declared resources."""


@dataclass(frozen=True)
class SkillDescription:
    """What the router sees about a skill."""

    name: str
    description: str


REQUEST = "request"
CONSTANT = "constant"


@dataclass(frozen=True)
class PrefetchBinding:
    """How one argument of a prefetch call gets its value (RM29).

    source is request: an identifier the gateway finds in a user turn
    under the RM05 rule. Or it is constant: a documented value for an
    argument that is not an identifier, held in value.
    """

    name: str
    source: str
    value: Any = None

    def to_json_object(self) -> dict[str, Any]:
        """The binding as the manifest declares it."""
        if self.source == REQUEST:
            return {"from": REQUEST}
        return {"constant": copy.deepcopy(self.value)}


@dataclass(frozen=True)
class PrefetchCall:
    """A read-only call a skill declares for the gateway to run first (RM29)."""

    tool: str
    bindings: tuple[PrefetchBinding, ...]

    @property
    def from_request(self) -> tuple[str, ...]:
        """The names of the arguments the gateway resolves from the request."""
        return tuple(b.name for b in self.bindings if b.source == REQUEST)

    @property
    def constants(self) -> Mapping[str, Any]:
        """The constant arguments, by name."""
        return MappingProxyType({b.name: b.value for b in self.bindings if b.source == CONSTANT})

    def to_json_object(self) -> dict[str, Any]:
        """The declaration as the manifest and run.accepted record it (section 8)."""
        return {
            "tool": self.tool,
            "arguments": {b.name: b.to_json_object() for b in self.bindings},
        }


@dataclass(frozen=True)
class Skill:
    """A validated skill package snapshot (RM06)."""

    name: str
    description: str
    version: str
    content_hash: str
    body: str
    languages: tuple[str, ...]
    local_eligible: bool
    allowed_tools: tuple[str, ...]
    completion_predicate: str
    parallel_workflow: str | None
    package_dir: Path
    # Declared resource paths, in manifest order, and their text.
    resource_texts: Mapping[str, str] = field(repr=False)
    # The read-only calls to run before the first generation (RM29), in order.
    prefetch: tuple[PrefetchCall, ...] = ()


# Section 4.2: the default root list is ["skills/"].
DEFAULT_SKILL_ROOT = "skills/"


def default_skill_roots() -> tuple[str, ...]:
    """The default root list, ``skills/`` (section 4.2).

    The path is relative, like any configured root, and resolves against
    the working directory when the registry loads. The gateway starts in
    the checkout root, so this is the checkout's ``skills/`` directory;
    it never depends on where the relaymind package is installed. A
    gateway started elsewhere configures its roots explicitly.
    """
    return (DEFAULT_SKILL_ROOT,)


def _fail(package: Path, message: str) -> SkillPackageError:
    return SkillPackageError(f"skill package {package.name!r}: {message}")


def _parse_skill_md(package: Path, text: str) -> tuple[str, str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise _fail(package, "SKILL.md must start with YAML frontmatter between '---' lines")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise _fail(package, "SKILL.md frontmatter is not closed with '---'") from None
    try:
        front = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise _fail(package, f"SKILL.md frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(front, dict):
        raise _fail(package, "SKILL.md frontmatter must be a mapping")
    unknown = sorted(str(key) for key in front if key not in _FRONTMATTER_KEYS)
    if unknown:
        raise _fail(package, f"unknown SKILL.md frontmatter fields: {', '.join(unknown)}")
    name = front.get("name")
    if not isinstance(name, str) or len(name) > _MAX_NAME or not _SKILL_NAME.fullmatch(name):
        raise _fail(package, "frontmatter 'name' must be lowercase letters, digits, and hyphens")
    if name != package.name:
        raise _fail(package, f"frontmatter name {name!r} must match the directory name")
    description = front.get("description")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > _MAX_DESCRIPTION
    ):
        raise _fail(package, "frontmatter 'description' must be a non-blank string")
    body = "".join(lines[end + 1 :]).strip()
    if not body:
        raise _fail(package, "SKILL.md has an empty instruction body")
    return name, description.strip(), body + "\n"


def _parse_manifest(package: Path, raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail(package, f"{MANIFEST_FILENAME} is not valid JSON: {exc}") from exc
    if isinstance(data, dict) and "schema_version" in data:
        try:
            check_schema_version(data["schema_version"], MANIFEST_SCHEMA_VERSION, ValueError)
        except ValueError as exc:
            raise _fail(package, f"{MANIFEST_FILENAME}: {exc}") from None
    error = first_schema_error(_MANIFEST_VALIDATOR, data)
    if error is not None:
        where = "/".join(str(part) for part in error.path) or "(manifest)"
        raise _fail(package, f"{MANIFEST_FILENAME} {where}: {error.message}")
    return data


def _inside(package: Path, path: Path, label: str) -> Path:
    """path resolved through any symlinks; it must stay in the package (section 4.2)."""
    resolved = path.resolve()
    if not resolved.is_relative_to(package.resolve()):
        raise _fail(package, f"{label} resolves outside the package")
    return resolved


def _resource_path(package: Path, relpath: str) -> Path:
    """Resolve a package-relative resource path, refusing anything outside.

    Only normalized POSIX paths are accepted ("examples.json", not
    "./examples.json" or "data//examples.json"), so the path the manifest
    declares is exactly the path read_resource accepts (section 4.2).
    """
    pure = PurePosixPath(relpath)
    if not relpath or pure.is_absolute() or ".." in pure.parts or "\\" in relpath:
        raise SkillResourceError(f"resource path {relpath!r} is outside the package")
    if str(pure) != relpath:
        raise SkillResourceError(f"resource path {relpath!r} is not normalized; use {str(pure)!r}")
    resolved = (package / pure).resolve()
    if not resolved.is_relative_to(package.resolve()):
        raise SkillResourceError(f"resource path {relpath!r} is outside the package")
    return resolved


def _prefetch_calls(
    package: Path,
    declared: Sequence[Mapping[str, Any]],
    *,
    tools: ToolRegistry,
    allowed: Sequence[str],
) -> tuple[PrefetchCall, ...]:
    """Check the declared prefetch calls without a request and build them (RM29, section 4.2).

    Each tool must be in the allowlist and read-only. Every required
    parameter needs a binding, and every bound name must be a parameter
    of the tool. A request binding names an identifier with a closed
    list of values (the gateway can find one of them in a user turn); a
    constant is for a parameter that is not an identifier, and must
    satisfy the tool schema.
    """
    calls = []
    for index, entry in enumerate(declared):
        tool_name = entry["tool"]
        where = f"prefetch[{index}] ({tool_name})"
        if tool_name not in allowed:
            raise _fail(package, f"{where}: the tool is not in allowed_tools")
        tool = tools.get(tool_name)
        if tool.side_effect is not SideEffect.READ_ONLY:
            raise _fail(package, f"{where}: only a read-only tool can be prefetched")
        schema = tool.declaration.input_schema
        properties = schema.get("properties", {})
        bindings = []
        for name, binding in entry["arguments"].items():
            if name not in properties:
                raise _fail(package, f"{where}: {name!r} is not a parameter of the tool")
            spec = properties[name]
            if "from" in binding:
                if not (identifiers.is_identifier_property(spec) and spec.get("enum")):
                    raise _fail(
                        package,
                        f"{where}: {name!r} is not an identifier with a closed list of values,"
                        " so it cannot be read from the request",
                    )
                bindings.append(PrefetchBinding(name, REQUEST))
                continue
            if identifiers.is_identifier_property(spec):
                raise _fail(
                    package,
                    f"{where}: {name!r} is an identifier, so it comes from the request,"
                    " never from a constant (RM05)",
                )
            bindings.append(PrefetchBinding(name, CONSTANT, copy.deepcopy(binding["constant"])))
        missing = [name for name in schema.get("required", ()) if name not in entry["arguments"]]
        if missing:
            raise _fail(package, f"{where}: required parameters without a binding: {missing}")
        # The tool's own schema judges the constants. The request-bound names are left out
        # and "required" is lifted, because their values exist only at run time.
        constants = {b.name: b.value for b in bindings if b.source == CONSTANT}
        partial = parameters_validator({**schema, "required": []})
        error = first_schema_error(partial, constants)
        if error is not None:
            name = ".".join(str(part) for part in error.path) or "(arguments)"
            raise _fail(package, f"{where}: constant for {name!r} fails the tool schema")
        calls.append(PrefetchCall(tool_name, tuple(bindings)))
    return tuple(calls)


def _content_hash(files: Sequence[tuple[str, bytes]], tool_hashes: Sequence[str]) -> str:
    """files are (relative path, SHA-256 digest of the file's bytes) pairs."""
    digest = hashlib.sha256()
    for relpath, file_digest in sorted(files):
        digest.update(relpath.encode("utf-8") + b"\0")
        digest.update(file_digest)
    for tool_hash in tool_hashes:
        digest.update(b"tool\0" + tool_hash.encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def _load_package(
    package: Path,
    *,
    tools: ToolRegistry,
    predicates: Collection[str],
    workflows: Collection[str],
    cache: ContentCache,
) -> Skill:
    skill_path = package / SKILL_FILENAME
    manifest_path = package / MANIFEST_FILENAME
    for path in (skill_path, manifest_path):
        if not path.is_file():
            raise _fail(package, f"missing {path.name}")
        _inside(package, path, path.name)
    skill_bytes = skill_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    skill_digest = hashlib.sha256(skill_bytes).digest()
    manifest_digest = hashlib.sha256(manifest_bytes).digest()
    # Spec 0.26 section 10: parses are cached by content hash. The package
    # name is part of the SKILL.md key because the frontmatter must match it.
    # One digest per file serves the cache key and the content hash.
    try:
        name, description, body = cache.get_or_parse(
            f"skill:{package.name}",
            skill_bytes,
            lambda raw: _parse_skill_md(package, raw.decode("utf-8")),
            digest=skill_digest,
        )
    except UnicodeDecodeError as exc:
        raise _fail(package, "SKILL.md is not UTF-8 text") from exc
    # The cache returns an independent manifest, including its nested lists.
    manifest = cache.get_or_parse(
        f"manifest:{package.name}",
        manifest_bytes,
        lambda raw: _parse_manifest(package, raw),
        digest=manifest_digest,
    )

    unknown_tools = [tool for tool in manifest["allowed_tools"] if tool not in tools.names()]
    if unknown_tools:
        raise _fail(package, f"allowed_tools are not registered tools: {', '.join(unknown_tools)}")
    predicate = manifest["completion_predicate"]
    if predicate not in predicates:
        raise _fail(package, f"completion_predicate {predicate!r} is not a registered validator")
    workflow = manifest.get("parallel_workflow")
    if workflow is not None and workflow not in workflows:
        raise _fail(package, f"parallel_workflow {workflow!r} is not a registered workflow")

    texts: dict[str, str] = {}
    files = [(SKILL_FILENAME, skill_digest), (MANIFEST_FILENAME, manifest_digest)]
    seen = {skill_path.resolve(), manifest_path.resolve()}
    for relpath in manifest["resources"]:
        try:
            path = _resource_path(package, relpath)
        except SkillResourceError as exc:
            raise _fail(package, f"resource: {exc}") from exc
        if not path.is_file() or path.name in (SKILL_FILENAME, MANIFEST_FILENAME):
            raise _fail(package, f"resource {relpath!r} is not a resource file in the package")
        if path in seen:
            raise _fail(package, f"resource {relpath!r} names a package file more than once")
        seen.add(path)
        data = path.read_bytes()
        data_digest = hashlib.sha256(data).digest()
        try:
            texts[relpath] = cache.get_or_parse(
                "resource", data, lambda raw: raw.decode("utf-8"), digest=data_digest
            )
        except UnicodeDecodeError as exc:
            raise _fail(package, f"resource {relpath!r} is not UTF-8 text") from exc
        files.append((relpath, data_digest))

    prefetch = _prefetch_calls(
        package, manifest.get("prefetch", ()), tools=tools, allowed=manifest["allowed_tools"]
    )
    tool_hashes = [tools.get(tool).schema_hash for tool in manifest["allowed_tools"]]
    return Skill(
        name=name,
        description=description,
        version=manifest["version"],
        content_hash=_content_hash(files, tool_hashes),
        body=body,
        languages=tuple(manifest["languages"]),
        local_eligible=manifest["local_eligible"],
        allowed_tools=tuple(manifest["allowed_tools"]),
        completion_predicate=predicate,
        parallel_workflow=workflow,
        package_dir=package.resolve(),
        resource_texts=MappingProxyType(texts),
        prefetch=prefetch,
    )


def _check_root(root: str | Path) -> Path:
    if isinstance(root, str) and _REMOTE_ROOT.search(root):
        raise SkillConfigError(f"skill root {root!r} must be a local filesystem path")
    path = Path(root)
    if not path.is_dir():
        raise SkillConfigError(f"skill root {str(path)!r} is not a directory")
    return path.resolve()


class SkillRegistry:
    """Validated skills loaded from operator-configured roots.

    roots are the resolved directories the registry scanned, for its own
    containment checks. configured_roots are the operator's root strings
    as given, in order; run.accepted records those (sections 4.2, 8), so
    the trace holds no host-specific resolved path.
    """

    def __init__(
        self,
        roots: tuple[Path, ...],
        skills: Mapping[str, Skill],
        *,
        configured_roots: tuple[str, ...] | None = None,
        parse_cache: ContentCache | None = None,
        reload_with: Mapping[str, Any] | None = None,
    ) -> None:
        self.parse_cache = parse_cache if parse_cache is not None else ContentCache()
        self._reload_with = reload_with
        self.roots = roots
        self.configured_roots = (
            configured_roots if configured_roots is not None else tuple(str(root) for root in roots)
        )
        self._skills = dict(skills)

    @classmethod
    def load(
        cls,
        roots: Iterable[str | Path] | None = None,
        *,
        tools: ToolRegistry,
        predicates: Collection[str],
        workflows: Collection[str] = (),
        parse_cache: ContentCache | None = None,
    ) -> "SkillRegistry":
        """Scan the roots in order and validate every package (section 4.2).

        roots is the operator's configured list; None means the default,
        ``skills/`` (default_skill_roots). Relative roots resolve against
        the working directory. A configured list replaces the default.
        predicates and workflows are the IDs registered in the gateway;
        a manifest naming anything else is a startup error. workflows
        are the known workflow IDs, not the operator-enabled ones:
        enablement is checked at routing (section 4.4).

        parse_cache (spec 0.26 section 10) holds parsed files by content
        hash; a new one is made when none is given.
        """
        cache = parse_cache if parse_cache is not None else ContentCache()
        if roots is None:
            roots = default_skill_roots()
        roots = tuple(roots)
        checked = tuple(_check_root(root) for root in roots)
        if not checked:
            raise SkillConfigError("at least one skill root must be configured")
        skills: dict[str, Skill] = {}
        for root in checked:
            # Every subdirectory is a candidate package; plain files are ignored.
            for package in sorted(p for p in root.iterdir() if p.is_dir()):
                # Section 4.2: a symlinked candidate must resolve inside its root.
                if not package.resolve().is_relative_to(root):
                    raise _fail(
                        package, f"package directory resolves outside the skill root {root}"
                    )
                skill = _load_package(
                    package, tools=tools, predicates=predicates, workflows=workflows, cache=cache
                )
                if skill.name in skills:
                    raise SkillConfigError(
                        f"skill name {skill.name!r} appears in more than one root: "
                        f"{skills[skill.name].package_dir} and {skill.package_dir}"
                    )
                skills[skill.name] = skill
        return cls(
            checked,
            skills,
            configured_roots=tuple(str(root) for root in roots),
            parse_cache=cache,
            # The resolved directories, so a reload reads the same ones even
            # after the working directory changed (configured_roots keep
            # the operator's strings).
            reload_with={
                "roots": checked,
                "tools": tools,
                "predicates": tuple(predicates),
                "workflows": tuple(workflows),
            },
        )

    def reloaded(self) -> "SkillRegistry":
        """A new registry from the same roots, re-reading every package (spec 0.26).

        Unchanged files hit the content-hash cache; changed files are parsed
        and get a new content hash (RM07). This registry is not changed, so
        a run keeps the snapshot it started with.
        """
        if self._reload_with is None:
            raise SkillConfigError("this registry was not loaded from skill roots")
        fresh = type(self).load(**self._reload_with, parse_cache=self.parse_cache)
        fresh.configured_roots = self.configured_roots
        return fresh

    def names(self) -> tuple[str, ...]:
        """Skill names in load order."""
        return tuple(self._skills)

    def get(self, name: str) -> Skill:
        """The loaded snapshot of one skill."""
        try:
            return self._skills[name]
        except KeyError:
            raise SkillNotFoundError(f"skill {name!r} is not registered") from None

    def catalog(self) -> list[SkillDescription]:
        """Name and description of every skill, for the router."""
        return [SkillDescription(s.name, s.description) for s in self._skills.values()]

    def offered_snapshot(self, names: Iterable[str]) -> dict[str, Any]:
        """The skill_roots and skills_offered fields of run.accepted (section 8).

        skill_roots are the configured root strings as given, not the
        resolved paths.
        """
        offered = [self.get(name) for name in names]
        return {
            "skill_roots": list(self.configured_roots),
            "skills_offered": [
                {"name": s.name, "version": s.version, "content_hash": s.content_hash}
                for s in offered
            ],
        }

    def prefetch_declarations(self, names: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """The prefetch_declarations field of run.accepted (RM29, section 8).

        One entry per offered skill, an empty array for a skill that
        declares none. The objects are copies; changing them changes
        no skill.
        """
        return {name: [c.to_json_object() for c in self.get(name).prefetch] for name in names}

    def load_skill(
        self,
        name: str,
        *,
        emit: Emit,
        version: str | None = None,
        content_hash: str | None = None,
    ) -> Skill:
        """Load one skill's instructions and emit skill.loaded (RM06).

        A run that pinned a version or hash gets exactly that snapshot or
        SkillVersionMissingError; nothing is substituted (RM07).
        """
        skill = self.get(name)
        if (version is not None and version != skill.version) or (
            content_hash is not None and content_hash != skill.content_hash
        ):
            raise SkillVersionMissingError(
                f"skill {name!r} version {version or skill.version} "
                f"hash {content_hash or skill.content_hash} is not available"
            )
        emit(
            "skill.loaded",
            {"name": skill.name, "version": skill.version, "content_hash": skill.content_hash},
        )
        return skill

    def read_resource(self, name: str, relpath: str) -> str:
        """Text of a resource the skill's manifest declares (section 4.2)."""
        texts = self.get(name).resource_texts
        if relpath not in texts:
            raise SkillResourceError(
                f"skill {name!r} does not declare resource {relpath!r}; "
                "reads are limited to declared package resources"
            )
        return texts[relpath]
