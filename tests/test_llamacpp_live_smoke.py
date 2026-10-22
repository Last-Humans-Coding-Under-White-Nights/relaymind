"""Live smoke test for the pinned Qwen3-0.6B artifact on llama-server.

RM18, spec section 6: non-thinking is the default, configured through the
pinned runtime's reasoning controls and verified by a smoke test;
thinking mode is an operator option, and reasoning output must not
appear as answer content in either mode. Model, template, parser,
quantization, and runtime are pinned and tested together (S2, S3).
Issue #3.

Run explicitly with: pytest -m live
Requires llama-server on PATH and the model file from
models/qwen3-0.6b-q8_0.manifest.json (see models/README.md).
RELAYMIND_LIVE_MANIFEST may name another manifest, such as
models/qwen3-0.6b-q4_k_m.manifest.json for the AC08 swap; the suite then
runs against that manifest's artifact, runtime, and launch settings
with no code change.
RELAYMIND_SMOKE_MODEL may point to the artifact elsewhere on disk, but
it must be the manifest's artifact itself (byte-identical, same SHA-256
as the manifest); any other file fails verification.
"""

import json
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
    LIVE_MANIFEST_ENV,
    MODELS_DIR,
    collect_events,
    live_manifest_path,
    make_request,
    run_closing,
)
from relaymind.adapters import (
    Completed,
    GenerationOptions,
    Message,
    Role,
    TextDelta,
    ToolCall,
    Usage,
)
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


@pytest.fixture(scope="module")
def manifest() -> ArtifactManifest:
    # The manifest owns the pinned launch settings (spec section 6); the
    # server argv and the adapter config derive from it. Loaded here, not
    # at import, so a broken manifest never breaks collection of the
    # default (non-live) suite. RELAYMIND_LIVE_MANIFEST selects another
    # manifest (AC08).
    return load_manifest(live_manifest_path(os.environ))


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
        # an explicit override that does not exist is a mistake. Checked
        # before the runtime, so a machine without the model skips.
        if override:
            pytest.fail(f"{MODEL_ENV} points to a missing file: {model_path}")
        # A manifest chosen on purpose (AC08) must run, never skip.
        if os.environ.get(LIVE_MANIFEST_ENV):
            pytest.fail(
                f"{LIVE_MANIFEST_ENV} names a manifest whose artifact is missing: {model_path}"
            )
        pytest.skip(f"model artifact not found: {model_path}")
    # Resolve and check explicit selections before a missing runtime can
    # skip the test. Both live suites share this server fixture.
    if shutil.which("llama-server") is None:
        pytest.skip("llama-server not on PATH")
    # RM18: a passing smoke on a different binary does not qualify this pin.
    version = subprocess.run(
        ["llama-server", "--version"], capture_output=True, text=True, check=True
    )
    banner = "\n".join((version.stdout, version.stderr))
    # Strip each line: Windows output may end lines with CR or spaces.
    banner_lines = [line.strip() for line in banner.splitlines()]
    assert f"version: {manifest.runtime_version}" in banner_lines, banner
    print(f"\nRuntime: {manifest.runtime_version}")
    # models/README.md: verify against the manifest before use. A
    # mismatched artifact is a failure, never a silent substitution.
    try:
        verify_artifact(manifest, model_path)
    except ManifestError as exc:
        if override:
            pytest.fail(
                f"{MODEL_ENV} must point to the manifest's artifact {manifest.file_name} "
                f"(byte-identical to the manifest); {exc}"
            )
        pytest.fail(f"artifact {model_path} failed verification: {exc}")
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
def wire_chunks():
    return []


