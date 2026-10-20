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

Not covered by this run: x86-64 Windows (#46), Chinese prompts, more than
one tool, and skill selection among several descriptions (#48).

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
