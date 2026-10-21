# Architecture

This document maps the components defined in
[`SPECIFICATION.md`](SPECIFICATION.md) section 3 to the code layout. The
spec is the source of truth; this page only explains where things live.

## Component map

| Spec component | Package | Responsibility |
|----------------|---------|----------------|
| API layer | `src/relaymind/api/` | Validate requests; expose the chat contract and native run API; authenticate callers. |
| Orchestrator | `src/relaymind/orchestrator/` | Own state transitions, budgets, routing, handoff, cancellation, and the final-answer owner. |
| Model adapter | `src/relaymind/adapters/` | Translate normalized messages, tools, and options into provider-specific requests and events. |
| Skill registry | `src/relaymind/skills/` | Validate packages; expose descriptions; load selected instructions and approved resources; check answers with the registered completion predicates (`predicates.py`). |
| Tool registry and executor | `src/relaymind/tools/` | Validate arguments and permissions; execute registered functions; record outcomes. `sandbox.py` holds the sandbox demo data service and the seven demo tools (spec section 9). |
| Run store | `src/relaymind/store/` | Persist ordered events, tool outcomes, pending approvals, and versioned state in SQLite. |
| Demo UI | `src/relaymind/ui/` | Display model decisions and execution evidence; collect clarification and confirmation. |
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
api ──► orchestrator ──► adapters
          │  │  │
          │  │  └─► tools ──► store
          │  └────► skills ──► tools
          └───────► store
```

The skill registry imports the tool registry (a manifest's tools are
checked against it at load, and completion predicates read tool
results), and the tools import
the store only for `TraceClock`. `canonical.py` and `schema_util.py` are
leaf modules that import no other RelayMind package: the store and the
tools use `canonical.py`; the orchestrator, skills, store, and tools use
`schema_util.py`.

Lower layers must not import higher ones. The store must not depend on
adapters. Adapters must not depend on SQLite.
