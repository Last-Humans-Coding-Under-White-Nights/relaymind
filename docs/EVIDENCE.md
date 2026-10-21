# Live evidence record

Recorded runs of the live model tests and timing probes. A milestone closes
when its deterministic tests pass and its live checks appear here
([`ROADMAP.md`](ROADMAP.md)). Entries are what actually ran, on the machine
named, with the pinned artifact and runtime; nothing here is a benchmark
result in the sense of spec section 12.3 until M6 produces the report.

Rules for an entry: name the machine (OS version, CPU, GPU or backend, RAM),
the llama.cpp build, the manifest, the exact command, and the result,
including failures. Numbers from a single pass are listed as single-pass
observations, never as p50 or p95. Raw exports go in `docs/evidence/`.

## 2026-10-20 — M1 live smoke test, Apple Silicon macOS

| Field | Value |
|-------|-------|
| Machine | Apple M1, 8 GiB RAM, macOS 27.0.1 (build 26A434), Metal backend |
| Runtime | llama.cpp 0.7.0 (build 11669, commit 620d2225c), Homebrew build, AppleClang arm64 |
| Artifact | `models/qwen3-0.6b-q8_0.manifest.json`: Qwen3-0.6B Q8_0, SHA-256 `9465e63a…bb031`, verified before launch |
| Launch | derived from the manifest: `--ctx-size 8192 --jinja --chat-template-kwargs {"enable_thinking":false}` |
| Command | `pytest -m live` at master `c65a5c0` |
| Result | 3 passed in 5.18 s: non-thinking answer has no `<think>` block; thinking-mode answer has no `<think>` block in answer content; the model proposed `get_service_health` with `{"service": "staging-api"}` for "Is staging-api healthy right now?" |

Earlier runs of the same suite on Apple Silicon macOS are described in pull
requests #36 and #44 (2 passed and 3 passed). Their hardware details were
not recorded, which is why this file exists.

