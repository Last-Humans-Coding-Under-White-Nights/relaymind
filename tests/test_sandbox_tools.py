"""Sandbox demo data service and the seven demo tools.

RM23: real gateway function executions against a clearly labeled
sandbox data service. Section 9: the seven tool tables (input fields
and output data), service IDs staging-api and payments-api, windows of
15, 60, or 1440 minutes, UTC timestamps, additionalProperties=false on
input objects, a capped log list with timestamp and source, fixture
scenario and observation metadata in every output, an unavailable
health status as a successful observation, a failed ToolResult when the
tool itself cannot be reached, and an atomic expected_revision compare
on delete_demo_database that returns a conflict without mutation.
Section 8: outputs sit inside the ToolResult envelope. Issue #12.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from helpers import BASELINE, make_demo_tools, make_sandbox
from relaymind.tools import (
    CallerContext,
    OperatorPolicy,
    SideEffect,
    ToolConflictError,
    ToolRejectedError,
    ToolStatus,
    validate_tool_result,
)
from relaymind.tools import sandbox as sandbox_module
from relaymind.tools.sandbox import (
    DEMO_TOOL_NAMES,
    DEMO_TOOL_SCHEMAS,
    SandboxDataService,
    SandboxFixtureError,
)

OBSERVED_AT = "2026-10-20T03:00:00Z"

SECTION_9_TOOLS = {
    "get_service_health",
    "get_latest_deployment",
    "get_incident_counts",
    "get_metrics",
    "get_recent_logs",
    "list_demo_databases",
    "delete_demo_database",
}


@pytest.fixture
def sandbox(tmp_path):
    """A fresh disposable copy of the baseline fixture for each test."""
    return make_sandbox(tmp_path)


@pytest.fixture
def registry(tmp_path, sandbox):
    return make_demo_tools(tmp_path, sandbox)


def call(registry, name, arguments, operation_id="op_1"):
    result = registry.invoke(name, arguments, available=SECTION_9_TOOLS, operation_id=operation_id)
    validate_tool_result(result.to_json_object())
    return result


def output_validator(name):
    return Draft202012Validator(DEMO_TOOL_SCHEMAS[name]["output_schema"])


# --- Registration and published schemas ------------------------------------


def test_the_seven_section_9_tools_are_registered(registry):
    assert DEMO_TOOL_NAMES == SECTION_9_TOOLS
    assert registry.names() == SECTION_9_TOOLS


def test_only_delete_demo_database_is_mutating(registry):
    mutating = {
        name for name in SECTION_9_TOOLS if registry.get(name).side_effect is SideEffect.MUTATING
    }
    assert mutating == {"delete_demo_database"}


def test_mutating_and_read_tools_need_different_caller_permissions(registry):
    """RM08: a read-only caller cannot reach the mutating tool."""
    reader = CallerContext(permissions=frozenset({"demo.read"}))
    available = registry.available_tools(
        skill_allowlist=SECTION_9_TOOLS,
        policy=OperatorPolicy(enabled_tools=frozenset(SECTION_9_TOOLS)),
        caller=reader,
    )
    assert available == SECTION_9_TOOLS - {"delete_demo_database"}


@pytest.mark.parametrize("name", sorted(SECTION_9_TOOLS))
def test_every_tool_schema_is_published_and_valid(name):
    path = (
        Path(__file__).resolve().parent.parent
        / "src/relaymind/tools/schemas/demo"
        / f"{name}.v1.json"
    )
    published = json.loads(path.read_text("utf-8"))
    assert published == DEMO_TOOL_SCHEMAS[name]
    for key in ("input_schema", "output_schema"):
        Draft202012Validator.check_schema(published[key])
    assert published["input_schema"]["additionalProperties"] is False


IDENTIFIER_LABELS = {
    ("get_service_health", "service"): ("service", "服务"),
    ("get_latest_deployment", "service"): ("service", "服务"),
    ("get_incident_counts", "service"): ("service", "服务"),
    ("get_metrics", "service"): ("service", "服务"),
    ("get_recent_logs", "service"): ("service", "服务"),
    ("delete_demo_database", "database_id"): ("database", "数据库"),
}


def _identifier_properties():
    """Input properties that name a resource: a closed set of strings or a
    pattern-constrained string (RM05)."""
    found = {}
    for name, schema in DEMO_TOOL_SCHEMAS.items():
        for prop, spec in schema["input_schema"]["properties"].items():
            strings = spec.get("enum") and all(isinstance(v, str) for v in spec["enum"])
            if strings or (spec.get("type") == "string" and "pattern" in spec):
                found[(name, prop)] = spec
    return found


def test_every_identifier_input_has_a_bilingual_label():
    """Section 12.2 and RM05: a clarification names the missing input in
    the run's language (section 5), so each identifier property carries
    an English title and an x-relaymind-label with en and zh text. Other
    properties (windows, limits, revisions) are not identifiers and have
    no label. The published schemas stay valid (checked above)."""
    found = _identifier_properties()
    assert set(found) == set(IDENTIFIER_LABELS)
    for key, (english, chinese) in IDENTIFIER_LABELS.items():
        spec = found[key]
        assert spec["title"] == english
        assert spec["x-relaymind-label"] == {"en": english, "zh": chinese}
    for name, schema in DEMO_TOOL_SCHEMAS.items():
        for prop, spec in schema["input_schema"]["properties"].items():
            if (name, prop) not in IDENTIFIER_LABELS:
                assert "x-relaymind-label" not in spec


def test_labels_are_annotations_that_do_not_change_validation(tmp_path):
    """An x- keyword is an unknown JSON Schema keyword, so it only
    annotates: the labelled schema accepts and rejects the same input."""
    tools = make_demo_tools(tmp_path)
    tool = tools.get("get_service_health")
    validator = Draft202012Validator(tool.declaration.input_schema)
    assert validator.is_valid({"service": "staging-api"})
    assert not validator.is_valid({"service": "Staging-API"})
    assert not validator.is_valid({"service": "服务"})


@pytest.mark.parametrize("name", sorted(SECTION_9_TOOLS - {"list_demo_databases"}))
def test_every_listed_input_field_is_required(name):
    """Section 9: all listed input fields are required."""
    schema = DEMO_TOOL_SCHEMAS[name]["input_schema"]
    assert set(schema["required"]) == set(schema["properties"])


def test_list_demo_databases_takes_an_empty_object():
    schema = DEMO_TOOL_SCHEMAS["list_demo_databases"]["input_schema"]
    assert schema["properties"] == {}
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_service_health", {"service": "prod-api"}),
        ("get_service_health", {"service": "staging-api", "region": "eu"}),
        ("get_latest_deployment", {}),
        ("get_incident_counts", {"service": "staging-api", "window_minutes": 30}),
        ("get_incident_counts", {"service": "staging-api"}),
        ("get_metrics", {"service": "payments-api", "window_minutes": "60"}),
        ("get_recent_logs", {"service": "staging-api", "limit": 0}),
        ("get_recent_logs", {"service": "staging-api", "limit": 101}),
        ("list_demo_databases", {"all": True}),
        ("delete_demo_database", {"database_id": "demo-orders-db"}),
        (
            "delete_demo_database",
            {"database_id": "postgres://admin:pw@prod-db:5432/orders", "expected_revision": 3},
        ),
        ("delete_demo_database", {"database_id": "demo-orders-db", "expected_revision": -1}),
    ],
)
def test_out_of_bounds_inputs_are_rejected_before_execution(registry, sandbox, name, arguments):
    """Section 9: bounded services, windows, and limits; explicit IDs;
    never a production connection string."""
    with pytest.raises(ToolRejectedError) as excinfo:
        call(registry, name, arguments)
    assert excinfo.value.code == "invalid_arguments"
    assert sandbox.list_databases() == [
        {"database_id": "demo-orders-db", "display_name": "Demo orders (sandbox)", "revision": 3},
        {
            "database_id": "demo-analytics-db",
            "display_name": "Demo analytics (sandbox)",
            "revision": 3,
        },
    ]


# --- Read tools -------------------------------------------------------------


def test_get_service_health_returns_status_and_check_time(registry):
    result = call(registry, "get_service_health", {"service": "staging-api"})
    assert result.status is ToolStatus.SUCCEEDED
    assert result.data == {
        "service": "staging-api",
        "status": "healthy",
        "checked_at": "2026-10-20T02:58:00Z",
        "fixture": {"scenario": "baseline", "observed_at": OBSERVED_AT},
    }


def test_an_unavailable_health_status_is_a_successful_observation(registry):
    """Section 9 and RM16: unavailable is data, not a tool failure."""
    result = call(registry, "get_service_health", {"service": "payments-api"})
    assert result.status is ToolStatus.SUCCEEDED
    assert result.data["status"] == "unavailable"
    assert result.data["checked_at"] == "2026-10-20T02:59:00Z"


def test_get_latest_deployment_returns_id_time_and_revision(registry):
    result = call(registry, "get_latest_deployment", {"service": "staging-api"})
    assert result.data == {
        "service": "staging-api",
        "deployment_id": "dep-staging-0142",
        "deployed_at": "2026-10-19T22:10:00Z",
        "revision": "4f2c9e1",
        "fixture": {"scenario": "baseline", "observed_at": OBSERVED_AT},
    }


def test_no_deployment_gives_null_id_time_and_revision_together(registry):
    result = call(registry, "get_latest_deployment", {"service": "payments-api"})
    assert result.status is ToolStatus.SUCCEEDED
    assert result.data["deployment_id"] is None
    assert result.data["deployed_at"] is None
    assert result.data["revision"] is None


def test_the_deployment_schema_keeps_id_and_time_nullable_together():
    validator = output_validator("get_latest_deployment")
    half = {
        "service": "staging-api",
        "deployment_id": "dep-1",
        "deployed_at": None,
        "revision": None,
        "fixture": {"scenario": "baseline", "observed_at": OBSERVED_AT},
    }
    assert not validator.is_valid(half)


def test_the_deployment_schema_requires_a_revision_when_a_deployment_exists():
    """Section 9: revision is nullable only when no deployment exists."""
    validator = output_validator("get_latest_deployment")
    missing_revision = {
        "service": "staging-api",
        "deployment_id": "dep-1",
        "deployed_at": "2026-10-19T22:10:00Z",
        "revision": None,
        "fixture": {"scenario": "baseline", "observed_at": OBSERVED_AT},
    }
    assert not validator.is_valid(missing_revision)


@pytest.mark.parametrize("window", [15, 60, 1440])
def test_get_incident_counts_categories_sum_to_the_total(registry, window):
    result = call(
        registry, "get_incident_counts", {"service": "payments-api", "window_minutes": window}
    )
    data = result.data
    assert data["service"] == "payments-api"
    assert data["window_minutes"] == window
    assert data["critical"] + data["warning"] + data["info"] == data["total"]
    assert min(data["total"], data["critical"], data["warning"], data["info"]) >= 0


def test_get_incident_counts_returns_the_fixture_counts(registry):
    result = call(
        registry, "get_incident_counts", {"service": "staging-api", "window_minutes": 1440}
    )
    assert {key: result.data[key] for key in ("total", "critical", "warning", "info")} == {
        "total": 7,
        "critical": 1,
        "warning": 3,
        "info": 3,
    }


def test_get_metrics_returns_bounded_samples(registry):
    result = call(registry, "get_metrics", {"service": "payments-api", "window_minutes": 15})
    samples = result.data["samples"]
    assert samples[0] == {
        "timestamp": "2026-10-20T02:50:00Z",
        "p95_latency_ms": 2450.0,
        "error_rate": 0.41,
        "request_count": 312,
    }
    assert all(0 <= s["error_rate"] <= 1 and s["request_count"] >= 0 for s in samples)
    assert result.data["window_minutes"] == 15


def test_the_metrics_schema_bounds_error_rate_and_request_count():
    validator = output_validator("get_metrics")
    base = {
        "service": "staging-api",
        "window_minutes": 15,
        "fixture": {"scenario": "baseline", "observed_at": OBSERVED_AT},
    }
    sample = {
        "timestamp": "2026-10-20T02:50:00Z",
        "p95_latency_ms": 1.0,
        "error_rate": 0.5,
        "request_count": 1,
    }
    assert validator.is_valid({**base, "samples": [sample]})
    assert not validator.is_valid({**base, "samples": [{**sample, "error_rate": 1.5}]})
    assert not validator.is_valid({**base, "samples": [{**sample, "request_count": -1}]})


def test_get_recent_logs_caps_the_list_and_flags_truncation(registry):
    result = call(registry, "get_recent_logs", {"service": "payments-api", "limit": 2})
    assert result.data["truncated"] is True
    assert result.truncated is True
    assert result.data["entries"] == [
        {
            "timestamp": "2026-10-20T02:58:52Z",
            "level": "error",
            "message": "upstream ledger-db timeout after 2000 ms",
            "source": "payments-api/worker-1",
        },
        {
            "timestamp": "2026-10-20T02:58:10Z",
            "level": "error",
            "message": "health probe failed: no healthy upstream",
            "source": "payments-api/web-1",
        },
    ]


def test_get_recent_logs_within_the_limit_is_not_truncated(registry):
    result = call(registry, "get_recent_logs", {"service": "staging-api", "limit": 100})
    assert result.data["truncated"] is False
    assert result.truncated is False
    assert len(result.data["entries"]) == 3


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_service_health", {"service": "staging-api"}),
        ("get_latest_deployment", {"service": "payments-api"}),
        ("get_incident_counts", {"service": "staging-api", "window_minutes": 60}),
        ("get_metrics", {"service": "staging-api", "window_minutes": 1440}),
        ("get_recent_logs", {"service": "staging-api", "limit": 5}),
        ("list_demo_databases", {}),
        ("delete_demo_database", {"database_id": "demo-orders-db", "expected_revision": 3}),
    ],
)
def test_every_output_carries_fixture_metadata_and_validates(registry, name, arguments):
    """Section 9: outputs carry fixture scenario and observation metadata
    and validate against the published output schema."""
    result = call(registry, name, arguments)
    assert result.status is ToolStatus.SUCCEEDED
    assert result.data["fixture"] == {"scenario": "baseline", "observed_at": OBSERVED_AT}
    assert output_validator(name).is_valid(result.data)
    assert result.observed_at == OBSERVED_AT


def test_list_demo_databases_returns_ids_names_and_revisions(registry):
    result = call(registry, "list_demo_databases", {})
    assert result.data["databases"] == [
        {"database_id": "demo-orders-db", "display_name": "Demo orders (sandbox)", "revision": 3},
        {
            "database_id": "demo-analytics-db",
            "display_name": "Demo analytics (sandbox)",
            "revision": 3,
        },
    ]


# --- delete_demo_database ----------------------------------------------------


def test_delete_with_the_current_revision_removes_only_that_fixture(registry, sandbox):
    result = call(
        registry,
        "delete_demo_database",
        {"database_id": "demo-orders-db", "expected_revision": 3},
    )
    assert result.status is ToolStatus.SUCCEEDED
    assert result.data == {
        "database_id": "demo-orders-db",
        "deleted": True,
        "revision": 4,
        "fixture": {"scenario": "baseline", "observed_at": OBSERVED_AT},
    }
    assert [db["database_id"] for db in sandbox.list_databases()] == ["demo-analytics-db"]


def test_delete_with_a_stale_revision_is_a_conflict_without_mutation(registry, sandbox):
    """Section 9: compare expected_revision atomically; conflict, no mutation."""
    before = sandbox.state_bytes()
    result = call(
        registry,
        "delete_demo_database",
        {"database_id": "demo-orders-db", "expected_revision": 2},
    )
    assert result.status is ToolStatus.FAILED
    assert result.data is None
    assert result.error["code"] == "revision_conflict"
    assert sandbox.state_bytes() == before


def test_a_second_delete_of_the_same_revision_conflicts(registry, sandbox):
    """RM11, section 9: the handler compares expected_revision first, so a
    stale revision is a revision_conflict even when the database it names
    is already gone."""
    arguments = {"database_id": "demo-orders-db", "expected_revision": 3}
    assert call(registry, "delete_demo_database", arguments).status is ToolStatus.SUCCEEDED
    before = sandbox.state_bytes()
    second = call(registry, "delete_demo_database", arguments, operation_id="op_2")
    assert second.status is ToolStatus.FAILED
    assert second.error["code"] == "revision_conflict"
    assert sandbox.state_bytes() == before


def test_a_stale_revision_for_an_unknown_database_is_a_revision_conflict(registry, sandbox):
    """RM11, section 9: the revision compare comes before the ID lookup."""
    before = sandbox.state_bytes()
    result = call(
        registry,
        "delete_demo_database",
        {"database_id": "demo-invented-db", "expected_revision": 1},
    )
    assert result.error["code"] == "revision_conflict"
    assert sandbox.state_bytes() == before


def test_deleting_an_unknown_database_changes_nothing(registry, sandbox):
    before = sandbox.state_bytes()
    result = call(
        registry,
        "delete_demo_database",
        {"database_id": "demo-invented-db", "expected_revision": 3},
    )
    assert result.status is ToolStatus.FAILED
    assert result.error["code"] == "database_not_found"
    assert sandbox.state_bytes() == before


def test_delete_never_writes_the_seed_fixture(registry, sandbox):
    """RM23: only the disposable working copy is modified."""
    seed_before = BASELINE.read_bytes()
    call(
        registry, "delete_demo_database", {"database_id": "demo-orders-db", "expected_revision": 3}
    )
    assert BASELINE.read_bytes() == seed_before
    assert sandbox.state_path.parent != BASELINE.parent


def test_reset_restores_the_seed_state(registry, sandbox):
    call(
        registry, "delete_demo_database", {"database_id": "demo-orders-db", "expected_revision": 3}
    )
    sandbox.reset()
    assert [db["database_id"] for db in sandbox.list_databases()] == [
        "demo-orders-db",
        "demo-analytics-db",
    ]


# --- Unreachable tool and fixture validation ---------------------------------


def write_seed(tmp_path, mutate) -> Path:
    data = json.loads(BASELINE.read_text("utf-8"))
    mutate(data)
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(data), "utf-8")
    return path


def test_an_unreachable_tool_produces_a_failed_tool_result(tmp_path):
    """Section 9: failure to contact the tool itself is a failed ToolResult."""
    seed = write_seed(tmp_path, lambda d: d.update(unreachable_tools=["get_service_health"]))
    registry = make_demo_tools(tmp_path, make_sandbox(tmp_path, seed))
    result = call(registry, "get_service_health", {"service": "staging-api"})
    assert result.status is ToolStatus.FAILED
    assert result.data is None
    assert result.error["code"] == "tool_unavailable"


def test_a_fixture_whose_incident_categories_do_not_sum_is_rejected(tmp_path):
    def break_sum(data):
        data["services"]["staging-api"]["incidents"]["60"]["total"] = 99

    seed = write_seed(tmp_path, break_sum)
    with pytest.raises(SandboxFixtureError, match="total"):
        SandboxDataService(seed, work_dir=tmp_path / "work")


def test_a_fixture_missing_a_demo_service_is_rejected(tmp_path):
    seed = write_seed(tmp_path, lambda d: d["services"].pop("payments-api"))
    with pytest.raises(SandboxFixtureError, match="payments-api"):
        SandboxDataService(seed, work_dir=tmp_path / "work")


def test_a_fixture_with_a_non_utc_timestamp_is_rejected(tmp_path):
    def local_time(data):
        data["services"]["staging-api"]["health"]["checked_at"] = "2026-10-20 10:58:00+08:00"

    seed = write_seed(tmp_path, local_time)
    with pytest.raises(SandboxFixtureError):
        SandboxDataService(seed, work_dir=tmp_path / "work")


def test_the_sandbox_is_labeled_as_synthetic(sandbox):
    """RM23: a clearly labeled sandbox data service."""
    assert sandbox.scenario == "baseline"
    assert "sandbox" in sandbox.label.lower()


def test_tests_never_share_working_state(tmp_path):
    """Fixtures reset between tests: two services never share a working copy."""
    first = SandboxDataService(BASELINE, work_dir=tmp_path / "a")
    second = SandboxDataService(BASELINE, work_dir=tmp_path / "b")
    first.delete_database("demo-orders-db", expected_revision=3)
    assert [db["database_id"] for db in second.list_databases()][0] == "demo-orders-db"
    shutil.rmtree(tmp_path / "a")


# --- Review fixes: whole-number floats, shared state, fixture checks ---------


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_incident_counts", {"service": "staging-api", "window_minutes": 60.0}),
        ("get_metrics", {"service": "staging-api", "window_minutes": 15.0}),
        ("get_recent_logs", {"service": "staging-api", "limit": 2.0}),
        ("delete_demo_database", {"database_id": "demo-orders-db", "expected_revision": 3.0}),
    ],
)
def test_whole_number_floats_are_rejected_before_the_handler(registry, sandbox, name, arguments):
    """RM09, section 9: windows, limits, and revisions are integers; 60.0
    passes draft 2020-12 but is rejected as invalid_arguments, so no handler
    sees it and no float revision is ever returned."""
    before = sandbox.state_bytes()
    with pytest.raises(ToolRejectedError) as excinfo:
        call(registry, name, arguments)
    assert excinfo.value.code == "invalid_arguments"
    assert sandbox.state_bytes() == before


def test_a_new_service_on_an_existing_work_dir_keeps_its_state(tmp_path):
    """RM23, section 9: a second service on the same working copy does not
    reset it; only an explicit reset does."""
    work = tmp_path / "shared"
    first = SandboxDataService(BASELINE, work_dir=work, reset=True)
    first.delete_database("demo-orders-db", expected_revision=3)
    second = SandboxDataService(BASELINE, work_dir=work)
    assert [db["database_id"] for db in second.list_databases()] == ["demo-analytics-db"]
    resetting = SandboxDataService(BASELINE, work_dir=work, reset=True)
    assert len(resetting.list_databases()) == 2
    assert len(first.list_databases()) == 2


def test_a_stale_revision_from_a_second_service_conflicts(tmp_path):
    """Section 9: the expected_revision compare holds across services that
    share one working copy."""
    work = tmp_path / "shared"
    first = SandboxDataService(BASELINE, work_dir=work, reset=True)
    second = SandboxDataService(BASELINE, work_dir=work)
    first.delete_database("demo-orders-db", expected_revision=3)
    with pytest.raises(ToolConflictError) as excinfo:
        second.delete_database("demo-orders-db", expected_revision=3)
    assert excinfo.value.code == "revision_conflict"


_HOLD_LOCK = """
import sys
from pathlib import Path
from relaymind.tools.sandbox import file_lock

