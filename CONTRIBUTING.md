# Contributing to RelayMind

Thanks for your interest in contributing to **RelayMind**.

## Development prerequisites

- **Python 3.12+**
- **llama.cpp server** (optional — only needed for live local-model tests)

Clone and install:

```bash
git clone <repo-url> && cd relaymind
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Commands

| Action | Command |
|--------|---------|
| Run full test suite | `pytest` |
| Run a single test file | `pytest tests/test_<name>.py` |
| Run a specific test | `pytest tests/test_<name>.py::test_case` |
| Run live model tests | `pytest -m live` (needs `llama-server` and the model; see `models/README.md`) |
| Lint | `ruff check .` |
| Check formatting | `ruff format --check .` |
| Format | `ruff format .` |

Manual testing from a terminal (JSON requests on macOS and Windows) is
described in [`docs/TESTING.md`](docs/TESTING.md).

## How we work: SDD and TDD

This project uses Spec-Driven Development and Test-Driven Development.

- The spec ([`docs/SPECIFICATION.md`](docs/SPECIFICATION.md)) is the single
  source of truth. Every task and test references its requirement IDs
  (RM01–RM25, AC01–AC16).
- Behavior changes need a spec revision **before** code changes.
  Implementation-only refactoring does not.
- Write a failing test first, then implement until it passes.

See [`AGENTS.md`](AGENTS.md) for the full workflow, invariants, and
repository map.

## Tests and fixtures

- `tests/` holds the deterministic suite. It uses model doubles and must
  not depend on live model output.
- `tests/fixtures/` holds disposable sandbox demo data. Tests must never
  touch a production resource.
- Live model tests are marked separately and are not part of the default
  run.

## Formatting and style

- Run `ruff format .` before committing.
- Run `ruff check .` and fix all warnings.
- Use type hints on public functions.
- Write docs, comments, and messages in simple English.

## Pull request expectations

- Keep PRs focused — one logical change per PR.
- Name the requirement IDs (RM / AC) the change covers in the description.
- All CI checks must pass (`pytest`, `ruff check`, `ruff format --check`).
- If the change touches API behavior, tool ownership, approvals, platforms,
  or evaluation definitions, link the spec revision that allows it.

## Reporting issues

Open an issue with:

- A minimal reproduction (request payload or test case).
- Expected vs. actual behavior.
- The requirement ID involved, if known.

## Code of conduct

This project is governed by our [Code of Conduct](CODE_OF_CONDUCT.md).
By participating, you are expected to uphold it.
