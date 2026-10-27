"""The OpenClaw provider plugin against the real gateway (RM08, RM12, RM19, RM26, RM33; AC24).

The Node driver ``integrations/openclaw/test/plugin-transport/drive-plugin.mjs``
plays the host around the plugin (#136): it registers the plugin entry,
runs the registered provider's catalog hook with the host credential, and
sends one request through the pinned openai-completions transport with
the model entry the catalog published, unchanged. The gateway runs over
scripted model doubles on a loopback socket (spec section 13).

Like ``tests/test_openclaw_host_transport.py`` these tests skip without
Node and ``npm ci`` in ``integrations/openclaw``; with
``RELAYMIND_OPENCLAW_NODE=1`` a missing dependency fails instead.
"""

import json
import os
import socket
import subprocess

import pytest

from api_helpers import RUN_ID_HEADER, RunningServer, make_app, token_of
from gateway_helpers import build_gateway, text_turn
from helpers import of_type
from relaymind.api.app import ChatSettings
from relaymind.api.chat import ClientToolsChat
from relaymind.orchestrator.local_status import LocalStatus
from relaymind.orchestrator.route_decision import ExecutionMode
from test_openclaw_host_transport import (
    PACKAGE,
    REQUIRED,
    _missing,
    _node,
    body_keys_outside_7_1,
)

DRIVER = PACKAGE / "test" / "plugin-transport" / "drive-plugin.mjs"
MODE_HEADER = "x-relaymind-execution-mode"
RECONNECT = "openclaw models auth paste-api-key --provider relaymind"


@pytest.fixture(autouse=True)
def node_dependencies():
    reason = _missing()
    if reason is not None:
        if REQUIRED:
            pytest.fail(f"RELAYMIND_OPENCLAW_NODE=1 but {reason}")
        pytest.skip(reason)


