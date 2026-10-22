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

Status (2026-10-22): the entries below cover M1 and M2 only. M3 to M5
were closed on deterministic tests with model doubles, and no live run
of their acceptance scenarios (AC02, AC05 to AC07, AC09 to AC14) is
recorded here yet. The missing M5 live evidence, including AC13 and
AC14, is tracked in
[#104](https://github.com/Last-Humans-Coding-Under-White-Nights/relaymind/issues/104)
for M6. AC08 has no live entry, because only one local artifact is
qualified. See [`ROADMAP.md`](ROADMAP.md) for what is carried forward.

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

## 2026-10-21 — Local tokenizer live check, Apple Silicon macOS

Issue #47. Same machine, runtime, artifact, and launch as the M1 entries.

| Field | Value |
|-------|-------|
| Command | `pytest -m live` on branch `feat/routing-probe-tokenizer` (base `c83f4a0`) |
| Result | 7 passed in 4.74 s: the three M1 smoke tests, and `test_token_count_matches_the_runtime_prompt_count` for an English and a Chinese prompt, each with and without the `get_service_health` tool. In all four, `LlamaCppAdapter.count_tokens` equals the prompt tokens llama-server reported for a generation of the same messages and tools |

The test was checked against a broken count: with the tools left out of
the template request it failed for both tool cases (29 counted against
175 reported, 30 against 176), so it detects a count that does not
measure the real prompt.

## 2026-10-21 — M2 routing probe, Apple Silicon macOS

Issue #48. Can the pinned model pick one of the six demo skills (spec
section 9) and produce a valid RouteDecision (spec section 8), in
English and Chinese, with each mechanism spec section 6 allows?

| Field | Value |
|-------|-------|
| Machine | Apple M1, 8 GiB RAM, macOS 27.0.1 (build 26A434), Metal backend |
| Runtime | llama.cpp 0.7.0 (build 11669, commit 620d2225c), Homebrew build, AppleClang arm64 |
| Artifact | `models/qwen3-0.6b-q8_0.manifest.json`: Qwen3-0.6B Q8_0, SHA-256 `9465e63a…bb031`, verified before launch |
| Launch | derived from the manifest: `--ctx-size 8192 --jinja --chat-template-kwargs {"enable_thinking":false}` |
| Command | `python scripts/probes/probe_routing.py scripts/probes/m2_routing.json --out docs/evidence/2026-10-21-m2-routing-apple-m1.json` on branch `feat/routing-probe-tokenizer` (base `c83f4a0`) |
| Probe definition | [`scripts/probes/m2_routing.json`](../scripts/probes/m2_routing.json): the six skill descriptions, twelve prompts (six English, six Simplified Chinese: three simple skill tasks, one missing identifier, one complex diagnostic, one sandbox deletion per language), temperature 0, at most 128 output tokens (spec section 11) |
| Procedure | Verify the artifact, launch llama-server, then per mechanism: three warm-ups with the first prompt, then one pass over the twelve prompts through `LlamaCppAdapter` in `TimingCollector`, sequentially, no other load. Router input is counted with the adapter's local tokenizer (#47) before each request. Every output is judged by the production validator `relaymind.orchestrator.route_decision`; nothing is repaired |
| Raw export | [`evidence/2026-10-21-m2-routing-apple-m1.json`](evidence/2026-10-21-m2-routing-apple-m1.json): `run.rows` holds one row per generation (warm-ups marked) with the raw model text or call, the validated decision or the validation error, grading, router input tokens, and first model action; `run` also records both system prompts, the native function schemas, and the grammar schema |
| Cold start | 0.96 s, launch to the first `/health` 200 (artifact pages warm from SHA-256 verification) |

First model action here is the time from request start to a validated
decision (spec 12.1): the parsed function call for native calls, the end
of the stream for an envelope, "-" when the output did not validate.
The `generations` block of the export has the collector's own metrics;
for envelope generations it has no first model action because no tool
call is emitted, so use the probe's value in `run.rows`.

Mechanisms tried:

