"""The PR monitor: new agent pull requests and real reviewer findings.

Developer tooling, not product code (spec section 13 does not apply). The
monitor is deliberately small and stateful: it detects events once, keeps an
unacknowledged event pending so an interrupted handler does not lose it, and
never talks to GitHub for its state. These tests cover the pure detection and
state rules; the ``gh`` calls are boundary code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from helpers import load_script_module

monitor = load_script_module("pr_monitor")

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def pr(number: int = 224, **overrides) -> dict:
    value = {
        "number": number,
        "title": "A new change",
        "url": f"https://github.com/o/r/pull/{number}",
        "author": {"login": "other-agent"},
        "headRefName": f"other-agent/topic-{number}",
        "baseRefName": "master",
        "createdAt": "2026-10-31T11:59:00Z",
        "updatedAt": "2026-10-31T11:59:00Z",
        "isDraft": False,
        "reviewDecision": "",
        "body": "The pull request body",
    }
    value.update(overrides)
    return value


def review(number: int = 224, **overrides) -> dict:
    value = {
        "id": 9001,
        "user": {"login": "ArcsinX"},
        "state": "CHANGES_REQUESTED",
        "body": "Please fix the missing check.",
        "submitted_at": "2026-10-31T11:59:30Z",
        "html_url": f"https://github.com/o/r/pull/{number}#pullrequestreview-9001",
    }
    value.update(overrides)
    return value


def review_comment(number: int = 224, **overrides) -> dict:
    value = {
        "id": 5001,
        "user": {"login": "ArcsinX"},
        "body": "This path can crash.",
        "path": "src/relaymind/example.py",
        "line": 42,
        "original_line": 42,
        "diff_hunk": "@@ -40,3 +40,4 @@",
        "created_at": "2026-10-31T11:59:40Z",
        "html_url": f"https://github.com/o/r/pull/{number}#discussion_r5001",
        "pull_request_url": f"https://api.github.com/repos/o/r/pulls/{number}",
        "in_reply_to_id": None,
        "commit_id": "a" * 40,
    }
    value.update(overrides)
    return value


def issue_comment(number: int = 224, **overrides) -> dict:
    value = {
        "id": 7001,
        "user": {"login": "ArcsinX"},
        "body": "One more finding at the top level.",
        "created_at": "2026-10-31T11:59:50Z",
        "html_url": f"https://github.com/o/r/pull/{number}#issuecomment-7001",
        "issue_url": f"https://api.github.com/repos/o/r/issues/{number}",
    }
    value.update(overrides)
    return value


def old_pr(number: int = 224, **overrides) -> dict:
    """An open PR that predates the watermark: its reviews still matter."""
    return pr(number, createdAt="2026-10-31T10:00:00Z", **overrides)


def build(**overrides):
    arguments = {
        "prs": [pr()],
        "reviews": {224: [review()]},
        "review_comments": [review_comment()],
        "issue_comments": [issue_comment()],
        "since": NOW - timedelta(minutes=5),
        "self_login": "devnullexile",
        "ignore_logins": set(),
        "ignore_prs": set(),
        "ignore_branches": set(),
        "include_drafts": False,
        "include_approvals": False,
    }
    arguments.update(overrides)
    return monitor.build_events(**arguments)


def test_new_pr_is_detected_once() -> None:
    events = build(reviews={}, review_comments=[], issue_comments=[])

    assert [event.id for event in events] == ["new_pr:224"]
    assert events[0].kind == "new_pr"
    assert events[0].action == "review_and_fix"
    assert events[0].pr["head"] == "other-agent/topic-224"


def test_pr_before_the_watermark_is_not_new() -> None:
    events = build(
        prs=[pr(createdAt="2026-10-31T11:00:00Z")],
        reviews={},
        review_comments=[],
        issue_comments=[],
    )

    assert events == []


def test_draft_and_ignored_prs_are_skipped() -> None:
    events = build(
        prs=[pr(isDraft=True), pr(number=225)],
        reviews={},
        review_comments=[],
        issue_comments=[],
        ignore_prs={225},
    )

    assert events == []


def test_own_branch_is_never_reviewed() -> None:
    events = build(
        prs=[pr(headRefName="devnullexile/DeepSeek-Fix-m10")],
        reviews={},
        review_comments=[],
        issue_comments=[],
        ignore_branches={"devnullexile/DeepSeek-Fix-m10"},
    )

    assert events == []


def test_reviewer_review_and_inline_comment_become_findings() -> None:
    events = build(prs=[old_pr()], since=NOW - timedelta(minutes=5))

    by_id = {event.id: event for event in events}
    assert by_id["review:9001"].action == "fix_findings"
    assert by_id["review:9001"].review_state == "CHANGES_REQUESTED"
    assert by_id["review_comment:5001"].action == "fix_findings"
    assert by_id["review_comment:5001"].path == "src/relaymind/example.py"
    assert by_id["review_comment:5001"].line == 42
    assert by_id["issue_comment:7001"].action == "assess_findings"


def test_self_and_bot_comments_are_not_findings() -> None:
    events = build(
        prs=[old_pr()],
        reviews={224: [review(user={"login": "devnullexile"})]},
        review_comments=[
            review_comment(user={"login": "devnullexile"}),
            review_comment(id=5002, user={"login": "dependabot[bot]"}),
        ],
        issue_comments=[issue_comment(user={"login": "github-actions[bot]"})],
    )

    assert events == []


def test_comments_on_closed_or_unknown_prs_are_ignored() -> None:
    events = build(
        prs=[old_pr()],
        reviews={},
        review_comments=[review_comment(number=999)],
        issue_comments=[issue_comment(number=999, html_url="https://github.com/o/r/issues/9")],
    )

    assert events == []


def test_issue_comment_that_is_not_a_pull_request_is_ignored() -> None:
    comment = issue_comment(html_url="https://github.com/o/r/issues/9#issuecomment-7001")
    comment["issue_url"] = "https://api.github.com/repos/o/r/issues/9"

    assert build(prs=[old_pr()], review_comments=[], issue_comments=[comment], reviews={}) == []


def test_approval_is_informational_and_opt_in() -> None:
    approval = review(state="APPROVED", body="LGTM")

    assert (
        build(prs=[old_pr()], reviews={224: [approval]}, review_comments=[], issue_comments=[])
        == []
    )
    events = build(
        prs=[old_pr()],
        reviews={224: [approval]},
        review_comments=[],
        issue_comments=[],
        include_approvals=True,
    )
    assert [event.action for event in events] == ["none"]
    assert events[0].review_state == "APPROVED"


def test_delivery_dedupes_and_reemits_pending_until_acknowledged() -> None:
    state = monitor.new_state(NOW)
    events = build(prs=[old_pr()], reviews={224: [review()]}, review_comments=[], issue_comments=[])

    first = monitor.deliver(state, events, now=NOW, retry_after=1800)
    second = monitor.deliver(state, events, now=NOW + timedelta(seconds=30), retry_after=1800)
    third = monitor.deliver(state, events, now=NOW + timedelta(seconds=1800), retry_after=1800)

    assert [event.id for event in first] == ["review:9001"]
    assert second == []
    assert [event.id for event in third] == ["review:9001"]
    assert third[0].reemit is True

    monitor.acknowledge(state, ["review:9001"])
    assert monitor.deliver(state, events, now=NOW + timedelta(hours=2), retry_after=1800) == []


def test_acknowledge_rejects_an_unknown_event() -> None:
    state = monitor.new_state(NOW)

    with pytest.raises(monitor.MonitorError):
        monitor.acknowledge(state, ["review:404"])


def test_state_round_trip_is_versioned_and_atomic(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = monitor.new_state(NOW)
    monitor.deliver(
        state,
        build(prs=[old_pr()], reviews={224: [review()]}, review_comments=[], issue_comments=[]),
        now=NOW,
        retry_after=1800,
    )

    monitor.save_state(path, state)

    loaded = monitor.load_state(path)
    assert loaded["version"] == monitor.STATE_VERSION
    assert loaded["pending"]["review:9001"]["event"]["kind"] == "review"
    assert path.read_text().endswith("\n")


def test_a_corrupt_state_file_is_reported_not_silently_reset(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(monitor.MonitorError, match="state"):
        monitor.load_state(path)


def test_peek_does_not_consume_the_state() -> None:
    state = monitor.new_state(NOW)
    events = build(prs=[old_pr()], reviews={224: [review()]}, review_comments=[], issue_comments=[])

    peeked = monitor.deliver(state, events, now=NOW, retry_after=1800, persist=False)

    assert [event.id for event in peeked] == ["review:9001"]
    assert state["pending"] == {}
    assert state["seen"] == []


def test_reply_is_still_a_finding_because_it_may_add_one() -> None:
    events = build(
        prs=[old_pr()],
        reviews={},
        review_comments=[review_comment(in_reply_to_id=4000)],
        issue_comments=[],
    )

    assert [event.action for event in events] == ["assess_findings"]


def test_pull_number_from_api_and_web_urls() -> None:
    assert monitor.pull_number("https://api.github.com/repos/o/r/pulls/224") == 224
    assert monitor.pull_number("https://github.com/o/r/pull/224#issuecomment-1") == 224
    assert monitor.pull_number("https://github.com/o/r/issues/9") is None


def test_login_and_repo_discovery_read_plain_text(monkeypatch) -> None:
    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def run(args, *, check=True):
        if args[:3] == ["gh", "api", "user"]:
            return Result("devnullexile\n")
        return Result("o/r\n")

    monkeypatch.setattr(monitor, "_run", run)

    assert monitor.discover_self_login() == "devnullexile"
    assert monitor.discover_repo() == "o/r"


def test_first_run_uses_the_bootstrap_window(tmp_path) -> None:
    class Args:
        since = None
        backfill = False
        bootstrap = 3600.0

    state, created = monitor._load_or_new(tmp_path / "state.json", NOW, Args())

    assert created is True
    assert monitor.parse_time(state["initialized_at"]) == NOW - timedelta(seconds=3600)
