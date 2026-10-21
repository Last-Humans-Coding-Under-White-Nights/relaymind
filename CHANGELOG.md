# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

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
  recovery time. Each run is recovered in one store transaction, one
  failing run does not stop the others, terminal runs are untouched,
  and a second recovery changes nothing. No model is called and no
  model loop is resumed. The startup call is part of the API work
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

- Requalify llama.cpp b11785 on Apple Silicon macOS/Metal and native
  x86-64 Windows/CPU (#58; RM18). Migrate the manifest to `--reasoning off`,
  derive the matching HTTP override, and explicitly separate reasoning
  from answer content. Non-thinking remains the default; operator-enabled
  thinking remains supported. Record both live runs separately from the
  historical runtime evidence and add a Windows runtime smoke workflow.
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
