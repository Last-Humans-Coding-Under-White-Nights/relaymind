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