with file_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.read()
"""


def test_delete_waits_for_the_cross_process_state_lock(tmp_path):
    """Section 9: the compare and the write happen under a lock that other
    processes honor (spec section 3 platforms include Windows, so this is a
    file lock, not a thread lock)."""
    sandbox = SandboxDataService(BASELINE, work_dir=tmp_path / "shared", reset=True)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(sandbox.lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        outcome = []
        worker = threading.Thread(
            target=lambda: outcome.append(
                sandbox.delete_database("demo-orders-db", expected_revision=3)
            )
        )
        worker.start()
        worker.join(timeout=0.5)
        assert worker.is_alive(), "delete ran while another process held the lock"
        holder.stdin.close()
        holder.wait(timeout=10)
        worker.join(timeout=10)
        assert outcome and outcome[0]["deleted"] is True
    finally:
        if holder.poll() is None:
            holder.kill()


def test_fixture_validation_runs_the_tools_own_output_functions(tmp_path, monkeypatch):
    """Section 9: the seed is checked with the same output functions the
    tools run, so an output that leaves its schema fails at load."""
    monkeypatch.setitem(
        sandbox_module._OUTPUTS, "get_latest_deployment", lambda state, args: {"service": "x"}
    )
    with pytest.raises(SandboxFixtureError, match="get_latest_deployment"):
        SandboxDataService(BASELINE, work_dir=tmp_path / "work")


def test_fixture_validation_covers_the_delete_output(tmp_path):
    """Section 9: delete_demo_database output is checked at load too; a
    float fixture revision in the seed is rejected (integer revision)."""

    def float_revision(data):
        data["revision"] = 3.0

    seed = write_seed(tmp_path, float_revision)
    with pytest.raises(SandboxFixtureError):
        SandboxDataService(seed, work_dir=tmp_path / "work")


# --- Review fixes: one fixture revision, reset, log order, temp files --------


def revisions(sandbox):
    return {db["database_id"]: db["revision"] for db in sandbox.list_databases()}


def test_every_database_reports_the_one_fixture_revision(sandbox):
    """Section 9: expected_revision is compared with the fixture revision,
    so every listed database reports that one revision."""
    assert revisions(sandbox) == {"demo-orders-db": 3, "demo-analytics-db": 3}


def test_a_delete_bumps_the_fixture_revision_and_returns_the_stored_value(registry, sandbox):
    """Section 9: delete returns the resulting fixture revision, the value
    now stored, and every other database moves to it."""
    result = call(
        registry, "delete_demo_database", {"database_id": "demo-orders-db", "expected_revision": 3}
    )
    assert result.data["revision"] == 4
    assert revisions(sandbox) == {"demo-analytics-db": 4}


def test_a_delete_makes_a_revision_read_before_it_stale(registry, sandbox):
    """Section 9: the fixture changed, so an approval bound to the old
    revision of another database conflicts without mutation."""
    call(
        registry, "delete_demo_database", {"database_id": "demo-orders-db", "expected_revision": 3}
    )
    before = sandbox.state_bytes()
    result = call(
        registry,
        "delete_demo_database",
        {"database_id": "demo-analytics-db", "expected_revision": 3},
        operation_id="op_2",
    )
    assert result.error["code"] == "revision_conflict"
    assert sandbox.state_bytes() == before


def test_reset_bumps_the_fixture_revision(registry, sandbox):
    """Section 9: a reset is a fixture change too, so a revision read
    before it, even the seed's own, is stale afterwards."""
    call(
        registry, "delete_demo_database", {"database_id": "demo-orders-db", "expected_revision": 3}
    )
    sandbox.reset()
    assert revisions(sandbox) == {"demo-orders-db": 5, "demo-analytics-db": 5}
    for stale in (3, 4):
        with pytest.raises(ToolConflictError) as excinfo:
            sandbox.delete_database("demo-orders-db", expected_revision=stale)
        assert excinfo.value.code == "revision_conflict"


