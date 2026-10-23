# Architecture

This document maps the components defined in
[`SPECIFICATION.md`](SPECIFICATION.md) section 3 to the code layout. The
spec is the source of truth; this page only explains where things live.

## Component map

| Spec component | Package | Responsibility |
|----------------|---------|----------------|
| API layer | `src/relaymind/api/` | Validate requests; expose the chat contract and native run API; authenticate callers. `errors.py` holds the error contract: the stable codes, one HTTP status for each, and the mapping of orchestrator exceptions and failed runs (section 11). `auth.py` checks the application token. `chat_request.py` validates a chat request body against the supported subset (RM19, spec 7.1). `chat.py` holds the chat backends (managed and client-owned) and the compatible response bodies. `native.py` holds the native run API without a web framework: request parsing, the run view, the SSE event stream over stored events, and readiness (RM21, spec 7.4). `app.py` is the FastAPI app for `GET /v1/models`, `POST /v1/chat/completions`, the six native endpoints, `/healthz`, and `/readyz`. `server.py` binds it to loopback only. |
| Orchestrator | `src/relaymind/orchestrator/` | Own state transitions, budgets, routing, handoff, cancellation, and the final-answer owner. `handoff.py` holds the HandoffEnvelope schema and builder, the external request messages, the context measurement, and the single dispatch authority (RM13, RM14). `client_loop.py` runs the client-owned alias: it offers the model the caller's functions and returns the call to the caller, executing nothing (RM08). `recovery.py` fails unfinished runs as `interrupted` at startup and settles their pending tool operations (RM10, RM17). `local_loop.py` builds the skill-stage conversation in the pure function `skill_stage_messages`, so that the timing probes send exactly what the gateway sends. The parallel workflow (RM15, spec 0.25) lives in `local_loop.py`; `workflows.py` lists the registered workflow IDs. `routing_cache.py` holds the optional routing-result cache: the exact key, the five-minute TTL, the 256-entry bound, and invalidation when a skill or the caller's permissions change; it stores only the skill choice of a local read-only route, and `local_loop.py` uses it, so a hit still authorizes and runs tools fresh (RM24, spec 0.26). |
| Model adapter | `src/relaymind/adapters/` | Translate normalized messages, tools, and options into provider-specific requests and events. A `Usage` event holds the cached and reasoning token counts the runtime or provider returned, and `None` for a count it did not return (RM16, spec 0.31 section 6). The shared stream pump (`openai_compat.py`) also sends the internal `FirstToken` marker when the first generated fragment arrives, which the orchestrator stamps with the run clock and never forwards; the llama.cpp adapter sends `cache_prompt`, and the external adapter sends the operator's `request_options` (`chat_completions.py`) and declares them for the trace; `model.started` records them only when at least one is declared (spec 0.33 section 8). |
| Skill registry | `src/relaymind/skills/` | Validate packages; expose descriptions; load selected instructions and approved resources; check answers with the registered completion predicates (`predicates.py`). `cache.py` caches parsed skill files and resource text by content hash (RM24, spec 0.26). |
| Tool registry and executor | `src/relaymind/tools/` | Validate arguments and permissions; execute registered functions; record outcomes. `sandbox.py` holds the sandbox demo data service and the seven demo tools (spec section 9). |
| Run store | `src/relaymind/store/` | Persist ordered events, tool outcomes, pending approvals, and versioned state in SQLite. |
| Demo UI | `src/relaymind/ui/` | Display model decisions and execution evidence; collect clarification and confirmation; compare modes. `trace_view.py` builds the evidence of one run from its stored events only; `render.py` renders it as escaped HTML with the persistent data mode label; `routes.py` serves the static page and the rendered traces; `comparison.py` runs auto and external_only on reset fixtures; `settings.py` holds the operator's declared data mode (live, replay, or double). The trace also shows the answer options recorded in run.accepted (spec 0.22). The page is a thin client of the native run API (RM23, spec 0.20 section 9). |
| Launcher | `src/relaymind/launcher.py` | The `relaymind serve --config` command: reads the TOML file, checks it all before any socket opens, wires models, skills, sandbox, store, settings, and policy, runs restart recovery once, and prints the application token once (spec 0.21 section 14.1). The `[budgets]` table maps its keys, the `LocalBudgets` field names, onto the orchestrator's budgets, and every run records the values in force in `run.accepted` (RM03, spec 0.31); `[external_model.request_options]` is checked before startup (no credential, header, gateway-owned field, or sampling key, no blank key at any depth, JSON values only). The read timeout of each real adapter is at least the budget in force (`active_seconds` for the local model, `external_seconds` for the external one) and never under 30 seconds, so a raised limit is not cut off by a shorter unrecorded one. |
| Redaction | `src/relaymind/redaction.py` | Redact configured secret fields before events and tool outcomes are persisted (RM12, section 8). |
| Canonical JSON | `src/relaymind/canonical.py` | One canonical JSON text form for tool schema hashes (RM07), operation IDs (RM10), and approval binding (RM11). |
| Schema boundary checks | `src/relaymind/schema_util.py` | Shared checks for published, versioned JSON Schemas (closed field sets, exact schema_version, first schema error; spec section 8). |
| Timing instrumentation | `src/relaymind/stats.py` | Record spec 12.1 metrics per generation around any adapter; export JSON for adapter comparisons (M1 working data, not benchmark evidence). |

