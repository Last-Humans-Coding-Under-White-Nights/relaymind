"""Dormant TS recommendation seam over the actual gateway (RM36/RM37/RM39; AC27/AC28/AC30).

The explicit backend qualification injection is a fixture, never real host evidence.
"""

import asyncio
import json
import subprocess

import pytest

from api_helpers import RunningServer
from recommendation_helpers import setup
from test_asset_user_actions import ACTION_TOKEN, enable_fixture_action
from test_openclaw_host_transport import PACKAGE, REQUIRED, _missing, _node
from test_openclaw_plugin import driver_env


@pytest.fixture(autouse=True)
def node_dependencies():
    reason = _missing()
    if reason:
        if REQUIRED:
            pytest.fail(reason)
        pytest.skip(reason)


def test_dormant_client_routes_and_explicit_fixture_actions(tmp_path):
    """RM36/RM37/RM39: exact routes, replay/CAS, erasure and default zero-action denial."""
    app, service, runs, _ = setup(tmp_path)
    enable_fixture_action(app, service, runs)
    asyncio.run(service.refresh_catalog())
    output = tmp_path / "commands.json"
    try:
        with RunningServer(app) as running:
            env = driver_env(running.base_url, app.state.application_token)
            env["RELAYMIND_ACTION_TOKEN"] = ACTION_TOKEN
            process = subprocess.run(
                [
                    _node(),
                    str(PACKAGE / "test/recommendation-transport/drive-recommendations.mjs"),
                    str(output),
                ],
                cwd=PACKAGE,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        raw = output.read_text()
        assert ACTION_TOKEN not in raw + process.stdout + process.stderr
        assert app.state.application_token not in raw + process.stdout + process.stderr
        assert process.returncode == 0, raw + process.stderr
        result = json.loads(raw)
        assert result["ready"]["state"] == "ready"
        assert result["created"] == result["replay"] == result["get"]
        assert result["created"]["state_version"] == "1"
        assert result["deny"] == {"ok": False, "code": "host_adapter_unavailable"}
        assert result["denyRequests"] == 0
        assert result["ordinaryAction"]["code"] == "user_action_required"
        assert result["stale"]["code"] == "stale_version"
        assert result["chosen"]["label"] == "preference-only"
        assert result["chosen"]["receipt"] == result["choiceReplay"]
        assert result["choiceReplay"]["state_version"] == "2"
        assert result["conflict"]["code"] == "idempotency_conflict"
        assert result["feedback"] == result["feedbackReplay"]
        assert result["page"]["records"][0]["feedback"] == {"kind": "usefulness", "value": "useful"}
        assert result["deleted"] == result["deleteReplay"]
        assert result["reset"]["feedback_revision"] == "3"
        assert result["current"]["state_version"] == "2"
        assert result["current"]["feedback_revision"] == "3"
        events = result["finalEvents"]["events"]
        assert sum(e["event_type"] == "chosen" for e in events) == 1
        assert sum(e["event_type"] == "feedback.recorded" for e in events) == 1
        assert next(e for e in events if e["event_type"] == "feedback.recorded")["erased"]
        assert result["runtime"]["code"] == "recommendation_unavailable"
        assert result["correction"]["label"] == "feedback"
        assert result["correction"]["receipt"]["feedback_revision"] == "4"
        assert result["rejection"]["feedback_revision"] == "5"
        assert result["firstPage"]["label"] == "privacy"
        assert result["firstPage"]["receipt"]["next_after_id"] is not None
        assert result["firstPage"]["receipt"]["records"][0]["feedback"]["kind"] == "correction"
        assert result["nextPage"]["next_after_id"] is None
        assert result["nextPage"]["records"][0]["feedback"]["kind"] == "rejection"
        assert all(e["path"].startswith("/relay/v1/") for e in result["exchanges"])
        assert not any(e["action"] for e in result["exchanges"] if e["path"].endswith("/events"))
        assert all(e["action"] for e in result["exchanges"] if "/feedback" in e["path"])
    finally:
        asyncio.run(service.stop())
        runs.close()


def test_real_client_abort_cancels_owned_create_without_retry(tmp_path):
    """RM36/AC27: abort crosses the socket and records exactly one cancelled lifecycle."""
    import time

    from relaymind.assets.settings import RecommendationSettings
    from test_recommendation_races import Gate

    gate = Gate()
    settings = RecommendationSettings(
        enabled=True, user_id="u", workspace_id="w", total_ms=5000, primary_ms=4500
    )
    app, service, runs, _ = setup(tmp_path, primary=gate, settings=settings)
    asyncio.run(service.refresh_catalog())
    output = tmp_path / "abort.json"
    try:
        with RunningServer(app) as running:
            process = subprocess.run(
                [
                    _node(),
                    str(PACKAGE / "test/recommendation-transport/abort-recommendation.mjs"),
                    str(output),
                ],
                cwd=PACKAGE,
                env=driver_env(running.base_url, app.state.application_token),
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert process.returncode == 0, process.stderr
            assert json.loads(output.read_text()) == {"code": "cancelled", "calls": 1}
            deadline = time.monotonic() + 5
            while True:
                with runs.sql_read() as db:
                    rows = list(db.execute("SELECT state FROM recommendations"))
                    if rows and rows[0][0] == "cancelled":
                        events = [
                            json.loads(row[0])
                            for row in db.execute("SELECT event_json FROM recommendation_events")
                        ]
                        break
                assert time.monotonic() < deadline, "create was not cancelled after the client left"
                time.sleep(0.01)
            assert len(rows) == len(gate.calls) == 1
            assert sum(e["event_type"] == "cancelled" for e in events) == 1
            assert events[-1]["payload"]["reason"] == "disconnect"
    finally:
        asyncio.run(service.stop())
        runs.close()


def test_built_schema_copies_match_the_published_gateway_contract():
    """RM36/RM37: packed validators consume exactly the published Python schemas."""
    names = (
        "recommendation_view",
        "recommendation_result",
        "recommendation_event",
        "recommendation_readiness",
        "asset_record",
        "action_evidence",
        "feedback_record",
    )
    script = (
        "const names=" + json.dumps(names) + "; const result={}; for(const name of names){"
        "result[name]=(await import('./dist/recommendation-schemas/'+name+'.js')).default;}"
        " console.log(JSON.stringify(result));"
    )
    process = subprocess.run(
        [_node(), "--input-type=module", "-e", script],
        cwd=PACKAGE,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    actual = json.loads(process.stdout)
    for name in names:
        path = PACKAGE.parents[1] / "src/relaymind/store/schemas" / (name + ".v1.json")
        assert actual[name] == json.loads(path.read_text())


def test_ts_numeric_tokens_match_python_strict_integer_admission(tmp_path):
    """RM36/RM37, AC27/AC28: float lexical kind cannot pass integer/const admission."""
    from relaymind.store.recommendation_event import validate_view

    fixture = (PACKAGE / "test/fixtures/recommendation-view.v1.json").read_text()
    probes = []
    for field, original, tokens in (
        ("schema_version", "1", ("1.0", "1e0", "1.00000000000000001")),
        ("attempt", "1", ("1.0", "1e0", "1.00000000000000001")),
        ("calls", "0", ("0.0", "0e0", "1e-1000")),
    ):
        for token in tokens:
            # Root schema_version is last in this canonical fixture; mutate that
            # strict Python boundary, rather than a referenced asset schema.
            before, after = fixture.rsplit('"' + field + '": ' + original, 1)
            text = before + '"' + field + '": ' + token + after
            with pytest.raises(ValueError):
                validate_view(json.loads(text))
            probes.append(text)
    path = tmp_path / "numeric-probes.json"
    path.write_text(json.dumps(probes))
    script = (
        "import {readFileSync} from 'node:fs';"
        "import {decodeRecommendationView} from './dist/recommendation-contract.js';"
        "const values=JSON.parse(readFileSync(process.argv[1],'utf8'));"
        "console.log(JSON.stringify(values.map(text=>{"
        "try{decodeRecommendationView(text);return false;}catch{return true;}})));"
    )
    process = subprocess.run(
        [_node(), "--input-type=module", "-e", script, str(path)],
        cwd=PACKAGE,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == [True] * 9
