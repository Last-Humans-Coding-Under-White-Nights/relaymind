"""PR162 boundary regressions: RM03/RM12/RM16/RM17/RM25/RM28, AC22."""

import asyncio
import copy
import dataclasses
import importlib.util
import io
import json
import socket
from pathlib import Path

import httpx
import pytest

from gateway_helpers import build_gateway
from helpers import FakeClock
from relaymind.adapters import Message, Role
from relaymind.evaluation.grading import selector_provenance
from relaymind.orchestrator.local_loop import _Run
from relaymind.orchestrator.run_state import RunState
from relaymind.selection import (
    SelectionContext,
    SelectionSettings,
    SelectorChain,
    parse_selection_request,
)
from relaymind.selection.http import HttpSelector
from relaymind.store import StaleVersionError
from relaymind.store.trace_event import TraceEventError, _check_selector
from test_selection_interface import Scripted, request_data, result


def example_module():
    spec = importlib.util.spec_from_file_location(
        "example_selector", Path("scripts/example_selector.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_bind_reuses_address_without_reverse_dns(monkeypatch):
    """RM28/AC22: an immediately restarted example retains TCPServer reuse policy."""
    module = example_module()

    def no_dns(*args):
        pytest.fail("literal loopback binding must not resolve a hostname")

    monkeypatch.setattr(socket, "getfqdn", no_dns)
    with module.LoopbackServer(("127.0.0.1", 0), module.Handler) as server:
        assert server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
        assert server.server_name == "localhost"
        assert server.server_port == server.server_address[1] > 0


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"garbage\r\n",
        b"GET / HTTP/bad\r\n\r\n",
        b"G" * 65537 + b"\r\n",
        b"HEAD / HTTP/1.0\r\n\r\n",
    ],
    ids=["empty", "malformed-line", "invalid-version", "overlong-line", "head"],
)
def test_example_real_handler_error_lifecycle(raw):
    """RM28/AC22: real stdlib error paths initialize command before JSON replies."""

    class Connection:
        def __init__(self):
            self.sent = bytearray()

        def makefile(self, *args):
            return io.BytesIO(raw)

        def sendall(self, data):
            self.sent.extend(data)

        def settimeout(self, seconds):
            pass

    connection = Connection()
    example_module().Handler(connection, ("127.0.0.1", 1), object())
    response = bytes(connection.sent)
    if not raw:
        assert response == b""
    elif raw.startswith(b"HEAD"):
        assert response.startswith(b"HTTP/1.0 405")
        assert response.endswith(b"\r\n\r\n")
    else:
        body = response.split(b"\r\n\r\n")[-1]
        assert json.loads(body) == {"error": "unsupported request"}
        assert len(body) < 100


def summary(claims=None):
    return dict(
        name=None,
        version=None,
        mechanism=None,
        elapsed_ms=0,
        attempts=[
            dict(
                name="remote",
                version="1",
                transport="http",
                outcome="none",
                elapsed_ms=0,
                model_calls_claim=claims,
            )
        ],
    )


@pytest.mark.parametrize("payload", [{}, {"selector": None}])
def test_grading_absent_or_null_selector_is_unknown(payload):
    """RM16/RM25: absent evidence is unknown; null never validates as a stored summary."""
    assert selector_provenance([{"payload": payload}]) == dict(
        scope="unknown", attempts=[], selector_internal_model_cost="not_verified"
    )
    with pytest.raises(TraceEventError):
        _check_selector(None)


def test_grading_null_does_not_hide_later_http_evidence():
    """RM16/RM25/RM28: defensive null tolerance retains genuine later attempts."""
    report = selector_provenance(
        [{"payload": {"selector": None}}, {"payload": {"selector": summary([])}}]
    )
    assert report["scope"] == "http_attempted"
    assert report["attempts"] == summary([])["attempts"]