## Phase 2 components (spec 0.31, planned)

Specification 0.31 adds components that do not exist in the code yet;
[`ROADMAP.md`](ROADMAP.md) M7 to M9 build them. Where they will live,
so that the dependency direction below stays true:

| Spec component | Planned package | Responsibility |
|----------------|-----------------|----------------|
| Selector (RM28) | `src/relaymind/selection/` | The SelectionRequest and SelectionResult schemas, the chain, the built-in `model_router` (the routing generation now in `orchestrator/router.py`) and `example_match`, and the loopback HTTP transport. Sits below the orchestrator and above skills; it executes nothing. |
| Placement policy (RM27) | `src/relaymind/orchestrator/placement.py` | Predicts where a generation runs from the timing samples the store keeps; never changes a budget. |
| Timing samples (RM27) | `src/relaymind/store/` | A table of per-generation and warm-up samples, read by placement, written by the orchestrator. The figures it will hold (first-token time, cached and reasoning tokens) are already recorded in every `model.finished` event (#120). |
| Prefetch (RM29) | `src/relaymind/skills/` and `src/relaymind/orchestrator/` | Manifest declarations validated at load; executed by the orchestrator through the executor before the first skill generation. |
| Workflow registry and runner (RM30) | `src/relaymind/workflows/` | Loads and validates `workflow.yaml` packages from the configured roots; runs tool, model, and render steps. The fixed parallel workflow stays in `orchestrator/local_loop.py` until a runner reproduces it. |
| Answer pass, gated streaming, early headers (RM31, RM32) | `src/relaymind/orchestrator/local_loop.py`, `src/relaymind/api/chat.py` | The draft pass, the per-statement holdback of the live answer stream, and early response headers for streamed chat requests. |

Demo workflow packages (`workflow.yaml` plus optional `examples.json`)
will live in `workflows/` at the repository root, the default workflow
root, with the same root rules as skills.

Demo skill packages (SKILL.md plus `skill.manifest.json`, spec sections
4.2 and 9) live in `skills/` at the repository root, one directory per
skill; that directory is the default skill root, and operators can
configure others. Pinned model artifact
manifests (RM18, spec section 6) live in `models/`; the GGUF files they
describe are downloaded per `models/README.md`, never checked in.

## Runtime shape

A single-user local service:

```
client ──► FastAPI (api) ──► orchestrator ──► adapters ──► llama.cpp server (local)
                                │                      └─► external provider (configured)
                                ├─► skills (registry)
                                ├─► tools (executor) ──► sandbox demo data service
                                └─► store (SQLite)
```

- The gateway binds to loopback and requires a generated application token.
- The operator starts the gateway with `relaymind serve --config <file>`
  (the launcher above, spec section 14.1). One TOML file wires the
  models, skills, sandbox, store, settings, policy, and UI data mode.
- The demo UI is served by the same FastAPI app under `/ui` when the
  operator passes `UiSettings`.
- Local inference runs in a separate llama.cpp server process, also on
  loopback.
- External credentials stay server-side (environment variables or OS
  credential store).

## Ownership boundaries

- The model produces **proposals**.
- The orchestrator decides whether a proposal is **admissible**.
- The executor owns **side effects**.
- The UI renders **evidence**.

No package may cross these lines. In particular: adapters never execute
tools, skills never execute anything, and the store never makes routing
decisions.

## Dependency direction

```
api ──► orchestrator ──► skills ──► tools ──► store
          │  │  │                    │
          │  │  │                    └─► adapters (ToolSchema)
          │  │  ├─► tools
          │  │  └─► store
          │  └────► adapters
          └───────► redaction
```

The layer order, from top to bottom, is: api, orchestrator, skills,
tools, then store and adapters side by side, then the leaf modules.
A module imports only from layers below it.

- The skill registry imports the tool registry: a manifest's tools are
  checked against it at load, and completion predicates read tool
  results.
- The tools import the store (`TraceClock`, and `RunStore` in the
  executor), `ToolSchema` from the adapters, and `redaction.py` in the
  executor.
- The orchestrator imports all of these, and `redaction.py` for the
  events it writes.
- `canonical.py`, `schema_util.py`, and `redaction.py` are leaf modules
  that import no other RelayMind package. The store and the tools use
  `canonical.py`; the orchestrator, skills, store, and tools use
  `schema_util.py`; the orchestrator and the tool executor use
  `redaction.py`.

The demo UI sits beside the API layer. `api/app.py` installs the UI
routes, and `ui/routes.py` reuses the API's token check, error contract,
and native request parsing. `ui/trace_view.py` builds views from
stored events, and `ui/render.py` renders them. `ui/comparison.py`
also imports `Message` from the adapters; it starts runs and requests
cancellation through the orchestrator, reads run state from the store,
and resets the configured sandbox fixtures before each comparison run.
A chat disconnect asks the orchestrator to cancel the run the request
owns (`request_cancel`), the same path as the native cancel (section 7.4).

Lower layers must not import higher ones. The store must not depend on
adapters. Adapters must not depend on SQLite.
