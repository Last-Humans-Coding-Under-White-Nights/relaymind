"""RM01, RM18 / spec section 6: bypass proxies only for local inference (#57).

Keep HTTPX's client routing, replacing proxy discovery and network transports.
This reproduces environment/system proxy discovery without a real proxy or model.
"""

import asyncio
import os
from types import SimpleNamespace

import httpx
import pytest

from helpers import delta_chunk, load_script_module, make_request, sse_response
from relaymind.adapters import Completed, TextDelta
from relaymind.adapters.chat_completions import ChatCompletionsAdapter, ChatCompletionsConfig
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig

ENDPOINTS = [
    ("localhost", True),
    ("LOCALHOST", True),
    ("127.0.0.1", True),
    ("127.42.0.9", True),
    ("[::1]", True),
    ("[0:0:0:0:0:0:0:1]", True),
    ("localhost.example", False),
    ("notlocalhost", False),
    ("127.0.0.1.example", False),
    ("localhost@remote.example", False),
    ("192.168.1.2", False),
    ("0.0.0.0", False),
    ("[::]", False),
    ("[2001:db8::1]", False),
    ("remote.example", False),
]


@pytest.fixture()
def proxy_routes(monkeypatch):
    routes = []
    monkeypatch.setattr(
        httpx._utils, "getproxies", lambda: {"all": "http://corporate-proxy.example:8080"}
    )

    def transport(*args, proxy=None, **kwargs):
        def handle(request):
            routes.append((proxy is not None, request.url.path))
            if request.url.path == "/health":
                return httpx.Response(200)
            return sse_response(
                [delta_chunk({"content": "pong"}), delta_chunk({}, finish_reason="stop")]
            )

        return httpx.MockTransport(handle)

    # Do not pass transport= to a client: HTTPX suppresses proxy discovery
    # for injected transports, which would hide the original regression.
    monkeypatch.setattr(httpx._client, "HTTPTransport", transport)
    monkeypatch.setattr(httpx._client, "AsyncHTTPTransport", transport)
    return routes


@pytest.mark.parametrize(("host", "loopback"), ENDPOINTS)
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_llamacpp_routes_health_and_completions(host, loopback, scheme, proxy_routes):
    before = dict(os.environ)

    async def run():
        async with LlamaCppAdapter(
            LlamaCppConfig(base_url=f"{scheme}://{host}:8033", context_limit=8192)
        ) as adapter:
            assert (await adapter.health()).warm
            assert [event async for event in adapter.generate(make_request())] == [
                TextDelta(text="pong"),
                Completed(finish_reason="stop"),
            ]

    asyncio.run(run())
    assert proxy_routes == [(not loopback, "/health"), (not loopback, "/v1/chat/completions")]
    assert dict(os.environ) == before


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "remote.example"])
def test_external_adapter_keeps_proxy_handling_even_on_loopback(host, proxy_routes, monkeypatch):
    monkeypatch.setenv("RELAYMIND_TEST_EXTERNAL_KEY", "test-key")

    async def run():
        async with ChatCompletionsAdapter(
            ChatCompletionsConfig(
                base_url=f"https://{host}/v1",
                model="external",
                context_limit=8192,
                api_key_env="RELAYMIND_TEST_EXTERNAL_KEY",
            )
        ) as adapter:
            events = [event async for event in adapter.generate(make_request())]
            assert events[-1] == Completed(finish_reason="stop")

    asyncio.run(run())
    assert proxy_routes == [(True, "/v1/chat/completions")]


@pytest.mark.parametrize(("host", "loopback"), ENDPOINTS)
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_runtime_health_client_routes_by_base_url(host, loopback, scheme, proxy_routes):
    from relaymind.adapters.local_http import health_client

    before = dict(os.environ)
    with health_client(f"{scheme}://{host}:8033") as client:
        assert client.get("/health").status_code == 200
    assert proxy_routes == [(not loopback, "/health")]
    assert dict(os.environ) == before


@pytest.mark.parametrize("caller", ["live_smoke", "timing_probe"])
def test_startup_health_polls_bypass_discovered_proxy(caller, proxy_routes, monkeypatch, tmp_path):
    if caller == "live_smoke":
        import test_llamacpp_live_smoke as module
    else:
        module = load_script_module("probe_timing")

    stopped = []
    process = SimpleNamespace(
        poll=lambda: None,
        terminate=lambda: stopped.append(True),
        wait=lambda **kwargs: 0,
    )
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(module, "_free_port", lambda: 8033)
    model_path = tmp_path / "model.gguf"
    model_path.touch()
    if caller == "live_smoke":
        monkeypatch.setenv(module.MODEL_ENV, str(model_path))
        monkeypatch.setattr(module, "verify_artifact", lambda *args: None)
        monkeypatch.setattr(module, "launch_settings_argv", lambda *args: [])
        manifest = SimpleNamespace(file_name=model_path.name, launch_settings={})
        factory = SimpleNamespace(mktemp=lambda name: tmp_path)
        server = module.server.__wrapped__(factory, manifest, (8192, {}))
        try:
            assert next(server) == "http://127.0.0.1:8033"
        finally:
            server.close()
    else:
        launched, base_url, _ = module._launch_server(model_path, [], tmp_path / "server.log")
        module._stop(launched)
        assert base_url == "http://127.0.0.1:8033"

    assert stopped == [True]
    assert proxy_routes == [(False, "/health")]
