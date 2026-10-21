"""The demo UI routes (RM23, RM25, spec 0.20 section 9).

``install_ui_routes`` adds five routes to the gateway app: the static
shell, script, and stylesheet (no token, no run data), the rendered
trace of one run, and the comparison. The trace and the comparison
need the application token and follow the error contract of section
11. The UI creates, resumes, and cancels runs only through the native
endpoints, from the page.
"""

from collections.abc import Awaitable, Callable
from importlib import resources

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from relaymind.api.auth import require_bearer_token
from relaymind.api.errors import ApiError
from relaymind.api.native import NativeSettings, parse_run_request, sweep_expired_waits
from relaymind.orchestrator.local_loop import LocalOrchestrator
from relaymind.orchestrator.route_decision import ExecutionMode
from relaymind.ui.comparison import COMPARED_MODES, run_comparison
from relaymind.ui.render import render_shell, render_trace
from relaymind.ui.settings import UiSettings
from relaymind.ui.trace_view import build_trace_view

__all__ = ["install_ui_routes"]

_Handler = Callable[[Request], Awaitable[Response]]

# Only this origin's own files may load or be called by the page.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_STATIC_HEADERS = {"Content-Security-Policy": CONTENT_SECURITY_POLICY, "Cache-Control": "no-cache"}


def _static(name: str) -> str:
    return (resources.files("relaymind.ui") / "static" / name).read_text("utf-8")


def install_ui_routes(
    app: FastAPI,
    *,
    orchestrator: LocalOrchestrator,
    token: str,
    settings: UiSettings,
    native: NativeSettings,
    guard: Callable[[_Handler], _Handler],
    decode: Callable[[bytes], object],
    require_mode: Callable[[ExecutionMode], None],
) -> None:
    """Add the UI routes. guard is the app's error contract wrapper (section 11)."""
    script, style = _static("app.js"), _static("app.css")

    @app.get("/ui")
    async def shell(request: Request) -> Response:
        return Response(render_shell(settings), media_type="text/html", headers=_STATIC_HEADERS)

    @app.get("/ui/app.js")
    async def app_js(request: Request) -> Response:
        return Response(script, media_type="text/javascript", headers=_STATIC_HEADERS)

    @app.get("/ui/app.css")
    async def app_css(request: Request) -> Response:
        return Response(style, media_type="text/css", headers=_STATIC_HEADERS)

    @app.get("/ui/runs/{run_id}")
    @guard
    async def trace(request: Request) -> Response:
        require_bearer_token(request.headers.get("authorization"), token)
        sweep_expired_waits(orchestrator)
        view = build_trace_view(orchestrator.store, request.path_params["run_id"])
        return Response(
            render_trace(view, settings),
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/ui/comparisons")
    @guard
    async def comparison(request: Request) -> Response:
        require_bearer_token(request.headers.get("authorization"), token)
        # Section 9: replay mode refuses every comparison, whatever its body.
        if not settings.interactive:
            raise ApiError("invalid_state", "the comparison is not available in replay mode")
        if settings.fixtures is None:
            raise ApiError("invalid_state", "no fixture service that can reset is configured")
        # The body of a native run creation (same rules, null is absent), without
        # execution_mode, because the comparison sets the mode of each run.
        body = parse_run_request(decode(await request.body()))
        if body.execution_mode is not None:
            raise ApiError(
                "unsupported_parameter", "execution_mode is not supported by this endpoint"
            )
        for mode in COMPARED_MODES:
            require_mode(mode)
        result = await run_comparison(
            orchestrator, settings, body.messages, cancel_wait_seconds=native.cancel_wait_seconds
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
