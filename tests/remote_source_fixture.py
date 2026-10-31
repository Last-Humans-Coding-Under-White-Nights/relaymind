"""Explicit test-only HTTP fixture; never a registered production source/origin."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx


@contextmanager
def server(reply):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            status, headers, chunks = reply(self.path)
            try:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    task = threading.Thread(target=httpd.serve_forever, daemon=True)
    task.start()
    try:
        yield httpd.server_port
    finally:
        httpd.shutdown()
        httpd.server_close()
        task.join(2)


class ForwardToFixture(httpx.AsyncBaseTransport):
    def __init__(self, port):
        self.port = port
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        # Only this trusted test constructor rewrites the actual socket destination.
        # Product origin/path admission still receives the fixed HTTPS profile URL.
        url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        mapped = httpx.Request(
            request.method,
            url,
            headers=request.headers,
            content=request.content,
            extensions=request.extensions,
        )
        return await self.inner.handle_async_request(mapped)

    async def aclose(self):
        await self.inner.aclose()


def reply_for(docs, files, path):
    path = path.split("?")[0]
    commit = docs["ref"]["object"]["sha"]
    root = docs["commit"]["tree"]["sha"]
    tree = next((e["sha"] for e in docs["root"]["tree"] if e["path"] == "skills"), None)
    if path.endswith("/git/ref/heads/master"):
        value = docs["ref"]
    elif path.endswith("/git/commits/" + commit):
        value = docs["commit"]
    elif path.endswith("/git/trees/" + root):
        value = docs["root"]
    elif tree is not None and path.endswith("/git/trees/" + tree):
        value = docs["skills"]
    elif "/" + commit + "/" in path:
        key = path.split("/" + commit + "/", 1)[1]
        return (200, {}, [files[key]]) if key in files else (404, {}, [b"fixture missing"])
    else:
        return 404, {}, [b"fixture missing"]
    return 200, {}, [json.dumps(value).encode()]
