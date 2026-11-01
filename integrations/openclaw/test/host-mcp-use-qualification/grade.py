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
from relaymind.store.trace_event import TRACE_EVENT_SCHEMA

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
STALE_CASES = (
    "schema_change",
    "server_removal",
    "session_reset",
    "arguments_changed",
)
# Each negative control has its own refusal. A stale-schema error is not
# evidence that a policy, identity, replay, cancellation or timeout control ran.
CASE_REJECTIONS = {
    "schema_change": ("STALE_BINDING", "final_io"),
    "server_removal": ("STALE_BINDING", "final_io"),
    "session_reset": ("STALE_BINDING", "final_io"),
    "arguments_changed": ("STALE_BINDING", "final_io"),
    "policy_denied": ("POLICY_DENIED", "before_dispatch"),
    "disconnected": ("DISCONNECTED", "before_dispatch"),
    "foreign_requester": ("UNAUTHORIZED", "before_dispatch"),
    "approval_replay": ("APPROVAL_REPLAY", "before_dispatch"),
    "cancelled": ("CANCELLED", "before_dispatch"),
    "timeout": ("TIMEOUT", "before_dispatch"),
    "choice_expired": ("CHOICE_EXPIRED", "before_dispatch"),
    "registration_revoked": ("REGISTRATION_REVOKED", "before_dispatch"),
}
NEGATIVE_CASES = tuple(CASE_REJECTIONS)
FIXTURE_FILES = (
    "qualify.py",
    "grade.py",
    "approval_ui.py",
    "probe.mjs",
    "server.mjs",
    "package.json",
    "openclaw.plugin.json",
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
        package / "dist/control-ui/control-ui.js",
        package / "dist/control-ui/recommendation-json.js",
    ]
    # Derive expected outputs from sources, never from a possibly absent dist/.
    for source in sorted((package / "src").rglob("*.ts")):
        if source.name.endswith(".d.ts"):
            # A declaration input has no emitted JavaScript; pin the source only.
            files.append(source)
            continue
        files.extend(
            [source, package / "dist" / source.relative_to(package / "src").with_suffix(".js")]
        )
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


