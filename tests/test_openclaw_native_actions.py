"""Real gateway/production adapter composition over controlled contexts (RM37/RM39; AC28/AC30).

This is a deterministic fixture. Native person/currentness qualification is separate.
"""

import asyncio
import json
import subprocess

import pytest

import test_openclaw_host_transport as node_guard
from api_helpers import RunningServer
from recommendation_helpers import setup
from test_asset_user_actions import ACTION_TOKEN, enable_fixture_action
from test_openclaw_host_transport import PACKAGE, _node
from test_openclaw_plugin import driver_env


@pytest.mark.parametrize(
    ("required", "typescript_present", "outcome"),
    [(False, False, "skip"), (True, False, "fail"), (True, True, "pass")],
)
def test_node_build_guard_respects_optional_and_required_modes(
    monkeypatch, tmp_path, required, typescript_present, outcome
):
    """RM39/AC30: core may skip missing npm deps; the plugin job must fail."""
    monkeypatch.setattr(node_guard, "PACKAGE", tmp_path)
    monkeypatch.setattr(node_guard, "_missing", lambda: None)
    monkeypatch.setattr(node_guard, "REQUIRED", required)
    if typescript_present:
        tsc = tmp_path / "node_modules/typescript/bin/tsc"
        tsc.parent.mkdir(parents=True)
        tsc.write_text("", encoding="utf-8")
    if outcome == "skip":
        with pytest.raises(pytest.skip.Exception, match="typescript"):
            node_guard.require_node_build_dependencies()
    elif outcome == "fail":
        with pytest.raises(pytest.fail.Exception, match="RELAYMIND_OPENCLAW_NODE=1"):
            node_guard.require_node_build_dependencies()
    else:
        assert node_guard.require_node_build_dependencies() is None


def test_native_action_factory_records_explicit_choice_and_private_feedback_once(tmp_path):
    """RM37/RM39: actual routes/CAS, scope, deduplication and erasure survive native translation."""
    node_guard.require_node_build_dependencies()
    for config in ("tsconfig.build.json", "tsconfig.control-ui.json"):
        subprocess.run(
            [_node(), str(PACKAGE / "node_modules/typescript/bin/tsc"), "-p", config],
            cwd=PACKAGE,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    app, service, runs, _ = setup(tmp_path)
    enable_fixture_action(app, service, runs)
    asyncio.run(service.refresh_catalog())
    output = tmp_path / "native-actions.json"
    try:
        with RunningServer(app) as server:
            env = driver_env(server.base_url, app.state.application_token)
            env["RELAYMIND_ACTION_TOKEN"] = ACTION_TOKEN
            env["RELAYMIND_FIXTURE_WORKSPACE"] = str(tmp_path)
            process = subprocess.run(
                [
                    _node(),
                    str(PACKAGE / "test/recommendation-transport/drive-native-actions.mjs"),
                    str(output),
                ],
                cwd=PACKAGE,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        assert output.exists(), process.stderr
        raw = output.read_text(encoding="utf-8")
        assert ACTION_TOKEN not in raw + process.stdout + process.stderr
        assert app.state.application_token not in raw + process.stdout + process.stderr
        assert process.returncode == 0, raw + process.stderr
        result = json.loads(raw)
        assert result["fixture"] == "controlled-host-context-real-gateway"
        assert result["choose"] == result["choiceReplay"]
        assert result["choose"]["state"] == "chosen"
        assert result["choose"]["selected_action"]["scope"] == {"user_id": "u", "workspace_id": "w"}
        assert result["inspect"]["records"][0]["feedback"]["kind"] == "correction"
        assert result["deleted"]["deleted"] is True
        assert result["reset"]["feedback_revision"] == "3"
        assert result["retired"]["ok"] is False
        assert result["retiredExchanges"] == 0
        with runs.sql_read() as db:
            events = [
                json.loads(row[0])
                for row in db.execute("SELECT event_json FROM recommendation_events ORDER BY seq")
            ]
        assert sum(event["event_type"] == "chosen" for event in events) == 1
        assert sum(event["event_type"] == "feedback.recorded" for event in events) == 1
        assert next(event for event in events if event["event_type"] == "feedback.recorded")[
            "erased"
        ]
    finally:
        asyncio.run(service.stop())
        runs.close()
