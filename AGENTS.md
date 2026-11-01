# AGENTS.md — Contributor guide for RelayMind

This file is for AI agents and human contributors working on the RelayMind
codebase.

## The two rules

1. **The spec is the single source of truth.**
   [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) defines what RelayMind
   does. Every implementation task and every acceptance test must reference
   requirement IDs from it (RM01–RM44, AC01–AC37; RM30 and AC23 are retired;
   RM33 and AC24 specify the M9 OpenClaw integration, added in 0.47 and
   revised in 0.49 and 0.50; RM34–RM44 and AC25–AC37 define M10
   recommendations, added in 0.51 and revised in 0.52, 0.53, 0.55, 0.56, 0.57, 0.58, 0.59, 0.60, 0.61, 0.62, 0.63, 0.64 and 0.65; the spec is at 0.65). Implementation status
   is recorded in docs/ROADMAP.md. If code and spec disagree,
   the spec wins. If the spec is wrong, revise the spec first, then change
   the code.
2. **Tests come before code.** Write a failing test that captures the
   requirement, watch it fail, then implement until it passes. Do not merge
   implementation code without tests.

## Repository map

| Path | Purpose |
|------|---------|
| `src/relaymind/api/` | API layer: request validation, chat contract, native run API, idempotency keys (`idempotency.py`; RM17, RM21), auth (RM19–RM21); the error contract (`errors.py`: stable codes, one status each, failed-run mapping; section 11), the bearer token check (`auth.py`), chat request validation (`chat_request.py`), the chat backends and response bodies (`chat.py`), the native run API (`native.py`: run view, SSE events, resume, cancel, readiness; RM21, section 7.4), the FastAPI app (`app.py`; a chat disconnect cancels the run the request owns, RM14, section 7.4), and the loopback-only server (`server.py`; RM19, section 7.1); early response headers for a streamed chat request (`app.py`, `chat.py`; RM32, section 7.3: the run is accepted first, headers and keep-alives go out at once, and the response owns the run's task); the operator starts it with `relaymind serve --config` (`src/relaymind/launcher.py`) |
| `src/relaymind/orchestrator/` | State transitions, budgets, routing, handoff, cancellation, final-answer ownership (RM01–RM05, RM13–RM17); the RouteDecision schema and validator (`route_decision.py`, `schemas/route_decision.v1.json`; RM02); the router (`router.py`; native function calling; RM01, RM02), the bounded local loop and the parallel workflow (`local_loop.py`, `workflows.py`; RM14–RM16; `skill_stage_messages` is the skill-stage conversation as a pure function), budgets (`budgets.py`; RM03, RM04, section 11), identifier provenance (`identifiers.py`; RM05), the client-owned alias run (`client_loop.py`; RM08, RM19), and the handoff envelope, dispatch authority, and external request (`handoff.py`, `schemas/handoff_envelope.v1.json`; RM13, RM14), and restart recovery (`recovery.py`; RM10, RM17, section 5), and the optional routing-result cache (`routing_cache.py`; RM07, RM24, section 10), and the performance options and draft guards (`performance.py`; RM31, RM32, section 14.1), and prefetch resolution with its budget check (`prefetch.py`; RM29); `local_loop.py` also admits several read-only calls per generation (section 4.3); the startup warm-up of the local model and its detection (`warmup.py`; RM18, RM26, RM27, section 6); the class of the local model, ready, unavailable, unsuitable, or absent, and the modes each forbids (`local_status.py`; RM26, section 7.4), with the local stage rule of spec 0.48 (`stage_status`; section 14.4.7) |
| `src/relaymind/adapters/` | Model adapters: normalized messages and events per provider (RM18); `Usage` keeps `None` for a count the source did not return, the `FirstToken` marker dates the first fragment, `cache_prompt` is sent to llama-server, and the external adapter sends and declares the operator's `request_options` (RM16, RM25, RM27, spec 0.31 section 6) |
| `src/relaymind/assets/`, `src/relaymind/store/asset_catalog.py` | Independent catalog discovery (#192; RM34/AC25): frozen records/snapshots and safe projections (`catalog.py`), bounded ordinary local SKILL.md and closed metadata-import staging (`sources.py`, `source_io.py`, `_windows_source_io.py`, `schemas/`), explicit configuration (`settings.py`), one cancellable scan worker (`service.py`), and atomic catalog/provenance/tombstone/job storage over RunStore (`src/relaymind/store/asset_catalog.py`). All admitted assets have availability unknown. Selection is in `src/relaymind/selection/` (#193) and the recommendation API in `src/relaymind/recommendations/` (#194); the installed `github.relaymind-skills.v1` profile adds bounded immutable remote capture (`remote_contracts.py`, `remote_transport.py`, `remote_github.py`), with profile-keyed cooldown in `store/asset_remote_state.py`. No host authority or execution is supplied. |
| `src/relaymind/selection/` | Recommendation-only v2 (`asset_contract.py`, `asset_admission.py`, `asset_chain.py`, `asset_lexical.py`, `asset_http.py`, the configured primary in `asset_factory.py`, v2 schemas; #193, RM35/AC26) has a projection-only lexical floor, strict loopback HTTP and separate result evidence; managed v1 remains separate. Skill selection behind one published interface (RM02, RM28): the SelectionRequest and SelectionResult schemas and their boundary checks (`contract.py`, `schemas/`), the catalog the request carries (`catalog.py`), the chain with its time limit and attempts (`chain.py`), the gateway's admission of a result (`admission.py`), the built-in `model_router` (the routing generation, the default chain; `model_router.py`) and `example_match` (deterministic, no model call; `example_match.py`), and the `[selection]` settings (`settings.py`) |
| `src/relaymind/recommendations/`, `src/relaymind/api/recommendations.py`, `src/relaymind/store/recommendations.py` | Independent recommendation lifecycle (#194; RM36/AC27): the service with its catalog provider, create, cancel, retention sweep and restart recovery (`service.py`), the recorded budgets and limits (`limits.py`) and domain errors (`errors.py`); the JSON polling API with readiness and events (`api/recommendations.py`); claims, idempotency, views, events and retention in SQLite (`store/recommendations.py`), checked against `store/schemas/recommendation_view.v1.json`, `recommendation_event.v1.json`, `recommendation_result.v1.json` and `recommendation_readiness.v1.json` (`store/recommendation_event.py`). TraceEvent stays at v1; no invented run ID. |
| `src/relaymind/api/user_actions.py`, `src/relaymind/recommendations/registrations.py`, `src/relaymind/recommendations/private_files.py`, `src/relaymind/recommendations/erasure.py`, `src/relaymind/store/asset_feedback.py` | Backend direct actions and feedback privacy (#195; RM37/AC28): operator-registered action authentication, independent choice/feedback CAS, scoped inspection and logical erasure/retention. The pinned OpenClaw 2026.10.7 direct-action operations are qualified under spec section 7.6.1 only for the exact adapter revision in the shipped registry, and every native request carries its binding's expected scope (spec 0.63); snapshots, exact skill loaded-use and MCP execution remain blocked. Deterministic fixtures inject their own qualification resolver. No public enrollment; the bounded local adaptation backend uses only explicit corrections and an independently qualified semantic configuration. The production embedding qualification registry holds only the two exact reviewed Linux CPU identities. |
| `src/relaymind/skills/` | Skill registry: package validation, instruction loading (RM06, RM07); completion predicates with their failed checks and the criteria stated to the external model (`predicates.py`; RM06, RM12, sections 4.4 and 9); the content-hash parse cache (`cache.py`; RM24, section 10); the manifest `prefetch` declarations are validated in `registry.py` (RM29) |
| `src/relaymind/tools/` | Tool registry and executor: argument validation, permissions, side effects (RM08–RM12); the sandbox demo data service and the seven demo tools (`sandbox.py`, `schemas/demo/`; RM23, section 9); the executor (`executor.py`: operation IDs, replay, retry, timeouts; RM10, RM14) |
| `src/relaymind/store/` | Run store: SQLite events, tool outcomes, approvals, versioned state (RM17, RM22); every event is validated against the published TraceEvent schema before it is written (`trace_event.py`, `schemas/trace_event.v1.json`; RM22, section 8), including the optional model-call fields `first_token_ms`, `usage` counts, and `request_options` (one table in `trace_event.py`); timing samples, their retention, and the timing and skill outcome estimates (`model_timings.py`, `schemas/model_timing.v1.json`; RM17, RM25, RM27, section 8); the skill and local stage outcomes projected in the event transaction (`outcomes.py`), the stage outcomes kept in `local_stage_outcomes` for the unsuitable rule (RM26, spec 0.48 section 14.4.7) |
| `src/relaymind/launcher.py` | The operator launcher, `relaymind serve --config` (`relaymind.example.toml`): config file (including the `[budgets]` and `[placement]` tables and `external_model.request_options`), startup checks, restart recovery once, the local model warm-up (`warm_up_gateway`; `orchestrator/warmup.py`; RM18, RM27, section 6), token handover (RM03, RM12, RM17–RM19, section 14.1) |
| `src/relaymind/canonical.py` | One canonical JSON text form, and its SHA-256 (`canonical_digest`), for tool schema hashes (RM07), operation IDs (RM10), approval binding (RM11), and idempotency request hashes (RM17) |
| `src/relaymind/schema_util.py` | Shared boundary checks for published, versioned JSON Schemas: closed field sets, exact schema_version, first schema error (spec section 8) |
| `src/relaymind/redaction.py` | Configured secret-field redaction before anything is persisted (RM12, section 8) |
| `src/relaymind/stats.py` | Per-generation timing metrics and JSON export for adapter comparisons, labeled with the routing cache setting (RM25, section 12.1) |
| `src/relaymind/ui/` | Demo UI: decisions and execution evidence (RM23); the trace view built from stored events (`trace_view.py`), escaped HTML (`render.py`), routes and static page (`routes.py`, `static/`), the comparison on reset fixtures (`comparison.py`), and the declared data mode (`settings.py`) |
| `src/relaymind/evaluation/` | Evaluation (RM25, spec section 12): the frozen corpus loader, digest check, and skill sidecar (`corpus.py`; RM28, spec 0.31), grading one run from its trace (`grading.py`), corpus and latency summaries and reports (`metrics.py`), the environment manifest and checkout revision (`environment.py`), and the OpenClaw evaluation tasks, grading, and arm summary (`openclaw.py`; RM33, spec 0.50 section 12.5) |
| `evaluation/` | The frozen correctness corpus (`corpus.v1.json`, its digest in `corpus.v1.lock.json`), the expected skill of every case (`corpus.v1.skills.json`, frozen in the same lock), and the latency prompts (`latency_prompts.v1.json`); a changed case is a new corpus version (spec section 12.2); the OpenClaw evaluation tasks (`openclaw_tasks.v1.json`, digest in `openclaw_tasks.v1.lock.json`; spec 0.50 section 12.5) are a separate frozen set |
| `models/` | Pinned model artifact manifests (RM18, spec section 6); GGUF files are downloaded per `models/README.md`, never checked in |
| `skills/` | Demo skill packages, one directory per skill, SKILL.md plus `skill.manifest.json`; the default skill root (operators may configure others, spec section 4.2) |
| `integrations/openclaw/` | M9 (RM33, spec 0.50): the OpenClaw provider package, TypeScript over the loopback client-owned chat API (section 7.5). #135 holds the qualification: the declared host switches (`src/compat.ts`), the pinned host transport driver (`test/host-transport/`), the frozen v2026.10.7 wire fixtures (`test/fixtures/`), and the host capture endpoint (`scripts/`); `npm ci && npm run typecheck && npm test`. #136 adds the provider plugin: the manifest (`openclaw.plugin.json`), the entry (`src/index.ts`, `definePluginEntry`), the provider and token method (`src/provider.ts`), catalog discovery and its diagnosis (`src/catalog.ts`), the loopback-only settings (`src/config.ts`), the capability statement (`src/diagnostics.ts`), and the redacting logger (`src/redaction.ts`); `npm run pack:check` builds `dist/` and checks the tarball; `tests/test_openclaw_plugin.py` drives it against the gateway (`test/plugin-transport/`). #137 adds the stream wrapper (`src/stream.ts`: the provider's `wrapStreamFn`, an empty answer or more than one call is a failure) and the input guard (`src/input-guard.ts`: unsupported input refused by field, never removed), the test-only host tools (`test/host-tool-plugin/`), and the round trips through the plugin (`tests/test_openclaw_round_trips.py`) with the host approval scenarios of `scripts/openclaw_qualification.py host --scenario`. #138 adds the error classes (`src/errors.ts`), the Idempotency-Key and turn ID (`src/idempotency.ts`), the bounded retries in the stream wrapper, and the per-attempt run-ID records (`src/attempts.ts`), with the abort, error, and replay cases in `tests/test_openclaw_cancel_errors.py` and the `abort` host scenario. #145 adds the release artifact (`npm run pack:release`, `scripts/release-pack.mjs`: a reproducible tarball with `SHA256SUMS` and `release.json`; `tests/test_openclaw_install.py`), the install check over the host's lifecycle (`scripts/openclaw_install_check.py`, `tests/test_openclaw_install_check.py`), the CI job `openclaw-plugin`, and the operator guide in its README. #139 adds the evaluation runner (`scripts/run_openclaw_eval.py`) over the tasks and grading in `evaluation/` and `src/relaymind/evaluation/openclaw.py`. Covered over model doubles; live and platform status is in `docs/ROADMAP.md` (M9); see `docs/ARCHITECTURE.md` |
| `tests/` | Test suite; `tests/fixtures/` holds sandbox demo data; `tests/test_demo_ui_browser.py` runs the UI page script in Chromium (Playwright; `playwright install chromium`) |
| `scripts/` | Developer scripts: model manifest generation (RM18), the live acceptance harness, the corpus runner (with its offline draft-pass regrade), the latency benchmark (RM25), `--runtime-extra-args` for a constrained runtime configuration (RM18, spec 0.31 section 12.2), the spikes in `scripts/probes/` (the offline ones over the corpus export, and the live timing probes of the runtime and of one provider), and the M9 OpenClaw scripts (`openclaw_qualification.py`, `openclaw_request_size.py`, `openclaw_install_check.py`, and the evaluation runner `run_openclaw_eval.py`; RM33, spec 0.50 section 12.5); `scripts/README.md` lists what exists |
| `docs/` | Spec, architecture, roadmap, testing guide, live evidence record (`docs/EVIDENCE.md`, raw exports in `docs/evidence/`), and design rationale for spec revisions (`docs/design/`, not normative). Customer recommendation entry points are `asset-selection.md` / `asset-selection.zh-CN.md`; `asset-privacy-security.md` covers current source/privacy/recovery boundaries. Detailed workflows remain in `SOURCE_BENCHMARK.md` and `ASSET_BENCHMARK.md`. |

## M10 ownership (spec 0.65; #187, #192, #193, #194, #195, #196)

The independent catalog discovery core above is implemented by #192,
recommendation-only selection v2 with its lexical fallback by #193, the
recommendation lifecycle/API above by #194, and backend direct actions and
feedback privacy above by #195. #196 adds lexical startup/publication/rebuild and the
shared background scheduler; real AC29 operational/platform measurements remain
open. The hybrid/index and bounded adaptation backends are implemented with trusted deterministic injection and provisional native evidence; ordinary artifact activation and final acceptance remain open. The installed remote profile has two actual periodic functional jobs. Other pending components below must not be described as implemented.
The user explicitly authorized implementation on the reviewed specification PR
stack before #187 merges; each implementation issue still needs its RM/AC
mapping. Merges remain user-owned. Contracts are in spec sections
4.6 to 14.5; blockers, gates, the merge gate and issue order are in
[docs/ROADMAP.md](docs/ROADMAP.md) (M10); the customer-approved defaults are in
spec section 1. Managed selection v1 (AC22) and the provider and host contract
(AC24) are unchanged.

| M10 path | Owner and requirements |
| --- | --- |
| `src/relaymind/assets/`, `src/relaymind/store/asset_catalog.py` | #196 implements lexical preparation/publication (`index_lifecycle.py`), cooperative batch gates (`scheduler.py`), shared source/derived rebuild ownership, compatibility/eligibility storage and restart repair; RM38/AC29. Full real operational measurements remain open. The installed GitHub profile implements #203 remote refresh, RM43/AC34; full operational/platform acceptance remains separate. The independent discovery core is listed above. |
| `src/relaymind/selection/asset_hybrid.py`, `src/relaymind/selection/asset_adaptation.py` | Implemented local pinned hybrid/index backend and bounded, reversible correction adaptation; RM41, RM42; AC32 (implemented), AC33 (backend only; the 100-episode gate is open). Inference/cost and base/final ranks remain separate. The production artifact qualification registry contains the two exact reviewed Linux CPU identities; ordinary activation is limited to that bundle, while host correction authority, formal episodes and final quality/platform gates remain open. |
| `src/relaymind/evaluation/asset_corpus.py`, `src/relaymind/evaluation/asset_benchmark.py`, `evaluation/assets/`, `scripts/asset_selection_benchmark.py` | Implemented source-grounded 412-asset/128-case development workflow and shared production-path run/compare, alongside formal corpus/lock tooling; RM40, RM44; AC31 (development profile). AC36/AC37 gates remain open (#205/#206). Developer-authored references do not establish independent formal labels or final gates; see docs/SOURCE_BENCHMARK.md. |
| `src/relaymind/evaluation/asset_whole_task.py`, `asset_episode_export.py`, `asset_platform_export.py`, `asset_whole_task_inputs.py`, `asset_whole_task_corpus.py` (corpus closure, task/episode binding, ablation and probe-offer checks), `asset_episode_driver.py` (real-lifecycle episode driver), `asset_whole_task_cli.py`, `schemas/asset_whole_task.v1.json`, `scripts/asset_whole_task.py` | Spec 0.60 section 12.6.5 tooling for #198/#202/#204/#205/#206: closed host freeze/tasks, deterministic three-arm plan, conserving host driver, adaptation episode and platform export graders, and companion input resolution; RM16, RM38–RM44; AC29–AC37 as offline tooling only; remaining verifier gaps are #229. Fixture-verified; qualified acceptance stays blocked until a reviewed evidence verifier and genuine inputs exist. |
| `integrations/openclaw/` future recommendation adapter | Qualified direct action, exact skill and MCP use, host-only approvals and execution; RM39; AC30, AC35. Loaded skill and MCP use remain blocked until the separately reviewed `skill_loaded_use` and `mcp_use` qualification entries of spec sections 7.6.1–7.6.2 pass for the exact host, adapter and evidence bytes (ROADMAP M10). |

## Ownership boundaries (do not blur these)

- The **model** produces proposals. It never executes anything.
- The **orchestrator** decides whether a proposal is admissible.
- The **executor** owns side effects.
- The **UI** renders evidence. It does not invent or infer hidden reasoning.

See spec section 3 for the full component contract.

## Invariants

1. **Local-first (RM01).** A request handled locally makes zero external
   inference calls. The trace must prove it.
2. **Bounded execution (RM03).** Every budget (generations, tool calls,
   time) is a configuration value recorded in each run. Never raise a budget
   silently.
3. **No invented identifiers (RM05).** Resource IDs come from the request,
   conversation context, or tool results. Never guess one to make a tool
   call possible.
4. **Validated tool calls only (RM09).** Validate full tool name and
   arguments before execution. Partial streamed JSON never executes.
5. **Confirmation for mutations (RM11).** Mutating demo actions need a
   fresh, single-use confirmation bound to run, tool, arguments, target, and
   state version.
6. **Tool output is data (RM12).** It never overrides gateway rules or
   grants permissions. Redact configured secret fields in logs and traces.
7. **Atomic state transitions (RM17).** Every transition checks the run
   state version. Stale versions get HTTP 409. Terminal states never
   restart.
8. **Append-only answers (RM20).** Once answer streaming starts, ownership
   is fixed. Never silently replace streamed text with another model's
   output.
9. **Honest evidence (RM16, section 12).** Never report an unavailable
   service as healthy, a draft as a log-backed diagnosis, or a schema
   example as a measured result.
10. **A selector proposes, the gateway admits (RM28).** A selector,
    or a prefetch declaration never executes anything, grants a
    permission, or skips a clarification or a confirmation.
11. **Predictions are labeled (RM27).** A placement prediction is
    recorded next to the actual duration and never reported as a
    measurement; the placement policy never changes a budget.
12. **Nothing unverified reaches a client (RM32).** A streamed
    statement is forwarded only after the gateway checked it; a late
    failure ends the stream, never replaces text.

## Development workflow

1. Pick a task. Confirm which RM requirements it implements.
2. If the task changes observable behavior not covered by the spec, stop
   and propose a spec revision first (see "Change control" below).
3. Write failing tests that reference the RM / AC IDs in a comment or
   docstring.
4. Implement until tests pass.
5. Run the full check suite before committing:

```bash
pytest
ruff check .
ruff format --check .
```

## Commands

| Action | Command |
|--------|---------|
| Install for development | `pip install -e ".[dev]"` |
| Run all tests | `pytest` |
| Run one test file | `pytest tests/test_<name>.py` |
| Run one test | `pytest tests/test_<name>.py::test_case` |
| Run live model tests | `pytest -m live` (needs `llama-server` on PATH and the model artifact; see `models/README.md`) |
| Lint | `ruff check .` |
| Format | `ruff format .` |

## Testing policy

- **Deterministic tests use model doubles.** Orchestration, state,
  authorization, and API contract tests must not depend on live model
  output (spec section 13).
- **Live model tests are separate.** Capability tests with real models are
  marked `live` and excluded from the default run and CI (`pytest -m live`
  runs them). Passing doubles alone does not satisfy live-demo
  requirements.
- **Fixtures are sandboxes.** `tests/fixtures/` holds disposable demo data
  only. Nothing in tests may touch a production resource.
- **Bilingual coverage.** Demo skills carry paired English and Chinese
  examples; the evaluation corpus keeps both languages (spec section 12.2).

## Change control (SDD)

Changes to tool ownership, approval rules, API behavior, supported
platforms, or evaluation definitions require a specification revision
**before** implementation changes. Implementation-only refactoring may keep
the spec unchanged when observable behavior is unchanged (spec section 14).

Spec revisions bump the version line at the top of
`docs/SPECIFICATION.md` and are recorded in `CHANGELOG.md`.

## Style

- Python 3.12+, type hints on public functions.
- Simple English in docs, comments, and messages.
- Keep PRs focused: one logical change per PR, with the RM / AC IDs it
  covers in the description.