- **native**: native function calling, one function per skill (argument
  `language`) plus `request_clarification` (`language`, `question`).
  The gateway maps the call to a RouteDecision: route and reason code
  come from the skill's entry in the probe definition, not from the
  model.
- **envelope**: the model writes the RouteDecision JSON object as text.
- **grammar**: the same envelope prompt with llama-server
  `response_format` `json_schema`: the published RouteDecision schema
  with `skill_id` closed over the six skill ids and null. The pinned
  build accepted it.

Per prompt, measured pass (route / skill / language of the validated
decision; "invalid" when the validator rejected the output; † marks a
grammar decision that the validator of the day accepted and that is
invalid under the spec 0.6 rules, see the correction below):

| Prompt | Expected | Tokens (native / envelope) | Native | ms | Envelope | Grammar | ms |
|--------|----------|---------------------------:|--------|---:|----------|---------|---:|
| Is staging-api healthy right now? | local, check-service-health | 810 / 341 | local, check-service-health, en | 389 | invalid | local, check-service-health, en | 986 |
| What was the latest deployment of payments-api? | local, lookup-latest-deployment | 812 / 343 | local, lookup-latest-deployment, en | 440 | invalid | external, lookup-latest-deployment, en † | 1235 |
| How many incidents did staging-api have in the last 60 minutes? | local, summarize-incident-counts | 818 / 349 | local, summarize-incident-counts, en | 458 | invalid | local, summarize-incident-counts, en | 1075 |
| Is the service healthy? | clarify, no skill | 808 / 339 | invalid (call written as text) | - | invalid | local, health-and-diagnostic-checklist, en | 1068 |
| payments-api got slow after the last deploy. Find out why. | external, investigate-performance | 816 / 347 | external, investigate-performance, en | 413 | invalid | external, investigate-performance, en † | 1149 |
| Delete the demo database demo-orders. | local, delete-demo-database | 810 / 341 | local, delete-demo-database, en | 413 | invalid | local, delete-demo-database, en | 1072 |
| staging-api 现在健康吗？ | local, check-service-health | 813 / 344 | local, check-service-health, **en** | 408 | invalid | local, check-service-health, **en** | 991 |
| payments-api 最近一次部署是什么？ | local, lookup-latest-deployment | 811 / 342 | local, lookup-latest-deployment, **en** | 435 | invalid | external, lookup-latest-deployment, **en** † | 1163 |
| staging-api 在过去 60 分钟内有多少个事故？ | local, summarize-incident-counts | 818 / 349 | local, summarize-incident-counts, **en** | 480 | invalid | local, summarize-incident-counts, **en** | 1061 |
| 这个服务健康吗？ | clarify, no skill | 808 / 339 | parallel, health-and-diagnostic-checklist, **en** | 437 | invalid | local, health-and-diagnostic-checklist, **en** † | 1263 |
| payments-api 在上次部署后变慢了，请查明原因。 | external, investigate-performance | 816 / 347 | external, investigate-performance, **en** | 403 | invalid | external, investigate-performance, **en** † | 1231 |
| 删除演示数据库 demo-orders。 | local, delete-demo-database | 809 / 340 | local, delete-demo-database, zh | 519 | invalid | local, delete-demo-database, **en** | 1024 |

Bold marks a wrong language. Totals per language (exact counts, from
`run.summary`):

| Mechanism | Language | Valid | Route correct | Skill correct | Language correct |
|-----------|----------|------:|--------------:|--------------:|-----------------:|
| native | en | 5/6 | 5/6 | 5/6 | 5/6 |
| native | zh | 6/6 | 5/6 | 5/6 | 1/6 |
| envelope | en | 0/6 | 0/6 | 0/6 | 0/6 |
| envelope | zh | 0/6 | 0/6 | 0/6 | 0/6 |
| grammar | en | 6/6 | 4/6 | 5/6 | 6/6 |
| grammar | zh | 6/6 | 4/6 | 5/6 | 0/6 |

