# #255 host use settlement on OpenClaw 2026.10.7 (1 November 2026)

RM16, RM37, RM39; AC30, AC35; spec 0.66 sections 7.6.1, 7.6.2 and 12.6.6.
MCP use ends in a **negative** `host_operation` record. Loaded skill use does
**not**: the search found a supported native delegation that may carry the
section 7.6.1 fence, so `stop_point_absent` is not established and no negative
record is made for it (see "Loaded use" below). Both operations stay disabled
in every environment. The MCP record is **pending**: it lifts nothing until a
pull request review by an org member other than the owner is written into it.

| Operation | Record | Finding |
| --- | --- | --- |
| `skill_loaded_use` | none | not established; stays disabled as unqualified |
| `mcp_use` | `m10-negative-mcp-use-openclaw-2026.10.7.json` | `exact_route_absent` |

The MCP record binds the host surface inventory
`openclaw-host-use-search-20261101-vps.json` and its captures by SHA-256. It
loads with `relaymind.evaluation.asset_qualification_record.load_record`
(`tests/test_m10_host_use_records.py`). The inventory lists the attempted
routes of the corrected VPS3 run, selected older attempts and loaded-use
controls (`attempted_routes`), plus candidate routes that were not called
(`attempted_routes.not_attempted`). It is not a complete list for older runs:
the committed CI and VPS2 MCP captures each contain three successful
`tools.effective` reads with the old empty projection, omitted from their
inventory entries. The capture events retain those attempts; tests check that
these are the only omitted method/status pairs across bound MCP captures.
The digest-bound inventory and captures remain unchanged.

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
    (`tests/test_m10_host_use_records.py`). These cases observe the host's own
    pin behaviour; no RelayMind fence was in place.

  Before commit, `/home/<user>/` was rewritten to `/home/vps-user/` (same
  length) and the hostname to `sha256:<hostname_sha256>` in all three
  `*-vps2.json` captures. Here `vps-user` is a fixed redaction placeholder,
  not the original account name. Their predicates give the same results after
  this. Privacy tests allow only this placeholder and the standard CI
  `/home/runner/` path, and reject other Linux, macOS and Windows home paths.
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
  bba0cf1 bytes. The other 71 rows are 37 built adapter files and 34
  installed package files (31 host `dist` files and three other package files).
  The record tests rebuild bba0cf1 in a disposable directory, compare all 37
  adapter files, check the 34 installed package files, and recompute adapter
  revision `f3241577…2c6c`, the revision that commit's registry names. The
  OpenClaw CI job runs these checks with installed dependencies and also hashes
  all 26 host files in the surface inventory. Missing or changed files fail.
  The capture holds no home path, user name,
  hostname or public address; it shows the private `rm0` address, the
  disposable `example.test` accounts and `/tmp` sandbox paths. The VPS2
  captures additionally retain the documented `/home/vps-user/` placeholder.
  The VPS3 capture was committed unmodified. The MCP record's revisions now
  name bba0cf1, that adapter revision and the MCP harness at that commit.
- **GitHub Actions ubuntu (run 41286397320, head 548588a; harness bytes
  identical at acc4041).** The runner's hostname in the loaded-use capture's
  model requests (`host=` in the runtime line) was rewritten to
  `sha256:<SHA-256 of the hostname>` after review; no digest inside the capture
  covers those request bodies, and its predicates give the same results. The
  `/home/runner/` paths are the standard GitHub runner home, as in the other CI
  captures.
  - `openclaw-loaded-use-observation-20261101-ci.json`: the positive, stale and
    deleted cases observed `loaded_pinned_revision`. The host still delivers the
    pinned bytes after the library head changes or the entry is removed. The
    reset case failed as a harness error: the person cannot call
    `sessions.reset`, which needs `operator.admin`. The harness now records that
    exact refusal as `control_refused` (commit "Record the person-scope session
    reset refusal…"). The VPS2 real-host run confirms it (`control_refused`
    above); the later CI run below also confirms it.
  - `openclaw-mcp-use-20261101-ci.json`: `status: blocked`,
    `exact_route_unqualified`. `tools.effective` succeeded, but the capture
    shows an empty list (see the erratum below). `mcp.app.listTools` without a
    view was refused. Both disposable stdio servers listed `append`, and no
    server calls were made.

