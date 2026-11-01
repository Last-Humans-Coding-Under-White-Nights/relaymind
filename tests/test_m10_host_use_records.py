"""#255 committed host-operation records (spec 0.66 sections 7.6.1, 7.6.2, 12.6.6).

RM16, RM39; AC30, AC35. The negative records for skill_loaded_use and mcp_use
on the pinned OpenClaw 2026.10.7 host must load with the packaged checker:
closed shape, linux_host machine identity, disabled operation, and every bound
capture and inventory byte. Until the pull request review is written into a
record it is pending and lists no known host limitation; the shipped adapter
registry never lists either operation.
"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from relaymind.evaluation.asset_qualification_record import known_host_limitations, load_record

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/evidence/m10"
INVENTORY = EVIDENCE / "openclaw-host-use-search-20261101-vps.json"
RECORDS = {
    "m10-negative-skill-loaded-use-openclaw-2026.10.7": ("skill_loaded_use", "stop_point_absent"),
    "m10-negative-mcp-use-openclaw-2026.10.7": ("mcp_use", "exact_route_absent"),
}


@pytest.mark.parametrize("record_id", sorted(RECORDS))
def test_committed_negative_record_loads_and_disables_the_operation(record_id):
    record = load_record(EVIDENCE / f"{record_id}.json")
    operation = record.document["operation"]
    assert record.record_id == record_id
    assert (record.outcome, record.scope) == ("negative", "host_operation")
    assert (operation["name"], operation["finding"]) == RECORDS[record_id]
    assert operation["disabled"] is True
    assert (operation["host"], operation["host_version"]) == ("openclaw", "2026.10.7")
    assert record.machine["role"] == "linux_host"
    roles = {subject["role"] for subject in record.document["subjects"]}
    assert roles == {"capture", "inventory"}
    # An approved record is a known limitation; a pending one lifts nothing.
    listed = known_host_limitations([record])
    assert listed == (
        []
        if not record.approved
        else [
            dict(
                operation=operation["name"],
                host="openclaw",
                host_version="2026.10.7",
                finding=operation["finding"],
                record_id=record_id,
            )
        ]
    )


def _module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def _capture(name):
    return json.loads((EVIDENCE / name).read_text())


def cited_source_mismatches(inventory):
    """Cited host sources whose path or SHA-256 differ from the inventory files table."""
    rows = inventory["files"]
    files = {row["path"]: row["sha256"] for row in rows}
    problems = [("duplicate", path) for path in files if sum(r["path"] == path for r in rows) > 1]
    for section in (inventory["skill_loaded_use"]["surfaces"], inventory["mcp_use"]["routes"]):
        for row in section:
            for source in row["source"]:
                if files.get(source["path"]) != source["sha256"]:
                    problems.append((row["name"], source["path"]))
    return problems


def test_inventory_concludes_each_finding_and_binds_host_files():
    """Every cited host file names the exact (path, SHA-256) of the files table (#255)."""
    inventory = json.loads(INVENTORY.read_text())
    assert inventory["host"]["version"] == "2026.10.7"
    assert "stop_point_absent" in inventory["skill_loaded_use"]["conclusion"]
    assert "exact_route_absent" in inventory["mcp_use"]["conclusion"]
    assert all(len(row["sha256"]) == 64 for row in inventory["files"])
    assert cited_source_mismatches(inventory) == []


def test_a_changed_cited_host_hash_is_detected():
    inventory = json.loads(INVENTORY.read_text())
    changed = copy.deepcopy(inventory)
    changed["mcp_use"]["routes"][0]["source"][0]["sha256"] = "0" * 64
    assert cited_source_mismatches(changed) == [
        ("tools.invoke", changed["mcp_use"]["routes"][0]["source"][0]["path"])
    ]
    duplicated = copy.deepcopy(inventory)
    duplicated["files"].append(dict(duplicated["files"][0]))
    assert ("duplicate", inventory["files"][0]["path"]) in cited_source_mismatches(duplicated)


def test_inventory_states_the_spec_0_66_finding_scope():
    """Spec 0.66 section 12.6.6: stop-only points and App view routes do not count."""
    inventory = json.loads(INVENTORY.read_text())
    loaded, mcp = inventory["skill_loaded_use"], inventory["mcp_use"]
    assert "stop or fence" not in loaded["question"]
    for text in (loaded["question"], loaded["conclusion"]):
        assert "fresh person authority, the session incarnation and the pinned bytes" in text
    assert "does not count" in loaded["conclusion"]
    for text in (mcp["question"], mcp["conclusion"]):
        assert "independent, non-App MCP tool by its exact native identity" in text
    app = next(row for row in mcp["routes"] if row["name"].startswith("mcp.app.callTool"))
    assert "App-scoped" in app["verdict"] and "does not count" in app["verdict"]
    effective = next(row for row in mcp["routes"] if "tools.effective" in row["name"])
    assert "empty tool list for the person session" not in effective["verdict"]
    assert "groups[].tools" in effective["verdict"]


def test_mcp_record_binds_the_corrected_discovery_capture():
    """RM39/AC35: the bba0cf1 VPS run lists both colliding tools and stays blocked.

    The earlier CI and VPS2 captures recorded an empty tools.effective list because
    the probe read payload.tools; the pinned host returns payload.groups[].tools.
    The current grader therefore rejects their discovery, and accepts it only in
    the corrected capture. No capture passes exact use.
    """
    record = load_record(EVIDENCE / "m10-negative-mcp-use-openclaw-2026.10.7.json")
    paths = {subject["path"] for subject in record.document["subjects"]}
    assert "openclaw-mcp-use-20261101-vps3.json" in paths
    grade = _module(
        "mcp_use_grade_255", "integrations/openclaw/test/host-mcp-use-qualification/grade.py"
    )
    corrected = _capture("openclaw-mcp-use-20261101-vps3.json")
    assert (corrected["status"], corrected["reason"]) == ("blocked", "exact_route_unqualified")
    assert grade.fixture_discovery_pass(corrected) is True
    listed = [
        event
        for event in corrected["mcp_use"]["inventories"]
        if event["method"] == "tools.effective"
    ]
    assert listed and all(
        {(row["server"], row["original"]) for row in event["mcp_tools"]}
        == {("alpha.one", "append"), ("alpha?one", "append")}
        for event in listed
    )
    assert {e["kind"] for e in corrected["mcp_use"]["server_events"]} <= {"started", "list"}
    for name in ("openclaw-mcp-use-20261101-ci.json", "openclaw-mcp-use-20261101-vps2.json"):
        earlier = _capture(name)
        assert all(
            event["tool_names"] == []
            for event in earlier["mcp_use"]["inventories"]
            if event["method"] == "tools.effective"
        )
        assert grade.fixture_discovery_pass(earlier) is False
    for path in paths - {"openclaw-host-use-search-20261101-vps.json"}:
        assert grade.mcp_use_pass(_capture(path)) is False


def test_current_loaded_use_predicate_accepts_the_vps2_capture():
    """RM39/AC30: the VPS2 capture predates the selected-skill pin check (5d92b64).

    Its embedded check is false; the current predicate accepts it. The reset case
    is the real-host operator.admin refusal. The fence finding is unchanged.
    """
    qualify = _module(
        "ordinary_qualify_255", "integrations/openclaw/test/host-ordinary-qualification/qualify.py"
    )
    capture = _capture("openclaw-loaded-use-observation-20261101-vps2.json")
    assert capture["checks"]["ordinary_skill_loaded_use_observation"] is False
    assert qualify.ordinary_skill_loaded_use_pass(capture) is True
    reset = [
        case
        for case in capture["ordinary_skill_loaded_use"]["cases"]
        if case.get("case") == "reset"
    ]
    assert [case["observed"] for case in reset] == ["control_refused"]


def test_shipped_registry_lists_neither_operation():
    source = (ROOT / "integrations/openclaw/src/recommendation-native-qualification.ts").read_text()
    shipped = source.split("NATIVE_QUALIFICATIONS", 1)[1]
    assert '"skill_loaded_use"' not in shipped and '"mcp_use"' not in shipped
