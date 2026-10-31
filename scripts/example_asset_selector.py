#!/usr/bin/env python3
"""RM35/AC26: deterministic v2 protocol example; no model, tools or quality claim."""

import argparse
import asyncio
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer

from relaymind.adapters.local_http import is_loopback_host
from relaymind.assets.catalog import validate_asset_record
from relaymind.canonical import canonical_digest
from relaymind.selection.asset_contract import (
    SelectorContext,
    parse_asset_request,
    parse_asset_result,
)
from relaymind.selection.asset_http import strict_json
from relaymind.selection.asset_lexical import LexicalAssetSelector, build_lexical_index
from relaymind.selection.asset_preparation import (
    parse_preparation_request,
    parse_preparation_response,
    preparation_binding,
)

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


class IndexedExample:
    """Bounded in-memory companion; one CPU builder and isolated caller IDs."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}
        self.ready = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="example-index")

    def _response(self, request, state, reason=None, index=None, reused=None, elapsed=None):
        result = dict(
            schema_version=1,
            preparation_id=request["preparation_id"],
            binding=request["binding"],
            profile=request["profile"],
            state=state,
            index_revision=None if index is None else index.index_revision,
            reused=reused,
            elapsed_ms=elapsed,
            usage_claims=None,
            reason=reason,
        )
        return parse_preparation_response(result, request)

    def exchange(self, value):
        request = parse_preparation_request(value)
        identity = request["preparation_id"]
        with self.lock:
            job = self.jobs.get(identity)
            if job is not None:
                old = job["request"]
                if any(old[k] != request[k] for k in ("binding", "profile")) or (
                    request["operation"] == "prepare" and old != request
                ):
                    return self._response(request, "failed", "identity_conflict")
                self._expire(job)
                if (
                    job["response"]["state"] == "ready"
                    and canonical_digest(old["binding"]) not in self.ready
                ):
                    job["response"] = self._response(request, "stale_catalog", "catalog_stale")
                if request["operation"] == "cancel" and job["response"]["state"] == "indexing":
                    job["cancel"].set()
                    job["response"] = self._response(
                        request,
                        "cancelled",
                        "cancelled",
                        elapsed=(time.monotonic() - job["started"]) * 1000,
                    )
                return copy.deepcopy(job["response"])
            if request["operation"] != "prepare":
                return self._response(request, "failed", "unknown_preparation")
            if len(self.jobs) >= 1000:
                return self._response(request, "failed", "build_failed")
            started = time.monotonic()
            key = canonical_digest(request["binding"])
            index = self.ready.get(key) if request["profile"] == "reuse" else None
            result = (
                self._response(request, "indexing")
                if index is None
                else self._response(
                    request,
                    "ready",
                    index=index,
                    reused=True,
                    elapsed=(time.monotonic() - started) * 1000,
                )
            )
            job = dict(
                request=copy.deepcopy(request),
                response=result,
                started=started,
                deadline=started + request["budget_ms"] / 1000,
                cancel=threading.Event(),
            )
            self.jobs[identity] = job
            if index is None:
                self.executor.submit(self._build, identity)
            return copy.deepcopy(result)

    def _expire(self, job):
        """Called under the registry lock, including before a queued job starts."""
        if job["response"]["state"] == "indexing" and time.monotonic() >= job["deadline"]:
            job["response"] = self._response(
                job["request"],
                "failed",
                "deadline_exceeded",
                elapsed=(time.monotonic() - job["started"]) * 1000,
            )

    def _build(self, identity):
        with self.lock:
            job = self.jobs[identity]
            self._expire(job)
            if job["response"]["state"] != "indexing":
                return
        request = job["request"]
        deadline = job["deadline"]

        def check():
            if job["cancel"].is_set():
                raise InterruptedError
            if time.monotonic() >= deadline:
                raise TimeoutError

        try:
            check()
            index = asyncio.run(
                build_lexical_index(
                    request["binding"]["catalog_revision"],
                    [validate_asset_record(r) for r in request["catalog"]],
                    context=SelectorContext(time.monotonic, check),
                )
            )
            check()
            result = self._response(
                request,
                "ready",
                index=index,
                reused=False,
                elapsed=(time.monotonic() - job["started"]) * 1000,
            )
        except Exception:
            reason = (
                "cancelled"
                if job["cancel"].is_set()
                else ("deadline_exceeded" if time.monotonic() >= deadline else "build_failed")
            )
            index = None
            result = self._response(
                request,
                "cancelled" if reason == "cancelled" else "failed",
                reason,
                elapsed=(time.monotonic() - job["started"]) * 1000,
            )
        with self.lock:
            # Never restart a terminal state observed by status/cancel while
            # this builder was running, or overwrite its original failure.
            self._expire(job)
            if job["response"]["state"] != "indexing":
                return
            # Cancellation/expiry can win after the last builder checkpoint.
            if job["cancel"].is_set() or time.monotonic() >= deadline:
                reason = "cancelled" if job["cancel"].is_set() else "deadline_exceeded"
                result = self._response(
                    request,
                    "cancelled" if reason == "cancelled" else "failed",
                    reason,
                    elapsed=(time.monotonic() - job["started"]) * 1000,
                )
            elif index is not None:
                self.ready[canonical_digest(request["binding"])] = index
            job["response"] = result

    async def select(self, value):
        request = parse_asset_request(value)
        raw = request.data
        binding = preparation_binding(
            selector=dict(name="example_lexical", version="1"),
            catalog_revision=raw["catalog_revision"],
            catalog=list(raw["catalog"]),
            scope=raw["scope"],
            permissions=raw["permissions"],
            selection_scope=raw["selection_scope"],
            requested_kinds=raw["requested_kinds"],
        )
        with self.lock:
            index = self.ready.get(canonical_digest(binding))
        started = time.monotonic()

        def check():
            if (time.monotonic() - started) * 1000 >= raw["deadline_ms"]:
                raise TimeoutError

        result = await LexicalAssetSelector(index).select(
            request, SelectorContext(time.monotonic, check)
        )
        result["selector"] = dict(name="example_lexical", version="1")
        result["mechanism"] = "example_lexical_overlap"
        return parse_asset_result(result).to_json_object()

    def stop(self):
        with self.lock:
            for job in self.jobs.values():
                if job["response"]["state"] == "indexing":
                    job["cancel"].set()
        self.executor.shutdown(wait=True, cancel_futures=True)


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
        if self.path not in (self.server.selection_path, self.server.preparation_path):
            self.reply(404, {"error": "unknown_path"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                raise ValueError
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError
            request = strict_json(body)
            if self.path == self.server.preparation_path:
                value = self.server.registry.exchange(request)
            else:
                value = asyncio.run(
                    selection(request)
                    if self.server.registry is None
                    else self.server.registry.select(request)
                )
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
    registry = None
    preparation_path = None
    selection_path = "/select"

    def server_close(self):
        if self.registry is not None:
            self.registry.stop()
        super().server_close()

    def server_bind(self):
        # Do not use HTTPServer's reverse hostname lookup.
        TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


def make_server(
    host: str, port: int, *, preparation_path: str | None = None, selection_path: str = "/select"
) -> ThreadingHTTPServer:
    if not is_loopback_host(host):
        raise ValueError("loopback host required")
    for path in (selection_path, preparation_path):
        if path is not None and (
            not path.startswith("/") or any(c in path for c in "?#") or path == "/"
        ):
            raise ValueError("explicit local path required")
    if preparation_path == selection_path:
        raise ValueError("distinct paths required")
    server = LoopbackServer((host, port), Handler)
    server.selection_path, server.preparation_path = selection_path, preparation_path
    server.registry = None if preparation_path is None else IndexedExample()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8913)
    parser.add_argument("--preparation-path")
    parser.add_argument("--selection-path", default="/select")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1 through 65535")
    with make_server(
        "127.0.0.1",
        args.port,
        preparation_path=args.preparation_path,
        selection_path=args.selection_path,
    ) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
