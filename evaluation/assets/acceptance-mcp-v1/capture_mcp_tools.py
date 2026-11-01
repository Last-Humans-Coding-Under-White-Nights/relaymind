"""Capture tools/list from locally running open-source MCP servers (spec 0.66 section 12.6.6).

Acquisition tooling for the acceptance catalog's genuine MCP tools (#261, #251;
RM40, AC31). For every pinned server in ``servers.json`` it starts the locally
installed server over stdio, sends ``initialize``, ``notifications/initialized``
and ``tools/list`` (following ``nextCursor``), and keeps the exact response
line bytes as the original. ``capture.json`` records each original's SHA-256
and length, the server's pinned package and license file digest, and for every
tool its canonical ``schema_hash`` (``canonical_digest`` of ``inputSchema``)
and whether the product's own tool-schema check admits it.

It never calls a tool. Run it with the interpreter of a RelayMind checkout:

    python capture_mcp_tools.py --servers servers.json --out <dir>
"""

import argparse
import hashlib
import json
import os
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


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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
        try:
            self.process.stdin.close()
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--servers", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    servers = json.loads(args.servers.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    captured = []
    for server in servers:
        try:
            captured.append(capture(server, args.out))
            print(f"{server['id']}: {len(captured[-1]['tools'])} tools", file=sys.stderr)
        except (OSError, ValueError, TimeoutError, EOFError, KeyError) as error:
            print(f"{server['id']}: failed {type(error).__name__} {error}", file=sys.stderr)
            return 2
    (args.out / "capture.raw.json").write_text(
        json.dumps(captured, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
