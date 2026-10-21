# Architecture

This document maps the components defined in
[`SPECIFICATION.md`](SPECIFICATION.md) section 3 to the code layout. The
spec is the source of truth; this page only explains where things live.

## Component map

| Spec component | Package | Responsibility |
|----------------|---------|----------------|
| API layer | `src/relaymind/api/` | Validate requests; expose the chat contract and native run API; authenticate callers. `errors.py` holds the error contract: the stable codes, one HTTP status for each, and the mapping of orchestrator exceptions and failed runs (section 11). `auth.py` checks the application token. `chat_request.py` validates a chat request body against the supported subset (RM19, spec 7.1). `chat.py` holds the chat backends (managed and client-owned) and the compatible response bodies. `native.py` holds the native run API without a web framework: request parsing, the run view, the SSE event stream over stored events, and readiness (RM21, spec 7.4). `app.py` is the FastAPI app for `GET /v1/models`, `POST /v1/chat/completions`, the six native endpoints, `/healthz`, and `/readyz`. `server.py` binds it to loopback only. |
| Orchestrator | `src/relaymind/orchestrator/` | Own state transitions, budgets, routing, handoff, cancellation, and the final-answer owner. `handoff.py` holds the HandoffEnvelope schema and builder, the external request messages, the context measurement, and the single dispatch authority (RM13, RM14). `client_loop.py` runs the client-owned alias: it offers the model the caller's functions and returns the call to the caller, executing nothing (RM08). `recovery.py` fails unfinished runs as `interrupted` at startup and settles their pending tool operations (RM10, RM17). |
| Model adapter | `src/relaymind/adapters/` | Translate normalized messages, tools, and options into provider-specific requests and events. |
| Skill registry | `src/relaymind/skills/` | Validate packages; expose descriptions; load selected instructions and approved resources; check answers with the registered completion predicates (`predicates.py`). |
| Tool registry and executor | `src/relaymind/tools/` | Validate arguments and permissions; execute registered functions; record outcomes. `sandbox.py` holds the sandbox demo data service and the seven demo tools (spec section 9). |
| Run store | `src/relaymind/store/` | Persist ordered events, tool outcomes, pending approvals, and versioned state in SQLite. |
| Demo UI | `src/relaymind/ui/` | Display model decisions and execution evidence; collect clarification and confirmation. |
| Redaction | `src/relaymind/redaction.py` | Redact configured secret fields before events and tool outcomes are persisted (RM12, section 8). |
| Canonical JSON | `src/relaymind/canonical.py` | One canonical JSON text form for tool schema hashes (RM07), operation IDs (RM10), and approval binding (RM11). |
| Schema boundary checks | `src/relaymind/schema_util.py` | Shared checks for published, versioned JSON Schemas (closed field sets, exact schema_version, first schema error; spec section 8). |
| Timing instrumentation | `src/relaymind/stats.py` | Record spec 12.1 metrics per generation around any adapter; export JSON for adapter comparisons (M1 working data, not benchmark evidence). |

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

Lower layers must not import higher ones. The store must not depend on
adapters. Adapters must not depend on SQLite.
