"""Bounded native OpenClaw configuration preflight (RM16/RM39/RM40/RM44).

This invokes the pinned validator, not a gateway JSON Schema approximation.
It does not authenticate captured evidence or establish qualified acceptance.
"""

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from relaymind.evaluation.asset_benchmark_io import CONFIG_BYTES

_PACKAGE = Path(__file__).resolve().parents[3] / "integrations/openclaw"
_VALIDATOR_FILE = "dist/validation-ongxOmrj.mjs"


def unavailable_verdict() -> dict:
    return dict(status="unavailable", validator=None, issue_path=None)


def validate_verdict(value: object) -> dict:
    """Reject malformed or extended checker output at the Python boundary."""
    if not isinstance(value, dict) or set(value) != {"status", "validator", "issue_path"}:
        raise ValueError("host_config_verdict")
    if value == unavailable_verdict():
        return value
    if value["status"] not in ("valid", "invalid"):
        raise ValueError("host_config_verdict")
    identity = value["validator"]
    if (
        not isinstance(identity, dict)
        or set(identity) != {"host_version", "validator_file", "validator_sha256"}
        or identity["host_version"] != "2026.10.7"
        or identity["validator_file"] != _VALIDATOR_FILE
        or not isinstance(identity["validator_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", identity["validator_sha256"]) is None
    ):
        raise ValueError("host_config_verdict")
    path = value["issue_path"]
    if value["status"] == "valid":
        if path is not None:
            raise ValueError("host_config_verdict")
    elif not isinstance(path, str) or len(path.encode("utf-8")) > 1024:
        raise ValueError("host_config_verdict")
    return value


def check_host_config(config: Mapping, *, host_root: Path | None = None) -> dict:
    """Check this exact preimage in Node, with a 15-second process deadline.

    Host discovery is relative to the installed integration, never the caller's
    working directory. Missing host/Node, timeouts and malformed output are
    unavailable. Child environment/state is isolated from operator secrets.
    """
    try:
        payload = json.dumps(config, ensure_ascii=True, allow_nan=False).encode("utf-8")
        if len(payload) > CONFIG_BYTES:
            return unavailable_verdict()
        with tempfile.TemporaryDirectory(prefix="relaymind-host-config-") as temporary:
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
