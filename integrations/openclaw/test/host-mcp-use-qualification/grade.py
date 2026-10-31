"""RM39/AC35 evidence accounting; no supported exact-use resolver on this pin yet.

This tier can establish discovery and person ingress. It deliberately cannot
promote a name-only invocation, an App without a genuine view, or synthetic
unit-test records into a native immutable use receipt. Adding that resolver
requires observed raw native evidence and separate review.
"""

import hashlib
import importlib.util
import json
from pathlib import Path

from relaymind.canonical import canonical_digest

ROOT = Path(__file__).resolve().parents[4]
TERMS = (
    "pinned_bytes",
    "genuine_person_ingress",
    "current_registered_scope",
    "retained_explicit_choice",
    "exact_native_binding",
    "live_session_runtime_requester_binding",
    "policy_before_and_after_approval",
    "currentness_at_final_io",
    "native_approval_receipt",
    "intended_effect_and_result",
    "zero_gateway_execution",
    "negative_controls",
    "preserved_baseline",
)
BINDING = ("server", "original", "exposed", "schema_hash", "session", "requester", "arguments")
NEGATIVE_CASES = (
    "schema_change",
    "server_removal",
    "session_reset",
    "policy_denied",
    "disconnected",
    "foreign_requester",
    "approval_replay",
    "cancelled",
    "timeout",
    "arguments_changed",
    "choice_expired",
    "registration_revoked",
)


def required_sources() -> set[str]:
    """Fixed source families, including host, SDK, adapter and fixture bytes."""
    package = ROOT / "integrations/openclaw"
    files = [
        package / "package-lock.json",
        package / "node_modules/openclaw/package.json",
        package / "node_modules/openclaw/openclaw.mjs",
        package / "node_modules/openclaw/dist/plugin-sdk/gateway-method-runtime.js",
        package / "node_modules/openclaw/dist/plugin-sdk/agent-harness-runtime.js",
        package / "node_modules/@modelcontextprotocol/sdk/package.json",
        Path(__file__),
        Path(__file__).with_name("qualify.py"),
        Path(__file__).with_name("approval_ui.py"),
        *sorted(Path(__file__).parent.glob("*.mjs")),
        *sorted(Path(__file__).parent.glob("*.json")),
        *sorted((package / "dist").glob("*.js")),
    ]
    for pattern in ("agent-bundle-mcp-*.mjs", "mcp-app-*.mjs", "tools-effective-*.mjs"):
        files.extend(sorted((package / "node_modules/openclaw/dist").glob(pattern)))
    return {p.relative_to(ROOT).as_posix() for p in files}


def source_bytes_match(inventory: object, *, root: Path, required: set[str]) -> bool:
    """Reopen originals; a capture's hashes or pass flag alone are insufficient."""
    try:
        if not isinstance(inventory, list) or not required:
            return False
        paths = [row["path"] for row in inventory]
        if len(paths) != len(set(paths)) or not required.issubset(paths):
            return False
        for row in inventory:
            path = root / row["path"]
            if Path(row["path"]).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                return False
        return True
    except (KeyError, TypeError, ValueError, OSError):
        return False


def zero_gateway_execution(runs: object) -> bool:
    """Absence counts only in a nonempty observed trace, never a summary counter."""
    try:
        return bool(runs) and all(
            run["events"]
            and all(
                event["type"]
                not in {
                    "tool.started",
                    "tool.executed",
                    "tool.execution_started",
                    "tool.execution_completed",
                }
                for event in run["events"]
            )
            for run in runs
        )
    except (KeyError, TypeError):
        return False


