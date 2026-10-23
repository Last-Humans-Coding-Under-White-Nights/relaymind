# Architecture

This document maps the components defined in
[`SPECIFICATION.md`](SPECIFICATION.md) section 3 to the code layout. The
spec is the source of truth; this page only explains where things live.

## Component map

| Spec component | Package | Responsibility |
|----------------|---------|----------------|
| API layer | `src/relaymind/api/` | Validate requests; expose the chat contract and native run API; authenticate callers. `errors.py` holds the error contract: the stable codes, one HTTP status for each, and the mapping of orchestrator exceptions and failed runs (section 11). `auth.py` checks the application token. `chat_request.py` validates a chat request body against the supported subset (RM19, spec 7.1). `chat.py` holds the chat backends (managed and client-owned) and the compatible response bodies. `native.py` holds the native run API without a web framework: request parsing, the run view, the SSE event stream over stored events, and readiness (RM21, spec 7.4). `app.py` is the FastAPI app for `GET /v1/models`, `POST /v1/chat/completions`, the six native endpoints, `/healthz`, and `/readyz`. `server.py` binds it to loopback only. Under `performance.early_headers`, `app.py` accepts a streamed chat run first (every check and the 429 slot still come before a header), returns the headers `X-RelayMind-Run-ID` and `X-RelayMind-Events-URL` at once, and `chat.py` (`early_stream`) sends SSE comments until the run's task finishes, then the buffered chunks or the mid-stream error event; the response owns the task from its creation, so a disconnect cancels the run through `request_cancel` and waits for it to stop up to `cancel_wait_seconds`, as the buffered disconnect does (RM14, RM32, spec 0.32 sections 7.3 and 7.4). Both aliases use it. |
| Orchestrator | `src/relaymind/orchestrator/` | Own state transitions, budgets, routing, handoff, cancellation, and the final-answer owner. `handoff.py` holds the HandoffEnvelope schema and builder, the external request messages (the rules, the skill instructions, the fixed completion criteria, the catalog, the answer instruction, and last the handoff JSON, so the constant part keeps the request prefix; spec 0.35), the context measurement, and the single dispatch authority (RM13, RM14). `local_loop.py` adds one gateway rules message with the criteria values of the latest results at the end of the conversation before each external generation (replacing its earlier copy, so a request never carries two), measures the request with it, and corrects a rejected external answer with the failed checks (`skills/predicates.py` holds the criteria beside each predicate; RM12, section 4.4). `warmup.py` builds the routing-shaped and skill-shaped warm-up requests with the gateway's own builders and times them (RM27, section 6); the launcher runs it after recovery. `client_loop.py` runs the client-owned alias: it offers the model the caller's functions and returns the call to the caller, executing nothing (RM08). `recovery.py` fails unfinished runs as `interrupted` at startup and settles their pending tool operations (RM10, RM17). `local_loop.py` builds the skill-stage conversation in the pure function `skill_stage_messages`, so that the timing probes send exactly what the gateway sends. The parallel workflow (RM15, spec 0.25) lives in `local_loop.py`; `workflows.py` lists the registered workflow IDs. `routing_cache.py` holds the optional routing-result cache: the exact key, the five-minute TTL, the 256-entry bound, and invalidation when a skill or the caller's permissions change; it stores only the skill choice of a local read-only route, and `local_loop.py` uses it, so a hit still authorizes and runs tools fresh (RM24, spec 0.26). `performance.py` holds `PerformanceSettings` (answer pass, calls per generation, prefetch, answer stream, early headers; every default is the baseline) and the draft guards of the answer pass (a non-default answer option, the run language, and tool-call JSON, RM31); `local_loop.py` publishes a checked draft under `answer_pass = draft` and `accept` takes the slot and writes `run.accepted` before any work starts (RM32). Under `calls_per_generation = many`, `local_loop.py` admits a generation's read-only calls as a set: it validates each, rejects the whole generation before anything runs when the set is over the limit, mixes a mutating call with others, has an invalid call, or needs more new executions than the tool budget has room for (`BudgetMeter.tool_room`, `admitted_calls_limit`), and then runs the calls in order, each with its own events and tool message (section 4.3, AC18). The rejection answers every call with what its `tool.proposed` records (`_Turn.call_rejections`): the call that failed its own checks with its error and the others as `not_executed`, or, for a rejection about the set, every call with that rejection (spec 0.34 section 4.3). The client-owned alias and the parallel workflow's `local_health` branch keep the one-call rule under `many`. `prefetch.py` resolves a skill's declared prefetch calls from the user turns by the RM05 rule and checks the declarations against the configured tool limits; `local_loop.py` runs the resolved calls together before the first skill generation as gateway-made proposals (actor `gateway`, mechanism `prefetch`, model null; RM29), in the external stage too when that stage loaded the skill (spec 0.37). `local_status.py` holds the class of the local model (`LocalStatus`: ready, unavailable, unsuitable, absent) and the modes each class forbids (RM26); `LocalOrchestrator` takes no local model when none is configured, keeps the class (startup detection sets it, readiness updates it with `placement.detect`), and in auto without a usable local model runs the selector chain without model-based selectors and hands off with the skill loaded, recording reason `no_local_model` (managed external execution, never degraded mode). `warmup.py` also detects the class at startup: the runtime must answer with the model loaded within `warmup_seconds`, and the routing-shaped warm-up must fit `active_seconds`. |
| Selector (RM28) | `src/relaymind/selection/` | Skill selection behind one published interface (spec 0.31, 0.36). `contract.py` holds the SelectionRequest and SelectionResult, published as `schemas/selection_request.v1.json` and `schemas/selection_result.v1.json` and checked at the boundary in both directions, the `Selector` protocol, and the context a model-based selector gets (the run's own messages and the routing generation callback). `catalog.py` builds the request's catalog, with the example requests of each skill's `examples.json` read through the registry. `chain.py` tries the configured selectors in order, gives a selector that is not a model `selection.timeout_seconds` through an injectable sleep, records every attempt (selected, none, timeout, error, inadmissible), and lets a failure of the gateway's own generation through unchanged. `admission.py` is the gateway's step: catalog membership, the route from the registry and the mode (`Router.skill_routing`), the RM05 identifier check and its clarification, and the narrowing intersected with the allowlist. `model_router.py` wraps `orchestrator/router.py` and runs its generation through the callback, so in a run it is the run's `_generate` and the routing request, cache key, and events are what they were; `example_match.py` matches the request against the offered skills' descriptions and example requests (BM25 over masked identifiers, word tokens and Chinese bigrams, the score divided by the query's summed idf and capped at 1). `settings.py` is the `[selection]` table. `local_loop.py` runs the chain after the capability check, the routing cache lookup, and the oversized-input bypass, and records `route.selected` with the `selector` and `tools_narrowed`; it executes nothing and grants nothing. |
| Model adapter | `src/relaymind/adapters/` | Translate normalized messages, tools, and options into provider-specific requests and events. A `Usage` event holds the cached and reasoning token counts the runtime or provider returned, and `None` for a count it did not return (RM16, spec 0.31 section 6). The shared stream pump (`openai_compat.py`) also sends the internal `FirstToken` marker when the first generated fragment arrives, which the orchestrator stamps with the run clock and never forwards; the llama.cpp adapter sends `cache_prompt`, and the external adapter sends the operator's `request_options` (`chat_completions.py`) and declares them for the trace; `model.started` records them only when at least one is declared (spec 0.33 section 8). |
| Skill registry | `src/relaymind/skills/` | Validate packages; expose descriptions; load selected instructions and approved resources; check answers with the registered completion predicates (`predicates.py`). `cache.py` caches parsed skill files and resource text by content hash (RM24, spec 0.26). `registry.py` also validates a manifest's `prefetch` declarations at load, without a request (RM29): read-only tools from the allowlist, identifiers bound to the request, constants only for non-identifiers, checked against the full tool schema with every allowed value of the request bindings (spec 0.34). |
| Tool registry and executor | `src/relaymind/tools/` | Validate arguments and permissions; execute registered functions; record outcomes. `sandbox.py` holds the sandbox demo data service and the seven demo tools (spec section 9). |
| Run store | `src/relaymind/store/` | Persist ordered events, tool outcomes, pending approvals, and versioned state in SQLite. |
| Demo UI | `src/relaymind/ui/` | Display model decisions and execution evidence; collect clarification and confirmation; compare modes. `trace_view.py` builds the evidence of one run from its stored events only; `render.py` renders it as escaped HTML with the persistent data mode label; `routes.py` serves the static page and the rendered traces; `comparison.py` runs auto and external_only on reset fixtures; `settings.py` holds the operator's declared data mode (live, replay, or double). The trace also shows the answer options recorded in run.accepted (spec 0.22) and the mechanism of a proposal, so a prefetch call is shown as the gateway's. The page is a thin client of the native run API (RM23, spec 0.20 section 9). |
| Launcher | `src/relaymind/launcher.py` | The `relaymind serve --config` command: reads the TOML file, checks it all before any socket opens, wires models, skills, sandbox, store, settings, and policy, runs restart recovery once, warms the local model up (`[placement]` `warmup`, `warmup_seconds`; a separate step in `main`, on a short-lived adapter, after `build_app`; a runtime that does not answer is one printed line), and prints the application token once (spec 0.21 section 14.1). The `[budgets]` table maps its keys, the `LocalBudgets` field names, onto the orchestrator's budgets, and every run records the values in force in `run.accepted` (RM03, spec 0.31); `[external_model.request_options]` is checked before startup (no credential, header, gateway-owned field, or sampling key, no blank key at any depth, JSON values only). The read timeout of each real adapter is at least the budget in force (`active_seconds` for the local model, `external_seconds` for the external one) and never under 30 seconds, so a raised limit is not cut off by a shorter unrecorded one. |
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
| Selector transport (RM28) | `src/relaymind/selection/` | The loopback HTTP transport for an out-of-process selector (#133). The interface, the chain, and the two built-in selectors exist; see the component map and the dependency direction below. It executes nothing. |
| Placement policy (RM27) | `src/relaymind/orchestrator/placement.py` | Predicts where a generation runs from the timing samples the store keeps; never changes a budget. |
| Timing samples (RM27) | `src/relaymind/store/` | A table of per-generation and warm-up samples, read by placement, written by the orchestrator. The figures it will hold (first-token time, cached and reasoning tokens) are already recorded in every `model.finished` event (#120). |
| Workflow registry and runner (RM30) | `src/relaymind/workflows/` | Loads and validates `workflow.yaml` packages from the configured roots; runs tool, model, and render steps. The fixed parallel workflow stays in `orchestrator/local_loop.py` until a runner reproduces it. |
| Gated answer streaming (RM32) | `src/relaymind/orchestrator/local_loop.py`, `src/relaymind/api/chat.py` | The per-statement holdback of the live answer stream. Not implemented: `performance.answer_stream = live` stops startup. The draft answer pass (RM31, #121) and early response headers (RM32, #140) are implemented; see the orchestrator and API rows above. |

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
- The selection package (RM28) sits beside the orchestrator's routing
  modules rather than below the whole orchestrator: `selection/` imports
  `orchestrator/router.py`, `route_decision.py`, and `identifiers.py` (the
  router that `model_router` wraps and admission reuses, and the identifier
  rule `example_match` masks with), the skills (the catalog), the tools
  (`example_match` reads the tool schemas), the adapters, and
  `schema_util.py`; `orchestrator/local_loop.py` and `routing_cache.py`
  import `selection/`. None of the modules `selection/` imports imports it
  back.
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
