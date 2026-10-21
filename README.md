# RelayMind

**RelayMind** is a local AI gateway. A small local model handles simple, bounded
tasks with skills and tools. Complex work is transferred to an external model.
Every routing decision, tool call, and model call is visible in a trace.

Status: **M1 (adapter and protocol qualification) complete.** Live smoke
results for Apple Silicon macOS and native x86-64 Windows are recorded in
[`docs/EVIDENCE.md`](docs/EVIDENCE.md). See
[`docs/ROADMAP.md`](docs/ROADMAP.md) for the milestone plan and
[`CHANGELOG.md`](CHANGELOG.md) for what has landed so far.

## How this project works

- **SDD (Spec-Driven Development).** The specification in
  [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) is the single source of
  truth. Every task and every acceptance test must reference requirement IDs
  from it (RM01, RM02, ...). Behavior changes require a spec revision first.
- **TDD (Test-Driven Development).** Write a failing test before writing
  implementation code. No feature is merged without tests.

See [`AGENTS.md`](AGENTS.md) for the full contributor workflow.

## What the prototype will do

- Accept requests through an OpenAI-compatible API (`/v1/chat/completions`)
  and a native run API (`/relay/v1/runs`).
- Let a small local model (initial candidate: Qwen3-0.6B via llama.cpp)
  select a skill, call registered tools, and finish simple tasks locally
  with zero external calls.
- Escalate complex tasks to an external model, handing over the original
  request, tool evidence, and remaining budgets.
- Show every decision in a trace UI: selected skill, tool arguments,
  execution results, escalation reason, final answer, and timing.
- Work in English and Chinese, on macOS and Windows.

## Repository layout

| Path | Purpose |
|------|---------|
| `docs/SPECIFICATION.md` | The spec. Single source of truth. |
| `docs/ARCHITECTURE.md` | How components map to the code layout. |
| `docs/ROADMAP.md` | Implementation milestones with requirement IDs. |
| `src/relaymind/` | Gateway source (Python; FastAPI API layer planned for M4). |
| `models/` | Pinned model artifact manifests; the GGUF files are downloaded, not checked in. |
| `skills/` | Demo skill packages (SKILL.md format). |
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

The gateway API (`/v1/chat/completions`, `/relay/v1/runs`) arrives in
M4. Until then, talk to the local model through the adapter. Start the
pinned runtime first:

```bash
llama-server -m models/Qwen3-0.6B-Q8_0.gguf \
  --host 127.0.0.1 --port 8033 --ctx-size 8192 \
  --jinja --chat-template-kwargs '{"enable_thinking": false}'
```

Then send a message and print the answer as it streams:

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

### From M4: talking to the gateway

This is the planned contract (spec section 7), not something that runs
yet. The gateway speaks the OpenAI Chat Completions shape, so any
OpenAI client works once pointed at it. Every request carries the
application token the gateway generates at start (spec section 3);
the examples below keep it in `RELAYMIND_TOKEN`. You do not pick a
model: the alias `relaymind` means "route it for me". The local model
selects a skill and proposes the tool call; the gateway runs it and
escalates to the external model only if the local run cannot finish.

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $RELAYMIND_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model": "relaymind",
       "messages": [{"role": "user", "content": "Is staging-api healthy right now?"}]}'
```

To force where inference runs, add one header with `auto`,
`local_only`, `external_only`, or `parallel_demo`. Operator policy
decides which values a caller may use; the trace always shows which
model answered. `parallel_demo` runs the one fixed parallel workflow
(spec section 4.4, planned for M5), works only with the `relaymind`
alias, and only when operator configuration enables the workflow. `local_only` answers with HTTP 422 and code `needs_external`
when the local model cannot finish.

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $RELAYMIND_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-RelayMind-Execution-Mode: external_only" \
  -d '{"model": "relaymind",
       "messages": [{"role": "user", "content": "Is staging-api healthy right now?"}]}'
```

The response header `X-RelayMind-Run-ID` names the run; its trace is
available from the native API at `/relay/v1/runs/{id}`.

## License

MIT. See [LICENSE](LICENSE).
