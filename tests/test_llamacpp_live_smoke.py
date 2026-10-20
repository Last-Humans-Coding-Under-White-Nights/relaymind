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
from relaymind.adapters.manifest import launch_settings_argv, load_manifest, verify_artifact

pytestmark = pytest.mark.live

# The manifest owns the pinned launch settings (spec section 6); the
# server argv and the adapter config below derive from it.
REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "models" / "qwen3-0.6b-q8_0.manifest.json"
MANIFEST = load_manifest(MANIFEST_PATH)
MODEL_PATH = Path(
    os.environ.get("RELAYMIND_SMOKE_MODEL", str(REPO_ROOT / "models" / MANIFEST.file_name))
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if shutil.which("llama-server") is None:
        pytest.skip("llama-server not on PATH")
    if not MODEL_PATH.is_file():
        pytest.skip(f"model artifact not found: {MODEL_PATH}")
    # models/README.md: verify against the manifest before use. A
    # mismatched artifact is a failure, never a silent substitution.
    verify_artifact(MANIFEST, MODEL_PATH)
    port = _free_port()
    # Keep server logs on disk so a startup failure (port conflict,
    # bad parameter, missing backend) is diagnosable from the test
    # failure message.
    log_path = tmp_path_factory.mktemp("llama-server") / "server.log"

    def log_tail() -> str:
        try:
            return log_path.read_text(errors="replace")[-2000:]
        except OSError:
            return "<no server log captured>"

    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            [
                "llama-server",
                "-m",
                str(MODEL_PATH),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                *launch_settings_argv(MANIFEST.launch_settings),
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"llama-server exited early with {process.returncode}\n{log_tail()}")
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            pytest.fail(f"llama-server did not become healthy in 120s\n{log_tail()}")
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Never leave an orphaned server holding the model file.
            process.kill()
            process.wait()


@pytest.fixture()
def make_adapter(server):
    def factory(enable_thinking: bool) -> LlamaCppAdapter:
        settings = MANIFEST.launch_settings
        template_kwargs = settings.get("chat_template_kwargs") or {}
        return LlamaCppAdapter(
            LlamaCppConfig(
                base_url=server,
                context_limit=int(settings["ctx_size"]),
                # The manifest's qualified settings, with the thinking
                # switch as the operator option under test (spec
                # section 6).
                chat_template_kwargs={**template_kwargs, "enable_thinking": enable_thinking},
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