def nested_trial_bound(original: dict) -> bool:
    """Require this tier's exact source bytes in the nested host environment."""
    try:
        environment = original["environment"]
        checkout = environment["checkout"]
        fixture = ROOT / "integrations/openclaw/test/host-mcp-use-qualification"
        required = {(fixture / name).relative_to(ROOT).as_posix() for name in FIXTURE_FILES}
        return (
            isinstance(checkout, str)
            and len(checkout) == 40
            and all(char in "0123456789abcdef" for char in checkout)
            and source_bytes_match(
                environment["fixture_plugin_sources"], root=ROOT, required=required
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


def installed_archive_matches(capture: dict) -> bool:
    """Bind the pre-launch artifact digest to the ordinary installation receipt."""
    try:
        digest = capture["built_archive_sha256"]
        return (
            isinstance(digest, str)
            and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest)
            and capture["ordinary_capture"]["ordinary_actions"]["installation"]["archive_sha256"]
            == digest
        )
    except (KeyError, TypeError):
        return False


def zero_gateway_execution(runs: object) -> bool:
    """Absence counts only in a nonempty observed trace, never a summary counter."""
    try:
        types = TRACE_EVENT_SCHEMA["properties"]["event_type"]["enum"]
        safe_types = {name for name in types if not name.startswith("tool.")}
        return (
            isinstance(runs, list)
            and bool(runs)
            and all(
                isinstance(run["events"], list)
                and bool(run["events"])
                and all(
                    isinstance(event, dict)
                    and event.get("event_type", event.get("type")) in safe_types
                    and ("type" not in event or event["type"] in safe_types)
                    for event in run["events"]
                )
                for run in runs
            )
        )
    except (KeyError, TypeError):
        return False


COLLIDING_SERVERS = ("alpha.one", "alpha?one")
INVENTORY_OUTCOMES = frozenset({"listed", "empty", "malformed", "none"})


def session_inventory_shows_collision(event: dict) -> bool:
    """One tools.effective projection lists both fixture servers' `append`.

    Each server has exactly one usable row with its original name, the exposed
    names differ, and both are in the session's listed tool names.
    """
    if (
        event.get("method") != "tools.effective"
        or event.get("status") != "ok"
        or event.get("inventory") != "listed"
    ):
        return False
    rows, names = event["mcp_tools"], event["tool_names"]
    exposed = []
    for server in COLLIDING_SERVERS:
        matches = [row for row in rows if row["server"] == server]
        if len(matches) != 1 or matches[0]["original"] != "append":
            return False
        if matches[0].get("denied_by_session") is not None:
            return False
        exposed.append(matches[0]["name"])
    return len(set(exposed)) == 2 and all(
        isinstance(name, str) and name in names for name in exposed
    )


def fixture_discovery_pass(capture: dict) -> bool:
    """Raw discovery is a prerequisite, never an AC35 exact-use proof term.

    The session inventory must actually show both colliding fixture tools; an
    empty, malformed or unlabelled tools.effective projection is not discovery.
    """
    try:
        value = capture["mcp_use"]
        events = value["server_events"]
        effective = [
            event for event in value["inventories"] if event.get("method") == "tools.effective"
        ]
        return (
            type(value["probe_exit_code"]) is int
            and value["probe_exit_code"] == 0
            and {event["server"] for event in events if event["kind"] == "list"}
            == set(COLLIDING_SERVERS)
            and all(event["kind"] in {"started", "list"} for event in events)
            and value["sandbox_files"] == {"alpha.one": [], "alpha?one": []}
            and all(
                event.get("inventory") in INVENTORY_OUTCOMES - {"malformed"} for event in effective
            )
            and any(session_inventory_shows_collision(event) for event in effective)
        )
    except (KeyError, TypeError, AttributeError):
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


def binding_matches(binding: dict, approval: dict) -> bool:
    """Every field is present and non-null; an empty arguments object is valid."""
    try:
        return all(
            key in binding and binding[key] is not None and approval.get(key) == binding[key]
            for key in BINDING
        )
    except (KeyError, TypeError, AttributeError):
        return False


def effect_matches(trial: dict, decision: str) -> bool:
    """Join server call/effect records; host-only identity comes from host_calls.

    This checks accounting, not the native origin or authority of host_calls.
    """
    try:
        if decision not in {"allow-once", "deny"}:
            return False
        binding = trial["binding"]
        approval = trial["approval"]
        if not binding_matches(binding, approval):
            return False
        driver = approval["driver"]
        if approval["decision"] != decision or driver not in {"automated_ui", "human"}:
            return False
        expected_ui = dict(
            approval_id=approval["id"],
            action=decision,
            driver=driver,
            observed=dict(id=approval["id"], session=binding["session"], detail=approval["detail"]),
        )
        if driver == "human":
            expected_ui["human_observed"] = True
        if trial["ui_events"] != [expected_ui]:
            return False
        before, pending, after = (trial[key] for key in ("before", "pending", "after"))
        if before != pending or set(before) != set(after) or len(before) != 2:
            return False
        server = binding["server"]
        calls = [event for event in trial["server_events"] if event["kind"] == "call"]
        effects = [event for event in trial["server_events"] if event["kind"] == "effect"]
        host_calls = trial["host_calls"]
        if decision == "deny":
            return (
                not calls
                and not effects
                and not host_calls
                and after == before
                and trial["tool_results"] == []
            )
        if len(calls) != 1 or len(effects) != 1 or len(host_calls) != 1:
            return False
        if any(host_calls[0][key] != binding[key] for key in BINDING):
            return False
        call, effect = calls[0], effects[0]
        if not call["call_id"] or effect["call_id"] != call["call_id"]:
            return False
        if any(
            call[key] != binding[key] or effect[key] != binding[key]
            for key in ("server", "original", "arguments")
        ):
            return False
        if effect["counter"] != len(before[server]) + 1:
            return False
        expected = {key: list(value) for key, value in before.items()}
        expected[server].append(binding["arguments"]["text"])
        return after == expected and trial["tool_results"] == [
            dict(binding, content=effect["result"])
        ]
    except (KeyError, TypeError, ValueError):
        return False


def negative_control_pass(case: dict) -> bool:
    """Require each control's own observed refusal and zero new calls."""
    try:
        code, phase = CASE_REJECTIONS[case["name"]]
        rejection = case["rejection"]
        return (
            case["status"] == "observed"
            and rejection.get("code") == code
            and rejection.get("phase") == phase
            and bool(case["before"])
            and case["before"] == case["after"]
            and not any(event["kind"] == "call" for event in case["server_events"])
        )
    except (KeyError, TypeError, AttributeError):
        return False


def stale_case_pass(case: dict) -> bool:
    """A schema validator refusal does not establish an authorization fence."""
    try:
        return case["name"] in STALE_CASES and negative_control_pass(case)
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
        checks["pinned_bytes"] = (
            json.loads(package.read_bytes())["version"] == "2026.10.7"
            and installed_archive_matches(capture)
            and source_bytes_match(
                capture.get("source_inventory"), root=ROOT, required=required_sources()
            )
        )
    except (KeyError, TypeError, ValueError, OSError):
        pass
    try:
        path = ROOT / "integrations/openclaw/test/host-ordinary-qualification/qualify.py"
        spec = importlib.util.spec_from_file_location("mcp_ordinary_evidence", path)
        ordinary = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ordinary)
        original = capture.get("ordinary_capture", {})
        bound = nested_trial_bound(original)
        checks["genuine_person_ingress"] = (
            bound and ordinary.BASE.person_module().person_composition_pass(original)
        )
        # This proves the existing direct-action registration only, not MCP use authority.
        checks["current_registered_scope"] = bound and ordinary.ordinary_pass(original)
        checks["zero_gateway_execution"] = zero_gateway_execution(capture.get("runs"))
    except (KeyError, TypeError, ValueError, OSError):
        pass
    return checks


def mcp_use_pass(capture: object) -> bool:
    """Functional exact use stays blocked until every raw native proof resolves."""
    return all(grade_capture(capture).values())
