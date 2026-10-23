"""Derive the pinned SkillsBench selection cases (RM25, RM28, section 12.4).

Only task bodies and skill names/descriptions enter the benchmark. Source
instructions and bundled executables are never executed. The frozen release
checks the documented registry digests and computes its own case and file hashes.
"""

import argparse
import hashlib
import json
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from relaymind.canonical import canonical_digest  # noqa: E402

REPOSITORY = "https://github.com/benchflow-ai/skillsbench"
REGISTRY_COMMIT = "f860ecf02ef53412a030f8661c83d644b739ff01"
TASK_COMMIT = "b63b7b2850226b6aa4fb5929a8c1ac7bc4d9a6af"
REGISTRY_VERSION = "1.1"


def _front_matter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8-sig")
    if not text.startswith("---\n"):
        raise ValueError(f"missing YAML front matter: {path}")
    front, separator, body = text[4:].partition("\n---")
    if not separator:
        raise ValueError(f"unterminated YAML front matter: {path}")
    data = yaml.safe_load(front)
    if not isinstance(data, dict):
        raise ValueError(f"front matter must be a mapping: {path}")
    return data, body.strip()


def _within(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"source path escapes archive: {relative}")
    return path


def _package_digest(directory: Path) -> str:
    entries = []
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink():
            entries.append([relative, "symlink", str(path.readlink())])
        elif path.is_file():
            with path.open("rb") as stream:
                entries.append(
                    [relative, "file", hashlib.file_digest(stream, "sha256").hexdigest()]
                )
    return canonical_digest(entries)


