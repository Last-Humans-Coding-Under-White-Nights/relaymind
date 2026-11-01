"""RM16/RM39/RM40/RM44; AC31/AC35/AC36/AC37: native config preflight.

A schema-only substitute would miss strict refinements and plugin validation.
Native tests skip only when the integration dependencies (and, for the
packaged RelayMind plugin, its built dist/) are missing; with
``RELAYMIND_OPENCLAW_NODE=1`` (the openclaw-plugin CI job) they fail instead.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_evidence import OfflineResolver
from relaymind.evaluation.asset_whole_task import WholeTaskBlocked, WholeTaskRejected
from relaymind.evaluation.asset_whole_task_inputs import resolve_whole_task_inputs
from test_asset_whole_task_inputs import Bundle

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "integrations/openclaw"
HOST = PACKAGE / "node_modules/openclaw"
CHECKER = PACKAGE / "scripts/check-host-config.mjs"
REQUIRED = os.environ.get("RELAYMIND_OPENCLAW_NODE") == "1"
UNAVAILABLE = {
    "status": "unavailable",
    "validator": None,
    "issue_path": None,
    "reason": "validator_unavailable",
}
RELAYMIND_URL = "http://127.0.0.1:8080/v1"
# Every pinned host entry point the checker imports, with its file digest.
PINNED_IDENTITY = {
    "host_version": "2026.10.7",
    "validator_files": {
        "dist/config-cli-validation-Dnu71lrA.mjs": (
            "6e3dbbcdab56f21053764642fbc17127574b52328e4d8fc01e9354fae025321c"
        ),
        "dist/config-state-ByKt1EBw.mjs": (
            "863d94fcfbba058d5dc439b3c6a6507eb152fe9d224e0015387cfab7dce6e79b"
        ),
        "dist/io.plugin-metadata-CyZLGhSx.mjs": (
            "6bf0142104107467f9c2619ab70e0545acff5383b3a2117a39fd4679c8d9a78d"
        ),
        "dist/plugin-metadata-snapshot-YmhmQlNq.mjs": (
            "2a03d6d904eaddae02f7b1ee0b342bef600535ea369b99e118bab29403e2ff8f"
        ),
        "dist/validation-ongxOmrj.mjs": (
            "a79aac9f01ef15e5abcc78205814fe38a3248cc2b22dd0f3e075600eee236889"
        ),
    },
}


def _assert_pinned_identity(identity, native_host):
    """The identity names every imported entry point and matches the install."""
    assert identity == PINNED_IDENTITY
    for name, digest in identity["validator_files"].items():
        assert hashlib.sha256((native_host / name).read_bytes()).hexdigest() == digest


def check(config, **kwargs):
    from relaymind.evaluation.asset_whole_task_host_config import check_host_config

    return check_host_config(config, **kwargs)


def _unmet(reason):
    if REQUIRED:
        pytest.fail(f"RELAYMIND_OPENCLAW_NODE=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture
def native_host():
    if not (PACKAGE / "node_modules").exists():
        _unmet("npm ci --prefix integrations/openclaw required")
    if not (PACKAGE / "dist/index.js").is_file():
        _unmet("npm run build --prefix integrations/openclaw required (packaged plugin)")
    return HOST


def test_missing_host_is_unavailable(tmp_path):
    assert check({}, host_root=tmp_path / "missing") == UNAVAILABLE


@pytest.mark.parametrize(
    ("config", "status", "path"),
    [
        ({"gateway": {"mode": "local", "port": 18789}}, "valid", None),
        ({"channels": {"feishu": {"enabled": False, "stickerSets": {"你好": {}}}}}, "valid", None),
        (
            {"channels": {"feishu": {"enabled": False, "stickerSets": {" bad": {}}}}},
            "invalid",
            "channels.feishu.stickerSets. bad",
        ),
        ({"gateway": {"port": "private-invalid-value"}}, "invalid", "gateway.port"),
        (
            {
                "plugins": {
                    "entries": {
                        "anthropic": {
                            "enabled": True,
                            "config": {"sessionCatalog": {"enabled": "no"}},
                        }
                    }
                }
            },
            "invalid",
            "plugins.entries.anthropic.config.sessionCatalog.enabled",
        ),
        # Normal installed-plugin discovery: no plugins.load.paths in the config.
        (
            {"plugins": {"entries": {"relaymind": {"enabled": True, "config": {"baseUrl": 123}}}}},
            "invalid",
            "plugins.entries.relaymind.config.baseUrl",
        ),
        (
            {
                "plugins": {
                    "entries": {
                        "relaymind": {"enabled": True, "config": {"baseUrl": RELAYMIND_URL}}
                    }
                }
            },
            "valid",
            None,
        ),
        # A plugins section that never names RelayMind does not install it.
        ({"plugins": {"entries": {"anthropic": {"enabled": True}}}}, "valid", None),
    ],
)
def test_native_validator_verdict_and_identity(native_host, config, status, path):
    result = check(config, host_root=native_host)
    assert set(result) == {"status", "validator", "issue_path", "reason"}
    assert (result["status"], result["issue_path"], result["reason"]) == (status, path, None)
    _assert_pinned_identity(result["validator"], native_host)
    assert "private-invalid-value" not in json.dumps(result)


def _assert_not_inspected(result, native_host, path):
    assert result["status"] == "unavailable"
    assert result["reason"] == "plugin_schema_not_inspected"
    assert result["issue_path"] == path
    _assert_pinned_identity(result["validator"], native_host)


def test_unknown_referenced_plugin_is_unavailable_not_valid(native_host):
    """A referenced plugin the host cannot discover has no inspected schema."""
    config = {"plugins": {"entries": {"nosuchplugin": {"enabled": True, "config": {"x": 1}}}}}
    _assert_not_inspected(
        check(config, host_root=native_host), native_host, "plugins.entries.nosuchplugin"
    )


def test_allowed_unknown_plugin_is_unavailable_not_valid(native_host):
    """RM39/RM44, AC35/AC37: an allow-listed plugin is enabled with its default
    config, which the host checks against the plugin's schema once discovered.
    An undiscovered allow-listed plugin therefore had no inspected schema."""
    config = {"plugins": {"allow": ["nosuchplugin"]}}
    _assert_not_inspected(check(config, host_root=native_host), native_host, "plugins.allow.0")


def test_denied_unknown_plugin_needs_no_schema(native_host):
    """A denied plugin is never enabled, so the host checks no config for it."""
    result = check({"plugins": {"deny": ["nosuchplugin"]}}, host_root=native_host)
    assert (result["status"], result["reason"]) == ("valid", None)


def test_non_object_config_gets_the_native_verdict(native_host):
    """RM16, AC37: a non-object preimage reaches the host, never a Python crash."""
    result = check([], host_root=native_host)
    assert (result["status"], result["issue_path"], result["reason"]) == ("invalid", "", None)


def test_non_object_config_never_raises(monkeypatch):
    """Malformed input is a verdict (here: the stubbed host's), not an exception."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(UNAVAILABLE).encode(), b""
        ),
    )
    assert check([]) == UNAVAILABLE
    assert check("text") == UNAVAILABLE


