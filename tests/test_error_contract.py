"""Error contract: one status and one stable code per failure (section 11).

Covers RM17 (409 on a stale version), RM21 (the native resume errors),
RM19 (401 without the application token), and AC16, error part: an
explicit, documented error and no silent downgrade. Issue #21; the HTTP
transport arrives with the API work, so these tests check the transport
independent contract that it will serve: ``relaymind.api.errors`` and
``relaymind.api.auth``. Every status of section 11 has a deterministic
test, most of them against the real orchestrator and a model double.
"""

import asyncio

import pytest

from gateway_helpers import build_gateway, clarify_turn, health_tail, route_turn, tool_turn
from relaymind.api.auth import require_bearer_token
from relaymind.api.errors import STATUS_BY_CODE, ApiError, error_from_exception
from relaymind.orchestrator.local_loop import ApprovalRejectedError, ResumeInputError
from relaymind.orchestrator.run_state import RunState, SlotUnavailableError
from relaymind.store import RunNotFoundError, StaleVersionError
from relaymind.tools import CallerContext
from relaymind.tools.sandbox import MUTATE_PERMISSION, READ_PERMISSION

# The section 11 table, written out again so a change to either side shows.
DOCUMENTED = {
    400: [
        "invalid_request",
        "unsupported_parameter",
        "invalid_execution_mode",
        "invalid_resume_input",
        "context_limit_exceeded",
    ],
    401: ["unauthenticated"],
    403: ["execution_mode_forbidden", "approval_invalid"],
    404: ["run_not_found"],
    409: ["stale_version", "run_terminal", "invalid_state", "idempotency_conflict"],
    422: ["needs_external"],
    429: ["overloaded"],
    500: ["internal_error"],
    503: ["workflow_unavailable", "external_failed"],
    504: ["deadline_exceeded"],
}


def start(gateway, prompt="Is the service healthy?"):
    return asyncio.run(gateway.orchestrator.run_text(prompt))


def waiting_for_input(tmp_path):
    gateway = build_gateway(tmp_path, [clarify_turn()])
    waiting = start(gateway)
    assert waiting.state is RunState.WAITING_INPUT
    return gateway, waiting, gateway.store.get_run(waiting.run_id).version


# --- the table -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "status"), [(c, s) for s, codes in DOCUMENTED.items() for c in codes]
)
def test_each_documented_code_has_its_status(code, status):
    """Section 11: a stable code per status, and no code the table does not list."""
    error = ApiError(code, "a message")
    assert error.status == status
    assert error.code == code


def test_the_status_table_lists_exactly_the_documented_codes():
    assert STATUS_BY_CODE == {c: s for s, codes in DOCUMENTED.items() for c in codes}


def test_an_undocumented_code_is_refused():
    """A code is part of the contract; a typo must not become a new one."""
    with pytest.raises(ValueError):
        ApiError("made_up_code", "x")


def test_the_error_body_has_a_code_a_message_and_a_run_reference():
    error = ApiError("run_not_found", "no run run_1", run_id="run_1")
    assert error.body() == {
        "error": {"code": "run_not_found", "message": "no run run_1", "run_id": "run_1"}
    }


def test_the_error_body_names_no_run_when_none_exists_and_adds_a_reason_code():
    assert ApiError("unauthenticated", "x").body()["error"]["run_id"] is None
    body = ApiError("needs_external", "x", run_id="r", reason_code="local_budget").body()
    assert body["error"]["reason_code"] == "local_budget"
    assert "reason_code" not in ApiError("overloaded", "x").body()["error"]


# --- 401 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer wrong", "Basic secret-token", "secret-token", "Bearer  "],
)
def test_a_missing_or_invalid_token_is_401(header):
    """Section 7.1: Authorization is required; a missing or invalid token is 401."""
    with pytest.raises(ApiError) as caught:
        require_bearer_token(header, "secret-token")
    assert (caught.value.status, caught.value.code) == (401, "unauthenticated")
    assert "secret-token" not in caught.value.message


def test_the_application_token_passes():
    assert require_bearer_token("Bearer secret-token", "secret-token") is None


# --- 400, 409, 404, 429 from the orchestrator ------------------------------------------


def test_a_resume_that_does_not_match_the_waiting_state_is_400(tmp_path):
    """Sections 7.4 and 11: invalid_resume_input; the run keeps waiting."""
    gateway, waiting, version = waiting_for_input(tmp_path)
    with pytest.raises(ResumeInputError) as caught:
        asyncio.run(gateway.orchestrator.resume(waiting.run_id, version, "  "))
    error = error_from_exception(caught.value, run_id=waiting.run_id)
    assert (error.status, error.code, error.run_id) == (400, "invalid_resume_input", waiting.run_id)
    assert gateway.store.get_run(waiting.run_id).version == version