def derive(
    root: Path,
    tasks: list[dict[str, Any]],
    *,
    commit: str,
    package_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Derive in registry order; duplicate skill names retain their first description."""
    root = root.resolve()
    catalog: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    cases, differences, excluded = [], [], []
    for task in tasks:
        task_id = task["name"]
        if task.get("active") is False:
            excluded.append({"id": task_id, "reason": "inactive task"})
            continue
        directory = _within(root, task["path"])
        skills = sorted((directory / "environment" / "skills").glob("*/SKILL.md"))
        if not skills:
            excluded.append({"id": task_id, "reason": "no bundled skill"})
            continue
        metadata, request = _front_matter(directory / "task.md")
        if not request:
            raise ValueError(f"empty task body: {task_id}")
        expected = []
        for path in skills:
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"skill path escapes archive: {path}")
            front, _ = _front_matter(path)
            name, description = front.get("name"), front.get("description")
            if not isinstance(name, str) or not name or not isinstance(description, str):
                raise ValueError(f"skill needs a name and description: {path}")
            package = path.parent.relative_to(root).as_posix()
            digest = (
                package_hashes[package]
                if package_hashes is not None
                else _package_digest(path.parent)
            )
            if name not in catalog:
                catalog[name] = {"id": name, "name": name, "description": description, "tasks": []}
                hashes[name] = digest
            elif hashes[name] != digest:
                differences.append(
                    {
                        "skill": name,
                        "task": task_id,
                        "first_task": catalog[name]["tasks"][0],
                        "first_sha256": hashes[name],
                        "sha256": digest,
                    }
                )
            if task_id not in catalog[name]["tasks"]:
                catalog[name]["tasks"].append(task_id)
            if name not in expected:
                expected.append(name)
        labels = metadata.get("metadata", {})
        cases.append(
            {
                "id": task_id,
                "request": request,
                "expected": expected,
                "category": labels.get("category", metadata.get("category", "unknown")),
                "difficulty": labels.get("difficulty", metadata.get("difficulty", "unknown")),
                "registry_digest": task.get("digest"),
                "task_commit": task.get("git_commit_id", commit),
            }
        )
    for case in cases:
        case["examples"] = {
            name: [
                other["request"]
                for other in cases
                if other["id"] != case["id"] and name in other["expected"]
            ]
            for name in case["expected"]
        }
        case["sha256"] = canonical_digest(case)
    return {
        "schema_version": 1,
        "version": 1,
        "source": {
            "repository": REPOSITORY,
            "commit": commit,
            "license": "Apache-2.0",
            "attribution": "Derived from SkillsBench by the SkillsBench contributors.",
            "registry_digest_verification": "recorded only; RelayMind hashes derived cases",
        },
        "catalog": list(catalog.values()),
        "cases": cases,
        "differences": differences,
        "excluded": excluded,
    }


def verify_registry(tasks: list[dict[str, Any]], task_hashes: dict[str, str]) -> None:
    """Check the documented task-content digests before deriving a frozen release."""
    for task in tasks:
        if task_hashes.get(task["path"]) != task.get("digest"):
            raise ValueError(f"registry task digest differs: {task['name']}")


def extract_inputs(
    archive_path: Path, destination: Path, *, task_hashes: dict[str, str] | None = None
) -> dict[str, str]:
    """Extract text inputs and hash package/task trees without retaining binary resources."""
    packages: dict[str, list[list[str]]] = {}
    tasks: dict[str, list[tuple[str, bytes]]] = {}
    with tarfile.open(archive_path, "r|gz") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if len(parts) < 2:
                continue
            relative = PurePosixPath(*parts[1:])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("unsafe archive member")
            package = None
            if (
                len(relative.parts) >= 6
                and relative.parts[0] == "tasks"
                and relative.parts[2:4] == ("environment", "skills")
            ):
                package = PurePosixPath(*relative.parts[:5]).as_posix()
                resource = PurePosixPath(*relative.parts[5:]).as_posix()
                if member.issym() or member.islnk():
                    packages.setdefault(package, []).append([resource, "symlink", member.linkname])
            if not member.isfile():
                continue
            task = (
                PurePosixPath(*relative.parts[:2]).as_posix()
                if len(relative.parts) >= 3 and relative.parts[0] == "tasks"
                else None
            )
            wanted = str(relative) in ("registry.json", "LICENSE", "NOTICE") or (
                relative.parts[0] == "tasks"
                and (
                    relative.name == "task.md"
                    or (relative.name == "SKILL.md" and "skills" in relative.parts)
                )
            )
            if not wanted and package is None and (task is None or task_hashes is None):
                continue
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"unreadable archive input: {relative}")
            if wanted:
                contents = source.read()
                target = _within(destination, str(relative))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(contents)
                digest = hashlib.sha256(contents).digest()
            else:
                digest = hashlib.file_digest(source, "sha256").digest()
            if package is not None:
                packages.setdefault(package, []).append([resource, "file", digest.hex()])
            if task is not None and task_hashes is not None:
                resource_path = PurePosixPath(*relative.parts[2:]).as_posix()
                tasks.setdefault(task, []).append((resource_path, digest))
    if task_hashes is not None:
        for task, entries in tasks.items():
            digest = hashlib.sha256()
            for name, content_hash in sorted(entries):
                digest.update(name.encode("utf-8") + b"\x00" + content_hash)
            task_hashes[task] = "sha256:" + digest.hexdigest()
    return {name: canonical_digest(sorted(entries)) for name, entries in packages.items()}


def _archive(commit: str, provided: Path | None, cache: Path) -> Path:
    if provided is not None:
        return provided
    path = cache / f"skillsbench-{commit}.tar.gz"
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".part")
        urllib.request.urlretrieve(f"{REPOSITORY}/archive/{commit}.tar.gz", temporary)
        temporary.replace(path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", type=Path, help="cached archive of the pinned registry commit")
    parser.add_argument(
        "--task-archive", type=Path, help="cached archive of the pinned task commit"
    )
    parser.add_argument("--cache-dir", type=Path, default=Path("/tmp/relaymind-benchmarks"))
    parser.add_argument("--out", type=Path, default=Path("evaluation/skills.v1.json"))
    parser.add_argument("--lock", type=Path, default=Path("evaluation/skills.v1.lock.json"))
    args = parser.parse_args()
    registry_archive = _archive(REGISTRY_COMMIT, args.archive, args.cache_dir)
    task_archive = _archive(TASK_COMMIT, args.task_archive, args.cache_dir)
    with tempfile.TemporaryDirectory(prefix="relaymind-skillsbench-") as temporary:
        root = Path(temporary)
        extract_inputs(registry_archive, root / "registry")
        registry = json.loads((root / "registry/registry.json").read_text(encoding="utf-8"))
        release = next(entry for entry in registry if entry["version"] == REGISTRY_VERSION)
        if {task["git_commit_id"] for task in release["tasks"]} != {TASK_COMMIT}:
            raise ValueError("registry task revision differs from pinned commit")
        task_hashes = {}
        package_hashes = extract_inputs(task_archive, root / "tasks", task_hashes=task_hashes)
        verify_registry(release["tasks"], task_hashes)
        document = derive(
            root / "tasks", release["tasks"], commit=REGISTRY_COMMIT, package_hashes=package_hashes
        )
        document["source"].update(
            registry_version=REGISTRY_VERSION,
            task_commit=TASK_COMMIT,
            registry_digest_verification=(
                "verified all registry task digests using docs/dataset-versioning.md"
            ),
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        lock = {
            "schema_version": 1,
            "file": args.out.name,
            "sha256": hashlib.sha256(args.out.read_bytes()).hexdigest(),
            "source_commit": REGISTRY_COMMIT,
            "task_commit": TASK_COMMIT,
            "registry_archive_sha256": hashlib.sha256(registry_archive.read_bytes()).hexdigest(),
            "task_archive_sha256": hashlib.sha256(task_archive.read_bytes()).hexdigest(),
            "cases": [{"id": case["id"], "sha256": case["sha256"]} for case in document["cases"]],
        }
        args.lock.parent.mkdir(parents=True, exist_ok=True)
        args.lock.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8", newline="\n")
        (args.out.parent / "skillsbench.LICENSE").write_bytes((root / "tasks/LICENSE").read_bytes())
        notice = (
            "SkillsBench selection benchmark\nDerived from " + REPOSITORY + "\n"
            f"Registry commit: {REGISTRY_COMMIT}\nTask commit: {TASK_COMMIT}\n"
            "Copyright: SkillsBench contributors. Licensed under Apache-2.0.\n"
            "RelayMind modifications: task bodies and skill metadata extracted; skill names "
            "deduplicated in registry order; leave-one-task-out examples and hashes added.\n"
        )
        upstream_notice = root / "tasks/NOTICE"
        if upstream_notice.exists():
            notice += "\nUpstream notice:\n" + upstream_notice.read_text(encoding="utf-8")
        (args.out.parent / "skillsbench.NOTICE").write_text(notice, encoding="utf-8", newline="\n")
        print(
            f"Derived {len(document['cases'])} tasks, {len(document['catalog'])} skills, "
            f"{len(document['excluded'])} exclusions"
        )


if __name__ == "__main__":
    main()
