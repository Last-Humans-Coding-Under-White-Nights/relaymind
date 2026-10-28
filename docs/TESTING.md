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
  waiting runs cancelled with nothing mutated, and the refusals.
  Phase 2 trace tests (#127; RM12, RM23, RM25–RM29, RM31) also check
  recorded configuration, selector attempts for every route, routing-call
  timing joined by call ID, first-token timing including zero, applied answer
  pass, escaped labels, missing historical fields (not recorded), and
  values recorded as null (shown with their meaning). Placement projections
  use illustrative optional event fields; they are not measured predictions.
  The Workflow steps placeholder is removed by #173 (RM30 retired).

  The page script (`app.js`) runs in a real browser with
  `pytest tests/test_demo_ui_browser.py` (#89, #127, spec 0.27 and 0.31): Playwright
  Chromium drives the page against the real app on a loopback socket over
  model doubles. It opens a run by ID, answers a clarification, approves,
  denies, cancels a waiting run, and runs the comparison; it asserts the
  native calls and their values, the data mode label in the shell and in
  every trace, single escaping, the shown browser timestamp, the answer
  options, the parallel workflow actors, a routing cache route, that approve
  and deny stay disabled until the page holds the token, and a replay-mode
  view with no controls. The phase 2 test checks the no-local status and
  recorded options in both the shell and trace, that the shell labels are
  visible while the configuration panel is closed, plus gateway prefetch
  labels.
  Install
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
  `PerformanceSettings` refuses a wrong value; `answer_stream = live` is an opt-in gate;
  the language guard compares by value, so a plain `"zh"` is the Chinese
  run, and an unknown language is an error.
- `pytest tests/test_launcher.py -k performance` (#121, #140; section 14.1):
  the `[performance]` keys are the `PerformanceSettings` fields, a file
  without the table keeps the defaults, each key reaches the settings, a
  wrong value or a key that does not exist is exit status 2 with one message
  that starts with `[performance]`, `answer_stream = "live"` enables the gate,
  and the commented block of `relaymind.example.toml` loads
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
  AC06, AC14, spec 0.35 section 4.4): every registered predicate has
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
- `pytest tests/test_selection_interface.py` (#131; RM02, RM05, RM08,
  RM22, RM28, AC22 in process, sections 8, 11, and 14.1): the two
  published schemas are closed with `schema_version` 1, and a request and
  a result are checked at the boundary (an unknown field, a candidate
  without its skill or text, a confidence that is out of range, infinite,
  NaN, or a boolean). The chain runs scripted selectors and records each
  attempt: none, a raised error, an invalid result, an unknown skill
  (inadmissible), a result that names another selector or a reserved
  mechanism (error), and a selector past the time limit (timeout, and
  cancelled), driven by an injected sleep, never the wall clock. A
  model-based selector has no time limit and no deadline, and a failure of
  the gateway's routing generation ends the chain even when the selector
  swallows it. `model_router` sends exactly the router's request through
  the injected generation callback and reports rejected routing output as a
  selector error. In the gateway: the default chain records `selector` and
  every attempt on `route.selected` with the local model's actor and the
  routing fields of before; another selector's choice has actor `gateway`
  and its own mechanism and no routing generation runs; the request carries
  the redacted conversation and the catalog with the `examples.json`
  requests in both languages; narrowing only removes tools (a tool outside
  the allowlist is dropped, and narrowing to none ends as
  `capability_missing`); a selected mutating skill still waits for
  confirmation; a selector clarification and a missing identifier are the
  gateway's clarify decisions; an exhausted chain records `selector_chain`
  with `selection_failed` and hands off with the catalog, or ends as
  `needs_external`; alternatives are ignored; a rejected `model_router`
  output alone still escalates with `invalid_output` and no
  `route.selected`; only a routing generation's choice enters the routing
  cache; `external_only` and the oversized-input bypass run no selector;
  and `SelectionSettings` refuses a wrong chain, time limit, or threshold.
  The routing tests (`tests/test_router.py`, `tests/test_ac01_local_health.py`,
  `tests/test_ac03_clarification.py`, `tests/test_ac04_invalid_proposals.py`,
  and `tests/test_routing_cache*.py`) run through `model_router` with the
  same decisions; the only edits there are the spec 0.31 additions: three
  reason codes, `selection.timeout_seconds` inside `budgets`, and the
  routing cache policy version 2 with the selector chain in the key.
  `tests/test_route_decision.py` checks that the reason code follows the
  route, and `tests/test_trace_event.py -k selection_fields` checks the new
  optional fields in the validator and the published schema alike.
- `pytest tests/test_example_match.py` (#132; RM01, RM05, RM28, section
  14.1): the catalog carries the `examples.json` requests of both
  languages; an unrelated request (English, Chinese, or empty) returns
  none; changing only the service name keeps the pick and the score; scores
  are in [0, 1] and ranked; both thresholds come from the configuration;
  a one-skill catalog compares with 0; a tie is none; the index is rebuilt
  only when a content hash changes; the ranking is the same under three hash
  seeds; and in the gateway the chain `["example_match"]` makes no routing
  generation and records actor `gateway`. The leave-one-out test records
  the measured result on the twenty demo example requests at the default
  thresholds, not a target: English 4 right, 1 wrong, 5 none; Chinese 1
  right, 1 wrong, 8 none; the held-out request's own skill ranks first for
  13 of 20.
- `pytest tests/test_launcher.py -k selection` (#131, #132; section 14.1):
  the `[selection]` keys are the `SelectionSettings` fields, every key
  reaches the chain, a file without the table runs `model_router` alone, a
  wrong type or range, an unknown selector, a repeated one, an empty chain,
  and an incomplete `http` selector table are exit status 2 with
  one `[selection]` message, `run.accepted` records the values, and the
  commented block of `relaymind.example.toml` loads with the defaults.
- `pytest tests/test_ac20_no_local.py` (#126; AC20, RM01, RM05, RM11,
  RM21, RM26, RM28, RM29, sections 7.4, 8, and 11): each scenario runs in
  the three classes, absent (no local model), unavailable, and unsuitable,
  with `build_gateway(local_status=...)`. A simple request is selected by
  `example_match` without a routing generation (`route.selected` with actor
  `gateway`, reason `no_local_model`, and `local_model_status`) and answered
  by the external model with the skill body in its system message; the
  completion predicate corrects a wrong answer; with `prefetch = true` the
  declared call runs after `handoff.started` and the first external
  generation is offered no tools; a missing service name gets the gateway's
  clarification, and the answer resumes into the external stage; a delete
  waits for confirmation in the external stage; a request no selector
  places hands off with the catalog (`selection_failed`). `local_only` and
  `parallel_demo` are 403 `execution_mode_forbidden` over the native and
  the chat API while the class forbids them, and before a run exists when
  the orchestrator is called directly; `local_only` still runs locally while
  the model is unsuitable. `run.accepted` records `local_model` (null only
  when absent), `local_model_status`, and `local_model_reason`; a ready model
  records `ready`. `/readyz` reports the class in `local_model`, auto is
  ready when the external model is, and an unavailable model is never shown
  available. With detection on, readiness classifies a runtime that stops
  answering or whose model is no longer loaded as unavailable. Recovery
  waits for the model to be warm and restores ready or its earlier unsuitable
  classification. With detection off the class never changes. `external_only`
  keeps its own `route.selected`, degraded mode never applies, a gateway
  without an external model ends as `needs_external`, and the client-owned
  alias runs on the external model. Every model output is a scripted double;
  the local double has no turns, so any local call fails the test. The UI
  clause of AC20 is #127.
- `pytest tests/test_ac20_no_local_chinese.py` (AC20, RM05, RM26, RM28)
  covers the demo skill's Chinese examples in all three no-local classes,
  Chinese clarification for a missing service, and an identifier-only resume
  that preserves `language: zh`, with no local generation.
- `pytest tests/test_launcher.py -k local_model` and
  `pytest tests/test_launcher_warmup.py` (#126; RM26, section 14.1): a file without
  `[local_model]` starts, with `auto` and `external_only` and the
  `example_match` chain as defaults, and its documented variant of
  `relaymind.example.toml` loads; a file with neither model table, local
  modes, or `model_router` without a local model is exit status 2. Detection
  runs over doubles with a fake clock and an injected sleep: a runtime that
  never answers is unavailable after `warmup_seconds`, a model that is
  still loading is waited for, a routing warm-up over `active_seconds` is
  unsuitable (at the limit is ready, and a raised limit is the one used), a
  failed warm-up, or one cut off before the stage budget, is unavailable,
  and one cut off at the budget is unsuitable; `warmup = false` checks only
  availability and `detect = false` keeps the earlier warm-up. `main`
  prints one `Local model:` line with the class and the cause, and none
  for a ready model. `tests/test_local_status.py` covers the modes each
  class forbids, and `tests/test_trace_event.py -k local_model_fields`
  checks the new optional fields in the validator and the published schema
  alike.
- `pytest tests/test_selection_benchmark.py tests/test_selection_benchmark_review.py`
  (#143, #144; RM02, RM08, RM09, RM25, RM28, section 12.4): the selection
  benchmarks. They cover frozen digests, set scoring and macro/micro
  summaries, leave-one-task-out examples, router catalog caps and context
  bypasses, both source tool schema styles, collision suffixes that do not
  depend on which candidate is positive, seeded candidate presentation, inert
  handlers, loopback HTTP selectors, proposal validation and schema validity
  (only `unknown_tool` and `invalid_arguments` count against it), failed and
  abstaining narrowing arms, call-budget scoring, and source derivation with
  lock and archive digest checks. ToolRet fixture rows are synthetic; no
  download or model call is needed in the default test suite. Real benchmark
  commands and the optional `bench` extra are described in
  [`scripts/README.md`](../scripts/README.md).
- `pytest tests/test_runner_configurations.py` (#128; RM03, RM25, RM26,
  RM29, RM31, RM32): the phase 2 runner configurations, alongside the corpus
  and latency script tests. They check launch-file round trips, validation,
  no-local runtime bypass, external prerequisites, export/report labels,
  declared external request options (spec 0.46: checked like the launch file,
  recorded only when declared, and part of the prior partition) and offline
  regrading. Grading and metric regressions distinguish gateway-owned
  answers from local-model completion and report no-local completion as not
  applicable. These deterministic checks do not constitute the live
  evaluation matrix or evidence for changing defaults.
- `pytest tests/test_model_timings.py tests/test_run_store.py tests/test_launcher_warmup.py`
  (#129; RM17, RM25, RM27, spec 0.38): timing storage and placement inputs.
  They check the previous-schema upgrade, validated sample imports, seven-day
  retention, explicit deletion and deterministic 1,000-sample bounds.
  Synthetic measurements check medians, separate warm/cold rates, unknown
  usage and zero-duration intervals. Model doubles cover managed
  local/external, client-owned, fixed-workflow, failed, cancelled and warm-up
  calls. Terminal outcome tests distinguish local completion from escalation,
  omit routing-only and local-only failures and parallel workflow runs,
  isolate external models, and verify transaction rollback and estimate reads
  without event-history scans. These are deterministic checks, not live
  performance evidence.
- `pytest tests/test_placement.py tests/test_idle_probe.py` (#130; AC21,
  RM03, RM04, RM17, RM21, RM22, RM27, RM28, spec 0.39): device placement and
  idle maintenance. Measured fixtures and fake clocks check production-only
  output estimates, latest prefix classes, exact tokenization requests,
  policy exemptions, hard caps, expected-wait observations, stage/run
  deadline precedence and rejected late output. The same generation boundary
  covers client-owned calls and fixed parallel branches. Readiness uses
  representative samples without tokenization and reports no routing fit
  without a usable local model. A routing decline stays on route.selected
  and is not carried by a later transition. Probe tests cover SQLite slot
  races across connections, failed/cancelled cooldowns, the scheduler's wait
  and recovery after a failed check, confirmation rollback, input resume,
  cancellation-resistant shutdown and genuine restart cleanup. New trace
  writes validate the closed placement object; historical UI readers still
  accept missing or previously unconstrained fields. No test claims live
  performance.

- `pytest tests/test_http_selector.py tests/test_http_selector_review.py`
  (#133, spec 0.40) covers RM03, RM08, RM11, RM12, RM14,
  RM16, RM22, RM25, RM26, RM28 and AC22. Ephemeral literal-loopback servers
  exercise actual POST requests: configured identity, closed startup settings,
  URL policy (plain http only) without credential echoes, proxy and redirect
  isolation, exactly one request, schema/identity/mechanism errors, the response
  size cap, unknown versus empty claims, admission, redaction, timeout, gateway
  budget precedence and cancellation. The actual stdlib example script is started
  on an ephemeral port. Clients close per attempt and remain usable across
  separate event loops. `tests/test_launcher.py` checks that a non-loopback or
  https selector URL stops `relaymind serve` with one message, before a socket and
  without echoing the URL.

  Corpus and latency exports classify HTTP-attempted, gateway-observed and historical
  unknown provenance, including their Markdown reports; a run that reached no selector
  (routing cache hit, input-cap bypass, execution mode, client owned) is gateway observed.
  HTTP internal model cost remains unverified and does not enter measured call/token totals.

  Trace tests check Python and published JSON Schema compatibility with historical
  attempts and rejection of partial or malformed new evidence. Gateway tests retain
  HTTP attempts on successful fallback, failed routing and run cancellation without
  inventing routes or adding claims to measured model calls. Unknown skills and
  foreign tools execute nothing; mutations still wait for confirmation. Both frozen
  benchmark tracks use the production HTTP boundary, preserve failed-case claims,
  and retain every failed case in the denominator. This deterministic coverage and
  the keyword SkillsBench export are transport evidence; no live model capability
  or useful keyword accuracy is implied.

- `pytest tests/test_gated_streaming.py tests/test_completion_predicates.py`
  (#125; RM12, RM17, RM20, RM32, AC19): verified live answer publication
  uses deterministic model doubles. It exercises cumulative safety and final
  completeness against the existing bilingual predicate cases, exact delimiter
  preservation, multiline checklist scope, full-answer redaction, early and late
  rejection, absent completion markers, provider failures, cancellation, deadline
  ties, persisted call identity, synthesis after joining, and failed persistence.
  The managed SSE tests include real socket/standard SDK error parsing, with
  early headers on and off, plus idempotent replay; raw ASGI tests synchronize
  on generated/published content before disconnecting. They make no inference
  calls and do not measure task-success parity or a latency improvement.

  The gate retains raw context privately. For checklist answers it waits until
  the first complete draft label, then verifies complete lines. With known secret
  values it conservatively withholds the whole answer so published text exactly
  matches the existing full-answer scrub, including overlapping secrets. These
  choices can delay first content. The predicates retain their documented
  syntactic limitations; the performance predicate is a section/evidence check,
  not a general factual verifier.

  The opt-in `performance.answer_stream = "live"` applies to native managed runs
  and streamed managed chat. Non-streamed chat and client-owned calls remain
  effectively buffered. Corpus grades retain configured/effective policy and
  terminal failure code, and report task success and `predicate_unmet` separately
  for each effective policy. The actual paired provider corpus run on one final
  revision and frozen timing prior is still required before changing defaults.

- M8 evaluation infrastructure (RM17, RM25–RM28, RM32; specification 14.4).

  Run the importer, local-phase clock, metric, checkpoint, matrix and runner
  regressions with model doubles before executing the matrix:

  ```sh
  .venv/bin/python -m pytest -o pythonpath=src tests/test_placement_prior.py tests/test_local_active_measurement.py tests/test_m8_metrics.py tests/test_evaluation_checkpoints.py tests/test_m8_matrix_driver.py tests/test_matrix_controller_recovery.py tests/test_m8_analysis.py tests/test_runner_configurations.py tests/test_run_corpus_script.py tests/test_latency_benchmark_script.py
  ```

  The prior tests use declared synthetic test fixtures to validate projection and
  retention, including an expected-wait decision from imported terminal history.
  They establish no performance, provider parity, or model capability. The
  checkpoint tests verify preservation/refusal behavior without inference.
  `tests/test_m8_analysis.py` checks the offline M8 report on a relocated
  fixture matrix: full 31-cell acceptance, inventory mode, paired comparisons,
  cost ledgers, hypothesis verdicts (including candidate timeouts that would hide
  behind an observed-value p95), and refusal of existing, checkout and recorded-root
  outputs. It reads no real measurement.
  Repository completion checks remain the entire pytest suite and Ruff lint/format.

### OpenClaw host transport (#135; RM33, AC24)

The pinned OpenClaw transport (`@openclaw/ai` 2026.10.7, under the host
transport policy of `openclaw` 2026.10.7) runs against the real gateway
over model doubles. It needs Node `>=24.22.1 <25` (or `>=26.12.1`) and
the package's dependencies:

```bash
cd integrations/openclaw && npm ci && npm run typecheck && npm test
cd ../.. && pytest tests/test_openclaw_host_transport.py \
  tests/test_openclaw_qualification_script.py tests/test_openclaw_request_size.py \
  tests/test_openclaw_host_captures.py
```

- `integrations/openclaw/test/compat.test.ts`: every declared host
  switch, and that the pinned transport resolves them for a loopback URL.
- `integrations/openclaw/test/fixtures.test.ts`: the frozen transport
  fixtures carry the pins, hold no credential, send only section 7.1
  fields and message keys under the declared switches, turn every gateway
  error into a host failure, and record the tool round trip and the abort.
- `tests/test_openclaw_host_transport.py`: the Node driver
  (`integrations/openclaw/test/host-transport/drive.mjs`) plays the host
  against a gateway on a loopback socket: the host defaults that 7.1
  rejects (`stream_options`, `reasoning_effort`, a late developer message
  with the switch on), the declared switches, streamed and buffered text,
  a host tool round trip (the host executes it once; the gateway records
  `tool.proposed` and no `tool.started`), an operator
  `parallel_tool_calls: true`, oversized requests in auto, local_only,
  and over both limits, the advertised window the host enforces before
  sending, external_only, a stale token, and an abort during a buffered
  generation (the run ends cancelled with cancel_requested).
- `tests/test_openclaw_qualification_script.py`: the harness's host mode
  keeps the token out of its export and records a missing command as
  not_run; a fresh capture reproduces the committed fixtures.
- `tests/test_openclaw_host_captures.py`: the real host's captured
  requests (macOS probe, `docs/evidence/2026-10-27-openclaw-mac-probe/`)
  against the gateway's validator: the loopback defaults are rejected by
  name, the switches on a config-file provider keep every request inside 7.1
  (the one plugin-model run declared none), `strictMessageKeys:
  true` breaks the tool linkage, and the host's rewritten call ID still
  passes the same-request check. No Node needed.
- `tests/test_openclaw_request_size.py`: the size report over captured
  requests, with its method on every number; over the UTF-8 bound a
  request is undecided, a sanitized capture's size is a lower bound that
  never proves a fit, and a byte bound under the external limit still
  counts as an external fit.
- `tests/test_openclaw_mac_qualification.py`: the measured macOS reports
  (`docs/evidence/2026-10-27-openclaw-mac-qualification/`): by the pinned
  tokenizer no host request fits the 8192 local limit, the live AC24 turn
  in auto without an external model (config-row workaround, #136) ended
  needs_external / context_limit with 0 model calls, and the evidence
  record and design doc cite the measured report. No Node needed.

### OpenClaw provider plugin (#136; RM08, RM12, RM19, RM26, RM33)

```bash
cd integrations/openclaw && npm run typecheck && npm test && npm run pack:check
cd ../.. && pytest tests/test_openclaw_plugin.py
```

- `integrations/openclaw/test/manifest.test.ts`: the plugin manifest (provider,
  token env var, api-key choice, closed config schema matching the plugin's
  checks), the package pins and compatibility range, the built entry, and
  that the sources import only published `openclaw/plugin-sdk` subpaths.
- `integrations/openclaw/test/config.test.ts`: loopback-only `baseUrl`
  (localhost, 127.0.0.0/8, `[::1]`; no credentials, query, or other path),
  `executionMode` (auto by default; parallel_demo and unknown values refused
  by name, a value that may be a secret not echoed), positive integer limits,
  and the closed key set.
- `integrations/openclaw/test/catalog.test.ts`: discovery with the token and
  no redirect; the published entry (switches, mode header, the window of the
  mode from the copied limits, the output limit, or none); the diagnoses for
  no token, an unreachable gateway, a 401 with the reconnect instruction, a
  redirect, another error, an absent alias, and the configured mode's
  readiness (an aggregate 503 with external_only ready stays usable); the
  host outcome of each.
- `integrations/openclaw/test/provider.test.ts`: registration through
  `definePluginEntry`, one provider with the api-key method, an invalid or
  missing configuration refused before registering, `resolveDynamicModel`
  (the alias entry only, with the catalog's switches, mode header, and
  configured limits, synchronous, no request, no credential; the headless
  `agent exec` path of the macOS host probe), the token handover (including
  that `models auth login` needs a terminal) and reconnect messages, the
  catalog hook with the host credential and redacted logs, the plugin's own
  discovery timeout when the host gives no abort signal (as the probe
  observed), and the capability statement.
- `integrations/openclaw/test/redaction.test.ts`: the token and any bearer
  credential are scrubbed at every log level.
- `npm run pack:check`: builds `dist/`, and checks that the packed tarball
  holds only the built entry, the manifest, the README, and `package.json`,
  and that the built code imports only public SDK subpaths.
- `tests/test_openclaw_plugin.py`: the plugin driver
  (`integrations/openclaw/test/plugin-transport/drive-plugin.mjs`) registers
  the plugin, runs its catalog hook against a gateway on a loopback socket,
  and sends one request through the pinned transport with the published
  entry: the default streamed request passes section 7.1 with no gateway
  change, the dynamically resolved entry makes the same request without
  any discovery request, a resolved alias the gateway does not serve fails
  rather than answering empty, the copied limits are the advertised ones, a stale token after a
  restart is 401 with the reconnect line and the fresh token restores
  access, an unreachable gateway is not an auth failure, a gateway without
  client-owned execution publishes nothing, external_only runs without a
  local model, local_only outside the policy is named as a policy refusal,
  local_only permitted without a local model is diagnosed as not ready and
  refused by the gateway through the local model's class (RM26) with no
  external call, local_only makes no external call, and parallel_demo is
  refused before anything is sent.

- `tests/test_openclaw_vps_functional.py`: reads the Linux VPS functional
  check (not a supported-platform qualification) of this package on the
  real pinned host against the gateway over doubles
  (`docs/evidence/2026-10-28-openclaw-vps-functional-136/`): `agent exec`
  found the alias before any catalog refresh (`resolveDynamicModel`), the
  host honored the plugin-supplied model's switches on the wire (no
  `stream_options`, section 7.5), the stale-token 401 line and recovery,
  the host's schema refusing parallel_demo, the command forms (Q4), and
  what uninstall leaves until `models auth logout` and `config unset`
  remove it. It also checks that the README gives those forms.
- `integrations/openclaw/test/manifest.test.ts` also checks that the
  `configSchema` enum leaves parallel_demo out, which is what makes the
  host refuse it before any plugin code runs.

The check itself is rerun with
`docs/evidence/2026-10-28-openclaw-vps-functional-136/scaffold/vps_functional.py`
(the pinned host from `integrations/openclaw/node_modules`, host state in a
temp dir; a few minutes, no model). The same steps on macOS and Windows for
this package are not_run
([`integrations/openclaw/README.md`](../integrations/openclaw/README.md)).

### OpenClaw round trips (#137; RM01, RM03, RM04, RM08, RM09, RM12, RM19, RM20, RM26, RM33; AC10, AC24)

```bash
cd integrations/openclaw && npm run typecheck && npm test
cd ../.. && pytest tests/test_openclaw_round_trips.py tests/test_openclaw_qualification_script.py \
  tests/test_openclaw_vps_functional_round_trips.py
```

- `integrations/openclaw/test/input-guard.test.ts`: the guard refuses, by
  field, an image part in a user message or a tool result (the pinned host
  would replace it with "(image omitted: ...)" text), a structured response
  format, `parallel_tool_calls: true` (quoting `unsupported_parameter`), a
  strict tool, `n` other than 1, a non-text content part, and a payload
  that lost a host tool; a text and tools request passes unchanged.
- `integrations/openclaw/test/stream.test.ts`: the provider's stream
  wrapper over the pinned transport, against scripted gateway replies
  (the #135 fixtures and synthetic streams): a text replay, a non-streamed
  answer, one complete call, keep-alive comments that are not content, an
  error event after content or before any (early headers), a stream with no
  finish chunk, malformed JSON, and an HTTP error are failures; an empty or
  blank answer and two calls in one response are failures the plugin adds;
  refused input sends no request; the host's own payload hook still runs.
- `integrations/openclaw/test/provider.test.ts`: `wrapStreamFn` wraps the
  host's stream and turns an empty success into a failure.
- `integrations/openclaw/test/host-tool-plugin.test.ts`: the test-only
  host tool plugin (`test/host-tool-plugin/`): the echo and write tools,
  the approval hook on every write, a write that stays in the sandbox, and
  a manifest whose `contracts.tools` names every tool it registers (the
  pinned host refuses the tools otherwise, Q9).
- `tests/test_openclaw_round_trips.py`: the plugin driver sends the host's
  conversation through the pinned transport wrapped by the plugin, and
  plays the host's tool execution and approval policy. Text and a host tool
  round trip, streamed and buffered; tool_choice none, required, and named;
  arguments that break the schema never reach the host; matching call and
  result IDs; one call exposed of two; a host tool named like a gateway tool
  that the gateway never runs; a sandbox mutation the host denies (no side
  effect) and allows (exactly one line); a tool result passed as data and a
  secret argument redacted in the trace; auto fallback before output with
  one answer owner, local_only with zero external calls, external_only
  without a local model, an oversized catalog in auto (external) and
  local_only (needs_external, no external call), with every host tool
  forwarded; budgets as configured and no managed selection; early-header
  comments that are not content; the plugin's refusals before sending.
  Recorded at the pin: the host sends its tool catalog ordered by name.
  The driver replays each call ID the way the real host did on the macOS
  probe (`call_echo_1` becomes `callecho1`), so the tests correlate the
  proposal and the result through the host's rule, never by the exact
  issued ID (section 7.5); a verbatim replay is accepted as well.
- `tests/test_openclaw_qualification_script.py`: the host's ID rule
  (`host_replayed_call_id`: the observed underscore removal, and the
  pinned source's `[A-Za-z0-9]`, 40 characters) and `answers_call`; the
  default `text-tool` scenario passing in one host turn (the echo call,
  then the closing text; a text-only turn does not pass, Q8); the host
  mode's `--scenario exec-deny|exec-full|tool-deny|tool-approve`
  checks against a stub host: the sandbox lines, no gateway tool
  execution, the proposed call, and the host's result in the next run,
  also when the host rewrote the call ID (kept as `replayed_call_ids`);
  the host's own JSON report kept
  as `host_report` and never deciding a pass (a denied tool with
  `"ok": true` does not pass an allow scenario); a denial that wrote or
  ran anyway and a scenario that did not run never pass.

- `tests/test_openclaw_vps_functional_round_trips.py`: reads the Linux VPS
  functional check (not a supported-platform qualification) of the harness
  host scenarios on the real pinned host against the gateway over doubles
  (`docs/evidence/2026-10-28-openclaw-vps-functional-137/`): the test host
  tools registered, `text-tool` passed in one `agent exec`, the headless
  plugin approval failed closed (`tool-deny` passed, `tool-approve` failed
  with no line written though the host said `ok`), `exec-deny` and
  `exec-full` passed, and every call ID came back without underscores. It
  also checks that the README gives the command forms that worked.

The deny and allow decisions in the first five come from the driver or a
stub standing in for the host's policy. The real host's decisions are in the
Linux VPS functional check, rerun with
`docs/evidence/2026-10-28-openclaw-vps-functional-137/scaffold/vps_functional.py`
(a few minutes, no model). A plugin approval answered by an operator at a
running OpenClaw Gateway, and these scenarios on macOS and Windows, are
not_run (the exact host commands are in
[`integrations/openclaw/README.md`](../integrations/openclaw/README.md)).

The Node-dependent tests skip with the reason when `node` or
`integrations/openclaw/node_modules` is missing; `RELAYMIND_OPENCLAW_NODE=1`
makes that a failure. This tier runs the host's transport, not its agent
loop: the real-host run against the gateway
(`scripts/openclaw_qualification.py host`) is an operator step
([`integrations/openclaw/README.md`](../integrations/openclaw/README.md));
its one macOS run at `a52f8bf` failed (`docs/EVIDENCE.md`, #136, #137),
and the Linux VPS functional check ran it on the pinned host (above).
The host request capture ran on macOS against a capture server (the host
probe), and its requests are checked offline by
`tests/test_openclaw_host_captures.py`.

### OpenClaw cancellation, errors, retries, and run IDs (#138; RM12, RM14, RM16, RM17, RM20, RM21, RM22, RM25, RM33; AC11, AC12, AC24)

```bash
cd integrations/openclaw && npm run typecheck && npm test
cd ../.. && pytest tests/test_openclaw_cancel_errors.py tests/test_openclaw_qualification_script.py \
  tests/test_openclaw_vps_functional_cancel_errors.py
```

- `integrations/openclaw/test/errors.test.ts`: every gateway code of
  section 11 read from the pinned transport's failed message (code, status,
  run ID, reason code; a cut body still gives the code) and mapped to the
  host's class (`context_limit_exceeded` the only context overflow,
  `needs_external` with a local context limit not one, 429 overload, 504
  timeout, 401 authentication with the exact reconnect instruction,
  `needs_external` with any reason code, `execution_mode_forbidden`,
  `idempotency_conflict`, and `run_terminal` the request format class, the
  rest failures of this provider, `external_failed` whatever its reason
  code), read back from the message alone and kept by the pinned host's own
  classifier, with the pinned host's same-model retry classes read from its
  build (`test/pinned-host.ts`; spec 0.49 section 7.5); the
  message is one line, bounded, without a known token or a bearer value;
  errors that are not RelayMind's are left to the host; only 429 and 504
  may be retried.
- `integrations/openclaw/test/idempotency.test.ts`: the key is the same for
  the same payload and mode and 1 to 255 printable ASCII characters; it
  changes with a message, a tool, the system prompt, the mode, the model,
  the output limit, the stream flag, a sampling or token option, the tool
  choice, the session, and the tool-result turn; it does not change with
  the token, the signal, the host's hooks or headers, or the gateway's
  address; a retry key per plugin retry; the turn ID is shared by the
  requests after one user message.
- `integrations/openclaw/test/retries.test.ts`: through the pinned
  transport, every request carries the key, the same for the same
  conversation and new for the next turn, with the host's headers and a key
  the host set kept; one attempt by default; with `maxRetries`, a 429 is
  retried under the same key and a 504 under the retry key, never shown to
  the host, at most `maxRetries` times; 503, 422, 409, 401, and 400 are
  never retried, nor a response with content or an aborted request; an
  abort during the retry wait ends the request aborted (event, result, and
  record), never as the 429; a `maxRetries` above 2 is refused. A host
  retry of the same conversation: after a 504 or a 409 `run_terminal` it
  sends `<key>-retry1`, after a plugin retry that succeeded it sends the
  key that succeeded, after an answer or a 429 it keeps the key, and a key
  the host set is never changed (`provider.test.ts`: the ledger is shared
  across `wrapStreamFn` calls).
- `integrations/openclaw/test/attempts.test.ts`: the attempt record takes
  the run ID from `X-RelayMind-Run-ID`, the error body, or the chat
  response ID, else `unavailable` (never a value that is not a gateway run
  ID), marks a replay, links the native run and the UI page, and says
  usage is unknown; one log line per attempt and one JSON line in
  `diagnosticsPath`, and an unwritable file is one warning; through the
  provider the answer is untouched, the token is in no line, and a retried
  504 gives two records, two run IDs, one turn.
- `integrations/openclaw/test/stream.test.ts`, `provider.test.ts`,
  `config.test.ts`, `manifest.test.ts`: the gateway's message in the error
  event and the result alike, a non-gateway failure unchanged; a stream
  that ends without a terminal event ends with the result's own reason (a
  call `toolUse`, a failure or an abort an error event, never `done`); the failover
  hooks; `maxRetries` 0 to 2 and an absolute `diagnosticsPath` in the
  settings and the manifest's closed schema.
- `tests/test_openclaw_cancel_errors.py`: the plugin driver against the
  gateway over doubles. Aborts before the request (no run, ID
  unavailable), during a buffered generation (cancelled with
  `cancel_requested`, the adapter asked to cancel, the model call kept, the
  slot free for the next request), under early headers (the run ID recorded
  before the cancel), while the host reads the replay (the run stays
  completed), and after the answer (nothing changes); a native events
  disconnect cancels nothing. 401 on the request (the headless path), 403,
  422, 503 and the same failure as a stream error event under early
  headers, 429 with nothing created, a 429 retried under the same key once
  the slot is free, and 504 are named failures, never empty answers, with
  the run ID where one exists and usage unknown; the pinned host's own
  classifier keeps the plugin's class: `format` for 403, 422
  `needs_external`, and both 409s (not a class it retries on the same
  model), `server_error` for `external_failed`. A host retry of the same
  request replays the completed run with the same run ID and no model call;
  the tool-result turn gets a new key and run in the same turn; a retry of
  the follow-up after an allowed write replays the answer and the host
  wrote once; a changed payload under the host's key, a redacted call, and
  a cancelled run's key sent by a new plugin process are 409; a host retry
  of a cancelled request in the same process sends `<key>-retry1` and gets
  an answer from a new run; a key survives a gateway token change.
- `tests/test_openclaw_qualification_script.py`: the host mode's
  `--scenario abort` against a stub host that exits 130 on SIGINT: the
  harness interrupts it once the generation started, the run is cancelled,
  no answer text, `passed`; a host that ended before the interrupt does
  not pass; a host that logs more than a pipe holds before its request is
  still interrupted (its output goes to files); a host-sized request (a
  40,000-character system prompt) still reaches the held generation (the
  gated double's count is fixed). `--scenario external-failed` against stub
  hosts: every run fails with 503 `external_failed`, the host's message
  names a recorded run, and the plugin's attempt records are kept, also
  across 7 host retries (the doubles never run out); a host that showed no
  message or made no request does not pass, and a failed host exit does
  not decide.
- `tests/test_openclaw_vps_functional_cancel_errors.py`: reads the Linux
  VPS functional check (not a supported-platform qualification) of the
  `abort` and `external-failed` host scenarios on the real pinned host
  against the gateway over doubles
  (`docs/evidence/2026-10-28-openclaw-vps-functional-138/`): SIGINT
  mid-generation cancelled the run (`cancel_requested`, exit 130, no
  report, as Q11 on macOS); the 503 failed every run with
  `external_failed`; the host retried the same model 7 times with a
  rewritten conversation, so 8 attempt records, keys, turns, and runs; the
  plugin's message reached the host's log while its final text was the
  host's generic one.

The aborts in the first tests come from the driver's abort signal and a
stub's SIGINT. The real host's interrupt and a 503 ran in the Linux VPS
functional check, rerun with
`docs/evidence/2026-10-28-openclaw-vps-functional-138/scaffold/vps_functional.py`
(about three minutes, no model). Other error codes, aborts under early
headers or before sending, the host's turn ID across its retries, its
failover to another model, and macOS and Windows at this commit are not_run
(the exact host commands are in
[`integrations/openclaw/README.md`](../integrations/openclaw/README.md)).

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
- Managed gated streaming has deterministic model-double and actual socket/SDK
  coverage (AC19); real provider corpus parity is still pending.
- The launcher (`relaymind serve --config`) has started with the pinned
  model on macOS (the harness above) and on x86-64 Windows (#110, five
  starts, `/readyz` 200 on all).
- Phase 2 (spec 0.31, RM26 to RM32 and AC17 to AC22; RM30 and AC23 were
  retired in 0.47) now has deterministic
  coverage for gated streaming, operation without a local model, placement,
  and the out-of-process selector (#133). The budgets table (#142), the call timing and count
  recording (#120), the draft answer pass (#121, AC17), early response
  headers (#140), admitted calls per generation (#122), prefetch (#123),
  both AC18, and the selection interface with its two built-in selectors
  (#131, #132, AC22 in process) are implemented; their tests are in
  [Phase 2 (spec 0.31)](#phase-2-spec-031). The cached-token figures in the existing exports
  are adapter defaults (finding F9 in [`EVIDENCE.md`](EVIDENCE.md)); the
  prefix-cache probe (#114) read the runtime's own counts once, and the
  gateway records them for every model call since #120, so a run made
  after it has measured counts. The M7 to M9
  issues in [`ROADMAP.md`](ROADMAP.md) carry the tests. The offline
  spikes (#115, #118, #119) and the timing probes (#114, #116, #117) test
  their own scripts, not the gateway.
- M9 OpenClaw integration (spec 0.47, RM33, AC24): #135 runs the pinned
  host transport against the gateway over model doubles
  ([OpenClaw host transport](#openclaw-host-transport-135-rm33-ac24)).
  A real host ran on macOS against a capture server (the host probe), and
  the tokenizer count on macOS measured 0 of 10 host requests within the
  8192 local limit; one live AC24 turn in auto without an external model
  (config-row workaround, #136) ended needs_external / context_limit with
  0 model calls. The harness host scenarios at `a52f8bf` failed (#136, #137).
  The plugin (#136) and its round trips (#137) are covered over doubles
  ([OpenClaw round trips](#openclaw-round-trips-137-rm01-rm03-rm04-rm08-rm09-rm12-rm19-rm20-rm26-rm33-ac10-ac24)),
  and a Linux VPS functional check (not a supported-platform qualification)
  ran the harness host scenarios on the real pinned host over doubles; a
  plugin approval answered by an operator, a macOS rerun, and any Windows
  host run are not_run, and no live result is claimed.
  YAML workflow acceptance AC23 is retired; AC14 still covers the fixed demo.
- The skill and tool selection benchmarks of spec section 12.4 are
  implemented (#143, #144), with frozen SkillsBench/ToolRet inputs and
  [recorded Apple M1 comparisons](EVIDENCE.md#2026-10-23--m7-skill-and-tool-selection-benchmarks-apple-m1).
  These source-domain measurements use the documented catalog caps and
  do not replace the ops correctness corpus. The built-in selectors do
  not produce strict nonempty tool subsets, so these runs do not establish
  a narrowing benefit. The workflow-plan track was retired with RM30
  (spec 0.47, section 12.4).

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

With the #57 and #167 fixes (RM01, RM18, RM19; spec section 6), `LlamaCppAdapter`,
both automated startup health-poll paths, and the live acceptance and evaluation
harness (`live_acceptance.py`, `run_corpus.py`, `latency_benchmark.py`) bypass
environment and system proxies for `localhost` and IPv4/IPv6 loopback literals. These
paths no longer require the session overrides above. Non-loopback endpoints and the
external Chat Completions adapter retain normal proxy handling; process-wide proxy
settings are unchanged. This policy applies to the Python clients, not the manual
terminal commands below.

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
