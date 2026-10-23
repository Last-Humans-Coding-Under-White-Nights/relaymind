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

`--runtime-extra-args='--n-gpu-layers 0 --threads 2'` adds runtime-only
llama-server arguments after the manifest's launch settings (#117; RM18,
spec 0.31 section 12.2), for a constrained run on the same machine. The
default adds none, and the arguments are recorded in the export as
`llama_server.extra_args`. Each allowed setting takes one integer value
(`--threads 2` or `--threads=2`); a positional argument, a missing,
non-integer, or out-of-range value, a `--no-` form, and any other flag
are refused (RM18). `scripts/run_corpus.py` and
`scripts/latency_benchmark.py` take the same option;
`tests/test_runtime_extra_args.py` covers it with doubles.

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
  sidecar is refused, the same sidecar in another JSON layout is
  accepted (the digest is of the canonical JSON), a lock without
  `sidecars` still checks the corpus, a `sidecars` value that is not an
  object is refused, and the corpus digest is still the one that froze
  version 1.
- `pytest tests/test_run_corpus_script.py` also covers the offline draft
  pass regrade (#115; RM31): a passing draft is published and graded as
  the local answer; a draft in the wrong language, with tool-call JSON, or
  after a non-default answer option is blocked and keeps its measured grade
  (an option at its API default does not block it); a run
  that changed the sandbox after the draft is not regraded; a draft that
  fails where the final pass succeeded shows as lost; the original export
  is never changed and the command line refuses an `--out` equal to it;
  an unknown application revision is recorded as `None`; every event the
  regrade adds passes `validate_trace_event`, with `active_ms` less the
  waits before the draft and `answer_pass_applied` `draft`; and every
  block reason the guards can return is documented in the export.
- `pytest tests/test_probe_count_multi_calls.py` and
  `pytest tests/test_probe_example_match.py`: the two spikes over small
  exports and the checked-in corpus, with no model (#119, #118): the
  section 4.3 rule (read-only, the room left in the per-stage limit of a
  local stage and in the run limit, a call that fails its own
  validation, null arguments read as `{}`, provenance from a
  clarification answer or an earlier result), the saved generations, the
  cross-check against the refusals in the trace, BM25 on a toy index,
  masking and tokens in both languages, precision, recall, and Jaccard,
  a tie that is never a pick, and the threshold search. The router
  baseline counts only the modes the export ran, and the report compares
  the two modes only when both ran; the caveats state temperature 0 and
  each skill's example pair count; both probes refuse an `--out` equal
  to the export. Results are reported per language, and the matcher's output is
  the same under six values of `PYTHONHASHSEED`.

- `pytest tests/test_local_loop.py` also pins `skill_stage_messages`
  (#114): the pure function that builds the skill-stage conversation is
  the conversation of a real run's skill-stage request, so the probes and
  the later warm-up send what the gateway sends.
- `pytest tests/test_runtime_extra_args.py` (#117; RM18, spec 0.31
  section 12.2): the default adds no launch argument, given extra
  arguments are launched after the manifest's settings and recorded as
  `extra_args` in the export, the option text is split like a shell would,
  a short flag, a pinned setting, the model, the bind address, and any
  setting that is not runtime-only (the template arguments, the reasoning
  budget, a metadata override, the slot count) are refused, so are a
  positional argument (a model path), a missing, non-integer, or
  out-of-range value, a `--no-` form, and the `--mlock` and `--mmap`
  switches b11785 does not have, each refusal names the token and never
  the value of a refused flag, a negative number is a value, every
  runtime-only setting is accepted with its value as the next token or
  after `=`, the extra arguments recorded in `docs/evidence/` pass the
  check, and the corpus runner, the latency runner, and the live harness
  take the option.
- `pytest tests/test_probe_prefix_cache.py` (#114): the body the probe
  sends equals `build_chat_request` for the same messages plus
  `cache_prompt` (written so that it still holds when the adapter itself
  sends `cache_prompt`), a group has a cold pair, warm-ups, and ten
  distinct requests per purpose, the whole `timings` and `usage` objects
  are kept, a count the runtime did not return is null and not 0, the
  summary gives the reused share per group, a failed request is a row and
  the pass goes on, and the common token prefix is computed correctly.
- `pytest tests/test_probe_cancel_prefill.py` (#117): the long prompt is
  sized to the target and fits the slot context, a repetition sends idle,
  long, cancel, short in that order, the recorded call time is measured
  and not the requested delay, the sizing and reference prompts tokenise
  like the repetition prompts, no two long prompts share a prefix, a
  definition without exactly one short-prompt pair per repetition is
  refused, a token count of zero stops the sizing with a clear error, and
  a failed short request leaves no long generation running.
- `pytest tests/test_probe_external_latency.py` (#116) over
  `httpx.MockTransport`, with no provider call: the checked-in definition
  is four arms of five, the answer arms offer no tool and carry the health
  exchange, the tool arms offer the tools of `investigate-performance`,
  the reasoning switch is the only difference between the arms of a shape,
  timings come from the clock in the order of the stream, a tool-call delta
  is the first output, usage is kept whole and a missing count is null,
  the plan is refused over its call cap, a failed call is not retried, the
  credential is sent and never recorded, and the entry point prints no
  credential.

The live runs are scripts, not tests: `scripts/run_corpus.py` and
`scripts/latency_benchmark.py` (see [`scripts/README.md`](../scripts/README.md));
their results are in [`EVIDENCE.md`](EVIDENCE.md). The three runtime and
provider timing probes of M7 (#114, #116, #117) are live scripts too
(`probes/probe_prefix_cache.py`, `probes/probe_cancel_prefill.py`, and
`probes/probe_external_latency.py`); the first two need the pinned
runtime and the model, the third calls a provider and needs its key in the
environment. Their deterministic tests above use doubles. The three offline
spikes of M7 (#115, #118, #119) are scripts over the stored corpus export
(`run_corpus.py --regrade-draft-pass`, `probes/count_multi_calls.py`, and
`probes/probe_example_match.py`; the commands are in
[`scripts/README.md`](../scripts/README.md)). They call no model, and
their results are in the 2026-10-23 entry of `EVIDENCE.md`.

### Phase 2 (spec 0.31)

Tests of the phase 2 features (RM26 to RM32), as they are implemented.
Every new option keeps the earlier behavior under its default, so each
feature has a test of the default as well. Later pull requests append here.

- `pytest tests/test_launcher.py -k budget` (#142; RM03, section 14.1): the
  `[budgets]` keys are the `LocalBudgets` field names, every key reaches
  the budgets, a file without the table keeps the defaults, a wrong type
  (text, a boolean, a float for an integer limit), a non-finite number, a
  zero or negative limit, `repair_generations` above `local_generations`,
  and an unknown key are exit status 2 with one message that starts with
  `[budgets]`, `run.accepted` records the values in force, and the commented
  block of `relaymind.example.toml` loads when uncommented and holds the
  defaults.
  The real adapters get a read timeout of at least `active_seconds` (local)
  and `external_seconds` (external), never under 30 seconds, and the local
  floor is the `LlamaCppConfig` field default, read from the module constant
  (`pytest tests/test_launcher.py -k "read_timeout"`).
- `pytest tests/test_llamacpp_translation.py tests/test_chat_completions_adapter.py`
  (#120; RM16, RM18, RM27, section 6): the usage details
  (`prompt_tokens_details.cached_tokens`,
  `completion_tokens_details.reasoning_tokens`) and llama-server's
  `timings.cache_n` fill `Usage`, the usage details win when both are sent,
  `timings` on an earlier chunk still fills the later usage chunk, a stream
  without the counts leaves them `None` and never 0, a reported 0 stays 0,
  a value that is not a non-negative integer is not measured, the
  `FirstToken` marker comes once at the first answer text, reasoning text,
  or tool-call fragment and before the call is complete (RM09), and the
  llama.cpp body carries `cache_prompt: true`.
  `tests/test_adapter_events.py` and `tests/test_timing_stats.py` no longer
  assert the old default of 0 for `cached_tokens`; spec 0.31 section 6
  forbids it (finding F9 in [`EVIDENCE.md`](EVIDENCE.md)).
- `pytest tests/test_model_call_evidence.py` (#120; RM16, RM22, RM25): the
  orchestrator stamps `first_token_ms` from the run's monotonic clock at
  the marker and not at the end of a tool-call stream, a double that sends
  no marker is dated at its first non-empty text or tool call, a generation
  with no token has `null`, `usage` holds `cached_tokens` and
  `reasoning_tokens` as `null` when the source reported none, and
  `model.started` records the declared `request_options` of an external
  call and no field when none is declared: for a local call and an external
  call without options, with the real `LlamaCppAdapter` and
  `ChatCompletionsAdapter` as with the doubles (spec 0.33 section 8).
- `pytest tests/test_trace_event.py -k model_call` (#120; RM22, section 8):
  the optional `first_token_ms`, `usage`, and `request_options` fields are
  checked by the validator and the published schema alike, from one table of
  optional model-call fields, and `schema_version` stays 1.
- `pytest tests/test_chat_completions_translation.py tests/test_launcher.py`
  (#120; RM12, RM16, section 6 and 14.1): the options reach the provider body
  verbatim and `model.started`, they never replace `model`, `messages`,
  `tools`, `stream`, or `stream_options`, and a key named `api_key` or
  `authorization` (any case), a header key (also nested), a gateway-owned
  key, a sampling key (`max_tokens`, `temperature`, `top_p`), an empty or
  blank key at any depth, or a value that is not JSON (a TOML date) stops
  startup with a message that names the key and never the value.
- `pytest -m live tests/test_llamacpp_live_smoke.py` (#120): with the
  pinned runtime first on the path, a repeated prompt reports a cached
  count above zero and the marker comes first. The live count is recorded
  in [`EVIDENCE.md`](EVIDENCE.md).
- `pytest tests/test_answer_pass.py tests/test_performance_settings.py`
  (#121; RM16, RM19, RM31, AC17): under `answer_pass = draft` a passing
  draft is published with purposes `routing, skill, skill` and no `final`,
  by the local actor, and `run.completed` records `answer_pass_applied:
  draft`; a caller `temperature` or `top_p` other than 1, or any
  `max_completion_tokens`, runs the final pass and `answer_options` records
  it, while `temperature` or `top_p` at its API default of 1 keeps the draft
  and is still recorded; a Chinese run with an
  English draft, an English run with a Chinese draft, and a draft that holds
  tool-call JSON take the final pass; a draft that misses the completion
  predicate is never published; an external answer, and an `external_only`
  run, record no `answer_pass_applied`. The default runs the final pass and
  records `final` (`tests/test_ac01_local_health.py` is unchanged), and
  `run.accepted` holds the five performance options in force.
  `PerformanceSettings` refuses a wrong value and `answer_stream = live`;
  the language guard compares by value, so a plain `"zh"` is the Chinese
  run, and an unknown language is an error.
- `pytest tests/test_launcher.py -k performance` (#121, #140; section 14.1):
  the `[performance]` keys are the `PerformanceSettings` fields, a file
  without the table keeps the defaults, each key reaches the settings, a
  wrong value or a key that does not exist is exit status 2 with one message
  that starts with `[performance]`, `answer_stream = "live"` is refused as not
  implemented, and the commented block of `relaymind.example.toml` loads
  when uncommented.
- `pytest tests/test_trace_event.py -k performance` (#121; RM22, section 8):
  the optional `run.accepted` option fields and `run.completed`
  `answer_pass_applied` are checked by the validator and the published
  schema alike, and an event without them (a run stored before 0.31) is
  valid.
- `pytest tests/test_early_headers.py tests/test_openai_sdk.py` (#140; RM14,
  RM19, RM20, RM21, RM32, AC12): with a gated model double, the status line,
  `X-RelayMind-Run-ID`, and `X-RelayMind-Events-URL` arrive while the run
  is still working, with only SSE comments in the body until the end, then
  exactly the chunks of the buffered response; the same arrive over a real
  loopback socket. A 401, a 400, a 403, and a busy slot (429) are normal
  responses with no run and no events header; an idempotent replay is
  answered from the stored run; a request without `stream` and the default
  setting are unchanged; a `needs_external` run and a failed external stage
  end with the error event (code, `run_id`, `reason_code` when there is one)
  and no `[DONE]`; a clarification streams as content; a disconnect after the
  headers cancels the run and asks the generation to stop, and the response
  waits for the run to stop, so a request right after it gets the slot,
  but no longer than `cancel_wait_seconds` when the run does not stop; a
  task cancelled before it ran still ends the run and frees the slot, and a
  cancel before it ran names the run and ends it with `cancel_requested`;
  the task-to-run mapping is gone when the task ends; the client-owned alias
  returns its call after the headers. Every SDK test runs with the option
  off and on.
- `pytest tests/test_latency_benchmark_script.py tests/test_evaluation_metrics.py`
  (#140; RM25, RM32): the benchmark config turns on only `early_headers`
  when asked, keep-alive comments are not an answer token, and the first
  transport event is labeled the header time, and not first useful content,
  when the option is on. Over a real socket a `routing` prompt under early
  headers ends with the `needs_external` error event and a deliberate close;
  the sample records `stream_error_code`, keeps its completion time, and is
  not a transport error, while a close without that event still is.
  The live check is `scripts/latency_benchmark.py --early-headers` with the
  pinned runtime first on the path (see
  [`scripts/README.md`](../scripts/README.md)); no live run with the option
  is recorded yet.
- `pytest tests/test_admitted_calls.py` and `pytest tests/test_local_budgets.py`
  (#122; RM05, RM08 to RM11, section 4.3, AC18): under `many`, two reads
  run in proposal order with their own proposal, start, and finish events,
  each charged, and the next request holds one assistant message with both
  calls and one tool message for each; three reads over the per-stage limit
  of 2, a set with more new executions than the remaining stage budget has
  room for, a mutating call with another, and one invalid call (schema,
  name, or provenance) are rejected whole with nothing run, the first
  rejection in proposal order is the one reported and belongs to the call
  that failed, the repair is used, and the rejection exchange answers every
  call with the rejection its `tool.proposed` records (`not_executed` for
  the others of a set with one invalid call; the set's own rejection for
  every call when the limit, a mutating call, or the room refused it; spec
  0.34 section 4.3); a repeated call in a set replays and is charged once; in the external stage the limit is the remaining
  `run_tool_executions` (a set of 3 passes the local limit of 2; a set over
  what the local stage left is refused). Under `one` the old rejection and its
  one-call exchange are unchanged (`tests/test_local_loop.py` passes as it
  was, but for the new `prefetch_declarations` key of `run.accepted`), and
  the client-owned alias exposes one call under `many`, and so does the
  parallel workflow's `local_health` branch (spec 0.34 section 4.4). The meter's room is
  the tighter of the stage and run limits locally, and the run limit in the
  external stage.
- `pytest tests/test_evaluation_grading.py tests/test_demo_ui_trace.py
  tests/test_live_acceptance_script.py` (#122, #123; section 12.1): several
  proposals of one generation are each counted and graded, a rejected set
  records the code on every call, a prefetch proposal is neither a model
  action nor a model proposal (tool correctness reports its n, which may be
  0), a required call is met by an admitted gateway call and not by a refused
  one, and the trace view lists the mechanism.
- `pytest tests/test_skill_registry.py -k prefetch` (#123; RM29, section
  4.2): a declaration loads and is kept on the skill; the key is optional;
  a mutating tool, a tool outside the allowlist, a constant for an identifier
  (required enum or optional pattern), a request binding for a non-identifier
  or for a pattern without a closed list, an unknown binding source or shape,
  an unknown parameter, a required argument with no binding, a constant
  outside the tool schema or of the wrong type, and an unknown field are load
  errors; constants are checked against the full tool schema with every
  allowed value of the request binding, so a constant that one value refuses
  (`if`/`then`) or that needs an unbound property (`dependentRequired`) is
  a load error, and one that `minProperties` accepts only with the
  identifier loads (spec 0.34); `prefetch_declarations` is keyed by skill with empty arrays.
- `pytest tests/test_prefetch.py` (#123; RM05, RM08 to RM10, RM12, RM14,
  RM29, AC18): with `prefetch = true` and "Is staging-api healthy?",
  `get_service_health` runs before the first skill generation
  (`tool.proposed` actor `gateway`, mechanism `prefetch`, model null), the
  first skill request holds the exchange and offers no tools, the run has
  one generation fewer, the call is charged, and `run.accepted` records the
  option and the declarations (an empty array for a skill with none); two
  services in the request, or none, prefetch nothing (a clarification
  resumes into a prefetch); two calls run at once (a barrier both handlers
  must reach) and `get_metrics` stays offered; the local actor keeps dispatch
  and the answer (RM14); a handoff carries the operations and the external
  repeat is a replay; a failed prefetch escalates like a failed proposal; a
  refused call is on the record and not run; a budget with no room starts
  nothing; limits the declarations cannot fit stop startup (and not while
  `prefetch` is false); under `false` the declarations stay idle.
  `tests/test_demo_skills.py -k prefetch` checks the section 9 declarations
  and that every English and Chinese example request resolves and its
  response still meets the predicate. `tests/test_launcher.py -k prefetch`
  checks the exit status 2 message. `tests/test_restart_recovery.py -k
  prefetch` checks the recovered event has no model.
- `pytest tests/test_trace_event.py -k gateway` (#123; RM22, section 8): a
  tool event may have model null only with actor `gateway`, the optional
  `mechanism` and `step`, and the `prefetch_declarations` shape are checked
  by the validator and the published schema alike.
- `pytest tests/test_external_criteria.py` (#141; RM06, RM12, RM13, RM16,
  AC06, AC14, spec 0.34 section 4.4): every registered predicate has
  criteria, and a passing answer has no failed check while every rejected
  answer has at least one (the predicate is true when none fails); the
  fixed part holds no run value; the values come from the latest successful
  result only, a failed result gives none, an identifier that is not a
  plain token is left out, and over the injected-logs fixture neither the
  log line nor its credential reaches them. At the gateway: the fixed part
  follows the skill body in the first system message, the values are one
  system message at the end of the request once a result exists, a new
  result replaces it instead of adding a second, and it stays last, also
  after a tool step or a correction, and the request is measured
  with it (a limit that the request fits only without it ends the run as
  `context_limit_exceeded` before any call), a rejected answer is corrected
  with the failed checks and without quoting the answer, an empty answer has
  its own check, `external_only` has no criteria, and the local correction
  and the parallel synthesis request (no criteria message, the generic
  correction) are unchanged. `tests/test_handoff_envelope.py` and
  `tests/test_ac02_handoff.py` check the order of the external system
  message: rules, skill instructions, criteria, catalog, answer
  instruction, then the handoff JSON as the last line, and that everything
  before it is the same for two runs of one skill.
- `pytest tests/test_evaluation_grading.py tests/test_evaluation_metrics.py`
  (#141; RM25, section 12.1, finding F6): the external text generations
  before the published one in a completed run are corrections, with their
  summed duration; a tool proposal, the parallel draft, an errored call, a
  failed run, and a local run have none; a missing duration leaves the sum
  unmeasured; the summary and the report give corrections per completed run
  per language and leave a grade without the figure out.
- `pytest tests/test_launcher_warmup.py` (#124; RM18, RM25, RM27, section
  6 and 14.1): `[placement]` takes `warmup` and `warmup_seconds`, a wrong
  type or a non-positive or non-finite duration is exit status 2, the
  other placement keys are unknown keys, and the commented block of
  `relaymind.example.toml` loads with the defaults. `build_app` sends no
  generation. `main` sends the routing-shaped and the skill-shaped request
  (built by the gateway's own builders) after recovery and before the
  address and the token, creates no run, and sends nothing with `warmup =
  false`. The timings come from the orchestrator's clock (a fake clock in the
  test), a count the runtime did not report stays `None` in the result and
  prints as `n/a`, a failed generation, a double that raises, and a runtime
  that does not answer within `warmup_seconds` (the generation is closed)
  are one line on standard error and the gateway still serves, and the
  short-lived adapter is closed in its own event loop. The warm-up text is
  not in the corpus or the latency prompts. `tests/test_latency_benchmark_script.py`
  and `tests/test_live_acceptance_script.py` check `--no-startup-warmup` and
  the `[placement]` table of the harness config; the latency export holds
  the warm-up samples read from the launcher line (n/a stays `None`; none
  when warm-up is off or no line was printed).
- Live, with the pinned runtime first on the path (#124): `pytest -m live
  tests/test_local_loop_live.py` and `scripts/run_corpus.py --modes
  local_only --languages en --cases simple-01 ... simple-12` ran with the
  skill-stage layout as it is and with the skill body before the
  conversation in two variants; one lowered the corpus subset and the other
  lowered AC01, so neither was kept. The runs are in
  [`EVIDENCE.md`](EVIDENCE.md).

### What is not covered yet

- The correctness corpus ran on Apple Silicon macOS only (#32). The
  latency benchmark ran on macOS (#33) and native x86-64 Windows
  (#110), closing AC15 with separate results and environment manifests.
  The 500 ms hypothesis is not met on either platform (macOS p95
  508 ms, Windows p95 3610 ms). The latency
  benchmark measures the local path only; no external first-transport
  timing was taken.

- The M3 to M5 scenarios ran live on Apple Silicon macOS with one
  external provider ([`EVIDENCE.md`](EVIDENCE.md)), and on x86-64
  Windows without an external model
  ([`EVIDENCE.md`](EVIDENCE.md#2026-10-23--launcher-start-and-latency-benchmark-on-x86-64-windows-110-ac15-native-windows));
  7 of 22 Windows scenarios are `not_run` (no external credential
  available).
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
- No test measures a cache speedup (AC13). Cache-on and cache-off runs
  of the AC01 prompts are recorded on macOS and Windows as single-pass
  observations. The Windows cache-on run has 3 hits, 4 misses, and one
  run that ended before it had a route to label; 0/8 completed in each arm.
- No production backend forwards a live provider stream yet, so the
  mid-stream failure path is tested with a backend double.
- The launcher (`relaymind serve --config`) has started with the pinned
  model on macOS (the harness above) and on x86-64 Windows (#110, five
  starts, `/readyz` 200 on all).
- Phase 2 (spec 0.31, RM26 to RM32, AC17 to AC23) is mostly specified and
  not implemented: there is no test for gated streaming, operation without
  a local model, the placement policy, the selection interface, or workflow
  packages. The budgets table (#142), the call timing and count recording
  (#120), the draft answer pass (#121, AC17), early response headers
  (#140), admitted calls per generation (#122), and prefetch (#123), both
  AC18, are implemented; their tests are in
  [Phase 2 (spec 0.31)](#phase-2-spec-031). The cached-token figures in the existing exports
  are adapter defaults (finding F9 in [`EVIDENCE.md`](EVIDENCE.md)); the
  prefix-cache probe (#114) read the runtime's own counts once, and the
  gateway records them for every model call since #120, so a run made
  after it has measured counts. The M7 to M9
  issues in [`ROADMAP.md`](ROADMAP.md) carry the tests. The offline
  spikes (#115, #118, #119) and the timing probes (#114, #116, #117) test
  their own scripts, not the gateway.
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