@pytest.mark.parametrize("last_kind", ["route.selected", "run.failed", "handoff.started"])
@pytest.mark.parametrize("last_transport", ["http", "in_process"])
def test_grading_preserves_all_routing_phases(last_kind, last_transport):
    """RM16/RM25/RM28: final-only or value dedup would erase real rerouting work."""
    first = summary([])
    second = copy.deepcopy(first)
    second["attempts"][0]["transport"] = last_transport
    if last_transport == "in_process":
        second["attempts"][0]["model_calls_claim"] = None
    events = [
        {"event_type": "route.selected", "payload": {"selector": first}},
        {"event_type": "clarification.required", "payload": {}},
        {"event_type": "clarification.answered", "payload": {}},
        {"event_type": last_kind, "payload": {"selector": second}},
    ]
    report = selector_provenance(events)
    assert report["scope"] == "http_attempted"
    assert report["attempts"] == first["attempts"] + second["attempts"]
    assert len(report["attempts"]) == 2


def opened_run(tmp_path):
    gateway = build_gateway(tmp_path, [])
    run = _Run(gateway.orchestrator, (Message(role=Role.USER, content="hello"),), None, None)
    run.open()
    return run


@pytest.mark.parametrize("boundary", ["emit_failure", "emit_handoff", "move", "cancel"])
@pytest.mark.parametrize("explicit", [False, True])
def test_selector_persistence_redacts_later_known_secret(tmp_path, boundary, explicit):
    """RM12/RM22/RM28: every persistence path scrubs explicit and fallback claims."""
    run = opened_run(tmp_path)
    record = summary([{"model": "later-secret", "call_id": "later-secret"}])
    record["attempts"] += [
        dict(record["attempts"][0], model_calls_claim=None),
        dict(record["attempts"][0], model_calls_claim=[]),
    ]
    run.pending_selector = record
    run.secrets.add("later-secret")
    payload = {"error_code": "internal_error", **run._summary()}
    if explicit:
        payload["selector"] = copy.deepcopy(record)
    if boundary == "cancel":
        # _abort is the real direct machine.cancel persistence path; its summary
        # normally has no selector, but explicit payloads must receive the same scrub.
        if explicit:
            original = run._summary
            run._summary = lambda: {**original(), "selector": copy.deepcopy(record)}
        run._abort(asyncio.CancelledError())
    elif boundary == "move":
        run.move(
            RunState.FAILED, error_code="internal_error", event=("run.failed", "gateway", payload)
        )
    elif boundary == "emit_handoff":
        payload = {
            **({"selector": payload["selector"]} if explicit else {}),
            "reason_code": "local_budget",
            "model": "external-model",
            "skill": None,
            "operation_ids": [],
            "allowed_tools": [],
            "request": {"size": 1, "limit": 100, "method": "tokenizer"},
        }
        run.emit("handoff.started", "gateway", payload)
    else:
        run.emit("run.failed", "gateway", payload)
    stored = list(run.o.store.events(run.run_id))[-1].payload["selector"]
    assert "later-secret" not in json.dumps(stored)
    assert stored["attempts"][0]["model_calls_claim"] == [
        {"model": "[redacted]", "call_id": "[redacted]"}
    ]
    assert [a["model_calls_claim"] for a in stored["attempts"][1:]] == [None, []]
    assert run.pending_selector is None


@pytest.mark.parametrize("boundary", ["emit", "move", "stale_move", "cancel"])
def test_failed_persistence_retains_pending_selector(tmp_path, boundary, monkeypatch):
    """RM17/RM28: validation/stale failures must not consume unpersisted evidence."""
    run = opened_run(tmp_path)
    run.pending_selector = summary([])
    snapshot = copy.deepcopy(run.pending_selector)
    error = TraceEventError
    if boundary == "stale_move":
        run.record = dataclasses.replace(run.record, version=run.record.version - 1)
        error = StaleVersionError
    if boundary == "cancel":
        # Model an independent version writer between _abort's get and cancel.
        original = run.o.machine.cancel

        def stale_cancel(run_id, version, **kwargs):
            return original(run_id, version - 1, **kwargs)

        monkeypatch.setattr(run.o.machine, "cancel", stale_cancel)
        run._abort(asyncio.CancelledError())
    else:
        with pytest.raises(error):
            if boundary == "emit":
                run.emit("run.failed", "gateway", {})
            else:
                run.move(
                    RunState.FAILED,
                    error_code="internal_error",
                    event=("run.failed", "gateway", {"error_code": "internal_error"}),
                )
    assert run.pending_selector == snapshot
    assert len(list(run.o.store.events(run.run_id))) == 1