def drive_plugin(base_url, token, tmp_path, *, secrets=(), **scenario):
    """Run the plugin and the transport once; return the driver's JSON."""
    path = tmp_path / f"plugin-{len(list(tmp_path.glob('plugin-*.json')))}.json"
    scenario.setdefault("pluginConfig", {})
    path.write_text(json.dumps(scenario), "utf-8")
    env = {
        **os.environ,
        "RELAYMIND_BASE_URL": base_url,
        "RELAYMIND_APP_TOKEN": token,
        "NO_PROXY": "127.0.0.1,localhost",
    }
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    out = path.with_suffix(".out.json")
    completed = subprocess.run(
        [_node(), str(DRIVER), str(path), str(out)],
        cwd=PACKAGE,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    text = out.read_text("utf-8")
    # No credential in the plugin's logs, the captured exchanges, or the host output (RM12).
    for secret in (token, *secrets):
        assert secret not in text
        assert secret not in completed.stdout + completed.stderr
    return json.loads(text)


def client_gateway(tmp_path, turns=None, settings=None, **options):
    gateway = build_gateway(tmp_path, turns, **options)
    app = make_app(gateway, client_tools=ClientToolsChat(gateway.orchestrator), settings=settings)
    return gateway, app


def warnings(output):
    return [log["message"] for log in output["logs"] if log["level"] == "warn"]


def inference(output):
    """The exchanges of chat requests, after the catalog's discovery requests."""
    return [x for x in output["exchanges"] if x["request"]["path"].endswith("/chat/completions")]


def test_the_catalog_entry_passes_gateway_validation_for_the_default_streamed_request(tmp_path):
    """Section 7.5 done-when: the pinned host's request with the published entry is accepted."""
    gateway, app = client_gateway(tmp_path, [text_turn("Hello ", "there.")], external_turns=[])
    with RunningServer(app) as running:
        output = drive_plugin(running.base_url, token_of(app), tmp_path, prompt="Say hello.")
    catalog = output["catalog"]
    assert catalog["outcomes"] == [{"provider": "relaymind", "status": "ready"}]
    assert catalog["api"] == "openai-completions"
    assert catalog["base_url_is_configured"] is True
    assert catalog["api_key_is_the_host_credential"] is True
    (model,) = catalog["models"]
    assert model["id"] == "relaymind-client-tools"
    # No limit is configured, so none is advertised (section 7.5).
    assert "contextWindow" not in model and "maxTokens" not in model
    (exchange,) = inference(output)
    body = exchange["request"]["body"]
    assert body_keys_outside_7_1(body) == set()
    assert body["stream"] is True
    assert body["model"] == "relaymind-client-tools"
    assert exchange["request"]["headers"][MODE_HEADER] == "auto"
    assert exchange["request"]["headers"]["authorization"] == "<redacted>"
    assert exchange["response"]["status"] == 200
    assert output["results"] == [
        {"stop_reason": "stop", "text": "Hello there.", "error_message": None}
    ]
    run_id = exchange["response"]["headers"][RUN_ID_HEADER.lower()]
    (accepted,) = of_type(gateway.store.events(run_id), "run.accepted")
    assert accepted.payload["model_alias"] == "relaymind-client-tools"
    assert accepted.payload["requested_execution_mode"] == "auto"


def test_the_declared_limits_are_the_configured_ones_and_the_gateway_accepts_them(tmp_path):
    """Section 7.5: the window of auto is the external limit; the output limit is sent as is."""
    _, app = client_gateway(tmp_path, [text_turn("Hello.")], external_turns=[])
    limits = {
        "externalContextLimit": 128000,
        "localContextLimit": 8192,
        "maxCompletionTokens": 1024,
    }
    with RunningServer(app) as running:
        output = drive_plugin(
            running.base_url,
            token_of(app),
            tmp_path,
            prompt="Say hello.",
            pluginConfig={"limits": limits},
        )
    (model,) = output["catalog"]["models"]
    assert model["contextWindow"] == 128000
    assert model["maxTokens"] == 1024
    (exchange,) = inference(output)
    assert exchange["request"]["body"]["max_completion_tokens"] == 1024
    assert exchange["response"]["status"] == 200


def test_a_stale_token_is_401_with_the_reconnect_path_and_a_fresh_token_restores_access(
    tmp_path,
):
    """Section 7.5: a gateway restart generates a new token; the old one fails with 401."""
    first, before = client_gateway(tmp_path / "first", [text_turn("unused")], external_turns=[])
    stale = token_of(before)
    _, after = client_gateway(tmp_path / "second", [text_turn("Back again.")], external_turns=[])
    fresh = token_of(after)
    assert stale != fresh
    with RunningServer(after) as running:
        old = drive_plugin(running.base_url, stale, tmp_path, secrets=(fresh,), prompt="Say hello.")
        new = drive_plugin(running.base_url, fresh, tmp_path, secrets=(stale,), prompt="Say hello.")
    # The stale token: the catalog's first request is 401, nothing is published.
    (exchange,) = old["exchanges"]
    assert exchange["response"]["status"] == 401
    assert exchange["response"]["body"]["error"]["code"] == "unauthenticated"
    assert old["catalog"]["outcomes"] == [{"provider": "relaymind", "status": "auth-rejected"}]
    assert old["catalog"]["models"] == []
    (warning,) = warnings(old)
    assert "new application token every time it starts" in warning
    assert RECONNECT in warning
    assert old["results"] == []
    # The fresh token, stored by the operator, restores access.
    assert new["catalog"]["outcomes"] == [{"provider": "relaymind", "status": "ready"}]
    assert new["results"][0]["text"] == "Back again."
    assert first.adapter.requests == []


def test_an_unreachable_gateway_is_not_reported_as_an_auth_failure(tmp_path):
    """Section 7.5: catalog diagnostics tell an unavailable gateway from a rejected token."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Nothing listens on the port now.
    url = f"http://127.0.0.1:{port}/v1"
    output = drive_plugin(url, "rm-unused-token-000000000000000000", tmp_path, baseUrl=url)
    assert output["catalog"]["outcomes"] == [{"provider": "relaymind", "status": "unavailable"}]
    (warning,) = warnings(output)
    assert f"unreachable at {url}" in warning
    assert "token" not in warning.lower()


def test_a_gateway_without_client_owned_execution_publishes_no_model(tmp_path):
    """RM08, section 7.2: the plugin publishes only the alias the gateway serves."""
    gateway = build_gateway(tmp_path, [text_turn("unused")], external_turns=[])
    app = make_app(gateway)
    with RunningServer(app) as running:
        output = drive_plugin(running.base_url, token_of(app), tmp_path, prompt="Say hello.")
    assert output["catalog"]["models"] == []
    (warning,) = warnings(output)
    assert "client-owned execution is not configured on the gateway" in warning
    assert output["results"] == []
    assert gateway.adapter.requests == []


def test_an_external_only_setup_works_without_a_local_model(tmp_path):
    """RM26, section 7.5: no local model; /readyz names external_only ready; the run completes."""
    settings = ChatSettings(
        allowed_modes=frozenset({ExecutionMode.AUTO, ExecutionMode.EXTERNAL_ONLY})
    )
    gateway, app = client_gateway(
        tmp_path,
        [],
        settings=settings,
        local_status=LocalStatus.absent(),
        external_turns=[text_turn("External.")],
    )
    with RunningServer(app) as running:
        output = drive_plugin(
            running.base_url,
            token_of(app),
            tmp_path,
            prompt="Say hello.",
            pluginConfig={"executionMode": "external_only"},
        )
    assert output["catalog"]["outcomes"] == [{"provider": "relaymind", "status": "ready"}]
    assert warnings(output) == []
    (exchange,) = inference(output)
    assert exchange["request"]["headers"][MODE_HEADER] == "external_only"
    assert output["results"][0]["text"] == "External."
    run_id = exchange["response"]["headers"][RUN_ID_HEADER.lower()]
    events = gateway.store.events(run_id)
    (accepted,) = of_type(events, "run.accepted")
    assert accepted.payload["effective_execution_mode"] == "external_only"
    assert accepted.payload["local_model_status"] == "absent"
    assert gateway.adapter.requests == []
    assert len(gateway.external.requests) == 1


def test_local_only_outside_the_policy_is_named_and_refused(tmp_path):
    """Section 7.4, 7.5: the policy keeps local_only refused; the plugin never changes the mode."""
    settings = ChatSettings(
        allowed_modes=frozenset({ExecutionMode.AUTO, ExecutionMode.EXTERNAL_ONLY})
    )
    gateway, app = client_gateway(
        tmp_path,
        [],
        settings=settings,
        local_status=LocalStatus.absent(),
        external_turns=[text_turn("unused")],
    )
    with RunningServer(app) as running:
        output = drive_plugin(
            running.base_url,
            token_of(app),
            tmp_path,
            prompt="Say hello.",
            pluginConfig={"executionMode": "local_only"},
        )
    (warning,) = warnings(output)
    assert "does not permit local_only" in warning
    (exchange,) = inference(output)
    assert exchange["request"]["headers"][MODE_HEADER] == "local_only"
    assert exchange["response"]["status"] == 403
    assert exchange["response"]["body"]["error"]["code"] == "execution_mode_forbidden"
    assert output["results"][0]["stop_reason"] == "error"
    assert gateway.external.requests == []


def test_local_only_permitted_without_a_local_model_is_not_ready_and_refused(tmp_path):
    """RM26, section 7.4, 7.5: policy permits local_only, the local model is absent.

    /readyz names local_only not ready because the local model is absent,
    so the plugin diagnoses mode_not_ready (not a policy refusal), and the
    gateway refuses the request through the class of the local model
    (mode_refusal), never by changing the mode or calling the external model.
    """
    settings = ChatSettings(
        allowed_modes=frozenset(
            {ExecutionMode.AUTO, ExecutionMode.LOCAL_ONLY, ExecutionMode.EXTERNAL_ONLY}
        )
    )
    gateway, app = client_gateway(
        tmp_path,
        [],
        settings=settings,
        local_status=LocalStatus.absent(),
        external_turns=[text_turn("never")],
    )
    with RunningServer(app) as running:
        output = drive_plugin(
            running.base_url,
            token_of(app),
            tmp_path,
            prompt="Say hello.",
            pluginConfig={"executionMode": "local_only"},
        )
    (warning,) = warnings(output)
    assert "RelayMind mode local_only is not ready" in warning
    assert "absent" in warning
    assert "does not permit" not in warning
    (exchange,) = inference(output)
    assert exchange["request"]["headers"][MODE_HEADER] == "local_only"
    assert exchange["response"]["status"] == 403
    error = exchange["response"]["body"]["error"]
    assert error["code"] == "execution_mode_forbidden"
    assert error["message"] == LocalStatus.absent().refusal(ExecutionMode.LOCAL_ONLY)
    assert output["results"][0]["stop_reason"] == "error"
    assert gateway.adapter.requests == []
    assert gateway.external.requests == []


def test_local_only_makes_zero_external_calls(tmp_path):
    """RM01, RM26: local_only through the plugin stays local."""
    gateway, app = client_gateway(
        tmp_path, [text_turn("Local.")], external_turns=[text_turn("never")]
    )
    with RunningServer(app) as running:
        output = drive_plugin(
            running.base_url,
            token_of(app),
            tmp_path,
            prompt="Say hello.",
            pluginConfig={"executionMode": "local_only"},
        )
    (exchange,) = inference(output)
    assert exchange["request"]["headers"][MODE_HEADER] == "local_only"
    assert output["results"][0]["text"] == "Local."
    run_id = exchange["response"]["headers"][RUN_ID_HEADER.lower()]
    events = gateway.store.events(run_id)
    assert all(e.actor != "external" for e in of_type(events, "model.started"))
    assert gateway.external.requests == []


def test_parallel_demo_is_refused_before_anything_is_sent(tmp_path):
    """Section 7.5: parallel_demo is not a mode of this alias; it is refused, never changed."""
    gateway, app = client_gateway(tmp_path, [text_turn("unused")], external_turns=[])
    with RunningServer(app) as running:
        output = drive_plugin(
            running.base_url,
            token_of(app),
            tmp_path,
            prompt="Say hello.",
            pluginConfig={"executionMode": "parallel_demo"},
        )
    assert output["register_error"]["name"] == "RelayMindConfigError"
    assert "parallel_demo" in output["register_error"]["message"]
    assert output["exchanges"] == []
    assert gateway.adapter.requests == []
