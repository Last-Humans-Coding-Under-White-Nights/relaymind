"""Capture and freeze genuine MCP tools for the acceptance catalog (spec 0.66 section 12.6.6).

Maintained successor (#261, #251; RM40, AC31) to the as-run copies
``capture_mcp_tools.py`` and ``freeze_mcp_capture.py`` in
``evaluation/assets/acceptance-mcp-v1/``. Those copies stay unchanged because
that capture's ``lock.v1.json`` binds their bytes. A new capture uses this
script and becomes a new capture version, never a replacement of v1.

    python scripts/acceptance_mcp_capture.py capture --servers servers.json --out CAPTURE_DIR
    python scripts/acceptance_mcp_capture.py freeze CAPTURE_DIR servers.json OUT_DIR CAPTURED_AT

``capture`` starts every pinned server of ``servers.json`` locally over stdio,
sends ``initialize``, ``notifications/initialized`` and ``tools/list``
(following ``nextCursor``), keeps the exact response line bytes as originals
and writes ``capture.raw.json``. It never calls a tool. It uses POSIX
``select`` on the server pipes.

``freeze`` writes into OUT_DIR the originals and licenses (exact bytes),
``capture.v1.json`` (servers, every tool with its original's SHA-256 and
canonical ``schema_hash``, and the selection of exactly 100 tools) and
``lock.v1.json`` (the document's canonical digest and the SHA-256 and length
of every referenced file, this script included). This script copies itself
into OUT_DIR when it is missing there and refuses an OUT_DIR copy with other
bytes, so the lock binds the bytes that ran.

Selection rule: order every captured tool by SHA-256 of the UTF-8 text
``<server_id>/<tool_name>`` and keep the first 100; the rest are recorded as
excluded with reason ``beyond_quota``. No tool is edited, translated or made up.

Every JSON file is written as UTF-8 with LF line endings, so its hash and
length do not depend on the platform. Run with a RelayMind checkout's ``src``
on the import path.
"""

import argparse
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

from relaymind.canonical import canonical_digest
from relaymind.tools.registry import check_parameters_schema

