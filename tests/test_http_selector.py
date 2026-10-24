"""Loopback selector boundary and unverified evidence (RM12, RM28, AC22)."""

import asyncio
import json
import threading
from contextlib import contextmanager
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from relaymind.selection import (
    SelectionContext,
    SelectionSettings,
    SelectorChain,
    parse_selection_request,
)
from test_selection_interface import Scripted, request_data, result


def async_test(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapped


@contextmanager
def endpoint(body=None, status=200, *, delay=0):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                (
                    self.path,
                    dict(self.headers),
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            if delay:
                import time

                time.sleep(delay)
            self.send_response(status)
            self.send_header("Location", "/redirected")
            self.end_headers()
            try:
                answer = body(len(requests)) if callable(body) else body
                self.wfile.write(
                    answer if isinstance(answer, bytes) else json.dumps(answer).encode()
                )
            except BrokenPipeError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
    server.socket.bind(server.server_address)
    server.server_port = server.socket.getsockname()[1]
    server.server_activate()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/select", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def http(url):
    from relaymind.selection.http import HttpSelector

    return HttpSelector(name="remote", version="1", url=url)


def injected_http(monkeypatch, handler):
    """Exercise the production HTTP boundary without OS connection/setup timing."""
    client_type = httpx.AsyncClient

    def client(**kwargs):
        return client_type(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return http("http://127.0.0.1:8051/select")


async def attempt(selector, *, timeout=0.5, admit=lambda r: r):
    return await SelectorChain([selector], timeout_seconds=timeout).run(
        parse_selection_request(request_data()), SelectionContext(), admit
    )


def test_configured_http_identity_and_names():
    settings = SelectionSettings(
        chain=(
            {
                "transport": "http",
                "name": "remote",
                "version": "1",
                "url": "http://localhost:8051/select",
            },
            "example_match",
        )
    )
    assert settings.to_json_object()["selection"] == ["remote", "example_match"]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/select",
        "file://localhost/a",
        "http://127.0.0.1:99999/a",
        "http://u:p@localhost/a",
        "http://localhost/a?secret=x",
        "http://localhost/a#x",
        "http://localhost:/a",
        "//localhost/a",
    ],
)
def test_url_rejected_without_echoing_credentials(url):
    with pytest.raises(ValueError) as exc:
        http(url)
    assert url not in str(exc.value)
    assert "secret=x" not in str(exc.value)


@pytest.mark.parametrize(
    "change", [{"name": "example_match"}, {"name": " "}, {"version": ""}, {"extra": True}]
)
def test_config_closed_reserved_identity(change):
    config = dict(transport="http", name="remote", version="1", url="http://localhost/select")
    config.update(change)
    with pytest.raises(ValueError):
        SelectionSettings(chain=(config,))


@async_test
async def test_one_post_no_ambient_headers_or_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with endpoint(result("remote")) as (url, requests):
        chosen = await attempt(http(url))
    assert chosen.admitted is not None
    assert len(requests) == 1
    path, headers, sent = requests[0]
    assert path == "/select"
    assert not {"authorization", "cookie"} & {h.lower() for h in headers}
    assert sent["deadline_ms"] == 500
    assert chosen.attempts[0].to_json_object() == dict(
        name="remote",
        version="1",
        transport="http",
        outcome="selected",
        elapsed_ms=chosen.attempts[0].elapsed_ms,
        model_calls_claim=[],
    )


@pytest.mark.parametrize(
    "body,status,outcome,claims",
    [
        (b"bad", 200, "error", None),
        (result("remote"), 302, "error", None),
        (result("remote"), 500, "error", None),
        ({"bad": 1}, 200, "error", None),
        (result("other"), 200, "error", []),
        (result("remote", mechanism="routing_cache"), 200, "error", []),
        (result("remote", kind="none"), 200, "none", []),
    ],
)
@async_test
async def test_failure_and_none_claims(body, status, outcome, claims):
    with endpoint(body, status) as (url, requests):
        chosen = await attempt(http(url))
    assert len(requests) == 1
    entry = chosen.attempts[0].to_json_object()
    assert entry["outcome"] == outcome
    assert entry["model_calls_claim"] == claims
    assert url not in str(entry)


@async_test
async def test_connection_error_and_timeout_claims_unknown(monkeypatch):
    """RM28: refusal versus no response is independent of OS refusal latency."""
    requests = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("injected connection refusal", request=request)
        await asyncio.Event().wait()

    selector = injected_http(monkeypatch, handler)
    failed = await attempt(selector)
    assert failed.attempts[0].outcome == "error"
    assert failed.attempts[0].to_json_object()["model_calls_claim"] is None
    timed = await attempt(selector, timeout=0.02)
    assert timed.attempts[0].outcome == "timeout"
    assert timed.attempts[0].to_json_object()["model_calls_claim"] is None
    assert len(requests) == 2


@async_test
async def test_claims_immutable_and_inadmissible_falls_through():
    from relaymind.selection import InadmissibleError

    body = result("remote")
    body["model_calls"] = [{"model": "claimed", "call_id": "c"}]

    def reject(r):
        raise InadmissibleError("unknown skill")

    with endpoint(body) as (url, _):
        chosen = await attempt(http(url), admit=reject)
    entry = chosen.attempts[0].to_json_object()
    assert entry["outcome"] == "inadmissible"
    entry["model_calls_claim"][0]["model"] = "tampered"
    assert chosen.attempts[0].to_json_object()["model_calls_claim"] == body["model_calls"]


@async_test
async def test_cancel_keeps_attempt_and_never_falls_through():
    seen = []
    with endpoint(result("remote"), delay=0.2) as (url, requests):
        chain = SelectorChain([http(url), Scripted("next", result("next"))], timeout_seconds=1)
        task = asyncio.create_task(
            chain.run(
                parse_selection_request(request_data()),
                SelectionContext(),
                lambda r: r,
                on_progress=seen.append,
            )
        )
        for _ in range(100):
            if requests:
                break
            await asyncio.sleep(0.002)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(seen[-1].attempts) == 1
    assert seen[-1].attempts[0].outcome == "cancelled"


def test_http_gateway_fallback_claims_redacted_and_not_measured(tmp_path):
    """AC22/RM12: unknown skill cannot execute; self-reports never charge calls."""
    from gateway_helpers import build_gateway, executed_tools, health_tail, run
    from test_selection_interface import pick

    body = result("remote", skill_id="unknown")
    body["model_calls"] = [{"model": "secret-value"}]
    with endpoint(body) as (url, requests):
        gateway = build_gateway(
            tmp_path, health_tail(), external_turns=[], selectors=[http(url), pick("fallback")]
        )
        outcome, events = run(gateway, "Is staging-api healthy right now?")
    route = next(e for e in events if e.event_type.value == "route.selected")
    assert route.actor == "gateway"
    attempts = route.payload["selector"]["attempts"]
    assert [a["outcome"] for a in attempts] == ["inadmissible", "selected"]
    assert attempts[0]["transport"] == "http"
    assert len(executed_tools(events)) == 1
    terminal = events[-1].payload
    assert all(c["model"] != "secret-value" for c in terminal["model_calls"])


def test_gateway_stop_preserves_completed_http_attempts(tmp_path):
    """RM28: model-router stop keeps prior HTTP provenance without a fake route."""
    from gateway_helpers import build_gateway, run
    from relaymind.selection import ModelRouterSelector

    with endpoint(result("remote", kind="none")) as (url, _):
        gateway = build_gateway(tmp_path, [], selectors=[http(url)])

        def stop(_):
            from relaymind.orchestrator.budgets import BudgetExceededError

            raise BudgetExceededError("run_generations", 10)

        gateway.adapter.on_generate = stop
        gateway.orchestrator.selection_chain = SelectorChain(
            [http(url), ModelRouterSelector(gateway.orchestrator.router)], timeout_seconds=0.5
        )
        outcome, events = run(gateway, "Is staging-api healthy right now?")
    summaries = [e.payload["selector"] for e in events if "selector" in e.payload]
    assert summaries
    assert [a["outcome"] for a in summaries[-1]["attempts"]] == ["none", "error"]
    assert not any(e.event_type.value == "route.selected" for e in events)


def test_grading_reports_http_provenance_on_fallback():
    """RM16/RM25: read fallback summaries; [] never verifies external inference zero."""
    from relaymind.evaluation.grading import selector_provenance

    attempt = dict(
        name="remote",
        version="1",
        transport="http",
        elapsed_ms=1,
        outcome="none",
        model_calls_claim=[],
    )
    events = [dict(event_type="run.failed", payload={"selector": {"attempts": [attempt]}})]
    report = selector_provenance(events)
    assert report["scope"] == "http_attempted"
    assert report["attempts"] == [attempt]
    assert report["selector_internal_model_cost"] == "not_verified"
    assert selector_provenance([])["scope"] == "unknown"


def test_benchmark_uses_configured_identity_and_keeps_failed_claims():
    """RM25/RM28: production boundary is reused; an invalid next case has unknown claims."""
    # The real frozen file keeps scoring definitions unchanged.
    from pathlib import Path

    from relaymind.evaluation.selection_benchmark import load_locked, run_skills

    data = load_locked(Path("evaluation/skills.v1.json"), Path("evaluation/skills.v1.lock.json"))
    body = result("wrong", kind="none")
    body["model_calls"] = [{"model": "unverified"}]
    with endpoint(body) as (url, _):
        report = asyncio.run(
            run_skills(data, http(url), seed=42, case_ids={data["cases"][0]["id"]})
        )
    row = report["cases"][0]
    assert row["status"] == "invalid_output"
    assert row["selector_attempt"]["name"] == "remote"
    assert row["selector_attempt"]["model_calls_claim"] == body["model_calls"]
    assert row["selector_attempt"]["transport"] == "http"


def test_example_script_serves_via_production_transport():
    """AC22: exercise actual stdlib example and catalog-only choices, with no model."""
    import subprocess
    import sys

    process = subprocess.Popen(
        [sys.executable, "scripts/example_selector.py", "--port", "0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        url = process.stdout.readline().strip()
        from relaymind.selection.http import HttpSelector

        selector = HttpSelector(name="example_keyword", version="1", url=url)
        chosen = asyncio.run(
            SelectorChain([selector], timeout_seconds=0.5).run(
                parse_selection_request(
                    request_data(conversation=[{"role": "user", "content": "Check service health"}])
                ),
                SelectionContext(),
                lambda r: r,
            )
        )
        assert chosen.admitted is not None
        assert chosen.attempts[0].to_json_object()["model_calls_claim"] == []
        import httpx

        with httpx.Client(trust_env=False) as client:
            for method in ("GET", "TRACE", "PATCH"):
                response = client.request(method, url)
                assert response.status_code in (405, 501)
                assert len(response.content) < 128
                assert "error" in response.json()
            assert client.post(url, json=[]).status_code == 400
            assert client.post(url.replace("/select", "/private"), json={}).status_code == 404
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_attempt_trace_schemas_keep_historical_and_reject_partial_evidence():
    """RM22/RM28: Python and published schema enforce identical closed claims."""
    from importlib import resources

    from jsonschema import Draft202012Validator

    from relaymind.store.trace_event import TraceEventError, _check_selector

    schema = json.loads(
        resources.files("relaymind.store").joinpath("schemas/trace_event.v1.json").read_text()
    )
    validator = Draft202012Validator({"$ref": "#/$defs/selector_summary", "$defs": schema["$defs"]})
    base = dict(name=None, version=None, mechanism=None, elapsed_ms=0, attempts=[])
    for a in [
        dict(name="old", outcome="none"),
        dict(
            name="remote",
            version="1",
            transport="http",
            outcome="cancelled",
            elapsed_ms=0,
            model_calls_claim=[],
        ),
    ]:
        record = {**base, "attempts": [a]}
        _check_selector(record)
        assert not list(validator.iter_errors(record))
    for change in [
        dict(transport="in_process", model_calls_claim=[]),
        dict(model_calls_claim=[{"model": "m", "secret": "s"}]),
        dict(version=""),
        dict(elapsed_ms=-1),
    ]:
        a = dict(
            name="remote",
            version="1",
            transport="http",
            outcome="error",
            elapsed_ms=1,
            model_calls_claim=None,
        )
        a.update(change)
        record = {**base, "attempts": [a]}
        with pytest.raises(TraceEventError):
            _check_selector(record)
        assert list(validator.iter_errors(record))


def test_http_request_and_claims_redacted_before_boundary(tmp_path):
    """RM12: known secrets are absent from outbound request and recorded claims."""
    from gateway_helpers import build_gateway, health_tail, run
    from relaymind.redaction import Redactor
    from test_selection_interface import pick

    body = result("remote", kind="none")
    body["model_calls"] = [{"model": "do-not-export"}]
    with endpoint(body) as (url, requests):
        gateway = build_gateway(
            tmp_path, health_tail(), external_turns=[], selectors=[http(url), pick("fallback")]
        )
        # Seed a conversation secret through configured field redaction.
        gateway.orchestrator.redactor = Redactor(secret_fields={"content", "model"})
        _, events = run(gateway)
    assert "staging-api" not in json.dumps(requests[0][2]["conversation"])
    recorded = next(e.payload["selector"] for e in events if "selector" in e.payload)
    assert "do-not-export" not in json.dumps(recorded)


def test_http_config_constructs_without_local_model(tmp_path):
    """RM26/AC22: startup chooses HTTP without any connection or model dispatch."""
    from gateway_helpers import build_gateway
    from relaymind.launcher import _selection

    config = dict(transport="http", name="remote", version="1", url="http://127.0.0.1:1/select")
    settings = _selection({"chain": [config, "example_match"]}, has_local=False)
    from relaymind.orchestrator.local_status import LocalStatus

    gateway = build_gateway(tmp_path, [], local_status=LocalStatus.absent(), selection=settings)
    assert gateway.orchestrator.selection_chain.identity == (
        ("remote", "1"),
        ("example_match", "1"),
    )


def test_http_schema_invalid_claims_are_unknown():
    """RM28: malformed self-report is not recorded as a valid claim."""
    body = result("remote", kind="none")
    body["model_calls"] = [{"model": "m", "api_key": "secret"}]
    with endpoint(body) as (url, _):
        chosen = asyncio.run(attempt(http(url)))
    assert chosen.attempts[0].to_json_object()["model_calls_claim"] is None


def test_tool_benchmark_uses_production_identity_boundary():
    """RM08/RM25/RM28: tool-narrowing HTTP cannot claim a different identity."""
    from relaymind.evaluation.selection_benchmark import run_tools

    case = {
        "id": "case",
        "request": "inspect",
        "instruction": "inspect",
        "expected": ["get"],
        "tools": [
            {
                "name": "get",
                "description": "get",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            }
        ],
    }
    body = result("other", skill_id="benchmark_case")
    with endpoint(body) as (url, _):
        report = asyncio.run(
            run_tools(
                [case],
                http(url),
                adapter=None,
                timeout=0.5,
                context_limit=4096,
                calls_per_generation="many",
                seed=42,
            )
        )
    row = report["narrowing"]["cases"][0]
    assert row["status"] == "invalid_output"
    assert row["selector_attempt"]["model_calls_claim"] == []


def test_cancelled_gateway_keeps_http_attempt(tmp_path):
    """RM14/RM28: actual cancellation persists provenance on run.cancelled."""
    from gateway_helpers import build_gateway

    async def execute(url, requests):
        gateway = build_gateway(tmp_path, [], selectors=[http(url)])
        task = asyncio.create_task(gateway.orchestrator.run_text("Check service health"))
        for _ in range(100):
            if requests:
                break
            await asyncio.sleep(0.002)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        events = gateway.store.events(requests[0][2]["run_id"])
        terminal = events[-1]
        assert terminal.event_type.value == "run.cancelled"
        assert terminal.payload["selector"]["attempts"][0]["outcome"] == "cancelled"
        assert not gateway.adapter.requests

    with endpoint(result("remote"), delay=0.15) as (url, requests):
        asyncio.run(execute(url, requests))


def test_short_run_budget_is_gateway_error_and_caps_post_deadline(tmp_path, monkeypatch):
    """RM03/RM28: exhausted gateway time is neither selector timeout nor not_placed."""
    from gateway_helpers import build_gateway, run
    from relaymind.orchestrator.budgets import LocalBudgets

    requests = []

    async def pending_response(request):
        # Capture the production POST before entering the timed response wait.
        requests.append(json.loads(request.content))
        await asyncio.Event().wait()

    gateway = build_gateway(
        tmp_path,
        [],
        selectors=[injected_http(monkeypatch, pending_response)],
        budgets=LocalBudgets(active_seconds=0.03, run_active_seconds=0.03),
    )
    _, events = run(gateway)
    terminal = events[-1]
    assert terminal.event_type.value == "run.failed"
    assert terminal.payload["selector"]["attempts"][0]["outcome"] == "error"
    assert len(requests) == 1
    assert requests[0]["deadline_ms"] <= 30
    assert not gateway.adapter.requests


@pytest.mark.parametrize(
    "choice,expected",
    [
        (dict(skill_id="unknown"), "inadmissible"),
        (dict(skill_id="check-service-health", tools=["delete_demo_database"]), "selected"),
    ],
)
def test_http_inadmissible_choices_never_execute(tmp_path, choice, expected):
    """AC22/RM08/RM28: neither unknown IDs nor foreign tools grant execution."""
    from gateway_helpers import build_gateway, executed_tools, run

    with endpoint(result("remote", **choice)) as (url, _):
        gateway = build_gateway(tmp_path, [], selectors=[http(url)])
        _, events = run(gateway)
    assert executed_tools(events) == []
    route = next(e for e in events if e.event_type.value == "route.selected")
    assert route.payload["selector"]["attempts"][0]["outcome"] == expected


def test_http_mutation_still_needs_confirmation(tmp_path):
    """AC22/RM11: HTTP selection cannot authorize a mutating proposal."""
    from gateway_helpers import build_gateway, executed_tools, run, tool_turn
    from relaymind.tools import CallerContext
    from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION

    with endpoint(result("remote", skill_id="delete-demo-database")) as (url, _):
        gateway = build_gateway(
            tmp_path,
            [
                tool_turn("list_demo_databases"),
                tool_turn(
                    "delete_demo_database", database_id="demo-orders-db", expected_revision=3
                ),
            ],
            selectors=[http(url)],
            caller=CallerContext(permissions=frozenset({MUTATE_PERMISSION, READ_PERMISSION})),
        )
        _, events = run(gateway, "Delete the demo-orders-db sandbox database.")
    assert any(e.event_type.value == "confirmation.required" for e in events)
    assert executed_tools(events) == ["list_demo_databases"]


def test_total_attempt_deadline_includes_admission():
    """RM28: a late admission cannot win after the per-attempt budget is spent."""
    from helpers import FakeClock

    clock = FakeClock()

    def slow_admit(result):
        clock.advance(1)
        return result

    chain = SelectorChain(
        [Scripted("remote", result("remote"))], timeout_seconds=0.5, clock=clock.monotonic
    )
    chosen = asyncio.run(
        chain.run(parse_selection_request(request_data()), SelectionContext(), slow_admit)
    )
    assert chosen.attempts[0].outcome == "timeout"
    assert chosen.admitted is None


def test_transport_clients_close_across_event_loops(monkeypatch):
    """RM28: no client, cookie jar or connection is shared across caller event loops."""
    import httpx

    real = httpx.AsyncClient
    clients = []

    def client(**kwargs):
        made = real(**kwargs)
        clients.append(made)
        return made

    monkeypatch.setattr(httpx, "AsyncClient", client)
    with endpoint(result("remote", kind="none")) as (url, _):
        selector = http(url)
        asyncio.run(attempt(selector))
        asyncio.run(attempt(selector))
    assert len(clients) == 2
    assert all(c.is_closed for c in clients)


def test_benchmark_none_is_valid_and_claims_do_not_leak_between_cases():
    """RM25/RM28: valid none differs from failure; a failed next case inherits no claims."""
    from pathlib import Path

    from relaymind.evaluation.selection_benchmark import load_locked, run_skills

    data = load_locked(Path("evaluation/skills.v1.json"), Path("evaluation/skills.v1.lock.json"))
    with endpoint(result("remote", kind="none")) as (url, _):
        report = asyncio.run(
            run_skills(data, http(url), seed=42, case_ids={data["cases"][0]["id"]})
        )
    row = report["cases"][0]
    assert row["status"] == "ok"
    assert row["none"] is True
    assert row["selector_attempt"]["outcome"] == "none"
    with endpoint(result("remote", skill_id="unknown")) as (url, _):
        report = asyncio.run(
            run_skills(data, http(url), seed=42, case_ids={data["cases"][0]["id"]})
        )
    assert report["cases"][0]["selector_attempt"]["outcome"] == "inadmissible"


def test_failed_benchmark_case_does_not_inherit_prior_claims():
    """RM16/RM25: per-attempt immutable evidence has no mutable last-response state."""
    from pathlib import Path

    from relaymind.evaluation.selection_benchmark import load_locked, run_skills

    data = load_locked(Path("evaluation/skills.v1.json"), Path("evaluation/skills.v1.lock.json"))
    first = result("remote", kind="none")
    first["model_calls"] = [{"model": "claimed-only"}]
    with endpoint(lambda n: first if n == 1 else b"not-json") as (url, _):
        report = asyncio.run(
            run_skills(data, http(url), seed=42, case_ids={c["id"] for c in data["cases"][:2]})
        )
    assert report["summary"]["count"] == 2
    assert report["cases"][0]["selector_attempt"]["model_calls_claim"] == first["model_calls"]
    assert report["cases"][1]["selector_attempt"]["model_calls_claim"] is None
    assert report["cases"][1]["status"] == "invalid_output"


@pytest.mark.parametrize(
    "policy,stage_seconds,run_seconds,reason,error,missed",
    [
        ("local_first", 0.15, 60, "local_budget", "time_budget", False),
        ("device_aware", 0.2, 60, "local_slow", "deadline_exceeded", True),
        ("device_aware", 0.2, 0.2, "local_budget", "time_budget", True),
    ],
)
@pytest.mark.parametrize("prior_http", [False, True])
def test_routing_generation_owns_real_awaiting_deadline(
    tmp_path, policy, stage_seconds, run_seconds, reason, error, missed, prior_http, monkeypatch
):
    """AC21/AC22, RM03/RM14/RM27: HTTP budget guards cannot preempt generation expiry."""
    from gateway_helpers import LOCAL_MODEL_ID, build_gateway
    from helpers import of_type
    from relaymind.adapters import Completed, GenerationFailed
    from relaymind.orchestrator.budgets import LocalBudgets
    from relaymind.orchestrator.placement import PlacementSettings
    from relaymind.selection import ModelRouterSelector
    from test_placement import seed

    async def execute():
        gateway = build_gateway(
            tmp_path,
            [],
            input_tokens=100,
            budgets=LocalBudgets(active_seconds=stage_seconds, run_active_seconds=run_seconds),
            placement=PlacementSettings(policy=policy),
            external_turns=[[GenerationFailed(code="unavailable", message="test")]],
        )
        # Transport setup is unrelated to the generation timer competition.
        # The preceding HTTP response is deterministic; the generation below
        # still awaits through the real stage/placement/run deadline.
        prefix = (
            [
                injected_http(
                    monkeypatch, lambda _: httpx.Response(200, json=result("remote", kind="none"))
                )
            ]
            if prior_http
            else []
        )
        selectors = prefix + [ModelRouterSelector(gateway.orchestrator.router)]
        gateway.orchestrator.selection_chain = SelectorChain(
            selectors, timeout_seconds=1, clock=gateway.clock.monotonic
        )
        seed(gateway.store, model=LOCAL_MODEL_ID)

        async def awaiting(request):
            gateway.adapter.requests.append(request)
            await asyncio.sleep(1)
            yield Completed(finish_reason="stop")

        gateway.adapter.generate = awaiting
        outcome = await gateway.orchestrator.run_text("Is staging-api healthy right now?")
        await asyncio.sleep(0)
        events = gateway.store.events(outcome.run_id)
        local = next(e.payload for e in of_type(events, "model.finished") if e.actor == "local")
        assert local["deadline_missed"] is missed
        assert local["error"]["code"] == error
        assert gateway.adapter.cancelled == [local["call_id"]]
        handoff = of_type(events, "handoff.started")[0].payload
        assert handoff["reason_code"] == reason
        assert not of_type(events, "route.selected")
        attempts = handoff["selector"]["attempts"]
        assert [a["outcome"] for a in attempts] == (["none", "error"] if prior_http else ["error"])
        if prior_http:
            assert attempts[0]["transport"] == "http"
            assert attempts[0]["model_calls_claim"] == []

    asyncio.run(execute())


def test_caught_generation_stop_cannot_leave_selector_waiting(tmp_path):
    """AC21/AC22, RM14/RM27: a caught gateway stop retains its owner and ends selection."""
    from gateway_helpers import LOCAL_MODEL_ID, build_gateway
    from helpers import of_type
    from relaymind.adapters import Completed, GenerationFailed
    from relaymind.orchestrator.budgets import LocalBudgets
    from relaymind.orchestrator.placement import PlacementSettings
    from relaymind.selection import ModelRouterSelector
    from test_placement import seed

    async def execute():
        gateway = build_gateway(
            tmp_path,
            [],
            input_tokens=100,
            budgets=LocalBudgets(active_seconds=0.2, run_active_seconds=60),
            placement=PlacementSettings(policy="device_aware"),
            external_turns=[[GenerationFailed(code="unavailable", message="test")]],
        )
        router = ModelRouterSelector(gateway.orchestrator.router)
        caught = asyncio.Event()

        class Catching:
            name, version, model_based = "catching", "1", True

            async def select(self, request, context):
                try:
                    return await router.select(request, context)
                except BaseException:
                    caught.set()
                    try:
                        # A poisoned callback cannot start another generation or
                        # disarm the interrupt before yielding to selector work.
                        await router.select(request, context)
                    except BaseException:
                        await asyncio.Event().wait()

        gateway.orchestrator.selection_chain = SelectorChain(
            [Catching()], timeout_seconds=1, clock=gateway.clock.monotonic
        )
        seed(gateway.store, model=LOCAL_MODEL_ID)

        async def awaiting(request):
            await asyncio.sleep(1)
            yield Completed(finish_reason="stop")

        gateway.adapter.generate = awaiting
        # The test's bound releases a broken swallowed-stop task rather than hanging pytest.
        outcome = await asyncio.wait_for(
            gateway.orchestrator.run_text("Is staging-api healthy right now?"), 0.8
        )
        assert caught.is_set()
        events = gateway.store.events(outcome.run_id)
        local = next(e.payload for e in of_type(events, "model.finished") if e.actor == "local")
        assert len([e for e in of_type(events, "model.finished") if e.actor == "local"]) == 1
        assert local["deadline_missed"] is True
        assert local["error"]["code"] == "deadline_exceeded"
        assert gateway.adapter.cancelled == [local["call_id"]]
        assert of_type(events, "handoff.started")[0].payload["reason_code"] == "local_slow"
        assert (
            of_type(events, "handoff.started")[0].payload["selector"]["attempts"][0]["outcome"]
            == "error"
        )
        assert not of_type(events, "route.selected")

    asyncio.run(execute())


@pytest.mark.parametrize("catch_stop", [False, True])
def test_real_user_cancel_beats_suspended_gateway_deadline(tmp_path, catch_stop):
    """RM14/RM27/AC21: real cancellation wins even after late provider cleanup or a caught stop."""
    from gateway_helpers import LOCAL_MODEL_ID, build_gateway, route_turn
    from helpers import of_type
    from relaymind.adapters import Message, Role
    from relaymind.orchestrator.budgets import LocalBudgets
    from relaymind.orchestrator.placement import PlacementSettings
    from relaymind.selection import ModelRouterSelector
    from test_placement import seed

    async def execute(url):
        gateway = build_gateway(
            tmp_path,
            [],
            input_tokens=100,
            external_turns=[],
            budgets=LocalBudgets(active_seconds=0.2, run_active_seconds=60),
            placement=PlacementSettings(policy="device_aware"),
        )
        router = ModelRouterSelector(gateway.orchestrator.router)

        class Catching:
            name, version, model_based = "catching", "1", True

            async def select(self, request, context):
                try:
                    return await router.select(request, context)
                except BaseException:
                    await asyncio.Event().wait()

        gateway.orchestrator.selection_chain = SelectorChain(
            [http(url), Catching() if catch_stop else router],
            timeout_seconds=1,
            clock=gateway.clock.monotonic,
        )
        seed(gateway.store, model=LOCAL_MODEL_ID)
        entered = asyncio.Event()

        async def resistant(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                gateway.clock.advance(0.3)
                for event in route_turn("check-service-health"):
                    yield event

        gateway.adapter.generate = resistant
        started = gateway.orchestrator.start(
            [Message(role=Role.USER, content="Is staging-api healthy right now?")]
        )
        await entered.wait()
        version = gateway.store.get_run(started.run_id).version
        await gateway.orchestrator.cancel(started.run_id, version)
        events = gateway.store.events(started.run_id)
        finished = next(e.payload for e in of_type(events, "model.finished") if e.actor == "local")
        assert finished["error"]["code"] == "cancelled"
        assert finished["deadline_missed"] is False
        assert gateway.adapter.cancelled == [finished["call_id"]]
        assert not of_type(events, "handoff.started")
        cancelled = of_type(events, "run.cancelled")[0].payload
        assert [a["outcome"] for a in cancelled["selector"]["attempts"]] == ["none", "cancelled"]
        assert cancelled["selector"]["attempts"][0]["model_calls_claim"] == []

    with endpoint(result("remote", kind="none")) as (url, _):
        asyncio.run(asyncio.wait_for(execute(url), 1))


def test_http_budget_guard_resumes_after_real_placement_decline(tmp_path, monkeypatch):
    """RM03/RM27/RM28: not_placed continues the chain with the gateway time bound intact."""
    from gateway_helpers import LOCAL_MODEL_ID, build_gateway, run
    from relaymind.adapters import GenerationFailed
    from relaymind.orchestrator.budgets import LocalBudgets
    from relaymind.orchestrator.placement import PlacementSettings
    from relaymind.selection import ModelRouterSelector
    from test_placement import seed

    requests = []

    async def pending_response(request):
        requests.append(json.loads(request.content))
        # Keep the response pending until the real gateway deadline cancels it.
        await asyncio.Event().wait()

    gateway = build_gateway(
        tmp_path,
        [],
        input_tokens=100,
        external_turns=[[GenerationFailed(code="unavailable", message="test")]],
        budgets=LocalBudgets(active_seconds=0.15, run_active_seconds=60),
        placement=PlacementSettings(policy="device_aware"),
    )
    gateway.orchestrator.selection_chain = SelectorChain(
        [
            ModelRouterSelector(gateway.orchestrator.router),
            injected_http(monkeypatch, pending_response),
        ],
        timeout_seconds=1,
        clock=gateway.clock.monotonic,
    )
    seed(gateway.store, model=LOCAL_MODEL_ID, duration_ms=10000)
    _, events = run(gateway)
    failed = next(e.payload for e in events if e.event_type.value == "handoff.started")
    assert failed["reason_code"] == "local_budget"
    assert [a["outcome"] for a in failed["selector"]["attempts"]] == ["not_placed", "error"]
    assert failed["selector"]["attempts"][1]["model_calls_claim"] is None
    assert len(requests) == 1
    assert requests[0]["deadline_ms"] <= 150
    assert not gateway.adapter.requests
