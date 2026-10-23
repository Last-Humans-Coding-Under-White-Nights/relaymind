"""The demo UI page script, run in a real browser (RM23, RM11, RM21, RM12, RM25, #89).

Spec 0.27 section 9 (Page script test). The page (``app.js``) runs
against the real gateway app over model doubles and a disposable
sandbox; no live model output is used (section 13). Each test checks
what the page sent to the native endpoints, what it showed, and that
the data mode label is visible on every view. Playwright is a
development dependency: without it these tests fail, they do not skip.
"""

import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from browser_helpers import (
    NATIVE_VIEW,
    WAIT_MS,
    browser_page,
    chat_run_id,
    open_run,
    open_ui,
    served,
)
from gateway_helpers import (
    STAGING_OK,
    build_gateway,
    clarify_turn,
    health_tail,
    route_turn,
    text_turn,
    tool_turn,
)
from helpers import FakeClock
from relaymind.orchestrator.local_status import LocalModelClass, LocalStatus
from relaymind.orchestrator.performance import PerformanceSettings
from relaymind.orchestrator.routing_cache import RoutingCache
from ui_helpers import (
    DELETE_ANSWER,
    DELETE_PROMPT,
    HEALTH_PROMPT,
    database_ids,
    delete_gateway,
    external_health_turns,
    health_gateway,
    timed_gateway,
)

WORKFLOW = "health-and-diagnostic-checklist"
ACTORS = {"local_health": "local", "external_checklist": "external", "synthesis": "external"}


@pytest.fixture(scope="module")
def browser():
    """One Chromium for the module; each test gets its own context and page."""
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        try:
            yield chromium
        finally:
            chromium.close()


def start_from_page(page, prompt, mode="auto"):
    page.fill("#prompt", prompt)
    page.select_option("#mode", mode)
    page.click("#run-form button[type=submit]")


def trace(page, state=None):
    selector = ".trace" + (f'[data-state="{state}"]' if state else "")
    return page.locator("#traces " + selector).first


def assert_mode_label_on_every_view(page, mode="DOUBLE"):
    """RM23: the shell label and the label of each trace in view are visible."""
    labels = page.locator(".mode-label")
    expect(labels).to_have_count(1 + page.locator(".trace").count())
    for index in range(labels.count()):
        expect(labels.nth(index)).to_be_visible()
        expect(labels.nth(index)).to_contain_text(mode)


def waiting_input_gateway(tmp_path):
    return timed_gateway(
        tmp_path,
        [clarify_turn(), route_turn("check-service-health"), *health_tail()],
        external_turns=[],
    )


def test_open_a_run_by_id_shows_the_label_and_escapes_once(tmp_path, browser):
    """RM23, RM12: the opened trace has the mode label and each value is escaped once."""
    gateway = health_gateway(tmp_path)
    hostile = 'Is staging-api up? Fish & <b>chips</b> "ok" <img src=x onerror=alert(1)>'
    with served(gateway) as (base, token), browser_page(browser) as (page, seen):
        run_id = chat_run_id(base, token, hostile)
        open_ui(page, base, token)
        assert_mode_label_on_every_view(page)
        open_run(page, run_id)
        expect(trace(page, "completed")).to_be_visible()
        assert_mode_label_on_every_view(page)
        shown = page.locator(".trace .prompt q").inner_text()
        assert shown == hostile  # not &amp;amp; or &lt; (nothing escaped twice)
        assert page.locator(".trace b, .trace img").count() == 0
        assert STAGING_OK in page.locator(".trace .answer").inner_text()
    assert seen.posts == []  # opening a run only reads


def test_the_recorded_answer_options_are_shown(tmp_path, browser):
    """RM23, RM25, spec 0.22: the page shows the stored answer options of a run."""
    gateway = health_gateway(tmp_path)
    with served(gateway) as (base, token), browser_page(browser) as (page, _):
        run_id = chat_run_id(base, token, HEALTH_PROMPT, max_completion_tokens=60)
        open_ui(page, base, token)
        open_run(page, run_id)
        expect(page.locator(".trace .options")).to_contain_text("Token budget: 60.")
        assert_mode_label_on_every_view(page)


