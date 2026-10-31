"""Bounded read-only evidence for one fixed GitHub skill profile (RM34/RM38/RM43).

This developer probe cannot register/activate a source. No fetched asset runs.
"""

import argparse
import asyncio
import hashlib
import json
import re
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx

from relaymind.assets.settings import SourceConfig
from relaymind.assets.sources import (
    CaptureContext,
    SourceFailure,
    SourceInventory,
    _json,
    _relative_path,
    capture_source,
)

PROFILE = "github.relaymind-skills.v1"
REPOSITORY = "Last-Humans-Coding-Under-White-Nights/relaymind"
API = "https://api.github.com/repos/" + REPOSITORY
RAW = "https://raw.githubusercontent.com/" + REPOSITORY
API_VERSION = "2022-11-28"
LICENSE_SHA256 = "a0df745636ded7b39d42761f4e5000fb0718c04e98be6b004bf00efdcd79509a"
_ALLOWED_HEADERS = (
    "etag",
    "x-poll-interval",
    "retry-after",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
)
_RIGHTS = re.compile(r"^(license|licence|copying|copyright|notice)(?:[.\-_].*)?$", re.I)
_SHA = re.compile(r"[0-9a-f]{40}\Z")


def _sha(value: object) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise SourceFailure("invalid_data")
    return value


def _tree(data: dict, expected: str) -> dict:
    if data.get("sha") != expected or data.get("truncated") is not False:
        raise SourceFailure("invalid_data")
    entries = data.get("tree")
    if not isinstance(entries, list) or len(entries) > 1000:
        raise SourceFailure("record_limit")
    result = {}
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SourceFailure("invalid_data")
        path = _relative_path(entry.get("path"))
        normalized = unicodedata.normalize("NFC", path).casefold()
        if normalized in seen:
            raise SourceFailure("invalid_data")
        seen.add(normalized)
        _sha(entry.get("sha"))
        if (entry.get("type"), entry.get("mode")) not in (
            ("blob", "100644"),
            ("blob", "100755"),
            ("blob", "120000"),
            ("tree", "040000"),
            ("commit", "160000"),
        ):
            raise SourceFailure("invalid_data")
        result[path] = entry
    return result


def _ordinary(entry: dict, limit: int) -> None:
    if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
        raise SourceFailure("invalid_data")
    size = entry.get("size")
    if type(size) is not int or not 0 <= size <= limit:
        raise SourceFailure("size_limit")


def _blob(raw: bytes, entry: dict) -> dict:
    digest = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    if len(raw) != entry["size"] or digest != entry["sha"]:
        raise SourceFailure("invalid_data")
    return dict(bytes=len(raw), git_blob_sha1=digest, sha256=hashlib.sha256(raw).hexdigest())


def _prior(path: Path, now: datetime) -> dict:
    observations = json.loads((path / "observations.json").read_text())
    entry = next(row for row in observations if row["label"] == "ref")
    raw = (path / "ref.raw").read_bytes()
    if (
        entry["url"] != API + "/git/ref/heads/master"
        or entry["status"] != 200
        or len(raw) != entry["bytes"]
        or hashlib.sha256(raw).hexdigest() != entry["sha256"]
    ):
        raise SourceFailure("invalid_data")
    document = json.loads(raw)
    commit = _sha(document["object"]["sha"])
    captured = datetime.fromisoformat(entry["captured_at"])
    floor = max(300, int(entry.get("headers", {}).get("X-Poll-Interval", "300")))
    if captured.tzinfo is None or now < captured + timedelta(seconds=floor):
        raise SourceFailure("source_unavailable")
    return dict(
        commit=commit,
        captured_at=entry["captured_at"],
        sha256=entry["sha256"],
        minimum_seconds=floor,
        elapsed_seconds=(now - captured).total_seconds(),
    )


