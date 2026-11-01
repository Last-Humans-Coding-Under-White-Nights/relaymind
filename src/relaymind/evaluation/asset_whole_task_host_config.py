"""Bounded native OpenClaw configuration preflight (RM16/RM39/RM40/RM44).

This invokes the pinned validator, not a gateway JSON Schema approximation.
It does not authenticate captured evidence or establish qualified acceptance.
Verdicts are closed: ``valid``/``invalid`` carry ``reason: null``;
``unavailable`` carries ``validator_unavailable`` (no host verdict) or
``plugin_schema_not_inspected`` (the host ran, but a referenced plugin's
schema could not be inspected, so the config cannot be called valid).
The validator identity is the pinned host version plus the SHA-256 of every
host entry point the checker imports; any other identity is rejected.
The input is the execution-effective preimage itself: the checker does not
resolve ``$include`` files or ``${VAR}`` substitutions from operator state.
"""

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from relaymind.evaluation.asset_benchmark_io import CONFIG_BYTES
from relaymind.evaluation.asset_trial_kit import PINNED_OPENCLAW

_PACKAGE = Path(__file__).resolve().parents[3] / "integrations/openclaw"
# Every pinned host entry point check-host-config.mjs imports, by file digest.
PINNED_VALIDATOR = {
    "host_version": PINNED_OPENCLAW,
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
# The packaged plugin files the host reads to discover RelayMind and its schema.
_PLUGIN_FILES = ("package.json", "openclaw.plugin.json", "dist")


def unavailable_verdict() -> dict:
    return dict(
        status="unavailable", validator=None, issue_path=None, reason="validator_unavailable"
    )


def validate_verdict(value: object) -> dict:
    """Reject malformed or extended checker output at the Python boundary."""
    if not isinstance(value, dict) or set(value) != {"status", "validator", "issue_path", "reason"}:
        raise ValueError("host_config_verdict")
    if value == unavailable_verdict():
        return value
    expected_reason = "plugin_schema_not_inspected" if value["status"] == "unavailable" else None
    if (
        value["status"] not in ("valid", "invalid", "unavailable")
        or value["reason"] != expected_reason
    ):
        raise ValueError("host_config_verdict")
    if value["validator"] != PINNED_VALIDATOR:
        raise ValueError("host_config_verdict")
    path = value["issue_path"]
    if value["status"] == "valid":
        if path is not None:
            raise ValueError("host_config_verdict")
    elif not isinstance(path, str) or len(path.encode("utf-8")) > 1024:
        raise ValueError("host_config_verdict")
    return value


def check_host_config(
    config: object, *, host_root: Path | None = None, plugin_root: Path | None = None
) -> dict:
    """Check this exact preimage in Node, with a 15-second process deadline.

    Host discovery is relative to the installed integration, never the caller's
    working directory. Missing host/Node, timeouts and malformed output are
    unavailable. Child environment/state is isolated from operator secrets: a
    disposable state directory replaces HOME. When the config references the
    ``relaymind`` plugin, that state's only installed plugin is the packaged
    RelayMind plugin from ``plugin_root`` (default: this integration's built
    package; pass the unpacked release the run uses), so ordinary
    installed-plugin discovery validates its settings; an unbuilt package is
    unavailable. Any other non-bundled plugin named in ``plugins.entries`` or
    ``plugins.allow`` stays undiscovered and the verdict is unavailable; an
    undiscovered ``plugins.slots`` owner is a native error, and a
    ``plugins.deny`` name enables nothing to check. A non-object config gets
    the host's own verdict.
    """
    try:
        payload = json.dumps(config, ensure_ascii=True, allow_nan=False).encode("utf-8")
        if len(payload) > CONFIG_BYTES:
            return unavailable_verdict()
        plugin = (plugin_root or _PACKAGE) if _references_relaymind(config) else None
        if plugin is not None and not all(
            (plugin / name).is_file()
            for name in ("package.json", "openclaw.plugin.json", "dist/index.js")
        ):
            return unavailable_verdict()
        with tempfile.TemporaryDirectory(prefix="relaymind-host-config-") as temporary:
            if plugin is not None:
                _install_plugin(plugin, Path(temporary) / "extensions/relaymind")
            env = {
                key: value
                for key, value in os.environ.items()
                if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
            }
            env.update(
                HOME=temporary,
                USERPROFILE=temporary,
                OPENCLAW_STATE_DIR=temporary,
                OPENCLAW_CONFIG_PATH=str(Path(temporary) / "unused.json"),
            )
            result = subprocess.run(
                [
                    "node",
                    "--max-old-space-size=256",
                    str(_PACKAGE / "scripts/check-host-config.mjs"),
                    str(
                        (
                            host_root
                            if host_root is not None
                            else _PACKAGE / "node_modules/openclaw"
                        ).absolute()
                    ),
                ],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
                cwd=temporary,
                env=env,
                check=False,
            )
        if result.returncode != 0 or len(result.stdout) > 4096:
            return unavailable_verdict()
        return validate_verdict(json.loads(result.stdout))
    except (OSError, ValueError, TypeError, RecursionError, subprocess.TimeoutExpired):
        return unavailable_verdict()


def _install_plugin(source: Path, target: Path) -> None:
    """Copy only the packaged plugin files; nothing else from the source tree."""
    target.mkdir(parents=True)
    for name in _PLUGIN_FILES:
        path = source / name
        if path.is_dir():
            shutil.copytree(path, target / name, symlinks=False)
        elif path.is_file():
            shutil.copyfile(path, target / name)


def _references_relaymind(config: object) -> bool:
    """Whether plugins.entries/allow/deny/slots name the RelayMind plugin."""
    plugins = config.get("plugins") if isinstance(config, Mapping) else None
    if not isinstance(plugins, Mapping):
        return False
    names: list[object] = []
    for key in ("entries", "slots"):
        value = plugins.get(key)
        if isinstance(value, Mapping):
            names.extend(value if key == "entries" else value.values())
    for key in ("allow", "deny"):
        value = plugins.get(key)
        if isinstance(value, list):
            names.extend(value)
    return any(isinstance(name, str) and name.strip().lower() == "relaymind" for name in names)
