"""Native panel DOM mechanics, not native person qualification (RM37/RM39; AC28/AC30)."""

import functools
import http.server
import json
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from test_openclaw_host_transport import PACKAGE, require_node_build_dependencies, run_tsc


@pytest.fixture(scope="module")
def panel_build(tmp_path_factory):
    """Compile the shipped browser entry/codec into an owned, disposable directory."""
    require_node_build_dependencies()
    target = tmp_path_factory.mktemp("native-panel")
    run_tsc("-p", str(PACKAGE / "tsconfig.control-ui.json"), "--outDir", str(target))
    return target


@contextmanager
def served_panel(root: Path):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    class Server(http.server.ThreadingHTTPServer):
        # The browser fetches the whole ES module graph at once; the default
        # listen backlog of 5 overflows on macOS and resets a module request.
        request_queue_size = 128

    server = Server(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("locale", ["en", "zh-CN"])
@pytest.mark.parametrize(
    "malformed", ["missing-evidence", "empty-candidate", "missing-version", "mismatched-asset"]
)
def test_native_panel_binds_explicit_choice_and_privacy_to_authoritative_receipts(
    panel_build, locale, malformed
):
    """RM37/RM39: native UI fixture sends exact lossless IDs/counters; source text is inert."""
    view = json.loads((PACKAGE / "test/fixtures/recommendation-view.v1.json").read_text("utf-8"))
    view["state_version"] = 9223372036854775806
    view["feedback_revision"] = 1234567890123456789
    entry = view["displayed_candidates"][0]
    entry["asset"].update(
        asset_id="skill:fixture",
        content_hash="a" * 64,
        source_locator="C:\\Users\\operator\\技能\\" + "long-package-" * 80 + "\\SKILL.md",
        description=(
            "<img src=x onerror=alert(1)> **source** [link](https://example.test) 中文\u202e"
        ),
    )
    entry["candidate"].update(asset_id="skill:fixture", content_hash="a" * 64, score=0.75)
    view["result"]["base_candidates"] = [entry["candidate"]]
    view["result"]["final_candidates"] = [entry["candidate"]]
    view["result"]["attempts"][0]["result"]["candidates"] = [entry["candidate"]]
    html = (
        (Path(__file__).parent / "fixtures/openclaw-native-panel/index.html")
        .read_text(encoding="utf-8")
        .replace("LOCALE", json.dumps(locale))
        .replace("VIEW_RECEIPT", json.dumps(json.dumps(view)))
    )
    (panel_build / "index.html").write_text(html, encoding="utf-8", newline="\n")
    labels = (
        ("Recommend", "Choose 1", "Useful", "Inspect feedback")
        if locale == "en"
        else ("推荐", "选择 1", "有帮助", "查看反馈")
    )
    with served_panel(panel_build) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--js-flags=--expose-gc"])
        try:
            page = browser.new_page()
            page.set_default_timeout(3000)
            page.goto(url)
            page.get_by_role("textbox").first.fill("你好，请推荐技能 / suggest a skill")
            page.get_by_role("button", name=labels[0], exact=True).click()
            expect(page.get_by_role("button", name=labels[1], exact=True)).to_be_visible()
            assert page.locator("img").count() == 0
            # RM12/RM39; AC30: text-only evidence is readable, localized and inert.
            detail = page.locator("pre").first
            expect(detail).to_contain_text("推荐:" if locale == "zh-CN" else "Recommendation:")
            expect(detail).to_contain_text("**source** [link](https://example.test) 中文\\u{202e}")
            expect(detail).to_contain_text("0.75; token_overlap_count")
            assert page.locator("pre a, pre img, pre script, pre strong").count() == 0
            assert (
                len(page.evaluate("calls.filter(x=>x.method==='relaymind.recommendations')")) == 1
            )
            # RM17/RM37: a receipt that cannot be displayed never replaces the
            # recommendation the later actions are bound to.
            original_detail = detail.inner_text()
            page.evaluate("kind => { window.malformedNext = kind; }", malformed)
            page.get_by_role("button", name=labels[0], exact=True).click()
            expect(page.get_by_role("status")).to_contain_text(
                "Request unavailable" if locale == "en" else "请求暂不可用"
            )
            expect(detail).to_contain_text("4774d146-17bd-4ec9-8507-f528ccea51fa")
            assert detail.inner_text() == original_detail
            # Feedback must precede choice: a choice receipt would overwrite a
            # corrupted feedback revision and hide the regression (RM17/RM37).
            page.get_by_role("button", name=labels[2], exact=True).click()
            page.get_by_role("button", name=labels[1], exact=True).click()
            page.get_by_role("button", name=labels[3], exact=True).click()
            calls = page.evaluate("calls.filter(x=>x.method==='relaymind.recommendations')")
            choose = next(x["params"] for x in calls if x["params"]["input"]["kind"] == "choose")
            assert choose["input"]["recommendationId"] == view["recommendation_id"]
            assert choose["expectedSessionId"] == "native-id"
            assert choose["expectedLifecycleRevision"] == "native-life"
            if locale == "en":
                assert choose["agentId"] == "main"
            else:
                assert "agentId" not in choose
            assert choose["input"]["request"]["expected_version"] == "9223372036854775806"
            assert choose["input"]["request"]["choice"] == {
                "kind": "asset",
                "asset_id": "skill:fixture",
                "content_hash": "a" * 64,
            }
            feedback = next(
                x["params"] for x in calls if x["params"]["input"]["kind"] == "feedback"
            )
            assert (
                feedback["input"]["request"]["expected_feedback_revision"] == "1234567890123456789"
            )
            assert feedback["input"]["recommendationId"] == "4774d146-17bd-4ec9-8507-f528ccea51fa"
            locator_label = "Source locator" if locale == "en" else "来源位置"
            assert (
                f"{locator_label}: {entry['asset']['source_locator']}"
                in detail.inner_text().split("\n")
            )
            remove_label = "Delete feedback" if locale == "en" else "删除反馈"
            expect(page.get_by_role("button", name=remove_label, exact=True)).to_be_visible()
            # Long unbroken locators and feedback JSON fit even a narrow panel.
            page.set_viewport_size({"width": 360, "height": 800})
            assert page.locator("pre").count() == 2
            assert page.locator("pre").evaluate_all(
                "nodes => nodes.every(node => node.scrollWidth <= node.clientWidth)"
            )
            assert "\u202e" not in page.locator("pre").last.inner_text()
            assert page.locator("img").count() == 0
            page.evaluate(
                "label => { window.retiredPrivateControl = new WeakRef("
                "[...document.querySelectorAll('button')].find(x=>x.textContent===label)); }",
                remove_label,
            )
            page.evaluate("hostFixture.connection.connected=false;listeners.forEach(fn=>fn())")
            assert page.get_by_role("button", name=labels[0], exact=True).is_disabled()
            # Collection may require a later browser task; each retry collects
            # before dereferencing so WeakRef's current-job hold cannot mask it.
            page.wait_for_function(
                "() => { gc(); return retiredPrivateControl.deref()===undefined; }",
                polling=50,
                timeout=3000,
            )
            assert page.evaluate("window.navigation?.page.id") == "recommendations"
        finally:
            browser.close()
