---
name: lookup-latest-deployment
description: Report the latest deployment of one demo service (staging-api or payments-api) with its deployment ID and time, or say that no deployment exists. 查询单个演示服务的最新部署。
---

# Look up the latest deployment

Use this skill when the user asks what was deployed last, or when, for
a demo service.

## Steps

1. Find the service ID in the request or the conversation (`staging-api`
   or `payments-api`). If none is named, ask. Do not guess.
2. Call `get_latest_deployment` with that service.
3. Answer from the tool result only.

## Rules

- Report the returned `deployment_id` and `deployed_at` exactly. Add the
  `revision` when it is present.
- If `deployment_id` is null, say the service has no recorded
  deployment. Never make up a deployment ID.
- If the tool call fails, say the lookup could not be completed.
- Answer in the language of the request. Keep IDs and timestamps
  unchanged.

## Completion criteria

The answer contains the exact `deployment_id` and `deployed_at` from the
latest successful `get_latest_deployment` result, or, when none exists,
names the service and states no deployment ID. Validator:
`deployment_reported`.

## Examples

Paired English and Chinese examples are in `examples.json`. They are
illustrative and use the sandbox fixture values.