@pytest.mark.parametrize(
    "identity",
    [
        # A single-file identity does not pin the code that produced the verdict.
        {
            "host_version": "2026.10.7",
            "validator_file": "dist/validation-ongxOmrj.mjs",
            "validator_sha256": "0" * 64,
        },
        # Another build of the pinned version (or a modified install).
        {
            **PINNED_IDENTITY,
            "validator_files": {
                **PINNED_IDENTITY["validator_files"],
                "dist/config-state-ByKt1EBw.mjs": "0" * 64,
            },
        },
        # A missing imported entry point.
        {
            **PINNED_IDENTITY,
            "validator_files": {
                k: v
                for k, v in PINNED_IDENTITY["validator_files"].items()
                if k != "dist/io.plugin-metadata-CyZLGhSx.mjs"
            },
        },
        {**PINNED_IDENTITY, "host_version": "2026.10.8"},
    ],
)
def test_unpinned_validator_identity_is_rejected(tmp_path, identity):
    """RM16/RM44, AC37: captured verdicts must name the pinned validator files."""
    from relaymind.evaluation.asset_whole_task_host_config import validate_verdict

    verdict = {"status": "valid", "issue_path": None, "reason": None, "validator": identity}
    with pytest.raises(ValueError, match="host_config_verdict"):
        validate_verdict(verdict)
    config = {"gateway": {"mode": "local"}}
    bundle = Bundle(tmp_path, host_config=config)
    with pytest.raises(WholeTaskRejected, match="host_config_validation_verdict"):
        resolve_whole_task_inputs(
            "host",
            bundle.host_report(),
            bundle.root / "inputs.json",
            resolver=OfflineResolver(bundle.resolver),
            host_config_validation={canonical_digest(config): verdict},
        )


