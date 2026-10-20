"""Live smoke test for the pinned Qwen3-0.6B artifact on llama-server.

Spec section 6: non-thinking is the default, configured through the
pinned runtime's chat-template settings and verified by a smoke test;
thinking mode is an operator option, and reasoning output must not
appear as answer content in either mode. Model, template, parser,
quantization, and runtime are pinned and tested together (S2, S3).
Issue #3.

Run explicitly with: pytest -m live
Requires llama-server on PATH and the model file from
models/qwen3-0.6b-q8_0.manifest.json (see models/README.md).
"""

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from helpers import HEALTH_TOOL, collect_events, make_request
from relaymind.adapters import Completed, Message, Role, TextDelta, ToolCall
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig

pytestmark = pytest.mark.live

MODEL_PATH = Path(os.environ.get("RELAYMIND_SMOKE_MODEL", "models/Qwen3-0.6B-Q8_0.gguf"))

# Pinned launch settings; one copy for the server and the adapters.
# TODO(#38): derive these from models/qwen3-0.6b-q8_0.manifest.json once
# the manifest machinery (PR #35) is on the same branch.
CTX_SIZE = 8192
NON_THINKING = {"enable_thinking": False}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    if shutil.which("llama-server") is None:
        pytest.skip("llama-server not on PATH")
    if not MODEL_PATH.is_file():
        pytest.skip(f"model artifact not found: {MODEL_PATH}")
    port = _free_port()
    process = subprocess.Popen(
        [
            "llama-server",
            "-m",
            str(MODEL_PATH),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--ctx-size",
            str(CTX_SIZE),
            "--jinja",
            "--chat-template-kwargs",
            '{"enable_thinking": false}',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"llama-server exited early with {process.returncode}")
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            pytest.fail("llama-server did not become healthy in 120s")
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture()
def make_adapter(server):
    def factory(enable_thinking: bool) -> LlamaCppAdapter:
        return LlamaCppAdapter(
            LlamaCppConfig(
                base_url=server,
                context_limit=CTX_SIZE,
                chat_template_kwargs={"enable_thinking": enable_thinking},
            )
        )

    return factory


@pytest.mark.parametrize("enable_thinking", [False, True], ids=["non-thinking", "thinking"])
def test_reasoning_never_appears_as_answer_content(make_adapter, enable_thinking):
    adapter = make_adapter(enable_thinking)
    events = collect_events(
        adapter,
        make_request(
            generation_id=f"smoke_text_{enable_thinking}",
            messages=(Message(role=Role.USER, content="Reply with exactly one word: pong"),),
        ),
    )
    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    assert text.strip(), f"no answer text in events: {events}"
    assert "<think>" not in text, f"thinking block leaked into answer: {text!r}"
    assert any(isinstance(event, Completed) for event in events)


def test_model_proposes_a_valid_tool_call(make_adapter):
    adapter = make_adapter(False)
    events = collect_events(
        adapter,
        make_request(
            generation_id="smoke_tool",
            messages=(
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You check service health. Use the get_service_health tool "
                        "to answer health questions."
                    ),
                ),
                Message(role=Role.USER, content="Is staging-api healthy right now?"),
            ),
            tool_schemas=(HEALTH_TOOL,),
        ),
    )
    tool_calls = [event for event in events if isinstance(event, ToolCall)]
    assert tool_calls, f"model proposed no tool call; events: {events}"
    call = tool_calls[0]
    assert call.tool_name == "get_service_health"
    assert call.arguments.get("service") == "staging-api"