@pytest.mark.parametrize("remaining", [None, 1.0])
def test_short_request_caps_attempt_timer_and_is_not_extended(remaining):
    """RM03/RM28: request allowance bounds timer even with a longer gateway allowance."""
    selector = Scripted("remote", hang=True)
    sleeps = []

    async def expire(seconds):
        sleeps.append(seconds)

    kwargs = {} if remaining is None else {"remaining_seconds": lambda: remaining}
    answer = asyncio.run(
        SelectorChain([selector], timeout_seconds=2, sleep=expire).run(
            parse_selection_request(request_data(deadline_ms=100)),
            SelectionContext(),
            lambda r: r,
            **kwargs,
        )
    )
    assert sleeps == [0.1]
    assert selector.requests[0].deadline_ms == 100
    assert selector.cancelled
    assert answer.attempts[0].outcome == "timeout"


def test_short_request_rejects_synchronous_late_admission():
    """RM03/RM28: admission elapsed uses the effective request allowance, not config."""
    clock = FakeClock()

    def admit(result):
        clock.advance(0.11)
        return result

    chain = SelectorChain(
        [Scripted("remote", result("remote"))], timeout_seconds=2, clock=clock.monotonic
    )
    answer = asyncio.run(
        chain.run(parse_selection_request(request_data(deadline_ms=100)), SelectionContext(), admit)
    )
    assert answer.admitted is None
    assert answer.attempts[0].outcome == "timeout"


def test_short_request_drains_cancellation_resistant_late_work():
    """RM14/RM28: a cancelled task's late result never wins the effective deadline."""
    clock = FakeClock()
    finished = []

    class Resistant(Scripted):
        async def select(self, request, context):
            try:
                clock.advance(0.11)
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                finished.append(True)
                return self.answer

    async def expire(seconds):
        pass

    answer = asyncio.run(
        SelectorChain(
            [Resistant("remote", result("remote"))],
            timeout_seconds=2,
            clock=clock.monotonic,
            sleep=expire,
        ).run(
            parse_selection_request(request_data(deadline_ms=100)), SelectionContext(), lambda r: r
        )
    )
    assert finished == [True]
    assert answer.admitted is None
    assert answer.attempts[0].outcome == "timeout"


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_literal_loopback_config_does_no_dns(monkeypatch, scheme, host):
    """RM01/RM12/RM28: IPv6 loopback startup needs neither DNS nor a working listener."""

    def forbidden(*args, **kwargs):
        pytest.fail("configuration must not resolve or connect")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    settings = SelectionSettings(
        chain=(
            {
                "transport": "http",
                "name": "remote",
                "version": "1",
                "url": f"{scheme}://{host}:8051/select",
            },
        )
    )
    assert settings.to_json_object()["selection"] == ["remote"]


@pytest.mark.parametrize(
    "url",
    [
        "http://[2001:db8::1]/select",
        "http://[::]/select",
        "http://secret@[::1]/select",
        "http://[::1]/select?secret",
        "http://[::1]/select#secret",
    ],
)
def test_ipv6_rejected_endpoint_policy(url):
    """RM12/RM28: IPv6 literals retain loopback/credential/query/fragment policy."""
    with pytest.raises(ValueError, match="loopback"):
        HttpSelector(name="remote", version="1", url=url)


def test_ipv6_production_client_sends_one_post(monkeypatch):
    """RM28/AC22: bracketed IPv6 routes through the production client without OS IPv6."""
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(200, json=result("remote"))

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(response), **kwargs),
    )
    selector = HttpSelector(name="remote", version="1", url="http://[::1]:8051/select")

    async def pending_timer(seconds):
        await asyncio.Event().wait()

    # This checks IPv6 transport semantics; real deadlines have separate coverage.
    answer = asyncio.run(
        SelectorChain([selector], timeout_seconds=2, clock=lambda: 0.0, sleep=pending_timer).run(
            parse_selection_request(request_data()), SelectionContext(), lambda r: r
        )
    )
    assert answer.admitted is not None
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.host == "::1"


