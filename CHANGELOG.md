# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- Loopback llama.cpp inference and runtime startup health polls bypass
  environment and system proxies without changing process-wide settings
  (#57; RM01, RM18). Non-loopback endpoints and the external Chat
  Completions adapter keep normal proxy handling. Deterministic tests
  cover localhost, IPv4/IPv6 loopback, and remote hostnames; the historical
  Windows qualification remains evidence for its recorded revision only.

### Added

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
  final answer is external (section 9). Native Chinese review is still needed.
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
- Project scaffold: repository layout, CI, and contributor guides.
- Specification v0.1 (`docs/SPECIFICATION.md`) as the single source of
  truth for the prototype.
- Architecture and roadmap documents mapping spec components to code
  layout and milestones.

### Changed

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
