# Runner configurations implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task by task.

**Goal:** Reproduce and label phase 2 runner configurations (#128).
**Architecture:** Shared settings validation feeds launch files and labels. Runs without a local model skip the runtime entirely and require an external endpoint.
**Tech Stack:** Python, argparse, TOML, pytest.
**Spec:** docs/SPECIFICATION.md, RM03, RM25, RM26, RM29, RM31, RM32; sections 12.1–12.3 and 14.1.

## Global constraints

Baseline defaults remain unchanged. Measurements are never invented. Credentials use the existing environment variable. Tests use model doubles.

## Review focus

Invalid and duplicate settings fail before launch. No local model means no manifest access or local process. Early-header aliases must agree. Offline regrading preserves recorded labels. External latency expectations differ from the baseline.

## Task 1: Runner configuration plumbing

Files: scripts/evaluation_config.py, scripts/live_acceptance.py, scripts/run_corpus.py, scripts/latency_benchmark.py, tests/test_runner_configurations.py, scripts/README.md.
Interfaces: add_configuration_arguments(parser), configuration_from_args(args), RunnerConfiguration.gateway_options(), RunnerConfiguration.to_json_object(), label_report(report, configuration).

- [x] Write failing parsing, launch round-trip, runtime-bypass and label tests.
- [x] Verify failures before implementation.
- [x] Implement validation and serialization; wire both runners, preserving old flags.
- [x] Run focused tests and full pytest, ruff check, ruff format --check.
- [x] Commit without attribution trailers.

The no-local report also excludes non-applicable local completion from its denominator and target verdict. New traces require the final answer model to be local; legacy traces without that field retain their historical grade. Tests cover absent local models and gateway-owned answers.

## Scope boundary

Full metrics expansion, live matrix, timing-sample import, placement experiments, workflows and evidence-backed defaults remain in #128. No new live evidence is claimed.

## Verification

Runner configuration tests failed first for the missing helper and CLI options.
No-local grading tests failed first for the old denominator and gateway owner behavior.
The report test caught an incorrect “not met” label for a non-applicable target.
Final verification: 3982 deterministic tests pass, 13 live tests deselected; ruff check and format checks pass.
No live measurements or default changes were made. Parent agent reviews and stacks this commit.
