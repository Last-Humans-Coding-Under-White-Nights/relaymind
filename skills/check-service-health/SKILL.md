---
name: check-service-health
description: Report the current health status of one demo service (staging-api or payments-api) with the exact status and check time the health tool returned. 查询单个演示服务的当前健康状态。
---

# Check service health

Use this skill when the user asks whether a demo service is up, healthy,
or available.

## Steps

1. Find the service ID in the request or the conversation. The only
   valid IDs are `staging-api` and `payments-api`.
2. If no service is named, ask which one. Do not guess.
3. Call `get_service_health` with that service.
4. Answer from the tool result only.

## Rules

- Repeat the returned `status` word exactly: `healthy`, `degraded`, or
  `unavailable`. An `unavailable` service is never described as healthy.
- Repeat the returned `checked_at` time exactly, in UTC.
- If the tool call fails, say the health check could not be completed.
  Do not report a status.
- Do not explain causes. This skill reports state only.
- Answer in the language of the request (English or Chinese). Keep the
  status word and the timestamp unchanged in a Chinese answer.

## Completion criteria

The answer names the service, the exact returned status, and the exact
`checked_at` time from the latest successful `get_service_health`
result. Validator: `health_status_preserved`.

## Examples

Paired English and Chinese examples are in `examples.json`. They are
illustrative and use the sandbox fixture values.
