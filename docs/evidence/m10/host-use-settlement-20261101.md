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
    `exact_route_unqualified`, exit 0, as in CI. `functional_exact_use` is
    false. The capture shows an empty `tools.effective` list; see the erratum
    below. `mcp.app.listTools` was refused. Both stdio servers listed `append`;
    no server call was made.
  - `openclaw-loaded-use-observation-20261101-vps2.json`: the run exited 2. The
    positive, stale and deleted cases observed `loaded_pinned_revision`, as in
    CI. The reset case is `control_refused` (the `operator.admin` refusal), so
    this real-host run confirms the reset fix (dc70a34, included in 62c4dcd).
    The capture's embedded `ordinary_skill_loaded_use_observation` check is
    false because the 62c4dcd predicate expected the session's selection
    listing to hold exactly one pin. The host lists every pin of the owner
    profile, including pins made in earlier sessions; the stale skill shows
    its new head revision there. The CI capture also lists two pins in its
    stale case. The selected-skill pin check (5d92b64, "scope loaded-use pins
    to selected skill") is already in this branch, and the current
    `ordinary_skill_loaded_use_pass` accepts this committed capture
    (`tests/test_m10_host_use_records.py`). A passing observation does not
    change the negative finding: no point fences the next turn.

  Before commit, `/home/<user>/` was rewritten to `/home/vps-user/` (same
  length) and the hostname to `sha256:<hostname_sha256>` in all three
  `*-vps2.json` captures. Their predicates give the same results after this.
  The skill record's revisions name 62c4dcd and the harness at that commit.
- **VPS third run, MCP tier only (same machine and private interface,
  checkout bba0cf1, Node 24.22.1).** `openclaw-mcp-use-20261101-vps3.json`
  (SHA-256 `a618b253…c1f`): `blocked`, `exact_route_unqualified`. The
  capture does not hold the process exit code; the harness's `trial_outcome`
  gives 0 for it (person tier `completed`, exit 0, discovery passed). The
  person session first calls `mcp.app.discover`, which starts its MCP runtime
  without an App view or a tool call. Each `tools.effective` read then lists
  both colliding tools with the host's own mapping: `alpha-one__append`
  (`alpha.one`/`append`) and `alpha-one-2__append` (`alpha?one`/`append`).
  `mcp.app.listTools` without a view was refused, and no server call was made.
  The current grader (byte-identical to the one in the capture) accepts
  fixture discovery and rejects exact use. All 22 `fixture_plugin_sources`
  rows and the 58 source inventory rows that are tracked files match the
  bba0cf1 bytes. The other 71 rows are the built `dist` files (68) and three
  installed package files. A local build at bba0cf1 matched all 68 `dist` rows
  and gives adapter revision `f3241577…2c6c`, the revision that commit's
  registry names. The capture holds no home path, user name,
  hostname or public address; it shows the private `rm0` address, the
  disposable `example.test` accounts and `/tmp` sandbox paths, as the VPS2
  captures do. It was committed unmodified. The MCP record's revisions now
  name bba0cf1, that adapter revision and the MCP harness at that commit.
- **GitHub Actions ubuntu (run 41286397320, head 548588a; harness bytes
  identical at acc4041).**
  - `openclaw-loaded-use-observation-20261101-ci.json`: the positive, stale and
    deleted cases observed `loaded_pinned_revision`. The host still delivers the
    pinned bytes after the library head changes or the entry is removed. The
    reset case failed as a harness error: the person cannot call
    `sessions.reset`, which needs `operator.admin`. The harness now records that
    exact refusal as `control_refused` (commit "Record the person-scope session
    reset refusal…"). The VPS2 real-host run confirms it (`control_refused`
    above); no new CI run has confirmed it yet.
  - `openclaw-mcp-use-20261101-ci.json`: `status: blocked`,
    `exact_route_unqualified`. `tools.effective` succeeded, but the capture
    shows an empty list (see the erratum below). `mcp.app.listTools` without a
    view was refused. Both disposable stdio servers listed `append`, and no
    server calls were made.

### Erratum: the empty `tools.effective` list

An earlier version of this document said that `tools.effective` "returned an
empty list" in the CI and VPS2 runs. That was wrong. The harness, not the host,
produced the empty list: its probe read `payload.tools`, but the pinned host
returns `{groups: [{tools: [entry]}], notices?}`, so the projection was always
empty. Those runs also did not start the person session's MCP runtime before
the read, so what the host actually listed then was not recorded. PR #249 fixed
both (commit be67556 projects `groups[].tools`; commit 53ac1e0 calls
`mcp.app.discover` first). The CI and VPS2 captures' embedded `fixture_discovery_pass: true` came
from the old grader; the current grader rejects their discovery. The VPS3
capture above is the corrected run: the host lists both colliding tools, and
the result is still `blocked`. Discovery lists names and the host's
server/original mapping; it does not invoke a tool, so `exact_route_absent`
stands.

## Loaded use: why `stop_point_absent`

Spec 0.66 section 12.6.6 defines `stop_point_absent` as: no host point can
fence the next model request as section 7.6.1 requires; a point that can stop
delivery but cannot re-verify fresh person authority, the session incarnation
and the pinned bytes does not count.

The pinned host has points that can *stop* something:

- `before_tool_call` can block the `skills_read` call.
- Agent tool-result middleware can replace the tool result.
- RelayMind's own provider (`wrapStreamFn`/`onPayload`) can fail the next
  request before it is sent.
- `before_agent_run` can block a run once, before its first model submission.

None of them can re-verify fresh person authority, the session incarnation and
the pinned bytes, so each is a stop-only point and does not count. Their contexts
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

## MCP use: why `exact_route_absent`

Spec 0.66 section 12.6.6 defines `exact_route_absent` as: no supported host
route invokes an independent, non-App MCP tool by its exact native identity;
MCP App view routes such as `mcp.app.callTool` are App-scoped and do not count
(section 7.6.2 conditions 3 and 10).

- `tools.invoke` resolves by name only. Its closed parameters are name, args,
  sessionKey, agentId, confirm, idempotencyKey and conversationReadOrigin.
- `agent-harness-tool-runtime` and `codex-mcp-projection` are documented as
  private to harnesses.
- `mcp.app.callTool` reaches a tool only through an active MCP App view
  (`viewId`). It is App-scoped and does not count. Independent tools have no
  view, and no legitimate selected-asset-to-view binding exists in this
  composition. An App-specific trial would be a separate path; it was not
  tried.
- `tools.effective` (after `mcp.app.discover`) lists the exact exposed names
  and the host's server/original mapping, but it is read-only discovery.

No supported host route invokes an independent, non-App MCP tool by its exact
native identity. `relaymind.mcp` stays unregistered, and the shipped registry
lists neither operation.

## What remains

- Approval of both records in the pull request, then the follow-up commit that
  fills `pull_request`, `reviewer_logins` and `approved_at`.
- A CI run of the current loaded-use harness. The current predicate already
  accepts the committed VPS2 capture, whose embedded check predates the
  selected-skill pin fix; no capture made by the current harness exists yet.
- A CI run of the corrected MCP tier; only the VPS3 run has used it.
- A later host version reopens both questions.
