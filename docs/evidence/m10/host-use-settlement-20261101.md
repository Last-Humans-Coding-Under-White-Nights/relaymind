# #255 host use settlement on OpenClaw 2026.10.7 (1 November 2026)

RM16, RM37, RM39; AC30, AC35; spec 0.66 sections 7.6.1, 7.6.2 and 12.6.6.
Both questions end in a **negative** `host_operation` record. Each operation
stays disabled in every environment. The records are **pending**: they lift
nothing until a pull request review by an org member other than the owner is
written into them.

| Operation | Record | Finding |
| --- | --- | --- |
| `skill_loaded_use` | `m10-negative-skill-loaded-use-openclaw-2026.10.7.json` | `stop_point_absent` |
| `mcp_use` | `m10-negative-mcp-use-openclaw-2026.10.7.json` | `exact_route_absent` |

Both records bind the host surface inventory
`openclaw-host-use-search-20261101-vps.json` and their captures by SHA-256.
They load with `relaymind.evaluation.asset_qualification_record.load_record`
(`tests/test_m10_host_use_records.py`).

## Where the evidence comes from

- **VPS (`linux_host`; Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic, x86_64,
  Intel Xeon Skylake, 11998482432 bytes RAM).** Node 24.22.1 (nvm) and npm
  11.19.1. `npm ci` and `npm run pack:release` built the same release tarball as
  CI (`63c2c660…24a3`). The host package digests in the inventory were computed
  here, and they match a second `npm ci` on the MacBook byte for byte. Both
  tiers were run with the exact CI commands. Neither could start a person trial:
  - `status: setup_unavailable`, `reason: private_network_unavailable`. The VPS
    has only a public IPv4 on `eth0`. There is no sudo, and AppArmor blocks
    unprivileged user namespaces, so no private interface can be added.
  - Playwright 1.64.1 Chromium and its headless shell are both missing
    `libgbm.so.1` and `libasound.so.2`. They were reported as missing, not
    worked around.

  The two VPS captures (`*-20261101-vps.json`) record these environment
  outcomes only.
- **GitHub Actions ubuntu (run 41286397320, head 548588a; harness bytes
  identical at acc4041).**
  - `openclaw-loaded-use-observation-20261101-ci.json`: the positive, stale and
    deleted cases observed `loaded_pinned_revision`. The host still delivers the
    pinned bytes after the library head changes or the entry is removed. The
    reset case failed as a harness error: the person cannot call
    `sessions.reset`, which needs `operator.admin`. The harness now records that
    exact refusal as `control_refused` (commit "Record the person-scope session
    reset refusal…"). This fix has unit tests only and no new real-host run yet.
  - `openclaw-mcp-use-20261101-ci.json`: `status: blocked`,
    `exact_route_unqualified`. `tools.effective` succeeded with an empty tool
    list for the person session. `mcp.app.listTools` without a view was refused.
    Both disposable stdio servers listed `append`, and no server calls were made.

## Loaded use: why `stop_point_absent`

The pinned host has points that can *stop* something:

- `before_tool_call` can block the `skills_read` call.
- Agent tool-result middleware can replace the tool result.
- RelayMind's own provider (`wrapStreamFn`/`onPayload`) can fail the next
  request before it is sent.
- `before_agent_run` can block a run once, before its first model submission.

None of them carries fresh native person authority for the turn. Their contexts
hold only agentId, sessionKey, sessionId, runId, toolCallId and requester facts.
There is no session access authority, no `assertCurrent` and no lifecycle
revision, and spec 7.6.2(2) excludes hook contexts as authority.
`dispatchGatewayMethod` works only inside the original authenticated request,
so the fence cannot re-read the pinned revision at use time. No point can
therefore *fence* the next model request as section 7.6.1 requires.

The one host object that could carry person currentness into a later turn is
`GatewaySessionAccessAuthority.retain()`. It is a native delegation whose
acquisition, scope, lifetime and revocation must be qualified separately, and
spec 0.66 defines no such qualification. It was recorded, not used.

Reviewers should note that the 0.66 wording reads "no host point can stop or
fence". The finding here means "no point can fence". Stop-only points exist.

## MCP use: why `exact_route_absent`

- `tools.invoke` resolves by name only. Its closed parameters are name, args,
  sessionKey, agentId, confirm, idempotencyKey and conversationReadOrigin.
- `mcp.app.callTool` needs an active MCP App view (`viewId`). Independent tools
  have no view.
- `agent-harness-tool-runtime` and `codex-mcp-projection` are documented as
  private to harnesses.

No public route reaches an exact server/name/schema identity. `relaymind.mcp`
stays unregistered, and the shipped registry lists neither operation.

## What remains

- Approval of both records in the pull request, then the follow-up commit that
  fills `pull_request`, `reviewer_logins` and `approved_at`.
- A CI run to confirm that the reset fix turns the observation step green.
- A later host version reopens both questions.