def schema_observation_matches(observation: dict) -> bool:
    """Compare mapping and schema bytes; identity-preserving normalization only.

    Other normalization needs a reviewed source-bound resolver. This accounting
    helper does not establish an immutable native use receipt.
    """
    try:
        original, tool = observation["server_event"], observation["tool"]
        schema, raw = tool["schema"], tool["schema_json"]
        return (
            original["kind"] == "list"
            and bool(tool["exposed_name"])
            and original["server"] == tool["mcp"]["serverName"]
            and original["original"] == tool["mcp"]["toolName"]
            and isinstance(schema, dict)
            and original["schema"] == schema == json.loads(raw)
            and canonical_digest(schema) == tool["canonical_schema_hash"]
            and hashlib.sha256(raw.encode("utf-8")).hexdigest() == tool["schema_sha256"]
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def effect_matches(trial: dict, decision: str) -> bool:
    """Accounting only, not qualification of the origin of a native use receipt."""
    try:
        if decision not in {"allow-once", "deny"}:
            return False
        binding = trial["binding"]
        approval = trial["approval"]
        if any(not binding[key] or approval[key] != binding[key] for key in BINDING):
            return False
        if approval["decision"] != decision or approval["driver"] != "automated_ui":
            return False
        if trial["ui_events"] != [
            dict(approval_id=approval["id"], action=decision, driver="automated_ui")
        ]:
            return False
        before, pending, after = (trial[key] for key in ("before", "pending", "after"))
        if before != pending or set(before) != set(after) or len(before) != 2:
            return False
        server = binding["server"]
        calls = [event for event in trial["server_events"] if event["kind"] == "call"]
        if decision == "deny":
            return not calls and after == before and trial["tool_results"] == []
        if len(calls) != 1 or any(calls[0][key] != binding[key] for key in BINDING):
            return False
        if calls[0]["counter"] != len(before[server]) + 1:
            return False
        expected = {key: list(value) for key, value in before.items()}
        expected[server].append(binding["arguments"]["text"])
        return after == expected and trial["tool_results"] == [
            dict(binding, content=calls[0]["result"])
        ]
    except (KeyError, TypeError, ValueError):
        return False


def stale_case_pass(case: dict) -> bool:
    """A schema validator refusal does not establish an authorization fence."""
    try:
        return (
            case["name"] in NEGATIVE_CASES
            and case["status"] == "observed"
            and case["rejection"] == {"code": "STALE_BINDING", "phase": "final_io"}
            and bool(case["before"])
            and case["before"] == case["after"]
            and not any(event["kind"] == "call" for event in case["server_events"])
        )
    except (KeyError, TypeError):
        return False


def grade_capture(capture: object) -> dict[str, bool]:
    """Resolve supported proofs; leave unsupported exact-use terms false.

    Raw receipt resolvers for exact immutable use, policy/currentness at I/O,
    approval and baseline composition are intentionally absent. Neither caller
    booleans nor the accounting helpers above can substitute for those resolvers.
    """
    checks = dict.fromkeys(TERMS, False)
    if not isinstance(capture, dict):
        return checks
    try:
        package = ROOT / "integrations/openclaw/node_modules/openclaw/package.json"
        checks["pinned_bytes"] = json.loads(package.read_bytes())[
            "version"
        ] == "2026.10.7" and source_bytes_match(
            capture.get("source_inventory"), root=ROOT, required=required_sources()
        )
    except (KeyError, TypeError, ValueError, OSError):
        pass
    try:
        path = ROOT / "integrations/openclaw/test/host-ordinary-qualification/qualify.py"
        spec = importlib.util.spec_from_file_location("mcp_ordinary_evidence", path)
        ordinary = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ordinary)
        original = capture.get("ordinary_capture", {})
        checks["genuine_person_ingress"] = ordinary.BASE.person_module().person_composition_pass(
            original
        )
        # This proves the existing direct-action registration only, not MCP use authority.
        checks["current_registered_scope"] = ordinary.ordinary_pass(original)
        checks["zero_gateway_execution"] = zero_gateway_execution(capture.get("runs"))
    except (KeyError, TypeError, ValueError, OSError):
        pass
    return checks


def mcp_use_pass(capture: object) -> bool:
    """Functional exact use stays blocked until every raw native proof resolves."""
    return all(grade_capture(capture).values())
