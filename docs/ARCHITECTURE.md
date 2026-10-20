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
| Skill registry | `src/relaymind/skills/` | Validate packages; expose descriptions; load selected instructions and approved resources. |
| Tool registry and executor | `src/relaymind/tools/` | Validate arguments and permissions; execute registered functions; record outcomes. |
| Run store | `src/relaymind/store/` | Persist ordered events, tool outcomes, pending approvals, and versioned state in SQLite. |
| Demo UI | `src/relaymind/ui/` | Display model decisions and execution evidence; collect clarification and confirmation. |

Demo skill packages (SKILL.md format, spec section 9) live in `skills/` at
the repository root, one directory per skill.

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
          │  │  └─► tools
          │  └────► skills
          └───────► store
```

Lower layers must not import higher ones. The store must not depend on
adapters. Adapters must not depend on SQLite.
