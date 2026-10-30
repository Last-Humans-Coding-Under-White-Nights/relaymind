#!/usr/bin/env python3
"""RM35/AC26: deterministic v2 protocol example; no model, tools or quality claim."""

import argparse
import asyncio
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer

from relaymind.adapters.local_http import is_loopback_host
from relaymind.assets.catalog import validate_asset_record
from relaymind.selection.asset_contract import (
    SelectorContext,
    parse_asset_request,
    parse_asset_result,
)
from relaymind.selection.asset_http import strict_json
from relaymind.selection.asset_lexical import LexicalAssetSelector, build_lexical_index

MAX_BODY = 33 * 1024 * 1024  # 32MiB catalog plus the bounded request envelope.


async def selection(value: object) -> dict:
    request = parse_asset_request(value)
    start = time.monotonic()

    def check():
        if (time.monotonic() - start) * 1000 >= request.data["deadline_ms"]:
            raise TimeoutError

    context = SelectorContext(time.monotonic, check)
    index = await build_lexical_index(
        request.data["catalog_revision"],
        [validate_asset_record(r) for r in request.data["catalog"]],
        catalog_bytes=request.catalog_bytes,
        context=context,
    )
    result = await LexicalAssetSelector(index).select(request, context)
    result["selector"] = {"name": "example_lexical", "version": "1"}
    result["mechanism"] = "example_lexical_overlap"
    return parse_asset_result(result).to_json_object()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def reply(self, status: int, value: dict):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self.reply(code, {"error": "unsupported_request"})

    def do_POST(self):
        if self.path != "/select":
            self.reply(404, {"error": "unknown_path"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                raise ValueError
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError
            value = asyncio.run(selection(strict_json(body)))
        except (ValueError, TypeError, UnicodeError, RecursionError, TimeoutError):
            self.reply(400, {"error": "invalid_request"})
            return
        self.reply(200, value)

    def do_GET(self):
        self.reply(405, {"error": "post_required"})

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_GET

    def log_message(self, *args):
        pass


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self):
        # Do not use HTTPServer's reverse hostname lookup.
        TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    if not is_loopback_host(host):
        raise ValueError("loopback host required")
    return LoopbackServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8913)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1 through 65535")
    with make_server("127.0.0.1", args.port) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
