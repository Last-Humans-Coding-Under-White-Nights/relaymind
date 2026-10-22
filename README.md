# RelayMind

**RelayMind** is a local AI gateway. A small local model handles simple, bounded
tasks with skills and tools. Complex work is transferred to an external model.
Every routing decision, tool call, and model call is visible in a trace.

Status: **M1 to M4 are closed.** M1 (adapter and protocol
qualification) and M2 (local skill-and-tool loop) have live runs
recorded in [`docs/EVIDENCE.md`](docs/EVIDENCE.md) for Apple Silicon
macOS and native x86-64 Windows. M3 (handoff and confirmation) and M4
(public API and observability) are implemented and covered by
deterministic tests with model doubles only: no live run is recorded
for their acceptance scenarios, and AC08 stays open until a second
local model artifact is qualified. The operator starts the gateway with
one command, `relaymind serve --config <file>` (see
[Start the gateway](#start-the-gateway)). Chinese support is best-effort
(spec 0.7): Chinese live results are recorded but are not a gate. M5
(parallel workflow and caching) is implemented with model doubles; M6
(evaluation) is next. See
[`docs/ROADMAP.md`](docs/ROADMAP.md) for the milestone plan and the
open items, and [`CHANGELOG.md`](CHANGELOG.md) for what has landed so
far.

## How this project works

- **SDD (Spec-Driven Development).** The specification in
  [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) is the single source of
  truth. Every task and every acceptance test must reference requirement IDs
  from it (RM01, RM02, ...). Behavior changes require a spec revision first.
- **TDD (Test-Driven Development).** Write a failing test before writing
  implementation code. No feature is merged without tests.

See [`AGENTS.md`](AGENTS.md) for the full contributor workflow.

## What the prototype does

- Accepts requests through an OpenAI-compatible API (`/v1/models`,
  `/v1/chat/completions`) and a native run API (`/relay/v1/runs`, with
  SSE events, resume, and cancel).
- Lets a small local model (initial candidate: Qwen3-0.6B via llama.cpp)
  select a skill, call registered tools, and finish simple tasks locally
  with zero external calls.
- Escalates complex tasks to an external model, handing over the original
  request, tool evidence, and remaining budgets.
- Asks for a fresh, single-use confirmation before the mutating demo
  action runs.
- Shows every decision in a demo trace UI (`/ui`): selected skill, tool
  arguments, execution results, escalation reason, final answer, and
  timing.
- Works in English, with best-effort Chinese, on macOS and Windows.

- Runs one bounded parallel workflow, `health-and-diagnostic-checklist`,
  in `parallel_demo` mode: the local model reads service health while the
  external model drafts a diagnostic checklist, then a new external call
  combines the two. The trace and the UI show each branch and the
  synthesis call as separate actors, with the extra call latency.

- Caches skill parsing by content hash, and can cache the routing choice of
  a repeated local read-only request. The routing cache is off by default
  and in the benchmark configuration; the operator turns it on with
  `[cache] routing = true` in the launcher file. A cached route skips only
  the routing generation: authorization and tools still run fresh, a skill
  or permission change drops the entries, and the trace records `cache.hit`
  and labels the run.

Not built yet: the evaluation corpus and latency benchmark (M6). The
parallel workflow and the caches are covered by deterministic tests with
model doubles only; no live run is recorded for them, and no cache speedup
is claimed.

## Repository layout

| Path | Purpose |
|------|---------|
| `docs/SPECIFICATION.md` | The spec. Single source of truth. |
| `docs/ARCHITECTURE.md` | How components map to the code layout. |
| `docs/ROADMAP.md` | Implementation milestones with requirement IDs. |
| `src/relaymind/` | Gateway source (Python): the FastAPI API layer (`api/`), orchestrator, adapters, skill and tool registries, run store, and demo UI (`ui/`). |
| `models/` | Pinned model artifact manifests; the GGUF files are downloaded, not checked in. |
| `skills/` | Demo skill packages (SKILL.md plus skill.manifest.json). |
| `tests/` | Test suite and sandbox fixtures. |
| `scripts/` | Developer scripts; see `scripts/README.md` for what each one does. |

## Development setup

Requires Python 3.12+.

```bash
git clone <repo-url> && cd relaymind
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run checks:

```bash
pytest           # deterministic test suite (model doubles, runs in CI)
ruff check .     # lint
ruff format --check .
```

Live model tests are separate: `pytest -m live` runs them against a
local llama-server with the pinned model artifact (install llama.cpp
and see `models/README.md` for fetching and verifying the model).
[`docs/TESTING.md`](docs/TESTING.md) covers manual testing from a
terminal on macOS and Windows.

## Start the gateway

### 1. Start the local runtime

Start the pinned runtime first (install it as described in
[`models/README.md`](models/README.md)):

```bash
llama-server -m models/Qwen3-0.6B-Q8_0.gguf \
  --host 127.0.0.1 --port 8033 --ctx-size 8192 \
  --jinja --reasoning off --reasoning-format deepseek
```

### 2. Start the gateway

One command starts the gateway on macOS and Windows (PowerShell or
Terminal). It needs a config file; copy
[`relaymind.example.toml`](relaymind.example.toml) and edit it:

```bash
relaymind serve --config relaymind.toml
```

The example wires the local model, the checked-in demo skills, the
sandbox demo data, a SQLite run store, and the demo UI into the gateway.
The command checks the whole configuration before it opens a socket. It
refuses a public bind address, a model file that does not match its
manifest, an invalid skill package, and a `[ui]` table without a declared
data mode, and it prints one explicit error (exit status 2). Then it ends
any run an earlier process left unfinished as `interrupted` (spec section
5), starts serving on loopback, and prints the application token once:

```
Serving on http://127.0.0.1:8080
Application token: <token>
```

Keep the token in `RELAYMIND_TOKEN` for the requests below. It is not
written to a log or a file. The external model is optional: without an
`[external_model]` table, a request that needs one ends with
`needs_external`. Its credential is never in the config file or on the
command line: the file names an environment variable (`api_key_env`),
and you export it in the shell that starts the gateway. The
configuration format is spec section 14.1.

The example config declares the demo UI with the `live` data mode, so
the UI is at `http://127.0.0.1:8080/ui`; enter the application token
there to create runs and view traces. The `LIVE` label describes
decisions from the local model; the tool data is the sandbox fixture
(spec section 9). Each mutation of the sandbox still requires a fresh
confirmation (RM11).

### 3. Talk to the gateway

The gateway speaks the OpenAI Chat Completions shape, so any OpenAI
client works once pointed at `http://127.0.0.1:8080/v1`. Every request
carries the application token. You do not pick a model: the alias
`relaymind` means "route it for me". The local model selects a skill
and proposes the tool call; the gateway runs it and escalates to the
external model only if the local run cannot finish.

```bash
curl -i -s http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $RELAYMIND_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model": "relaymind",
       "messages": [{"role": "user", "content": "Is staging-api healthy right now?"}]}'
```

To force where inference runs, add one header with `auto`,
`local_only`, `external_only`, or `parallel_demo`. Operator policy
decides which values a caller may use; the trace always shows which
model answered. `local_only` answers with HTTP 422 and code
`needs_external` when the local model cannot finish. `parallel_demo`
works only with the `relaymind` alias and only when operator
configuration enables the workflow (`enabled_workflows =
["health-and-diagnostic-checklist"]` in the `[policy]` table) and an
external model is configured. It then runs the parallel workflow (spec
section 4.4): a local health branch and an external checklist branch,
joined by a new external call that writes the answer. Without the
workflow or the external endpoint it fails with `workflow_unavailable`,
and `/readyz` does not report the mode ready.

```bash
curl -i -s http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $RELAYMIND_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-RelayMind-Execution-Mode: local_only" \
  -d '{"model": "relaymind",
       "messages": [{"role": "user", "content": "Is staging-api healthy right now?"}]}'
```

The response header `X-RelayMind-Run-ID` names the run. The native run
API shows it and its trace:

```bash
curl -s http://127.0.0.1:8080/relay/v1/runs/<run id> \
  -H "Authorization: Bearer $RELAYMIND_TOKEN"
curl -s http://127.0.0.1:8080/relay/v1/runs/<run id>/events \
  -H "Authorization: Bearer $RELAYMIND_TOKEN"
```

`POST /relay/v1/runs` with a `messages` body starts a run without
waiting for it and answers 202 with its `run_id` and `events_url`.
Resume, cancel, `Idempotency-Key`, `/healthz`, and `/readyz` are
described in spec section 7.4; [`docs/TESTING.md`](docs/TESTING.md)
lists the tests that cover them.

### Talk to the local model without the gateway

To check the runtime alone, ask llama-server directly:

```bash
curl -s http://127.0.0.1:8033/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Reply with exactly one word: pong"}]}'
```

The answer is in `choices[0].message.content`. Windows commands and
tool-call requests are in [`docs/TESTING.md`](docs/TESTING.md).

## License

MIT. See [LICENSE](LICENSE).