def test_a_reset_service_on_an_existing_work_dir_bumps_the_revision(tmp_path):
    """Section 9: reset=True on an existing working copy is a reset."""
    work = tmp_path / "shared"
    SandboxDataService(BASELINE, work_dir=work, reset=True)
    again = SandboxDataService(BASELINE, work_dir=work, reset=True)
    assert set(revisions(again).values()) == {4}


def test_reset_validates_the_seed_and_keeps_the_copy_on_failure(tmp_path):
    """Section 9: reset reads the seed again and runs the same fixture
    check as the constructor; a broken seed changes nothing."""
    seed = write_seed(tmp_path, lambda d: None)
    sandbox = SandboxDataService(seed, work_dir=tmp_path / "work", reset=True)
    sandbox.delete_database("demo-orders-db", expected_revision=3)
    before = sandbox.state_bytes()
    data = json.loads(seed.read_text("utf-8"))
    data["services"]["staging-api"]["incidents"]["60"]["total"] = 99
    seed.write_text(json.dumps(data), "utf-8")
    with pytest.raises(SandboxFixtureError, match="total"):
        sandbox.reset()
    assert sandbox.state_bytes() == before


def test_reset_refreshes_the_scenario_and_label(tmp_path):
    """Section 9: outputs carry the scenario of the fixture now loaded."""
    seed = write_seed(tmp_path, lambda d: None)
    sandbox = SandboxDataService(seed, work_dir=tmp_path / "work", reset=True)
    registry = make_demo_tools(tmp_path, sandbox)
    data = json.loads(seed.read_text("utf-8"))
    data.update(scenario="outage", label="RelayMind sandbox outage scenario. Synthetic.")
    seed.write_text(json.dumps(data), "utf-8")
    sandbox.reset()
    assert sandbox.scenario == "outage"
    assert sandbox.label == "RelayMind sandbox outage scenario. Synthetic."
    result = call(registry, "get_service_health", {"service": "staging-api"})
    assert result.data["fixture"]["scenario"] == "outage"


