# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- `Usage.cached_tokens` is `None` when the source returned no count, not
  0 (#120; RM16, spec 0.31 section 6, finding F9), and the same holds for
  `reasoning_tokens`. The recorded `usage` of an event, the timing
  statistics, the latency benchmark's model calls, and the live harness
  keep `null`. In the latency benchmark's prefix cache record, a call with
  no reported count is counted as not measured and a total over no
  measured call is `null`. Exports made before this change still hold the
  adapter's default of 0.
- Adapter event streams now start a generation with a `FirstToken` marker
  before its first answer text, reasoning text, or tool-call fragment. The
  timing statistics ignore it, so their definitions are unchanged; a
  consumer that lists every event of a stream sees one more (#120).

- Specification 0.31 → 0.32 (PR #146 review; RM05, RM17, RM22,
  RM26, RM28 to RM32; AC11, AC17 to AC20, AC22, AC23): distinguish
  an absent local model from an unavailable or unsuitable configured
  one; separate the prefetch option from its declarations; complete
  the answer-pass event and terminal-error contracts; define selector
  threshold keys, ranges, and provisional defaults; separate prefetch
  binding validation from request-time resolution; and specify workflow
  package loading before workflow-ID checks. Documentation only;
  phase 2 remains unimplemented.

- Specification 0.30 → 0.31 (phase 2; RM26 to RM32, AC17 to AC23):
  operation without a usable local model, with detection; placement
  from recorded timing samples; one replaceable selection interface;
  prefetch; workflow packages; the draft answer pass; gated answer
  streaming and early response headers; several read-only calls per
  generation; completion criteria in the external request; timing and
  cached-token recording; a budgets launch table; and the selection
  benchmarks of section 12.4. Every new option defaults to the earlier
  behavior. Reasoning: `docs/design/2026-10-23-performance-first-routing.md`;
  findings: the 2026-10-23 analysis in `docs/EVIDENCE.md`; plan: M7 to
  M9 in `docs/ROADMAP.md`. Documentation only: no code or observable
  behavior changes.
- `README.md` is now a short user guide (what it is, install,
  configure, start, use); project status lives in `docs/ROADMAP.md`
  and `docs/RELEASE.md`.

### Fixed

- Correct the skill guide's workflow-key description, repair the testing
  guide's README link, and install the client SDK before the Python
  quickstart example (PR #146 review).

- `docs/EVIDENCE.md`: the 2026-10-22 latency entry reported "0 cached
  prompt tokens" as a runtime figure; it is the adapter's default
  (finding F9 of the 2026-10-23 analysis).
- Demo skill completion predicates reject answers that break spec
  section 9 (#13; RM05, RM16, RM23): negated or mixed health statuses,
  inferred incident causes or a missing service, invented deployment IDs,
  negated or extra-database deletion reports, and empty or mid-sentence
  diagnostic section labels. Example notes and `docs/EVIDENCE.md` state
  that the Chinese material has not been natively reviewed (spec 0.7
  section 9); all six packages move to version 0.1.1 (RM07).
- Loopback llama.cpp inference and runtime startup health polls bypass
  environment and system proxies without changing process-wide settings
  (#57; RM01, RM18). Non-loopback endpoints and the external Chat
  Completions adapter keep normal proxy handling. Deterministic tests
  cover localhost, IPv4/IPv6 loopback, and remote hostnames; the historical
  Windows qualification remains evidence for its recorded revision only.

### Added

- Native Windows launcher and latency evidence (#110; RM12, RM17 to
  RM19, RM25, AC15; sections 12.1 to 12.3 and 14.1): supplied exports
  from a physical Windows 11 x86-64 machine with the pinned Q8_0 model
  and llama.cpp b11785, built from source. Five gateway configurations
  reached `/healthz` and `/readyz`; the local latency benchmark retained
  all 120 samples, including 76 errors and zero request timeouts. Its
  p95 first model action was 3610 ms, so the 500 ms hypothesis was not
  met. AC15 closes with separate macOS and Windows latency reports;
  acceptance failures, missing external scenarios, and the unavailable
  browser driver remain recorded. A hosted-runner workflow is no longer
  needed to obtain the Windows result.
- Budgets table in the launch file (#142; RM03, RM17, RM19, spec 0.31
  sections 11 and 14.1): `[budgets]` sets the section 11 limits, with the
  `LocalBudgets` field names as keys. A wrong type, a non-finite number, a
  limit that must be positive and is not, `repair_generations` above
  `local_generations`, and an unknown key stop startup with one message and
  exit status 2; every run records the values in force in `run.accepted`.
  A file without the table keeps the defaults. `relaymind.example.toml`
  shows the block, commented.
- First-token time, cached and reasoning tokens, and provider request
  options per model call (#120; RM12, RM16, RM18, RM25, RM27, spec 0.31
  sections 6, 8, and 14.1): `model.finished` carries `first_token_ms`
  (integer milliseconds from the request start on the run's monotonic
  clock, `null` when no token arrived) and, inside `usage`, `cached_tokens`
  and `reasoning_tokens` (`null` when the runtime or provider returned no
  count). The adapters read `usage.prompt_tokens_details.cached_tokens`,
  `usage.completion_tokens_details.reasoning_tokens`, and llama-server's
  `timings.cache_n` (the usage details win when both are sent); the
  llama.cpp adapter sends `cache_prompt`. An internal `FirstToken` marker
  dates the first generated fragment even when a tool call is only emitted
  after the finish chunk (RM09). `[external_model.request_options]` is a
  table of provider request fields sent verbatim and recorded in
  `model.started`; a credential, a header, a key that would replace
  `model`, `messages`, `tools`, `stream`, or `stream_options`, a sampling
  key (`max_tokens`, `temperature`, `top_p`), and a value
  that is not JSON stop startup with a message that names the key and never
  the value. The trace schema stays at version 1 with optional fields. The
  pinned runtime (build 11785) reported 558 of 559 prompt tokens as cached
  on a repeated prompt and no reasoning count; the live check is in
  `docs/EVIDENCE.md`.
- Runtime and provider timing probes (#114, #116, #117; RM18, RM25, RM26,
  RM27, spec 0.32 sections 6, 10, and 12.2):
  `scripts/probes/probe_prefix_cache.py` sends the gateway's own routing
  and skill-stage requests to llama-server with `cache_prompt` on and off
  and reads the whole `timings` and `usage` objects;
  `scripts/probes/probe_cancel_prefill.py` cancels a prompt of about 6,000
  tokens during prefill and measures the wait of the next request (its
  definition lists exactly two short prompts per repetition);
  `scripts/probes/probe_external_latency.py` records the time to headers,
  to the first reasoning delta, to the first content or tool-call delta,
  and to the end of streamed provider calls, with and without tools and
  with the provider's reasoning default and off. `--runtime-extra-args`
  adds llama-server arguments after the manifest's launch settings (for a
  CPU-only runtime with two threads) in the live harness, the corpus
  runner, the latency benchmark, and the probes; the default adds none,
  the arguments are recorded in every export, and only runtime-only
  settings (GPU layers, threads, batch sizes), each with one integer value
  in its range, are accepted; a positional argument (a model path), a
  missing or invalid value, a `--no-` form, and any other flag are refused. `skill_stage_messages` in
  `orchestrator/local_loop.py` is the skill-stage conversation as a pure
  function, with no change in behavior. The results are in the 2026-10-23
  runtime and provider timing entry of `docs/EVIDENCE.md`. No gateway
  default changes.
- Offline S0 spikes on the 2026-10-22 corpus export (#115, #118, #119;
  RM28, RM31, spec 0.31 sections 4.3, 12.1, 12.2, and 12.4):
  `scripts/run_corpus.py --regrade-draft-pass` grades the stored traces as
  if the skill-stage draft had been the answer, after the RM31 guards, and
  writes a separate export labeled as an offline counterfactual;
  `scripts/probes/count_multi_calls.py` counts the round trips that several
  read-only calls per generation would have saved;
  `scripts/probes/probe_example_match.py` scores a throwaway BM25 matcher
  on the 52 corpus prompts against the new skill sidecar
  `evaluation/corpus.v1.skills.json`. The corpus lock freezes the sidecar
  in a `sidecars` key, `check_lock` verifies the sidecars a lock names,
  `load_skills_sidecar` checks it against the corpus, and the corpus
  digest is unchanged. `relaymind.evaluation.environment` gains
  `application_revision()`, which each spike records. The results are in
  the 2026-10-23 entry of `docs/EVIDENCE.md`. No gateway behavior or
  default changes. After review (PR #147): the counterfactual
  `run.completed` carries every field section 8 requires and
  `answer_pass_applied` `draft`, the export documents every block
  reason, a local set is limited to the room left in its stage's tool
  limit, null arguments read as `{}`, the matcher picks only a unique
  best skill, all three scripts refuse an `--out` equal to the export,
  and a lock whose `sidecars` value is not an object is refused; no
  recorded figure changed.
- Correctness corpus and latency benchmark (#32, #33; RM25, spec 0.28
  sections 12.1 to 12.3 and 14): the frozen corpus
  `evaluation/corpus.v1.json` (40 English cases in the section 12.2
  classes and 12 best-effort Chinese simple cases, three new sandbox
  seeds, digest lock `evaluation/corpus.v1.lock.json`);
  `relaymind.evaluation` (corpus checks, grading from the trace,
  summaries with exact numerators and denominators, nearest-rank
  percentiles, the environment manifest); `scripts/run_corpus.py` (each
  case and mode in a new gateway from `relaymind serve --config`, with a
  `--regrade` option for stored traces); `scripts/latency_benchmark.py`
  (the compatible stream, a cold request, three warm-ups, ten runs per
  prompt in a seeded order); and `docs/RELEASE.md`. The sandbox now
  publishes its tool permission table (`DEMO_TOOL_PERMISSIONS`). The
  first runs on Apple Silicon macOS are in `docs/EVIDENCE.md`: local
  completion 4/12 on the English simple subset, no missed confirmation in
  156 runs, and p95 first model action 508 ms, so the 500 ms hypothesis
  is not met there. Those first runs covered macOS only; the Windows
  follow-up is recorded above (#110, AC15).
- Live acceptance harness and the first live runs of the M3 to M5
  scenarios (#88, #104; RM04, RM08 to RM25, spec sections 10, 12.1, 13,
  and 14.1): `scripts/live_acceptance.py` starts llama-server from the
  manifest and the gateway with `relaymind serve --config`, and runs
  AC02, AC05, AC06, AC07, AC09, AC10, AC11, AC12 (cancel and restart
  during a generation), `external_only`, `parallel_demo`, the AC08
  external swap, a demo UI session in live mode, and cache-off and
  cache-on runs of the AC01 prompts over HTTP. It records gateway checks
  and model findings apart; scenarios that need an external endpoint
  are recorded as not run without one. The macOS run with Z.ai `glm-4.6`
  is in `docs/EVIDENCE.md`: all 90 gateway checks held; 9 model checks
  failed and are recorded as findings. Deterministic coverage over model
  doubles is `tests/test_live_acceptance_script.py`;
  `tests/test_live_acceptance.py` is the `live` variant. No Windows
  machine was available. AC08 stays open: its external swap ran between
  two models of one provider.

- Live AC08 external swap to a second endpoint (#109; RM18, AC08, spec
  0.29): the live acceptance harness ran with Z.ai international
  (`https://api.z.ai/api/paas/v4`) and then Zhipu bigmodel.cn
  (`https://open.bigmodel.cn/api/paas/v4`), both `glm-4.6`, with only the
  `[external_model]` table changed. The swap passed and every gateway
  check of the run held; the run is in `docs/EVIDENCE.md`. AC08 is
  closed. Both endpoints are Zhipu's (international and mainland China
  platforms), so a second vendor's API is not covered.
- Second local model artifact and the live AC08 local swap (#90; RM18, AC08,
  spec sections 6 and 12.3): `models/qwen3-0.6b-q4_k_m.manifest.json`
  (Qwen3-0.6B Q4_K_M), generated from the artifact, passed the live
  smoke suite and the English live skill-and-tool runs on llama.cpp
  b11785. `RELAYMIND_LIVE_MANIFEST` selects the manifest the live suite
  runs against, so the swap from Q8_0 to Q4_K_M needs no code change;
  the live trace export names the manifest. The runs are in
  `docs/EVIDENCE.md`. `models/qwen3-0.6b-q4_0.manifest.json` (Q4_0) is
  recorded as not qualified: it failed the thinking-mode smoke check.
  The external half of AC08 stays covered by scripted endpoints only,
  so AC08 stays open.
- Chat disconnect cancels the run (#92; RM14, RM17, RM19, RM20, AC12;
  spec 0.24 section 7.4): when the client of `POST /v1/chat/completions`
  leaves before the response is sent, with stream true or false, the run
  the request owns is cancelled through the same path as the native
  cancel (adapter asked to cancel, in-flight tool operation settled, run
  `cancelled`). A native events disconnect still leaves the run alone.
  Tests: `tests/test_chat_disconnect_cancel.py`.
- Operator launcher (#87; RM12, RM17, RM18, RM19, RM21, RM23; spec 0.21
  section 14.1): `relaymind serve --config relaymind.toml` starts the
  gateway on macOS and Windows. The TOML file wires the local and
  external models, skill roots, sandbox, run store, chat and native
  settings, the degraded-mode policy, enabled workflows, and the UI data
  mode. Startup refuses a public bind address, a manifest hash mismatch,
  an invalid skill package, an undeclared data mode, an unknown key, a
  credential in the file, a non-loopback local model URL, an out-of-range
  port, a non-positive duration or limit, an unknown permission, an
  unreadable sandbox seed, and an unusable run store, with one message and
  exit status 2. A refused command-line argument is never echoed. Restart
  recovery runs once before the server accepts a run, and the token is
  printed once to standard output only. `relaymind.example.toml` is the
  template; the README, `docs/TESTING.md`, and `docs/ROADMAP.md` use the
  command. Tests use model doubles (`tests/test_launcher.py`); no live run
  is recorded.
- Specification 0.20 → 0.21: section 14.1 defines the operator launcher
  (#87; RM12, RM17, RM18, RM19, RM21, RM23): the `relaymind serve --config`
  command, the TOML tables, credentials from the environment only, the
  all-or-nothing startup order, restart recovery once before serving, and
  the explicit startup errors, including the value-range, permission, and
  loopback local model checks.
- Demo trace UI (#28; RM23, RM25, RM11, RM21; spec 0.20 sections 8 and 9):
  the gateway serves a static page at `GET /ui` (no token, no run data,
  a content security policy) that shows a run's original prompt, mode,
  models, skill, route, tool proposals and operations, failed
  operations, escalation reason, clarification, confirmation, final
  answer, and timing. `GET /ui/runs/{id}` renders a trace from the
  stored events only, with every value escaped. The operator declares
  the data mode (live, replay, or double); it has no default and is
  shown in every view. Latency is the first model action, never the
  completed answer, and the run's active and waiting time are separate
  fields. Confirmation, denial, clarification, and cancel go through the
  native resume and cancel endpoints. `POST /ui/comparisons` runs the
  same input in auto and then external_only, resetting the sandbox before
  each run and cancelling any run that waits for input; it never resets
  the sandbox while a run is active, and an arm still active after its
  cancel is reported and ends the comparison. The run.accepted
  event now carries `original_messages` (the caller's own text, redacted
  by field name only, kept with the run's seven-day retention). Tests use
  model doubles (`tests/test_demo_ui*.py`); the page script is not run by
  an automated browser test and no live run is recorded.
- Specification 0.19 → 0.20: section 8 adds `original_messages` to the
  run.accepted payload, and section 9 defines the demo UI (#28): the
  static shell, the declared data mode and its label, the trace fields,
  the model action latency rule, the clarification and confirmation
  controls, and the comparison on reset fixtures (RM23, RM25).
- Cancellation during generation and in-flight writes (#29; RM10, RM14,
  RM17, AC12; spec 0.19 section 7.4): a cancel of a running run now asks
  the adapter of the generation in flight, local or external, to cancel
  that generation ID while the generation is still running, before the
  run ends as cancelled. The run never waits for that request: it is
  bounded, and its failure is never the run's, also after a time limit
  stops a generation. A late result of a provider that ignores the cancel
  is dropped: it proposes, runs, and publishes nothing. The timing
  collector (RM25) no longer keeps a cancel that arrives after its
  generation ended. Deterministic tests (`tests/test_ac12_cancellation.py`)
  cover a cancel during local and external generation (including the real
  llama-server adapter over a mock transport), a provider that does not
  stop, a provider cancel that hangs or fails, a cancel during a write
  (recorded as `unknown_outcome`, never replayed, not rewritten by the
  late handler result), during a read, and after a write succeeded
  (outcome kept), and the native cancel endpoint. With restart recovery
  (#8) this closes the deterministic part of AC12; no live model is run.
- Specification 0.18 → 0.19: section 7.4 defines cancellation during
  generation and writes (RM10, RM14, RM17, AC12, #29): the order of
  stopping dispatch, the best-effort adapter cancel, the dropped late
  result, how an in-flight tool operation is recorded, and that the
  cancelled run retains every outcome and is never replayed.
- Idempotency keys (#26; RM17, RM21, AC11; spec 0.18 section 7.4):
  `POST /relay/v1/runs` and `POST /v1/chat/completions` accept an
  `Idempotency-Key` (1 to 255 printable ASCII characters, sent once; else
  400 `invalid_request`) instead of refusing it. The key is stored with
  the run it creates, in the same store transaction (new
  `idempotency_keys` table, schema version 3), scoped to the caller and
  bound to a SHA-256 of the canonical request (endpoint, decoded body,
  and for chat the execution mode header). A duplicate never reaches the
  orchestrator, so it makes no model call and runs no tool. A native
  duplicate returns 202 with the existing run reference and the header
  `Idempotent-Replayed: true`, even while the run is active or the
  execution slot is taken. A chat duplicate of a completed run returns
  the stored response (answer or client-owned tool calls), as the stored
  `answer.delta` chunks when streaming; a duplicate of an unfinished run
  is 409 `idempotency_conflict` with its run ID, and one of a failed or
  cancelled run is 409 `run_terminal`. A changed payload is 409
  `idempotency_conflict`; a request that loses a race for the key after
  the lookup is answered the same way. A duplicate first ends an expired
  wait, so it never answers from a run waiting past its limit. A request
  refused before a run exists stores no key, and a request without a key
  is not hashed. Keys leave with their run at the seven-day retention
  limit or on deletion; a key whose run is past that limit is unknown
  even before a purge. Client-owned `function.arguments` are sent with
  sorted keys, as the store keeps them, so a replay sends the same text.
  `tests/test_idempotency.py` covers it with model doubles;
  the old tests that expected the header to be refused are removed.
- Native run API (#25; RM21, AC11; spec 0.17 section 7.4):
  `POST /relay/v1/runs` accepts messages and an optional
  `execution_mode` and answers 202 with `run_id`, `state`,
  `state_version`, and `events_url`; the run continues in a background
  task (`LocalOrchestrator.start`), so it does not depend on the request
  that created it. `GET /relay/v1/runs/{id}` shows state, version,
  output, error and reason codes, the pending question or approval,
  metrics, and the requested and effective modes; the confirmation token
  is shown once, to the first reader, and is never stored or traced.
  `GET /relay/v1/runs/{id}/events` is SSE with the stored `seq` as the
  event ID: a reconnect with `Last-Event-ID` replays later retained
  events from the store with no model call, tool run, or write, and a
  live stream follows the run across waits until its terminal event.
  `POST .../resume` (input, or approval ID, token, and `approve`) and
  `POST .../cancel` take `expected_version`; a stale version is 409,
  a body that does not match the waiting state is 400
  `invalid_resume_input`, and a bad token is 403 `approval_invalid`.
  Cancel ends a waiting run at once (reason `cancel_requested`, pending
  approval voided in the same transaction) and asks a running run to
  stop; a run owned by a chat request is 409 `invalid_state`. Every
  `run.cancelled` names its reason: `user_denied` for a denied
  confirmation, otherwise `cancel_requested`, also when the run task is
  cancelled with no recorded reason (a chat disconnect or the event
  loop); the published schema and the write-time validator require it
  (RM22, spec 0.17 section 8). `/healthz`
  and `/readyz` report liveness and, for each permitted mode, whether
  its models are available and warm. The execution modes share the
  chat endpoint's operator policy (`ChatSettings.allowed_modes`).
  `parallel_demo` now fails explicitly with `workflow_unavailable`
  (reason `parallel_workflow`, 503 on chat) when no workflow is enabled,
  no external model is configured or its health check says it is not
  available (checked before routing), or the run routes to the workflow,
  which is not implemented until #30; auto and `local_only` keep their
  rules. `Idempotency-Key` was refused until #26 (now implemented, see
  above), and cancel during an in-flight write is #29. `tests/test_native_run_api.py` covers it
  with model doubles, including one loopback-socket test of a stream
  that stays open across a wait; no live model is run.
- Client-owned tools alias (#24; RM08, RM19, AC10; spec 0.16 section 7.2):
  `relaymind-client-tools` now has a backend (`ClientToolsChat`,
  `relaymind.orchestrator.client_loop`). Each request is its own run with
  no routing generation: the gateway records its own decision (local, or
  external in `external_only`). The model is offered only the functions
  the caller supplied, narrowed by `tool_choice` (`none` offers none, a
  named function offers only that one), and the gateway installs and
  executes no tool: the trace has `tool.proposed` for the exposed call and
  never `tool.started` or `tool.finished`. A generation is admissible as
  non-blank text, or as a call to an offered function whose arguments pass
  the supplied schema (the strict RM09 check); `required` and a named
  function need a call and `none` needs text. Invalid output gets one
  repair generation, then an escalation. One call is exposed per response,
  and a call ID is kept unless it was used earlier in the conversation.
  The reply is buffered, so external fallback happens only before anything
  is exposed, `local_only` returns 422 `needs_external`, and `external_only`
  skips the local model. A function whose `parameters` is not a valid JSON
  Schema, or has a `$ref` that does not resolve inside it, is
  `invalid_request`; the gateway never retrieves a remote schema.
  Invalid output with no room left for the repair stops as
  `invalid_output`, the repair exchange is measured against the context
  limit before the next generation, `max_completion_tokens` bounds every
  generation of the run, and an external stage that stops on invalid
  output records `invalid_output`. `RunOutcome.tool_calls` carries the
  calls.
  `tests/test_client_owned_tools.py` covers the round trip (in process and
  with the OpenAI SDK over a loopback socket), the no-execution rule, the
  modes, and the ownership-mixing rejections on both aliases. This backend
  buffers its generation, so it does not return a `LiveAnswer`; no
  production backend forwards a live provider stream yet (spec section
  7.3 now says so). No live model is run.
- Streaming contract (#23; RM20, AC09; spec 0.15 section 7.3): the
  stream of the managed alias is the replay of the run's `answer.delta`
  events, so content is append-only and nothing but framing is added. A
  backend can return a `LiveAnswer` (`relaymind.api.chat`) that forwards
  provider text as it arrives. The role chunk waits for the first answer
  text, so a provider failure before it, or in a request without stream
  (which collects the whole live answer), is a normal 503
  `external_failed` response. If the provider fails after the first
  chunk, the stream asks the backend to record `run.failed`, sends one
  `error` event with fixed text (`external_failed`, the run ID, no
  provider text), and closes the connection with no finish chunk and no
  `[DONE]`; no replacement answer is sent, and the server does not log
  that expected abort as a crash. A client disconnect closes the
  provider stream, which requests cancellation. A result cannot carry
  both client tool calls and a live answer. `tests/test_streaming_contract.py`
  covers the framing of both aliases and both terminal paths (`stop`,
  `tool_calls`), append-only content, the final generation with tools
  disabled, the mid-stream failure in process and over a loopback socket
  with the OpenAI SDK, and an unmet evidence predicate that cannot
  finalize. The production client-owned backend that returns a
  `LiveAnswer` and records `run.failed` is issue #24.
- OpenAI-compatible endpoints (#22; RM19, AC09, AC16 request part; spec
  0.14 section 7.1): `GET /v1/models` and `POST /v1/chat/completions` on a
  FastAPI app (`relaymind.api.app`) over the orchestrator. The request
  parser (`relaymind.api.chat_request`) accepts the section 7.1 subset
  and rejects every other behavior-changing field with a named
  `unsupported_parameter` error, validates message ordering and tool-call
  references, and keeps tool ownership apart: the `relaymind` alias
  refuses client tools, and `parallel_tool_calls` true is refused. The
  response carries a stable `chatcmpl-<run id>` ID, the virtual alias, and
  `X-RelayMind-Run-ID`; the stream is framing plus the run's
  `answer.delta` texts, then `[DONE]`. The optional
  `X-RelayMind-Execution-Mode` header is checked against the alias and an
  operator policy (`ChatSettings`): 400 `invalid_execution_mode`, 403
  `execution_mode_forbidden`, never downgraded. `max_completion_tokens`,
  `temperature`, and `top_p` apply to the answer generations only
  (`LocalOrchestrator.run(final_options=...)`); an external generation
  offered tools takes the sampling but keeps its own token budget, so the
  budget never cuts off a tool call. `max_completion_tokens` is bounded by
  the operator limit `ChatSettings.max_completion_tokens` (default
  32,768). A run that fails as run state (for example `missing_input`) is
  409 `run_terminal`, not a 500. The gateway binds only to loopback
  (`relaymind.api.server`; `[::1]` is bound as `::1`) and generates its
  own application token at startup (`app.state.application_token`); no
  caller supplies one. An unexpected failure is logged with its type and
  frames, never its text. `fastapi` and
  `uvicorn` join the runtime dependencies and `openai` the dev ones.
  The `relaymind-client-tools` alias is served only when a client-owned
  backend is configured (#24), and `Idempotency-Key` was refused until #26.
  `tests/test_chat_request.py`, `tests/test_chat_endpoint.py`,
  `tests/test_final_options.py`, `tests/test_api_binding.py`, and
  `tests/test_openai_sdk.py` (the official SDK, in process and over a
  loopback socket) cover it. The requested answer options are not yet
  recorded in `run.accepted`, which needs a trace schema change.
- AC08 model substitution test for the orchestrator and skill packages
  (#49; RM18, AC08, spec section 12.3): one managed scenario (local
  skill and tools, local budget stop, handoff) runs through the real
  orchestrator and adapters under two configurations, a second local
  endpoint and model and a second external endpoint and model, with one
  skill root and no code change. The traces name their own models and
  are equal in routing, tool proposals, results, and handoff content.
  `build_gateway` in the test helpers accepts configured models over
  real adapters. No live variant: only one local artifact has a
  manifest, which `docs/TESTING.md` records, so AC08 stays open until a
  second local artifact is qualified.
- Failure behavior and error codes (#21; RM04, AC07, AC16 error part; spec
  0.13 section 11): `relaymind.api.errors` holds the error contract, an
  `ApiError` with a stable code, message, and run reference, one status
  per code (400, 401, 403, 404, 409, 422, 429, 500, 503, 504), and the
  mapping of orchestrator exceptions (`error_from_exception`) and failed
  runs (`error_from_outcome`: `external_failed` is 503, or 504 when a
  time limit stopped it; `context_limit_exceeded` is 400). An exception
  raised by a run's own work is 500 `internal_error` whatever its type,
  as its `run.failed` event says. Internal errors carry a fixed message
  and no credentials.
  `relaymind.api.auth.require_bearer_token` gives 401 `unauthenticated`.
  `RunOutcome.budget` names the limit that stopped a failed run, as the
  `budget` field of its `run.failed` event does.
  Degraded mode is an operator policy (`degraded_mode`, off by default):
  an unavailable local model hands off to the external model, with the
  evidence so far, when external dispatch is permitted. The trace records
  it as `route.selected` with mechanism `degraded_mode` (when the stage
  had no routing decision, also after a clarification) and
  `degraded_mode` and `local_failure` (scrubbed of known secrets) in
  `handoff.started`.
  `tests/test_error_contract.py`, `tests/test_ac07_failure_behavior.py`,
  and `tests/test_degraded_mode.py` cover every status, the bounded
  failure after an exhausted local budget, and degraded mode.
- Tool-output injection defenses and secret redaction (#20; RM08, RM12,
  AC06; spec 0.12 section 8): `Redactor.redact_with_repeats` also replaces
  a value found under a secret field (a string or a number) wherever the
  same whole token is repeated, in one pass. The executor scrubs those
  values, and the ones the run already knows, from the result data and
  error message before the first write; the envelope fields are left
  whole. Model-proposed tool arguments are scrubbed of the run's known
  secrets in `tool.proposed`, `model.finished`, approvals, and tool
  operations. A failure while recording a finished handler still
  finishes the pending operation.
  `tests/test_ac06_tool_output_injection.py` plays an injected log entry
  with obedient model doubles: the unallowed `delete_demo_database` call
  is rejected, caller permissions, the skill allowlist, and the offered
  tool schemas stay unchanged, the injection reaches the model only as a
  tool message, and the secret is absent from events, tool operations,
  model requests, and the SQLite store read back from disk, also when
  the model copies it into a tool argument.
- Restart recovery (#8; RM10, RM17, AC12 restart part; spec 0.11
  sections 5 and 8): `LocalOrchestrator.recover_after_restart` (and
  `relaymind.orchestrator.recovery.recover_interrupted_runs`) fails
  every unfinished run with `interrupted` and a `run.failed` event that
  names the state it was in, rebuilds `model_calls` from the trace, and
  releases the execution slot. A pending write is finished as
  `unknown_outcome` and a pending read as `failed` (error code
  `tool_interrupted`, a `tool.finished` event marked `recovered`);
  no handler runs, nothing is retried, and a later proposal of the
  same call replays the recorded outcome. Pending approvals expire at
  recovery time (a void approval is not counted). Each run is read again
  and recovered in one store transaction, one failing run does not stop
  the others (a `RecoveryError` carries the report of the recovered
  ones), terminal runs are untouched, and a second recovery changes
  nothing. Recovery uses the orchestrator's executor and clock, and is
  refused once the orchestrator has accepted a run. No model is called
  and no model loop is resumed. The startup call is part of the API work
  (#25); the cancellation half of AC12 stays in #29. Deterministic
  coverage is `tests/test_restart_recovery.py`.
- Confirmation flow for mutating demo actions (#19; RM11, RM10, RM17,
  AC05; spec 0.10 sections 4.3, 5, 7.4, 8): an admitted
  `delete_demo_database` proposal no longer stops the run. The run
  suspends in `waiting_confirmation` (from `local_running` or
  `external_running`), records `confirmation.required`, and returns a
  random single-use token to the caller in `RunOutcome.confirmation`;
  the store keeps only its hash and the token is never traced.
  `LocalOrchestrator.resume_confirmation` takes the approval ID, the
  token, and `approve`. The approval binds the run, tool, canonical
  arguments, target (`database_id`, declared by the tool as
  `confirmation_target`), and run state version; the arguments carry the
  fixture revision, which the handler still compares atomically. A
  matching approval opens a new stage like a clarification resume and
  executes the held call once, and its `tool.started` event names the
  approval. A denial cancels the run with `user_denied`; an approval
  older than five minutes fails it with `approval_expired` (also swept
  by `expire_waiting`); a wrong token or approval ID is 403
  `approval_invalid`, a malformed body or a text resume is 400
  `invalid_resume_input`, and a busy execution slot is 429 with the
  approval kept. A call that already has a recorded operation is a
  replay and asks for nothing. A mutating tool without a confirmation
  target is rejected back to the model and never runs; the target must
  be a required input field. The approval ID and token are checked
  before expiry and the slot, the approval is consumed in one store
  transaction with the resume or cancel (`RunStore.transaction`), a
  secret target is redacted, and secret argument values are bound by an
  in-memory digest. An approval whose run moved on is no longer
  pending. A textual yes in
  chat is not an approval. Deterministic coverage is
  `tests/test_ac05_confirmation.py`; no live model was run.

- Handoff envelope and escalation to the external model (#18; RM13,
  RM14, AC02; spec 0.9 sections 4.4, 8, 11): the HandoffEnvelope is
  published as `relaymind/orchestrator/schemas/handoff_envelope.v1.json`
  and validated at the boundary (`relaymind.orchestrator.handoff`). It is
  built from the run store's tool operations and keeps the original
  messages, the skill version, completed operation IDs with their
  structured results, pending work, permitted tools, remaining budgets,
  and language; rejected local proposals and draft answers travel only
  as `local_proposals`, each labeled a proposal. An escalation with
  external dispatch permitted records `handoff.started`, moves the run
  to `external_running`, and revokes the local actor's dispatch
  authority; the external model proposes tools through the same
  validation, identifier provenance check, and executor, and a repeated
  completed operation gets its recorded result and runs no handler. One
  actor owns the final answer. The whole external request is measured
  against the external context limit (the adapter's tokenizer, or the
  UTF-8 byte length as an upper bound) before dispatch and before every
  external generation; over the limit the run fails with
  `context_limit_exceeded` and no external call. An operation with no
  known outcome blocks handoff, and so does a stop the external stage
  could not resolve either (a mutation that needs confirmation, or a
  skill with no permitted tool): those end as `needs_external` with
  `capability_missing` and no external call. An admitted parallel route
  (the workflow is not wired yet) and an unavailable local model (no
  operator policy permits degraded mode yet) do not hand off either. An
  `external_failed` run keeps an RM04 reason code. A timed-out generation is
  traced with the limit that bounded it, never as local time. New budgets `external_generations` (6)
  and `external_seconds` (30) are recorded in each run. External failures
  end as `external_failed`; without a configured external model, in
  `local_only`, or with no run budget left, the run still ends as
  `needs_external`. `handoff.started` now requires `reason_code`,
  `model`, and `operation_ids`. Deterministic coverage is
  `tests/test_ac02_handoff.py`; no live external model was run, and
  external skill selection when no skill was chosen (`external_only`)
  hands over the catalog and tools but does not load a skill body.
- Live AC01 record on the pinned llama.cpp b11785 runtime in
  `docs/EVIDENCE.md` (raw export
  `docs/evidence/2026-10-21-m2-local-trace-b11785-apple-m1.json`; RM01,
  RM16, RM18, RM22): 3 of 4 check-service-health runs completed locally
  with zero external calls, both English prompts among them; the
  Chinese staging-api answer again translated the status word.
  `docs/ROADMAP.md` records M1 (#58, #63) and M2 (#27, #67, #69) as
  closed, with the items carried into M3 to M5.
- Native x86-64 Windows M1 qualification record in `docs/EVIDENCE.md`
  (#46; RM18, spec sections 3, 6, 14): three live smoke tests passed
  with the pinned model and runtime commit on the contributor's Windows
  machine. Records the CPU build, app-local CRT setup, proxy workaround,
  and PowerShell quoting; `docs/TESTING.md` links the reproduction steps.
  M1 is now recorded as complete on both target platforms.
- Model adapter contract (RM18): capabilities, generate, cancel, and
  health with normalized messages and events (`relaymind.adapters`).
- llama.cpp adapter for Qwen3-0.6B over a llama-server endpoint, with
  non-thinking mode configured through chat-template settings and
  exercised by an opt-in live smoke test (`pytest -m live`, spec
  section 6).
- External Chat Completions adapter for an operator-configured
  OpenAI-compatible endpoint, sharing the stream and request handling
  with the llama.cpp adapter (`relaymind.adapters.chat_completions`,
  RM18).
- Model artifact manifest machinery: GGUF metadata hashing,
  `load_manifest`/`verify_artifact`, the manifest generator script, and
  the hash-pinned Qwen3-0.6B Q8_0 manifest (`models/`). The model is a
  candidate; it becomes the default only after the bilingual tool tests
  (spec section 6).
- Launch settings are manifest data (RM18, spec section 6): the live
  smoke test verifies the artifact against its manifest and derives the
  llama-server arguments and adapter config from `launch_settings`
  (`launch_settings_argv`, `launch_adapter_settings`). Manifests with
  unusable settings, or settings that would load other weights or
  replace the pinned chat template, are rejected when built or loaded.
- Timing and statistics export for adapter comparisons (RM25, spec
  section 12.1): `relaymind.stats` records per-generation metrics around
  any adapter, and `scripts/compare_adapters.py` runs the same prompts
  against several configurations and writes one JSON export each. This
  is engineering instrumentation, not benchmark evidence (spec 12.3).
- AC08 model substitution test at the adapter layer (RM18): the same
  caller code drives a swapped local model, a swapped external endpoint,
  and the local/external boundary with the same normalized outcome. The
  orchestrator and skill half of AC08 remains open.
- Manual testing guide for the M1 deliverable on macOS and Windows
  (`docs/TESTING.md`).
- Live evidence record (`docs/EVIDENCE.md`, raw exports in
  `docs/evidence/`): the M1 live smoke run and a timing probe on Apple
  Silicon macOS, with machine, runtime build, manifest, exact command,
  and result. Single-pass observations, not benchmark results (spec
  section 12.3).
- Reproducible timing probe (`scripts/probe_timing.py` with probe
  definitions in `scripts/probes/`): launches llama-server from the
  manifest, records cold start, warm-ups of both prompt kinds, and one
  measured pass, and writes an export whose `run` block names the
  command, prompts, system prompt, tool schema, and each generation's
  outcome (RM16, RM25). `export_stats` accepts that block as `run_info`.
- RouteDecision schema and boundary validator (spec section 8, RM02;
  part of #14): the versioned JSON Schema is published as
  `relaymind/orchestrator/schemas/route_decision.v1.json`, and
  `parse_route_decision` / `parse_route_decision_json` reject unknown
  or missing fields, invalid values, skills outside the advertised
  catalog, prose around the JSON, duplicate keys, non-finite numbers,
  and anything else the decoder refuses. The spec 0.6 cross-field rules
  (#60) are enforced: a clarify route carries a non-blank clarification
  and every other route none; with a `RoutingContext` (managed or
  client-owned catalog, effective execution mode, operator-enabled
  parallel workflows) managed local needs a skill, parallel needs an
  enabled workflow and an admitting mode, and the client-owned alias
  has no skills and no parallel. The reason code `execution_mode` is
  added. The schema mirrors the clarification rule with if/then and
  requires non-blank text, so it rejects what the validator rejects.
- Local tokenizer on the llama.cpp adapter (#47, spec section 4.1):
  `LlamaCppAdapter.count_tokens` counts a normalized message list and
  its tools through llama-server `/apply-template` and `/tokenize`, and
  `capabilities()` now reports `tokenizer_available=True`. A runtime
  failure raises `TokenCountError` instead of estimating. The external
  adapter still reports no tokenizer and refuses to count. A live test
  checks the count against the runtime's prompt tokens in English and
  Chinese.
- Routing probe spike (#48): `scripts/probes/probe_routing.py` with
  `scripts/probes/m2_routing.json`, its live result in
  `docs/EVIDENCE.md`, and the routing mechanism decision for #14
  (native function calling) in `docs/ROADMAP.md`. The probe applies
  the 2,048-token router cap (an over-cap row is bypassed and marked),
  starts its clock before the token count, sends and records
  llama-server's `cache_prompt` setting, and records adapter errors in
  their own field. The recorded run was re-scored under spec 0.6 and
  corrected in `docs/EVIDENCE.md` (grammar 7 of 12 valid, not 12 of
  12); its limits (tokenizer time excluded, prompt cache on and
  unmeasured) are stated there.
- Run state machine (#6, spec section 5, RM17):
  `relaymind.orchestrator.run_state` with the ten section 5 states and
  their allowed transitions. Each transition is a compare-and-set on the
  run state version, so stale transitions, resumes, and cancels get 409
  and leave the run unchanged; a stale version is reported before a
  terminal state. A resume starts the resuming store's run clock
  (RM25). Terminal runs never change or restart; a
  retry is a new run linked to the previous one. One execution slot
  (section 11) is held in the active states, released by waiting and
  terminal states, and reacquired on resume or refused with 429. The
  slot is a flag on the run row, set in the same SQLite transaction as
  the state change, so every process on one store file shares it. A
  transition into completed, failed, or cancelled carries its
  run.completed, run.failed, or run.cancelled event (RM22), and a
  failed run's event names the run's error code. A waiting run
  continues only through resume, to the state that suspended it, or
  fails or is cancelled; deleting or purging a run frees a slot it held.
  `create(accepted=...)` writes run.accepted with the run in one
  transaction, so a run can fail or be cancelled straight from
  accepted. `cancel()` without an event records a gateway run.cancelled
  with the reason, if any.
- SQLite run store (#7, spec section 5): `relaymind.store.RunStore`
  with runs, events, tool_operations, approvals, and artifact_refs
  under versioned migrations, split into statements with
  `sqlite3.complete_statement`. Event sequence numbers increase per
  run. A tool operation is recorded as pending with its canonical
  arguments and their SHA-256 before its side effect, then finished
  once as succeeded, failed, or unknown_outcome; a CHECK constraint
  admits only those statuses (RM10). Approval tokens are kept only as
  SHA-256 hashes; an approval is issued for the run's current state
  version, expires after the configured TTL (five minutes, RM11), and
  is consumed once in one atomic update that matches its run, tool,
  canonical arguments, target, and the run's current version. A run's
  pending approval can be read without its token hash (RM21). Store
  times must be timezone-aware UTC. A failed commit rolls back, and a
  failed rollback reopens the connection. Runs older than the seven-day
  default retention are purged or deleted explicitly with everything
  they own. `tool_operations(run_id)` and `artifact_refs(run_id)` list a
  run's operations and artifact references in record order, for the
  section 8 HandoffEnvelope (RM13).
- TraceEvent schema (spec section 8, RM22; part of #27): published as
  `relaymind/store/schemas/trace_event.v1.json` and validated before
  every write; tests check that the validator and the schema (through
  jsonschema) accept and reject the same events. It covers the RM22
  event types; tool events must name their operation or proposal ID and
  originating model, model events their call and model, and
  run.completed the final-answer model (RM16), and skill.loaded its
  name, version, and content hash (RM06). clarification.answered (spec
  0.6 section 8) needs a non-blank redacted `input`, a non-blank
  `missing`, and a non-negative integer `waited_ms`. Text fields must contain
  a non-space character, and an ID that is present must be non-blank.
  run.accepted is the first event (seq 1) and carries the section 8
  payload: requested and effective execution mode, model_alias
  (`relaymind`, `relaymind-client-tools`, or null for a native run;
  spec 0.6 section 8), budgets, skill_roots, and skills_offered (any
  sequence, stored as lists).
  The trace event API is exported from `relaymind.store`. Timestamps are UTC and elapsed_ms
  comes from a monotonic clock (RM25); a store joining a run continues
  from its last persisted elapsed_ms, so elapsed_ms never goes back.
- Tool registry (#10, RM08, RM09): `relaymind.tools.ToolRegistry`.
  Registration requires a name, description, input and output JSON
  Schemas, a timeout of at most two seconds (section 11), a side-effect
  class, a handler, and an authorization rule; input objects must use
  additionalProperties=false, and output schemas must be object
  schemas. The registered declaration holds deep, read-only copies of
  both schemas, so they cannot drift from the schema hash. Availability is the intersection of the
  skill allowlist, operator policy, and caller permissions, and a name
  that is not registered never gets a handler. `invoke` validates the
  name and the decoded argument object before the handler runs and
  validates the returned data; a handler error or bad output is a
  failed ToolResult, except that a mutating handler that raises an
  unexpected error or returns bad output gives unknown_outcome, since
  its side effect may have happened (RM10). Integers are strict: a float such as 60.0 or a
  boolean where a schema declares an integer, or an enum or const of
  integers, is rejected as invalid_arguments. Rejection messages name
  the JSON path and schema keyword, never the rejected value (RM12), and
  a tool name that is not a string is unknown_tool. The schema hash
  covers the name, description, both schemas, timeout, side-effect
  class, and required permission, in the canonical JSON form
  (`relaymind.canonical`) that the run store also uses for approval
  binding; that form refuses NaN and infinities. `invoke` requires a
  non-blank operation ID and never reports a negative duration. The
  ToolResult envelope (section 8) is published as
  `relaymind/tools/schemas/tool_result.v1.json`. Operation IDs, replay,
  and timeout enforcement stay with the executor (#11).
- Sandbox demo data service and the seven demo tools (#12, RM23,
  section 9): `relaymind.tools.sandbox` with schemas published under
  `relaymind/tools/schemas/demo/`. The seed fixture is
  `tests/fixtures/sandbox/baseline.json`, checked at load by running
  the real tool methods on it; the service works on a disposable copy in
  its work directory, which a new service keeps and only an explicit
  reset replaces; a reset reads and checks the seed again, and writes go
  through a unique temp file. Every output carries the scenario of the
  working copy and the observation time, an `unavailable` health status
  is a successful observation, an unreachable tool is a failed
  ToolResult, and recent logs come newest first. The fixture has one
  revision that every listed database reports and every delete and
  reset bumps; `delete_demo_database` compares `expected_revision` with
  it under a cross-process file lock (fcntl on POSIX, msvcrt on
  Windows) before it looks up the ID, returns `revision_conflict`
  without mutation, and returns
  the revision it stored. Each identifier input (`service`,
  `database_id`) carries an English `title` and an
  `x-relaymind-label` with `en` and `zh` text, so a clarification can
  name the missing input in the run's language.
- Skill registry (#9, RM06, RM07, spec 0.4 section 4.2):
  `relaymind.skills.SkillRegistry` loads SKILL.md packages with a
  `skill.manifest.json` manifest (schema
  `relaymind/skills/schemas/skill_manifest.v1.json`) from
  operator-configured local roots, `skills/` by default (a configured
  list replaces it), one level deep and in order. Relative roots, the
  default among them, resolve against the working directory, the
  checkout root when the gateway starts there. Every subdirectory of a
  root is a candidate package. Missing or malformed package files,
  duplicate names across roots, remote roots, unregistered tools,
  predicates, or workflows, package files or directories that resolve
  outside their package or root, and resource paths that are not
  normalized or name one file twice are startup errors. The content hash covers the body, manifest, declared
  resources, and the allowed tools' schema hashes. Loads emit
  `skill.loaded` with name, version, and hash; a pinned version or hash
  that is not held fails without substitution; resource reads are
  limited to declared package files. `offered_snapshot` gives the
  `skill_roots` and `skills_offered` fields of run.accepted;
  `skill_roots` are the configured root strings as given, not resolved
  host paths.
- Completion predicates (`relaymind.skills.predicates`) as registered
  gateway validators: the health check reads the status stated for the
  returned service, the checklist needs its draft label, each incident
  count must sit next to its own label, diagnostic section labels count
  only where they start a line, the deployment report must name the
  returned service, and with no recorded deployment any deployment-like
  ID (dep, deploy, or deployment followed by a separator and a
  digit-bearing token, or by digits) fails. Service and database IDs, lowercase by
  schema, match in any case at ID boundaries ("Staging-api is
  healthy"); a quoted deployment ID must stay exact. The fixed parallel workflow ID
  (`relaymind.orchestrator.workflows`, RM15) is the known workflow;
  operator enablement is checked at routing (spec 0.6 section 4.4).
- Six demo skill packages under `skills/` (part of #13, section 9) with
  paired English and Chinese examples that are checked against the
  sandbox outputs. investigate-performance and
  health-and-diagnostic-checklist are not local-eligible, because their
  final answer is external (section 9). The Chinese examples are not
  natively reviewed (best-effort since spec 0.7).
- Runtime dependencies `jsonschema` and `pyyaml` for schema validation
  and SKILL.md frontmatter.
- Tool executor (#11, RM10, RM14; spec section 11):
  `relaymind.tools.executor.ToolExecutor` is the only owner of
  operation IDs and replay. `validate` checks a call once (RM09) and
  returns a `ValidatedCall`; `execute` runs only such a call. The
  logical operation ID comes from the run, the tool name, the canonical
  arguments, and the attempt, and is stored as `pending` before the
  handler runs. Replaying a finished operation returns the recorded
  result without running it; a pending one is in flight
  (`OperationInFlightError`), never a replay. Handlers run on a daemon
  thread, so the event loop is not blocked, bounded by the tool timeout
  or the caller's remaining active budget, whichever is shorter. A
  timed-out or interrupted mutating tool is `unknown_outcome` and is
  never retried automatically; a read-only one is `failed`. Every
  pending record is finished, and a worker error is recorded as
  `tool_error`, not as a timeout. A read-only call gets one retry after
  a transient failure (`tool_unavailable` or `tool_timeout`), as a new
  operation. Arguments and results are redacted of configured secret
  fields before they are stored (RM12). `ToolRegistry.validate`,
  `ToolRegistry.run`, `ToolRegistry.tool_schemas`, and
  `ToolResult.from_json_object` are added; `invoke` is validate plus
  run.
- Secret-field redaction (RM12, section 8): `relaymind.redaction.Redactor`
  replaces the values of operator-configured secret fields (a default
  set of credential key names) at any depth, and `redact_text` scrubs
  those values from model text, before anything is persisted.
- Router (#14, RM01, RM02, RM05; spec 0.6 sections 4.1, 4.2, 4.4, 5, 7.4,
  8): `relaymind.orchestrator.router` uses native function calling, with
  one function per advertised skill (at most eight) plus
  `request_clarification`. The gateway builds the RouteDecision and
  validates it with a `RoutingContext`. Route and reason code come from
  the skill registry and the run: a skill with a parallel-workflow ID
  routes parallel only when operator configuration enables the workflow
  and the effective mode admits it (`parallel_demo`, or `auto` with
  `parallel_in_auto`); otherwise it routes external with
  `parallel_workflow`. `external_only` gets the gateway's own decision
  (external, skill null, `execution_mode`) without a routing generation.
  The language comes from the latest user turn with words in it, so an
  identifier-only reply, or a single bare word such as `auth`, keeps it. A selected skill whose tools all need
  an enumerated identifier gets a clarify decision with `missing_input`,
  naming the skill and the missing argument, when no user turn names one;
  the question's label is the schema's `x-relaymind-label` in the run's
  language (`请问是哪个服务？`), else its title, taken from any allowed
  tool's schema. Router input over the
  2,048 local-token cap takes the bypass decision (`context_limit`)
  without a routing generation. Invalid routing output never reaches a
  tool. RM05 provenance (user turns and tool results only) lives in
  `relaymind.orchestrator.identifiers`, shared with the loop; a value
  joined to a longer token by `.`, `:`, or `/` is another resource.
- Bounded local generation loop (#15, RM03, RM04, RM16; spec sections
  7.3, 8, 11): `relaymind.orchestrator.local_loop.LocalOrchestrator` with
  `LocalBudgets` and `BudgetMeter` (`relaymind.orchestrator.budgets`).
  Every budget is a finite, configurable number. `run.accepted` carries
  the section 8 payload: requested and effective execution mode, model
  alias, budgets, skill roots, and skills offered, and is written with
  the run in one transaction, so an invalid payload creates no run; the
  selected skill is loaded at exactly that snapshot (RM07). Every generation is charged by
  its purpose in one place, and only real tool executions are charged.
  Exceeding a budget, unrepaired output, a missing capability, an
  oversized input, a failed tool outcome (`tool_failure`), or a
  non-local route ends the run as failed with error code
  `needs_external` and an RM04 reason code, because external dispatch
  is not wired yet (#18). Any other exception fails the run with
  `internal_error` (a cancelled run is cancelled), which releases the
  execution slot. Tool proposals are checked for identifier-like
  arguments, enum or pattern, that no user turn or tool result named.
  Every call of a multi-call turn is recorded as its own `tool.proposed`
  event. A repair is offered only while both the repair and the local
  generation limits have room. A pending tool operation is finished on
  every exit, even before its handler runs, and free-text redaction
  replaces secret values by whole token only. A
  mutating tool is never executed by this loop: the confirmation flow
  (#19, RM11) is not wired yet, so such a proposal ends the run as
  `needs_external` with `capability_missing` and nothing is mutated. A
  finalize proposal must meet the skill's completion predicate. The
  final generation then runs with tools disabled, and its answer is
  checked again before the `answer.delta` events are appended in one
  batch. Every model call is labelled with its adapter's declared
  locality (`ModelAdapter.locality`: `local` for `LlamaCppAdapter`,
  `external` for `ChatCompletionsAdapter`), and the trace counts the
  external ones. One `TraceClock` serves the store, the tools, and the
  loop.
- AC01 with model doubles (`tests/test_ac01_local_health.py`) for both
  demo services in English and Chinese, with zero external calls
  measured on a recording external model double, and a live AC01 test
  (`tests/test_local_loop_live.py`, `pytest -m live`). The first
  recorded run is in `docs/EVIDENCE.md`: 3 of 4 completed locally. A
  rerun on the rebased stack, with the stricter #55 completion
  predicates, is recorded next to it: 2 of 4 completed, with the same
  model output. `ScriptedAdapter` (tests/helpers.py) now also scripts
  one turn per generation, and `of_type` is a shared test helper.
- Clarification and resume (#16, RM05, RM17, AC03; spec 0.6 sections 5,
  8, 11): a request that omits the target service waits in
  `waiting_input`. Its `clarification.required` event carries the
  question in the run's language (the schema's Chinese label for zh),
  the language, the reason code, `skill_id`, a non-blank `missing`
  (`skill_selection` for a model-issued clarification), and
  `waiting_seconds`. `LocalOrchestrator.resume` takes explicit input
  text. A stale version is 409 and a terminal run never restarts.
  Missing or blank text is 400 `invalid_resume_input`, and the run keeps
  waiting with its version unchanged, even after its wait expired: the
  body is checked before the waiting limit (sections 7.4, 11). A busy slot is 429, and the run keeps waiting. A resume
  records `clarification.answered` (redacted input, `missing`,
  `waited_ms`). It appends the question as an assistant turn and the
  input as a user turn, and opens a new local stage with the full
  per-stage budgets. When the clarify named a skill, the gateway checks
  the input without a routing generation (`Router.resume_decision`).
  When `skill_id` was null, the run routes again. The run keeps the
  language of its first RouteDecision. `LocalBudgets` gains the
  entire-run limits (`run_active_seconds` 60, `run_generations` 8,
  `run_tool_executions` 6), which bound all stages, as well as
  `waiting_seconds` (300) and `clarification_rounds` (2). A third needed
  clarification fails the run with `missing_input`, and an expired wait
  fails it with `input_expired`; its wait counts only toward the
  `waiting_ms` of `run.failed` (section 12.1). `expire_waiting` expires
  every other wait when one of them raises; the run that raised keeps its
  wait unchanged for the next sweep, and a run cancelled during its own
  expiry is dropped without an error (RM17). `run.completed` and `run.failed` report
  `active_ms` and `waiting_ms` separately (section 12.1). A time stop
  names the tighter budget (`active_seconds` or `run_active_seconds`).
  The `clarification.required` payload rules are added to the
  TraceEvent validator and schema: a non-blank question, language,
  reason code, and `missing`, a `skill_id` that is non-blank or null,
  and a positive, finite `waiting_seconds` (RM03). When `run.completed`
  or `run.failed` carry them, `model_calls` must be an array of objects
  and `external_calls`, `active_ms`, and `waiting_ms` non-negative
  integers (section 8) (`clarification.answered` is validated since the
  TraceEvent schema entry above).
- Invalid proposals (#17, RM02, RM04, RM08, RM09, AC04): the AC04 test
  (`tests/test_ac04_invalid_proposals.py`) covers an unknown tool, a bad
  enum value, an undeclared field, a missing field, a wrong type, and a
  tool outside the permitted set. None executes, exactly one repair is
  counted within the generation limit, and an unrepaired proposal stops
  with `invalid_output`. `Escalation` refuses a reason code outside the
  section 8 list. Inside a run, that refusal fails the run with
  `internal_error` and frees the slot.
- A third live AC01 run, on the rebuilt stack, is recorded in
  `docs/EVIDENCE.md`: 3 of 4 completed locally. The model's text
  changed with the labelled tool schemas, so the case-insensitive
  service match was not exercised. M2 is not closed: see
  `docs/ROADMAP.md`.
- Project scaffold: repository layout, CI, and contributor guides.
- Specification v0.1 (`docs/SPECIFICATION.md`) as the single source of
  truth for the prototype.
- Architecture and roadmap documents mapping spec components to code
  layout and milestones.

### Changed

- Specification 0.21 -> 0.22: section 8 adds `answer_options` to the
  run.accepted payload (#91; RM19, RM22, RM25): the effective token
  budget, temperature, and top_p applied to the answer generations. A run
  without options records an empty object, and a run stored before this
  revision has no field, which the trace and the demo UI report as not
  recorded. Section 9 adds the options to the run trace.
- Specification 0.22 -> 0.23: section 5 states that the approval and the
  execution slot are one store transaction when two gateway processes
  share a store file (#93; RM10, RM11, RM17). A second process that takes
  the slot after the first check makes the resume 429 and keeps the
  approval pending; there is no store lock. A deterministic test covers it.
- Specification 0.23 -> 0.24: section 7.4 states that a compatible chat
  disconnect cancels the run that request owns, with stream true or false,
  by the same path as the native cancel (#92; RM14, RM17, RM19, RM20,
  AC12). A native events disconnect still leaves the run untouched.
- Specification 0.24 -> 0.25: section 4.4 specifies the bounded parallel
  workflow (#30; RM14, RM15, RM16, RM22, RM23, AC14). The "not implemented"
  cause of workflow_unavailable is removed; the workflow runs a local_health
  branch and an external_checklist branch, joins them, and hands final
  ownership to a new synthesis call. Section 8 adds the branch field and the
  workflow summary of terminal events, section 9 the workflow part of the
  trace, section 12.1 the extra call latency, and section 7.4 the readiness
  of parallel_demo. The workflow checks room for both external generations
  before its branches start and again at the join, measures the draft
  request before parallel_running, and passes the draft to the synthesis
  as a JSON-encoded data message. abandoned_calls lists only parallel
  branch calls; restart recovery keeps the branch of a settled operation and
  writes no workflow object; the trace shows each stage's duration from the
  terminal workflow object.
- Specification 0.25 -> 0.26: section 10 specifies the caching policy
  (#31; RM07, RM24, RM22, AC13). The skill parse cache is keyed by file
  content hash. The routing-result cache is set by the new launcher table
  `cache` (section 14.1), off by default and in the benchmark
  configuration, and is limited to local read-only skill choices with an
  exact key, a five-minute TTL, and 256 entries. A hit skips only the
  routing generation: authorization and tools run fresh. Skill or
  permission changes drop dependent entries. Section 8 defines the
  cache.hit payload (originating_version and originating_run_id), the
  routing_cache label of run.accepted, and the routing_cache route
  mechanism; statistics exports name the cache setting (section 12.1).
  The key holds the tool calls of assistant turns and a routing prompt
  version derived from the routing prompt and functions; expired entries
  are removed as expired before each lookup and store; an unusable hit is
  counted as a miss; and every route.selected of a cache-enabled run,
  gateway-made ones included, carries the hit or miss label.
- Specification 0.26 -> 0.27: section 9 requires an automated browser test of
  the demo UI page script (#89; RM11, RM12, RM21, RM23, RM25, AC03, AC05,
  AC12). The test runs the page against the app over model doubles and
  checks the native endpoint calls, the data mode label on every view, single
  escaping, and the shown browser timestamp. The browser is a development
  dependency installed in CI on all three platforms. The approve and deny
  controls are rendered disabled and the page enables them only once it
  holds the approval token, so an early click cannot send a resume without
  it. A replay-mode view is also run in the browser.
- Specification 0.27 -> 0.28: section 12.2 defines the evaluation
  (#32, #33; RM25, sections 12.1 to 12.3 and 14). The correctness corpus
  is `evaluation/corpus.v1.json` with its sandbox seeds, frozen by a
  SHA-256 digest in `evaluation/corpus.v1.lock.json`; each case names
  its class, language, fixture seed, permissions, expected route,
  acceptable tool calls, clarification answer, confirmation decision,
  expected outcomes, required and forbidden evidence, expected sandbox
  state, and rubric, and no case repeats a development example. The
  corpus runner starts a new gateway for every case and mode, so the
  three modes start from the same fixture snapshot, and the section
  defines task success, local completion, tool correctness, schema
  validity, escalation, missed mandatory confirmation, and provider
  calls over the trace. The latency runner measures the local path over
  the compatible stream: first transport event and first answer token
  on the client's clock, first model action and first useful content
  from the trace, nearest-rank p50 and p95, timeouts and errors kept,
  a cold request apart, and the runtime's prefix cache and sampling
  settings recorded. `docs/RELEASE.md` is the release record of
  section 14.
- Specification 0.29 -> 0.30: the demo UI trace view (section 9;
  RM22, RM23) reads a tool result stored by reference back from its
  artifact and marks it as stored by reference, with its size. When
  the artifact is no longer retained, the operation shows the status
  of its tool.finished event, the reference, and that the result is
  not retained, with no result data. Before this, a run whose tool
  result was over the inline limit could not be shown (KeyError).
- Specification 0.28 -> 0.29: AC08 defines a second compatible external
  endpoint (#109; RM18, section 12.3): it has its own base URL, and the
  launch file names its own credential variable (it may hold the same
  key when one vendor serves both base URLs); another model behind the
  same base URL is not one.
  For the external switch only the external_model table of the launch
  file changes. The live acceptance harness takes
  `--second-external-base-url`, `--second-external-model`, and
  `--second-external-context-limit`, reads the second credential from
  `RELAYMIND_SECOND_EXTERNAL_API_KEY` (the launch file names only the
  variable; a second base URL needs its own context limit), and records
  the swap as `ac08_external_swap` only when the two base URLs differ
  (ignoring case, a default port, and a final slash) and the second
  names its own credential variable; any other swap is recorded as
  `external_model_swap` (RM18) and does not count for AC08. The #88
  same-URL swap in `docs/EVIDENCE.md` is marked as not AC08.
- Documentation brought up to date after M3 and M4: the README status,
  features, layout, and a verified script that starts the gateway by
  hand (there is no launcher yet); the M3 and M4 status and carried
  forward items in the roadmap; the open gaps in `docs/TESTING.md` and
  `docs/EVIDENCE.md`; the API and UI notes in `docs/ARCHITECTURE.md`
  and `AGENTS.md`; and small fixes in `models/README.md` and
  `skills/README.md`. No behavior change.
- Specification 0.17 → 0.18: section 7.4 defines idempotency keys (RM17,
  RM21, AC11, #26): the key format, the caller scope, the request hash,
  that a key is stored with the run it creates, the replay of a native
  creation (202 for the existing run) and of a stored chat response
  (also as chunks), 409 `idempotency_conflict` for a changed payload or
  an unfinished chat duplicate, 409 `run_terminal` for a failed or
  cancelled one, and the key leaving with its run at the retention limit.
  Section 7.1 replaces the refusal of `Idempotency-Key` with its format
  check and the check order, and section 11 widens the 409 row.
- Specification 0.16 → 0.17: section 7.4 defines the native run bodies and
  views (RM21, AC11, #25): the creation checks and 202 body, the run view
  and the once-only confirmation token, the SSE stream (event ID is the
  stored `seq`, replay from `Last-Event-ID` reads the store only, the
  stream stays open across waits and ends after the terminal event), the
  resume check order, cancel semantics, and `/healthz` and `/readyz`
  readiness per permitted mode. Section 4.4 names the unimplemented
  workflow as a cause of `workflow_unavailable` and says the gateway
  checks it before routing. Section 8 adds the `cancel_requested` reason
  to `run.cancelled`.
- Requalify llama.cpp b11785 on Apple Silicon macOS/Metal and native
  x86-64 Windows/CPU (#58; RM18). Migrate the manifest to `--reasoning off`,
  derive the matching HTTP override, and explicitly separate reasoning
  from answer content. Non-thinking remains the default; operator-enabled
  thinking remains supported. Record both live runs separately from the
  historical runtime evidence and add a Windows runtime smoke workflow.
- Specification 0.14 → 0.15: section 7.3 defines the streaming contract
  (RM20, AC09, #23): the buffered stream of the managed alias as a replay of
  the run's `answer.delta` events, the live stream of a backend that forwards
  provider text, append-only content chunks, and the mid-stream failure
  behavior (run recorded as failed, one fixed `error` data event, the
  connection closed with no finish chunk and no `[DONE]`). A failure
  before any answer text, or of a request without stream, is a normal
  503 `external_failed` response, and a client disconnect closes the
  provider stream.
- Specification 0.13 → 0.14: section 7.1 defines request validation for the
  compatible chat endpoint (RM19, AC16, #22): the order of the checks, which
  fields and message shapes are supported, the named `unsupported_parameter`
  and `invalid_request` cases, message ordering and tool-call references,
  how sampling and token budgets are applied (an external generation
  offered tools keeps its own token budget) and the operator limit on the
  token budget, the rejection of `Idempotency-Key` until it is
  implemented, the response and stream shape, 409 `run_terminal` for a
  run that ends as run state, and the loopback bind and generated token
  rules.
- Specification 0.12 → 0.13: section 11 defines the error contract (RM04,
  AC07, AC16, #21): the error body with a stable code, message, and run
  reference, the status for every code, and the mapping of failed runs
  (needs_external 422, context_limit_exceeded 400, external_failed 503 or
  504 for an exhausted time limit, workflow_unavailable 503, internal_error
  500). It also defines degraded mode as an operator policy, off by
  default, with its trace record (`degraded_mode` and `local_failure` in
  `handoff.started`). Sections 4.4 and 8 follow.
- Specification 0.11 → 0.12: section 8 states that redaction also replaces a
  secret value (string or number) repeated in the data or error message of
  the same tool result, and in later tool results, model text, proposed
  tool arguments, and resume input of the run, and that
  injected text in tool output never changes tools, permissions, or
  approvals (RM08, RM11, RM12, AC06, #20).
- Specification 0.10 → 0.11: section 5 defines restart recovery (RM10,
  RM17, #8): when it runs, what it settles (pending writes as
  `unknown_outcome`, pending reads as `failed`, pending approvals
  expired), the `interrupted` failure and that a recovered run is never
  resumed. Section 8 adds `interrupted_state` and `settled_operations`
  to the `run.failed` payload.
- Specification 0.9 → 0.10: sections 4.3, 4.4, 5, 7.4, 8, 9, and 11 define
  the confirmation flow (RM11, #19): the `confirmation_target` a mutating
  tool declares, the single-use token and its hash, the binding, the
  `confirmation.required` and `run.cancelled` payloads, resume with
  `approval_id`, `token`, and `approve`, `approval_expired`,
  `user_denied`, and 403 `approval_invalid`. A mutating proposal no
  longer ends the run as `needs_external`, and the external stage uses
  the same flow.
- Specification 0.8 → 0.9: sections 4.4, 8, and 11 define the handoff
  (RM13, RM14, #18): how the HandoffEnvelope is built from the run
  store and sent as normalized messages, the context measurement and the
  `context_limit_exceeded` failure, what blocks a handoff (including a
  mutation that needs confirmation, a skill with no permitted tool, an
  admitted parallel route, and an unavailable local model),
  the external stage and its limits, the `handoff.started` payload, and
  the `external_failed` error code.
- Specification 0.7 → 0.8: section 6 uses the selected runtime's
  supported reasoning controls (RM18, #58). llama.cpp b11785 uses
  `--reasoning off/on` at launch and the supported HTTP template boolean
  for per-request overrides, with explicit `deepseek` reasoning extraction.
- Specification 0.6 → 0.7: Chinese support is best-effort; native
  Chinese review is no longer required (sections 9, 12.2). English
  examples, corpus scenarios, and live traces stay required; Chinese
  ones are kept where they exist and reported per language, but do not
  block acceptance (sections 1, 6, 12.3, AC01, AC15, section 14).
  Evidence must say when Chinese material is not natively reviewed
  (RM16). No native Chinese reviewer is available to the project.
- Specification 0.5 → 0.6: section 8 adds cross-field rules for
  RouteDecision (clarification, and skill_id by route, mode, and the
  enabled parallel workflow), names the `run.accepted` `model_alias`
  field, and defines the `clarification.required` payload and the new
  required `clarification.answered` event. Section 7.4 states that a
  native run takes no model alias and always uses managed execution,
  that `external_only` records a gateway-produced RouteDecision with
  reason code `execution_mode`, and that a resume body that does not
  match the waiting state is 400 `invalid_resume_input`. Section 4.4
  says the operator enables the parallel workflow and which execution
  modes admit it. RM04 scopes the explicit escalation proposal to the
  routing decision for the MVP. Sections 5 and 11 define clarification
  resume: continue with the chosen skill or route again, per-stage
  local budgets with a two-round clarification limit, the run language
  kept across resume, and the resume input and waiting time recorded
  as evidence. After review: RM04's explicit proposal is `route=external`
  with `complex_skill` only, the `parallel_workflow` external route is
  gateway-produced, and `route=parallel` where the workflow is not
  admitted is invalid output; section 8 constrains `reason_code` by
  route, says the gateway sets the language of every later decision,
  and defines the `run.completed` and `run.failed` payloads
  (`active_ms`, `waiting_ms`); the resume body field is `input`
  (section 7.4); `parallel_demo` with an unavailable workflow fails with
  `workflow_unavailable` (503 on the chat endpoint); section 12.1 sums
  all recorded waits; AC03 references RM22. Owners: the RouteDecision validator (#53; #14), the
  run.accepted validator (#54; #27), and the clarification flow
  (#61; #16).
- Specification 0.4 → 0.5: require proxy bypass for loopback llama.cpp
  inference and startup health polls, preserving normal proxy handling
  for non-loopback endpoints and the external Chat Completions adapter
  (#57; RM01, RM18; section 6).
- Roadmap after M1: M2 split into a routing probe plus vertical slice
  and a widening stage; required trace events (#27) moved from M4 to
  M2; restart recovery (#8) moved from M2 to M3; Windows qualification
  (#46) added to M1; the orchestrator half of AC08 (#49) tracked in M3.
  Milestones now close against `docs/EVIDENCE.md`. No behavior change
  and no spec revision.
- Specification 0.3 → 0.4: the compatible chat endpoint accepts an
  optional `X-RelayMind-Execution-Mode` request header carrying the
  native `execution_mode` values under the same operator policy, so a
  standard OpenAI client can force a local or external run without
  the native API. The model alias keeps selecting tool ownership only.
  Section 7.1 lists request headers in their own table and names the
  error codes for invalid, forbidden, and body-supplied modes
  (sections 7.1, 7.2, 7.4; issues #22, #24).
- Specification 0.4: `needs_external` on the chat endpoint is HTTP 422
  with an error object (sections 7.2, 11).
- Specification 0.4: section 4.2 states where skills come from: an
  operator-configured list of local roots, `["skills/"]` by default,
  where every subdirectory is a candidate package with `SKILL.md` and
  `skill.manifest.json`, unique names across roots, and strict load
  errors (issue #9).
- Specification 0.4: section 8 defines the `run.accepted` payload,
  which records the execution mode, model alias, budgets, skill roots,
  and offered skills for every run.
- Specification 0.2 → 0.3: the artifact manifest records the "runtime
  version and commit" (one composite field) instead of only a "runtime
  commit", matching the implemented `runtime_version` field.

- Specification 0.1 → 0.2: section 6 now allows an operator option to
  enable Qwen3 thinking mode through the same chat-template settings.
  Non-thinking stays the default and the latency-experiment
  configuration; reasoning output never appears as answer content, and
  results from the two modes are never mixed.