@pytest.mark.parametrize("model_based", [False, True])
def test_exhausted_gateway_allowance_never_dispatches(model_based):
    """RM03/RM28: no fresh POST or model callback starts on an exhausted allowance."""
    selector = Scripted("remote", result("remote"))
    selector.model_based = model_based
    with pytest.raises(TimeoutError):
        asyncio.run(
            SelectorChain([selector], timeout_seconds=2).run(
                parse_selection_request(request_data()),
                SelectionContext(),
                lambda r: r,
                remaining_seconds=lambda: 0,
            )
        )
    assert selector.requests == []


@pytest.mark.parametrize(
    "run_limit,stage_limit,budget",
    [
        (0.03049, 1, "run_active_seconds"),
        (1, 0.03049, "active_seconds"),
        (0.03049, 0.03049, "active_seconds"),
    ],
)
def test_gateway_budget_rounding_retains_owner_and_stops_fallback(
    tmp_path, monkeypatch, run_limit, stage_limit, budget
):
    """RM03/RM14/RM28: actual gateway allowance, not rounded wire ms, owns expiry."""
    from gateway_helpers import run
    from relaymind.orchestrator.budgets import LocalBudgets
    from test_http_selector import injected_http

    requests, sleeps, closed = [], [], []

    async def pending(request):
        requests.append(json.loads(request.content))
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    gateway = build_gateway(
        tmp_path, [], budgets=LocalBudgets(active_seconds=stage_limit, run_active_seconds=run_limit)
    )
    fallback = Scripted("fallback", result("fallback"))

    async def expire(seconds):
        sleeps.append(seconds)
        gateway.clock.advance(seconds)

    gateway.orchestrator.selection_chain = SelectorChain(
        [injected_http(monkeypatch, pending), fallback],
        timeout_seconds=2,
        clock=gateway.clock.monotonic,
        sleep=expire,
    )
    _, events = run(gateway)
    failed = events[-1].payload
    assert failed["budget"] == budget
    assert failed["error_code"] == "needs_external"
    assert failed["reason_code"] == "local_budget"
    assert [a["outcome"] for a in failed["selector"]["attempts"]] == ["error"]
    assert sleeps == [pytest.approx(0.03049)]
    assert len(requests) == 1 and requests[0]["deadline_ms"] == 30
    assert closed == [True]
    assert fallback.requests == []
    assert gateway.adapter.requests == []


def test_real_clarification_phases_preserve_equal_attempts(tmp_path):
    """RM16/RM25/RM28: separate successful routing phases may contain equal summaries."""
    selector = Scripted("remote", result("remote", kind="clarify", clarification="Which service?"))
    selector.transport = "http"
    gateway = build_gateway(tmp_path, [], selectors=[selector])
    run = _Run(gateway.orchestrator, (Message(role=Role.USER, content="hello"),), None, None)
    run.open()

    async def twice():
        await run.drive()
        run.reopen(run.record.version)
        await run.continue_with("Please clarify again.")

    asyncio.run(twice())
    events = [event.to_json_object() for event in gateway.store.events(run.run_id)]
    routes = [event for event in events if event["event_type"] == "route.selected"]
    assert len(routes) == 2
    first, second = [route["payload"]["selector"]["attempts"] for route in routes]
    assert first == second
    assert selector_provenance(events)["attempts"] == first + second
    assert run.pending_selector is None
    assert gateway.adapter.requests == []


