# Testing RelayMind (Milestones 1 to 5)

How to verify the M1 to M5 deliverables, by hand on macOS and Windows
and with the automated suites. M1 qualifies the local model runtime and
the adapters; M2 adds the bounded local loop; M3 adds handoff,
confirmation, restart recovery, and the error contract; M4 adds the
gateway's public API and the demo UI; M5 adds the launcher, the parallel
workflow, the caches, and the browser test of the page script (see
[`ROADMAP.md`](ROADMAP.md)). M3 to M5 are covered by deterministic tests
with model doubles only;
[What is not covered yet](#what-is-not-covered-yet) lists the gaps. The
gateway's public API (`/v1/chat/completions`, `/relay/v1/runs`) is
started with `relaymind serve --config` (see
[Starting the gateway](#starting-the-gateway)); the
[README](../README.md#use) shows the requests to send it.
The manual JSON steps below target the llama-server endpoint that the
`LlamaCppAdapter` talks to.

## Automated suites

```bash
pytest                  # deterministic suite (model doubles, runs in CI)
pytest -m live          # live tests against a local llama-server
ruff check .
ruff format --check .
```

The live suite needs `llama-server` on PATH and the pinned model
artifact (see [`models/README.md`](../models/README.md)). It starts and
stops its own server; nothing below is required for it. Besides the
smoke checks, `tests/test_llamacpp_live_smoke.py` runs the live
tokenizer check: the local token count for English and Chinese prompts,
with and without tools, must equal the prompt tokens the runtime reports
(spec section 4.1).

To use the pinned GGUF from another location, set
`RELAYMIND_SMOKE_MODEL=<path>`. The file must be the pinned artifact
itself, with the manifest's SHA-256; any other file fails the run.

The live suite runs against `models/qwen3-0.6b-q8_0.manifest.json` by
default. Relative `RELAYMIND_LIVE_MANIFEST` paths are resolved from the
repository root, regardless of the current directory; absolute paths are
also accepted. Set `RELAYMIND_LIVE_MANIFEST=<manifest path>` to run it against
another manifest, for example the second local artifact
`models/qwen3-0.6b-q4_k_m.manifest.json` (AC08). The suite then checks
that manifest's runtime banner, verifies its artifact (from `models/`,
or from `RELAYMIND_SMOKE_MODEL`), and launches llama-server with its
launch settings. No code changes between the two runs. The variable
plays the part of `local_model.manifest` in the launcher config
(`relaymind serve --config`), which the live suite does not use: it
starts its own llama-server.

### Acceptance and contract tests (M2 to M5)

- AC01, deterministic: `pytest tests/test_ac01_local_health.py` runs a
  simple health request in English and Chinese through the local loop
  with model doubles (in the default suite and CI).
- AC01, live: `pytest -m live tests/test_local_loop_live.py` runs the
  real router, skill loop, executor, and sandbox against the pinned
  model on llama-server. It reuses the smoke test's fixtures, so it
  needs the same runtime and artifact, and `RELAYMIND_SMOKE_MODEL`
  works the same way. Set `RELAYMIND_TRACE_OUT=<file.json>` to write
  the full traces (the warm-up run included) that `EVIDENCE.md` records;
  nothing is written otherwise.
- AC02, deterministic: `pytest tests/test_ac02_handoff.py` runs the
  handoff with scripted local and external model doubles: the external
  request carries the original task and the completed evidence, completed
  operations are not replayed, local assumptions stay labeled proposals,
  evidence over the external context limit fails with
  `context_limit_exceeded` before any external call, and the local actor
  cannot dispatch after the handoff. `tests/test_handoff_envelope.py`
  checks the published schema against the validator. No live external
  model is run here; the live AC02 run is in the live acceptance harness
  (below), which runs it only when an external endpoint is configured.
- AC03 and AC04, deterministic: `pytest tests/test_ac03_clarification.py`
  (clarification and resume) and `pytest tests/test_ac04_invalid_proposals.py`
  (invalid proposals never execute) run with scripted model doubles in
  the default suite. There is no live AC03 or AC04 test yet.

- AC05, deterministic: `pytest tests/test_ac05_confirmation.py` runs the
  delete-demo-database confirmation with scripted model doubles on a
  disposable sandbox copy: approve (executes once), deny (cancelled with
  `user_denied`), replay of a used token, an approval older than five
  minutes (`approval_expired`), a changed target or arguments, a changed
  fixture revision, a wrong token (403), a malformed body (400), a busy
  execution slot (429), a textual yes (not an approval), and the same
  flow in the external stage. The live run is in the live acceptance harness (below).

- AC06, deterministic: `pytest tests/test_ac06_tool_output_injection.py`
  plays an injected sandbox log entry to model doubles that obey it. The
  unallowed `delete_demo_database` call is rejected, caller permissions,
  the skill allowlist, and the offered tool schemas stay unchanged, and
  the secret is absent from events, tool operations, model requests, and
  the SQLite store read back from disk. `tests/test_redaction.py` covers
  the redaction rules. The live run is in the live acceptance harness (below).

- AC07 and the error part of AC16, deterministic:
  `pytest tests/test_ac07_failure_behavior.py tests/test_error_contract.py
  tests/test_degraded_mode.py` checks bounded failure with no invented
  result when the local budget runs out or the external generation
  fails, one HTTP status and stable code per failure (spec section 11),
  and degraded mode as an operator policy recorded in the trace. The
  live run is in the live acceptance harness (below).

- AC12, restart part, deterministic: `pytest tests/test_restart_recovery.py`
  simulates a restart with a new gateway over the same SQLite file. Runs
  left in any unfinished state fail as `interrupted`; a pending write is
  `unknown_outcome` and a pending read is `failed`, with no handler run
  and no model call; a pending approval expires and its token no longer
  works; a second recovery changes nothing.

- AC12, cancel part, deterministic: `pytest tests/test_ac12_cancellation.py`
  cancels a run during a local and an external generation (the adapter,
  including the real llama-server adapter over a mock transport,
  receives cancel for that generation while it still runs, and no tool
  runs), with a provider that ignores the cancel (its late result is
  dropped) and one whose cancel hangs or fails (the run still ends
  cancelled at once), during an in-flight write (recorded as
  `unknown_outcome`, never run again, not rewritten when the abandoned
  handler finishes; the test joins the handler thread), during an
  in-flight read (`failed`), and after a write succeeded (the
  `succeeded` outcome stays). The native cancel endpoint is covered. A
  live cancel during a generation and a live restart during a generation
  (not during a write) are in the live acceptance harness (below).
- AC12, chat disconnect, deterministic:
  `pytest tests/test_chat_disconnect_cancel.py` drives the app as raw ASGI
  and checks that a client that leaves a managed chat request, with
  stream true or false, ends the run as `cancelled` with the generation
  asked to cancel, that an in-flight read is settled as `failed`, that a
  client that stays gets its answer, and that a native events
  disconnect leaves the run alone. It also covers the client-owned
  alias, a backend that owns no run, the bounded wait for the cancel, a
  request the server cancels, a live answer collected without stream, a
  receive that fails (not a disconnect), and a stored Idempotency-Key
  reply. No live model is run.

- Answer options in run.accepted, deterministic (issue #91, RM19, RM22,
  RM25, spec 0.22 sections 8 and 9): `tests/test_trace_event.py` checks the
  optional `answer_options` object in the validator and the published
  schema; `tests/test_chat_endpoint.py`, `tests/test_client_owned_tools.py`,
  and `tests/test_native_run_api.py` check what each endpoint records (the
  legacy `max_tokens` under `max_completion_tokens`, none for a run without
  options or a native run); `tests/test_demo_ui_trace.py` and
  `tests/test_demo_ui.py` check that the trace shows them and that a run
  stored earlier stays readable and shows as not recorded.
- The demo UI, deterministic (issue #28, RM23, RM25, spec 0.20 section 9):
  `pytest tests/test_demo_ui_trace.py tests/test_demo_ui.py
  tests/test_demo_ui_comparison.py` run the real orchestrator over model
  doubles and a disposable sandbox. They check that the trace view holds
  every listed field and only what the stored events hold (a missing
  field is none, never a guess), that latency is the first model action
  and not the completed answer, the persistent live, replay, and double
  labels in the shell and in every trace, escaped output, the static
  shell without a token or run data, the controls and their data
  attributes, confirmation, denial, clarification, and cancel completing
  through the native endpoints from exactly those values, replay mode with
  no controls and no model call, and the comparison: fixtures reset
  before each run (a spy and a real sandbox), runs one after the other,
  waiting runs cancelled with nothing mutated, and the refusals. The page
  script (`app.js`) is run in a real browser by
  `pytest tests/test_demo_ui_browser.py` (#89, spec 0.27): Playwright
  Chromium drives the page against the real app on a loopback socket over
  model doubles. It opens a run by ID, answers a clarification, approves,
  denies, cancels a waiting run, and runs the comparison; it asserts the
  native calls and their values, the data mode label in the shell and in
  every trace, single escaping, the shown browser timestamp, the answer
  options, the parallel workflow actors, a routing cache route, that approve
  and deny stay disabled until the page holds the token, and a replay-mode
  view with no controls. Install
  the browser once with `playwright install chromium`; a machine without
  it fails the test. CI installs it on ubuntu, macOS, and Windows. One
  live UI session is in the live acceptance harness (below).

- AC08, deterministic: `pytest tests/test_ac08_model_substitution.py`
  runs one managed scenario (a local skill and two local tools, a local
  budget stop, then a handoff that finishes the run) through the real
  orchestrator, the real llama.cpp and Chat Completions adapters over
  scripted endpoints, and one copy of the checked-in skill packages in
  which only `investigate-performance` sets `local_eligible`, as in the
  AC02 scenario; the test checks that this is the only change. It runs under
  configuration A, then under configuration B (a second local endpoint
  and model identifier, a second external endpoint, model, and
  credential variable), with no code change and one skill root. The two
  traces name their own models and are otherwise equal: routing, tool
  proposals, results, handoff content, and the request the external
  model received. The adapter layer is `tests/test_model_substitution.py`.
  In this test the second local model is a configuration value.
- AC08, live (#90): the local swap runs the same live suite twice, first
  with the default manifest (Qwen3-0.6B Q8_0), then with
  `RELAYMIND_LIVE_MANIFEST=models/qwen3-0.6b-q4_k_m.manifest.json`
  (Qwen3-0.6B Q4_K_M), on the same runtime build, code, and skill root:

  ```bash
  RELAYMIND_TRACE_OUT=a.json pytest -m live
  RELAYMIND_LIVE_MANIFEST=models/qwen3-0.6b-q4_k_m.manifest.json \
      RELAYMIND_TRACE_OUT=b.json pytest -m live
  ```

  `tests/test_local_loop_live.py` checks that every model call in a run
  names the configured artifact, and the export names the manifest, so
  each trace shows which model ran. `EVIDENCE.md` records both runs. The
  external half ran live once through the harness below
  (`--second-external-model`): the same file with only
  `[external_model] model` changed, between two models of one provider.
  Under spec 0.29 that is not a second compatible endpoint. The AC08
  external swap ran live with `--second-external-base-url` (#109), from
  Z.ai international to Zhipu bigmodel.cn, both `glm-4.6`; `EVIDENCE.md`
  records it.

- AC09 and the request part of AC16, deterministic (issue #22, spec 0.14
  section 7.1): `pytest tests/test_chat_request.py` checks the parameter
  table row by row, with an accept test and a reject test each, and the
  named `unsupported_parameter` errors; `tests/test_chat_endpoint.py`
  checks `GET /v1/models` and `POST /v1/chat/completions` over HTTP
  against the real orchestrator and model doubles (response shape, the
  `X-RelayMind-Run-ID` header, stream framing, the execution mode header
  and its policy, the 400, 401, 403, 409, 422, 429, and 500 statuses the
  endpoint can reach); `tests/test_openai_sdk.py` runs the official OpenAI
  Python SDK with `stream` true and false, in process and over a real
  loopback socket; `tests/test_api_binding.py` checks the loopback bind
  rule and the generated token. The client-owned alias is checked at the
  API seam there, with a backend double; its real backend is covered
  below (issue #24). The live SDK round trip is in the live acceptance harness (below).

- The streaming contract, deterministic (issue #23, spec 0.15 section 7.3):
  `pytest tests/test_streaming_contract.py` checks the stream framing for
  both aliases and both terminal paths, append-only content equal to the
  run's `answer.delta` events, the final generation with tools disabled,
  an unmet evidence predicate that cannot finalize, and a provider
  failure after the first chunk (an error event, no finish chunk, no
  `[DONE]`, no replacement answer), in process and with the OpenAI SDK
  over a loopback socket. It also checks that a provider failure before
  any answer text, or in a request without stream, is a normal 503
  `external_failed` response, and that a client disconnect closes the
  provider stream without recording a failure. The failing provider is a
  backend double: the client-owned backend of issue #24 buffers and
  validates its generation, so no production backend forwards a live
  provider stream yet.

- Idempotency keys, deterministic (issue #26, RM17, RM21, AC11, spec 0.18
  section 7.4): `pytest tests/test_idempotency.py` runs the real
  orchestrator over model doubles. A duplicate is proved to create no new
  work by the model double seeing no further request and the store
  holding one run. It checks the key format, the same key and payload on
  native creation and on chat (stored response, also as stream chunks,
  and client-owned tool calls), a changed payload, an unfinished or
  failed chat duplicate, a refused request that stores no key, retention
  and deletion removing the key, and the unique-key guard.
- The native run API, deterministic (issue #25, RM21, AC11, spec 0.17
  section 7.4): `pytest tests/test_native_run_api.py` runs the real
  orchestrator over model doubles inside one event loop, because a native
  run continues in the background after its 202. It checks the creation
  checks and the 202 body, the run view, event replay with
  `Last-Event-ID` (the model double, tool operations, and stored events
  are unchanged by a replay), an SSE disconnect that leaves the run
  alone, resume and cancel with stale, wrong-state, and wrong-token
  cases, the once-only confirmation token, operator policy over the
  execution modes, `local_only` returning `needs_external` with no
  external call, `parallel_demo` failing with `workflow_unavailable` when
  its configuration is missing, and
  `/healthz` and `/readyz` with explicit model warm state. One test
  reads a live stream over a loopback socket across a clarification.
  The live AC11 run is in the live acceptance harness (below).

- AC10 and the client-owned alias, deterministic (issue #24, RM08, RM19,
  spec 0.16 section 7.2): `pytest tests/test_client_owned_tools.py` runs
  the real orchestrator over model doubles. It checks the function call
  and tool-result round trip with the same call ID (in process and with
  the OpenAI SDK over a loopback socket), that the gateway executes no
  client-declared function, even one named like a gateway tool (a handler
  spy, no `tool.started`, no tool operation), that the model is offered
  only the supplied functions narrowed by `tool_choice`, arguments checked
  against the supplied schema (with local `$ref`s; an unresolvable or
  remote one is `invalid_request` and nothing is fetched), one exposed
  call per response, unique call IDs, the token budget on every
  generation, the repair limits and the context limit after a repair,
  the stop reason of an external stage, external fallback only before
  anything is exposed, the three execution modes, and the
  ownership-mixing rejections on both aliases.
  The live AC10 run is in the live acceptance harness (below).

### Live acceptance harness (#88, #104)

`scripts/live_acceptance.py` runs the M3 to M5 scenarios against the
pinned model the way an operator starts the gateway. It verifies the
runtime banner and the artifact, starts llama-server from the manifest,
and then, for each launch plan, writes a TOML file and runs
`relaymind serve --config` as a child process. It reads the bind URL
and the token from the launcher output (the token is never recorded),
checks `/readyz`, drives the scenarios over HTTP (the native run API,
the chat endpoint through the OpenAI SDK, and the demo UI in Chromium),
stops the gateway, and reads the traces back from the run store.

```bash
python scripts/live_acceptance.py \
    --out docs/evidence/<date>-live-acceptance-<machine>.json
```

The scenarios: AC05 deny and approve (with a replayed approval), AC06
through a caller's tool result and through a sandbox log entry, AC07
without an external model, after the local budget runs out, and with an
unreachable external endpoint, AC09 with `stream` false and true, AC10
call and result, AC11 replay and idempotent resubmission, AC12 cancel
during a generation and a gateway kill and restart during a generation,
one demo UI session in live mode, and the cache-off and cache-on runs of
the AC01 prompts (two rounds each), which also write one statistics
export per setting. With `--external-base-url`, `--external-model`,
`--external-context-limit`, and `RELAYMIND_EXTERNAL_API_KEY`, three more
launches run AC02, `parallel_demo`, `external_only`, AC09 and AC10 with
the `external_only` header, and AC06 through the external model; with
`--second-external-model` one more launch runs the external swap. Only a
second base URL counts for AC08 (spec 0.29): `--second-external-base-url`
with `--second-external-context-limit`, and the second
credential in `RELAYMIND_SECOND_EXTERNAL_API_KEY`; the launch file names
only the variable. A swap at the same base URL is recorded as
`external_model_swap` (RM18), not as AC08.
Without them those scenarios are recorded as not run. Every check has an owner: a gateway check is an invariant the code
keeps whatever the model does, and a model check is a capability. A
failed model check is a finding, never a pass.

`pytest tests/test_live_acceptance_script.py` (default suite) runs every
scenario against the launcher's app over model doubles.
`pytest -m live tests/test_live_acceptance.py` runs the harness against
the live model and fails if a scenario errors or a gateway check fails.
Model-quality findings remain recorded without failing that test.
The default suite also checks malformed SDK responses, missing trace
evidence, cancel/restart races, and cleanup after startup or export
failures (`tests/test_live_acceptance_review.py`,
`tests/test_live_acceptance_sdk_failures.py`, and
`tests/test_live_acceptance_lifecycle.py`).

- AC13, caching policy, deterministic (#31; RM07, RM22, RM24):
  tested with model doubles only.
  `tests/test_skill_parse_cache.py` covers the content-hash parse cache,
  `tests/test_routing_cache.py` the key, TTL, size bound, and invalidation,
  `tests/test_routing_cache_orchestrator.py` the gateway behavior (off by
  default, a hit skips the routing generation, fresh tools and current
  authorization on a hit, skill and permission changes, mutating and
  non-local routes never stored), `tests/test_routing_cache_trace.py` the
  `cache.hit` and `run.accepted` contract and the labeled statistics
  export, and `tests/test_launcher_cache.py` the `cache.routing` setting.

- AC14, parallel workflow, deterministic (#30; RM14, RM15, RM16,
  RM22, RM23): `tests/test_parallel_workflow.py` uses model doubles to
  cover both branches and the synthesis,
  branch attribution in the trace and the UI, provenance of an
  unavailable service and of the draft label, the extra call latency,
  abandoned calls in the external usage counts, failure of either branch,
  and cancel (native and chat disconnect) and restart during a branch. The
  branches overlap in a test that blocks each on the other.

### Evaluation: correctness corpus and latency benchmark (#32, #33)

Deterministic tests over model doubles:

- `pytest tests/test_evaluation_corpus.py`: the frozen corpus has 40
  English cases in the section 12.2 classes, every required field, valid
  seeds and patterns, best-effort Chinese versions linked to English
  cases, no development example among its prompts, and the digest of
  `evaluation/corpus.v1.lock.json`; a changed case or seed breaks it.
- `pytest tests/test_evaluation_grading.py`: task success, local
  completion, tool correctness and schema validity, escalation (auto
  only), missed mandatory confirmations, permissions, sandbox state, and
  provider calls, from synthetic traces.
- `pytest tests/test_evaluation_metrics.py`: nearest-rank p50 and p95,
  exact numerators and denominators per mode, language, and class,
  timeouts kept in the denominators, the section 12.3 targets, the 500 ms
  hypothesis, and the reports.
- `pytest tests/test_run_corpus_script.py` and
  `pytest tests/test_latency_benchmark_script.py`: the runners against
  the launcher's app built from their own config files, over doubles:
  clarification answers, approve and deny, a wrong call denied, timeouts,
  the seeded order, the cold and warm-up samples, and the client and
  trace timings.
- `pytest tests/test_evaluation_environment.py`: the machine manifest and
  the runtime log facts (threads, slots, model load time), and the
  checkout revision that an offline analysis records.
- `pytest tests/test_evaluation_corpus.py` also covers the skill sidecar
  (#118; RM28, spec 0.31 section 12.2): it names every corpus case once,
  each value is a demo skill or null, a Chinese case follows its English
  case, the expected skill allows a tool of every required call group,
  the lock holds its digest under `sidecars`, a changed or missing
  sidecar is refused, and the corpus digest is still the one that froze
  version 1.
- `pytest tests/test_run_corpus_script.py` also covers the offline draft
  pass regrade (#115; RM31): a passing draft is published and graded as
  the local answer; a draft in the wrong language, with tool-call JSON, or
  after an answer option is blocked and keeps its measured grade; a run
  that changed the sandbox after the draft is not regraded; a draft that
  fails where the final pass succeeded shows as lost; the original export
  is never changed.
- `pytest tests/test_probe_count_multi_calls.py` and
  `pytest tests/test_probe_example_match.py`: the two spikes over small
  exports and the checked-in corpus, with no model (#119, #118): the
  section 4.3 rule (read-only, the per-stage and remaining run limits,
  a call that fails its own validation, provenance from a clarification
  answer or an earlier result), the saved generations, the cross-check
  against the refusals in the trace, BM25 on a toy index, masking and
  tokens in both languages, precision, recall, and Jaccard, and the
  threshold search. Results are reported per language.

The live runs are scripts, not tests: `scripts/run_corpus.py` and
`scripts/latency_benchmark.py` (see [`scripts/README.md`](../scripts/README.md));
their results are in [`EVIDENCE.md`](EVIDENCE.md). The three offline
spikes of M7 (#115, #118, #119) are scripts over the stored corpus export
and call no model; their results are in the 2026-10-23 entry there.

### What is not covered yet

- The correctness corpus and the latency benchmark ran once, on Apple
  Silicon macOS only (#32, #33). The x86-64 Windows run is open (#110),
  so AC15 is not closed. The latency benchmark measures the local path
  only; no external first-transport timing was taken.

- The M3 to M5 scenarios ran live once, on Apple Silicon macOS only,
  with one external provider ([`EVIDENCE.md`](EVIDENCE.md)).
- AC08 is closed by the live local swap (#90) and the live external
  swap from Z.ai to bigmodel.cn (#109), but both endpoints are
  Zhipu's (international and mainland China platforms), so a second
  vendor's API is not covered.
- The browser test of the page script (#89) uses model doubles and a
  disposable sandbox. The one live UI session is a run of the harness,
  not a test of the page against live output.
- The parallel workflow (AC14) ran live twice against one external
  model (one completed, one draft over the 30 s limit); two single runs
  are not a latency measurement.
- No test measures a cache speedup (AC13). One cache-on run of the AC01
  prompts is recorded next to its cache-off run (single pass, macOS
  only).
- No production backend forwards a live provider stream yet, so the
  mid-stream failure path is tested with a backend double.
- The launcher (`relaymind serve --config`) has started with the pinned
  model on macOS only (the harness above); no Windows start is recorded
  (#110).
- Phase 2 (spec 0.31, RM26 to RM32, AC17 to AC23) is specified and not
  implemented: there is no test for the draft answer pass, admitted
  calls per generation, prefetch, gated streaming, operation without a
  local model, the placement policy, the selection interface, or
  workflow packages. The cached-token figures in the existing exports
  are adapter defaults (finding F9 in [`EVIDENCE.md`](EVIDENCE.md)). The M7 to M9
  issues in [`ROADMAP.md`](ROADMAP.md) carry the tests. The offline
  spikes (#115, #118, #119) test their own scripts, not the gateway.
- The selection benchmarks of spec section 12.4 (skills from
  SkillsBench, tools from ToolRet-Training-20w, workflow plans) do not
  exist yet (#143 to #145). Until they run, no claim is made about
  which selector is better outside the six-skill ops corpus.

### Required trace events (RM16, RM22, RM25)

`pytest tests/test_required_trace_events.py tests/test_trace_artifacts.py`
checks completed local runs, clarification/resume, failed and cancelled
calls, monotonic durations despite UTC clock changes, and large-result
persistence. Completed traces identify every actual model request, the
final-answer model, and the external call count. Cache-hit execution is
covered by `tests/test_routing_cache_orchestrator.py`;
`tests/test_routing_cache_trace.py` checks the `cache.hit` and
`run.accepted` event contract and the labeled statistics export (#31).
The handoff flow runs in `tests/test_ac02_handoff.py` (#18) and the confirmation flow in `tests/test_ac05_confirmation.py`
(#19).

The published `src/relaymind/store/schemas/trace_event.v1.json` and the
write-time validator require terminal timing and completed/failed call
inventories. Tool events retain their operation or proposal ID and
originating model. Interrupted model and tool calls retain finish events
before the run's terminal event.

`RunStore(max_inline_result_bytes=16384)` stores tool results larger than
that UTF-8 JSON byte threshold as SQLite artifacts. The trace contains
`result_ref` in place of `result`, with `artifact_id`, `uri`, `size_bytes`,
and `truncated`. `read_artifact(artifact_id)` retrieves the retained JSON;
its `sqlite:artifact/…` URI is an opaque store reference. Artifact content
is already redacted by the executor, commits with the event, survives
reopening, and is removed with its run by deletion or retention. The
executor and model still receive the full result; storing a reference
does not itself truncate evidence. Predicates requiring complete results
reject truncated sources instead of falling back to older evidence. The
diagnostic predicate still accepts partial observations, as its skill
contract permits.

Store migration 2 adds artifact content and measured state intervals.
The intervals let cancellation report active and waiting time separately,
including waits resumed without intervening trace events. Reopening uses
the last recorded elapsed value as its monotonic baseline; process
downtime and intervals from before the migration are not reconstructed
from wall-clock timestamps.

### Windows qualification notes

The native x86-64 Windows run for #46 used CPython 3.12.10, PowerShell
5.1, and a CPU build of the manifest's llama.cpp commit (RM18; spec
sections 3, 6, 14). The environment, build commands, failures, and passing
results are recorded in [`EVIDENCE.md`](EVIDENCE.md#2026-10-21--m1-live-smoke-test-x86-64-windows).

On that machine, a corporate proxy caused loopback health checks and
completion requests to time out. The historical #56 qualification used
the session settings below before running the automated suites from the
repository root. Ensure `llama-server.exe` is on PATH first; for the
source build in the evidence entry, use
`$env:PATH = "$build\bin\Release;$env:PATH"`, with `$build` set to the
build directory.

To reproduce that historical workaround, use a dedicated test shell: it
disabled the registry proxy for all HTTPX requests in that session,
including external destinations. CPython uses environment proxy settings (including
`NO_PROXY`) before falling back to the Windows registry. Assigning `""`
to `HTTP_PROXY` / `HTTPS_PROXY` removes those variables in PowerShell
5.1. Other configured proxy variables, such as `ALL_PROXY`, may still
apply. Windows environment variable names are case-insensitive, so only
one spelling of `NO_PROXY` is needed.

```powershell
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:HTTP_PROXY = ""
$env:HTTPS_PROXY = ""

py -3.12 -m pytest
py -3.12 -m pytest tests/test_llamacpp_live_smoke.py -m live -v
```

The official prebuilt runtime initially crashed in an older system
`MSVCP140.dll`. The successful run used the pinned source commit, MSVC
19.42, and matching VC143 CRT DLLs next to `llama-server.exe`; see the
evidence entry for the exact build flags and DLL versions. Native Windows
qualification requires neither WSL nor Docker.

Untested alternatives for the prebuilt binary: install the
[Microsoft Visual C++ Redistributable (x64)](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170),
or place the matching VC143 CRT DLLs next to the prebuilt
`llama-server.exe`. Microsoft requires a redistributable at least as
recent as the MSVC tools used to build the application. Either approach
may avoid rebuilding, but neither was tried in this run; record the
result in `EVIDENCE.md` if tested. Only the source build plus app-local
CRT is qualified here.

No manifest or tool-call parser change was needed for that historical
run. Build 11669 accepted `--chat-template-kwargs` with a deprecation
warning on all platforms. The #58 update selects b11785 and the reasoning
controls below; the old run does not qualify the new runtime.

With the #57 fix (RM01, RM18; spec section 6), `LlamaCppAdapter` and
both automated startup health-poll paths bypass environment and system
proxies for `localhost` and IPv4/IPv6 loopback literals. These paths no
longer require the session overrides above. Non-loopback endpoints and
the external Chat Completions adapter retain normal proxy handling;
process-wide proxy settings are unchanged. This policy applies to the
Python clients, not the manual terminal commands below.

`pytest tests/test_loopback_proxy.py` checks routing with controlled proxy
discovery and transport doubles, without a real proxy or model. The #56
Windows run predates this fix. The #58 hosted native Windows run now
passes without those overrides (see the new evidence entry); it does not
reproduce the earlier contributor's corporate proxy environment.

## Starting the gateway

The same command starts the gateway on macOS (Terminal) and Windows
(PowerShell 5.1+). Copy `relaymind.example.toml` to `relaymind.toml`,
start the pinned runtime as in step 1 below, then run:

```bash
relaymind serve --config relaymind.toml
```

It prints the application token once as `Application token: <token>`;
use it as the bearer token. A public bind address, a model file that does
not match its manifest, an invalid skill package, or a `[ui]` table with no
data mode stops it with a message on standard error and exit status 2.
`pytest tests/test_launcher.py` checks these paths and the single restart
recovery call with model doubles (spec 0.21 section 14.1); it does not
start a real server or model. On Windows, set the external credential with
`$env:RELAYMIND_EXTERNAL_API_KEY = "..."` in the same shell; never pass it
on the command line.

## Manual JSON testing from a terminal

### 1. Start the pinned runtime

Use the same launch settings the live smoke test pins (spec section 6:
non-thinking uses the runtime's reasoning control, not prompt strings).
Install the exact build and verify its archive as described in
[`models/README.md`](../models/README.md#runtime-and-reasoning-controls).

Start it as [`models/README.md`](../models/README.md#start-the-runtime)
shows. For a thinking-mode request, set
`chat_template_kwargs` to `{"enable_thinking": true}` and
`reasoning_format` to `"deepseek"` in the JSON body. Reasoning must appear
only in `reasoning_content`; `content` is the answer. The live suite checks
both channels while consuming the actual stream, and requires reasoning
to be present in thinking mode and absent in non-thinking mode.

Note for all Windows steps below: in Windows PowerShell 5.1, `curl` is
a built-in alias for `Invoke-WebRequest`, so the macOS curl commands
fail if pasted as-is. Type `curl.exe` explicitly (or
`Remove-Item alias:curl`) to use real curl.

### 2. Health check

This is the endpoint `LlamaCppAdapter.health()` maps to
ready / loading / unreachable.

macOS / Linux:

```bash
curl -s http://127.0.0.1:8033/health
```

Windows (PowerShell 7+):

```powershell
Invoke-RestMethod http://127.0.0.1:8033/health -SkipHttpErrorCheck
```

Expect `{"status": "ok"}` once the model is loaded (HTTP 503 means
still loading). Without `-SkipHttpErrorCheck`, PowerShell turns the
503 into a thrown error instead of showing the loading status; on
Windows PowerShell 5.1, which has no such switch, wrap the call in
`try/catch` to inspect the response status.

### 3. Send a chat completion

Checks the answer path and that non-thinking mode holds: the reply must
contain no `<think>` block.

macOS / Linux:

```bash
curl -s http://127.0.0.1:8033/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "stream": false,
    "messages": [
      {"role": "user", "content": "Reply with exactly one word: pong"}
    ]
  }'
```

Windows:

```powershell
$body = @{
    stream   = $false
    messages = @(
        @{ role = "user"; content = "Reply with exactly one word: pong" }
    )
} | ConvertTo-Json -Depth 5

(Invoke-RestMethod -Uri http://127.0.0.1:8033/v1/chat/completions `
    -Method Post -ContentType "application/json; charset=utf-8" `
    -Body $body).choices[0].message.content
```

Selecting `.choices[0].message.content` matters: PowerShell's default
formatting truncates nested objects, which would hide the content you
are checking for a leaked `<think>` block. The `charset=utf-8` matters
too — Windows PowerShell 5.1 otherwise encodes the body as Latin-1,
which mangles the Chinese prompts the bilingual tests use (spec
section 12.2).

(`curl.exe` also ships with Windows 10+; the macOS command works there
too if you keep the JSON in a file and pass `-d @body.json`.)

### 4. Send a tool-call request

Mirrors the live smoke test: the model should propose
`get_service_health` with `{"service": "staging-api"}`.

macOS / Linux:

```bash
curl -s http://127.0.0.1:8033/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "stream": false,
    "messages": [
      {"role": "system", "content": "You check service health. Use the get_service_health tool to answer health questions."},
      {"role": "user", "content": "Is staging-api healthy right now?"}
    ],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_service_health",
        "description": "Return current health for a service.",
        "parameters": {
          "type": "object",
          "properties": {"service": {"enum": ["staging-api", "payments-api"]}},
          "required": ["service"],
          "additionalProperties": false
        }
      }
    }]
  }'
```

Windows:

```powershell
$body = @{
    stream   = $false
    messages = @(
        @{ role = "system"; content = "You check service health. Use the get_service_health tool to answer health questions." }
        @{ role = "user"; content = "Is staging-api healthy right now?" }
    )
    tools = @(
        @{
            type     = "function"
            function = @{
                name        = "get_service_health"
                description = "Return current health for a service."
                parameters  = @{
                    type                 = "object"
                    # 'enum' is quoted: it is a reserved keyword in
                    # PowerShell 5.0+ and can confuse the parser unquoted.
                    properties           = @{ service = @{ 'enum' = @("staging-api", "payments-api") } }
                    required             = @("service")
                    additionalProperties = $false
                }
            }
        }
    )
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Uri http://127.0.0.1:8033/v1/chat/completions `
    -Method Post -ContentType "application/json; charset=utf-8" -Body $body |
    ConvertTo-Json -Depth 10
```

Expect `finish_reason` `"tool_calls"` and a `tool_calls` entry naming
`get_service_health` with arguments `{"service": "staging-api"}`. Note
that on the wire `function.arguments` is a JSON-encoded **string**
(`"{\"service\": \"staging-api\"}"`), not a nested object — that is
the Chat Completions protocol, not a bug. RelayMind's adapters parse
it before any tool call exists (RM09).

## What manual checks do not replace

Manual terminal checks are for exploration and platform qualification
notes (the spec's platform matrix includes Windows). Evidence for
closing a milestone comes from the recorded automated runs: the
deterministic suite plus the marked live tests (spec section 13, RM16:
a manual poke is not a measured result). Live runs are recorded in
[`EVIDENCE.md`](EVIDENCE.md) with the machine, runtime build, manifest,
command, and result.

## Runtime update qualification in CI

The **Runtime smoke** workflow runs the marked smoke file on a native
`windows-2022` x86-64 GitHub-hosted runner when a PR changes runtime,
adapter, or smoke-test files. It downloads the pinned CPU runtime and
model, verifies the runtime archive checksum, and lets the fixture verify
the model hashes and runtime banner. It uses neither WSL nor Docker.
Normal CI remains deterministic and excludes live tests.

The workflow uploads machine details, the manifest, runtime banner,
pytest output, JUnit results, and server logs as `runtime-smoke-windows`.
Seven passing cases with zero skips are required (non-thinking,
thinking, the tool call, and four token-count checks). A hosted Windows run
qualifies that Windows Server/CPU environment, not every Windows desktop
or GPU configuration. Repeat the same smoke file on Apple Silicon and
record both results in `EVIDENCE.md` before claiming runtime qualification.
The smoke suite is separate from the AC15/M6 benchmark.