PROTOCOL = "2025-06-18"
TIMEOUT = 90.0
QUOTA = 100
SCRIPT = Path(__file__).resolve()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_json(path: Path, value, *, ensure_ascii: bool = False) -> None:
    """Write indented JSON as UTF-8 bytes with LF newlines on every platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, ensure_ascii=ensure_ascii) + "\n"
    path.write_bytes(text.encode("utf-8"))


# Capture.


class Session:
    def __init__(self, argv, cwd, env):
        self.process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.buffer = b""

    def send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def _line(self, deadline: float) -> bytes:
        fd = self.process.stdout.fileno()
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("server_timeout")
            ready, _, _ = select.select([fd], [], [], remaining)
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise EOFError("server_closed")
                self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line

    def response(self, request_id: int) -> bytes:
        """The exact bytes of the response line for ``request_id``."""
        deadline = time.monotonic() + TIMEOUT
        while True:
            line = self._line(deadline)
            try:
                message = json.loads(line)
            except ValueError:
                continue  # A log line on stdout is not a protocol message.
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                # A server request (e.g. roots/list): this client offers nothing.
                self.send(
                    dict(
                        jsonrpc="2.0",
                        id=message["id"],
                        error=dict(code=-32601, message="not supported"),
                    )
                )
                continue
            if message.get("id") == request_id:
                return line.rstrip(b"\r")

    def close(self) -> None:
        """End the session: close both pipes, then reap the server."""
        for pipe in (self.process.stdin, self.process.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        try:
            self.process.wait(5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(5)


def capture(server: dict, out: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    env.update(server.get("env", {}))
    session = Session(server["argv"], server.get("cwd"), env)
    pages = []
    try:
        session.send(
            dict(
                jsonrpc="2.0",
                id=1,
                method="initialize",
                params=dict(
                    protocolVersion=PROTOCOL,
                    capabilities={},
                    clientInfo=dict(name="relaymind-catalog-capture", version="1"),
                ),
            )
        )
        initialize = session.response(1)
        session.send(dict(jsonrpc="2.0", method="notifications/initialized"))
        cursor, request_id = None, 2
        while True:
            params = {} if cursor is None else {"cursor": cursor}
            session.send(dict(jsonrpc="2.0", id=request_id, method="tools/list", params=params))
            raw = session.response(request_id)
            pages.append(raw)
            cursor = json.loads(raw).get("result", {}).get("nextCursor")
            request_id += 1
            if not cursor:
                break
    finally:
        session.close()
    sid = server["id"]
    originals = out / "originals"
    originals.mkdir(parents=True, exist_ok=True)
    files = []
    (originals / f"{sid}.initialize.json").write_bytes(initialize)
    files.append(("initialize", f"originals/{sid}.initialize.json", initialize))
    for index, raw in enumerate(pages):
        name = f"originals/{sid}.tools-list.{index}.json"
        (out / name).write_bytes(raw)
        files.append(("tools_list", name, raw))
    info = json.loads(initialize)["result"]
    tools = []
    for index, raw in enumerate(pages):
        for tool in json.loads(raw)["result"]["tools"]:
            schema = tool.get("inputSchema")
            try:
                check_parameters_schema(schema)
                admitted, reason = True, None
            except Exception as error:  # The product check's own refusal.
                admitted, reason = False, type(error).__name__
            tools.append(
                dict(
                    name=tool["name"],
                    original=f"originals/{sid}.tools-list.{index}.json",
                    schema_hash=canonical_digest(schema) if isinstance(schema, dict) else None,
                    product_schema_check=admitted,
                    product_schema_refusal=reason,
                )
            )
    license_path = Path(server["license_file"])
    licenses = out / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    license_name = f"{sid}.{license_path.name}"
    shutil.copyfile(license_path, licenses / license_name)
    license_bytes = (licenses / license_name).read_bytes()
    return dict(
        id=sid,
        package=server["package"],
        source_url=server["source_url"],
        license=dict(
            spdx=server["license_spdx"],
            path=f"licenses/{license_name}",
            evidence=license_path.name,
            sha256=_sha(license_bytes),
            bytes=len(license_bytes),
        ),
        server_info=info.get("serverInfo"),
        protocol_version=info.get("protocolVersion"),
        originals=[
            dict(kind=kind, path=path, sha256=_sha(raw), bytes=len(raw))
            for kind, path, raw in files
        ],
        tools=tools,
    )


def capture_all(servers_json: Path, out: Path) -> int:
    servers = json.loads(servers_json.read_bytes())
    out.mkdir(parents=True, exist_ok=True)
    captured = []
    for server in servers:
        try:
            captured.append(capture(server, out))
            print(f"{server['id']}: {len(captured[-1]['tools'])} tools", file=sys.stderr)
        except (OSError, ValueError, TimeoutError, EOFError, KeyError) as error:
            print(f"{server['id']}: failed {type(error).__name__} {error}", file=sys.stderr)
            return 2
    write_json(out / "capture.raw.json", captured)
    return 0


# Freeze.


def _placeholder(arg: str) -> str:
    arg = re.sub(r"^.*/node_modules/", "<npm>/node_modules/", arg)
    arg = re.sub(r"^.*/bin/node$", "<node>", arg)
    arg = re.sub(r"^.*/py/bin/python$", "<python>", arg)
    arg = re.sub(r"^.*/mcp/(fsroot|gitrepo)$", r"<\1>", arg)
    return arg


def _place_script(out: Path) -> bytes | None:
    """This script's bytes in OUT_DIR: copied when missing, None when they differ."""
    source = SCRIPT.read_bytes()
    target = out / SCRIPT.name
    if not target.exists():
        out.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source)
    return source if target.read_bytes() == source else None


