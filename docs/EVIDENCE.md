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

Same machine, runtime, artifact, and launch as above. A throwaway script
launched llama-server from the manifest, wrapped `LlamaCppAdapter` in
`relaymind.stats.TimingCollector`, sent three warm-up requests, then one
pass over six plain prompts and six tool-call prompts (three English and
three Simplified Chinese each), sequentially, with no other load. Raw
export: [`evidence/2026-10-20-m1-timing-apple-m1.json`](evidence/2026-10-20-m1-timing-apple-m1.json).

Cold start, process launch to `/health` 200: 1.11 s.

Tool-call prompts (system prompt plus one `get_service_health` schema;
first model action is the parsed tool call, spec section 12.1):

| Prompt | First model action | Proposal |
|--------|-------------------:|----------|
| Is staging-api healthy right now? | 526 ms | `get_service_health` `{"service": "staging-api"}` |
| Check the health of payments-api. | 430 ms | `get_service_health` `{"service": "payments-api"}` |
| Is payments-api up? | 417 ms | `get_service_health` `{"service": "payments-api"}` |
| staging-api 现在健康吗？ | 434 ms | `get_service_health` `{"service": "staging-api"}` |
| 检查 payments-api 的健康状况。 | 384 ms | `get_service_health` `{"service": "payments-api"}` |
| payments-api 正常运行吗？ | 416 ms | `get_service_health` `{"service": "payments-api"}` |

Plain prompts (no tools; first answer token and completion):

| Prompt | First answer token | Completion | Answer |
|--------|-------------------:|-----------:|--------|
| Reply with exactly one word: pong | 23 ms | 38 ms | pong |
| What is the capital of France? Answer in one word. | 48 ms | 68 ms | Paris |
| Say hello in one short sentence. | 451 ms | 581 ms | Hello! How can I help you today? |
| 只用一个词回答：pong | 41 ms | 57 ms | pong |
| 法国的首都是哪里？用一个词回答。 | 38 ms | 55 ms | 巴黎 |
| 用一句话打个招呼。 | 43 ms | 174 ms | 你好！有什么我可以帮助你的吗？ |

Observations, not conclusions:

- Six of six tool-call prompts produced the correct tool and argument,
  in both languages, with one tool offered. This is the first Chinese
  tool-call evidence for the candidate. It says nothing yet about
  selecting among several skills (#48).
- First model action for a tool call sat between 384 and 526 ms on this
  machine, single pass. The 500 ms hypothesis (p95, warmed, concurrency
  one) is neither met nor missed by this: it needs the section 12.2
  protocol in M6. One of six observations exceeded 500 ms.
- Tool-call output arrives from llama-server as one chunk after the
  arguments are complete, so first transport event equals first model
  action for those prompts. Plain text streams from the first token.
- The 451 ms first token on "Say hello in one short sentence." against
  23 to 48 ms for the others is unexplained; a single pass cannot
  separate a sampling effect from a runtime one.