@pytest.mark.parametrize("second_phase", [False, True])
def test_route_evidence_is_consumed_before_terminal_fallback(tmp_path, second_phase):
    """RM17/RM28: terminal fallback contains only a new interrupted routing phase."""
    from gateway_helpers import run as drive
    from relaymind.orchestrator.budgets import BudgetExceededError

    if not second_phase:
        selector = Scripted("remote", result("remote"))
        selector.transport = "http"

        def stop(_):
            raise BudgetExceededError("generations", 3)

        gateway = build_gateway(tmp_path, [], selectors=[selector], on_generate=stop)
        _, raw = drive(gateway)
    else:
        selector = Scripted(
            "remote", result("remote", kind="clarify", clarification="Which service?")
        )
        selector.transport = "http"
        gateway = build_gateway(tmp_path, [], selectors=[selector])
        run = _Run(gateway.orchestrator, (Message(role=Role.USER, content="hello"),), None, None)
        run.open()

        async def interrupt_second():
            await run.drive()
            selector.hang = True

            async def expire(seconds):
                gateway.clock.advance(seconds)

            gateway.orchestrator.selection_chain = SelectorChain(
                [selector], timeout_seconds=1000, clock=gateway.clock.monotonic, sleep=expire
            )
            run.reopen(run.record.version)
            await run.continue_with("staging-api")

        asyncio.run(interrupt_second())
        raw = list(gateway.store.events(run.run_id))
    events = [event.to_json_object() for event in raw]
    assert events[-1]["event_type"] == "run.failed"
    terminal = events[-1]["payload"]
    report = selector_provenance(events)
    if second_phase:
        assert [a["outcome"] for a in report["attempts"]] == ["selected", "error"]
        assert [a["outcome"] for a in terminal["selector"]["attempts"]] == ["error"]
    else:
        assert "selector" not in terminal
        assert [a["outcome"] for a in report["attempts"]] == ["selected"]
    assert report["scope"] == "http_attempted"


def test_historical_selector_evidence_remains_unknown():
    """RM16/RM25: historical attempts lacking transport must not imply observed cost."""
    record = summary()
    record["attempts"] = [{"name": "historical", "outcome": "selected"}]
    report = selector_provenance([{"payload": {"selector": record}}])
    assert report["scope"] == "unknown"
    assert report["attempts"] == record["attempts"]
    assert report["selector_internal_model_cost"] == "not_verified"


@pytest.mark.parametrize("stage_limited", [False, True])
@pytest.mark.parametrize("resists_cancel", [False, True])
def test_real_gateway_short_budget_never_starts_fallback(
    tmp_path, monkeypatch, stage_limited, resists_cancel
):
    """RM03/RM14/RM28: a real awaited POST keeps its gateway stop and drains late work."""
    from gateway_helpers import run
    from relaymind.orchestrator.budgets import LocalBudgets
    from test_http_selector import injected_http

    requests, closed = [], []

    async def pending(request):
        requests.append(json.loads(request.content))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if resists_cancel:
                return httpx.Response(200, json=result("remote"))
            raise
        finally:
            closed.append(True)

    fallback = Scripted("fallback", result("fallback"))
    gateway = build_gateway(
        tmp_path,
        [],
        selectors=[injected_http(monkeypatch, pending), fallback],
        budgets=LocalBudgets(
            active_seconds=0.03 if stage_limited else 1,
            run_active_seconds=1 if stage_limited else 0.03,
        ),
    )
    _, events = run(gateway)
    failed = events[-1].payload
    assert failed["error_code"] == "needs_external"
    assert failed["reason_code"] == "local_budget"
    assert failed["budget"] == ("active_seconds" if stage_limited else "run_active_seconds")
    assert [a["outcome"] for a in failed["selector"]["attempts"]] == ["error"]
    assert len(requests) == 1 and requests[0]["deadline_ms"] <= 30
    assert closed == [True]
    assert fallback.requests == []
    assert gateway.adapter.requests == []


def test_wire_rounding_does_not_shorten_actual_allowance():
    """RM03/RM28: millisecond wire rounding cannot reject timely admission."""
    clock = FakeClock()
    selector = Scripted("remote", result("remote"))

    def admit(result):
        clock.advance(0.1003)
        return result

    chosen = asyncio.run(
        SelectorChain([selector], timeout_seconds=2, clock=clock.monotonic).run(
            parse_selection_request(request_data()),
            SelectionContext(),
            admit,
            remaining_seconds=lambda: 0.10049,
        )
    )
    assert selector.requests[0].deadline_ms == 100
    assert chosen.admitted is not None
    assert chosen.attempts[0].outcome == "selected"