**Correction, 2026-10-21, after spec 0.6.** The validator that judged
this run checked fields and catalog membership only. Spec 0.6 (PR #60)
added the section 8 cross-field rules and the validator now enforces
them. The recorded decision objects in `run.rows` were re-scored under
the new rules without re-running the probe; the export file is
unchanged. Five grammar outputs counted valid above attach a
clarification to a non-clarify route (`json_envelope_grammar` rows 01,
04, 07, 09, and 10, marked † in the table) and are invalid. The native
and envelope rows are unchanged under the context-free rules.
Corrected totals:

| Mechanism | Language | Valid | Route correct | Skill correct | Language correct |
|-----------|----------|------:|--------------:|--------------:|-----------------:|
| native | en | 5/6 | 5/6 | 5/6 | 5/6 |
| native | zh | 6/6 | 5/6 | 5/6 | 1/6 |
| grammar | en | 4/6 (was 6/6) | 3/6 (was 4/6) | 3/6 (was 5/6) | 4/6 (was 6/6) |
| grammar | zh | 3/6 (was 6/6) | 3/6 (was 4/6) | 3/6 (was 5/6) | 0/6 |

Grammar validated 7 of 12, not 12 of 12, and routed 6 of 12 correctly,
not 8 of 12; the seven valid grammar decisions took 986 to 1075 ms. The
run-dependent rules (execution mode, enabled parallel workflow) need a
run context the probe does not have. Under a managed `auto` run without
an operator-enabled parallel workflow, the native `parallel` decision
for "这个服务健康吗？" (row 09) is invalid too, so native zh would read
5/6 valid; that route was already graded wrong. The native result for
the ten prompts that name their target is unchanged: 10 of 10 valid and
correct.

Limits of the recorded numbers, stated 2026-10-21:

- First model action excludes the tokenizer round trips. The probe
  started its clock after `count_tokens` returned, so the
  `/apply-template` and `/tokenize` requests the router pays for every
  prompt are not inside the 389 to 519 ms (native) or the grammar
  times. The probe now starts the clock before the count; future runs
  include that time and are not directly comparable to these numbers.
- Prefix-cache behavior was neither controlled nor measured (spec
  sections 10 and 12.2). The probe did not set llama-server's
  `cache_prompt`, which defaults to on, so the twelve measured prompts
  could reuse the KV cache of the roughly 800-token system-prompt-and-
  functions prefix left by the warm-ups. The warm-up drop from 1226 ms
  to 373 ms on the same prompt is consistent with that reuse; the
  measured numbers are warm-prefix numbers. The export's
  `cached_tokens` is 0 for all 45 generations, but that is the
  adapter's default value: the adapter does not read a cached-token
  count from the runtime's usage, so 0 is not a measurement. The probe
  now sends `cache_prompt` explicitly and records it in the export.
- The 2,048-token router cap (spec 4.1) was measured but not applied.
  Every input was under it (339 to 818 tokens), so no row is affected;
  the probe now marks an over-cap row as bypassed and sends it no
  generation.

Warm-ups: native 1226, 373, 376 ms (the first request carrying the seven
function schemas was the slow one); grammar 942, 934, 923 ms; envelope
warm-ups did not validate.

Observations, not conclusions:

- Skill selection on prompts that name their target: native picked the
  expected skill for 10 of 10 (5 of 5 per language), and every one of
  those calls validated. This is a single pass over a small set, not a
  section 12.3 quality result.
- Missing identifier (RM05): no mechanism produced a clarification for
  "Is the service healthy?" or "这个服务健康吗？". Native wrote the
  clarification call as plain text in English (invalid, so no tool would
  run) and selected the checklist skill in Chinese. Grammar selected the
  checklist skill in both.
- Language: the model's language value was wrong for 5 of 6 Chinese
  prompts with native calls and 6 of 6 with grammar.
- Review: the six Chinese probe prompts, like the Chinese examples of
  the demo skills, have not been natively reviewed. Spec 0.7 section 9
  makes Chinese support best-effort and does not require that review
  (RM16).
- The envelope without grammar never validated: all twelve outputs were
  wrapped in a Markdown code fence, and some were also wrong inside
  (`"route": "simple_skill"`). Grammar fixed the shape as the validator
  of the day checked it (12 of 12; 7 of 12 under the spec 0.6 rules, see
  the correction above) but not the content: it sent the deployment
  lookup to external in both languages, chose reason codes such as
  `local_budget` for a delete, and attached clarifications to routes
  that take none.
- Cost: native first model action 389 to 519 ms; grammar 986 to 1263 ms
  (986 to 1075 ms for the seven that validate under spec 0.6), because
  the model writes the whole object before it is usable. Both exclude
  the tokenizer round trips (see the limits above). Router input stayed
  far under the 2,048-token cap (808 to 818 native, 339 to 349
  envelope).
- The routing decision that follows from this is in
  [`ROADMAP.md`](ROADMAP.md) (M2 stage A).

## 2026-10-21 — M2 Stage A live check-service-health trace, Apple Silicon macOS

Issues #11, #14, #15 (AC01, spec section 13). The full local loop runs
against the pinned model: the router (native function calling, one
function per skill), the check-service-health skill, the tool executor,
and the sandbox data service. Does a simple health request finish
locally, in English and Chinese, with the correct status and zero
external calls?

| Field | Value |
|-------|-------|
| Machine | Apple M1, 8 GiB RAM, macOS 27.0.1 (build 26A434), Metal backend |
| Runtime | llama.cpp 0.7.0 (build 11669, commit 620d2225c), Homebrew build, AppleClang arm64 |
| Artifact | `models/qwen3-0.6b-q8_0.manifest.json`: Qwen3-0.6B Q8_0, SHA-256 `9465e63a…bb031`, verified before launch |
| Launch | derived from the manifest: `--ctx-size 8192 --jinja --chat-template-kwargs {"enable_thinking":false}` |
| Command | `RELAYMIND_TRACE_OUT=docs/evidence/2026-10-21-m2-local-trace-apple-m1.json pytest -m live tests/test_local_loop_live.py` on branch `feat/orchestrator-core` (base `d55d02a`) |
| Prompts | The four paired requests of `skills/check-service-health/examples.json`: both demo services, each in English and Simplified Chinese. One warm-up run with the first prompt, recorded and not graded |
| Configuration | Default budgets (RM03): one routing generation, three further local generations, two tool calls, one repair, four seconds active time, 2,048 router input tokens, 128 routing output tokens. Temperature 0. External dispatch not configured, so any escalation ends as `needs_external` |
| Raw export | [`evidence/2026-10-21-m2-local-trace-apple-m1.json`](evidence/2026-10-21-m2-local-trace-apple-m1.json): every TraceEvent of each run, including model output text and tool calls per generation, tool results, the RouteDecision, budgets, and budget use |
| Result | 3 passed, 1 failed in 17.21 s |

Each run made four local generations: routing, a tool proposal, a
finalize proposal (answer text), and the final generation with tools
disabled (spec section 7.3). Times are single-pass observations from
the trace (`budget_usage.active_ms` of `run.completed` or `run.failed`).

| Prompt | Route (decision language; model's language argument) | Tool call | Final answer | Outcome | Active time |
|--------|------------------------------------------------------|-----------|--------------|---------|------------:|
| Is staging-api healthy right now? (warm-up) | local, check-service-health (en; en) | `get_service_health` `{"service": "staging-api"}` → healthy, 2026-10-20T02:58:00Z | The staging-api is healthy and checked at 2026-10-20T02:58:00Z. | completed (not graded) | 3659 ms |
| Is staging-api healthy right now? | local, check-service-health (en; en) | `{"service": "staging-api"}` → healthy | The staging-api is healthy and checked at 2026-10-20T02:58:00Z. | **completed** | 2356 ms |
| staging-api 现在健康吗？ | local, check-service-health (zh; **en**) | `{"service": "staging-api"}` → healthy | (not published) model wrote: staging-api 现在健康。检查时间是 2026-10-20T02:58:00Z。 | **failed**, `needs_external`, `invalid_output` | 3029 ms |
| Check the health of payments-api. | local, check-service-health (en; en) | `{"service": "payments-api"}` → unavailable, 2026-10-20T02:59:00Z | The payments-api is unavailable at checked_at 2026-10-20T02:59:00Z. | **completed** | 2878 ms |
| 帮我检查一下 payments-api 的健康状态。 | local, check-service-health (zh; zh) | `{"service": "payments-api"}` → unavailable | payments-api 的健康状态为 unavailable，检查时间是 2026-10-20T02:59:00Z。 | **completed** | 2958 ms |

Every run, the failed one included, made zero external calls: each
`model.started` names the local model with actor `local`, and
`run.completed` / `run.failed` record `external_calls: 0` and the list
of model calls (RM01, RM16).

Observations, not conclusions:

- Skill selection: 5 of 5 runs selected check-service-health, and every
  tool proposal was a native call with the right service. Router input
  was 962 to 965 local tokens. That is more than in the #48 probe
  (808 to 818) because the router now advertises the registry's skill
  descriptions, which are longer than the probe's.
- The failed Chinese run is the completion predicate working as
  intended (RM16). The model answered in Chinese but translated the
  status word `healthy` to 健康. The skill requires the returned status
  word unchanged, so `health_status_preserved` rejected the final answer,
  nothing was published, and the run ended as `needs_external` with
  reason `invalid_output`. The other Chinese prompt kept `unavailable`
  and passed. So far one of the two Chinese health traces completes.
- The model's own language argument was wrong for one of the two
  Chinese prompts. The RouteDecision language comes from the request
  text, so both Chinese runs got `zh`.
- Every finalize proposal was written in English, even for Chinese
  requests. The final generation, told the request language, switched to
  Chinese.
- Active local time was 2.4 to 3.0 s in the measured runs, against the
  four-second budget. The final generation took 0.7 to 1.1 s. With
  default budgets, a run that needs a repair generation cannot also
  finish, because routing plus four generations is over the limit.
- The skill-stage prompt layout (skill description and tool instruction
  first, the full skill body as a system message after the request,
  temperature 0) was chosen from development runs earlier the same day,
  made with scratch scripts that were not kept. In those runs, with the
  body first in the system prompt, the model often wrote its tool call
  as JSON text instead of a native call. No numbers from those runs are
  claimed here. This entry is the recorded run.

## 2026-10-21 — M2 Stage A live check-service-health rerun on the rebased stack, Apple Silicon macOS

The same live AC01 test as the entry above, rerun after #59 was rebased
onto the fixed lower PRs (#53, #54, #55) and its review findings were
fixed. The entry above is unchanged; this one records what the same
prompts give now.

| Field | Value |
|-------|-------|
| Machine | Apple M1, 8 GiB RAM, macOS 27.0.1 (build 26A434), Metal backend |
| Runtime | llama.cpp 0.7.0 (build 11669, commit 620d2225c), Homebrew build, AppleClang arm64 |
| Artifact | `models/qwen3-0.6b-q8_0.manifest.json`: Qwen3-0.6B Q8_0, verified against the manifest SHA-256 before launch |
| Launch | derived from the manifest, as above |
| Command | `RELAYMIND_TRACE_OUT=docs/evidence/2026-10-21-m2-local-trace-rebased-apple-m1.json pytest -m live tests/test_local_loop_live.py` on the rebased `feat/orchestrator-core` (base `2879e89`) |
| Prompts | The same four paired requests and the same warm-up run as above |
| Configuration | Default budgets (RM03), temperature 0, external dispatch not wired. A recording external model double is configured; it received no requests in any run |
| Raw export | [`evidence/2026-10-21-m2-local-trace-rebased-apple-m1.json`](evidence/2026-10-21-m2-local-trace-rebased-apple-m1.json) |
| Result | 2 passed, 2 failed in 16.60 s |

| Prompt | Route (decision language; model's language argument) | Final answer or last model text | Outcome | Active time |
|--------|------------------------------------------------------|---------------------------------|---------|------------:|
| Is staging-api healthy right now? (warm-up) | local, check-service-health (en; en) | (not published) finalize proposal and repair both: Staging-api is healthy and checked at 2026-10-20T02:58:00Z. | failed, `needs_external`, `invalid_output` (not graded) | 3264 ms |
| Is staging-api healthy right now? | local, check-service-health (en; en) | (not published) same text as the warm-up | **failed**, `needs_external`, `invalid_output` | 2213 ms |
| staging-api 现在健康吗？ | local, check-service-health (zh; **en**) | (not published) staging-api 现在健康。检查时间是 2026-10-20T02:58:00Z。 | **failed**, `needs_external`, `invalid_output` | 2948 ms |
| Check the health of payments-api. | local, check-service-health (en; en) | The payments-api is unavailable at checked_at 2026-10-20T02:59:00Z. | **completed** | 2834 ms |
| 帮我检查一下 payments-api 的健康状态。 | local, check-service-health (zh; zh) | payments-api 的健康状态为 unavailable，检查时间是 2026-10-20T02:59:00Z。 | **completed** | 3085 ms |

Every run made zero external calls: the external double received no
request, every `model.started` has actor `local` (the locality
`LlamaCppAdapter` declares), and `run.completed` / `run.failed` count
`external_calls: 0` from the recorded calls (RM01, RM16). In the entry
above, `external_calls` and the `local` actor were constants in the code,
so those two fields were not measurements there; the zero-external
claim there rests on no external adapter being configured at all.

Observations, not conclusions:

- The model's output did not change. Every generation the two runs have
  in common produced the same text and the same tool calls as the entry
  above (temperature 0, same runtime and artifact).
- The English staging-api result changed from completed to failed
  because the completion predicate changed, not the model. The model's
  finalize proposal starts with `Staging-api`, capitalized. The earlier
  predicate accepted it. The #55 predicate reads the status stated for
  the exact returned service ID, so a capitalized ID is not that
  service, and the proposal fails. The repair generation repeated the
  same text, so the run ended as `invalid_output` before a final
  generation. In the entry above the final generation wrote `The
  staging-api is healthy ...`, which the current predicate also accepts;
  that generation was not reached here.
- The Chinese staging-api run failed as before: the final answer
  translated `healthy` to 健康.
- So 2 of 4 measured runs now complete locally (one per language, both
  payments-api), against 3 of 4 above. Both are single-pass
  observations of a fixed model on four prompts, not a completion rate.
  Whether the predicate should accept a capitalized service ID at the
  start of a sentence is a question for the predicate owner (#55) and
  the M6 corpus; this rerun does not change it.

## 2026-10-21 — M2 live check-service-health run on the rebuilt stack (#55 974d4f7, #59 333f5ec, #61), Apple Silicon macOS

The same live AC01 test as the two entries above, run once more on the
#61 branch after the stack was rebuilt: #55 now matches service and
database IDs in any case and gives the identifier inputs English and
Chinese labels, #59 uses those labels, and #61 adds the clarification
resume flow and the spec 0.6 stage budgets. The entries above are
unchanged.

| Field | Value |
|-------|-------|
| Machine | Apple M1, 8 GiB RAM, macOS 27.0.1 (build 26A434), Metal backend |
| Runtime | llama.cpp 0.7.0 (build 11669, commit 620d2225c), Homebrew build, AppleClang arm64 |
| Artifact | `models/qwen3-0.6b-q8_0.manifest.json`: Qwen3-0.6B Q8_0, verified against the manifest SHA-256 before launch (given with `RELAYMIND_SMOKE_MODEL`) |
| Launch | derived from the manifest, as above |
| Command | `RELAYMIND_TRACE_OUT=<file> pytest -m live tests/test_local_loop_live.py` on `feat/clarification-repair` (base #59 `333f5ec`); the export's `command` field records the scratch path it was first written to |
| Prompts | The same four paired requests and the same warm-up run as above |
| Configuration | Default budgets (RM03, spec 0.6 section 11), temperature 0, external dispatch not wired. A recording external model double is configured; it received no requests in any run |
| Raw export | [`evidence/2026-10-21-m2-local-trace-stack-rebuilt-apple-m1.json`](evidence/2026-10-21-m2-local-trace-stack-rebuilt-apple-m1.json) |
| Result | 3 passed, 1 failed in 18.14 s |

| Prompt | Route (decision language; model's language argument) | Final answer or last model text | Outcome | Active time |
|--------|------------------------------------------------------|---------------------------------|---------|------------:|
| Is staging-api healthy right now? (warm-up) | local, check-service-health (en; en) | The staging-api is healthy and checked at 2026-10-20T02:58:00Z. | completed (not graded) | 3834 ms |
| Is staging-api healthy right now? | local, check-service-health (en; en) | The staging-api is healthy and checked at 2026-10-20T02:58:00Z. | **completed** | 2521 ms |
| staging-api 现在健康吗？ | local, check-service-health (zh; **en**) | (not published) staging-api 现在健康。检查时间是 2026-10-20T02:58:00Z。 | **failed**, `needs_external`, `invalid_output` | 3133 ms |
| Check the health of payments-api. | local, check-service-health (en; en) | The payments-api is unavailable at checked_at 2026-10-20T02:59:00Z. | **completed** | 2978 ms |
| 帮我检查一下 payments-api 的健康状态。 | local, check-service-health (zh; zh) | payments-api 的健康状态为 unavailable，检查时间是 2026-10-20T02:59:00Z。 | **completed** | 3076 ms |

Every run made zero external calls: the external double received no
request, every `model.started` has actor `local`, and `run.completed` /
`run.failed` count `external_calls: 0` from the recorded calls (RM01,
RM16). Each run is one stage, and `waiting_ms` is 0 in every
`run.completed` / `run.failed`: none of these prompts asks for
clarification.

Observations, not conclusions:

- 3 of 4 measured runs completed locally, against 2 of 4 in the entry
  above. The English staging-api run completed again, but not because
  the case-insensitive service match was used: the model's text
  changed. Its finalize proposal now starts with `The staging-api`, not
  `Staging-api`. The skill-stage prompt is not the same as before: the
  tool input schemas offered to the model now carry `title` and
  `x-relaymind-label` on the `service` property (#55). So the
  case-insensitive match was not exercised by this run.
- The Chinese staging-api run failed as in both entries above: the
  final answer translated `healthy` to 健康, so the predicate refused it.
- This is a single-pass observation of a fixed model on four prompts,
  not a completion rate.
- No live clarification (AC03) run is recorded. AC03 and AC04 are
  checked with model doubles only (`tests/test_ac03_clarification.py`,
  `tests/test_ac04_invalid_proposals.py`).

## 2026-10-21 — Reasoning-control update, llama.cpp b11785 (#58)

RM18, specification 0.8 sections 6 and 14. This is a new M1 runtime
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

## 2026-10-21 — M2 live check-service-health run on the pinned b11785 runtime, Apple Silicon macOS

The same live AC01 test as the three M2 entries above, run on the
runtime the manifest now pins (#58, #63: llama.cpp b11785 with
`--reasoning off`) and on the current code: the hardened #66 completion
predicates (skill packages 0.1.1) and the #67 trace schema, which
requires the model-call inventory and separate active and waiting
timings in every terminal event. The earlier entries used the Homebrew
build 11669 and the old reasoning controls; they are unchanged. This
run is the M2 close-out evidence.

| Field | Value |
|-------|-------|
| Machine | Apple M1, 8 GiB RAM, macOS 27.0.1 (build 26A434), Metal backend |
| Runtime | Official b11785 macOS arm64 archive (`0.7.0-dev (build 11785, commit 8baabd53b)`, AppleClang 21.0.0.21000101), SHA-256 verified against `models/README.md` before use |
| Python | 3.14.7, pytest 9.1.1 |
| Artifact | `models/qwen3-0.6b-q8_0.manifest.json`: Qwen3-0.6B Q8_0, SHA-256 `9465e63a…bb031`, verified before launch (given with `RELAYMIND_SMOKE_MODEL`) |
| Launch | derived from the manifest: `--ctx-size 8192 --jinja --reasoning off --reasoning-format deepseek`; the adapter sends `enable_thinking=false` and `reasoning_format=deepseek` |
| Application | branch `docs/roadmap-m1-closed` on #69 `5ebd317`, master `da33f53` (#67); no source change since |
| Command | `RELAYMIND_TRACE_OUT=docs/evidence/2026-10-21-m2-local-trace-b11785-apple-m1.json pytest -m live tests/test_local_loop_live.py` |
| Prompts | The same four paired requests and the same warm-up run as above |
| Configuration | Default budgets (RM03, spec section 11), temperature 0, external dispatch not wired. A recording external model double is configured; it received no requests in any run |
| Raw export | [`evidence/2026-10-21-m2-local-trace-b11785-apple-m1.json`](evidence/2026-10-21-m2-local-trace-b11785-apple-m1.json) |
| Result | 3 passed, 1 failed in 24.46 s |

| Prompt | Route (decision language; model's language argument) | Final answer or last model text | Outcome | Active time |
|--------|------------------------------------------------------|---------------------------------|---------|------------:|
| Is staging-api healthy right now? (warm-up) | local, check-service-health (en; en) | The staging-api is healthy and checked at 2026-10-20T02:58:00Z. | completed (not graded) | 3573 ms |
| Is staging-api healthy right now? | local, check-service-health (en; en) | The staging-api is healthy and checked at 2026-10-20T02:58:00Z. | **completed** | 2085 ms |
| staging-api 现在健康吗？ | local, check-service-health (zh; **en**) | (not published) staging-api 现在健康。检查时间是 2026-10-20T02:58:00Z。 | **failed**, `needs_external`, `invalid_output` | 2671 ms |
| Check the health of payments-api. | local, check-service-health (en; en) | The payments-api is unavailable at checked_at 2026-10-20T02:59:00Z. | **completed** | 2683 ms |
| 帮我检查一下 payments-api 的健康状态。 | local, check-service-health (zh; zh) | payments-api 的健康状态为 unavailable，检查时间是 2026-10-20T02:59:00Z。 | **completed** | 2619 ms |

Every run made zero external calls: the external double received no
request, every `model.started` has actor `local`, and the `model_calls`
inventory in each `run.completed` / `run.failed` lists exactly four
local calls (routing, two skill generations, final) with
`external_calls: 0` (RM01, RM16, RM22). Each run is one stage with one
tool call; `waiting_ms` is 0 everywhere, since no prompt asks for
clarification. Router input measured 962 to 965 local tokens with six
skills (spec 4.1 cap 2,048). The export was written through the store,
so every event passed the #67 write-time schema validation.

Observations, not conclusions:

- 3 of 4 measured runs completed locally, as in the entry above. Both
  English prompts completed, which is the M2 exit condition (roadmap
  Stage A and B); the Chinese results are best-effort (spec 0.7).
- The Chinese staging-api run failed for the fourth time with the same
  shape: the skill-stage text was English (`The staging-api is currently
  healthy, checked at 2026-10-20T02:58:00Z.`) and the final generation
  rewrote it in Chinese, translating `healthy` to 健康, which the
  predicate refuses. The model's routing language argument was again
  `en` for this prompt; the decision language came from the
  conversation (zh). Model-quality finding for the M6 corpus.
- Active time per measured run was 2.1 to 2.7 s, against 2.5 to 3.1 s
  on build 11669 above. Single pass on four prompts, not a latency
  result; the 500 ms first-response target is measured in M6.
- The routing generation of the warm-up run took 1236 ms; the later
  routing generations took 350 to 497 ms.
- No live clarification (AC03) or invalid-proposal (AC04) run is
  recorded. Both are checked with model doubles only.