def test_a_second_service_reports_the_scenario_of_the_shared_copy(tmp_path):
    """Section 9: scenario metadata comes from the working copy, so a
    service that did not run the reset still reports the right one."""
    seed = write_seed(tmp_path, lambda d: None)
    first = SandboxDataService(seed, work_dir=tmp_path / "work", reset=True)
    second = SandboxDataService(seed, work_dir=tmp_path / "work")
    data = json.loads(seed.read_text("utf-8"))
    data["scenario"] = "outage"
    seed.write_text(json.dumps(data), "utf-8")
    first.reset()
    assert second.scenario == "outage"
    assert second.service_health("staging-api")["fixture"]["scenario"] == "outage"


def test_recent_logs_are_returned_newest_first_whatever_the_seed_order(tmp_path):
    """Section 9: get_recent_logs returns the newest entries, newest first."""

    def oldest_first(data):
        data["services"]["payments-api"]["logs"].reverse()

    sandbox = make_sandbox(tmp_path, write_seed(tmp_path, oldest_first))
    entries = sandbox.recent_logs("payments-api", 2)["entries"]
    assert [entry["timestamp"] for entry in entries] == [
        "2026-10-20T02:58:52Z",
        "2026-10-20T02:58:10Z",
    ]


def test_a_write_does_not_depend_on_a_fixed_temp_file_name(sandbox):
    """Section 9: the working copy is replaced through a unique temp file,
    so a leftover file at any fixed name cannot block or corrupt a write."""
    (sandbox.state_path.parent / "sandbox_state.tmp").mkdir()
    sandbox.delete_database("demo-orders-db", expected_revision=3)
    assert revisions(sandbox) == {"demo-analytics-db": 4}
    leftovers = {p.name for p in sandbox.state_path.parent.iterdir()}
    assert leftovers == {"sandbox_state.json", "sandbox_state.lock", "sandbox_state.tmp"}
