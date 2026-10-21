---
name: health-and-diagnostic-checklist
description: Run the fixed parallel demo workflow, where the local branch reads current service health and the external branch drafts a diagnostic checklist, then combine both. 并行演示：本地读取健康状态，外部模型起草诊断检查清单。
---

# Health and diagnostic checklist

Use this skill when the user asks for both the current health of a demo
service and a checklist for diagnosing it. It runs the fixed
`health-and-diagnostic-checklist` parallel workflow.

## Steps

1. Find the service ID (`staging-api` or `payments-api`). If none is
   named, ask. Do not guess.
2. Local branch: call `get_service_health` for the service.
3. External branch: draft a diagnostic checklist from the user request
   alone. The draft is provisional.
4. After both branches finish, a new external call combines the health
   observation and the checklist.

## Rules

- Report the observed health exactly as returned: the `status` word and
  the `checked_at` time. An `unavailable` service is never described as
  healthy.
- Label the checklist as a draft: `Diagnostic checklist (draft, not
  log-backed)`. In Chinese: `诊断检查清单（草稿，未基于日志）`.
- The checklist is not a diagnosis. Do not present its items as findings.
- Answer in the language of the request.

## Completion criteria

The answer preserves the exact status and `checked_at` from the latest
successful `get_service_health` result and labels the checklist as a
draft. Validator: `health_checklist_separated`.

## Examples

Paired English and Chinese examples are in `examples.json`. They are
illustrative and use the sandbox fixture values.
