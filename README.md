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
local model artifact is qualified. There is no launcher or command-line
entry point yet; the gateway starts from a short Python script (see
[Send a message](#send-a-message)). Chinese support is best-effort
(spec 0.7): Chinese live results are recorded but are not a gate. M5
(parallel workflow and caching) and M6 (evaluation) are next. See
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

Not built yet: the one bounded parallel workflow and skill caching
(M5), and the evaluation corpus and latency benchmark (M6). Until the
workflow exists, the `parallel_demo` execution mode always fails with
`workflow_unavailable`.

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

## Send a message

### 1. Start the local runtime

Start the pinned runtime first (install it as described in
[`models/README.md`](models/README.md)):

```bash
llama-server -m models/Qwen3-0.6B-Q8_0.gguf \
  --host 127.0.0.1 --port 8033 --ctx-size 8192 \
  --jinja --reasoning off --reasoning-format deepseek
```

### 2. Start the gateway

There is no launcher or command-line entry point yet (open work,
issue #87). Until it exists, this script wires the local model, the
checked-in demo skills, the sandbox demo data, and a SQLite run store
into the gateway and serves it on loopback. Save it as
`start_gateway.py` and run it from the checkout root:

```python
from pathlib import Path

from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig
from relaymind.api.app import create_app
from relaymind.api.server import serve
from relaymind.orchestrator.local_loop import ConfiguredModel, LocalOrchestrator
from relaymind.orchestrator.workflows import PARALLEL_WORKFLOWS
from relaymind.skills import SkillRegistry
from relaymind.skills.predicates import COMPLETION_PREDICATES
from relaymind.store import RunStore
from relaymind.tools import CallerContext, OperatorPolicy, ToolRegistry
from relaymind.tools.sandbox import (
    DEMO_TOOL_NAMES,
    MUTATE_PERMISSION,
    READ_PERMISSION,
    SandboxDataService,
    register_demo_tools,
)
from relaymind.ui import DataMode, UiSettings

work = Path("demo-run")
tools = ToolRegistry()
sandbox = SandboxDataService(
    Path("tests/fixtures/sandbox/baseline.json"), work_dir=work / "sandbox", reset=True
)
register_demo_tools(tools, sandbox)
skills = SkillRegistry.load(
    [Path("skills")],
    tools=tools,
    predicates=COMPLETION_PREDICATES.keys(),
    workflows=PARALLEL_WORKFLOWS,
)
local = ConfiguredModel(
    adapter=LlamaCppAdapter(LlamaCppConfig(base_url="http://127.0.0.1:8033", context_limit=8192)),
    model_id="qwen3-0.6b-q8_0",
)
orchestrator = LocalOrchestrator(
    local,
    skills=skills,
    tools=tools,
    store=RunStore(work / "runs.sqlite3"),
    policy=OperatorPolicy(enabled_tools=DEMO_TOOL_NAMES),
    caller=CallerContext(permissions=frozenset({READ_PERMISSION, MUTATE_PERMISSION})),
)
orchestrator.recover_after_restart()  # once, before any run (spec section 5)
app = create_app(
    orchestrator=orchestrator,
    ui=UiSettings(mode=DataMode.LIVE, fixtures=sandbox),
)
print("RELAYMIND_TOKEN=" + app.state.application_token, flush=True)
serve(app, host="127.0.0.1", port=8080)
```

It prints the application token the gateway generated at start; keep
it in `RELAYMIND_TOKEN` for the requests below. The run store and the
sandbox copy go to `demo-run/`; delete that directory when you are
done. This script configures no external model, so a request that
needs one ends with `needs_external`. The caller can read and mutate
the sandbox; each mutation still requires a fresh confirmation (RM11).
The demo UI is at `http://127.0.0.1:8080/ui`; enter the application
token there to create runs and view traces. Its `LIVE` label describes
decisions from the local model; the tool data is the sandbox fixture
(spec section 9).

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
works only with the `relaymind` alias, and in this build it always
fails with `workflow_unavailable`, because the parallel workflow
(spec section 4.4) is planned for M5.

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

To check the runtime alone, send a message through the adapter and
print the answer as it streams:

```python
import asyncio

from relaymind.adapters import GenerationRequest, Message, Role, TextDelta
from relaymind.adapters.llamacpp import LlamaCppAdapter, LlamaCppConfig


async def main() -> None:
    config = LlamaCppConfig(base_url="http://127.0.0.1:8033", context_limit=8192)
    request = GenerationRequest(
        generation_id="hello_1",
        messages=(Message(role=Role.USER, content="Reply with exactly one word: pong"),),
    )
    async with LlamaCppAdapter(config) as adapter:
        async for event in adapter.generate(request):
            if isinstance(event, TextDelta):
                print(event.text, end="", flush=True)
    print()


asyncio.run(main())  # prints: pong
```

Or skip Python and ask llama-server directly:

```bash
curl -s http://127.0.0.1:8033/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Reply with exactly one word: pong"}]}'
```

The answer is in `choices[0].message.content`. Windows commands and
tool-call requests are in [`docs/TESTING.md`](docs/TESTING.md).

## License

MIT. See [LICENSE](LICENSE).
