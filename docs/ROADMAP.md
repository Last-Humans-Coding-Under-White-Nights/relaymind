# Roadmap

Implementation order follows the dependency guidance in
[`SPECIFICATION.md`](SPECIFICATION.md) section 14. Each milestone lists the
requirements it implements and the acceptance tests that close it. A
milestone is done when its deterministic tests pass and its live checks are
recorded in [`EVIDENCE.md`](EVIDENCE.md). Issues on GitHub carry an `M1`
to `M10` label; the lists below name the issues where the order matters.
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

## M9 — OpenClaw integration (spec 0.47, revised in 0.49 and 0.50; P1)

Ship an installable provider plugin that lets OpenClaw use RelayMind's
bounded local/external inference through `relaymind-client-tools`.
OpenClaw owns sessions, tool execution, and approvals; the Python gateway
remains a separate loopback service. The feasibility analysis and alternatives
are in [`design/2026-10-27-openclaw-integration.md`](design/2026-10-27-openclaw-integration.md).
The plugin and the evaluation runner are implemented and tested over model
doubles; this is not a claim of a live-qualified or faster plugin.

- Compatibility qualification and captured protocol fixtures (#135;
  RM08, RM19, RM20, RM33; AC24). Pin the host/SDK and check its actual
  requests against the existing chat contract before implementation.
  Record the host's per-model compatibility switches and its defaults
  for a loopback URL (they send `stream_options`, which the gateway
  rejects) and the token size of its requests against the local input
  limit, which bounds how much host work can run locally.
  Status (spec 0.49): the pinned transport ran against the gateway over
  model doubles, with 16 frozen wire fixtures and the compatibility table
  in the design doc; no gateway contract change is needed. A real host
  (2026.10.7, macOS) ran against a capture server; its requests are in
  `docs/evidence/2026-10-27-openclaw-mac-probe/` and stay inside section
  7.1 with the switches on a config-file provider; the switches on a
  plugin-supplied model are not_run (#136 must show them). Their size is
  measured on macOS with the pinned tokenizer: 0 of 3 fresh host requests
  and 0 of 7 probe requests fit the 8192 local limit (10,500 to 10,577
  tokens), and a live AC24 turn in auto without an external model ended
  needs_external / context_limit with 0 model calls (config-row
  workaround, #136: a `models.providers` row, not the plugin's model)
  (`docs/evidence/2026-10-27-openclaw-mac-qualification/`). The local
  context stays 8192: in practice OpenClaw turns go external in auto and
  get needs_external in local_only, and #139 reports that. The real host
  against the gateway over doubles ran on the same Mac at `a52f8bf` and
  failed (harness host scenarios 4a to 4c, `checks.passed` false; the
  fixes belong to #136 and #137, and the Linux VPS functional checks of
  #136 to #138 and #145 below ran the real host after them). The macOS
  rerun at `6fc0954` passed the harness host scenarios over doubles, and
  the live AC24 turn then went through the plugin as packaged (its own
  resolved model, no config row): in auto without an external model and
  in local_only it ended needs_external / context_limit with 0 model calls
  (9,384 and 9,389 tokens against 8192;
  `docs/evidence/2026-10-29-openclaw-mac-qualification/`). The switches on
  the plugin-supplied model are shown there by the gateway's acceptance
  (it rejects `stream_options` by name, and the runs through the plugin's
  model completed), not by a wire capture. Windows host runs are not_run.
- Remove the retired workflow steps placeholder from the demo UI
  (#173; RM23, AC14). Independent of the plugin. Implemented with model
  doubles (`tests/test_demo_ui_trace.py`): the trace has no Workflow steps
  section, the fixed parallel demo keeps its summary, and a stored trace
  with the reserved `workflow` tool mechanism still renders.
- Provider package, model catalog, configuration, and token handover
  (#136; RM12, RM19, RM26, RM33), after #135.
  Status: the package registers the `relaymind` provider through the
  public SDK, publishes `relaymind-client-tools` from the gateway's
  listing and through `resolveDynamicModel` (the only path headless
  `agent exec` used on the macOS host probe), maps the plugin's execution mode to the header, advertises only
  copied limits, and tells an unreachable gateway, a stale token (with the
  reconnect instruction), an absent alias, and the configured mode's
  readiness apart; deterministic tests drive it against the gateway over
  doubles (`tests/test_openclaw_plugin.py`). A Linux VPS functional check
  (not a supported-platform qualification) ran the package on the real
  pinned host against the gateway over doubles
  (`docs/evidence/2026-10-28-openclaw-vps-functional-136/`): install,
  enable, inspect, token setup, `models list --refresh`, `agent exec`
  through `resolveDynamicModel` with no catalog refresh, the stale-token
  401 line and recovery, and removal (`models auth logout`, `config unset`)
  worked, and the host honored the plugin's switches on the wire (no
  `stream_options`). The package ran on macOS at `6fc0954`
  (`docs/evidence/2026-10-29-openclaw-mac-qualification/`): the install
  check of the release artifact passed, and a live turn went through its
  resolved model. Windows runs of this package are not_run.
- Text and host-owned tool round trips, request compatibility, and
  buffered SSE (#137; RM08, RM09, RM19, RM20, RM33; AC10, AC24), after #136.
  Status: the provider's stream wrapper refuses unsupported input by name
  before sending (images, structured output, parallel calls, strict tools,
  a dropped tool) and turns an empty answer or more than one call into a
  failure; text and host tool round trips, the host's deny and allow
  decisions on a sandbox mutation, tool_choice, validation, modes, fallback,
  absent-local operation, and an oversized catalog are covered through the
  pinned transport over doubles (`tests/test_openclaw_round_trips.py`),
  with call IDs replayed the way the real host rewrites them. The real
  host's harness scenarios ran in a Linux VPS functional check (not a
  supported-platform qualification) on the pinned host against the gateway
  over doubles (`docs/evidence/2026-10-28-openclaw-vps-functional-137/`):
  `text-tool` (one `agent exec`, Q8), `tool-deny`, `exec-deny`, and
  `exec-full` passed; `tool-approve` failed, correctly, because headless
  `agent exec` has no route for a plugin approval and nothing was
  written. The macOS rerun at `6fc0954` passed the same way
  (`docs/evidence/2026-10-29-openclaw-mac-qualification/`: `tool-approve`
  failed correctly, with "Plugin approval unavailable"). A plugin approval
  answered by an operator is still not_run, and the Windows reruns are
  not_run.
- Cancellation, bounded retry behavior, errors, and trace correlation
  (#138; RM14, RM17, RM20, RM22, RM33; AC11, AC12, AC24), after #137.
  Status: the plugin passes the host's abort signal through, maps every
  gateway error code to a named host error class with one sanitized
  message (401 with the reconnect instruction), sends an Idempotency-Key
  per inference payload (the host's turn ID changes on every call at the
  pin, so the key is the payload's digest), retries only 429 and 504 before
  any content and only with `maxRetries`, and records each attempt with
  its run ID or `unavailable`. Aborts before sending, during a buffered or
  early-header generation, during the replay, and after completion, every
  error status, and replay and conflict cases are covered through the
  pinned transport over doubles (`tests/test_openclaw_cancel_errors.py`).
  A Linux VPS functional check (not a supported-platform qualification)
  ran the `abort` and `external-failed` host scenarios on the pinned host
  over doubles (`docs/evidence/2026-10-28-openclaw-vps-functional-138/`):
  SIGINT mid-generation cancelled the run (as Q11 on macOS), and the 503
  was a named failure that the host retried 7 times with a rewritten
  conversation, each retry a new key and run. On macOS at `6fc0954`
  (`docs/evidence/2026-10-29-openclaw-mac-qualification/`) `abort` (exit
  130, the run `cancelled`) and `external-failed` (8 runs failed, 7 host
  retries, its own message) passed the same way. Other error codes on the
  real host, its failover, and Windows at this commit are not_run.
- Installable artifact, CI compatibility checks, platform qualification,
  and operator documentation (#145; RM18, RM19, RM33; AC24): packaging
  and CI after #136, platform qualification and the guide after #138.
  Status: `npm run pack:release` builds the release artifact reproducibly
  with its SHA-256 and release record (`tests/test_openclaw_install.py`:
  locked pins, same bytes twice, runtime files only, a text and a host
  tool round trip from the unpacked artifact). `scripts/openclaw_install_check.py`
  runs the artifact through the host's lifecycle (install, enable, inspect,
  token, discovery, a text and a host-tool exchange, disable, uninstall, and
  removal of what uninstall leaves). It passed against the pinned host on a
  Linux VPS over gateway doubles, as a Linux VPS functional check (not a
  supported-platform qualification), last at `393a6bf`
  (`docs/evidence/2026-10-28-openclaw-vps-functional-145/`). The CI job `openclaw-plugin` runs the
  package checks, every OpenClaw pytest tier, and the install check on
  ubuntu, macOS, and Windows runners; its install check exports at `78fc94d`
  are recorded as a GitHub-hosted runner functional check over doubles (not
  a user-machine qualification). The operator guide is in
  `integrations/openclaw/README.md`. The macOS row of a user's machine is
  recorded at `6fc0954`: the install check passed with `--target-platform`
  (`docs/evidence/2026-10-29-openclaw-mac-qualification/`). Windows runs
  of this revision on a user's machine are not_run and stay the support
  gap in `docs/RELEASE.md`.
- M9 live evaluation (#139; RM16, RM25, RM26, RM33; section 12.5), after
  #145: direct external versus RelayMind external_only versus auto, plus
  a separate no-local setup; exact success, latency, calls, and usage.
  Status (spec 0.50): the frozen bilingual tasks and their lock
  (`evaluation/openclaw_tasks.v1.json`), grading and the arm summary
  (`relaymind.evaluation.openclaw`), and the runner
  (`scripts/run_openclaw_eval.py`) are implemented and tested over a stub
  host and doubles, including a dry run of auto at the 8192 context with a
  host-sized request (0 local turns, reason context_limit). The
  measurement is recorded: one run on the user's Apple M1 with
  `glm-5.3-flash` (`docs/evidence/2026-10-29-openclaw-eval-apple-m1/`),
  all four arms ran and 144 of 144 attempts succeeded, with 0 local turns
  in every RelayMind arm (relaymind_auto by `context_limit`). The revision
  `2751124` was recorded dirty: two unrelated files were being written
  while it ran (its evidence test module and the CI line that lists it).
  Windows did not run.
- Closes: AC24. Existing AC10 to AC12 and AC14 remain regression gates.

The first milestone version supports text and one host-owned function call
per response. It does not claim managed skill selection or M8's managed
placement for the client-owned alias. No dependency on an unfinished YAML
runner, workflow benchmark, or managed live-streaming feature is introduced.

Exit: the pinned plugin installs and runs through OpenClaw without a core
fork; AC24 passes with the actual host transport over model doubles; live
evidence and platform results are recorded separately. Performance gains
are measured hypotheses, not acceptance claims inferred from compatibility.

Exit status: met over model doubles: the plugin installs and runs through
the pinned host without a core fork, and AC24's parts (text and host tool
round trips, local, external, absent-local, an oversized request,
cancellation, errors, restart authentication) pass through the actual host
transport over doubles. The real host ran them in Linux VPS functional
checks and the install check on GitHub-hosted runners, neither a
supported-platform qualification. The macOS qualification of this
revision is recorded at `6fc0954`
(`docs/evidence/2026-10-29-openclaw-mac-qualification/`). The live #139
measurement is recorded on macOS
(`docs/evidence/2026-10-29-openclaw-eval-apple-m1/`: 144 of 144 attempts
succeeded, 0 local turns). Open: the Windows qualification and a Windows
measurement of this revision, and a plugin approval answered on a real
host (all not_run). The local context stays 8192:
OpenClaw requests (about 9.4k to 10.6k tokens with the pinned tokenizer on
macOS) do not fit it, so auto goes external and local_only gets
needs_external.

Status (2026-10-29): **M9 closed** (PRs #176 to #185, spec 0.49 and
0.50). The GitHub milestone is closed (2026-10-29), with all 16 of its
issues closed. The compatibility qualification (#135), the provider package
(#136), the round trips (#137), cancellation and errors (#138), packaging
and CI (#145), the evaluation (#139), and the UI placeholder removal (#173)
are merged, with the macOS qualification and the live measurement recorded
in `EVIDENCE.md`. The Windows qualification and a plugin approval answered
on a real host stay declared gaps, not_run; neither is inferred from a
runner.

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


## M10 — Multilingual asset recommendations (spec 0.66)

Spec 0.66 (#251) adds the owner-run acceptance profile (section 12.6.6): the
owner runs every trial as operator on the owner's Apple M1 8 GiB MacBook
(target macOS), Windows PC (named native x86-64 target) and Ubuntu VPS (Linux
functional host, never a target platform). Labels may be `owner_machine`
(owner plus one recorded machine annotator, Cohen's kappa before owner
adjudication), and reports say so. A closed `QualificationRecordV1` in
`docs/evidence/m10/` binds the frozen inputs and reports by SHA-256 and is
approved by a pull request review from at least one org member other than the
owner; an approved negative record (a pending one lifts nothing) closes the
`skill_loaded_use` or `mcp_use` gate on the pinned host with the operation
disabled and listed as a known host limitation. The verifier support is
implemented (`--qualification-record`, `--labelling owner_machine`) and
fixture-tested only. No gate has passed.
#255 has committed one **pending** negative `host_operation` record for the
pinned 2026.10.7 host: `m10-negative-mcp-use-openclaw-2026.10.7`
(`exact_route_absent`). No supported route invokes an independent, non-App MCP
tool by its exact native identity: `tools.invoke` is name-only, the harness
runtimes are documented private, and the App-scoped `mcp.app.callTool` does not
count. Those three verdicts rest on the pinned host source; the captures
attempted only `mcp.app.discover`, `tools.effective` and `mcp.app.listTools`
(listed in the inventory's `attempted_routes`). The record lifts nothing until
a pull request review by an org member other than the owner is written into
it, which will make MCP use a host limitation. There is **no** negative
`skill_loaded_use` record: the hooks, tool-result middleware and the provider
stream can stop a read or a request but cannot re-verify fresh person
authority, the session incarnation and the pinned bytes on their own, while
the host's `GatewaySessionAccessAuthority.retain()` borrow re-checks the
person's grant and the exact session incarnation at call time and could back a
provider `onPayload` fence. Spec 7.6.1 admits such a qualified delegation, so
`stop_point_absent` is not established; the earlier pending record asserting it
was withdrawn after review. Loaded use stays disabled as unqualified, and its
AC30 evidence is still required on this host. Both operations stay disabled.
See `docs/evidence/m10/host-use-settlement-20261101.md`. The first VPS run could
not start a person tier (no private interface, missing Chromium libraries), so
the first substantive captures came from CI run 41286397320. A second VPS run at
62c4dcd, after the owner's setup, also ran; the MCP record binds its MCP
capture. Person ingress passes there. MCP use is `blocked`
(`exact_route_unqualified`). The reset case
is now `control_refused` on the real host. That run exited 2 because the
62c4dcd predicate expected one pin in the session's selection listing while
the host lists every pin of the owner profile; the selected-skill pin check
(5d92b64) is already in this branch, and the current predicate accepts the
committed capture. [CI run 41340879424](https://github.com/LHCWN/relaymind/actions/runs/41340879424)
at ed21e007 completed loaded-use observation with the reset case
`control_refused`; its captures are workflow artifacts, not committed here.
The older CI and VPS2 MCP captures show an empty `tools.effective` list only because the old
probe read `payload.tools` instead of the host's `groups[].tools`; a third VPS
run at bba0cf1 with the #249 fix lists both colliding tools and is still
`blocked`, and the MCP record binds it. The MCP finding is unchanged. #259, #260 and #261 remain open and
need the real runs, captures, labels and reviews. Host configuration semantics
(#250/#240) still keep host reports below `qualified`. Spec-only decisions
awaiting implementation: the rank 4–10 choice receipt returning the offered
record, the MCP ledger location/mode/retention and erasure on reset, blind
success judging, and episode corrections through the native feedback RPC.


#207 customer documentation now has [English](asset-selection.md) and
[简体中文](asset-selection.zh-CN.md) entry guides, v1/v2 migration and third-party
index readiness navigation, exact source JSON versus formal JSON/Markdown
workflows, explicit stdout-only choice receipts, and a focused
[privacy/recovery reference](asset-privacy-security.md). The packaged bilingual
M10 skill/MCP, real approval and correction walkthrough still requires the final
qualified host contract/artifact and actual installed-host exercises. These
repository guides do not claim that remaining walkthrough or close #207.
The #238 recorder (`integrations/openclaw/test/host-walkthrough/record.py`)
now drives the unchanged ordinary installed-host qualification in `en-US` and
`zh-CN` through the plugin's own labels, with screenshots, video and a digest
manifest. Loaded skill use, MCP use and host MCP approval are recorded as
`blocked`. No walkthrough has been recorded with it yet, and its
`functional_fixture` tier does not satisfy the qualified-host walkthrough of
#238.

#270 (RM12/RM39; AC30) localizes all recommendation-detail labels for `en-US`
and `zh-CN`, displays ordinary characters through text-only DOM rendering,
neutralizes control characters (in the evidence, the privacy data and the
host skill labels of the native skill select), and wraps long evidence and
feedback values.
Source locators remain the exact offered record: spec sections 4.6.1 and 7.6
define local locators as resolved `SKILL.md` paths and return that record in
the scoped view; no home-path abbreviation/redaction rule is specified.
In text mode only a backslash introducing literal `\u{` notation is doubled;
ordinary Windows path and description backslashes stay exact. The quote inside
the quoted description is escaped, so source text cannot imitate a neutralized
control or a field; one locale rule (`zh`, `zh-CN`, `zh_CN`, `zh-Hans`) serves the
panel and its evidence. A recommendation receipt is validated against the
published contract before updating the shown evidence, actions or feedback
revision; malformed receipts leave all three in place. Native skill labels
distinguish literal control notation using the same text helper. The ledger
comment limits its 0600 owner-only access claim to POSIX; Windows access depends
on the filesystem ACL.
The adapter revision is repinned to the built bytes that include these review
fixes and the #256 ledger and fence fixes
(`38a65c787be8ddfbed99527c4e3b2c167d4a308bd2b566d2cf18acac975f06b4`).
The ordinary installed-host run of these exact bytes (pinned OpenClaw
2026.10.7, Ubuntu runner, CI run 41354462728, commit f229cff, PR merge
checkout dd71bc2) passed the ordinary-pass and ordinary-skill-pass predicates
with all 15 harness sources matching the wall-clock harness (the host
qualification backend runs on the real clock, not the test `FakeClock`). Its
unmodified capture replaces
[openclaw-native-actions-20261101-adapter.json](evidence/m10/openclaw-native-actions-20261101-adapter.json)
(SHA-256 `a5f39a046399baf8ee1c5e5d1ea767086b1f8a4d36630c694709600c2978e9e7`),
which the registry's evidence revision names. Browser fixtures alone do
not establish native host qualification for those bytes; as before, this is
Linux functional evidence with disposable accounts.

The #192 implementation supplies independent catalog discovery: closed records
and snapshots, ordinary local SKILL.md readers, explicit hash-verified metadata
imports, safe internal searchable bodies, atomic shared-SQLite publication,
source-owned tombstones, retained stale observations, recovery and one bounded
local worker. `[recommendations]` defaults disabled; enabled catalog work starts
in the application lifespan. The #187 foundation merged in #212. The three
#192 catalog criteria, discovery, atomic change visibility and honest provenance
without execution, are implemented and verified. #192 alone did not change the
warm-up serving order; #196's lifecycle work is recorded below. The qualified
#203 GitHub refresh profile is recorded below; catalog discovery supplies no
host availability or execution authority.
#193 selection and #194's partial lifecycle implementation are recorded below.
The AC25 live host subcases, full 300-asset measurements, Windows/macOS
qualification, Matrix rights/cohort/refresh and formal independent corpus gates
remain separate from #192's completed catalog criteria. #268 (spec 0.66
amendment) admits each local SKILL.md candidate on its own: a rejected file is
an exclusion in a partial generation, not a whole-folder failure, and body text
is no longer markup/repetition checked. A local scan of the pinned OpenClaw
2026.10.7 package admits all 49 bundled skills; this is a developer
observation, not an AC25 live host subcase.

The #193 selection component implements RM35/AC26: separate closed selection
v2 and recommendation-result schemas, projection-only lexical ranking, strict
loopback HTTP replacement and example, bounded primary/floor coordination,
cancellation, current admission and observed-versus-reported cost evidence.
The configuration factory is available without recommendation API attachment.
All lexical matches require user choice; no calibrated confidence or retrieval
quality is claimed. Managed v1/AC22 and provider/host AC24 remain unchanged.
Spec 0.52 also revisions effective stale changes atomically with stored offered
record bytes; capture-only observations retain the prior projection through
restart. Current revision equality applies to computing recommendations,
including abstentions and empty offers. This does not change later choice
semantics, which retain their exact-candidate/current-policy checks.
The #194 implementation supplies RM36/AC27's separate recommendation object,
event and index-job schemas, atomic recommendation slot and exact replay receipts,
bounded JSON create/read/event polling, request-owned cancellation, recovery,
expiry and 30-day parent retention. It publishes a compatible lexical offer
after actual configured-source acquisition and reports current readiness facts.
Actions and feedback are supplied by #195; public compute cancel and SSE remain
unavailable. Qualified host/source adapters and the final corpus/whole-task
gates remain open.

The #196 implementation supplies RM38/AC29 lifecycle infrastructure: a real
post-listener warm-up task for enabled launchers, pending inference/probe fences,
prepared matching source/lexical publication, durable eligibility/representation
bindings, derived-only rebuild/recovery, and cooperative sync/async batch gates
on the existing single background worker. Foreground recommendation admission
and the native execution slot remain independent. Actual source and derived jobs
retain batch, overlap and drain evidence; private feedback/key ownership survives
restart and derived repair. This does not assert the operational targets: ten
clean process launches from the genuine full 300-asset catalog, each within
1,000 ms; every 25-record receipt/update visible within 1,000 ms; the complete
60-second, two-request/second workload with four import batches, warm p95 within
300 ms and zero errors/timeouts; and peak incremental memory within 1 GiB across
startup, rebuild, import and queries remain unmeasured. Real embeddings/rebuild
and inference contention, actual Apple M1 8 GiB/macOS and named native x86-64
Windows measurements remain required. Linux fixtures, open sockets and model
doubles close none of those gates; semantic ready count remains zero.

Specification 0.51 froze the independent M10 recommendation core (spec
section 1), and 0.52 revised its offer cache and freshness rules
(RM24, RM34, RM38); every full-milestone gate stays. The
[published design](design/2026-10-29-multilingual-asset-routing.md) reconciles
qualifications #188–#190 per capability. The adapter contracts behind the
blockers MATRIX-COHORT, MATRIX-OVERLAP, MATRIX-RIGHTS, MATRIX-REFRESH,
HOST-ACTION-AUTHORITY, HOST-EXACT-SKILL-USE, HOST-MCP-BINDING and
HOST-MCP-CURRENTNESS remain capability-specific gates. The direct-action and
exact-selection exception is the qualified section 7.6.1 entry below. Spec 0.65
now defines loaded-use and MCP-use activation conditions in sections 7.6.1–7.6.2;
it does not qualify or enable either operation. Their separate reviewed entries
still require actual supported native composition and all negative controls.
Host snapshots/runtime recommendations remain disabled pending their own later
normative contract and qualification. Source gates stay unchanged. There is no
host upgrade, alternate dataset, new milestone or relaxed/partial acceptance,
except that an approved spec 0.66 negative record removes the loaded-use or
MCP-use requirement on the pinned host version and lists it as a host limitation.

The owner-approved defaults of 29 October 2026 are in spec section 1.

The owner explicitly authorized implementation on the reviewed specification
stack on 29 October 2026 before #187 merges; the former merge-first sequencing
restriction is superseded. Merges remain owner-owned. The table gives the
current RM/AC mapping without claiming that a remote issue has already changed.

The source-grounded development deliverables for #191, #199 and #200 are
implemented under spec 0.56/0.57. The pinned working catalog has 412 actual assets:
87 Matrix skills, 194 original-English SkillsBench skills and 131 genuine GitHub
MCP tools, with 61 explicit exclusions. Its 128 preregistered Chinese, English
and mixed cases cover 54 capability requests and 58 distinct targets, including
an original-English cross-language example. References are developer-authored
from retained originals, not independent human judgments. Captured rights,
availability, missing language pairs and reference limits stay explicit.

The shared production-path run/compare tooling supports builtin and team HTTP
selectors over the same full offer and frozen cases, explicit stateless/cold/reuse
index preparation, bounded polling, retained failures and separate setup/query
costs. See [the source workflow](SOURCE_BENCHMARK.md) and
[the team/protocol guide](ASSET_BENCHMARK.md). Recorded stateless/cold primary
query timeouts and blocked reuse preparation remain visible; successful lexical
fallback is not primary quality. Corrected offline comparison rejects cold/reuse
policy mismatches without rewriting the historical reports.

This usable development profile and its input/lock infrastructure complete the
working deliverables of #191/#199/#200. The separately supported independent
200-skill/300-asset qualification profile, native authorship/double annotation,
rights review and full numerical/platform gates remain open. Those acceptance
inputs are not prerequisites for the working source benchmark or these delivered
development components; they remain prerequisites for claims under that formal
profile and for full M10 acceptance.

| Qualification / gate | Current evidence and remaining requirement |
| --- | --- |
| #188 Matrix capture | Historical 29 October qualification: one genuine version 1.1.1 archive, two equal-hash downloads; one Chinese body. This is distinct from the later 87-skill source-grounded development capture, which does not qualify market refresh or independent corpus rights. MATRIX-COHORT (100 original Chinese), MATRIX-OVERLAP (25 functional pairs), MATRIX-RIGHTS and MATRIX-REFRESH remain blocked; same-source permitted operator export/rights/inventory input required. No automatic substitute or source permission. |
| #189 direct commands | Real pre-model ingress and isolated credential/native search/read/forged-input checks pass over doubles; verified person and immutable choice-to-library composition failed. These historical probes did not qualify host authority. The later spec0.62 ordinary direct-action proof closes HOST-ACTION-AUTHORITY for #195; exact skill/use gates remain open for #197/#198. Real interactive approvals not_run; read-only does not waive exact content/authority. |
| #190 MCP | Same-instance public genuine provenance/schema, collision-safe host execution and changed-schema execution passed; three server invocations, ten completed gateway runs, zero gateway execution. Disconnect retains schemas and actual call fails; deny/removal exclude tools. HOST-MCP-BINDING and HOST-MCP-CURRENTNESS remain blocked: session/turn/host revision absent, availability unknown, third-party authorized runtime inventory refused. |
| Evidence tiers | #189/#190 are Linux real-host functional runs with model doubles. #190 uses coding profile/toolSearch=false/disposable conversation grant; not intact-native/default or owner-run acceptance. HTTP/OAuth, live quality, supported-platform M10 and human interactive approval remain not_run. |

Within the authorized reviewed stack, these components proceed within their dependencies: the
independent catalog, reads of ordinary SKILL.md directories, closed reviewed
metadata imports, discovery v2, the lexical and HTTP selectors, the persisted
API, and the backend registration, revocation and privacy tests. A runtime
create returns recommendation_unavailable, and snapshot payloads and non-null
bindings are rejected (spec section 7.6). No production host registration
is enabled by these probes. #203 cannot claim Matrix refresh from one download
route. Generic infrastructure and doubles never close actual source or host
gates. The hybrid selector and adaptation keep the trial and data dependencies
in the table below. Partial PRs must state retained acceptance; incomplete host,
model and platform deliverables stay open. Development completion of
#191/#199/#200 does not assert independent corpus or full M10 acceptance.

#201/#202 backend integration checkpoint (spec 0.59): the bounded local artifact
loader, cooperative hybrid ranking/vector lifecycle and private explicit-correction
adaptation/reset backend are implemented. Native loading reacquires the same CPU
owner and retains actual cancellation/drain evidence. The production embedding
qualification registry now contains two exact reviewed Linux CPU identities;
fresh ordinary hybrid activation is recorded in the model evidence. No portable
qualified model is claimed. Historical 47-asset/36-case native development results
remain provisional, distinct from the current 412-asset/128-case source benchmark.
Real host correction authority, formal chronological episodes and final quality,
whole-task, supported-platform and resource gates remain open.

#203 source qualification checkpoint (spec 0.59): the reviewed fixed public
GitHub Git skill profile has an actual complete six-skill/twelve-file acquisition
plus root MIT evidence, 17 serial GETs and two ref observations separated by
704.62 seconds. The commit remained 849c1be; change was not observed. Section
4.6.1.1 defines this installed protocol profile. The implementation candidate
now includes bounded transport, immutable acquisition, profile-persisted cooldown
and scheduling on the existing worker. Two actual periodic product jobs were
304.28 seconds apart; both published six assets with lexical readiness/API200,
and the upstream was unchanged. Controlled source-server tests cover changes,
outage/recovery, cancellation, atomic restart and foreground I/O boundaries.
Independent review and integrated functional verification passed; no AC29/AC34 full workload or
supported-platform pass, Matrix market refresh or corpus substitution is inferred.
Local/import user libraries remain supported. Actual/fixture distinctions and
versioned observations are in [the source record](evidence/2026-10-30-github-skill-source/README.md).

The table retains the other issues' direct dependencies and foundations.
#191/#199/#200 describe the delivered working profile; their separate formal
qualification gates remain as specified above. Other planned components do not
become implemented by appearing in the table.

| Issue | Direct dependencies | Existing foundations | New RM/AC IDs | Deliverable / completion gate |
| --- | --- | --- | --- | --- |
| #191 | #187; reviewed source capture | RM07, RM12, RM16, RM25, RM28 | RM40; AC31 | Implemented source-grounded skill inputs, pinned originals/exclusions, 128 developer-authored CN/EN/mixed references and strict lock tooling; independent qualification remains separate |
| #192 | #187, #188 | RM07, RM08, RM12, RM17, RM24, RM28 | RM34; AC25 | Durable atomic catalog with safe projection/import/scans/deletion and honest runtime availability |
| #193 | #187, #192 | RM02, RM04, RM08, RM12, RM18, RM22, RM28; AC22 | RM35; AC26 | v2 schemas/chain/lexical fallback + HTTP replacement; unchanged managed v1 |
| #194 | #187, #193 | RM12, RM17, RM19, RM21, RM22, RM28, RM33 | RM36; AC27 | Independent recommendation API/store/events; atomic idempotency, disconnect and recovery |
| #195 | #187, #189, #194 | RM08, RM11, RM12, RM17, RM19, RM22, RM33 | RM37; AC28 | Authenticated direct-input choices/feedback, scope isolation, retention/delete/reset |
| #196 | #187, #192, #193, #194 | RM03, RM16–RM19, RM22, RM24, RM26, RM27, RM28 | RM38; AC29 | Separate readiness, lexical visibility/rebuild/scheduler behavior; full 300-asset/live embedding measurements later |
| #197 | #187, #189, #194, #195, #193 | RM08, RM11, RM12, RM19, RM20, RM22, RM33; AC24 | RM39; AC30 | Packaged direct commands, exact installed-skill load/use and feedback; unchanged provider |
| #198 | #187, #191, #192, #196, #197 | RM16, RM18, RM25, RM26, RM28, RM33; AC22, AC24 | RM39, RM44; AC30, AC36 | Actual early Apple M1 8 GiB owner-run trial and three-arm costs; observations before hybrid investment |
| #199 | #187, #191 development inputs | RM07, RM12, RM16, RM25, RM28, RM33 | RM40; AC31 | Implemented mixed working catalog: 412 real source assets including 131 genuine MCP tools, frozen references and reproducible inputs; formal independent held-out qualification remains separate |
| #200 | #187, #193, #191 | RM03, RM12, RM16, RM18, RM22, RM25, RM28; AC22 | RM35, RM40; AC26, AC31 | Implemented shared production-path run/compare, builtin/team HTTP examples and explicit index readiness with separate setup costs; working source and formal qualification profiles retain their own evidence rules |
| #201 | #187, #198, #199, #200, #196 | RM03, RM04, RM12, RM16, RM18, RM22, RM24–RM28 | RM38, RM41; AC29, AC32 | Development-selected pinned local embedding artifact, bounded revisioned hybrid ranking/indexing, lifecycle/resource evidence |
| #202 | #187, #195, #201, #199, #200 | RM04, RM08, RM11, RM12, RM16, RM17, RM22, RM24, RM28 | RM37, RM42; AC28, AC33 | Bounded separate adaptation stage, 100 chronological episodes, unrelated controls and exact reset |
| #203 | #187, #188, #192, #196, #198 | RM03, RM07, RM12, RM16, RM17, RM19, RM24, RM28 | RM34, RM38, RM43; AC25, AC29, AC34 | Permitted automatic refresh, bounded staging/atomic publication, stale failure states and live demonstration |
| #204 | #187, #190, #197, #195, #192 | RM08–RM12, RM16, RM19, RM22, RM33; AC24 | RM39; AC35 | Exact host MCP use and real interactive approval, duplicate/schema/policy/disconnect rejection |
| #205 | #187, #199, #200, #201, #202, #203, #204 | RM03, RM04, RM16, RM18, RM22, RM25–RM28, RM33; AC22, AC24 | RM40–RM44; AC31–AC36 | Every language/hard cohort and ≥100 paired host tasks, honest whole-task consumption, confidence bounds |
| #206 | #187, #197, #201, #202, #203, #204 | RM03, RM16, RM18, RM19, RM25, RM26, RM33; AC24 | RM38, RM39, RM44; AC29, AC30, AC35, AC37 | Current package and operational gates on actual Apple M1 8 GiB macOS and named native x86-64 Windows |
| #207 | #187 formally; completion follows shipped features and #205/#206 evidence | RM07, RM12, RM16–RM19, RM22, RM25, RM28, RM33; AC22, AC24 | RM34–RM44; AC25–AC37 | Incremental guides, exact benchmark/migration/privacy commands, verified bilingual skill/MCP walkthroughs and evidence references |

Choice and feedback revisions follow spec sections 5.1 and 7.6, and the
inputs to adaptation follow RM42.

M10 closes in one decision, when every issue and gate above passes and
AC25–AC37 pass with the evidence spec section 12.6 and RM44 require. The
actual Apple M1 8 GiB trial (#198) is delivery order, not milestone closure. Failed, unknown or not_run evidence stays open;
WSL, CI, doubles and historical M9 evidence do not substitute. Missing export,
rights, profile, interactive or platform inputs are concrete blockers, not
permission to fabricate completion.

#195 backend status (RM37/AC28): the independent core now implements private
operator registration/authentication, exact-offer choice, independent scoped
feedback CAS and insertion cursor, feedback delete/reset, keyed erased-replay
handling and atomic dependent retention. Deterministic fixtures inject their
qualification resolver. The later pinned OpenClaw2026.10.7 spec0.62
`direct_actions` profile now also has actual signed-person/session composition
and fresh ordinary installed Control UI proof: explicit choice, feedback,
inspection/delete/reset, account isolation and connection retirement. The
ordinary entry uses the default Gateway registration resolver and a separately
registered private action credential. This completes #195's direct-action and
privacy delivery; skill loaded-use, snapshots, MCP execution, human approvals
and final platform/quality gates remain separate. Functional proof uses authored
sandbox data and does not claim full M10 acceptance. The exact sanitized
[ordinary-entry capture](evidence/m10/openclaw-native-actions-20261031.json)
is preserved at SHA-256 `21e8c4a77017d1543323056d3c1c892ea2fe6901971919ac008257252b663faf`.
Its person-component `production_adapter_qualified:false` is not a self-issued
product verdict; the independently reviewed `ordinary_actions:true`, exact
runtime key and direct-action registry identify this qualified scope.

#195/#197 scope isolation correction (spec 0.63; RM37/RM39, AC28/AC30): an
independent review of the native direct-action channel reproduced, against the
actual gateway, that a native binding could act in another configured scope.
The recommendation path ignored the binding, and nothing compared the
gateway's single active scope with the binding's `userId`/`workspaceId`, so a
shared base URL or a credential registered for another scope created, listed,
deleted or reset in that scope. Spec 0.63 adds the optional
`X-RelayMind-Expected-User`/`X-RelayMind-Expected-Workspace` pair, checked
after the bearer and before any read or write, with the fixed `scope_mismatch`
(403). The native adapter now sends its binding's scope on every gateway
request. The same revision clarifies that only isolation conditions apply to
every role, so valid read-only or other-agent roles are no longer refused, and
the dormant skill bridge rechecks choice expiry before each native dispatch.
The changed adapter bytes have the new exact revision
`f3241577e903feddd3da61fd58513b4a3e90889552d9bd7f52a302fd81f52c6c`. A fresh
ordinary installed-host run of those exact bytes (CI run 41221091650, pinned
OpenClaw 2026.10.7, Ubuntu runner, PR merge checkout `89d374f`) passed the
repository's ordinary-pass predicate: default registration, the production
registry enabled, explicit choice, feedback, inspection/delete/reset, account
isolation and connection retirement. Its unmodified capture is
[openclaw-native-actions-20261101.json](evidence/m10/openclaw-native-actions-20261101.json)
(SHA-256 `d5c186ac552b6028cd47c9bd4679f79259eab1092d20dd95b48c36fa35437a39`), which the registry's evidence revision names. The 31 October
capture remains the record of the earlier revision. The loaded-use fence and
the conditional MCP use path (#239/#204) change the adapter bytes again; the
registry pinned the exact revision
`653902615edfd3b6bd1074f9d50d560e3a9eaf4318a39e96feb85e512e06441f`, and
the ordinary installed-host run of those exact bytes (CI run 41286397320,
pinned OpenClaw 2026.10.7, Ubuntu runner, commit 548588a, PR merge checkout
6a4a99f) passed the ordinary-pass and ordinary-skill-pass predicates with all
15 harness sources matching. Its unmodified capture is
[openclaw-native-actions-20261101-adapter.json](evidence/m10/openclaw-native-actions-20261101-adapter.json)
(SHA-256 `933fb4e7c8b3057305fae165561825984a63325275b821be84b008dd574589f7`), which the registry's evidence revision names. The #256
review fixes (ledger single settle and close, in-flight receipt recovery,
honest gateway failure codes, attributable conversation blocks, independent
registration; then exclusive ledger ownership, admission-time key binding
with dispatch markers, reserved block capacity and the post-read fence
recheck) change the adapter bytes again: the entry is re-pinned to
`943e1f0e2ba6e9e8dad45456e6eb8122a475c91ced0c9e1e3f8d249471fe240f`, and
the ordinary installed-host run of these exact bytes with the stricter
session-selection harness (CI run 41318389858, commit c0bd6f4, PR merge
checkout c22ad40) passed the ordinary-pass and ordinary-skill-pass predicates
with all 15 harness sources matching; its capture
`openclaw-native-actions-20261101-adapter.json` (SHA-256
`35436e519245…`) was the registry's evidence revision until the ledger fixes
below were recaptured. As before, this is
Linux functional evidence with disposable accounts, not macOS/Windows or
owner-run acceptance. The person-ingress harness hardening raised in the #224 review
(#228) changed evidence-bound harness files; a fresh ordinary installed-host
run with the hardened harness, including the fixes from the #231 reviews, and the
same adapter bytes passed the ordinary-pass predicate after the runner
stabilization (CI run 41252553272, commit 84798e9), with all 15 harness sources
matching. Its
unmodified capture is
[openclaw-native-actions-20261101-harness.json](evidence/m10/openclaw-native-actions-20261101-harness.json)
(SHA-256 `ca88ffcbcea2558975c74e3a25f38c58f552e6108576dc2cad032a7b6118ec53`), which the registry's evidence revision now names.

#197 first-slice status (RM36, RM37, RM39; AC27/AC28, partial AC30): the
package includes dormant recommendation/action clients, closed lossless protocol
validation, inert evidence rendering and an unregistered controller whose host
boundary denies unsupported operations by default. Those clients are reused by
the separately qualified spec0.62 direct-action entry described under #195.
Exact skill selection is now qualified for the same pinned host and adapter
revision: a Skill Library entry saved through `skills.library.save` is mapped
to the RelayMind recommendation and selected for the next turn through the
native `relaymind.skill` receipt and the Control UI. Unmapped and foreign-person
selections are refused: an unmapped skill (`INVALID_REQUEST`), a cross-session
foreign person (`FORBIDDEN`) and a foreign person on its own session, refused by
RelayMind's own authority check (`INVALID_REQUEST`). The predicate also binds
the asset ID, content hash and source version, and requires the saved revision
in `skills.library.list`, whose `session` must be the authenticated target
session with exactly that skill and revision as its selection. The ordinary
installed-host run of this stricter harness (OpenClaw 2026.10.7, Ubuntu runner,
CI run 41317645014, commit 6e11c2d, PR merge checkout 077e402) passed both the
ordinary-pass and ordinary-skill-pass predicates, with all 15 harness sources
matching. Its unmodified capture is
[openclaw-native-actions-20261101-skill.json](evidence/m10/openclaw-native-actions-20261101-skill.json)
(SHA-256 `3a063ab374119dbe5b04f4c6625519a44f4ea75594525975ba661dfd9f9ff439`), which the registry's evidence revision now names for the
operations `direct_actions` and `exact_skill_selection`. The receipt records
`sessionActivation: next-turn` and `instructions: not_observed`: selection is
not observed loaded instruction use, which stays open as HOST-EXACT-SKILL-USE in
#239. MCP use and real interactive approvals remain open (#204). As before, this
is Linux functional evidence with disposable accounts, not macOS/Windows or
owner-run acceptance.

Spec 0.65 checkpoint (#239/#204/#240; RM16/RM34/RM37/RM39/RM40/RM44;
AC25/AC28/AC30/AC31/AC35–AC37): the normative contract now distinguishes a
library head update from invalidating a chosen immutable revision. #239 must
observe actual native instruction bytes in the next actual model request of the
same session incarnation, correlate native and RelayMind runs, and enforce the
later-use fence for removal, choice expiry/erasure/remapping, reset and revoked
authority. Native pins surviving removal do not satisfy that fence. A real-host
model-double round trip with every listed control may support a reviewed
skill_loaded_use registry entry for that exact composition in the
deterministic-model functional tier; live-model capability and full acceptance
remain separate. The current selection receipt still says not_observed.

The #239 observation harness
(`integrations/openclaw/test/host-ordinary-qualification/qualify.py`) now reads
provider attempt records by their own per-turn file window and retains a
failure that never reached the gateway with its actual outcome; an
unexplained record leaves the case incomplete
(`ordinary_skill_provider_unexplained`). It drains asynchronous evidence until
every proposed or answered tool call has its native hook, so a failed or
cancelled tool's late hook is collected; an unresolved call stays incomplete
(`ordinary_skill_tool_unresolved`), and a refused send is accepted only after
the same bounded drain. The committed loaded-use observation capture
(`tests/fixtures/openclaw-loaded-use-observation-20261101-vps2.json`) predates
this harness change, binds the earlier harness by digest, and must be
recaptured with this harness before it is cited as evidence for it.

#204 may build a disposable qualification harness against public authenticated
dispatch and actual native approval paths, but MCP activation remains disabled.
Section 7.6.2 defines a separate explicit direct-use request and ten qualification
conditions. Section 14.5 caps native RPC requests at 512 KiB; the 60-second
preparation/dispatch deadline excludes the separately bounded native approval
wait, without changing any existing budget. The mcp_use registry operation is
separate from the platform-report lifecycle step of the same name.
Neither name-only tools.invoke nor the narrower MCP App view route proves a
generic independent-tool bridge. Exact native identity/lifetime, post-approval
and final-I/O currentness, actual server effects and negative
controls must pass for the exact pinned operation/route/transport. Automated
native UI allow/deny is functional evidence only; genuine human approval,
actual Apple M1/macOS and native Windows, and whole-task gates remain open.
No adapter/registry activation or host-use measurement is supplied by this
revision; its only code change adds the reviewed specification evidence anchor.
The #204 harness now projects the pinned host's grouped `tools.effective`
inventory; its fixture discovery check requires both colliding fixture tools in
the session inventory, so an empty or malformed inventory fails the run. Earlier
captures recorded an empty list because of the old flat projection. The person
probe first starts the session MCP runtime with public `mcp.app.discover`. One
Linux x86-64 VPS run at bba0cf1 listed both colliding tools; it is now committed
as `docs/evidence/m10/openclaw-mcp-use-20261101-vps3.json` and bound by the
#255 MCP record. It is discovery evidence only, exact MCP use remains blocked,
and [CI run 41340879424](https://github.com/LHCWN/relaymind/actions/runs/41340879424)
at ed21e007 confirmed the corrected discovery and blocked exact-use result.
That newer CI capture remains a workflow artifact, not committed evidence.

#250 implements bounded native configuration preflight with the installed
OpenClaw 2026.10.7 validator under Node, including strict/plugin checks and
native default handling, with a Python timeout and closed verdicts recording
the host version and the SHA-256 of every pinned host entry point the checker
imports; any other validator identity is rejected. The child state is a disposable
directory without operator credentials; when the config names `relaymind` it
holds only the packaged RelayMind plugin (the built integration, or the
unpacked release passed as `plugin_root`), so installed-plugin discovery checks
its settings. A plugin referenced in `plugins.entries` or `plugins.allow`
without an inspected schema is `unavailable`
(`plugin_schema_not_inspected`), not `valid`. Valid/invalid, Unicode-pattern,
plugin-schema, installed RelayMind, unknown/uninspected plugin and
unavailable-host fixtures run in the OpenClaw CI job.
`resolve_whole_task_inputs(..., host_config_checker=check_host_config)` opts
into native preflight; `host_config_validation={digest: verdict}` instead
supplies captured closed verdicts without a process launch. Both reject
native-invalid configs and retain validator identities by host_config_digest. Ordinary offline grading never invokes Node;
without captured validation, host configurations remain unverified.
This local preflight does not authenticate historical captures or qualify
any acceptance gate; the reviewed evidence verifier remains #240.

#239/#204 adapter checkpoint (spec 0.65; RM16/RM37/RM39; AC28/AC30/AC35): the
OpenClaw adapter now carries the loaded-use fence
(`src/recommendation-native-use.ts`) and the conditional `relaymind.mcp` handler
(`src/recommendation-native-mcp.ts`), both behind their own registry operations
(`src/recommendation-native-registry.ts`). The fence checks fresh native
authority for the same session incarnation, registration, the unexpired
choice, the eligible asset and operator association, a nonremoved library
entry and the immutable selected bytes at every boundary, and blocks the
conversation after a failure. The MCP handler implements the closed request,
the 512 KiB native row, the 60-second deadline with the separately bounded
approval wait, the exact binding with a recomputed canonical schema hash,
approval binding to the bound target and single use, the closed receipt and
idempotency. Key bindings, dispatch markers, consumed approvals and conversation blocks are kept
in a durable private ledger synced before dispatch, so a restart replays an
unresolved key as indeterminate and never reuses an approval; without a
ledger nothing is dispatched. Retention/erasure of ledger records and the
host-owned ledger location are not yet defined for a qualified composition.
Host schema patterns are admitted only in a provably bounded subset. Only
deterministic tests with injected fixture resolvers and routes exercise the
admitted paths. Neither `skill_loaded_use` nor `mcp_use` is in the shipped
registry, no production MCP route exists (the pinned host has no qualified
exact-use route), and the adapter wires the fence to no host delivery boundary:
spec 0.65 names no qualified native boundary that can stop the next model
request. HOST-EXACT-SKILL-USE (#239) and MCP use with real approvals (#204)
remain open. The changed adapter bytes have a new exact revision, and the
shipped `direct_actions`/`exact_skill_selection` entry is re-pinned to it
(`b49d94d8267076516430fb2e4fb64d41fad2961fc8009c84e60921cf43e9e98e`), as for the spec 0.63 scope fix. After review, the
ledger takes over a stale owner lock without ever removing a live owner's
lock (per-owner lock files; one owner under any interleaving) and writes each
record completely across short writes before acknowledging it; these changed
bytes are that new revision. The ordinary installed-host run of these exact
bytes (CI run 41322540111, pinned OpenClaw 2026.10.7, Ubuntu runner, commit
7af9cc2, PR merge checkout ad97e0b) passed the ordinary-pass and
ordinary-skill-pass predicates with all 15 harness sources matching the #247
harness; its unmodified capture replaces
`openclaw-native-actions-20261101-adapter.json` (SHA-256
`6b8947df711e70c0d87f1f50d8a43e1ffb57b2a68699ff69d6457249f4b828b2`), which
the registry's evidence revision named for that revision. A fourth review
made the MCP use request fail as `invalid_request` before identity resolution
when it has no canonical text (an explicit undefined property or an unpaired
surrogate) or a non-string session field, checks registration once on the
in-flight duplicate path, and reads the loaded-use fence deadline live. The
ledger then cuts a torn final record through a separate read-write handle,
because a Windows append handle cannot change the file length. These
changed bytes were re-pinned to adapter revision
`a29cac121c5e01932b6b3ee75e9bab2214e5e9e72cbc36d8f802f62b917c7cfe`. A fifth
review made a same-key MCP request read its key history again after its
registration check (an attempt that settled meanwhile is recovered, never
reported as `not_dispatched`), kept a host `UNAVAILABLE` result of the exact
skill read from blocking the conversation, digested the adapter bytes once
per resolver, took over a stale ledger lock that names this process's own
PID but was not created by it (PID 1 in a container), reserved the exact
room for each binding's dispatch marker and longest receipt under the byte
cap, and compiled each host schema pattern once per operation. These bytes are
re-pinned to adapter revision
`37f9b415c47260065d42453b5d85474375a796b8a7416af78732668863357f2d`. The
host qualification harnesses then built their backend on the real wall clock
(the test default `FakeClock` stamped every view in the past, so the
installed adapter refused the first selection), which changed the harness
sources but not the adapter bytes. The ordinary installed-host run of these
exact adapter bytes with that harness (CI run 41351781170, pinned OpenClaw
2026.10.7, Ubuntu runner, commit 293d73e, PR merge checkout 503adf0) passed
the ordinary-pass and ordinary-skill-pass predicates with all 15 harness
sources matching; its unmodified capture replaces
`openclaw-native-actions-20261101-adapter.json` (SHA-256
`58c6df0ba6dc32f454ef2c131b5e498f971876877108427d29755fbd230fefe4`), which
the registry's evidence revision names. As before, this is Linux
functional evidence with disposable accounts. `tests/test_openclaw_install.py` fails when the registry and
the built adapter digest of the release artifact drift.

#240 host_config evidence must validate with the pinned host's own validator
and captured schema/validator identities, including the native regex dialect.
The pinned config validate command is recorded in the
[host probe](evidence/2026-10-27-openclaw-mac-probe/REPORT.md).
Provider/sampling/budget documents need their own applicable source-bound
schema or a verified extraction from a validated parent; otherwise they remain
unverified. These section 12.6.5 requirements do not implement the verifier or
make fixture exports qualified.

#240 qualification assessment (spec 0.65 sections 12.6.2/12.6.5; RM16, RM40,
RM42, RM44; AC31, AC33, AC36, AC37): `asset_whole_task_qualification.py`
assesses every graded S3 report from its resolved inputs (fixture corpus or
fixture evidence: fixture; unqualified corpus, unknown artifact provenance,
non-target platform or any unverified input: provisional), rejects a
non-fixture label over fixture inputs, and never lets an unqualified tier pass
a gate. `plan-host` now records the preregistered input/freeze digests and
`plan-episodes` verifies frozen episodes and writes their probe plan
(#236/#237 preregistration). The specification still defines no reviewed S3
qualification record and enables no native configuration verifier, so no
report can be assessed qualified; that, genuine inputs and the host-validator
parsing (#250) remain open. The [0.65 rationale](design/2026-11-01-spec-0-65-skill-use-mcp-host-config.md)
records the decisions and remaining qualification work.

#198/#202/#204/#205/#206 evaluation contract checkpoint (spec 0.60): closed
adaptation episode, whole-task and platform exports authorize the independent
drivers/graders. Exact freeze companions, three-correction chronology, complete
paired probe/three-arm denominators, per-model costs and evidence-bound reset
remain required. The closed schemas, deterministic three-arm plan, conserving
host driver and offline graders are implemented (`asset_whole_task.py`,
`asset_episode_export.py`, `asset_platform_export.py`,
`scripts/asset_whole_task.py`; see docs/ASSET_BENCHMARK.md and the Chinese
section of docs/asset-selection.zh-CN.md) and verified over explicitly
fixture-qualified documents. Spec 0.64 adds the reset evidence document. The
graders verify the frozen corpus closure, bind host tasks and episodes to it,
check the off/on adaptation ablation, and recompute every dispatched probe's
offer. `asset_episode_driver.py` runs episodes through the real
recommendation lifecycle on the locked corpus projection (evaluation-only, so
never qualified). Native host preflight is implemented by #250; source-bound
configuration
validation/extraction and a qualified-evidence verifier remain open
(#229/#240); spec 0.65 defines
the native validation evidence required, so qualified acceptance
grades blocked, never pass. No new measured gate is claimed; genuine episodes,
paired host tasks and platform runs still need real corpora, hosts and
hardware. Read-only native RPC and genuine MCP qualification probes do not
enable runtime snapshots or further host operations. The separate spec0.62
direct-action profile above is qualified; exact selected skill/MCP use and
post-approval currentness still require their own qualification. Missing genuine
inputs do not block tooling implementation or explicitly unqualified fixture runs.

#241/#235 platform runs on GitHub-hosted runners (RM38, RM39, RM44; AC29,
AC37): the separate `Platform runs` workflow
(`.github/workflows/platform-runs.yml`; manual, or on pull requests that change
its files) runs on `macos-latest` (arm64), `windows-latest` (native x86-64) and
`ubuntu-latest`. It runs the lexical portion of the AC29 operations protocol as
a fixture (`scripts/asset_operations_run.py operations`): ten clean launches from a
generated 300-skill catalog, a recommendations-disabled memory baseline, and
the 60-second 2 req/s workload. The workload has four 25-record metadata-import
batches and a derived rebuild, driven by the existing `run_workload` through an
evaluation-only control channel (`asset_operations_child`). It then runs the
packaged install-to-uninstall lifecycle, the ordinary installed-host actions
(Linux x86-64 only; elsewhere the truthful `linux_runner_required` capture is
kept) and the platform report, and verifies everything again with the existing
graders (`scripts/asset_platform_run.py verify`). Only harness errors fail the
job.

Spec 0.65 section 12.6.5 excludes CI runners from the actual Apple Silicon macOS
/ named native x86-64 Windows acceptance. Every report stays `fixture`, and the
platform gate stays `not_applicable`. Skill use and MCP use stay `not_run`
(loaded use and MCP use remain disabled, spec 7.6.1/7.6.2), as do restart and
the interactive approvals. The committed 300-asset acceptance catalog does not
exist yet, so #241 and #235 stay open. The run is lexical-only: all-phase
worker/device memory, embedding/rebuild/inference contention, and separate
download, scan-detection and OpenClaw startup times are not measured, so it
cannot close the AC29 protocol (spec 0.65 section 12.6). No runner or local
report is committed as evidence yet.

#236/#237 run kit (tooling only): blind section 12.6.1 label templates, the
adjudication worksheet and label freeze; host-task and episode preregistration
checks that record the bound digests and acceptance-eligibility reasons; a
crash-safe journal for `run-host` and the episode driver (completed work kept,
unrecorded started work exported as `cancelled` with unknown cost, never rerun);
an operator executor-program protocol; and capture of external call usage
from RelayMind traces for the provider arms (`asset_labelling.py`,
`asset_run_journal.py`, `asset_host_runner.py`, `asset_run_kit_cli.py`).
Covered by deterministic fixtures only. #236 still needs genuine independent
labels, frozen ≥100 acceptance episodes and host-authorised corrections. The
episode driver's evaluation-only registration is not host correction
authority, and no host-driven episode procedure is specified. #237 still needs
≥100 preregistered tasks, a real host executor for all three arms, native-arm
usage capture from the host, and the #240 qualified-evidence verifier. No gate
is claimed.

#234 early-trial kit: the `trial-*` commands of `scripts/asset_whole_task.py`
(`asset_trial_kit.py`) add the Apple M1 8 GiB / pinned host / release-checksum
preflight, the pinned-CLI plugin install, and the frozen plan run through the
existing driver over operator-captured row observations, bundled with
SHA-256 digests and graded offline. #269: the install follows the host's
lifecycle (install, configure `baseUrl`, enable, inspect), because the pinned
host installs the plugin disabled until it is configured. The bundle is
private, the install record keeps no host free text, and the provisional gate
binds closed preflight/install records to the frozen package. They are verified
over fixture bundles and a host double only. The owner-run trial itself (#259),
its frozen inputs and genuine
native row observations remain open; no provisional cost has been recorded.

#201 native benchmark contract checkpoint (spec 0.61): explicit local-cold
preparation records actual gateway worker jobs/readiness, setup and owned cleanup
without fabricated HTTP or hidden query initialization. Narrow runtime
applicability permits a verified local selector runtime versus corroborated
non-use, with a closed per-arm runtime display for the comparison-only renderer.
Native build/device/execution controls, Python/dependencies,
source/label/policy and normal machine equality stay enforced. Existing 0.58
semantic documents and artifact/reference slots suffice. Parser/factory/offline
closure, local preparation and native comparison/runtime-display tooling are
implemented with explicit deterministic qualification fixtures; ordinary
artifact activation has independent exact-key review and a fresh unmodified
console-launch proof. The full 412/128 provisional source runs retain the
observed hybrid quality regression, primary failure and lexical fallback. No
host correction authority, dataset/hardware-wide qualification,
new numerical threshold or final-M10 pass is supplied by this contract.
