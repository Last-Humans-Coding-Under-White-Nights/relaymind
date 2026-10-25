#!/usr/bin/env python3
"""Deterministic loopback keyword selector example (RM28); no model or tool calls."""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer

MAX_BODY = 2 * 1024 * 1024


def selection(request: dict) -> dict:
    """Choose the catalog ID with the greatest shared words, or none."""
    words = set(
        re.findall(
            r"[a-z]{3,}",
            " ".join(str(m.get("content", "")) for m in request.get("conversation", [])).lower(),
        )
    )
    best = None
    score = 0
    for entry in request.get("catalog", []):
        terms = set(re.findall(r"[a-z]{3,}", (entry["id"] + " " + entry["description"]).lower()))
        overlap = len(words & terms)
        if overlap > score:
            best, score = entry["id"], overlap
    return dict(
        schema_version=1,
        candidate={"kind": "skill", "skill_id": best} if best else {"kind": "none"},
        mechanism="keyword_rule",
        confidence=None,
        selector={"name": "example_keyword", "version": "1"},
        model_calls=[],
    )


class Handler(BaseHTTPRequestHandler):
    """Only POST /select; no filesystem, authentication, permission or tool access."""

    def reply(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(2)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.reply(code, {"error": "unsupported request"})

    def do_POST(self) -> None:
        if self.path != "/select":
            self.reply(404, {"error": "unknown path"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                raise ValueError
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError
            response = selection(request)
        except (ValueError, KeyError, TypeError, AttributeError, TimeoutError):
            self.reply(400, {"error": "invalid request"})
            return
        self.reply(200, response)

    def do_GET(self) -> None:
        self.reply(405, {"error": "POST required"})

    do_HEAD = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_PATCH = do_GET
    do_OPTIONS = do_GET

    def log_message(self, *args: object) -> None:
        pass


class LoopbackServer(ThreadingHTTPServer):
    """Avoid hostname resolution; only a literal loopback socket is bound."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8051)
    args = parser.parse_args()
    with LoopbackServer(("127.0.0.1", args.port), Handler) as server:
        print(f"http://127.0.0.1:{server.server_port}/select", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
