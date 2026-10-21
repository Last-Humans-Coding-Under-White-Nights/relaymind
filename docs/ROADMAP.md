# Roadmap

Implementation order follows the dependency guidance in
[`SPECIFICATION.md`](SPECIFICATION.md) section 14. Each milestone lists the
requirements it implements and the acceptance tests that close it. A
milestone is done when its deterministic tests pass and its live checks are
recorded in [`EVIDENCE.md`](EVIDENCE.md). Issues on GitHub carry an `M1`
to `M6` label; the lists below name the issues where the order matters.

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
- Closes: AC08 groundwork (adapter layer, #43). The orchestrator and
  skill half of AC08 is #49 in M3.

Status: code complete; the live smoke test is recorded on Apple Silicon
macOS. Open before M1 closes:

- Windows qualification (#46): the same live smoke test on an x86-64
  Windows machine, recorded in `EVIDENCE.md`. The platform matrix is
  both platforms (spec section 3).

What M1 did not test, and M2 therefore must: skill selection among
several descriptions, the RouteDecision envelope, and Chinese input. The
model stays a candidate until the bilingual tool tests (spec section 6).

## M2 — Local skill-and-tool loop

The core demo: local routing, skill loading, tool execution, bounded
budgets. The work runs in two stages so the project's largest open risk,
whether a 0.6B model can route at all, is tested before the
infrastructure around it is built.

### Stage A — Routing probe and vertical slice

- Live routing probe (#48; RM02): a throwaway spike that feeds the six
  skill descriptions and bilingual prompts to the pinned model through
  the M1 adapter and timing collector, tries the two mechanisms spec
  section 6 allows for local control (native function calling and a
  validated structured envelope; the envelope is tried with and without
  grammar-constrained decoding), and records the result in
  `EVIDENCE.md`. Its outcome selects the routing mechanism for #14 and
  is documented here.
- Local tokenizer (#47): router input is capped at 2,048 local tokens
  (spec section 4.1); the adapter needs a token count for it.
- Run states and SQLite store, first increment (#6, #7; RM17,
  section 5): runs, events, tool_operations.
- Required trace events (#27; RM22, section 8), recorded while the loop
  is built. AC01 needs the trace to prove zero external calls (RM01), so
  events cannot wait for M4. SSE exposure and the UI stay in M4.
- One skill end to end: check-service-health with get_service_health
  against the sandbox data service, the first increment of #9, #10,
  #11, #12, #13 (RM06, RM08–RM10, RM23).
- Router (#14) and local generation loop with recorded budgets (#15;
  RM01–RM03).
- Exit: AC01 passes with doubles, and the live trace for
  check-service-health is recorded in both languages.

### Stage B — Widen

- Remaining skill packages and tools (#12, #13): lookup-latest-deployment,
  summarize-incident-counts, investigate-performance, and the read-only
  tools behind them. delete-demo-database and
  health-and-diagnostic-checklist are registered but exercised in M3
  and M5.
- Full skill and tool registries (#9, #10; RM06–RM08): registry manifest,
  permission intersection, resource-path restriction, version and hash
  invalidation.
- Clarification for missing identifiers (#16; RM05).
- Invalid proposals: bounded repair and explicit escalation (#17; RM02,
  RM08, RM09).
- Closes: AC01, AC03, AC04.

## M3 — Handoff and confirmation

- Handoff envelope and escalation to the external model (#18; RM13,
  RM14): single dispatch authority, revoked on escalation; no duplicate
  completed operations.
- Confirmation flow for the mutating demo tool (#19; RM11), including
  deny, replay, expiry, and altered-arguments cases.
- Restart recovery: interrupted runs and unknown outcomes (#8; RM10,
  RM17, section 5), moved here from M2 because it only matters once
  mutations exist. Part of AC12, which closes in M4 together with #29.
- Prompt-injection test for tool output and secret redaction (#20;
  RM12).
- Failure behavior and error codes (#21; RM04, section 11).
- AC08, orchestrator and skill half (#49; RM18): the same orchestrator and
  skill packages across a local and an external swap.
- Closes: AC02, AC05, AC06, AC07, AC08.

## M4 — Public API and observability

- `/v1/models` and `/v1/chat/completions` with the supported subset
  (#22, #23, #24; RM19, RM20), including the `X-RelayMind-Execution-Mode`
  header that selects auto, local_only, or external_only from a standard
  chat client (spec 0.4, section 7.1).
- Native run API: runs, events SSE, resume, cancel, idempotency (#25,
  #26; RM21). SSE exposes the events recorded since M2.
- Cancellation during generation and in-flight writes (#29; RM10, RM14,
  RM17); closes AC12 together with #8.
- Demo trace UI (#28; RM23), including the visual timing view over the
  statistics collected since M1.
- Closes: AC09, AC10, AC11, AC12, AC16.

## M5 — Parallel workflow and caching

- The one bounded parallel workflow with read-only branches (#30; RM15).
- Skill/template caching by content hash; routing cache experiments
  behind a flag, off by default (#31; RM24).
- Closes: AC13, AC14.

## M6 — Evaluation

- Bilingual correctness corpus: 40 scenario pairs, frozen before
  comparison (#32; section 12.2).
- Latency experiment on both target platforms; benchmark report with
  full environment manifest (#33; RM25, section 12.3).
- Closes: AC15 and the release record (section 14).

## Out of scope for the prototype

See the priority table in spec section 1. Highlights: token-level
speculative decoding, production multi-tenancy, native installers, general
PDF ingestion, automatic skill installation.

## Plan changes

The plan is adjusted when a milestone teaches something. Each change is
listed here so the current order can be traced to its reason.

- 2026-10-20: timing and statistics export moved from M6 into M1, so
  adapters can be compared from the first live run.
- 2026-10-20, after M1: M2 split into a routing probe plus vertical
  slice (stage A) and a widening (stage B); required trace events (#27)
  moved from M4 to M2; restart recovery (#8) moved from M2 to M3; Windows
  qualification (#46) added to M1; the orchestrator half of AC08 (#49)
  added to M3; live evidence recorded in `EVIDENCE.md`.
