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
