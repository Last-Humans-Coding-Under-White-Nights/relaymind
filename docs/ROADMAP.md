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

Status: complete. Passing live smoke tests are recorded in
[`EVIDENCE.md`](EVIDENCE.md) for Apple Silicon macOS and
native x86-64 Windows (#46), using the pinned model and runtime commit
(RM18; spec sections 3, 6, 14). Windows setup differences and the proxy
workaround are recorded with that run.
Automatic loopback proxy bypass is implemented with deterministic coverage in
[#57](https://github.com/Last-Humans-Coding-Under-White-Nights/relaymind/issues/57);
the historical Windows run used a session workaround and predates this fix.
The reasoning-control migration
([#58](https://github.com/Last-Humans-Coding-Under-White-Nights/relaymind/issues/58),
#63) requalified llama.cpp b11785 with `--reasoning off` on both target
platforms; the live runs are recorded in [`EVIDENCE.md`](EVIDENCE.md),
separately from the historical runtime evidence. The M1 milestone is
closed.

What M1 did not test, and M2 therefore must: skill selection among
several descriptions, the RouteDecision envelope, and Chinese input. The
model stays a candidate until the English tool tests (spec section 6);
Chinese results are reported but are not a gate (spec 0.7).

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
  `LlamaCppAdapter.count_tokens` counts through the runtime's own chat
  template and tokenizer; the router (#14) takes that count as its input
  measure.
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
  check-service-health is recorded in English. The Chinese live AC01
  trace is best-effort, recorded but not a gate (spec 0.7).

Status (2026-10-21): **Stage A exit reached.** AC01 passes with model
doubles (`tests/test_ac01_local_health.py`, both services in both
languages). The live check-service-health traces are recorded in
[`EVIDENCE.md`](EVIDENCE.md#2026-10-21--m2-stage-a-live-check-service-health-trace-apple-silicon-macos):
3 of 4 measured runs completed locally with zero external calls (both
English, one Chinese). The other Chinese run translated the status
word, so the completion predicate refused the answer and the run ended
as `needs_external`. That is a model-quality finding for Stage B and
M6, not a met target. A rerun on the rebased stack, recorded in a
second `EVIDENCE.md` entry, completed 2 of 4 with the same model output:
the stricter #55 health predicate refuses the English staging-api
finalize proposal, which starts with a capitalized `Staging-api`. Both
traces are recorded in both languages, so the exit condition holds; the
completion count is open for Stage B. Since that rerun, #55 matches
service and database IDs in any case (they are lowercase by schema), so
`Staging-api is healthy ...` now passes the predicate, and the tool
schemas carry Chinese labels for the clarification question; neither
change had a live run at that point. The b11785 run recorded under
Stage B runs the same code but exercises neither path (no capitalized
ID in the model's text, no clarification asked). The executor (#11), router
(#14), and bounded loop (#15) are in `relaymind.tools.executor`,
`relaymind.orchestrator.router`, and `relaymind.orchestrator.local_loop`.

#### Routing mechanism for #14 (decided 2026-10-21)

Chosen: **native function calling, one function per advertised skill**
(spec section 6 allows it; the mechanism is visible in configuration and
tests). Evidence: the routing probe in
[`EVIDENCE.md`](EVIDENCE.md#2026-10-21--m2-routing-probe-apple-silicon-macos)
(Apple M1, Qwen3-0.6B Q8_0, llama.cpp build 11669, single pass).

- Native calls picked the expected skill for 10 of 10 prompts that named
  their target (5 of 5 in each language), all valid, first model action
  389 to 519 ms (excluding the tokenizer round trips; see the limits in
  `EVIDENCE.md`). Unchanged by the spec 0.6 re-scoring below.
- The JSON envelope without grammar produced 0 of 12 valid outputs
  (every one wrapped in a code fence). With grammar-constrained decoding
  it was judged 12 of 12 valid on the day but 8 of 12 routes correct, at
  986 to 1263 ms. Corrected 2026-10-21 under the spec 0.6 cross-field
  rules, re-scored from the recorded export (correction note in
  `EVIDENCE.md`): 7 of 12 valid and 6 of 12 routes correct, because
  five grammar outputs attached a clarification to a non-clarify route.
  The envelope is not used for routing. The correction widens the gap
  between the mechanisms; the decision stands.

How the router uses it, so the model is trusted only for the skill
choice:

- The gateway builds the RouteDecision. The selected function names the
  skill; route and reason code come from that skill's registry entry
  (local eligibility, external-required, parallel-workflow ID; spec
  4.2, RM04), not from the model. The result goes through the boundary
  validator (`relaymind.orchestrator.route_decision`) with the run's
  `RoutingContext` (catalog ownership, effective execution mode,
  operator-enabled parallel workflows) before anything else happens, so
  the spec 0.6 cross-field rules are checked there. Parallel
  admissibility is the gateway's call (spec 4.4): the one native
  `parallel` decision in the probe (the Chinese missing-identifier
  prompt) is invalid in any mode that does not admit the workflow.
- Missing identifiers are not left to the model (RM05). Neither prompt
  without a service ("Is the service healthy?", "这个服务健康吗？") got a
  clarification from any mechanism. The router checks the selected
  skill's required identifiers against the request and conversation
  context, and returns clarify with `missing_input` when one is absent.
  No tool runs on a decision that fails this check.
- The model's language value is not used alone: it was wrong for 5 of 6
  Chinese prompts. Done in #14: the RouteDecision language is set from
  the conversation (Chinese characters in the latest user turn that has
  words, so an identifier-only reply such as `staging-api` keeps the
  language), checked by tests in both languages. Spec 0.6 (section 5)
  fixes the run language at the first RouteDecision; the loop uses that
  decision's language for every later step.
- Router input measured 808 to 818 local tokens with six skills, well
  under the 2,048 cap; the bypass path is still required for longer
  inputs (spec 4.1).

Skill selection was reliable in both languages for well-specified
prompts, so the narrower-scope path of spec section 12.3 is not taken
now. If the M2 live traces or the M6 corpus show it failing, that path
(fewer skills per prompt, or another candidate model, never relabeled
as Qwen3-0.6B) is the fallback.

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

Status (2026-10-21): **M2 closed.** AC01, AC03, and AC04 pass with
model doubles (`tests/test_ac01_local_health.py`,
`tests/test_ac03_clarification.py`, `tests/test_ac04_invalid_proposals.py`).
The clarification and resume behavior follows spec 0.6 sections 5, 8,
and 11 (PR #60, now spec 0.7 with the same text): a resume opens a new
local stage within the entire-run limits, the run keeps its first
language, and a third clarification fails the run with `missing_input`.
Invalid proposals (#17) never execute, get at most one repair inside the
generation limit, and every escalation carries a section 8 reason code.
The required trace events (#27, PR #67) are complete: terminal events
carry the model-call inventory and separate active and waiting timings,
large tool results are stored as redacted artifacts, and every event is
schema-checked when written. The live AC01 run that closes the
milestone is recorded in
[`EVIDENCE.md`](EVIDENCE.md#2026-10-21--m2-live-check-service-health-run-on-the-pinned-b11785-runtime-apple-silicon-macos)
on the pinned runtime (llama.cpp b11785, `--reasoning off`), the #66
predicates, and the #67 trace schema: 3 of 4 runs completed locally with
zero external calls, both English prompts among them, so the exit
condition holds on the stack M3 starts from. The three earlier live runs
used build 11669 and remain as history. Master CI went red on Windows
after #67 because the tool-budget test gave routing, persistence, and
the tool a shared 0.3 s wall-clock budget; #69 drives that time with the
fake clock. The runtime enforced the budget correctly.

Carried forward, not M2 scope:

- `confirmation.required`, `handoff.started`, `run.cancelled`, and
  `cache.hit` have state and store contract coverage (#67); their
  execution flows are M3 (#18, #19), M4 (#29), and M5 (#31).
- AC03 and AC04 are deterministic gates with model doubles. There is no
  live AC03 or AC04 test.
- The resume API exists only on `LocalOrchestrator`. The HTTP route
  `POST /relay/v1/runs/{id}/resume` (section 7.4) is part of the native
  run API in M4 (#25, #26).
- Waiting runs are kept in process memory. After a restart, they are
  failed as `interrupted` by restart recovery (#8, M3), which the
  service must call once at startup.

Informational, not blocking (spec 0.7: Chinese support is best-effort):

- Chinese live AC01 is best-effort, recorded but not a gate. The
  staging-api run failed in all four recorded runs, because the final
  answer translates the status word. That is a model-quality finding
  for the M6 corpus.
- #13: the Chinese skill examples have not been natively reviewed.
  Spec 0.7 section 9 no longer requires that review; the evidence says
  the examples are not reviewed.

## M3 — Handoff and confirmation

- Handoff envelope and escalation to the external model (#18; RM13,
  RM14): single dispatch authority, revoked on escalation; no duplicate
  completed operations.
- Confirmation flow for the mutating demo tool (#19; RM11), including
  deny, replay, expiry, and altered-arguments cases.
  Implemented with model doubles (`tests/test_ac05_confirmation.py`,
  spec 0.10); no live run.
- Restart recovery: interrupted runs and unknown outcomes (#8; RM10,
  RM17, section 5), moved here from M2 because it only matters once
  mutations exist. Part of AC12, which closes in M4 together with #29.
  Implemented with a new gateway over the same store file
  (`tests/test_restart_recovery.py`, spec 0.11); the startup call that
  runs it belongs to the API work (#25).
- Prompt-injection test for tool output and secret redaction (#20;
  RM12).
- Failure behavior and error codes (#21; RM04, section 11): the error
  contract (`relaymind.api.errors`, `relaymind.api.auth`: one status and
  stable code per failure, the mapping of failed runs, no internal
  detail in a response) and degraded mode as an operator policy, off by
  default and recorded in the trace (spec 0.13). AC07 and the error part
  of AC16 pass with model doubles (`tests/test_ac07_failure_behavior.py`,
  `tests/test_error_contract.py`, `tests/test_degraded_mode.py`); the
  HTTP transport that serves the contract is the M4 API work.
- AC08, orchestrator and skill half (#49; RM18): the same orchestrator and
  skill packages across a local and an external swap. Implemented with
  scripted endpoints behind the real adapters
  (`tests/test_ac08_model_substitution.py`); no live variant, because
  only one local artifact has a manifest. AC08 asks for a second
  qualified local model, so it stays open until a second artifact is
  qualified (spec section 6) and the swap runs against it.
- Closes: AC02, AC05, AC06, AC07. AC08 is not closed (see above).

## M4 — Public API and observability

- `/v1/models` and `/v1/chat/completions` with the supported subset
  (#22, #23, #24; RM19, RM20), including the `X-RelayMind-Execution-Mode`
  header that selects auto, local_only, external_only, or parallel_demo
  from a standard chat client (spec 0.4, section 7.1). parallel_demo
  only has a workflow to run once M5 lands.
  #22 is implemented with model doubles (`tests/test_chat_request.py`,
  `tests/test_chat_endpoint.py`, `tests/test_openai_sdk.py`, spec 0.14):
  the two endpoints, the parameter table, the execution mode header and
  policy, the loopback bind, and the generated token. The
  `relaymind-client-tools` alias is served only when a client-owned
  backend is configured, which is #24, so the gateway lists only what it
  can run. `Idempotency-Key` is rejected until #26 implements it.
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

- Correctness corpus: 40 English scenarios, with best-effort Chinese
  versions reported per language, frozen before comparison (#32;
  section 12.2, spec 0.7).
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
- 2026-10-21, after the routing probe (#48): native function calling
  chosen as the routing mechanism for #14; route and reason code come
  from the skill registry, and missing-identifier and language checks
  are done by the gateway rather than taken from the model.
- 2026-10-21, after spec 0.6 (#60): the probe's grammar results were
  re-scored under the new RouteDecision cross-field rules, 7 of 12 valid
  instead of 12 of 12 and 6 of 12 routes correct instead of 8 of 12; the
  native result and the mechanism decision are unchanged. The recorded
  numbers exclude tokenizer round trips and ran with the runtime's
  prompt cache on; both are stated in `EVIDENCE.md`.
- 2026-10-21, Stage A exit: the skill-stage prompt puts the skill
  description and the tool to call first and the full skill body after
  the request, at temperature 0, because the pinned model often wrote
  its tool call as plain text when the body came first. The final
  generation is told the request language. Chinese answers that
  translate the returned status word fail the completion predicate; that
  goes to Stage B and the M6 corpus.
- 2026-10-21, Stage B (#16, #17): an earlier draft of #61 decided the
  resume rules in this log. In that draft, a resume after a model
  clarification was charged to the run's one routing generation, so it
  stopped with `local_budget`. Spec 0.6 sections 5, 8, and 11 (#60) now
  define the resume behavior, and they supersede that entry: per-stage
  budgets, entire-run limits, the clarification.answered event, and
  `invalid_resume_input`. The code follows the spec. Nothing about
  resume is decided here.
- 2026-10-22, #49: AC08 no longer closes in M3. The orchestrator and
  skill swap passes with scripted endpoints, but the spec asks for a
  second qualified local model, and only one artifact is qualified.
