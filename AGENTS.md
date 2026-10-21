# AGENTS.md — Contributor guide for RelayMind

This file is for AI agents and human contributors working on the RelayMind
codebase.

## The two rules

1. **The spec is the single source of truth.**
   [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) defines what RelayMind
   does. Every implementation task and every acceptance test must reference
   requirement IDs from it (RM01–RM25, AC01–AC16). If code and spec disagree,
   the spec wins. If the spec is wrong, revise the spec first, then change
   the code.
2. **Tests come before code.** Write a failing test that captures the
   requirement, watch it fail, then implement until it passes. Do not merge
   implementation code without tests.

## Repository map

| Path | Purpose |
|------|---------|
| `src/relaymind/api/` | API layer: request validation, chat contract, native run API, idempotency keys (`idempotency.py`; RM17, RM21), auth (RM19–RM21); the error contract (`errors.py`: stable codes, one status each, failed-run mapping; section 11) the bearer token check (`auth.py`), chat request validation (`chat_request.py`), the chat backends and response bodies (`chat.py`), the FastAPI app (`app.py`), and the loopback-only server (`server.py`; RM19, section 7.1) |
| `src/relaymind/orchestrator/` | State transitions, budgets, routing, handoff, cancellation, final-answer ownership (RM01–RM05, RM13–RM17); the RouteDecision schema and validator (`route_decision.py`, `schemas/route_decision.v1.json`; RM02); the router (`router.py`; native function calling; RM01, RM02), the bounded local loop (`local_loop.py`), budgets (`budgets.py`; RM03, RM04, section 11), identifier provenance (`identifiers.py`; RM05), the client-owned alias run (`client_loop.py`; RM08, RM19), and the handoff envelope, dispatch authority, and external request (`handoff.py`, `schemas/handoff_envelope.v1.json`; RM13, RM14), and restart recovery (`recovery.py`; RM10, RM17, section 5) |
| `src/relaymind/adapters/` | Model adapters: normalized messages and events per provider (RM18) |
| `src/relaymind/skills/` | Skill registry: package validation, instruction loading (RM06, RM07); completion predicates (`predicates.py`; RM06, section 9) |
| `src/relaymind/tools/` | Tool registry and executor: argument validation, permissions, side effects (RM08–RM12); the sandbox demo data service and the seven demo tools (`sandbox.py`, `schemas/demo/`; RM23, section 9); the executor (`executor.py`: operation IDs, replay, retry, timeouts; RM10, RM14) |
| `src/relaymind/store/` | Run store: SQLite events, tool outcomes, approvals, versioned state (RM17, RM22); every event is validated against the published TraceEvent schema before it is written (`trace_event.py`, `schemas/trace_event.v1.json`; RM22, section 8) |
| `src/relaymind/canonical.py` | One canonical JSON text form for tool schema hashes (RM07), operation IDs (RM10), and approval binding (RM11) |
| `src/relaymind/schema_util.py` | Shared boundary checks for published, versioned JSON Schemas: closed field sets, exact schema_version, first schema error (spec section 8) |
| `src/relaymind/redaction.py` | Configured secret-field redaction before anything is persisted (RM12, section 8) |
| `src/relaymind/stats.py` | Per-generation timing metrics and JSON export for adapter comparisons (RM25, section 12.1) |
| `src/relaymind/ui/` | Demo UI: decisions and execution evidence (RM23) |
| `models/` | Pinned model artifact manifests (RM18, spec section 6); GGUF files are downloaded per `models/README.md`, never checked in |
| `skills/` | Demo skill packages, one directory per skill, SKILL.md plus `skill.manifest.json`; the default skill root (operators may configure others, spec section 4.2) |
| `tests/` | Test suite; `tests/fixtures/` holds sandbox demo data |
| `scripts/` | Developer scripts: model manifest generation (RM18), evaluation and benchmarks (RM25); `scripts/README.md` lists what exists |
| `docs/` | Spec, architecture, roadmap, testing guide, live evidence record (`docs/EVIDENCE.md`, raw exports in `docs/evidence/`) |

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