def test_shell_and_trace_show_the_runs_phase_two_configuration(tmp_path, browser):
    """RM23, RM25, RM26, AC20: shell labels follow the displayed run's evidence."""
    gateway = build_gateway(
        tmp_path,
        [],
        local_status=LocalStatus(LocalModelClass.ABSENT, "not configured"),
        external_turns=external_health_turns(),
        performance=PerformanceSettings(answer_pass="draft", prefetch=True),
    )
    with served(gateway) as (base, token), browser_page(browser) as (page, _):
        run_id = chat_run_id(base, token, HEALTH_PROMPT)
        open_ui(page, base, token)
        expect(page.locator("#run-configurations")).to_contain_text("No run selected")
        open_run(page, run_id)
        for selector in ("#run-configurations", ".trace .configuration"):
            expect(page.locator(selector)).to_contain_text("Local model: none configured")
            expect(page.locator(selector)).to_contain_text("Local model status: absent")
            expect(page.locator(selector)).to_contain_text("Answer pass: draft")
            expect(page.locator(selector)).to_contain_text("Prefetch: on")
        expect(page.locator("#run-configurations")).to_contain_text(run_id)
        expect(page.locator(".trace .skill")).to_contain_text("Selector: example_match")
        expect(page.locator(".trace .proposals")).to_contain_text("gateway (prefetch)")
        expect(page.locator(".trace .models")).to_contain_text("First token")


def test_a_clarification_is_answered_through_the_native_resume_endpoint(tmp_path, browser):
    """RM05, RM21, AC03: the answer goes to resume with the version the trace carried."""
    gateway = waiting_input_gateway(tmp_path)
    before = datetime.now(UTC)
    with served(gateway) as (base, token), browser_page(browser) as (page, seen):
        open_ui(page, base, token)
        start_from_page(page, "Is the service healthy?")
        waiting = trace(page, "waiting_input")
        expect(waiting).to_be_visible()
        assert_mode_label_on_every_view(page)
        # RM25: the browser time of the submission is shown as the browser's clock.
        note = waiting.locator('[data-slot="submitted"]')
        expect(note).to_contain_text("You submitted the request (browser clock): ")
        stamp = note.inner_text().rsplit(": ", 1)[1]
        submitted = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        assert before <= submitted <= datetime.now(UTC)
        version = int(waiting.get_attribute("data-state-version"))
        run_id = waiting.get_attribute("data-run-id")

        waiting.locator('[data-action="answer"] input').fill("staging-api")
        waiting.locator('[data-action="answer"] button').click()
        expect(trace(page, "completed")).to_be_visible()
        assert_mode_label_on_every_view(page)
        expect(page.locator(".trace .answer")).to_contain_text(STAGING_OK)
        expect(page.locator(".trace .clarification")).to_contain_text("staging-api")
    resume = [c for c in seen.posts if c["path"].endswith("/resume")]
    assert resume == [
        {
            "path": f"/relay/v1/runs/{run_id}/resume",
            "body": {"expected_version": version, "input": "staging-api"},
        }
    ]


def test_approve_goes_to_the_native_endpoint_and_runs_the_mutation_once(tmp_path, browser):
    """RM11, RM21, AC05: approve carries the approval ID, the token, and the version."""
    gateway = delete_gateway(tmp_path, text_turn(DELETE_ANSWER), text_turn(DELETE_ANSWER))
    with served(gateway) as (base, token), browser_page(browser) as (page, seen):
        open_ui(page, base, token)
        start_from_page(page, DELETE_PROMPT)
        waiting = trace(page, "waiting_confirmation")
        expect(waiting).to_be_visible()
        approve = waiting.locator('[data-action="approve"]')
        expect(approve).to_be_enabled()  # the page read the token once
        assert_mode_label_on_every_view(page)
        ids = (
            approve.get_attribute("data-run-id"),
            approve.get_attribute("data-approval-id"),
            int(approve.get_attribute("data-expected-version")),
        )
        approve.click()
        expect(trace(page, "completed")).to_be_visible()
        assert_mode_label_on_every_view(page)
        expect(page.locator(".trace .confirmation")).to_contain_text("approved")
    # RM11: captured approval evidence remains usable after the browser context closes.
    issued = seen.issued_tokens()
    (resume,) = [c for c in seen.posts if c["path"].endswith("/resume")]
    run_id, approval_id, version = ids
    assert len(issued) == 1
    assert resume == {
        "path": f"/relay/v1/runs/{run_id}/resume",
        "body": {
            "expected_version": version,
            "approval_id": approval_id,
            "token": issued[0],
            "approve": True,
        },
    }
    assert "demo-orders-db" not in database_ids(gateway)


