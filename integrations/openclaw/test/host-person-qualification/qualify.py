"""Bounded fixture person-ingress qualification; never production host authority.

Only an actual assigned private IPv4 interface is eligible. No public bind,
synthetic forwarding address, actor row, external account or human claim.
"""

import ipaddress
import json
import secrets
import subprocess
import time
from pathlib import Path

TOTAL_SECONDS = 570
HOST_VERSION = "2026.10.7"
AUTHCRUNCH_VERSION = "1.5.0"
AUTHCRUNCH_SHA256 = "86a1ed8446bca803149bdcf1335005b840033a17c41680a067d7de11dfbaeca9"
AUTHCRUNCH_BYTES = 31627615
PERSON_SCOPES = ("operator.read", "operator.write", "operator.approvals", "operator.questions")
AUTHCRUNCH_URL = (
    "https://github.com/authcrunch/authcrunch/releases/download/v1.5.0/"
    "authcrunch_1.5.0_linux_amd64.tar.gz"
)
STEPS = (
    "network_peer",
    "proxy_signature_controls",
    "account_a_login",
    "account_a_pairing",
    "account_a_session",
    "account_a_person_rpc",
    "account_b_login",
    "account_b_pairing",
    "account_b_session",
    "account_b_person_rpc",
    "cross_account_denied",
    "revocation_denied",
)
LOGIN_SUBPHASES = frozenset(
    {
        "navigation",
        "username_fill",
        "proceed_click",
        "password_fill",
        "authenticate_click",
        "control_ui_attachment",
        "peer_binding",
    }
)
PERSON_RPC_EVENT_REASONS = {
    "guard_denied": frozenset(
        {"admission_refused", "initial_authority_retired", "post_await_authority_retired"}
    ),
    "probe_failed": frozenset(
        {"fixture_hold_timeout", "fixture_pause_failed", "inventory_unavailable"}
    ),
}
_PRIVATE = tuple(
    ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _private_ipv4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return any(address in network for network in _PRIVATE)


def eligible_private_ipv4(interfaces: list[dict]) -> list[str]:
    """Use actual UP non-loopback interface records, never broad is_private."""
    addresses = set()
    for interface in interfaces:
        flags = interface.get("flags", [])
        if "UP" not in flags or "LOOPBACK" in flags:
            continue
        for value in interface.get("addr_info", []):
            address = value.get("local")
            if value.get("family") == "inet" and _private_ipv4(address):
                addresses.add(address)
    return sorted(addresses, key=ipaddress.IPv4Address)


def discover_network() -> list[str]:
    """Read only actual interface state; no interface setup or user IP override."""
    result = subprocess.run(["ip", "-j", "address"], capture_output=True, timeout=5, check=True)
    if len(result.stdout) > 1024 * 1024:
        raise ValueError("network_inventory_size")
    value = json.loads(result.stdout)
    if not isinstance(value, list):
        raise ValueError("network_inventory_invalid")
    return eligible_private_ipv4(value)


def unavailable_capture(reason: str) -> dict:
    """Retain the complete planned denominator when setup cannot start."""
    return dict(
        kind="openclaw-native-person-ingress",
        host_version=HOST_VERSION,
        fixture="disposable local identity-provider accounts; no human qualification",
        status="setup_unavailable",
        reason=reason,
        steps=[dict(name=name, status="not_run") for name in STEPS],
        login_progress=[],
        accounts=[],
        network=None,
        checks=dict(person_composition=False, production_adapter_qualified=False),
    )


def person_composition_pass(capture: dict) -> bool:
    """Grade correlated fixture mechanics, not caller assertions of authority."""
    try:
        if capture["status"] != "completed" or capture["host_version"] != HOST_VERSION:
            return False
        if capture.get("configured_session_policy") != person_role_config(list(PERSON_SCOPES)):
            return False
        if capture["checks"]["production_adapter_qualified"] is not False:
            return False
        if capture["steps"] != [dict(name=name, status="passed") for name in STEPS]:
            return False
        network = capture["network"]
        if (
            not _private_ipv4(network["bind_ip"])
            or network["peer_ip"] != network["bind_ip"]
            or network["upstream"] != "127.0.0.1"
        ):
            return False
        accounts = capture["accounts"]
        if len(accounts) != 2:
            return False
        events = capture["events"]
        by_id = {event["id"]: event for event in events}
        if len(by_id) != len(events):
            return False
        native_rpcs = [event for event in events if event.get("kind") == "native_rpc"]
        request_ids = [event["request_id"] for event in native_rpcs]
        if len(request_ids) != len(set(request_ids)) or any(
            event["id"] != "request:" + event["request_id"] for event in native_rpcs
        ):
            return False
        creates = [event for event in events if event.get("kind") == "session_created"]
        if len(creates) != len(accounts):
            return False
        profiles, sessions, keys, sockets, create_ids = set(), set(), set(), set(), set()
        for account in accounts:
            if any(
                account[field] is not True
                for field in ("login_verified", "native_device_paired", "scoped_rpc_admitted")
            ):
                return False
            if account["proxy_email"] != account["native_person_email"]:
                return False
            for field in (
                "profile_id",
                "session_key",
                "socket_id",
                "create_evidence_id",
                "connection_id",
                "native_session_id",
                "lifecycle_revision",
                "device_id",
                "login_evidence_id",
                "rpc_evidence_id",
            ):
                if not isinstance(account[field], str) or not account[field]:
                    return False
            login = by_id[account["login_evidence_id"]]
            rpc = by_id[account["rpc_evidence_id"]]
            if (
                login["kind"] != "authenticated_proxy"
                or login["email"] != account["proxy_email"]
                or login["peer_ip"] != network["peer_ip"]
            ):
                return False
            if rpc["kind"] != "person_rpc" or rpc["ok"] is not True:
                return False
            created = by_id[account["create_evidence_id"]]
            if (
                created["kind"] != "session_created"
                or created["id"] != created["request_id"]
                or created["ok"] is not True
                or any(
                    created[field] != account[target]
                    for field, target in {
                        "socket_id": "socket_id",
                        "account_email": "proxy_email",
                        "session_key": "session_key",
                        "session_id": "native_session_id",
                        "lifecycle_revision": "lifecycle_revision",
                    }.items()
                )
                or rpc["socket_id"] != account["socket_id"]
            ):
                return False
            for request_id, method in (
                (created["request_id"], "sessions.create"),
                (account["rpc_evidence_id"], "rmqualification.person"),
            ):
                transport = by_id["request:" + request_id]
                if (
                    transport["kind"] != "native_rpc"
                    or transport["method"] != method
                    or transport["request_id"] != request_id
                    or transport["ok"] is not True
                    or transport["socket_id"] != account["socket_id"]
                ):
                    return False
            native = rpc["payload"]
            if native["authenticatedControlUi"] is not True:
                return False
            bindings = {
                "requestId": "rpc_evidence_id",
                "profileId": "profile_id",
                "authenticatedUserId": "native_person_email",
                "sessionKey": "session_key",
                "sessionId": "native_session_id",
                "lifecycleRevision": "lifecycle_revision",
                "deviceId": "device_id",
                "connectionId": "connection_id",
            }
            if any(native[source] != account[target] for source, target in bindings.items()):
                return False
            keys.add(account["session_key"])
            sockets.add(account["socket_id"])
            create_ids.add(account["create_evidence_id"])
            profiles.add(account["profile_id"])
            sessions.add((account["native_session_id"], account["lifecycle_revision"]))
        crosses = [event for event in events if event.get("kind") == "cross_account_denied"]
        retired = [event for event in events if event.get("kind") == "connection_revocation"]
        if len(crosses) != 1 or len(retired) != 1:
            return False
        cross, retirement = crosses[0], retired[0]
        if (
            cross["caller_connection_id"] != accounts[1]["connection_id"]
            or cross["target_session_key"] != accounts[0]["session_key"]
            or cross["target_lifecycle_revision"] != accounts[0]["lifecycle_revision"]
            or cross["target_session_id"] != accounts[0]["native_session_id"]
            or cross["ok"] is not False
            or cross["error_code"] not in {"FORBIDDEN", "INVALID_REQUEST", "NOT_FOUND"}
        ):
            return False
        prepared, observed_close, denied = (
            retirement["prepared"],
            retirement["retired"],
            retirement["denied"],
        )
        if (
            retirement["browser_context_closed"] is not True
            or retirement["native_sequence"] != ["prepared", "connection_retired", "guard_denied"]
            or prepared["phase"] != "prepared"
            or observed_close["phase"] != "connection_retired"
            or observed_close["connection_aborted"] is not True
            or denied["phase"] != "guard_denied"
            or observed_close["requestId"] != prepared["requestId"]
            or prepared["requestId"] != denied["requestId"]
            or denied["reason"] != "post_await_authority_retired"
            or observed_close["connectionId"] != prepared["connectionId"]
            or denied["connectionId"] != prepared["connectionId"]
            or prepared["connectionId"] != accounts[0]["connection_id"]
        ):
            return False
        return len(profiles) == len(sessions) == len(keys) == len(sockets) == len(create_ids) == 2
    except (KeyError, TypeError):
        return False


def person_role_config(scopes: list[str]) -> dict:
    """Use the host's named role to hide another person's native sessions."""
    return dict(
        default="person",
        definitions=dict(
            person=dict(sessions=dict(others="none"), agents=["main"], scopes=list(scopes))
        ),
    )


def session_create_receipt(observed: dict, account_email: str) -> dict:
    """Keep only nonsecret native create binding, never the unrestricted reply."""
    try:
        if observed["frame"]["ok"] is not True or observed["frame"]["id"] != observed["id"]:
            raise ValueError("native_session_create_binding")
        payload = observed["frame"]["payload"]
        receipt = dict(
            id=observed["id"],
            kind="session_created",
            request_id=observed["id"],
            socket_id=observed["socket_id"],
            account_email=account_email,
            ok=True,
            session_key=payload["key"],
            session_id=payload["sessionId"],
            lifecycle_revision=payload["entry"]["lifecycleRevision"],
        )
        if any(
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > 1024
            or any(ord(char) < 32 for char in value)
            for key, value in receipt.items()
            if key != "ok"
        ):
            raise ValueError("native_session_create_binding")
        return receipt
    except (KeyError, TypeError, UnicodeError):
        raise ValueError("native_session_create_binding") from None


def write_capture(path: Path, capture: dict) -> None:
    """Sanitized capture only; raw homes, stores, cookies and keys stay private."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(capture, ensure_ascii=True, indent=2) + "\n").encode("utf-8"))


def click_proceed(page) -> None:
    """Use the pinned portal's unique role control, whose name includes an icon."""
    page.get_by_role("button", name="Proceed").click()


def observe_login_subphase(capture: dict, account: str, subphase: str, action, *args, **kwargs):
    """Keep only fixed login progress and elapsed time; never page or error data."""
    if account not in {"account_a", "account_b"} or subphase not in LOGIN_SUBPHASES:
        raise ValueError("login_subphase_invalid")
    progress = capture["login_progress"]
    if len(progress) >= 2 * len(LOGIN_SUBPHASES):
        raise ValueError("login_subphase_bound")
    started = time.monotonic()
    status = "passed"
    try:
        return action(*args, **kwargs)
    except BaseException:
        status = "failed"
        raise
    finally:
        progress.append(
            dict(
                account=account,
                subphase=subphase,
                status=status,
                observed_ms=max((time.monotonic() - started) * 1000, 0.0),
            )
        )


def project_person_rpc_failure(path: Path, request_id: str) -> dict:
    """Project one native request's fixed event classes from a bounded private file."""
    empty = {"observations": []}
    if not isinstance(request_id, str) or not 0 < len(request_id) <= 128:
        return dict(status="event_file_invalid", **empty)
    try:
        with path.open("rb") as source:
            raw = source.read(1024 * 1024 + 1)
    except OSError:
        return dict(status="event_file_unavailable", **empty)
    if len(raw) > 1024 * 1024:
        return dict(status="event_file_oversize", **empty)
    observations = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return dict(status="event_file_invalid", **empty)
        if not isinstance(event, dict):
            return dict(status="event_file_invalid", **empty)
        if event.get("requestId") != request_id:
            continue
        phase, reason = event.get("phase"), event.get("reason")
        if not isinstance(phase, str) or (reason is not None and not isinstance(reason, str)):
            return dict(status="event_file_invalid", **empty)
        if phase in {"prepared", "admitted"} and reason is None:
            observation = {"phase": phase, "reason": None}
        elif phase in PERSON_RPC_EVENT_REASONS and reason in PERSON_RPC_EVENT_REASONS[phase]:
            observation = {"phase": phase, "reason": reason}
        else:
            return dict(status="event_file_invalid", **empty)
        observations.append(observation)
        if len(observations) > 4:
            return dict(status="event_file_invalid", **empty)
    return {
        "status": "handler_event_present" if observations else "handler_event_absent",
        "observations": observations,
    }


def project_held_native_events(path: Path, request_id: str) -> dict:
    """Retain only fixed facts from the bound held request before fixture cleanup."""
    empty = {"observations": []}
    if not isinstance(request_id, str) or not 0 < len(request_id) <= 128:
        return dict(status="event_file_invalid", **empty)
    try:
        with path.open("rb") as source:
            raw = source.read(1024 * 1024 + 1)
    except OSError:
        return dict(status="event_file_unavailable", **empty)
    if len(raw) > 1024 * 1024:
        return dict(status="event_file_oversize", **empty)
    observations = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return dict(status="event_file_invalid", **empty)
        if not isinstance(event, dict):
            return dict(status="event_file_invalid", **empty)
        if event.get("requestId") != request_id:
            continue
        connection_id = event.get("connectionId")
        if not isinstance(connection_id, str) or not 0 < len(connection_id) <= 128:
            return dict(status="event_file_invalid", **empty)
        phase, reason = event.get("phase"), event.get("reason")
        if phase in {"prepared", "admitted"} and reason is None:
            observation = {"phase": phase, "reason": None}
        elif (
            phase == "connection_retired"
            and reason is None
            and type(event.get("connection_aborted")) is bool
        ):
            observation = {"phase": phase, "connection_aborted": event["connection_aborted"]}
        elif phase in PERSON_RPC_EVENT_REASONS and reason in PERSON_RPC_EVENT_REASONS[phase]:
            observation = {"phase": phase, "reason": reason}
        else:
            return dict(status="event_file_invalid", **empty)
        observations.append(observation)
        if len(observations) > 8:
            return dict(status="event_file_invalid", **empty)
    return {
        "status": "handler_event_present" if observations else "handler_event_absent",
        "observations": observations,
    }


def read_bound_peer(page, address: str, email: str) -> dict:
    peer = page.evaluate(
        """async () => {
            const response = await fetch('/__qualification/peer');
            if (!response.ok) throw new Error('peer_denied');
            return response.json();
        }"""
    )
    if peer.get("peer") != address or peer.get("email") != email:
        raise ValueError("actual_peer_binding")
    return peer


def install_authcrunch(archive: Path, destination: Path) -> Path:
    """Extract only the pinned executable; no arbitrary archive path is used."""
    import hashlib
    import os
    import tarfile

    if archive.stat().st_size != AUTHCRUNCH_BYTES:
        raise ValueError("artifact_digest")
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != AUTHCRUNCH_SHA256:
        raise ValueError("artifact_digest")
    with tarfile.open(archive, "r:gz") as package:
        members = package.getmembers()
        if len(members) > 1000:
            raise ValueError("artifact_members")
        selected = [item for item in members if item.name in ("bin/authcrunch", "./bin/authcrunch")]
        if len(selected) != 1 or not selected[0].isfile() or selected[0].size > 128 * 1024 * 1024:
            raise ValueError("artifact_executable")
        if destination.is_symlink():
            raise ValueError("artifact_path")
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        binary = destination / "authcrunch"
        fd = os.open(binary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        count = 0
        with os.fdopen(fd, "wb") as target, package.extractfile(selected[0]) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                count += len(chunk)
                if count > selected[0].size:
                    raise ValueError("artifact_expansion")
                target.write(chunk)
        if count != selected[0].size:
            raise ValueError("artifact_expansion")
    return binary


def proxy_configuration(
    bind_ip: str,
    proxy_port: int,
    gateway_port: int,
    accounts: list[dict],
    signing_key: str,
    work: Path,
) -> str:
    """Supported local-store login, actual socket attribution, fixed loopback upstream."""
    import re

    if not _private_ipv4(bind_ip):
        raise ValueError("private_network_required")
    if any(
        type(port) is not int or not 1024 <= port <= 65535 for port in (proxy_port, gateway_port)
    ):
        raise ValueError("qualification_port")
    if len(accounts) != 2:
        raise ValueError("qualification_accounts")
    users = []
    for account in accounts:
        if not re.fullmatch(r"[a-z][a-z0-9]{0,31}", account["username"]) or not re.fullmatch(
            r"[a-z0-9]+@example\.test", account["email"]
        ):
            raise ValueError("qualification_accounts")
        users.append(
            "user "
            + account["username"]
            + " {\n email "
            + account["email"]
            + "\n password "
            + json.dumps(account["password"])
            + "\n roles authp/user\n }"
        )
    origin = f"https://rm-person.test:{proxy_port}"
    return "\n".join(
        [
            "{",
            "admin off",
            "persist_config off",
            "auto_https disable_redirects",
            "skip_install_trust",
            "storage file_system " + json.dumps(str(work / "caddy-data")),
            "security {",
            "local identity store fixtures {",
            "realm local",
            "path :memory:",
            *users,
            "}",
            "authentication portal personportal {",
            "enable identity store fixtures",
            "crypto default token lifetime 300",
            "crypto key sign-verify " + json.dumps(signing_key),
            "cookie guess domain enabled",
            "cookie prefix RMPERSON",
            f"trust login redirect uri domain exact rm-person.test:{proxy_port} path prefix /",
            "}",
            "authorization policy personpolicy {",
            "set auth url " + origin + "/auth/login",
            "crypto key verify " + json.dumps(signing_key),
            "allow roles authp/user",
            "set session_id cookie name RMPERSON_SESSION_ID",
            "set access_token cookie name RMPERSON_ACCESS_TOKEN",
            "}",
            "}",
            "}",
            origin + " {",
            "bind " + bind_ip,
            "tls internal",
            'route /healthz {\n respond "fixture proxy ready" 200\n }',
            "route /auth* {\n redir /auth /auth/ 308\n authenticate with personportal\n }",
            "route /__qualification/peer {",
            "authorize with personpolicy",
            "header Content-Type application/json",
            'respond `{"peer":"{http.request.remote.host}","email":"{http.auth.user.email}"}` 200',
            "}",
            "route /* {",
            "authorize with personpolicy",
            f"reverse_proxy 127.0.0.1:{gateway_port} {{",
            "header_up -Authorization",
            "header_up -Cookie",
            "header_up -Forwarded",
            "header_up -X-Real-IP",
            "header_up X-Forwarded-User {http.auth.user.email}",
            "header_up X-Forwarded-For {http.request.remote.host}",
            "header_up X-Forwarded-Proto https",
            "header_up X-Forwarded-Host {http.request.host}",
            "}",
            "}",
            "}",
            "",
        ]
    )


def download_authcrunch(path: Path) -> None:
    """Fetch one fixed release with finite bounds; no ambient proxy/auth headers."""
    import time
    import urllib.request
    from urllib.parse import urlsplit

    class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            url = urlsplit(newurl)
            if (
                url.scheme != "https"
                or url.hostname
                not in {
                    "github.com",
                    "release-assets.githubusercontent.com",
                    "objects.githubusercontent.com",
                }
                or url.username
                or url.password
            ):
                raise ValueError("artifact_redirect")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), ReleaseRedirect())
    deadline = time.monotonic() + 45
    count = 0
    try:
        with opener.open(AUTHCRUNCH_URL, timeout=10) as response, path.open("xb") as target:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("artifact_deadline")
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > AUTHCRUNCH_BYTES:
                    raise ValueError("artifact_size")
                target.write(chunk)
        if count != AUTHCRUNCH_BYTES:
            raise ValueError("artifact_size")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _free_port(address: str) -> int:
    import socket

    with socket.socket() as listener:
        listener.bind((address, 0))
        return listener.getsockname()[1]


