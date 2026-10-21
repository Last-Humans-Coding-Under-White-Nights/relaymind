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

Windows qualification for #46 (RM18; spec sections 3, 6, 14), transcribed
from @ArcsinX's supplied Windows run notes. The application revision and
raw test/server logs were not included. Durations below are single-pass
observations.

The recorded counts (258 passed, 3 deselected; 48 formatted files) are
consistent with unmodified master commits `777f1c8`, `c65a5c0`, or
`9370473`. This narrows the likely revision from the counts; it does not
recover the exact checkout or establish that it was unmodified.
`git diff 777f1c8 2847013 -- src/relaymind/adapters tests/test_llamacpp_live_smoke.py`
is empty, so the adapter implementation and smoke tests in that range
match the PR's base `2847013`.

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

The banner's build number and commit were set explicitly with
`-DLLAMA_BUILD_NUMBER` / `-DLLAMA_BUILD_COMMIT` because the source tarball
has no `.git` directory. They repeat the requested pin and do not
independently verify the source commit. Source provenance is the reported
download of the commit-specific tarball in step 1; no source archive hash
was recorded. The `-dev` suffix reflects the pinned source's default
[`LLAMA_BUILD_IS_DEV=ON`](https://github.com/ggml-org/llama.cpp/blob/620d2225c/CMakeLists.txt#L9-L19).
The model hashes and launch settings are unchanged.

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
were run from the repository root (`$build` names the build directory).
The redundant lowercase `$env:no_proxy` assignment in the notes is
omitted: Windows environment variable names are case-insensitive.

```powershell
$env:PATH = "$build\bin\Release;$env:PATH"

# Proxy workaround for this test session; scope explained below.
$env:NO_PROXY = "127.0.0.1,localhost,::1"
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
- **Runtime deprecation warning (not platform-specific):** the pinned build accepted
  `--chat-template-kwargs` but warned that setting `enable_thinking`
  this way is deprecated in favor of `--reasoning on` / `--reasoning off`.
  This warning comes from the pinned build's
  [common argument parser](https://github.com/ggml-org/llama.cpp/blob/620d2225c/common/arg.cpp#L3554-L3570)
  on every platform; it was not a test failure. The passing qualification
  retains the manifest settings. [#58](https://github.com/Last-Humans-Coding-Under-White-Nights/relaymind/issues/58)
  tracks migrating the controls and requalifying the adapter at the next
  runtime update.
- **Proxy and loopback:** before the environment overrides above,
  health polls and completion requests timed out on a machine with a
  corporate proxy configured in Windows registry settings. With the
  overrides, the live suite passed in 11.73 s. At the recorded revision,
  the adapter created its HTTP client with default proxy handling.
  The subsequent loopback bypass fix in
  [#57](https://github.com/Last-Humans-Coding-Under-White-Nights/relaymind/issues/57)
  has deterministic coverage; this historical run does not qualify that
  new behavior on Windows. See [the current testing notes](TESTING.md#windows-qualification-notes).
  In CPython 3.12.10 on Windows,
  [`getproxies()`](https://github.com/python/cpython/blob/v3.12.10/Lib/urllib/request.py#L2766-L2773)
  uses environment proxy settings when any exist, falling back to the
  registry only otherwise. With `NO_PROXY` set and `HTTP_PROXY` /
  `HTTPS_PROXY` removed, this machine's registry proxy was skipped for
  all HTTPX requests in the session, including external destinations.
  This is broader than a loopback-only bypass. The run qualifies this
  configured environment only; the workaround is not a gateway fix.
- **C++ runtime distribution:** the prebuilt executable failed with
  the older system CRT; the source build plus matching app-local CRT
  succeeded. This changes runtime installation, not the pinned commit,
  model, launch settings, or tool-call parser.


## 2026-10-21 — Reasoning-control update, llama.cpp b11785 (#58)

RM18, specification 0.6 sections 6 and 14. This is a new M1 runtime
qualification, separate from the historical Windows #46/#56 record above
and from the AC15/M6 benchmark. The model file, tokenizer, and template
hashes are unchanged. Runtime and launch controls changed together.

The selected runtime is **0.7.0-dev (build 11785, commit 8baabd53b)**,
full source commit `8baabd53b77d1c48c6ef8d3b620436292f3f6231`. Release
archive hashes and download links are in
[`models/README.md`](../models/README.md#runtime-and-reasoning-controls).
The selected build's documentation and request parser (spec source S11)
confirm that the deprecation applies to the CLI template boolean, while
the HTTP `enable_thinking` boolean remains supported. Non-empty
`reasoning_effort` alone does not enable thinking after `--reasoning off`.

Both platforms use the hash-verified Qwen3-0.6B Q8_0 artifact and manifest
launch settings `--ctx-size 8192 --jinja --reasoning off --reasoning-format
deepseek`, with the harness supplying only the model path and loopback
address/port. The adapter derives `enable_thinking=false` from the
manifest, allows an operator request with `true`, and sends
`reasoning_format=deepseek` in both cases. Text cases use temperature 0
and a 512-token output cap. The live tests inspect actual SSE:
non-thinking must have no reasoning, thinking must have reasoning, and
normalized answer text must equal only the wire `content` channel.

### Apple Silicon macOS

| Field | Value |
|-------|-------|
| Machine | Apple M1, 8 GiB RAM, macOS 27.0.1 (26A434); Metal MTL0, all 29 layers offloaded |
| Runtime | Official b11785 macOS arm64 archive, SHA-256 verified; AppleClang 21.0.0.21000101 |
| Python | 3.14.7, pytest 9.1.1 |
| Application | Based on `ed3292d`; tested-file SHA-256 values recorded in the machine JSON, before the single PR commit was created |
| Command | Exact command and environment in [macos-machine.json](evidence/2026-10-21-reasoning-controls/macos-machine.json); marked smoke file with `-v -s`, runtime verbosity 4 for backend evidence |
| Result | **3 passed, 0 skipped in 5.12 s**: both text cases answered `pong`; reasoning absent/present respectively; parsed `get_service_health` with `service=staging-api` |
| Raw logs | [pytest](evidence/2026-10-21-reasoning-controls/macos-pytest.txt), [server](evidence/2026-10-21-reasoning-controls/macos-server.txt) |

The first attempt was blocked by the local execution sandbox's loopback
socket restriction before any model request. Outside that sandbox, an
initial pass took 13.51 s; the recorded repeat enabled verbose runtime
logging to capture the backend. These are single smoke runs, not latency
percentiles. Server warnings about CORS/API key defaults, a model token
type, and slot prefix reuse are retained in the raw log; no deprecated
reasoning-control warning was emitted.

Deterministic TDD: the new launch/adapter expectations failed in 15 cases
before the migration; the full suite then passed **356 tests**, with
three live cases deselected. Ruff lint, format, and diff whitespace checks
passed. The earlier 342-test baseline also passed.

### Native x86-64 Windows

| Field | Value |
|-------|-------|
| Machine | GitHub-hosted `windows-2022` x86-64 runner, Windows Server 2022 Datacenter 10.0.20348; AMD EPYC 9V45, 17,174,360,064 bytes RAM (about 16 GiB); CPU backend |
| Runner image | `20261012.333.1`; [machine record](evidence/2026-10-21-reasoning-controls/windows-machine.json) |
| Runtime | Official b11785 Windows CPU x64 archive, SHA-256 verified; Clang 20.1.8 for Windows x86_64; [banner](evidence/2026-10-21-reasoning-controls/windows-runtime-version.txt) and [archive hash](evidence/2026-10-21-reasoning-controls/windows-runtime-archive-sha256.txt) |
| Python | CPython 3.12.10, pytest 9.1.1 |
| Application | PR head `6ccd7cc5ebfba259835f7473bf2cb00b110fbcf2`, tested merge `4b3eb3db4f2cb8fd777dc254294b30cf6c40cf26`; the subsequent single-commit amendment adds documentation/evidence only |
| Command | `python -m pytest tests/test_llamacpp_live_smoke.py -m live -v -s --basetemp=evidence/tmp --junitxml=evidence/results.xml` |
| Result | **3 passed, 0 skipped in 5.75 s**: both text cases answered `pong`; reasoning absent/present respectively; parsed `get_service_health` with `service=staging-api` |
| Workflow | [Runtime smoke run 39564456074](https://github.com/Last-Humans-Coding-Under-White-Nights/relaymind/actions/runs/39564456074), successful; artifact `runtime-smoke-windows` |
| Raw evidence | [pytest](evidence/2026-10-21-reasoning-controls/windows-pytest.txt), [JUnit](evidence/2026-10-21-reasoning-controls/windows-results.xml), [server](evidence/2026-10-21-reasoning-controls/windows-server.txt), [provenance and tested-file hashes](evidence/2026-10-21-reasoning-controls/windows-provenance.json), [qualified manifest](evidence/2026-10-21-reasoning-controls/qualified-manifest.json) |

The Windows job ran native executables without WSL, Docker, proxy
overrides, or a source rebuild. It used the same manifest launch flags and
adapter settings as macOS. No tool-call parser change or platform-specific
reasoning argument was needed. No deprecated reasoning-control warning
appeared. Other runtime warnings are retained in the server log. These
results qualify this hosted Windows Server/CPU environment; they do not
requalify the earlier contributor's Windows 11 machine, its corporate
proxy, or Windows GPU backends. Text artifact line endings were normalized
to LF and trailing spaces removed when copied into this repository
(also for the macOS logs).

The corresponding [deterministic CI run](https://github.com/Last-Humans-Coding-Under-White-Nights/relaymind/actions/runs/39564455956)
passed on Ubuntu, macOS, and Windows. Both target platforms now have new
non-thinking, thinking, and tool-call smoke evidence for b11785. These
English smoke cases do not establish bilingual skill quality or the M6
latency target.
