---
name: investigate-performance
description: Collect deployment, metrics, and log evidence for a slow or failing demo service, then give an evidence-based assessment that keeps observations apart from hypotheses. 收集性能问题的证据，并区分观察与假设。
---

# Investigate performance

Use this skill when the user asks why a demo service is slow, failing,
or showing errors. The local model may prepare evidence; the assessment
is expected to come from the external model after handoff.

## Steps

1. Find the service ID (`staging-api` or `payments-api`). If none is
   named, ask. Do not guess.
2. Collect evidence with the allowed tools:
   - `get_latest_deployment` for the service,
   - `get_metrics` with a window of 15, 60, or 1440 minutes,
   - `get_recent_logs` with a limit from 1 to 100.
3. Hand the evidence over for assessment when the local budget or the
   task needs it. Do not repeat tool calls that already completed.

## Rules

- Write an `Observations:` section with only values the tools returned,
  each with its source tool.
- Write a `Hypotheses:` section for possible causes. Mark them as
  hypotheses, not findings.
- If the evidence does not support any cause, write
  `Insufficient evidence` instead of guessing.
- A missing deployment or a truncated log list is itself an observation.
- In Chinese, use the section labels `观察：`, `假设：`, and `证据不足`.

## Completion criteria

At least one evidence tool returned a successful result, and the answer
has an observations section plus either a hypotheses section or an
insufficient-evidence statement. Validator:
`performance_evidence_separated`.

## Examples

Paired English and Chinese examples are in `examples.json`. They are
illustrative and use the sandbox fixture values.