async def qualify_github_source(
    output_dir: Path, *, transport=None, prior_capture: Path | None = None
) -> dict:
    """Capture/verify one immutable selected scope, with optional earlier actual ref evidence."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    context = CaptureContext(now)
    report = dict(
        profile=PROFILE,
        api_version=API_VERSION,
        started_at=now.isoformat(),
        qualification="provisional",
        production_activation=False,
        complete=False,
        candidate_count=0,
        files=[],
        requests=[],
        checks={"acquisition": "not_run"},
        second_observation="not_run",
    )

    async def get(client, url, label, limit):
        context.check_limits()
        remaining = min(10.0, context.deadline - time.monotonic())
        if remaining <= 0:
            raise SourceFailure("deadline")
        observation = dict(
            label=label,
            method="GET",
            url=url,
            captured_at=datetime.now(UTC).isoformat(),
            status=None,
            bytes=0,
            sha256=None,
            headers={},
        )
        report["requests"].append(observation)
        data = bytearray()
        async with asyncio.timeout(remaining):
            async with client.stream("GET", url) as response:
                observation["status"] = response.status_code
                observation["headers"] = {
                    k: response.headers[k] for k in _ALLOWED_HEADERS if k in response.headers
                }
                if response.status_code != 200:
                    raise SourceFailure("source_unavailable")
                if len(report["requests"]) > 1000:
                    raise SourceFailure("request_limit")
                async for chunk in response.aiter_raw(1024 * 1024):
                    context.check_limits()
                    context.add_bytes(len(chunk))
                    observation["bytes"] += len(chunk)
                    if len(data) + len(chunk) > limit:
                        raise SourceFailure("size_limit")
                    data.extend(chunk)
        raw = bytes(data)
        observation["sha256"] = hashlib.sha256(raw).hexdigest()
        (output_dir / (label + ".raw")).write_bytes(raw)
        return raw

    try:
        prior = None if prior_capture is None else _prior(Path(prior_capture), now)
        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=10.0,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "RelayMind-source-qualification",
                "Accept-Encoding": "identity",
            },
        ) as client:
            reference = _json(
                await get(client, API + "/git/ref/heads/master", "ref", 4 * 1024 * 1024), context
            )
            if (
                reference.get("ref") != "refs/heads/master"
                or reference.get("object", {}).get("type") != "commit"
            ):
                raise SourceFailure("invalid_data")
            commit = _sha(reference["object"].get("sha"))
            document = _json(
                await get(client, API + "/git/commits/" + commit, "commit", 4 * 1024 * 1024),
                context,
            )
            if document.get("sha") != commit:
                raise SourceFailure("invalid_data")
            root_id = _sha(document.get("tree", {}).get("sha"))
            root = _tree(
                _json(
                    await get(client, API + "/git/trees/" + root_id, "root-tree", 4 * 1024 * 1024),
                    context,
                ),
                root_id,
            )
            rights = [path for path in root if _RIGHTS.fullmatch(path)]
            if rights != ["LICENSE"]:
                raise SourceFailure("invalid_data")
            license_entry = root["LICENSE"]
            _ordinary(license_entry, 4 * 1024 * 1024)
            raw = await get(client, RAW + "/" + commit + "/LICENSE", "license", 4 * 1024 * 1024)
            identity = _blob(raw, license_entry)
            if identity["sha256"] != LICENSE_SHA256:
                raise SourceFailure("invalid_data")
            report["rights"] = dict(path="LICENSE", license="MIT", **identity)
            report["files"].append(dict(path="LICENSE", **identity))
            if "skills" not in root:
                entries = {}
                tree_id = None
            else:
                if (root["skills"].get("mode"), root["skills"].get("type")) != ("040000", "tree"):
                    raise SourceFailure("invalid_data")
                tree_id = _sha(root["skills"]["sha"])
                entries = _tree(
                    _json(
                        await get(
                            client,
                            API + "/git/trees/" + tree_id + "?recursive=1",
                            "skills-tree",
                            4 * 1024 * 1024,
                        ),
                        context,
                    ),
                    tree_id,
                )
            # A newly nested permission/notice is an unreviewed rights layout.
            if any(_RIGHTS.fullmatch(path.split("/")[-1]) for path in entries):
                raise SourceFailure("invalid_data")
            for path, entry in entries.items():
                if "/" not in path and entry["mode"] in ("120000", "160000"):
                    raise SourceFailure("invalid_data")
            if "SKILL.md" in entries:
                packages = ["."]
            else:
                packages = sorted(
                    path[:-9]
                    for path in entries
                    if path.count("/") == 1 and path.endswith("/SKILL.md")
                )
            selected = []
            for package in packages:
                context.add_record()
                if package != ".":
                    parent = entries.get(package, {})
                    if (parent.get("mode"), parent.get("type")) != ("040000", "tree"):
                        raise SourceFailure("invalid_data")
                for name, limit in (
                    ("SKILL.md", 10 * 1024 * 1024),
                    ("skill.manifest.json", 4 * 1024 * 1024),
                ):
                    relative = name if package == "." else package + "/" + name
                    if relative not in entries:
                        continue
                    entry = entries[relative]
                    _ordinary(entry, limit)
                    selected.append((relative, entry, limit))
            for ordinal, (relative, entry, limit) in enumerate(selected):
                locator = "skills/" + relative
                raw = await get(
                    client,
                    RAW + "/" + commit + "/" + quote(locator, safe="/"),
                    f"original-{ordinal:04}",
                    limit,
                )
                identity = _blob(raw, entry)
                path = output_dir / "originals" / locator
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                report["files"].append(dict(path=locator, **identity))
            (output_dir / "originals/skills").mkdir(parents=True, exist_ok=True)
            validation = CaptureContext(now)
            validation.deadline = context.deadline
            capture = capture_source(
                SourceConfig(
                    "qualification-only", "local_directory", output_dir / "originals/skills"
                ),
                previous=SourceInventory.empty("qualification-only"),
                context=validation,
            )
            if len(capture.documents) != len(packages):
                raise SourceFailure("invalid_data")
            context.check_limits()
            report.update(
                complete=True,
                candidate_count=len(packages),
                commit=commit,
                root_tree=root_id,
                skills_tree=tree_id,
                received_bytes=context.received_bytes,
                checks={
                    "acquisition": "pass",
                    "rights": "pass",
                    "original_blob_hashes": "pass",
                    "ordinary_skill_validation": "pass",
                },
                package_versions=[
                    asset.record.identity["package_version"] for asset in capture.documents
                ],
            )
            if prior is not None:
                report["second_observation"] = dict(
                    prior=prior, current_commit=commit, movement_observed=prior["commit"] != commit
                )
    except (
        SourceFailure,
        httpx.HTTPError,
        TimeoutError,
        UnicodeError,
        ValueError,
        KeyError,
        TypeError,
    ) as failure:
        reason = failure.reason if isinstance(failure, SourceFailure) else "source_unavailable"
        report.update(reason=reason, checks={"acquisition": "fail"}, complete=False)
    finally:
        report["duration_ms"] = max(0, (time.monotonic() - context.batch_started) * 1000)
        report["finished_at"] = datetime.now(UTC).isoformat()
        (output_dir / "observations.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prior-capture", type=Path)
    args = parser.parse_args()
    result = asyncio.run(qualify_github_source(args.output, prior_capture=args.prior_capture))
    print(
        json.dumps(
            {key: result.get(key) for key in ("complete", "candidate_count", "commit", "reason")}
        )
    )
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