def test_uninspected_plugin_discovery_is_unavailable_not_valid(native_host, tmp_path):
    """The host skips plugin config checks when discovery preserves an owner."""
    plugin = tmp_path / "lab"
    plugin.mkdir()
    (plugin / "package.json").write_text(
        json.dumps(
            {
                "name": "lab",
                "version": "1.0.0",
                "type": "module",
                "openclaw": {"extensions": ["./index.js"], "compat": {"pluginApi": ">=2099.1.1"}},
            }
        )
    )
    (plugin / "index.js").write_text("export default {};\n")
    (plugin / "openclaw.plugin.json").write_text(
        json.dumps(
            {
                "id": "lab",
                "configSchema": {"type": "object", "properties": {"n": {"type": "number"}}},
            }
        )
    )
    config = {
        "plugins": {
            "load": {"paths": [str(plugin)]},
            "entries": {"lab": {"enabled": True, "config": {"n": "not-a-number"}}},
        }
    }
    result = check(config, host_root=native_host)
    assert result["status"] == "unavailable"
    assert result["reason"] == "plugin_schema_not_inspected"
    assert "not-a-number" not in json.dumps(result)


def test_child_state_is_isolated_but_carries_the_packaged_plugin(monkeypatch, tmp_path):
    """No operator HOME/state reaches Node; only the pinned plugin package does.

    The package is installed only for a config that references ``relaymind``:
    an installed provider plugin is enabled by default and would otherwise be
    validated against a native-arm config that never installs it.
    """
    plugin = tmp_path / "package"
    (plugin / "dist").mkdir(parents=True)
    (plugin / "package.json").write_text("{}")
    (plugin / "openclaw.plugin.json").write_text('{"id": "relaymind"}')
    (plugin / "dist/index.js").write_text("")
    (plugin / "secret.txt").write_text("private")
    monkeypatch.setenv("HOME", str(tmp_path / "operator-home"))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "operator-state"))
    monkeypatch.setenv("RELAYMIND_APP_TOKEN", "private-token")
    seen = {}

    def run(args, **kwargs):
        env = kwargs["env"]
        state = Path(env["OPENCLAW_STATE_DIR"])
        seen["env"] = env
        seen["files"] = sorted(
            str(p.relative_to(state))
            for p in state.joinpath("extensions").rglob("*")
            if p.is_file()
        )
        return subprocess.CompletedProcess(args, 0, json.dumps(UNAVAILABLE).encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert check({"plugins": {"entries": {"other": {}}}}, plugin_root=plugin) == UNAVAILABLE
    assert seen["files"] == []
    assert check({"plugins": {"allow": ["relaymind"]}}, plugin_root=plugin) == UNAVAILABLE
    env = seen["env"]
    assert env["HOME"] == env["OPENCLAW_STATE_DIR"]
    assert str(tmp_path) not in env["HOME"]
    assert "RELAYMIND_APP_TOKEN" not in env
    assert seen["files"] == [
        "extensions/relaymind/dist/index.js",
        "extensions/relaymind/openclaw.plugin.json",
        "extensions/relaymind/package.json",
    ]


def test_checker_rejects_oversize_input(native_host):
    result = subprocess.run(
        ["node", str(CHECKER), str(native_host)],
        input=b" " * (4 * 1024 * 1024 + 1),
        capture_output=True,
        timeout=20,
        check=True,
    )
    assert json.loads(result.stdout)["status"] == "unavailable"
    assert len(result.stdout) < 4096 and result.stderr == b""


def test_wrapper_timeout_is_unavailable(monkeypatch):
    def timeout(*args, **kwargs):
        assert 0 < kwargs["timeout"] <= 15
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    assert check({})["status"] == "unavailable"


@pytest.mark.parametrize(
    "stdout",
    [
        b"not-json",
        b'{"status":"valid"}',
        b'{"status":"valid","validator":null,"issue_path":null,"reason":null}',
        b'{"status":"unavailable","validator":null,"issue_path":null,"reason":"other"}',
        b'{"status":"unavailable","validator":null,"issue_path":null}',
        b"x" * 4097,
    ],
)
def test_wrapper_fails_closed_for_broken_checker(monkeypatch, stdout):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout, b""),
    )
    assert check({})["status"] == "unavailable"


def test_resolution_rejects_native_invalid_preimage(tmp_path, native_host):
    bundle = Bundle(tmp_path, host_config={"gateway": {"port": "invalid"}})
    report = bundle.host_report()
    with pytest.raises(WholeTaskRejected, match="host_config_invalid"):
        resolve_whole_task_inputs(
            "host",
            report,
            bundle.root / "inputs.json",
            resolver=OfflineResolver(bundle.resolver),
            host_config_checker=check,
        )