class RecordingStream(httpx.AsyncByteStream):
    """Observe the actual SSE while retaining incremental adapter consumption."""

    def __init__(self, stream, chunks):
        self.stream = stream
        self.chunks = chunks

    async def __aiter__(self):
        async for chunk in self.stream:
            self.chunks.append(chunk)
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class RecordingTransport(httpx.AsyncHTTPTransport):
    def __init__(self, chunks):
        super().__init__()
        self.chunks = chunks

    async def handle_async_request(self, request):
        response = await super().handle_async_request(request)
        response.stream = RecordingStream(response.stream, self.chunks)
        return response


@pytest.fixture()
def make_adapter(server, adapter_settings, wire_chunks):
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
            ),
            transport=RecordingTransport(wire_chunks),
        )

    return factory


@pytest.mark.parametrize("enable_thinking", [False, True], ids=["non-thinking", "thinking"])
def test_reasoning_never_appears_as_answer_content(make_adapter, enable_thinking, wire_chunks):
    adapter = make_adapter(enable_thinking)
    events = collect_events(
        adapter,
        make_request(
            generation_id=f"smoke_text_{enable_thinking}",
            messages=(Message(role=Role.USER, content="Reply with exactly one word: pong"),),
            options=GenerationOptions(max_output_tokens=512, temperature=0),
        ),
    )
    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    assert text.strip(), f"no answer text in events: {events}"
    assert "<think>" not in text, f"thinking block leaked into answer: {text!r}"
    assert "</think>" not in text, f"thinking closing tag leaked into answer: {text!r}"
    assert any(isinstance(event, Completed) for event in events)
    # RM18: prove the option changed actual runtime behavior, and that the
    # adapter emitted exactly the content channel, never the reasoning channel.
    deltas = []
    for line in b"".join(wire_chunks).decode().splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        for choice in json.loads(data).get("choices") or []:
            deltas.append(choice.get("delta") or {})
    thoughts = "".join(delta.get("reasoning_content") or "" for delta in deltas)
    assert bool(thoughts.strip()) is enable_thinking
    assert text == "".join(delta.get("content") or "" for delta in deltas)
    print(
        f"thinking={enable_thinking}; reasoning_present={bool(thoughts.strip())}; answer={text!r}"
    )


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
            options=GenerationOptions(max_output_tokens=512, temperature=0),
        ),
    )
    tool_calls = [event for event in events if isinstance(event, ToolCall)]
    assert tool_calls, f"model proposed no tool call; events: {events}"
    call = tool_calls[0]
    assert call.tool_name == "get_service_health"
    assert call.arguments.get("service") == "staging-api"
    print(f"tool={call.tool_name}; arguments={dict(call.arguments)}")


@pytest.mark.parametrize(
    ("prompt", "with_tools"),
    [
        ("Is staging-api healthy right now?", False),
        ("staging-api 现在健康吗？", False),
        ("Is staging-api healthy right now?", True),
        ("payments-api 正常运行吗？", True),
    ],
    ids=["en", "zh", "en-tools", "zh-tools"],
)
def test_token_count_matches_the_runtime_prompt_count(make_adapter, request, prompt, with_tools):
    # Issue #47, spec section 4.1: the router input is measured by the
    # local tokenizer. The count must equal the prompt tokens the runtime
    # reports for a generation of the same messages and tools.
    messages = (
        Message(role=Role.SYSTEM, content="You check service health."),
        Message(role=Role.USER, content=prompt),
    )
    tools = (HEALTH_TOOL,) if with_tools else ()
    counted = run_closing(make_adapter(False), lambda a: a.count_tokens(messages, tools))
    events = collect_events(
        make_adapter(False),
        make_request(
            # One id per case (en, zh, en-tools, zh-tools), so the four
            # generations never share one in adapter bookkeeping.
            generation_id=f"smoke_count_{request.node.callspec.id}",
            messages=messages,
            tool_schemas=tools,
            options=GenerationOptions(max_output_tokens=1),
        ),
    )
    usage = [event for event in events if isinstance(event, Usage)]
    assert usage, f"runtime reported no usage; events: {events}"
    assert counted == usage[-1].input_tokens
