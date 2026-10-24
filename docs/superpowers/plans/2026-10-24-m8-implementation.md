# M8 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver M8 timing storage, placement, HTTP selection, gated answers, and measured evaluation as focused PRs extending the existing stack.

**Architecture:** Keep the gateway's ownership and budget boundaries. The store supplies measured timing and outcome statistics; placement consumes those statistics without changing budgets. HTTP selectors propose through the existing admission chain, and live answers pass the existing evidence predicates before publication. Evaluation records the exact configuration, source revision, failures, and measured results.

**Tech Stack:** Python 3.12+, SQLite, asyncio, FastAPI, httpx, pytest, Ruff; the pinned llama.cpp runtime for live evaluation.

**Spec:** `docs/SPECIFICATION.md`, starting at version 0.37; issues #129, #130, #133, #125, and #134 in milestone M8.

## Global Constraints

- The specification is the single source of truth. Any new observable contract is specified and versioned before implementation.
- Python 3.12+, type hints on public functions.
- Tests come before implementation; acceptance tests reference RM/AC IDs. Model doubles are deterministic and live runs are separate.
- A selector proposes; the gateway admits; the executor alone owns side effects.
- No invented identifiers, hidden budget increases, unsafe mutation, unverified answer publication, or replacement of already emitted text.
- Missing provider measurements remain null, not zero. Predictions remain distinct from measurements. Failed and timed-out evaluation cases remain in denominators.
- Keep macOS and native Windows support. Preserve `local_first`, `buffered`, and the current selector defaults until evidence supports a specification decision.
- Every new PR is assigned to devnullexile, carries milestone M8, names its RM/AC requirements, links its issue, and targets the previous PR branch. The first targets `m7/runner-configurations` (#159).
- No AI attribution in commits, PRs, or review replies. Do not merge PRs.

## Review Focus

- Unknown token counts, zero-length timings, failed generations, and warm/cold prefixes must not produce fabricated or infinite throughput estimates (Task 1).
- Timeout cleanup and idle-probe races must not admit a late generation, lose evidence, or overlap an active run (Task 2).
- Redirects, environment proxies, malformed responses, forged identity, and sensitive conversation fields must not bypass the HTTP selector boundary (Task 3).
- A rejected statement or late completeness failure must never leak text, emit a success terminator, or replace a published prefix; cancellation must reach inference (Task 4).
- Seeded timing samples, machine configuration, selector claims, and missing usage must stay visible in evaluation; failed cases and separate configurations must not disappear in aggregation (Task 5).

---

### Task 1: Timing samples and outcome estimates — #129

**Files:** Create `src/relaymind/store/model_timings.py`; modify `src/relaymind/store/run_store.py`, `src/relaymind/store/__init__.py`, generation recording in `src/relaymind/orchestrator/local_loop.py`, `client_loop.py`, and `warmup.py` as needed; extend `tests/test_run_store.py` and add `tests/test_model_timings.py`; update `docs/TESTING.md` and `CHANGELOG.md`.

**Interfaces:** Introduce immutable `ModelTiming` with model, purpose, kind (`run`, `warm_up`, `probe`), input_tokens, nullable cached_tokens/first_token_ms/output_tokens, duration_ms, succeeded, nullable run_id, and recorded_at. Expose store methods `add_model_timing(sample: ModelTiming) -> None`, `model_timings(*, model: str | None = None, purpose: str | None = None) -> list[ModelTiming]`, and `timing_estimate(model: str, purpose: str)` returning typed warm/cold throughput and duration/output statistics with optional unavailable values. Expose typed per-model/skill outcome statistics to Task 2; document their exact signature in the report before integration. Task 5 uses the same validated sample writer for seeding.

- [ ] Write failing migration, round-trip, invalid-input, retention, and estimate tests (RM17, RM25, RM27). A database at the previous user_version must upgrade without editing old migrations.
- [ ] Test seven-day default retention, explicit run deletion, and a fixed bound of 1,000 samples per model/purpose, ordered deterministically by timestamp and insertion order. Failed samples remain recorded but do not enter successful-throughput estimates.
- [ ] Test cold and warm samples separately, null measurements, zero-duration samples, and medians from synthetic data with known results. No division by zero or missing-as-zero estimates.
- [ ] Append the migration and implement the validated sample API and bounded queries. Use the store clock and transaction conventions.
- [ ] Test that every actual local, external, client-owned, and fixed-workflow generation produces one sample, including failures/cancellation, and that warm-up samples are labeled without inventing runs. Wire the recording at the existing completion boundaries; avoid duplicate recording.
- [ ] Test terminal outcome accounting exactly once: locally completed and escalated attempts remain distinct; runs that never attempted local execution do not lower the local success rate. Retain external-generation counts needed for expected-wait prediction. Do not scan complete event histories on estimate reads.
- [ ] Run targeted tests, full pytest, Ruff lint/format, and diff checks; independently review the diff; commit and open the timing PR on #159 with `Closes #129`, M8, and assignee devnullexile.

### Task 2: Placement prediction, deadlines, and idle probe — #130

**Files:** Create `src/relaymind/orchestrator/placement.py` and `tests/test_placement.py`; modify `local_loop.py`, `warmup.py`, API readiness/lifespan, launcher settings, trace validation/schema, example launch file, and the relevant docs/tests.

**Interfaces:** `Placement.predict(model, purpose, input_tokens)` consumes Task 1's typed statistics and returns a typed prediction with optional predicted duration and its evidence. The integration evaluates remaining stage time before calling the adapter and records effective policy, prediction, and deadline in the published trace fields. Use the existing placement table defaults: policy local_first, margin 2, probe_after_seconds 600. Freeze exact trace/readiness field shapes in a spec revision if 0.37 does not define them.

- [ ] Write AC21 failing tests with fixture timings and FakeClock: fast/slow samples, no samples, hard cap before adapter dispatch, deadline miss with preserved evidence, and local_only/local_first compatibility.
- [ ] Implement purpose-specific expected output with the purpose token cap only as fallback. Honor the prefix state of the latest applicable sample; keep missing predictions explicit.
- [ ] Apply hard cap, then deadline, then experimental expected-wait according to RM27. Preserve the selected skill and completion predicate on local_slow handoff. Never introduce an external routing generation.
- [ ] Test and implement an idle probe under the same admission/slot boundary as runs: only stale estimates, no active run, one bounded warm-up-shaped generation, sample kind probe, and cancellation at shutdown.
- [ ] Test and implement run.accepted, route.selected, model.started/finished and readiness fields, including the new budget labels. Preserve the old decisions under local_first and do not skip work under local_only.
- [ ] Verify and review the complete change, then open its PR on Task 1 with `Closes #130`, M8, assignee devnullexile.

### Task 3: Loopback HTTP selector and example evidence — #133

**Files:** Create `src/relaymind/selection/http.py`, `scripts/example_selector.py`, and `tests/test_http_selector.py`; modify selector settings/construction, launcher, trace/evaluation claim recording, benchmark selector integration, README, TESTING, CHANGELOG, and EVIDENCE.

**Interfaces:** A configured `{transport = "http", url = "http://127.0.0.1:PORT/..."}` item implements the existing Selector protocol. One POST carries the redacted SelectionRequest, one 200 JSON SelectionResult returns, and the existing chain applies its timeout and admission. Resolve and specify transport identity and selector-reported model-call fields before implementing any new published shape.

- [ ] Write failing AC22 tests using an ephemeral loopback server: configuration swap, outside-loopback rejection, redirect/proxy isolation, timeout, connection/non-200/JSON/schema failures, identity/mechanism rejection, redaction, and inadmissible candidate fallback without side effects.
- [ ] Implement bounded httpx transport with environment proxies disabled, redirects disabled, schema validation, and cancellation propagation. Preserve the distinction between gateway-measured calls and selector claims.
- [ ] Implement the standard-library keyword example server and exercise the actual script through the transport in tests.
- [ ] Add the README launch-file example linking to the published schemas and RM28. Label out-of-process runs separately in exports.
- [ ] Run the example selector on the frozen 87-case SkillsBench file with the existing benchmark runner, record source/code fingerprints and all results beside the existing built-in evidence, and state the keyword method's limitations.
- [ ] Verify, review, and open its PR on Task 2 with `Closes #133`, M8, assignee devnullexile.

### Task 4: Verified live answer statements — #125

**Files:** Extend `src/relaymind/skills/predicates.py`, managed generation/publication in `local_loop.py`, `src/relaymind/api/chat.py` and related API lifecycle, evaluation exports, `tests/test_completion_predicates.py`, and new `tests/test_gated_streaming.py`; update docs and evidence.

**Interfaces:** Split each existing completion predicate into per-statement validation and final completeness, preserving equivalence to the buffered predicate. A holdback gate consumes text deltas only from answer generations and publishes verified statements as answer.delta. Managed LiveAnswer consumes that publication without changing client-owned streaming.

- [ ] Prove predicate equivalence on existing cases before replacing any validation. Test both languages and punctuation/chunk boundaries.
- [ ] Write AC19 failures for an invalid first statement, invalid later statement, incomplete ending, successful completion, cancellation/disconnect, and early-header interactions. Pin no rejected bytes, no replacement, no [DONE] on late failure, and run.failed predicate_unmet.
- [ ] Implement external answer-only generation once pending work is empty, and gate local final passes and workflow synthesis. Preserve tool planning, ownership, token limits, and the buffered correction loop.
- [ ] Connect the managed stream queue to verified publications and ensure cancellation and terminal/error delivery cannot hang or publish after failure.
- [ ] Report live versus buffered success/failure in corpus exports and run the required labeled corpus comparison; retain all failures and make no default change without parity evidence.
- [ ] Verify old AC09/client-owned/buffered behavior, run the full suite and independent review, then open the PR on Task 3 with `Closes #125` only when code and required comparison are complete; otherwise keep the issue linked with explicit remaining evidence until Task 5 supplies it. Set M8 and assignee devnullexile.

### Task 5: M8 evaluation and default decision — #134

**Files:** Extend both evaluation runners, `src/relaymind/evaluation/`, metrics tests and runner tests; add reproducible timing sample manifests and evidence exports; update EVIDENCE, RELEASE, TESTING, CHANGELOG, and the specification decision.

**Interfaces:** Both runners accept `--timing-samples FILE` and seed every fresh store through Task 1's validated API before case execution, recording the file digest. Add placement labels/arguments through the existing validated launch configuration. Reports preserve machine configuration, policy, chain, answer_stream, and sample provenance separately.

- [ ] Test sample-file validation, digest export, independent per-case seeding, and unchanged baseline with no file. Missing counts remain unknown.
- [ ] Add prediction-error and wasted-local-time distributions, generations per completed run, selection P/R/J, and external calls/tokens per completed task from recorded traces. Preserve all failure denominators and separate normal/constrained, languages, and transport claims.
- [ ] Build seed files from recorded normal and constrained samples, with their originating revisions/runtime arguments and measured class. Check the exact pinned runtime and model before live execution.
- [ ] Run the complete corpus/latency matrix required by #134: normal and constrained M1, each local_first/device_aware/expected_wait, each model_router and example_match+model_router. Keep controls/budgets fixed and reported; include the Task 4 live/buffered comparison. Record every failed/timeout case rather than silently retrying or dropping it.
- [ ] Report each section 12.3 hypothesis as met or not met. Make the default decision in a new specification revision citing those measurements; retaining defaults is a valid evidence-based decision.
- [ ] Verify and independently review code, metrics, provenance, and evidence. Open the evaluation PR on Task 4 with `Closes #134` only when its recorded runs and decision meet the issue; assign M8/devnullexile and keep any unmet criterion explicit.

## Completion

- [ ] Confirm every new PR's base is the preceding open branch, its issue links and milestone/assignee are correct, and no attribution trailers were added.
- [ ] Resolve stack conflicts, verify final published CI heads, and report implemented behavior, deterministic checks, measured evidence, and any unmet target separately.