def freeze(capture_dir: Path, servers_json: Path, out: Path, captured_at: str) -> int:
    capture_dir, servers_json, out = Path(capture_dir), Path(servers_json), Path(out)
    captured = json.loads((capture_dir / "capture.raw.json").read_bytes())
    launches = {s["id"]: s for s in json.loads(servers_json.read_bytes())}
    script = _place_script(out)
    if script is None:
        print(f"script_mismatch: {out / SCRIPT.name} differs from {SCRIPT}", file=sys.stderr)
        return 2
    for name in ("originals", "licenses"):
        shutil.copytree(capture_dir / name, out / name, dirs_exist_ok=False)
    servers, tools = [], []
    for server in captured:
        launch = launches[server["id"]]
        servers.append(
            dict(
                id=server["id"],
                package=server["package"],
                launch=[_placeholder(a) for a in launch["argv"]],
                source_url=server["source_url"],
                license=server["license"],
                server_info=server["server_info"],
                protocol_version=server["protocol_version"],
                originals=server["originals"],
            )
        )
        digests = {o["path"]: o["sha256"] for o in server["originals"]}
        for tool in server["tools"]:
            tools.append(
                dict(
                    server_id=server["id"],
                    name=tool["name"],
                    original=tool["original"],
                    original_sha256=digests[tool["original"]],
                    schema_hash=tool["schema_hash"],
                    product_schema_check=tool["product_schema_check"],
                    origin="genuine_mcp",
                )
            )
    ordered = sorted(tools, key=lambda t: _sha(f"{t['server_id']}/{t['name']}".encode()))
    if len(ordered) < QUOTA or not all(t["product_schema_check"] for t in ordered[:QUOTA]):
        print("fewer than 100 admissible tools", file=sys.stderr)
        return 2
    selected = [f"{t['server_id']}/{t['name']}" for t in ordered[:QUOTA]]
    excluded = [
        dict(tool=f"{t['server_id']}/{t['name']}", reason="beyond_quota") for t in ordered[QUOTA:]
    ]
    document = dict(
        kind="relaymind-acceptance-mcp-capture",
        version=1,
        captured_at=captured_at,
        capture_machine_role="linux_host",
        capture_tool=dict(path=SCRIPT.name, sha256=_sha(script)),
        protocol_version_requested=PROTOCOL,
        servers=servers,
        tools=sorted(tools, key=lambda t: (t["server_id"], t["name"])),
        selection=dict(
            quota=QUOTA,
            rule="sha256(utf8(server_id + '/' + tool_name)) ascending, first 100",
            selected=sorted(selected),
            excluded=sorted(excluded, key=lambda e: e["tool"]),
        ),
    )
    write_json(out / "capture.v1.json", document)
    files = sorted(
        {o["path"] for s in servers for o in s["originals"]}
        | {s["license"]["path"] for s in servers}
        | {SCRIPT.name}
    )
    lock = dict(
        kind="relaymind-acceptance-mcp-capture-lock",
        version=1,
        document=dict(path="capture.v1.json", digest=canonical_digest(document)),
        files=[
            dict(path=p, sha256=_sha((out / p).read_bytes()), bytes=(out / p).stat().st_size)
            for p in files
        ],
    )
    write_json(out / "lock.v1.json", lock, ensure_ascii=True)
    print(
        json.dumps(
            dict(tools=len(tools), selected=len(selected), digest=lock["document"]["digest"])
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("capture", help="capture tools/list from local servers")
    run.add_argument("--servers", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    frozen = commands.add_parser("freeze", help="freeze a capture into a locked tool set")
    frozen.add_argument("capture_dir", type=Path)
    frozen.add_argument("servers_json", type=Path)
    frozen.add_argument("out_dir", type=Path)
    frozen.add_argument("captured_at")
    args = parser.parse_args(argv)
    if args.command == "capture":
        return capture_all(args.servers, args.out)
    return freeze(args.capture_dir, args.servers_json, args.out_dir, args.captured_at)


if __name__ == "__main__":
    raise SystemExit(main())