def test_deny_goes_to_the_native_endpoint_and_mutates_nothing(tmp_path, browser):
    """RM11, RM21, AC05: deny resumes with approve false; the database stays."""
    gateway = delete_gateway(tmp_path)
    with served(gateway) as (base, token), browser_page(browser) as (page, seen):
        open_ui(page, base, token)
        start_from_page(page, DELETE_PROMPT)
        waiting = trace(page, "waiting_confirmation")
        expect(waiting).to_be_visible()
        deny = waiting.locator('[data-action="deny"]')
        expect(deny).to_be_enabled()
        ids = (
            deny.get_attribute("data-run-id"),
            deny.get_attribute("data-approval-id"),
            int(deny.get_attribute("data-expected-version")),
        )
        deny.click()
        expect(trace(page, "cancelled")).to_be_visible()
        assert_mode_label_on_every_view(page)
        expect(page.locator(".trace .confirmation")).to_contain_text("denied")
    # RM11: captured approval evidence remains usable after the browser context closes.
    issued = seen.issued_tokens()
    run_id, approval_id, version = ids
    assert len(issued) == 1
    assert [c for c in seen.posts if c["path"].endswith("/resume")] == [
        {
            "path": f"/relay/v1/runs/{run_id}/resume",
            "body": {
                "expected_version": version,
                "approval_id": approval_id,
                "token": issued[0],
                "approve": False,
            },
        }
    ]
    assert "demo-orders-db" in database_ids(gateway)


def test_a_waiting_run_is_cancelled_through_the_native_cancel_endpoint(tmp_path, browser):
    """RM14, RM17, AC12: cancel carries the version the trace was rendered for."""
    gateway = waiting_input_gateway(tmp_path)
    with served(gateway) as (base, token), browser_page(browser) as (page, seen):
        open_ui(page, base, token)
        start_from_page(page, "Is the service healthy?")
        waiting = trace(page, "waiting_input")
        expect(waiting).to_be_visible()
        run_id = waiting.get_attribute("data-run-id")
        version = int(waiting.get_attribute("data-state-version"))
        waiting.locator('[data-action="cancel"]').click()
        expect(trace(page, "cancelled")).to_be_visible()
        assert_mode_label_on_every_view(page)
        assert page.locator("[data-action]").count() == 0  # no controls on a finished run
    assert [c for c in seen.posts if c["path"].endswith("/cancel")] == [
        {"path": f"/relay/v1/runs/{run_id}/cancel", "body": {"expected_version": version}}
    ]


def test_the_comparison_runs_both_modes_and_shows_the_label_in_each_arm(tmp_path, browser):
    """RM23, RM25: the page posts the messages only and shows both arms and their timestamps."""
    gateway = health_gateway(tmp_path, external_turns=external_health_turns())
    with (
        served(gateway, fixtures=gateway.sandbox) as (base, token),
        browser_page(browser) as (page, seen),
    ):
        open_ui(page, base, token)
        page.fill("#prompt", HEALTH_PROMPT)
        page.click("#compare")
        arms = page.locator("#comparison .arm")
        expect(arms).to_have_count(2)
        expect(arms.nth(0).locator(":scope > h2")).to_have_text("auto: completed")
        expect(arms.nth(1).locator(":scope > h2")).to_have_text("external_only: completed")
        expect(page.locator("#comparison .trace")).to_have_count(2)
        assert_mode_label_on_every_view(page)
        for index in range(2):
            expect(arms.nth(index).locator('[data-slot="submitted"]')).to_contain_text(
                "browser clock"
            )
    assert seen.posts == [
        {
            "path": "/ui/comparisons",
            "body": {"messages": [{"role": "user", "content": HEALTH_PROMPT}]},
        }
    ]


def test_the_parallel_workflow_shows_branch_and_synthesis_actors(tmp_path, browser):
    """RM15, RM23, AC14: the page lists each stage as its own actor."""
    gateway = build_gateway(
        tmp_path,
        [route_turn(WORKFLOW), tool_turn("get_service_health", service="staging-api")],
        external_turns=[
            text_turn("1. Check the upstream dependencies."),
            text_turn(
                "Observation: staging-api is healthy (checked at 2026-10-20T02:58:00Z).\n\n"
                "Diagnostic checklist (draft, not log-backed):\n- Check the upstream dependencies."
            ),
        ],
        enabled_workflows={WORKFLOW},
    )
    with served(gateway) as (base, token), browser_page(browser) as (page, _):
        open_ui(page, base, token)
        start_from_page(page, "Run the health checklist for staging-api.", "parallel_demo")
        expect(trace(page, "completed")).to_be_visible()
        rows = page.locator(".trace .workflow tr[data-branch]")
        expect(rows).to_have_count(3)
        for branch, actor in ACTORS.items():
            cells = page.locator(f'.trace .workflow tr[data-branch="{branch}"] td')
            expect(cells.nth(0)).to_have_text(branch)
            expect(cells.nth(1)).to_have_text(actor)  # the Actor column
        assert_mode_label_on_every_view(page)


