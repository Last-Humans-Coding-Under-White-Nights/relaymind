# RelayMind

**RelayMind** is a local AI gateway. A small local model handles simple, bounded
tasks with skills and tools. Complex work is transferred to an external model.
Every routing decision, tool call, and model call is visible in a trace.

Status: **specification stage**. The spec is written; implementation has not
started yet.

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
| `src/relaymind/` | Gateway source (Python, FastAPI). |
| `skills/` | Demo skill packages (SKILL.md format). |
| `tests/` | Test suite and sandbox fixtures. |
| `scripts/` | Evaluation and benchmark scripts. |

## Development setup

Requires Python 3.12+.

```bash
git clone <repo-url> && cd relaymind
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run checks:

```bash
pytest           # test suite
ruff check .     # lint
ruff format --check .
```

## License

MIT. See [LICENSE](LICENSE).
