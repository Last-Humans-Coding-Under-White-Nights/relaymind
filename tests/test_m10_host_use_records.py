"""#255 committed host-operation records (spec 0.66 sections 7.6.1, 7.6.2, 12.6.6).

RM16, RM39; AC30, AC35. The negative records for skill_loaded_use and mcp_use
on the pinned OpenClaw 2026.10.7 host must load with the packaged checker:
closed shape, linux_host machine identity, disabled operation, and every bound
capture and inventory byte. Until the pull request review is written into a
record it is pending and lists no known host limitation; the shipped adapter
registry never lists either operation.
"""

import json
from pathlib import Path

import pytest

from relaymind.evaluation.asset_qualification_record import known_host_limitations, load_record

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/evidence/m10"
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


def test_inventory_concludes_each_finding_and_binds_host_files():
    inventory = json.loads((EVIDENCE / "openclaw-host-use-search-20261101-vps.json").read_text())
    assert inventory["host"]["version"] == "2026.10.7"
    assert "stop_point_absent" in inventory["skill_loaded_use"]["conclusion"]
    assert "exact_route_absent" in inventory["mcp_use"]["conclusion"]
    paths = {row["path"] for row in inventory["files"]}
    cited = {
        source["path"]
        for section in (inventory["skill_loaded_use"]["surfaces"], inventory["mcp_use"]["routes"])
        for row in section
        for source in row["source"]
    }
    assert cited <= paths


def test_shipped_registry_lists_neither_operation():
    source = (ROOT / "integrations/openclaw/src/recommendation-native-qualification.ts").read_text()
    shipped = source.split("NATIVE_QUALIFICATIONS", 1)[1]
    assert '"skill_loaded_use"' not in shipped and '"mcp_use"' not in shipped