Not covered by this macOS smoke run: x86-64 Windows (recorded below for
#46), Chinese prompts, more than one tool, and skill selection among
several descriptions (#48).

## 2026-10-20 — M1 timing probe, Apple Silicon macOS

Same machine, runtime, artifact, and launch as above.

| Field | Value |
|-------|-------|
| Command | `python scripts/probe_timing.py scripts/probes/m1_timing.json --out docs/evidence/2026-10-20-m1-timing-apple-m1.json` on this branch |
| Probe definition | [`scripts/probes/m1_timing.json`](../scripts/probes/m1_timing.json): the system prompt, one `get_service_health` schema, six plain prompts and six tool-call prompts (three English and three Simplified Chinese each) |
| Procedure | `scripts/probe_timing.py` verifies the artifact against the manifest, launches llama-server from the manifest's launch settings, waits for `/health` 200, sends three warm-up rounds (one plain and one tool-call request each), then one pass over all twelve prompts through `LlamaCppAdapter` in `relaymind.stats.TimingCollector`, sequentially, with no other load |
| Raw export | [`evidence/2026-10-20-m1-timing-apple-m1.json`](evidence/2026-10-20-m1-timing-apple-m1.json): spec 12.1 metrics per generation, warm-ups included (their ids are listed in `run.warmup_generation_ids`), plus a `run` block with the command, cold start, server argv, runtime banner, system prompt, tool schema, prompts, and each generation's answer or proposal |
| Cold start | 1.06 s from llama-server process launch to the first `/health` 200 (`run.cold_start_seconds`). The artifact was read in full for SHA-256 verification just before launch, so its pages were warm in the OS file cache; this is not a cold-disk start |

Tool-call prompts, measured pass (first model action is the parsed tool
call, spec section 12.1):

| Prompt | First model action | Proposal |
|--------|-------------------:|----------|
| Is staging-api healthy right now? | 382 ms | `get_service_health` `{"service": "staging-api"}` |
| Check the health of payments-api. | 386 ms | `get_service_health` `{"service": "payments-api"}` |
| Is payments-api up? | 385 ms | `get_service_health` `{"service": "payments-api"}` |
| staging-api 现在健康吗？ | 426 ms | `get_service_health` `{"service": "staging-api"}` |
| 检查 payments-api 的健康状况。 | 379 ms | `get_service_health` `{"service": "payments-api"}` |
| payments-api 正常运行吗？ | 390 ms | `get_service_health` `{"service": "payments-api"}` |

Plain prompts, measured pass (no tools; first answer token and
completion):

| Prompt | First answer token | Completion | Answer |
|--------|-------------------:|-----------:|--------|
| Reply with exactly one word: pong | 28 ms | 44 ms | pong |
| What is the capital of France? Answer in one word. | 43 ms | 63 ms | Paris |
| Say hello in one short sentence. | 72 ms | 198 ms | Hello! How can I assist you? |
| 只用一个词回答：pong | 44 ms | 63 ms | 乒乓球 |
| 法国的首都是哪里？用一个词回答。 | 42 ms | 61 ms | 巴黎 |
| 用一句话打个招呼。 | 47 ms | 175 ms | 你好！有什么我可以帮你的吗？ |

Warm-up requests, not part of the measured pass: the first tool-call
request after launch took 475 ms to the parsed tool call, the next two
381 ms and 379 ms; the three plain warm-ups took 92, 30, and 29 ms to
the first token.

Observations, not conclusions:

- Six of six tool-call prompts produced the correct tool and argument,
  in both languages, with one tool offered. This is the first Chinese
  tool-call evidence for the candidate. It says nothing yet about
  selecting among several skills (#48).
- First model action for a tool call sat between 379 and 426 ms on this
  machine, single pass, after three tool-call warm-ups. The 500 ms
  hypothesis (p95, warmed, concurrency one) is neither met nor missed by
  this: it needs the section 12.2 protocol in M6.
- The first request that carried the system prompt and tool schema
  (`warmup_tool_0`) was the slowest tool call of the run. Warm-ups must
  cover the tool-call shape, not only plain text, before any tool-call
  latency is read.
- Tool-call output arrives from llama-server as one chunk after the
  arguments are complete, so first transport event equals first model
  action for those prompts. Plain text streams from the first token.
- The plain prompts are timing shapes, not graded cases: "只用一个词回答：pong"
  was answered with a translation (乒乓球) instead of the word asked for.
  Correctness is measured by the M6 corpus, not here.
- An earlier probe the same day, run from a script that was not kept,
  was removed from this record because its exact command could not be
  reproduced. This entry replaces it.

## 2026-10-21 — M1 live smoke test, x86-64 Windows

Windows qualification for #46 (RM18; spec sections 3, 6, 14). This entry
transcribes the contributor-supplied Windows run record; it was not rerun
on the macOS machine used to prepare this documentation. The Windows
application revision and raw test/server logs were not included in the
supplied record. Durations below are single-pass observations.

### Environment and pins

| Field | Value |
|-------|-------|
| Operating system | Microsoft Windows 11 Pro, version 10.0.26200, build 26200; x86-64 (AMD64) |
| CPU | 11th Gen Intel Core i7-1165G7 @ 2.80 GHz; 4 cores, 8 logical processors |
| GPU / backend | Intel Iris Xe Graphics, driver 30.0.100.9864; llama.cpp CPU backend, AVX512, 4 threads; no CUDA, Metal, or Vulkan |
| RAM | 16 GB |
| Python / shell | CPython 3.12.10 via `py -3.12`; Windows PowerShell 5.1; native Windows, no WSL or Docker |
| Manifest | `models/qwen3-0.6b-q8_0.manifest.json` |
| Artifact | `models/Qwen3-0.6B-Q8_0.gguf`, 639,446,688 bytes |
| Artifact SHA-256 | `9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031`, matching `manifest.file_sha256` |
| Verification | `relaymind.adapters.manifest.verify_artifact` passed: file, tokenizer, and chat-template hashes matched |
| Pinned runtime | llama.cpp `0.7.0 (build 11669, commit 620d2225c)` |
| Binary version banner | `version: 0.7.0-dev (build 11669, commit 620d2225c)`; `built with MSVC 19.42.34436.0 for x64` |
| Launch | `-m models/Qwen3-0.6B-Q8_0.gguf --host 127.0.0.1 --port <free> --ctx-size 8192 --jinja --chat-template-kwargs '{"enable_thinking": false}'`; settings derived from the manifest via `launch_settings_argv` |

The reported version has a `-dev` suffix; its build number and commit
match the manifest. The model hashes and launch settings are unchanged.

### Runtime setup and commands

The official `llama-b11669-bin-win-cpu-x64.zip` binary initially crashed
with `0xC0000005` in `MSVCP140.dll`. The run record identified the system
DLL as version 14.16.27033.0. The successful run used a source build of
the pinned commit instead:

1. Downloaded the [source tarball for commit 620d2225c](https://codeload.github.com/ggml-org/llama.cpp/tar.gz/620d2225c)
   after HTTPS git clone timed out on that network.
2. Built with CMake and Visual Studio 2022, MSVC 19.42.34436.0, x64,
   CPU only. Build metadata was supplied explicitly because the source
   tarball has no `.git` directory. In PowerShell, with `$src` and
   `$build` set to the extracted source and build directories:

   ```powershell
   cmake -S $src -B $build -G "Visual Studio 17 2022" -A x64 `
     -DLLAMA_BUILD_NUMBER=11669 -DLLAMA_BUILD_COMMIT=620d2225c `
     -DLLAMA_CURL=OFF -DGGML_CUDA=OFF -DGGML_BLAS=OFF `
     -DBUILD_SHARED_LIBS=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF
   cmake --build $build --config Release --target llama-server
   ```

3. Copied the matching VC143 CRT DLLs, including `msvcp140.dll`,
   `vcruntime140.dll`, and `vcruntime140_1.dll` (14.42.34433.0), from
   `VC\Redist\MSVC\14.42.34433\x64\Microsoft.VC143.CRT` next to
   `llama-server.exe`. The binary then started and reported the banner
   above. This build used `BUILD_SHARED_LIBS=OFF`; the record reports
   no additional runtime DLLs beyond the VC143 CRT.

With the project and dev dependencies installed, the following commands
were run from the repository root (`$build` names the build directory):

```powershell
$env:PATH = "$build\bin\Release;$env:PATH"

# Bypass this machine's corporate proxy for loopback traffic.
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:no_proxy = "127.0.0.1,localhost,::1"
$env:HTTP_PROXY = ""
$env:HTTPS_PROXY = ""

py -3.12 -m pytest
py -3.12 -m pytest tests/test_llamacpp_live_smoke.py -m live -v
ruff check .
ruff format --check .
```

### Results and scope

| Suite | Recorded outcome |
|-------|------------------|
| Deterministic suite | **258 passed, 3 deselected** in 24.47 s |
| Live smoke suite | **3 passed** in 11.73 s |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 48 files already formatted |

The live tests were:

1. `test_reasoning_never_appears_as_answer_content[non-thinking]`
2. `test_reasoning_never_appears_as_answer_content[thinking]`
3. `test_model_proposes_a_valid_tool_call`: `LlamaCppAdapter` parsed
   the streamed proposal for `get_service_health` with
   `{"service": "staging-api"}`.

These cover the same non-thinking, thinking, and tool-call smoke cases as
the macOS entry. No Windows-specific tool-call parsing difference was
reported. Together, the two platform records complete M1 qualification;
bilingual skill selection and the M6 benchmark remain separate gates.

### Platform and environment differences

- **PowerShell quoting:** manual `--chat-template-kwargs` JSON was
  mangled by native argument quoting on PowerShell 5.1. The existing
  [`TESTING.md`](TESTING.md#1-start-the-pinned-runtime) stop-parsing
  example covers this. The live suite uses a Python `subprocess.Popen`
  argument list and passed without a launch-setting change.
- **Runtime deprecation warning:** the pinned build accepted
  `--chat-template-kwargs` but warned that setting `enable_thinking`
  this way is deprecated in favor of `--reasoning on` / `--reasoning off`.
  This was a warning, not a test failure. The passing qualification
  retains the manifest settings; any future runtime migration must
  requalify the reasoning controls together with the adapter.
- **Proxy and loopback:** before the environment overrides above,
  health polls and completion requests timed out on a machine with a
  corporate proxy configured in Windows registry settings. With the
  overrides, the live suite passed in 11.73 s. The adapter currently
  creates its HTTP client with default proxy handling; automatic
  loopback proxy bypass remains a follow-up robustness concern. This
  run qualifies the explicitly configured environment only.
- **C++ runtime distribution:** the prebuilt executable failed with
  the older system CRT; the source build plus matching app-local CRT
  succeeded. This changes runtime installation, not the pinned commit,
  model, launch settings, or tool-call parser.