def test_resolution_records_identity_without_qualifying_evidence(tmp_path, native_host):
    config = {"gateway": {"mode": "local", "port": 18789}}
    bundle = Bundle(tmp_path, host_config=config)
    inputs = resolve_whole_task_inputs(
        "host",
        bundle.host_report(),
        bundle.root / "inputs.json",
        resolver=OfflineResolver(bundle.resolver),
        host_config_checker=check,
    )
    assert set(inputs.host_config_validation) == {canonical_digest(config)}
    verdict = inputs.host_config_validation[canonical_digest(config)]
    assert verdict["status"] == "valid"
    assert verdict["validator"]["host_version"] == "2026.10.7"
    assert "configuration_semantics" in inputs.unverified
    assert "qualified_evidence_review" in inputs.unverified


def test_resolution_blocks_unavailable_native_checker(tmp_path):
    bundle = Bundle(tmp_path, host_config={})
    with pytest.raises(WholeTaskBlocked, match="host_config_validator_unavailable"):
        resolve_whole_task_inputs(
            "host",
            bundle.host_report(),
            bundle.root / "inputs.json",
            resolver=OfflineResolver(bundle.resolver),
            host_config_checker=lambda config: check(config, host_root=tmp_path / "missing"),
        )


def test_unpinned_host_is_unavailable(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "openclaw", "version": "2026.10.8"}))
    assert check({}, host_root=tmp_path)["status"] == "unavailable"


def test_nonzero_checker_cannot_claim_valid(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            json.dumps(
                {
                    "status": "valid",
                    "issue_path": None,
                    "reason": None,
                    "validator": PINNED_IDENTITY,
                }
            ).encode(),
            b"",
        ),
    )
    assert check({})["status"] == "unavailable"


def test_offline_resolution_consumes_captured_verdict_without_node(tmp_path, monkeypatch):
    config = {"gateway": {"mode": "local"}}
    bundle = Bundle(tmp_path, host_config=config)
    verdict = {
        "status": "valid",
        "issue_path": None,
        "reason": None,
        "validator": PINNED_IDENTITY,
    }

    def no_node(*args, **kwargs):
        raise AssertionError("offline grading must not launch Node")

    monkeypatch.setattr(subprocess, "run", no_node)
    inputs = resolve_whole_task_inputs(
        "host",
        bundle.host_report(),
        bundle.root / "inputs.json",
        resolver=OfflineResolver(bundle.resolver),
        host_config_validation={canonical_digest(config): verdict},
    )
    assert inputs.host_config_validation == {canonical_digest(config): verdict}
    assert "qualified_evidence_review" in inputs.unverified


def test_captured_verdict_for_another_config_is_rejected(tmp_path):
    bundle = Bundle(tmp_path, host_config={})
    with pytest.raises(WholeTaskRejected, match="host_config_validation_binding"):
        resolve_whole_task_inputs(
            "host",
            bundle.host_report(),
            bundle.root / "inputs.json",
            resolver=OfflineResolver(bundle.resolver),
            host_config_validation={"0" * 64: {}},
        )


def test_unbuilt_plugin_package_is_unavailable_not_invalid(monkeypatch, tmp_path):
    """A missing packaged plugin is an environment gap, not a config verdict."""
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "openclaw.plugin.json").write_text('{"id": "relaymind"}')

    def no_node(*args, **kwargs):
        raise AssertionError("an incomplete plugin package must not reach Node")

    monkeypatch.setattr(subprocess, "run", no_node)
    config = {"plugins": {"entries": {"relaymind": {"config": {"baseUrl": "x"}}}}}
    assert check(config, plugin_root=tmp_path) == UNAVAILABLE


def test_captured_uninspected_plugin_verdict_blocks_resolution(tmp_path):
    config = {"plugins": {"entries": {"nosuchplugin": {}}}}
    bundle = Bundle(tmp_path, host_config=config)
    verdict = {
        "status": "unavailable",
        "issue_path": "plugins.entries.nosuchplugin",
        "reason": "plugin_schema_not_inspected",
        "validator": PINNED_IDENTITY,
    }
    with pytest.raises(WholeTaskBlocked, match="host_config_validator_unavailable"):
        resolve_whole_task_inputs(
            "host",
            bundle.host_report(),
            bundle.root / "inputs.json",
            resolver=OfflineResolver(bundle.resolver),
            host_config_validation={canonical_digest(config): verdict},
        )
