# Roadmap

Implementation order follows the dependency guidance in
[`SPECIFICATION.md`](SPECIFICATION.md) section 14. Each milestone lists the
requirements it implements and the acceptance tests that close it. A
milestone is done when its deterministic tests pass and its live checks are
recorded.

## M1 — Adapter and protocol qualification

Pin the local model artifact and runtime, and prove both adapters work.
[`TESTING.md`](TESTING.md) describes how to exercise the M1 deliverable,
including manual JSON requests from a macOS or Windows terminal.

- Model artifact manifest: source, revision, SHA-256, quantization,
  template hashes, runtime version and commit, launch settings (RM18,
  spec section 6).
- llama.cpp adapter with Qwen3-0.6B in non-thinking mode, verified by a
  smoke test.
- External Chat Completions adapter with operator-configured endpoint.
- Capabilities, generate, cancel, and health on both adapters.
- Basic timing and statistics export: per-generation timings using the
  section 12.1 metric definitions (monotonic clock, RM25) and a JSON
  export, so small and large models can be compared on the same
  prompts from day one. The visual dashboard comes in M4; the formal
  benchmark report in M6.
- Closes: AC08 groundwork.

## M2 — Local skill-and-tool loop

The core demo: local routing, skill loading, tool execution, bounded
budgets.

- Run states and SQLite store (RM17, section 5).
- Skill registry with SKILL.md packages and registry manifest (RM06, RM07).
- Tool registry, validation, and executor with the sandbox demo data
  service (RM08–RM10, section 9).
- Routing decision, local generation loop, budgets, clarification
  (RM01–RM05).
- Closes: AC01, AC03, AC04.

## M3 — Handoff and confirmation

- Handoff envelope and escalation to the external model (RM13, RM14).
- Single dispatch authority; no duplicate completed operations.
- Confirmation flow for the mutating demo tool (RM11), including deny,
  replay, expiry, and altered-arguments cases.
- Prompt-injection test for tool output (RM12).
- Closes: AC02, AC05, AC06, AC07.

## M4 — Public API and observability

- `/v1/models` and `/v1/chat/completions` with the supported subset
  (RM19, RM20).
- Native run API: runs, events SSE, resume, cancel, idempotency (RM21).
- Required trace events (RM22) and the demo trace UI (RM23), including
  the visual timing view over the statistics collected since M1.
- Closes: AC09, AC10, AC11, AC12, AC16.

## M5 — Parallel workflow and caching

- The one bounded parallel workflow with read-only branches (RM15).
- Skill/template caching by content hash; routing cache experiments
  behind a flag, off by default (RM24).
- Closes: AC13, AC14.

## M6 — Evaluation

- Bilingual correctness corpus: 40 scenario pairs, frozen before
  comparison (section 12.2).
- Latency experiment on both target platforms; benchmark report with
  full environment manifest (RM25, section 12.3).
- Closes: AC15 and the release record (section 14).

## Out of scope for the prototype

See the priority table in spec section 1. Highlights: token-level
speculative decoding, production multi-tenancy, native installers, general
PDF ingestion, automatic skill installation.
