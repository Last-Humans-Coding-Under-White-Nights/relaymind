---
name: summarize-incident-counts
description: Summarize incident counts for one demo service over a 15, 60, or 1440 minute window, using only the returned totals by severity. 汇总单个演示服务在指定时间窗口内的事件数量。
---

# Summarize incident counts

Use this skill when the user asks how many incidents a demo service had
over a recent time window.

## Steps

1. Find the service ID (`staging-api` or `payments-api`) and the window.
   The window must be 15, 60, or 1440 minutes. "Last hour" is 60 and
   "last day" or "last 24 hours" is 1440.
2. If the service is missing, or the window is missing or not one of the
   allowed values, ask. Do not guess.
3. Call `get_incident_counts` with the service and `window_minutes`.
4. Answer from the tool result only.

## Rules

- Report `total`, `critical`, `warning`, and `info` exactly, and name the
  window in minutes.
- Do not infer causes, trends, or blame. Counts only.
- If the tool call fails, say the counts could not be retrieved.
- Answer in the language of the request. Keep the numbers unchanged.

## Completion criteria

The answer contains the window and the exact `total`, `critical`,
`warning`, and `info` values from the latest successful
`get_incident_counts` result. Validator: `incident_counts_reported`.

## Examples

Paired English and Chinese examples are in `examples.json`. They are
illustrative and use the sandbox fixture values.
