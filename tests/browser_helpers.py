"""A real browser over the real gateway app, for the page script tests (RM23, #89).

The gateway app runs on a loopback socket in a thread, over model
doubles and a disposable sandbox (spec section 13). The browser is
Playwright's Chromium; install it with ``playwright install chromium``.
A machine without the browser fails the test, it does not skip it.
"""

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from api_helpers import RUN_ID_HEADER, RunningServer, chat_body, token_of
from relaymind.adapters.local_http import is_loopback_url
from relaymind.api.native import NativeSettings
from ui_helpers import ui_app

WAIT_MS = 30_000  # Windows runners are slow
NATIVE_VIEW = re.compile(r"^/relay/v1/runs/[^/]+$")


@contextmanager
def served(gateway, *, mode="double", **ui):
    """The app on a loopback port: yields (origin, application_token)."""
    app = ui_app(gateway, mode, native=NativeSettings(cancel_wait_seconds=0.5), **ui)
    with RunningServer(app) as server:
        yield server.origin, token_of(app)


@dataclass
class Seen:
    """What the page asked the gateway for while a test ran."""

    posts: list[dict] = field(default_factory=list)  # every POST to the API: path and body
    views: list[dict] = field(default_factory=list)  # decoded native run views

    def issued_tokens(self):
        """The approval tokens the native run view returned to the page (it returns each once)."""
        tokens = []
        for view in self.views:
            pending = view.get("pending_approval") or {}
            if pending.get("token"):
                tokens.append(pending["token"])
        return tokens


@contextmanager
def browser_page(browser):
    """A fresh page in its own context of a shared browser; yields (page, seen)."""
    seen = Seen()
    context = browser.new_context()
    try:
        page = context.new_page()
        page.set_default_timeout(WAIT_MS)

        def on_request(request):
            path = urlsplit(request.url).path
            if request.method == "POST" and path.startswith(("/relay/", "/ui/comparisons")):
                seen.posts.append({"path": path, "body": request.post_data_json})

        def on_response(response):
            request = response.request
            if request.method == "GET" and NATIVE_VIEW.match(urlsplit(request.url).path):
                seen.views.append(response.json())

        page.on("request", on_request)
        page.on("response", on_response)
        yield page, seen
    finally:
        context.close()


def open_ui(page, base, token):
    page.goto(base + "/ui")
    page.fill("#token", token)


def open_run(page, run_id):
    """Open a stored run by its ID from the page form."""
    page.fill("#open-id", run_id)
    page.click("#open-form button[type=submit]")


def chat_run_id(base, token, prompt, **fields):
    """Make one chat completion over HTTP; return the run it owned."""
    with httpx.Client(
        base_url=base,
        headers={"Authorization": f"Bearer {token}"},
        trust_env=not is_loopback_url(base),
    ) as client:
        response = client.post(
            "/v1/chat/completions", json=chat_body(prompt=prompt, **fields), timeout=30
        )
    assert response.status_code == 200, response.text
    return response.headers[RUN_ID_HEADER]