def test_a_stale_state_version_is_409(tmp_path):
    """RM17: stale versions are rejected; the run is unchanged."""
    gateway, waiting, version = waiting_for_input(tmp_path)
    with pytest.raises(StaleVersionError) as caught:
        asyncio.run(gateway.orchestrator.resume(waiting.run_id, version - 1, "staging-api"))
    error = error_from_exception(caught.value)
    assert (error.status, error.code, error.run_id) == (409, "stale_version", waiting.run_id)
    assert gateway.store.get_run(waiting.run_id).state == "waiting_input"


def test_a_resume_of_a_finished_run_is_409(tmp_path):
    """RM17: a terminal run never restarts."""
    gateway = build_gateway(tmp_path, [route_turn("check-service-health"), *health_tail()])
    done = start(gateway, "Is staging-api healthy right now?")
    assert done.state is RunState.COMPLETED
    version = gateway.store.get_run(done.run_id).version
    with pytest.raises(Exception) as caught:
        asyncio.run(gateway.orchestrator.resume(done.run_id, version, "payments-api"))
    error = error_from_exception(caught.value, run_id=done.run_id)
    assert (error.status, error.code) == (409, "run_terminal")


def test_a_resume_in_a_state_that_cannot_take_it_is_409(tmp_path):
    gateway = build_gateway(tmp_path, [])
    record = gateway.orchestrator.machine.create()
    with pytest.raises(Exception) as caught:
        asyncio.run(gateway.orchestrator.resume(record.run_id, record.version, "staging-api"))
    error = error_from_exception(caught.value, run_id=record.run_id)
    assert (error.status, error.code) == (409, "invalid_state")


def test_a_missing_run_is_404(tmp_path):
    gateway = build_gateway(tmp_path, [])
    with pytest.raises(RunNotFoundError) as caught:
        gateway.store.get_run("run_missing")
    error = error_from_exception(caught.value, run_id="run_missing")
    assert (error.status, error.code, error.run_id) == (404, "run_not_found", "run_missing")


def test_a_second_active_run_is_429_and_the_waiting_run_is_unchanged(tmp_path):
    """Section 11: reject additional work with 429 rather than hide queue time."""
    gateway, waiting, version = waiting_for_input(tmp_path)
    machine = gateway.orchestrator.machine
    other = machine.create()
    machine.transition(other.run_id, other.version, RunState.ROUTING)
    with pytest.raises(SlotUnavailableError) as caught:
        asyncio.run(gateway.orchestrator.resume(waiting.run_id, version, "staging-api"))
    error = error_from_exception(caught.value, run_id=waiting.run_id)
    assert (error.status, error.code) == (429, "overloaded")
    assert gateway.store.get_run(waiting.run_id).version == version


# --- 403 -------------------------------------------------------------------------


def test_a_wrong_approval_token_is_403_and_the_run_keeps_waiting(tmp_path):
    """Section 5: approval_invalid; nothing is consumed and nothing is mutated."""
    caller = CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION}))
    gateway = build_gateway(
        tmp_path,
        [
            route_turn("delete-demo-database"),
            tool_turn("list_demo_databases", call_id="call_list"),
            tool_turn(
                "delete_demo_database",
                call_id="call_delete",
                database_id="demo-orders-db",
                expected_revision=3,
            ),
        ],
        caller=caller,
    )
    waiting = start(gateway, "Delete the demo-orders-db sandbox database.")
    assert waiting.state is RunState.WAITING_CONFIRMATION
    version = gateway.store.get_run(waiting.run_id).version
    with pytest.raises(ApprovalRejectedError) as caught:
        asyncio.run(
            gateway.orchestrator.resume_confirmation(
                waiting.run_id,
                version,
                approval_id=waiting.confirmation.approval_id,
                token="not-the-token",
                approve=True,
            )
        )
    error = error_from_exception(caught.value, run_id=waiting.run_id)
    assert (error.status, error.code) == (403, "approval_invalid")
    assert "not-the-token" not in error.message
    assert gateway.store.get_run(waiting.run_id).state == "waiting_confirmation"
    assert [d["database_id"] for d in gateway.sandbox.list_databases()].count("demo-orders-db") == 1


# --- 500 -------------------------------------------------------------------------


def test_an_unexpected_error_is_500_with_a_fixed_message(tmp_path):
    """Section 11: internal errors omit credentials, exception text, and secrets."""
    error = error_from_exception(
        RuntimeError("connect failed: Authorization: Bearer sk-live-123"), run_id="run_1"
    )
    assert (error.status, error.code, error.run_id) == (500, "internal_error", "run_1")
    assert "sk-live-123" not in error.message
    assert "sk-live-123" not in str(error.body())
    assert "connect failed" not in error.message


def test_an_api_error_passes_through_unchanged():
    original = ApiError("unsupported_parameter", "parameter n must be 1")
    assert error_from_exception(original) is original