def _distinct_ports(address: str, attempts: int = 16) -> tuple[int, int]:
    """Gateway and proxy ports differ, so a check of one never reaches the other."""
    gateway_port = _free_port("127.0.0.1")
    for _ in range(attempts):
        proxy_port = _free_port(address)
        if proxy_port != gateway_port:
            return gateway_port, proxy_port
    raise ValueError("qualification_port")


def _unreachable(address: str, port: int) -> bool:
    """A refused, dropped or filtered connection means nothing listens for us there."""
    import socket

    try:
        with socket.create_connection((address, port), timeout=0.2):
            return False
    except OSError:
        return True


def _close_contexts(contexts: list) -> None:
    """Close every browser context; one failed close does not keep the others open."""
    for context in contexts:
        try:
            context.close()
        except Exception:
            pass


def read_native_events(path: Path) -> list[dict]:
    """Complete JSONL events only; a trailing line still being appended is retried later."""
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("native_event_bound")
    complete, _, _partial = raw.rpartition(b"\n")
    events = []
    for line in complete.split(b"\n"):
        if not line:
            continue
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("native_event_invalid") from None
        if not isinstance(event, dict):
            raise ValueError("native_event_invalid")
        events.append(event)
    return events


def _wait_listener(process, address: str, port: int) -> None:
    import socket
    import time

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ValueError("child_startup_failed")
        try:
            with socket.create_connection((address, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("child_startup_timeout")


def _run_private_trial(work: Path, address: str, capture: dict) -> None:
    import sys
    import time
    import uuid

    from playwright.sync_api import sync_playwright

    root = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(root / "scripts"), str(root / "src")]
    from openclaw_install_check import host_environment

    if not _private_ipv4(address):
        raise ValueError("private_network_required")
    capture["phase"] = "artifact_setup"
    archive = work / "authcrunch.tar.gz"
    download_authcrunch(archive)
    binary = install_authcrunch(archive, work / "proxy-bin")
    gateway_port, proxy_port = _distinct_ports(address)
    origin = f"https://rm-person.test:{proxy_port}"
    nonce = secrets.token_hex(6)
    accounts = [
        dict(
            username=name, email=name + nonce + "@example.test", password=secrets.token_urlsafe(32)
        )
        for name in ("accounta", "accountb")
    ]
    signing_key = secrets.token_urlsafe(48)
    secrets_to_exclude = [signing_key, *(account["password"] for account in accounts)]
    for name in ("home", "state", "sandbox"):
        (work / name).mkdir(mode=0o700)
    events_path, release_path = work / "native-events.jsonl", work / "release-probe"
    events_path.write_bytes(b"")
    events_path.chmod(0o600)
    plugin = Path(__file__).resolve().parent
    scopes = list(PERSON_SCOPES)
    config = dict(
        gateway=dict(
            mode="local",
            bind="loopback",
            port=gateway_port,
            roles=person_role_config(scopes),
            publicOrigin=origin,
            trustedProxies=["127.0.0.1"],
            controlUi=dict(allowedOrigins=[origin]),
            auth=dict(
                mode="trusted-proxy",
                identityScopes={account["email"]: scopes for account in accounts},
                trustedProxy=dict(
                    userHeader="x-forwarded-user",
                    requiredHeaders=["x-forwarded-proto", "x-forwarded-host"],
                    allowUsers=[account["email"] for account in accounts],
                    allowLoopback=True,
                    deviceAutoApprove=dict(enabled=True, scopes=scopes),
                ),
            ),
        ),
        agents=dict(defaults=dict(workspace=str(work / "sandbox"))),
        plugins=dict(
            allow=["relaymind-person-qualification"],
            load=dict(paths=[str(plugin)]),
            entries={
                "relaymind-person-qualification": dict(
                    enabled=True,
                    config=dict(
                        origin=origin, eventFile=str(events_path), releaseFile=str(release_path)
                    ),
                )
            },
        ),
    )
    config_path = work / "openclaw.json"
    config_path.write_bytes((json.dumps(config) + "\n").encode("utf-8"))
    config_path.chmod(0o600)
    capture["configured_session_policy"] = config["gateway"]["roles"]
    environment = host_environment(work)
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        "LD_LIBRARY_PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
    }
    environment = {
        name: value
        for name, value in environment.items()
        if name in allowed
        or name in {"OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH"}
    }
    environment.update(
        HOME=str(work / "home"),
        XDG_CONFIG_HOME=str(work / "home/.config"),
        XDG_DATA_HOME=str(work / "home/.local/share"),
        NO_PROXY="127.0.0.1,localhost,::1," + address + ",rm-person.test",
    )
    host = ["node", str(root / "integrations/openclaw/node_modules/openclaw/openclaw.mjs")]
    actual_version = json.loads(
        (root / "integrations/openclaw/node_modules/openclaw/package.json").read_bytes()
    )["version"]
    if actual_version != HOST_VERSION:
        raise ValueError("native_host_version")
    caddyfile = work / "Caddyfile"
    caddyfile.write_bytes(
        proxy_configuration(address, proxy_port, gateway_port, accounts, signing_key, work).encode(
            "utf-8"
        )
    )
    caddyfile.chmod(0o600)
    capture["phase"] = "configuration_validation"
    with (work / "private-setup.log").open("wb") as diagnostics:
        for command in (
            [str(binary), "adapt", "--adapter", "caddyfile", "--config", str(caddyfile)],
            host + ["config", "validate", "--json"],
        ):
            completed = subprocess.run(
                command,
                env=environment,
                cwd=work,
                stdout=subprocess.DEVNULL,
                stderr=diagnostics,
                timeout=30,
            )
            if completed.returncode != 0:
                raise ValueError("configuration_invalid")
    import hashlib

    source_paths = [
        plugin / "qualify.py",
        plugin / "probe.mjs",
        plugin / "browser-wire.mjs",
        plugin / "openclaw.plugin.json",
        plugin / "package.json",
        root / "integrations/openclaw/package-lock.json",
    ]
    source_inventory = []
    for source_path in sorted(source_paths):
        payload = source_path.read_bytes()
        source_inventory.append(
            dict(
                path=source_path.relative_to(root).as_posix(),
                bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    node_version = subprocess.run(
        ["node", "--version"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    ).stdout.strip()
    checkout = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    ).stdout.strip()
    capture["environment"] = dict(
        checkout=checkout,
        node_version=node_version,
        observed_host_version=actual_version,
        sdk_package_version=actual_version,
        fixture_plugin_sources=source_inventory,
    )
    capture["proxy"] = dict(release=AUTHCRUNCH_VERSION, sha256=AUTHCRUNCH_SHA256)
    capture["events"] = []
    processes, files = [], []
    held_request_id = None

    def mark(name):
        for step in capture["steps"]:
            if step["name"] == name:
                step["status"] = "passed"
                return
        raise ValueError("unknown_step")

    def native_events():
        return read_native_events(events_path)

    try:
        for label, command in (
            ("gateway", host + ["gateway", "run", "--compact"]),
            ("proxy", [str(binary), "run", "--config", str(caddyfile)]),
        ):
            log = (work / ("private-" + label + ".log")).open("wb")
            files.append(log)
            processes.append(
                subprocess.Popen(
                    command, env=environment, cwd=work, stdout=log, stderr=subprocess.STDOUT
                )
            )
        capture["phase"] = "listener_startup"
        _wait_listener(processes[0], "127.0.0.1", gateway_port)
        _wait_listener(processes[1], address, proxy_port)
        # The gateway must not become a direct private-interface ingress.
        if not _unreachable(address, gateway_port):
            raise ValueError("gateway_nonloopback_listener")
        # A wildcard proxy listener would also accept loopback; refuse it.
        if not _unreachable("127.0.0.1", proxy_port):
            raise ValueError("proxy_loopback_listener")
        capture["listeners"] = dict(
            gateway=dict(address="127.0.0.1", port=gateway_port),
            proxy=dict(address=address, port=proxy_port),
            gateway_private_refused=True,
            proxy_loopback_refused=True,
        )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                args=["--no-proxy-server", f"--host-resolver-rules=MAP rm-person.test {address}"],
                env=environment,
            )
            contexts = []
            try:
                capture["phase"] = "proxy_signature_controls"
                for forged in (False, True):
                    context = browser.new_context(ignore_https_errors=True)
                    try:
                        if forged:
                            context.set_extra_http_headers(
                                {"X-Forwarded-User": accounts[0]["email"]}
                            )
                            context.add_cookies(
                                [
                                    dict(
                                        name="RMPERSON_ACCESS_TOKEN",
                                        value="invalid-signature",
                                        url=origin,
                                    )
                                ]
                            )
                        page = context.new_page()
                        page.goto(origin + "/", wait_until="domcontentloaded", timeout=30000)
                        page.locator('input[name="username"]').wait_for(timeout=15000)
                        if not page.url.startswith(origin + "/auth"):
                            raise ValueError("unauthenticated_proxy_ingress")
                    finally:
                        context.close()
                mark("proxy_signature_controls")
                script = (plugin / "browser-wire.mjs").read_text(encoding="utf-8").replace(
                    "export function installNativeSocketObserver",
                    "function installNativeSocketObserver",
                    1,
                ) + "\ninstallNativeSocketObserver();\n"
                pages, sessions = [], []

                def rpc(page, method, params):
                    observed = page.evaluate(
                        "({method,params}) => window.__rmPersonWire.rpc(method, params)",
                        dict(method=method, params=params),
                    )
                    if observed["frame"].get("id") != observed["id"]:
                        raise ValueError("native_response_binding")
                    error = observed["frame"].get("error") or {}
                    error_code = error.get("code")
                    if (
                        not isinstance(error_code, str)
                        or not error_code.replace("_", "").isalnum()
                        or len(error_code) > 64
                    ):
                        error_code = None
                    capture["events"].append(
                        dict(
                            id="request:" + observed["id"],
                            kind="native_rpc",
                            method=method,
                            request_id=observed["id"],
                            socket_id=observed["socket_id"],
                            ok=observed["frame"].get("ok") is True,
                            error_code=error_code,
                        )
                    )
                    return observed

                for index, account in enumerate(accounts):
                    prefix = "account_a" if index == 0 else "account_b"
                    capture["phase"] = prefix + "_login"
                    context = browser.new_context(ignore_https_errors=True)
                    contexts.append(context)
                    context.add_init_script(script=script)
                    page = context.new_page()
                    pages.append(page)
                    observe_login_subphase(
                        capture,
                        prefix,
                        "navigation",
                        page.goto,
                        origin + "/",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    observe_login_subphase(
                        capture,
                        prefix,
                        "username_fill",
                        page.locator('input[name="username"]').fill,
                        account["username"],
                    )
                    observe_login_subphase(capture, prefix, "proceed_click", click_proceed, page)
                    observe_login_subphase(
                        capture,
                        prefix,
                        "password_fill",
                        page.locator('input[type="password"]').fill,
                        account["password"],
                    )
                    observe_login_subphase(
                        capture,
                        prefix,
                        "authenticate_click",
                        page.get_by_role("button", name="Authenticate", exact=True).click,
                    )
                    observe_login_subphase(
                        capture,
                        prefix,
                        "control_ui_attachment",
                        page.locator("openclaw-app").wait_for,
                        state="attached",
                        timeout=30000,
                    )
                    peer = observe_login_subphase(
                        capture,
                        prefix,
                        "peer_binding",
                        read_bound_peer,
                        page,
                        address,
                        account["email"],
                    )
                    capture["network"] = dict(
                        bind_ip=address, peer_ip=peer["peer"], upstream="127.0.0.1"
                    )
                    login_id = str(uuid.uuid4())
                    capture["events"].append(
                        dict(
                            id=login_id,
                            kind="authenticated_proxy",
                            email=peer["email"],
                            peer_ip=peer["peer"],
                        )
                    )
                    mark(prefix + "_login")
                    capture["phase"] = prefix + "_pairing"
                    deadline = time.monotonic() + 60
                    while not page.evaluate("() => window.__rmPersonWire?.state().ready === true"):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("native_ui_pairing_timeout")
                        time.sleep(0.05)
                    mark(prefix + "_pairing")
                    capture["phase"] = prefix + "_session"
                    created = rpc(page, "sessions.create", session_create_params())
                    if created["frame"].get("ok") is not True:
                        raise ValueError("native_session_create_denied")
                    session = created["frame"]["payload"]
                    params = dict(
                        sessionKey=session["key"],
                        expectedSessionId=session["sessionId"],
                        expectedLifecycleRevision=session["entry"]["lifecycleRevision"],
                    )
                    capture["events"].append(session_create_receipt(created, account["email"]))
                    sessions.append(params)
                    mark(prefix + "_session")
                    capture["phase"] = prefix + "_person_rpc"
                    result = rpc(page, "rmqualification.person", params)
                    if result["frame"].get("ok") is not True:
                        capture["person_rpc_failure"] = project_person_rpc_failure(
                            events_path, result["id"]
                        )
                        raise ValueError("native_person_rpc_denied")
                    receipt = result["frame"]["payload"]
                    if (
                        receipt["requestId"] != result["id"]
                        or receipt["authenticatedUserId"] != account["email"]
                    ):
                        raise ValueError("native_person_identity_binding")
                    capture["events"].append(
                        dict(
                            id=result["id"],
                            kind="person_rpc",
                            socket_id=result["socket_id"],
                            ok=True,
                            payload=receipt,
                        )
                    )
                    capture["accounts"].append(
                        dict(
                            login_verified=True,
                            native_device_paired=True,
                            scoped_rpc_admitted=True,
                            proxy_email=account["email"],
                            native_person_email=receipt["authenticatedUserId"],
                            profile_id=receipt["profileId"],
                            session_key=receipt["sessionKey"],
                            socket_id=result["socket_id"],
                            create_evidence_id=created["id"],
                            native_session_id=receipt["sessionId"],
                            lifecycle_revision=receipt["lifecycleRevision"],
                            device_id=receipt["deviceId"],
                            connection_id=receipt["connectionId"],
                            login_evidence_id=login_id,
                            rpc_evidence_id=result["id"],
                        )
                    )
                    mark(prefix + "_person_rpc")
                # Every account's actual TCP peer is bound before the step passes.
                mark("network_peer")
                capture["phase"] = "cross_account_denied"
                denied = rpc(pages[1], "rmqualification.person", sessions[0])
                denial_code = (denied["frame"].get("error") or {}).get("code")
                if denied["frame"].get("ok") is not False or denial_code not in {
                    "FORBIDDEN",
                    "INVALID_REQUEST",
                    "NOT_FOUND",
                }:
                    raise ValueError("cross_account_admitted")
                capture["events"].append(
                    dict(
                        id="cross:" + denied["id"],
                        kind="cross_account_denied",
                        caller_connection_id=capture["accounts"][1]["connection_id"],
                        target_session_key=capture["accounts"][0]["session_key"],
                        target_lifecycle_revision=capture["accounts"][0]["lifecycle_revision"],
                        target_session_id=capture["accounts"][0]["native_session_id"],
                        ok=False,
                        error_code=denial_code,
                    )
                )
                mark("cross_account_denied")
                capture["phase"] = "revocation_denied"
                request_id = pages[0].evaluate(
                    "params => window.__rmPersonWire.startRpc('rmqualification.person', params)",
                    dict(sessions[0], hold=True),
                )
                held_request_id = request_id
                deadline = time.monotonic() + 3
                while not any(
                    event.get("requestId") == request_id and event.get("phase") == "prepared"
                    for event in native_events()
                ):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("native_hold_not_observed")
                    time.sleep(0.01)
                observed_events = native_events()
                prepared_event = next(
                    event
                    for event in observed_events
                    if event.get("requestId") == request_id and event.get("phase") == "prepared"
                )
                connection_id = capture["accounts"][0]["connection_id"]
                if prepared_event.get("connectionId") != connection_id:
                    raise ValueError("native_revocation_unverified")
                contexts[0].close()
                deadline = time.monotonic() + 3
                while not any(
                    event.get("requestId") == request_id
                    and event.get("connectionId") == connection_id
                    and event.get("phase") == "connection_retired"
                    and event.get("connection_aborted") is True
                    for event in native_events()
                ):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("native_connection_retirement_not_observed")
                    time.sleep(0.01)
                release_path.write_bytes(b"release\n")
                deadline = time.monotonic() + 5
                while not any(
                    event.get("requestId") == request_id
                    and event.get("phase") in {"guard_denied", "probe_failed"}
                    for event in native_events()
                ):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("native_revocation_not_observed")
                    time.sleep(0.01)
                observed_events = native_events()
                retired_event = next(
                    event
                    for event in observed_events
                    if event.get("requestId") == request_id
                    and event.get("connectionId") == connection_id
                    and event.get("phase") == "connection_retired"
                    and event.get("connection_aborted") is True
                )
                denied_event = next(
                    event
                    for event in observed_events
                    if event.get("requestId") == request_id
                    and event.get("phase") in {"guard_denied", "probe_failed"}
                )
                capture["events"].append(
                    dict(
                        id="retired:" + request_id,
                        kind="connection_revocation",
                        browser_context_closed=True,
                        prepared=prepared_event,
                        retired=retired_event,
                        denied=denied_event,
                        native_sequence=[
                            event.get("phase")
                            for event in observed_events
                            if event.get("requestId") == request_id
                        ],
                    )
                )
                if denied_event.get("reason") != "post_await_authority_retired" or denied_event.get(
                    "connectionId"
                ) != prepared_event.get("connectionId"):
                    raise ValueError("native_revocation_unverified")
                mark("revocation_denied")
                capture["transport"] = (
                    "programmatic fixture RPC on native authenticated Control UI socket"
                )
                capture["status"], capture["reason"] = "completed", None
                if any(secret in json.dumps(capture) for secret in secrets_to_exclude):
                    raise ValueError("credential_export_refused")
            finally:
                if held_request_id is not None:
                    capture["held_native_observations"] = project_held_native_events(
                        events_path, held_request_id
                    )
                _close_contexts(contexts)
                browser.close()
    finally:
        release_path.write_bytes(b"release\n")
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in files:
            log.close()
        if any(secret in json.dumps(capture) for secret in secrets_to_exclude):
            raise ValueError("credential_export_refused")


def session_create_params() -> dict:
    """One disposable session per account; the host refuses a label another session holds."""
    token = secrets.token_hex(8)
    return dict(
        agentId="main",
        key="agent:main:person-" + token,
        label="Disposable person qualification " + token,
    )


# Every reason the harness raises by name is exported as itself.
SAFE_REASONS = frozenset(
    {
        "private_network_required",
        "linux_runner_required",
        "caller_alarm_in_use",
        "native_host_version",
        "configuration_invalid",
        "gateway_nonloopback_listener",
        "proxy_loopback_listener",
        "unauthenticated_proxy_ingress",
        "actual_peer_binding",
        "native_response_binding",
        "native_session_create_denied",
        "native_person_rpc_denied",
        "native_person_identity_binding",
        "cross_account_admitted",
        "native_hold_not_observed",
        "native_revocation_not_observed",
        "native_connection_retirement_not_observed",
        "native_revocation_unverified",
        "native_session_create_binding",
        "native_ui_pairing_timeout",
        "credential_export_refused",
        "overall_deadline_exceeded",
        "artifact_digest",
        "artifact_size",
        "artifact_redirect",
        "artifact_members",
        "artifact_executable",
        "artifact_expansion",
        "artifact_path",
        "child_startup_failed",
        "child_startup_timeout",
        "native_event_bound",
        "artifact_deadline",
        "native_event_invalid",
        "network_inventory_size",
        "network_inventory_invalid",
        "qualification_port",
        "qualification_accounts",
        "login_subphase_invalid",
        "login_subphase_bound",
        "unknown_step",
    }
)


def run_qualification(work: Path, output: Path) -> int:
    """Requested integration fails honestly when private setup is unavailable."""
    import platform
    import signal
    import time

    capture = unavailable_capture("private_network_unavailable")
    started = time.monotonic()
    previous_handler = None
    timer_installed = False

    def expired(_number, _frame):
        raise TimeoutError("overall_deadline_exceeded")

    def finish() -> int:
        capture["observed_ms"] = (time.monotonic() - started) * 1000
        write_capture(output, capture)
        return 0 if capture["checks"]["person_composition"] else 2

    try:
        if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
            capture["reason"] = "linux_runner_required"
            return finish()
        candidates = discover_network()
        if not candidates:
            return finish()
        if not _private_ipv4(candidates[0]):
            raise ValueError("private_network_required")
        work = work.parent.resolve(strict=True) / work.name
        work.mkdir(exist_ok=False, mode=0o700)
        if not hasattr(signal, "setitimer"):
            raise ValueError("linux_runner_required")
        if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
            raise ValueError("caller_alarm_in_use")
        previous_handler = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, TOTAL_SECONDS)
        timer_installed = True
        _run_private_trial(work, candidates[0], capture)
        capture["checks"]["person_composition"] = person_composition_pass(capture)
        if not capture["checks"]["person_composition"]:
            capture.update(status="failed", reason="person_composition_unverified")
    except Exception as error:
        reason = str(error) if str(error) in SAFE_REASONS else "qualification_failed"
        if reason == "credential_export_refused":
            safe = unavailable_capture(reason)
            for previous, step in zip(capture["steps"], safe["steps"], strict=True):
                if previous.get("status") in {"passed", "failed", "not_run"}:
                    step["status"] = previous["status"]
            capture = safe
        capture.update(status="failed", reason=reason, error_type=type(error).__name__)
        for step in capture["steps"]:
            if step["name"] == capture.get("phase"):
                step["status"] = "failed"
    finally:
        if timer_installed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
    return finish()


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    return run_qualification(args.work_dir, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
