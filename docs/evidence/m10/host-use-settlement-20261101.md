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
  here, and they match a second `npm ci` on the MacBook byte for byte. In the
  first VPS run, both tiers were run with the exact CI commands. Neither could
  start a person trial:
  - `status: setup_unavailable`, `reason: private_network_unavailable`. The VPS
    has only a public IPv4 on `eth0`. There is no sudo, and AppArmor blocks
    unprivileged user namespaces, so no private interface can be added.
  - Playwright 1.64.1 Chromium and its headless shell are both missing
    `libgbm.so.1` and `libasound.so.2`. They were reported as missing, not
    worked around.

  The two VPS captures (`*-20261101-vps.json`) record these environment
  outcomes only.
- **VPS second run (same machine, checkout 62c4dcd, 09:02–09:07 UTC).** The
  owner then added the libraries and a private dummy interface (`rm0`,
  10.213.47.1/24). The checkout was a git clone of 62c4dcd, because the person
  harness records `git rev-parse HEAD`. Python 3.14.4 ran the harness; CI uses
  3.12. The release tarball was `1ba82f18…4b81`. All three ubuntu tiers ran
  with the exact CI commands. Every `fixture_plugin_sources` row matches the
  checkout bytes.
  - `openclaw-person-ingress-20261101-vps2.json`: `completed`.
    `ordinary_pass` and `ordinary_skill_pass` are true.
  - `openclaw-mcp-use-20261101-vps2.json`: `blocked`,
    `exact_route_unqualified`, exit 0, as in CI. `fixture_discovery_pass` is
    true and `functional_exact_use` is false. `tools.effective` returned an
    empty list and `mcp.app.listTools` was refused. Both stdio servers listed
    `append`; no server call was made.
  - `openclaw-loaded-use-observation-20261101-vps2.json`: exit 2. The positive,
    stale and deleted cases observed `loaded_pinned_revision`, as in CI. The
    reset case is now `control_refused` (the `operator.admin` refusal), so the
    reset fix works on the real host. `ordinary_skill_loaded_use_pass` is still
    false, for a different reason. The predicate expects the session's
    selection listing to hold exactly one pin. The host lists every pin of the
    owner profile, including pins made in earlier sessions; the stale skill
    shows its new head revision there. When only the selected skill's pin is
    checked, the predicate accepts the capture. The CI capture also lists two
    pins in its stale case. This observation does not change the negative
    finding: no point fences the next turn.

  Before commit, `/home/<user>/` was rewritten to `/home/vps-user/` (same
  length) and the hostname to `sha256:<hostname_sha256>` in all three
  `*-vps2.json` captures. Their predicates give the same results after this.
  The skill record's revisions now name 62c4dcd and the harness at that
  commit. The MCP harness bytes are unchanged since acc4041.
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
- The observation step is not green yet. The reset fix works on the real host,
  but the single-pin check in `ordinary_skill_loaded_use_pass` fails on the
  host's profile-wide pin listing. The harness needs a reviewed change, then a
  CI run.
- A later host version reopens both questions.
