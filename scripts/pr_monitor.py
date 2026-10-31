"""Watch other agents' pull requests and real reviewers' findings.

This is developer tooling for this repository's agent workflow, not product
code. It answers one question on every poll: *which pull request or reviewer
finding has appeared since the last poll and has not been acknowledged yet?*

The monitor is intentionally split in two:

* detection and state (``build_events``, ``deliver``, ``acknowledge``) are
  pure and tested without GitHub;
* ``gh`` and ``git`` calls are boundary code at the bottom.

An event stays *pending* until ``--ack`` records that an agent handled it.
That way an interrupted or failed handler is retried instead of silently
losing a reviewer finding. An agent handling an event must write a failing
test first, fix the PR in its own branch, push without force and without AI
attribution, update any documentation that the fix makes stale, and reply in
the reviewer's thread before acknowledging it.

Usage:
    python scripts/pr_monitor.py --once
    python scripts/pr_monitor.py --peek --json
    python scripts/pr_monitor.py --watch --interval 300
    python scripts/pr_monitor.py --ack review_comment:5001

The state lives in the repository's common Git directory, so every worktree
shares it and nothing is committed:

    <git-common-dir>/relaymind-pr-monitor/state.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

STATE_VERSION = 1
DEFAULT_INTERVAL = 300.0
DEFAULT_OVERLAP = 300.0
DEFAULT_BOOTSTRAP = 3600.0
DEFAULT_RETRY_AFTER = 1800.0
MAX_SEEN = 5000
MAX_PENDING = 200
MAX_BODY = 4000

PULL_NUMBER_RE = re.compile(r"/pulls?/(?P<number>\d+)(?:$|[/?#])")

# Accounts whose comments are automation noise, not a real reviewer's finding.
# Any login ending in ``[bot]`` is also treated as a bot.
BOT_LOGINS = {
    "github-actions",
    "dependabot",
    "renovate",
    "codecov",
    "sonarcloud",
    "coderabbitai",
    "copilot-pull-request-reviewer",
    "copilot",
}


class MonitorError(RuntimeError):
    """A monitor input or state file is not usable."""


class LockBusy(MonitorError):
    """Another monitor process owns the state lock."""


@dataclass
class Event:
    """One actionable GitHub event."""

    id: str
    kind: str
    action: str
    created_at: str
    author: str
    body: str
    url: str
    pr: dict[str, Any]
    path: str | None = None
    line: int | None = None
    diff_hunk: str | None = None
    review_state: str | None = None
    in_reply_to: int | None = None
    commit_id: str | None = None
    reemit: bool = False
    first_seen: str | None = None
    deliveries: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MonitorError(f"bad timestamp {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _after(value: str | None, since: datetime) -> bool:
    moment = parse_time(value)
    return moment is not None and moment > since


def pull_number(url: str | None) -> int | None:
    if not url:
        return None
    match = PULL_NUMBER_RE.search(url)
    return int(match.group("number")) if match else None


def _login(value: Mapping[str, Any] | str | None) -> str:
    if isinstance(value, Mapping):
        return str(value.get("login") or "")
    return str(value or "")


def is_bot(login: str) -> bool:
    lowered = login.lower()
    return lowered.endswith("[bot]") or lowered in BOT_LOGINS


def _is_ignored_author(login: str, self_login: str, ignore_logins: set[str]) -> bool:
    if not login:
        return True
    lowered = login.lower()
    if self_login and lowered == self_login.lower():
        return True
    if lowered in {value.lower() for value in ignore_logins}:
        return True
    return is_bot(login)


def _pr_ref(pr: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "number": int(pr["number"]),
        "title": str(pr.get("title") or ""),
        "url": str(pr.get("url") or ""),
        "head": str(pr.get("headRefName") or ""),
        "base": str(pr.get("baseRefName") or ""),
        "author": _login(pr.get("author")),
        "is_draft": bool(pr.get("isDraft")),
        "review_decision": str(pr.get("reviewDecision") or ""),
    }


def _pr_event(pr: Mapping[str, Any]) -> Event:
    return Event(
        id=f"new_pr:{pr['number']}",
        kind="new_pr",
        action="review_and_fix",
        created_at=str(pr.get("createdAt") or ""),
        author=_login(pr.get("author")),
        body=str(pr.get("body") or "")[:MAX_BODY],
        url=str(pr.get("url") or ""),
        pr=_pr_ref(pr),
    )


def draft_ready_events(prs: Sequence[Mapping[str, Any]], known_drafts: set[int]) -> list[Event]:
    """Return ready PRs that were previously observed as drafts."""
    return [
        _pr_event(pr) for pr in prs if not pr.get("isDraft") and int(pr["number"]) in known_drafts
    ]


def _review_event(
    pr: Mapping[str, Any], review: Mapping[str, Any], include_approvals: bool
) -> Event | None:
    state = str(review.get("state") or "").upper()
    if state in {"", "PENDING", "DISMISSED"}:
        return None
    body = str(review.get("body") or "")[:MAX_BODY]
    if state == "APPROVED":
        if not include_approvals:
            return None
        action = "none"
    elif state == "CHANGES_REQUESTED":
        action = "fix_findings"
    else:
        action = "assess_findings"
        if not body:
            return None
    review_id = int(review["id"])
    return Event(
        id=f"review:{review_id}",
        kind="review",
        action=action,
        created_at=str(review.get("submitted_at") or ""),
        author=_login(review.get("user")),
        body=body,
        url=str(review.get("html_url") or f"{pr.get('url', '')}#pullrequestreview-{review_id}"),
        pr=_pr_ref(pr),
        review_state=state,
    )


def _review_comment_event(pr: Mapping[str, Any], comment: Mapping[str, Any]) -> Event:
    comment_id = int(comment["id"])
    reply_to = comment.get("in_reply_to_id")
    return Event(
        id=f"review_comment:{comment_id}",
        kind="review_comment",
        # A reviewer's reply can add a new finding; a fresh thread is a finding.
        action="assess_findings" if reply_to else "fix_findings",
        created_at=str(comment.get("created_at") or ""),
        author=_login(comment.get("user")),
        body=str(comment.get("body") or "")[:MAX_BODY],
        url=str(comment.get("html_url") or ""),
        pr=_pr_ref(pr),
        path=str(comment.get("path") or "") or None,
        line=comment.get("line") or comment.get("original_line"),
        diff_hunk=str(comment.get("diff_hunk") or "")[:MAX_BODY] or None,
        in_reply_to=int(reply_to) if reply_to else None,
        commit_id=str(comment.get("commit_id") or "") or None,
    )


def _issue_comment_event(pr: Mapping[str, Any], comment: Mapping[str, Any]) -> Event:
    comment_id = int(comment["id"])
    return Event(
        id=f"issue_comment:{comment_id}",
        kind="issue_comment",
        action="assess_findings",
        created_at=str(comment.get("created_at") or ""),
        author=_login(comment.get("user")),
        body=str(comment.get("body") or "")[:MAX_BODY],
        url=str(comment.get("html_url") or ""),
        pr=_pr_ref(pr),
    )


def build_events(
    *,
    prs: Sequence[Mapping[str, Any]],
    reviews: Mapping[int, Sequence[Mapping[str, Any]]],
    review_comments: Sequence[Mapping[str, Any]],
    issue_comments: Sequence[Mapping[str, Any]],
    since: datetime,
    self_login: str = "",
    ignore_logins: set[str] | None = None,
    ignore_prs: set[int] | None = None,
    ignore_branches: set[str] | None = None,
    include_drafts: bool = False,
    include_approvals: bool = False,
) -> list[Event]:
    """Return the events after ``since`` that an agent should handle.

    ``self_login`` and bot accounts are removed from *reviewer* events only:
    another agent may use the same GitHub account, so a pull request authored
    by that account is still reviewed. The monitor's own branch is excluded by
    ``ignore_branches`` instead.
    """

    ignore_logins = ignore_logins or set()
    ignore_prs = ignore_prs or set()
    ignore_branches = ignore_branches or set()

    open_prs: dict[int, Mapping[str, Any]] = {}
    events: list[Event] = []
    for pr in prs:
        number = int(pr["number"])
        open_prs[number] = pr
        if number in ignore_prs:
            continue
        if pr.get("headRefName") in ignore_branches:
            continue
        if pr.get("isDraft") and not include_drafts:
            continue
        if not _after(pr.get("createdAt"), since):
            continue
        events.append(_pr_event(pr))

    for number, items in reviews.items():
        pr = open_prs.get(int(number))
        if pr is None:
            continue
        for review in items:
            if not _after(review.get("submitted_at"), since):
                continue
            if _is_ignored_author(_login(review.get("user")), self_login, ignore_logins):
                continue
            event = _review_event(pr, review, include_approvals)
            if event is not None:
                events.append(event)

    for comment in review_comments:
        pr = open_prs.get(pull_number(comment.get("pull_request_url")) or 0)
        if pr is None:
            continue
        if not _after(comment.get("created_at"), since):
            continue
        if _is_ignored_author(_login(comment.get("user")), self_login, ignore_logins):
            continue
        events.append(_review_comment_event(pr, comment))

    for comment in issue_comments:
        number = pull_number(comment.get("html_url"))
        if number is None:
            # An issue comment on a plain issue is outside the PR monitor.
            continue
        pr = open_prs.get(number)
        if pr is None:
            continue
        if not _after(comment.get("created_at"), since):
            continue
        if _is_ignored_author(_login(comment.get("user")), self_login, ignore_logins):
            continue
        events.append(_issue_comment_event(pr, comment))

    unique: dict[str, Event] = {}
    for event in events:
        unique.setdefault(event.id, event)
    return sorted(unique.values(), key=lambda event: (event.created_at, event.id))


def new_state(now: datetime, initialized_at: datetime | None = None) -> dict[str, Any]:
    start = to_iso(initialized_at or now)
    return {
        "version": STATE_VERSION,
        "initialized_at": start,
        "last_check": start,
        "seen": [],
        "pending": {},
        "drafts": [],
    }


def load_state(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise MonitorError(f"cannot read monitor state {path}: {error}") from error
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as error:
        raise MonitorError(f"monitor state {path} is not valid JSON: {error}") from error
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise MonitorError(
            f"monitor state {path} has version "
            f"{state.get('version') if isinstance(state, dict) else '?'}; "
            f"expected {STATE_VERSION}"
        )
    for key in ("initialized_at", "last_check", "seen", "pending"):
        if key not in state:
            raise MonitorError(f"monitor state {path} is missing {key!r}")
    if not isinstance(state["seen"], list) or not isinstance(state["pending"], dict):
        raise MonitorError(f"monitor state {path} has malformed seen/pending")
    if "drafts" not in state:
        # Existing state from before draft tracking: no drafts are known yet.
        state["drafts"] = []
    if not isinstance(state["drafts"], list):
        raise MonitorError(f"monitor state {path} has malformed drafts")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = json.dumps(state, indent=2, sort_keys=True) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _is_due(meta: Mapping[str, Any], now: datetime, retry_after: float) -> bool:
    last = parse_time(meta.get("last")) or datetime.min.replace(tzinfo=UTC)
    return (now - last).total_seconds() >= retry_after


def _replay(meta: Mapping[str, Any], now: datetime) -> Event:
    event = Event(**meta["event"])
    event.reemit = True
    event.first_seen = meta.get("first")
    event.deliveries = int(meta.get("deliveries") or 1) + 1
    return event


def deliver(
    state: dict[str, Any],
    events: Sequence[Event],
    *,
    now: datetime,
    retry_after: float = DEFAULT_RETRY_AFTER,
    persist: bool = True,
) -> list[Event]:
    """Return new events and due pending ones, recording them when asked.

    A fresh event is written to ``pending`` as well as ``seen``. It remains
    pending until :func:`acknowledge`; after ``retry_after`` seconds without an
    acknowledgement it is returned again with ``reemit=True``.
    """

    now_iso = to_iso(now)
    output: list[Event] = []
    fresh_ids = set()

    for event in events:
        fresh_ids.add(event.id)
        if event.id in state["seen"]:
            meta = state["pending"].get(event.id)
            if meta is not None and _is_due(meta, now, retry_after):
                output.append(_replay(meta, now))
                if persist:
                    meta["last"] = now_iso
                    meta["deliveries"] = int(meta.get("deliveries") or 1) + 1
            continue
        if persist:
            state["seen"].append(event.id)
            state["pending"][event.id] = {
                "first": now_iso,
                "last": now_iso,
                "deliveries": 1,
                "event": event.as_dict(),
            }
        output.append(event)

    if persist:
        overflow = len(state["seen"]) - MAX_SEEN
        if overflow > 0:
            state["seen"] = state["seen"][overflow:]
        while len(state["pending"]) > MAX_PENDING:
            oldest = min(state["pending"], key=lambda key: state["pending"][key].get("first", ""))
            del state["pending"][oldest]

    # Pending events that were not in this snapshot still need a retry; this
    # covers a PR briefly missing from the open list or a fetch that skipped it.
    for event_id, meta in list(state["pending"].items()):
        if event_id in fresh_ids:
            continue
        if not _is_due(meta, now, retry_after):
            continue
        output.append(_replay(meta, now))
        if persist:
            meta["last"] = now_iso
            meta["deliveries"] = int(meta.get("deliveries") or 1) + 1

    return output


def acknowledge(state: dict[str, Any], event_ids: Sequence[str]) -> int:
    known = set(state["pending"])
    unknown = [event_id for event_id in event_ids if event_id not in known]
    if unknown:
        raise MonitorError(
            "not pending (already acknowledged or never seen): " + ", ".join(sorted(unknown))
        )
    for event_id in event_ids:
        del state["pending"][event_id]
    return len(event_ids)


# ---------------------------------------------------------------------------
# GitHub and Git boundary
# ---------------------------------------------------------------------------


def _run(args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(args, text=True, capture_output=True)
    if check and process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()
        raise MonitorError(f"{' '.join(args)} failed: {detail}")
    return process


def _gh_json(args: Sequence[str]) -> Any:
    process = _run(["gh", *args])
    try:
        return json.loads(process.stdout or "null")
    except json.JSONDecodeError as error:
        raise MonitorError(f"gh returned invalid JSON: {error}") from error


def gh_api(
    repo: str,
    endpoint: str,
    *,
    since: datetime | None = None,
    paginate: bool = True,
) -> Any:
    path, _, existing = endpoint.partition("?")
    params: dict[str, str] = {}
    if existing:
        for pair in existing.split("&"):
            key, _, value = pair.partition("=")
            if key:
                params[key] = value
    if since is not None:
        params["since"] = to_iso(since)
    url = f"repos/{repo}/{path}"
    if params:
        url += "?" + urlencode(params)
    args = ["api", "--method", "GET", url]
    if paginate:
        args += ["--paginate", "--slurp"]
    data = _gh_json(args)
    if paginate:
        pages = data if isinstance(data, list) else [data]
        flattened: list[Any] = []
        for page in pages:
            flattened.extend(page if isinstance(page, list) else [page])
        return flattened
    return data


def discover_repo() -> str:
    return _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    ).stdout.strip()


def discover_self_login() -> str:
    try:
        return _run(["gh", "api", "user", "-q", ".login"]).stdout.strip()
    except MonitorError:
        return ""


def discover_branch() -> str:
    process = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False)
    if process.returncode != 0:
        return ""
    branch = process.stdout.strip()
    return "" if branch == "HEAD" else branch


def default_state_path() -> Path:
    process = _run(["git", "rev-parse", "--git-common-dir"], check=False)
    if process.returncode == 0 and process.stdout.strip():
        common = Path(process.stdout.strip())
        if not common.is_absolute():
            common = Path.cwd() / common
        return common.resolve() / "relaymind-pr-monitor" / "state.json"
    return Path.home() / ".local" / "state" / "relaymind-pr-monitor" / "state.json"


def fetch_open_prs(repo: str) -> list[dict[str, Any]]:
    fields = (
        "number,title,url,author,headRefName,baseRefName,createdAt,updatedAt,"
        "isDraft,reviewDecision,body"
    )
    data = _gh_json(
        ["pr", "list", "--repo", repo, "--state", "open", "--limit", "100", "--json", fields]
    )
    return data if isinstance(data, list) else []


def fetch_reviews(repo: str, number: int) -> list[dict[str, Any]]:
    data = gh_api(repo, f"pulls/{number}/reviews?per_page=100", paginate=True)
    return data if isinstance(data, list) else []


def fetch_review_comments(repo: str, since: datetime) -> list[dict[str, Any]]:
    data = gh_api(
        repo,
        "pulls/comments?sort=created&direction=desc&per_page=100",
        since=since,
        paginate=True,
    )
    return data if isinstance(data, list) else []


def fetch_issue_comments(repo: str, since: datetime) -> list[dict[str, Any]]:
    data = gh_api(
        repo,
        "issues/comments?sort=created&direction=desc&per_page=100",
        since=since,
        paginate=True,
    )
    return data if isinstance(data, list) else []


def collect_snapshot(repo: str, since: datetime) -> dict[str, Any]:
    prs = fetch_open_prs(repo)
    reviews = {int(pr["number"]): fetch_reviews(repo, int(pr["number"])) for pr in prs}
    return {
        "prs": prs,
        "reviews": reviews,
        "review_comments": fetch_review_comments(repo, since),
        "issue_comments": fetch_issue_comments(repo, since),
    }


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # pragma: no cover - exercised on Windows
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except BlockingIOError as error:
            raise LockBusy(f"another monitor run holds {path}") from error
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover - exercised on Windows
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def _event_summary(event: Event) -> str:
    location = ""
    if event.path:
        location = f" {event.path}"
        if event.line:
            location += f":{event.line}"
    body = " ".join(event.body.split())
    if len(body) > 180:
        body = body[:177] + "..."
    prefix = "re-emitted " if event.reemit else ""
    return (
        f"- {prefix}[{event.kind}] #{event.pr['number']} {event.pr['title']}"
        f"{location} by {event.author}: {body}"
    )


def _print_events(result: Mapping[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    events: Sequence[Event] = result["events"]
    if not events:
        print("PR monitor: no new events")
        return
    print(f"PR monitor: {len(events)} event(s) in {result['repo']}")
    for event in events:
        print(_event_summary(event))


def _since_for(state: Mapping[str, Any], args: argparse.Namespace, now: datetime) -> datetime:
    if args.since:
        return parse_time(args.since) or now
    if args.backfill:
        return datetime(1970, 1, 1, tzinfo=UTC)
    last = parse_time(state.get("last_check")) or now
    initialized = parse_time(state.get("initialized_at")) or now
    base = last if last > initialized else initialized
    return base - timedelta(seconds=args.overlap)


def _load_or_new(
    path: Path, now: datetime, args: argparse.Namespace
) -> tuple[dict[str, Any], bool]:
    if path.exists():
        return load_state(path), False
    if args.since:
        initialized = parse_time(args.since) or now
    else:
        initialized = now - timedelta(seconds=args.bootstrap)
    return new_state(now, initialized_at=initialized), True


def _poll_once(args: argparse.Namespace, *, persist: bool) -> tuple[int, dict[str, Any]]:
    now = utc_now()
    state_path = Path(args.state) if args.state else default_state_path()
    state, _ = _load_or_new(state_path, now, args)
    since = _since_for(state, args, now)

    repo = args.repo or discover_repo()
    snapshot = collect_snapshot(repo, since)
    self_login = args.self_login if args.self_login is not None else discover_self_login()
    ignore_branches = set(args.ignore_branch or [])
    branch = discover_branch()
    if branch and branch != "HEAD":
        ignore_branches.add(branch)

    events = build_events(
        prs=snapshot["prs"],
        reviews=snapshot["reviews"],
        review_comments=snapshot["review_comments"],
        issue_comments=snapshot["issue_comments"],
        since=since,
        self_login=self_login,
        ignore_logins=set(args.ignore_login or []),
        ignore_prs={int(value) for value in (args.ignore_pr or [])},
        ignore_branches=ignore_branches,
        include_drafts=args.include_drafts,
        include_approvals=args.include_approvals,
    )
    # A draft that becomes ready may be older than the sliding watermark; the
    # draft record lets it surface once without reviewing it while in progress.
    previous_drafts = {int(number) for number in state.get("drafts", [])}
    ready = draft_ready_events(snapshot["prs"], previous_drafts)
    already = {event.id for event in events}
    events.extend(event for event in ready if event.id not in already)
    delivered = deliver(
        state,
        events,
        now=now,
        retry_after=args.retry_after,
        persist=persist,
    )
    if persist:
        open_numbers = {int(pr["number"]) for pr in snapshot["prs"]}
        drafts_now = {int(pr["number"]) for pr in snapshot["prs"] if pr.get("isDraft")}
        emitted = {int(event.pr["number"]) for event in ready}
        state["drafts"] = sorted(((previous_drafts | drafts_now) & open_numbers) - emitted)
        state["last_check"] = to_iso(now)
        save_state(state_path, state)

    result = {
        "repo": repo,
        "generated_at": to_iso(now),
        "state": str(state_path),
        "count": len(delivered),
        "events": [event.as_dict() for event in delivered],
    }
    return (0 if delivered else 1), result


def _ack(args: argparse.Namespace) -> int:
    state_path = Path(args.state) if args.state else default_state_path()
    state = load_state(state_path)
    ids = list(args.ack or [])
    if args.ack_all:
        ids.extend(state["pending"])
    if not ids:
        raise MonitorError("--ack needs at least one event id, or use --ack-all")
    count = acknowledge(state, ids)
    save_state(state_path, state)
    print(f"PR monitor: acknowledged {count} event(s)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", help="owner/name; defaults to the current gh repository")
    parser.add_argument(
        "--state", type=Path, help="state file; defaults under the common Git directory"
    )
    parser.add_argument("--once", action="store_true", help="poll once and record the result")
    parser.add_argument("--watch", action="store_true", help="poll repeatedly")
    parser.add_argument(
        "--peek", action="store_true", help="poll without consuming state; exit 0 when work exists"
    )
    parser.add_argument("--ack", nargs="+", metavar="ID", help="mark event ids handled")
    parser.add_argument("--ack-all", action="store_true", help="mark every pending event handled")
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL, help="watch interval in seconds"
    )
    parser.add_argument(
        "--overlap", type=float, default=DEFAULT_OVERLAP, help="re-read window in seconds"
    )
    parser.add_argument(
        "--bootstrap",
        type=float,
        default=DEFAULT_BOOTSTRAP,
        help="first-run lookback in seconds when no state exists",
    )
    parser.add_argument(
        "--retry-after",
        type=float,
        default=DEFAULT_RETRY_AFTER,
        help="seconds before an unacknowledged event is re-emitted",
    )
    parser.add_argument(
        "--since", help="explicit ISO 8601 lower bound (also first-run initialization)"
    )
    parser.add_argument(
        "--backfill", action="store_true", help="consider every open PR and comment"
    )
    parser.add_argument(
        "--self-login", help="GitHub login whose comments are not reviewer findings"
    )
    parser.add_argument(
        "--ignore-login", action="append", help="reviewer login to ignore (repeatable)"
    )
    parser.add_argument(
        "--ignore-pr", action="append", type=int, help="PR number to ignore (repeatable)"
    )
    parser.add_argument(
        "--ignore-branch", action="append", help="head branch to ignore (repeatable)"
    )
    parser.add_argument("--include-drafts", action="store_true", help="also review draft PRs")
    parser.add_argument(
        "--include-approvals", action="store_true", help="emit approvals as informational"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.ack or args.ack_all:
        if args.watch or args.peek:
            parser.error("--ack cannot be combined with --watch or --peek")
        return _ack(args)

    if args.watch and args.peek:
        parser.error("--watch and --peek are mutually exclusive")
    if args.interval <= 0 or args.overlap < 0 or args.bootstrap < 0 or args.retry_after < 0:
        parser.error(
            "--interval must be positive; --overlap, --bootstrap and --retry-after "
            "cannot be negative"
        )

    if args.watch:
        while True:
            try:
                with exclusive_lock(
                    (Path(args.state) if args.state else default_state_path()).with_suffix(".lock")
                ):
                    _, result = _poll_once(args, persist=True)
                if result["count"] or not args.json:
                    _print_events(result, as_json=args.json)
                time.sleep(args.interval)
            except KeyboardInterrupt:
                return 0
            except LockBusy as error:
                print(f"PR monitor: {error}", file=sys.stderr)
                time.sleep(args.interval)

    if args.peek:
        _, result = _poll_once(args, persist=False)
        _print_events(result, as_json=args.json)
        return 0 if result["count"] else 1

    state_path = Path(args.state) if args.state else default_state_path()
    try:
        with exclusive_lock(state_path.with_suffix(".lock")):
            _, result = _poll_once(args, persist=True)
    except LockBusy as error:
        print(f"PR monitor: {error}", file=sys.stderr)
        return 0
    _print_events(result, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