- **GitHub Actions ubuntu, later run ([41340879424](https://github.com/LHCWN/relaymind/actions/runs/41340879424),
  head ed21e007).** The workflow artifacts were inspected during review; these
  newer captures are not committed here or bound by the MCP record.
  - `openclaw-loaded-use-observation-ubuntu-1` contains
    `openclaw-loaded-use-observation-ubuntu.json`: `status: completed`,
    `ordinary_skill_loaded_use_observation: true`, reset `control_refused`.
    This confirms both the reset handling and selected-skill pin predicate in CI.
  - `openclaw-mcp-use-ubuntu-1` contains `openclaw-mcp-use-ubuntu.json`:
    `status: blocked`, `reason: exact_route_unqualified`. The current grader
    accepts fixture discovery: `mcp.app.discover` succeeds and `tools.effective`
    lists both `alpha-one__append` and `alpha-one-2__append`;
    `mcp.app.listTools` without a view is refused. This is discovery evidence,
    not qualified exact use.

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

## Loaded use: why `stop_point_absent` is not established

Spec 0.66 section 12.6.6 defines `stop_point_absent` as: no host point can
fence the next model request as section 7.6.1 requires; a point that can stop
delivery but cannot re-verify fresh person authority, the session incarnation
and the pinned bytes does not count.

The pinned host has points that can *stop* something:

- `before_tool_call` can block the `skills_read` call. Its context holds
  agentId, sessionKey, sessionId, runId, toolCallId and requester facts.
- Agent tool-result middleware can replace the tool result. Its context holds
  agentId, sessionId, sessionKey and runId.
- RelayMind's own provider (`wrapStreamFn`/`onPayload`) can fail the next
  request before it is sent. It sees the outgoing messages, so it can compare
  the delivered instruction bytes with the pinned revision, and
  `options.sessionId`, but no sessionKey, lifecycle revision or person
  authority.
- `before_agent_run` can block a run once, before its first model submission.
  It sees the prompt, messages and system prompt.

`before_prompt_build` cannot stop anything: a thrown error or timeout is logged
and the handler is skipped. With `requiresToolAuthority` it receives
`ctx.toolAuthority`, which checks the finalized tool surface of the active turn
and fails closed after the hook dispatch; its fingerprint is not an
authorization proof. Spec 7.6.1 says the same of this hook.

On its own, none of these points carries session access authority or person
authority, so none of them is a fence. But the host also offers a supported
native delegation. A `sessionAccess` gateway method handler, such as
RelayMind's `relaymind.skill`, receives `sessionAccessAuthority`, and its
`retain()` returns a borrow `{signal, assertCurrent, release}` that outlives
the handler (the host documents it for interactive viewers). In the pinned
`request-authorization` code, each `assertCurrent()` of that borrow re-runs at
call time the person's grant and operator run authority checks, the profile's
currentness, the role's agent scope, the tool policy and sharing, and the
session check: the same store path, store generation, `sessionId` and
`lifecycleRevision` as the admitted target. Reset, deletion, store replacement
and profile, role or grant changes abort its signal. This is not an earlier
RPC's closed scope or a JSON copy of its identity.

So RelayMind's provider could hold such a borrow and, in `onPayload`, call its
`assertCurrent()` and compare the outgoing instruction bytes with the pinned
revision before dispatch. Spec 7.6.1 admits "a supported native delegation
whose scope, expiry and revocation are separately qualified". This search did
not show that this composition fails. The open questions are in the
inventory's `skill_loaded_use.open_questions`: whether the provider runs where
it can hold the borrow, whose authority the borrow is for the turn that reaches
the provider and how long it lives, whether an `onPayload` throw actually stops
dispatch on this host, how the library-entry and operator-mapping checks of
7.6.1 are made at that boundary (the host itself keeps delivering a removed
entry's pinned bytes), and the revocation, reset and removal stops while a turn
waits.

The earlier version of this document called `retain()` an "unqualified
delegation (spec gap, not used)" and asserted `stop_point_absent`. That was
wrong: an unqualified delegation is a RelayMind qualification gap, not proof
that the host cannot fence. The negative `skill_loaded_use` record was
therefore withdrawn before review. `skill_loaded_use` stays disabled because it
is unqualified, not as a host limitation, and its AC30 evidence is still
required on this host version. A later qualification attempt of the
`retain()` composition decides it either way: a positive record if every
section 7.6.1 control passes, or a negative record whose captures show where it
fails.

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

- Approval of the MCP record in the pull request, then the follow-up commit
  that fills `pull_request`, `reviewer_logins` and `approved_at`. The record
  closes the `mcp_use` gate only once that approval is written into it.
- A qualification attempt of the `retain()` fence candidate for
  `skill_loaded_use` (see "Loaded use" above).
- The MCP search did not call `tools.invoke`, `mcp.app.callTool` or the
  harness tool runtimes; their verdicts rest on the pinned host source. A
  reviewer may ask for bounded attempts of them before approval.
- The later CI captures from run 41340879424 remain workflow artifacts; they
  have not been committed or added to the record's digest-bound subjects.
  Both current harnesses have run in CI, with the results recorded above.
- A later host version reopens both questions.
