"""RM16/RM39/RM40/RM44; AC31/AC35/AC36/AC37: native config preflight.

A schema-only substitute would miss strict refinements and plugin validation.
Native tests skip only when the integration dependencies have not been installed.
"""

import hashlib
import json
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


def check(config, **kwargs):
    from relaymind.evaluation.asset_whole_task_host_config import check_host_config

    return check_host_config(config, **kwargs)


@pytest.fixture
def native_host():
    if not (PACKAGE / "node_modules").exists():
        pytest.skip("npm ci --prefix integrations/openclaw required")
    return HOST


def test_missing_host_is_unavailable(tmp_path):
    assert check({}, host_root=tmp_path / "missing") == {
        "status": "unavailable",
        "validator": None,
        "issue_path": None,
    }


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
    ],
)
def test_native_validator_verdict_and_identity(native_host, config, status, path):
    result = check(config, host_root=native_host)
    assert set(result) == {"status", "validator", "issue_path"}
    assert (result["status"], result["issue_path"]) == (status, path)
    identity = result["validator"]
    assert set(identity) == {"host_version", "validator_file", "validator_sha256"}
    assert identity["host_version"] == "2026.10.7"
    assert (
        identity["validator_sha256"]
        == hashlib.sha256((native_host / identity["validator_file"]).read_bytes()).hexdigest()
    )
    assert "private-invalid-value" not in json.dumps(result)


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
        b'{"status":"valid","validator":null,"issue_path":null}',
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
                    "validator": {
                        "host_version": "2026.10.7",
                        "validator_file": "dist/validation-ongxOmrj.mjs",
                        "validator_sha256": "0" * 64,
                    },
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
        "validator": {
            "host_version": "2026.10.7",
            "validator_file": "dist/validation-ongxOmrj.mjs",
            "validator_sha256": "0" * 64,
        },
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
