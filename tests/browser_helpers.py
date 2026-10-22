"""A real browser over the real gateway app, for the page script tests (RM23, #89).

The gateway app runs on a loopback socket in a thread, over model
doubles and a disposable sandbox (spec section 13). The browser is
Playwright's Chromium; install it with ``playwright install chromium``.
A machine without the browser fails the test, it does not skip it.
"""

from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx

from api_helpers import RUN_ID_HEADER, RunningServer, make_app, token_of
from relaymind.api.native import NativeSettings
from relaymind.ui import UiSettings

WAIT_MS = 30_000  # Windows runners are slow


@contextmanager
def served(gateway, *, mode="double", **ui):
    """The app on a loopback port: yields (base_url, application_token)."""
    app = make_app(
        gateway,
        ui=UiSettings(mode=mode, **ui),
        native=NativeSettings(cancel_wait_seconds=0.5),
    )
    with RunningServer(app) as server:
        host, port = server.address[:2]
        yield f"http://{host}:{port}", token_of(app)


@contextmanager
def browser_page():
    """A fresh page; yields (page, calls) where calls lists every API request it made."""
    from playwright.sync_api import sync_playwright

    calls: list[dict] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_default_timeout(WAIT_MS)

            def record(request):
                path = urlsplit(request.url).path
                if request.method == "POST" and path.startswith(("/relay/", "/ui/comparisons")):
                    calls.append({"path": path, "body": request.post_data_json})

            page.on("request", record)
            yield page, calls
        finally:
            browser.close()


def open_ui(page, base, token):
    page.goto(base + "/ui")
    page.fill("#token", token)


def open_run(page, run_id):
    """Open a stored run by its ID from the page form."""
    page.fill("#open-id", run_id)
    page.click("#open-form button[type=submit]")


def chat_run_id(base, token, prompt, **fields):
    """Make one chat completion over HTTP; return the run it owned."""
    with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "relaymind",
                "messages": [{"role": "user", "content": prompt}],
                **fields,
            },
            timeout=30,
        )
    assert response.status_code == 200, response.text
    return response.headers[RUN_ID_HEADER]
