# Roadmap

Implementation order follows the dependency guidance in
[`SPECIFICATION.md`](SPECIFICATION.md) section 14. Each milestone lists the
requirements it implements and the acceptance tests that close it. A
milestone is done when its deterministic tests pass and its live checks are
recorded in [`EVIDENCE.md`](EVIDENCE.md). Issues on GitHub carry an `M1`
to `M9` label; the lists below name the issues where the order matters.
M1 to M6 are the prototype; M7 to M9 are phase 2 (spec 0.31, revised
for OpenClaw integration in spec 0.47), the
answer to customer feedback, with shorter waiting time as the objective
(see
[`design/2026-10-23-performance-first-routing.md`](design/2026-10-23-performance-first-routing.md)
for the reasoning).

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
  (`tests/test_restart_recovery.py`, spec 0.11). The API work (#25) did
  not add a startup call; the operator launcher (#87, see M5) runs it
  once at startup.
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

Status (2026-10-22): **M3 closed** (PRs #71 to #76, spec 0.9 to 0.13).
Handoff (#18), confirmation (#19), restart recovery (#8), tool-output
injection defenses with repeated-secret redaction (#20), the error
contract and degraded mode (#21), and the AC08 orchestrator and skill
swap (#49) are merged. AC02, AC05, AC06, and AC07 pass with model
doubles (`tests/test_ac02_handoff.py`, `tests/test_ac05_confirmation.py`,
`tests/test_ac06_tool_output_injection.py`,
`tests/test_ac07_failure_behavior.py`). The milestone was closed on
deterministic coverage only: no live run of an M3 acceptance scenario is
recorded in [`EVIDENCE.md`](EVIDENCE.md), which is less than the rule at
the top of this page asks.

Carried forward, not done in M3:

- Live-model runs for AC02, AC05, AC06, and AC07 (#88). These now ran
  live on macOS with a live external endpoint
  ([`EVIDENCE.md`](EVIDENCE.md#2026-10-22--live-acceptance-runs-through-relaymind-serve---config-88-104-apple-silicon-macos)).
  Gateway checks held; the AC02 run failed on a model finding (the
  external answer missed the skill's section labels), and no live
  approve that deletes is recorded for AC05.
- AC08: the second local artifact and the live local swap are done in
  M6 (#90). A live external swap ran (#88) between two models of one
  endpoint, which spec 0.29 does not count as a second endpoint. The
  swap to a second compatible endpoint, Z.ai to bigmodel.cn with only
  the `[external_model]` table changed, ran live in M6 and closes AC08
  ([#109](https://github.com/Last-Humans-Coding-Under-White-Nights/relaymind/issues/109)).
  Caveat: both endpoints are Zhipu's (international and mainland
  China platforms), so a second vendor's API is not covered.
- Restart recovery exists as `LocalOrchestrator.recover_after_restart`,
  but nothing called it at startup, because there was no launcher. The
  operator launcher (#87, see M5) now calls it once before serving.
- Recovery always marks an in-flight write `unknown_outcome`: the MVP
  has no source to reconcile a write against.
- One approval race is open (#93): a second process sharing the store
  could take the execution slot between the slot check and the resume.
  In one process nothing awaits in that window.

## M4 — Public API and observability

- `/v1/models` and `/v1/chat/completions` with the supported subset
  (#22, #23, #24; RM19, RM20), including the `X-RelayMind-Execution-Mode`
  header that selects auto, local_only, external_only, or parallel_demo
  from a standard chat client (spec 0.4, section 7.1). parallel_demo
  runs the parallel workflow since M5 (#30).
  #22 is implemented with model doubles (`tests/test_chat_request.py`,
  `tests/test_chat_endpoint.py`, `tests/test_openai_sdk.py`, spec 0.14):
  the two endpoints, the parameter table, the execution mode header and
  policy, the loopback bind, and the generated token. The
  `relaymind-client-tools` alias is served only when a client-owned
  backend is configured, which is #24, so the gateway lists only what it
  can run. `Idempotency-Key` was rejected until #26 implemented it.
  #23 is implemented with model doubles (`tests/test_streaming_contract.py`,
  spec 0.15): append-only buffered streams, a live-stream seam
  (`LiveAnswer`) and its mid-stream failure behavior. The backend that
  forwards a live provider stream and records `run.failed` is not part
  of #24 (see below).
  #24 is implemented with model doubles (`tests/test_client_owned_tools.py`,
  spec 0.16): the `relaymind-client-tools` backend returns tool calls to
  the caller and never executes them. It buffers and validates a whole
  generation, so a live provider stream (`LiveAnswer`) has no production
  backend yet and the mid-stream failure path stays tested with a double.
- Native run API: runs, events SSE, resume, cancel, idempotency (#25,
  #26; RM21). SSE exposes the events recorded since M2.
  #25 is implemented with model doubles (`tests/test_native_run_api.py`,
  spec 0.17): the six endpoints, `/healthz` and `/readyz`, Last-Event-ID
  replay that reads the store only, and `parallel_demo` failing with
  `workflow_unavailable` when its configuration is missing (the workflow
  itself is #30). In-flight write cancellation is #29.
  #26 is implemented with model doubles (`tests/test_idempotency.py`,
  spec 0.18): `Idempotency-Key` on native creation and chat, stored with
  the run in one transaction, with 409 for a changed payload, the stored
  response (also as stream chunks) for a completed chat duplicate, and
  keys leaving with their run at the seven-day limit.
- Cancellation during generation and in-flight writes (#29; RM10, RM14,
  RM17); closes AC12 together with #8. Implemented with model doubles
  (`tests/test_ac12_cancellation.py`, spec 0.19); no live run.
- Demo trace UI (#28; RM23), including the visual timing view over the
  statistics collected since M1.
  #28 is implemented with model doubles (`tests/test_demo_ui*.py`, spec
  0.20): a static page served by the gateway, rendered traces built from
  stored events, persistent live, replay, or double labels, confirmation
  and clarification through the native resume endpoint, and the
  comparison on reset fixtures. The page script is not run by an automated
  browser test, and no live run is recorded.
- Closes: AC09, AC10, AC11, AC12, AC16.

Status (2026-10-22): **M4 closed** (PRs #79 to #85, spec 0.14 to 0.20).
The compatible endpoints (#22), the streaming contract (#23), the
client-owned tools alias (#24), the native run API (#25), idempotency
keys (#26), cancellation during generation and in-flight writes (#29),
and the demo trace UI (#28) are merged. AC09, AC10, AC11, AC12 (with
#8), and the request and error parts of AC16 pass with model doubles;
[`TESTING.md`](TESTING.md) lists the tests. As in M3, the milestone was
closed on deterministic coverage only, and no live run is recorded.

Carried forward, not done in M4:

- Live-model runs for AC09, AC10, AC11, and AC12, and a UI session in
  live mode (#88). These now ran live on macOS
  ([`EVIDENCE.md`](EVIDENCE.md#2026-10-22--live-acceptance-runs-through-relaymind-serve---config-88-104-apple-silicon-macos)).
- An operator launcher (#87). Nothing wired models, store, token,
  `ChatSettings`, `NativeSettings`, and `UiSettings` into a start
  command, and `pyproject.toml` had no entry point. This is now done in
  M5: `relaymind serve --config` starts the gateway and runs restart
  recovery once at startup (see below).
- An automated browser test for the UI page script (`app.js`; #89).
  The browser timestamp of a UI submission is shown but not asserted.
  This is now done in M5: spec 0.27 (see below).
- The requested answer options (token budget, temperature, top_p) were
  applied but not recorded in `run.accepted`; that needed a trace schema
  change (#91). This is now done in M5: spec 0.22 records them (see
  below).
- A chat disconnect now cancels the run the request owns (spec 0.24
  section 7.4; #92). A write cannot be in flight under a chat request,
  because a chat reply cannot approve it, so the write settlement is
  covered by the native cancel tests only. No production backend
  forwards a live provider stream yet (`LiveAnswer` is tested with a
  backend double).
- `cache.hit` execution was left to #31 (done in M5). (`parallel_demo` runs the
  workflow since #30; see M5.)
- The UI has no run listing (runs are opened by ID), and the comparison
  pairing is held by the page, not stored.
- A write in flight at a cancel is recorded as `unknown_outcome` even if
  it finishes a moment later; its late result is dropped, not kept as
  evidence.

## M5 — Parallel workflow and caching

- The one bounded parallel workflow with read-only branches (#30; RM14,
  RM15, RM16, RM22, RM23; spec 0.25 section 4.4): `parallel_demo` runs a
  local health branch and an external checklist branch, joins them, and
  hands final ownership to a new external call. Branches are attributable
  in the trace and the UI, the extra call latency is reported in the
  terminal event, abandoned calls count in the external usage, and cancel
  and restart settle both branches and the synthesis. `/readyz` reports
  `parallel_demo` ready when the workflow is enabled and both models are
  ready. Implemented with model doubles (`tests/test_parallel_workflow.py`);
  not yet run against live models, so no latency figure is claimed.
- Browser test of the demo UI page script (#89; RM11, RM12, RM21, RM23,
  RM25, spec 0.27 section 9): `tests/test_demo_ui_browser.py` runs
  `app.js` in Playwright Chromium against the real app over model
  doubles. New dev dependency `playwright`; CI installs Chromium on all
  three runners.
- Caching policy (#31; RM07, RM22, RM24, AC13, spec 0.26 section 10):
  skill parsing and resource text are cached by content hash
  (`SkillRegistry.reloaded` re-reads the roots; unchanged files are not
  parsed again). The routing-result cache is behind `cache.routing` in the
  launcher file, off by default and in the benchmark configuration. It
  stores only the skill choice of a local read-only route (five minutes,
  256 entries); a hit skips the routing generation and emits `cache.hit`
  with the originating version, while authorization and tools run fresh.
  A skill or permission change drops dependent entries. Runs and exported
  statistics are labeled cache on or off. Implemented with model doubles;
  no live run and no speedup is claimed.
- Operator launcher (#87; RM17, RM18, RM19, RM21, RM23, spec 0.21
  section 14.1): `relaymind serve --config relaymind.toml` replaces the
  hand-written start script. It wires the models, skill roots, sandbox,
  run store, settings, and policy, refuses an invalid configuration
  before any socket opens, runs restart recovery once, and prints the
  application token once. Implemented with model doubles
  (`tests/test_launcher.py`); not yet run against a live model.
- Requested answer options in `run.accepted` (#91; RM19, RM22, RM25,
  spec 0.22 section 8): both chat aliases record the effective token
  budget, temperature, and top_p of the answer; a run without options and
  a native run record none; a run stored earlier shows as not recorded in
  the demo UI. Implemented with model doubles; no live run.
- Approval resume race (#93; RM10, RM11, RM17, spec 0.23 section 5):
  consuming the approval and taking the execution slot share one store
  transaction. If another gateway takes the slot, the resume returns
  429 without consuming the approval; the same approval works once the
  slot is free. Covered with two store connections and model doubles in
  `tests/test_ac05_confirmation.py`.
- Chat disconnect cancellation (#92; RM14, RM17, RM21, spec 0.24
  section 7.4): a disconnect cancels the run that the chat request owns,
  including during buffered generation. It leaves unrelated native runs
  alone; a native SSE disconnect does not cancel its run. Implemented
  with model doubles (`tests/test_chat_disconnect_cancel.py`); no live
  run is recorded.
- Closes: AC13, AC14.

Status (2026-10-22): **M5 closed** (PRs #97, #95, #96, #99, #100, #101,
and #102, spec 0.21 to 0.27). The GitHub milestone is closed
(2026-10-23), with all 16 of its issues closed. The operator launcher (#87), the recorded
answer options (#91), the approval resume race (#93), the chat
disconnect cancel (#92), the parallel workflow (#30), the caching policy
(#31), and the browser test of the page script (#89) are merged. AC13
and AC14 pass with model doubles (`tests/test_routing_cache*.py`,
`tests/test_skill_parse_cache.py`, `tests/test_launcher_cache.py`,
`tests/test_parallel_workflow.py`);
[`TESTING.md`](TESTING.md) lists the tests. As in M3 and M4, the
milestone was closed on deterministic coverage only: no live run of an
M5 scenario is recorded in [`EVIDENCE.md`](EVIDENCE.md), which is less
than the rule at the top of this page asks. The live evidence is #104
in M6. Update (2026-10-22, #104): the launcher started with the pinned
model on macOS eight times and once more after a kill; a cache-on run
of the AC01 prompts is recorded next to its cache-off run (4 hits and 4
misses, each hit without a routing call); and `parallel_demo` ran twice
against `glm-4.6` (one completed run with an 802 ms local branch, a
28.0 s external branch, and a 13.9 s synthesis call; one run whose draft
reached the 30 s external limit), in
[`EVIDENCE.md`](EVIDENCE.md#2026-10-22--live-acceptance-runs-through-relaymind-serve---config-88-104-apple-silicon-macos).
Update (2026-10-23, #110): the launcher started with the pinned model
on x86-64 Windows five times (5.25 s to 10.23 s to `/healthz`, `/readyz`
200 on all starts), and the latency benchmark ran on the same machine
(p95 first model action 3610 ms, 0 timeouts, 76 of 120 samples ended
other than their label), in
[`EVIDENCE.md`](EVIDENCE.md#2026-10-23--launcher-start-and-latency-benchmark-on-x86-64-windows-110-ac15-native-windows).
No external model was available, so 7 of 22 acceptance scenarios are
`not_run`. The UI session errored (Playwright Chromium not installed).

The `parallel_demo` runs, cache-on runs, and launcher starts remain
single-pass observations; they do not measure a cache speedup. The
Windows latency benchmark is recorded separately in M6 below.

Carried forward, not done in M5:

- The launcher reads the external credential from the environment
  variable the config file names; it does not read an OS credential
  store directly (#87).
- Runs stored before spec 0.22 have no `answer_options`; the store was
  not migrated, and the reader treats the field as optional (#91).
- The approval resume race is closed by the single store transaction;
  no lock refuses a second gateway on the same store file (#93).
- A chat disconnect during an in-flight write is covered by the native
  cancel tests only; there is no chat-level test for an in-flight write
  (#92). Both chat aliases have dedicated disconnect tests in
  `tests/test_chat_disconnect_cancel.py`.
- The skill parse cache is process-local and used on registry load and
  `SkillRegistry.reloaded()` only; a running gateway does not watch
  skill files (#31).
- The browser test drives Chromium only (#89).

## M6 — Evaluation

- Correctness corpus: 40 English scenarios, with best-effort Chinese
  versions reported per language, frozen before comparison (#32;
  section 12.2, spec 0.7 and 0.28). Built and run: the frozen corpus,
  its runner, and one full live run on Apple Silicon macOS (156 runs in
  three modes, [`EVIDENCE.md`](EVIDENCE.md)). Local completion on the
  English simple subset is 4/12, below the 80 % target; no mandatory
  confirmation was missed.
- Latency experiment on both target platforms; benchmark report with
  full environment manifest (#33; RM25, section 12.3). Built and run on
  Apple Silicon macOS: p95 first model action 508 ms, so the 500 ms
  hypothesis is not met there. Built and run on x86-64 Windows
  (#110, 2026-10-23): p95 first model action 3610 ms, 0 timeouts, 76 of
  120 samples ended other than their label. The 500 ms hypothesis is not
  met on either platform. #33 and #110 are closed; AC15 is closed with
  these two runs.
- Second local artifact and the AC08 local swap (#90; RM18, sections 6
  and 12.3): Qwen3-0.6B Q4_K_M has a manifest, passed the live smoke
  suite and the English live skill-and-tool runs, and the live suite ran
  from Q8_0 to Q4_K_M with only `RELAYMIND_LIVE_MANIFEST` changed
  ([`EVIDENCE.md`](EVIDENCE.md)). Q4_0 was tried and is not qualified.
  The external half of AC08 is covered with scripted endpoints (#49).
- AC08 external swap to a second endpoint (#109; RM18, spec 0.29): the
  live acceptance harness switched from Z.ai international
  (`https://api.z.ai/api/paas/v4`) to Zhipu bigmodel.cn
  (`https://open.bigmodel.cn/api/paas/v4`), both `glm-4.6`, with only the
  `[external_model]` table changed; both traces show the same skill
  packages and budgets, and every gateway check held
  ([`EVIDENCE.md`](EVIDENCE.md)). Caveat: both endpoints are Zhipu's
  (international and mainland China platforms), so a second vendor's
  API is not covered.
- Release record (section 14): [`RELEASE.md`](RELEASE.md).
- Closes: AC08 (#90, #109), AC15 (#33, #110), and the release record.

## M7 — Fewer round trips, selection, and no-local operation (spec 0.31)

Order of work in M7, M8, and M9: each issue's "Depends on" line, which
is the one place a dependency is written down.

Phase 2 starts with the levers the recorded evidence already supports,
the selection interface the no-local mode needs, and the mode the
customers without a local model need. Nothing in M7 needs the placement
policy; everything in M7 records the timing fields that M8 needs. Every
new option defaults to the behavior before spec 0.31, so the baseline
stays reproducible by launch keys; the M7 runs decide which defaults
the next revision changes. The milestone opens with the S0 spikes, each
a day or less on existing exports or the existing benchmark.

- Specification 0.31 and the documents around it (#113): RM26 to RM32,
  AC17 to AC23, the changelog, the design rationale, this roadmap.
- S0 spikes, measure before commitment (spec section 12.3):
  - read the runtime's `timings` and cached tokens, then rerun the
    latency benchmark, because every cached-token figure so far is an
    adapter default (#114). The probe is done (2026-10-23,
    [`EVIDENCE.md`](EVIDENCE.md)): with `cache_prompt` on, the runtime
    reused 946 or 947 of 960 to 970 routing tokens (98 %) and 257 or 258
    of 559 to 564 skill-stage tokens (46 %), because the user's text sits
    before the skill body. The latency benchmark rerun is not done;
  - regrade the corpus export offline with the skill-stage draft as the
    answer, which decides `answer_pass` (#115). Done offline
    (2026-10-23, [`EVIDENCE.md`](EVIDENCE.md)): of the 36 auto and
    local_only runs with a final generation, 27 drafts pass the RM31
    guards (the 9 blocked are all language) and 23 of those pass
    grading; 6 runs go from failing to passing and none the other way.
    English local completion on the simple subset would rise from 4/12
    to 7/12 in `local_only` and to 6/12 in `auto`, still below the
    80 % target. A prediction from one recorded run, not a measurement:
    `final` stays the default until #128;
  - external first-token time with and without tools and with provider
    reasoning on and off, which decides the external-path lever (#116).
    Done for one provider (2026-10-23, 20 calls, Z.ai `glm-4.6`): the
    response headers came with the first token, so there is no earlier
    sign of life; reasoning off still left 4.7 to 6.7 s on a short
    answer. A first look, not a benchmark;
  - timings in a constrained configuration of the Apple M1 (a
    CPU-only runtime with fewer threads) next to its normal one, cold
    and warm prefix, cancel during prefill, which sets the deadline
    margin (#117). Done (2026-10-23): CPU-only with two threads, a
    cold routing request took 4121 to 4403 ms against 1028 to 1054 ms in
    the normal configuration, and the next request waited about a fifth of
    the long prompt's full prefill after a cancel (1.76 to 1.82 s normal,
    10.4 to 11.4 s constrained). One run on one machine;
  - score an example-matching selector offline on the corpus prompts,
    with the expected-skill sidecar file (#118, P0: it decides whether
    no-local selection works). Done offline (2026-10-23): the sidecar
    `evaluation/corpus.v1.skills.json` is frozen in the corpus lock. A
    throwaway BM25 matcher scores an English macro Jaccard of 0.675 at
    thresholds chosen on the same cases, against 0.750 for the local
    router, so it does not reach the hypothesis of section 12.3 on this
    corpus; it picks where the router makes no pick (mutation: 6 right
    against 2). #132 should not assume that the default chain changes;
  - count the round trips several read-only calls per generation would
    have saved (#119). Done offline (2026-10-23): 21 generations with 73
    proposals, all external and all read-only; the section 4.3 rule
    admits 17 and keeps 4 rejected (identifiers the request never
    named), and 42 later generations re-proposed a refused call. A
    prediction, not a measurement.
- Budgets table in the launch file, recorded in every run (#142; RM03,
  section 14.1): the one knob a slow-laptop operator needs. Done
  (2026-10-23): `[budgets]` takes the `LocalBudgets` field names and
  `run.accepted` records the values in force.
- First-token, cached, and reasoning tokens per model call, and provider
  request options (#120; sections 6, 8, 14.1). Done (2026-10-23):
  `model.finished` carries `first_token_ms` and, inside `usage`, the
  counts (`null` when the source returned none), `model.started` carries
  the declared `request_options` (no field when none is declared; spec
  0.33), and the pinned runtime reported 558 of 559 prompt
  tokens as cached on a repeated prompt and no reasoning count.
- Draft answer pass (#121; RM31, AC17): `answer_pass = draft` publishes
  the validated draft; `final` stays the default until #115 and #128
  report. Done (2026-10-23, deterministic tests with model doubles):
  the `[performance]` table, the draft guards (run language, tool-call
  JSON, no caller answer option), and `answer_pass_applied` in
  `run.completed`; no corpus run in this configuration yet (#128).
- Several read-only tool calls admitted per generation under
  `calls_per_generation = many` (#122; section 4.3, AC18). Done
  (2026-10-23, deterministic tests with model doubles): the limit is the
  per-stage tool limit locally and the remaining run limit in the external
  stage, a set is rejected whole, and no corpus or live run in this
  configuration yet.
- Prefetch declarations in skill manifests under `prefetch = true`
  (#123; RM29, AC18): the gateway collects request-determined evidence
  before the first skill generation. Done (2026-10-23, deterministic tests
  with model doubles): the manifest `prefetch` key, the load checks, the
  run of the resolved calls as gateway proposals, and the three demo
  skills; the external-stage call is #126, and no corpus or live run with
  prefetch yet.
- Local warm-up at startup and the cache-friendly prompt layout (#124;
  sections 6 and 10), after #114. Done (2026-10-23): `[placement]`
  `warmup` and `warmup_seconds`, the warm-up step in `main`, and the
  external system message with the handoff JSON last. The cold routing
  call took 995 ms without the warm-up and 364 ms with it, on one run each.
  The local layout (skill body before the conversation) was measured in two
  variants and not kept: the body after the instruction kept AC01 at 3 of 4
  but lowered corpus local completion (4/12 to 1/12) and tool correctness
  (11/12 to 7/12); the body first lowered AC01 to 2 of 4, then 1 of 4, and was
  not run on the corpus.
- Completion criteria in the external request, and corrections per
  completed run as a metric (#141; section 4.4; finding F6). Done
  (2026-10-23, deterministic tests with model doubles): the criteria beside
  each predicate, the refreshed values message, the named failed checks, and
  the metric in grading and reports; no live external run, so the effect on
  corrections is not measured yet (#128).
- Early response headers and keep-alives for streamed chat requests
  (#140; RM32, section 7.3): a chat client gets its run reference and
  the events URL before the answer. Done (2026-10-23, deterministic
  tests with model doubles and one loopback socket): `early_headers`
  in `[performance]` for both aliases; no live latency run with the
  option yet.
- Selection interface: the request and result schemas, the chain, and
  `model_router` behind it with its decisions unchanged (#131; RM28,
  AC22). Done (2026-10-23, deterministic tests with scripted selectors and
  model doubles): `relaymind.selection`, the `[selection]` table, and the
  in-process part of AC22; the HTTP transport is #133.
- Example-match selector (#132; RM28): no model call; the no-local
  default chain. Done in part (2026-10-23): the selector, its thresholds in
  `[selection]`, and its tests; on the twenty demo example requests held out
  one at a time it picks 5 right, 2 wrong, and 13 none at the default
  thresholds, so the default chain stays `model_router`. Being the default
  chain without a usable local model is #126.
- Operation without a usable local model (#126; RM26, AC20):
  `[local_model]` optional; the gateway detects a local model that
  cannot run or cannot help on this machine; auto is managed external
  execution through the example matcher; external_only is unchanged.
  Done (2026-10-23, deterministic tests with model doubles and a fake
  clock): the optional table, `[placement] detect`, the class in
  `run.accepted`, `route.selected`, and `/readyz`, and AC20 except its UI
  clause, which is #127. Spec 0.37 records the choices.
- Demo UI labels and progress (#127; section 9).
- Selection benchmarks (section 12.4), our own, built from the
  SkillsBench dataset for skills (#143, P0) and the
  ToolRet-Training-20w dataset for tools (#144, P1): every selector is
  scored through the RM28 interface on the same frozen files with
  precision, recall, and Jaccard, so a customer's own selector and the
  default are compared the same way.
- M7 evaluation (#128): labeled configurations, the new metrics, the
  timing-sample file option, the corpus and latency runs that confirm
  or revise the defaults, the release record.
- Closes: AC17, AC18, AC20; the in-process part of AC22.

Exit: no-local auto completes the English simple subset with
clarification and confirmation working through the example matcher;
corrections per completed run is reported before and after #141; every
M7 configuration has a recorded run, reported apart from the baseline;
the next spec revision states which defaults change, with the numbers.

## M8 — Device-aware placement and the selector transport (spec 0.31)

The placement policy needs the timing samples M7 starts recording; the
HTTP selector transport is what makes requirement 4 (replaceable
selection) true for a third party.

- Timing samples in the run store (#129; RM27): per-generation,
  warm-up, and probe samples, throughput estimates, per-skill local
  outcomes.
- Placement policy (#130; RM27, AC21): the hard cap, per-generation
  deadlines, the idle probe, `local_slow`, readiness, and the
  expected-wait rule as a labeled experiment. `local_first` stays the
  default and reproduces the pre-0.31 decisions; #134 decides whether
  `device_aware` becomes the default.
- Out-of-process selectors over loopback HTTP, P0 (#133; RM28, AC22),
  with an example selector server.
- Gated answer streaming, P1 (#125; RM32, AC19): answer generations
  only, per-statement holdback, `buffered` the default; the gate is
  task-success parity with buffered and the streamed failures reported;
  after #141, because every rejected answer under live is a failed run.
- M8 evaluation (#134): the normal and the constrained configuration
  of the M1, each reported with its measured class, with the
  timing-sample file and an external model so the placement rules can
  fire; prediction error, wasted local time, selection precision,
  recall, and Jaccard, external calls and tokens per completed task,
  the release record.
- Unsuitable detection (#170; RM26, spec 0.48 section 14.4.7): the
  phase 2 run kept the constrained configuration `ready` although its
  local stages kept spending active_seconds. The startup check now holds
  the routing-shaped and skill-shaped warm-ups to the stage budget
  together, and a ready model whose three latest local stages ran out of
  local time is unsuitable until that streak is older than
  `probe_after_seconds`. The hypothesis is measured offline on the
  recorded phase 2 cells (`scripts/analyze_unsuitable.py`), with a short
  live check of the class on the constrained configuration; no matrix
  rerun.
- Closes: AC19, AC21, AC22.

Exit: the unsuitable-machine hypothesis of section 12.3 is reported as
met or not met with exact numbers in the constrained configuration; a
selector swap by configuration, including an HTTP selector, leaves the
orchestrator and the packages unchanged.

## M9 — OpenClaw integration (spec 0.47, P1)

Ship an installable provider plugin that lets OpenClaw use RelayMind's
bounded local/external inference through `relaymind-client-tools`.
OpenClaw owns sessions, tool execution, and approvals; the Python gateway
remains a separate loopback service. The feasibility analysis and alternatives
are in [`design/2026-10-27-openclaw-integration.md`](design/2026-10-27-openclaw-integration.md).
This is planned integration, not a claim of a working or faster plugin.

- Compatibility qualification and captured protocol fixtures (#135;
  RM08, RM19, RM20, RM33; AC24). Pin the host/SDK and check its actual
  requests against the existing chat contract before implementation.
  Record the host's per-model compatibility switches and its defaults
  for a loopback URL (they send `stream_options`, which the gateway
  rejects) and the token size of its requests against the local input
  limit, which bounds how much host work can run locally.
- Remove the retired workflow steps placeholder from the demo UI
  (#173; RM23, AC14). Independent of the plugin; can start now.
- Provider package, model catalog, configuration, and token handover
  (#136; RM12, RM19, RM26, RM33), after #135.
- Text and host-owned tool round trips, request compatibility, and
  buffered SSE (#137; RM08, RM09, RM19, RM20, RM33; AC10, AC24), after #136.
- Cancellation, bounded retry behavior, errors, and trace correlation
  (#138; RM14, RM17, RM20, RM22, RM33; AC11, AC12, AC24), after #137.
- Installable artifact, CI compatibility checks, platform qualification,
  and operator documentation (#145; RM18, RM19, RM33; AC24): packaging
  and CI after #136, platform qualification and the guide after #138.
- M9 live evaluation (#139; RM16, RM25, RM26, RM33; section 12.5), after
  #145: direct external versus RelayMind external_only versus auto, plus
  a separate no-local setup; exact success, latency, calls, and usage.
- Closes: AC24. Existing AC10 to AC12 and AC14 remain regression gates.

The first milestone version supports text and one host-owned function call
per response. It does not claim managed skill selection or M8's managed
placement for the client-owned alias. No dependency on an unfinished YAML
runner, workflow benchmark, or managed live-streaming feature is introduced.

Exit: the pinned plugin installs and runs through OpenClaw without a core
fork; AC24 passes with the actual host transport over model doubles; live
evidence and platform results are recorded separately. Performance gains
are measured hypotheses, not acceptance claims inferred from compatibility.

The unimplemented YAML workflow expansion and plan benchmark are removed
from M9 (RM30 and AC23 retired). The implemented fixed parallel demo remains
RM15/AC14. Issues #135 to #139 and #145 are repurposed with a scope-change
note so their former purpose remains traceable; #173 removes the UI
placeholder that the retired scope left behind.

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
- 2026-10-22, M3 and M4 closed: both milestones were closed on
  deterministic coverage with model doubles, without the live checks
  the rule at the top of this page asks for. Their live runs, the
  launcher those runs need, and AC08 are carried forward (see the M3
  and M4 status paragraphs); M5 and M6 start from that state.
- 2026-10-22, M5 closed: the M4 carry-forward items (the launcher #87,
  the browser test #89, the recorded answer options #91, the chat
  disconnect cancel #92) and the approval resume race (#93) were done in
  M5 next to #30 and #31. M5 was closed on deterministic coverage with
  model doubles, like M3 and M4; its live evidence (#104) joins the M6
  latency experiment (#33).
- 2026-10-22, #90: the local half of AC08 runs live in M6. The second local artifact is
  Qwen3-0.6B Q4_K_M from `unsloth/Qwen3-0.6B-GGUF`, because the Qwen
  repository publishes only Q8_0. Q4_0 from `ggml-org/Qwen3-0.6B-GGUF`
  was tried first and failed the thinking-mode smoke check. The local
  swap ran live; the external swap rested on scripted endpoints only, so
  AC08 stayed open until a live external swap ran (#88; see the next
  entry).
- 2026-10-22, #88 and #104: the M3 to M5 acceptance scenarios ran live
  through `relaymind serve --config` on macOS, with the pinned local
  model and a live external endpoint (Z.ai, `glm-4.6`), using a harness
  that records gateway checks and model findings apart
  (`scripts/live_acceptance.py`). Every gateway check held; the model
  findings are recorded, not counted as passes. #104 is closed by this
  work; its Windows launcher start moves to #110, next to the Windows run
  of the latency experiment (#33).
- 2026-10-22, #109: spec 0.29 defines a second compatible external
  endpoint as one with its own base URL and its own credential variable,
  so the #88 swap between two models of one endpoint does not count.
  The live swap from Z.ai international to Zhipu bigmodel.cn closes
  AC08. Both endpoints belong to one vendor, so a second vendor's API is
  not covered.
- 2026-10-23, spec 0.31 (#113): phase 2 answers customer feedback
  with milestones M7 to M9. The reasons, the options rejected, and what
  three reviews and the owner changed are in
  [`design/2026-10-23-performance-first-routing.md`](design/2026-10-23-performance-first-routing.md);
  the findings behind them are the 2026-10-23 analysis in
  [`EVIDENCE.md`](EVIDENCE.md). Each milestone opens with measurement
  and ends with the runs that decide the next revision's defaults.
- 2026-10-23, #110: the launcher started and the latency benchmark ran
  on x86-64 Windows (Intel i7-1165G7, CPU backend only, llama.cpp b11785
  built from source). The 500 ms hypothesis is not met on Windows (p95
  3610 ms). No Windows correctness corpus ran; 0/60 simple latency
  requests completed, and 0/8 in each acceptance cache arm. AC15
  is closed with the macOS and Windows runs together. No external model
  was available on this machine, so 7 of 22 acceptance scenarios are
  `not_run`; the UI session errored because Playwright Chromium was not
  installed. Acceptance failures and environment gaps are retained in
  the report.

- 2026-10-27, spec 0.47: M9 changes from Workflows to OpenClaw integration
  at the owner's request. Retire the unimplemented RM30/AC23 expansion; add
  RM33/AC24 for a provider plugin over the client-owned chat API. Reuse the
  six open M9 issues with revised scope and dependencies. Preserve the
  implemented fixed parallel demo and its historical evidence.
  The revision is numbered 0.47 because the M8 evidence stack took 0.46
  (#171, merged before this revision). The pinned OpenClaw source was read for the
  host's request defaults and compatibility switches; section 7.5 names
  the ones the plugin declares, so that the first request to the
  loopback URL is not rejected for `stream_options` or a misplaced
  developer message. The UI placeholder removal moved from #145 to #173
  so it does not wait for the plugin.
