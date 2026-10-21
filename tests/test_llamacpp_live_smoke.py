"""Live smoke test for the pinned Qwen3-0.6B artifact on llama-server.

RM18, spec section 6: non-thinking is the default, configured through the
pinned runtime's chat-template settings and verified by a smoke test;
thinking mode is an operator option, and reasoning output must not
appear as answer content in either mode. Model, template, parser,
quantization, and runtime are pinned and tested together (S2, S3).
Issue #3.

Run explicitly with: pytest -m live
Requires llama-server on PATH and the model file from
models/qwen3-0.6b-q8_0.manifest.json (see models/README.md).
RELAYMIND_SMOKE_MODEL may point to the artifact elsewhere on disk, but
it must be the pinned artifact itself (byte-identical, same SHA-256 as
the manifest); any other file fails verification.
"""

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from helpers import (
    HEALTH_TOOL,
    MODELS_DIR,
    PINNED_MANIFEST_PATH,
    collect_events,
    make_request,
)
from relaymind.adapters import Completed, Message, Role, TextDelta, ToolCall
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig
from relaymind.adapters.local_http import health_client
from relaymind.adapters.manifest import (
    ArtifactManifest,
    ManifestError,
    launch_adapter_settings,
    launch_settings_argv,
    load_manifest,
    verify_artifact,
)

pytestmark = pytest.mark.live

MODEL_ENV = "RELAYMIND_SMOKE_MODEL"


@pytest.fixture(scope="module", autouse=True)
def require_llama_server() -> None:
    # Autouse, so it runs before the manifest fixtures: on a machine
    # without llama-server the suite skips, even with a broken manifest.
    if shutil.which("llama-server") is None:
        pytest.skip("llama-server not on PATH")


@pytest.fixture(scope="module")
def manifest() -> ArtifactManifest:
    # The manifest owns the pinned launch settings (spec section 6); the
    # server argv and the adapter config derive from it. Loaded here, not
    # at import, so a broken manifest never breaks collection of the
    # default (non-live) suite.
    return load_manifest(PINNED_MANIFEST_PATH)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def adapter_settings(manifest) -> tuple[int, dict[str, object]]:
    # Context limit and base template kwargs, derived once per module.
    return launch_adapter_settings(manifest.launch_settings)


@pytest.fixture(scope="module")
def server(tmp_path_factory, manifest, adapter_settings):
    # adapter_settings is unused here; requesting it derives the adapter
    # settings before the server starts, so unusable settings fail
    # without a model load.
    override = os.environ.get(MODEL_ENV)
    model_path = Path(override) if override else MODELS_DIR / manifest.file_name
    if not model_path.is_file():
        # Only the default location may be absent (no local artifact);
        # an explicit override that does not exist is a mistake.
        if override:
            pytest.fail(f"{MODEL_ENV} points to a missing file: {model_path}")
        pytest.skip(f"model artifact not found: {model_path}")
    # models/README.md: verify against the manifest before use. A
    # mismatched artifact is a failure, never a silent substitution.
    try:
        verify_artifact(manifest, model_path)
    except ManifestError as exc:
        if override:
            pytest.fail(
                f"{MODEL_ENV} must point to the pinned artifact {manifest.file_name} "
                f"(byte-identical to the manifest); {exc}"
            )
        pytest.fail(f"pinned artifact {model_path} failed verification: {exc}")
    server_args = launch_settings_argv(manifest.launch_settings)
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
                str(model_path),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                *server_args,
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
                with health_client(base_url) as client:
                    if client.get("/health").status_code == 200:
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
def make_adapter(server, adapter_settings):
    ctx_size, template_kwargs = adapter_settings

    def factory(enable_thinking: bool) -> LlamaCppAdapter:
        return LlamaCppAdapter(
            LlamaCppConfig(
                base_url=server,
                context_limit=ctx_size,
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