def test_a_cached_route_is_shown_as_the_routing_cache_mechanism(tmp_path, browser):
    """RM24, RM23: the second run's route comes from the cache and the page says so."""
    cache = RoutingCache(clock=FakeClock().monotonic)
    gateway = timed_gateway(
        tmp_path,
        [route_turn("check-service-health"), *health_tail(), *health_tail()],
        external_turns=[],
        routing_cache=cache,
    )
    with served(gateway) as (base, token), browser_page(browser) as (page, _):
        open_ui(page, base, token)
        start_from_page(page, HEALTH_PROMPT)
        expect(page.locator(".trace[data-state=completed]")).to_have_count(1)
        assert "mechanism routing_cache" not in page.locator(".trace .skill").inner_text()
        start_from_page(page, HEALTH_PROMPT)
        expect(page.locator(".trace[data-state=completed]")).to_have_count(2)
        newest = page.locator("#traces .trace").first  # the newest run is prepended
        expect(newest.locator(".skill")).to_contain_text("mechanism routing_cache")
        assert_mode_label_on_every_view(page)
    assert cache.counters()["hits"] == 1


def test_the_label_follows_the_declared_mode(tmp_path, browser):
    """RM23: a live-declared UI shows LIVE in the shell and in the trace."""
    gateway = health_gateway(tmp_path)
    with served(gateway, mode="live") as (base, token), browser_page(browser) as (page, _):
        run_id = chat_run_id(base, token, HEALTH_PROMPT)
        open_ui(page, base, token)
        open_run(page, run_id)
        expect(trace(page, "completed")).to_be_visible()
        assert_mode_label_on_every_view(page, "LIVE")


def test_approve_and_deny_stay_disabled_until_the_token_is_read(tmp_path, browser):
    """RM11, RM21: a click before the page holds the token cannot send a resume without it."""
    gateway = delete_gateway(tmp_path)
    held = []
    with served(gateway) as (base, token), browser_page(browser) as (page, seen):
        page.route(
            lambda url: bool(NATIVE_VIEW.match(urlsplit(url).path)),
            lambda route: held.append(route),
        )
        open_ui(page, base, token)
        start_from_page(page, DELETE_PROMPT)
        waiting = trace(page, "waiting_confirmation")
        expect(waiting).to_be_visible()
        approve = waiting.locator('[data-action="approve"]')
        deny = waiting.locator('[data-action="deny"]')
        deadline = time.monotonic() + WAIT_MS / 1000
        while not held:  # the token read is in flight and held
            assert time.monotonic() < deadline, "the page never read the native run view"
            page.wait_for_timeout(50)
        expect(approve).to_be_disabled()
        expect(deny).to_be_disabled()
        held[0].continue_()
        page.unroute_all()
        expect(approve).to_be_enabled()
        expect(deny).to_be_enabled()
        assert seen.posts == [
            {
                "path": "/relay/v1/runs",
                "body": {
                    "messages": [{"role": "user", "content": DELETE_PROMPT}],
                    "execution_mode": "auto",
                },
            }
        ]


def test_replay_mode_opens_a_run_with_the_label_and_no_controls(tmp_path, browser):
    """RM23: the replay shell has no run form or compare button, and the script still works."""
    gateway = delete_gateway(tmp_path)
    with served(gateway) as (base, token), browser_page(browser) as (page, _):
        open_ui(page, base, token)
        start_from_page(page, DELETE_PROMPT)
        waiting = trace(page, "waiting_confirmation")
        expect(waiting).to_be_visible()
        run_id = waiting.get_attribute("data-run-id")
    errors = []
    with served(gateway, mode="replay") as (base, token), browser_page(browser) as (page, seen):
        page.on("pageerror", lambda error: errors.append(error))
        open_ui(page, base, token)
        assert page.locator("#run-form, #compare").count() == 0
        open_run(page, run_id)
        expect(trace(page, "waiting_confirmation")).to_be_visible()
        assert_mode_label_on_every_view(page, "REPLAY")
        assert page.locator("[data-action]").count() == 0  # nothing to resume or cancel
    assert errors == []
    assert seen.posts == []
