# OpenClaw direct commands, skill loading and approvals — 29 October 2026

Issue #189 supplies bounded qualification evidence for #187. Spec 0.50 remains
normative (RM08, RM11, RM12, RM16, RM19, RM33; AC24). No product behavior changes.
This Linux x86-64 shell run uses real OpenClaw 2026.10.7, the real RelayMind
provider/gateway and deterministic model doubles. It is neither live-model
quality evidence nor a supported-platform acceptance run.

| Capability | Outcome | Observed boundary |
| --- | --- | --- |
| Registered commands before inference | passed | Real authenticated `chat.send` produced one recommendation action and one choice action, each with zero model calls. |
| Application/session provenance | passed | Host supplied `cli`, `webchat`, `operator.write`, agent, session key and session ID; an explicit disposable registration scoped the recorder. |
| Verified person/workspace authority | failed | `cli` identifies an application, not a person. The shared-token CLI library response has `profileId: null`; no production person/workspace authorization is qualified. |
| Separate probe credential | passed, bounded | The handler used a private recorder credential. Export scans found none of the three random credentials in model context, arguments or artifacts. This is no protection from a compromised host process or unrestricted shell. |
| Forged input isolation | passed | A wrong Gateway token, `actor=user` command arguments, ordinary `2`, a quoted command, and a real host echo tool result created no choice action. The forged echo arguments/result traversed native deferred `tool_call`. |
| Native host baseline | passed | The original catalog remained enabled. Native `tool_search` found core `skills_search` and `skills_read`; `skills_read` loaded the authored sandbox skill. No host search/catalog removal or replacement. |
| File identity and use | passed, bounded | Recorded source path/SHA-256; actual skill content reached the model double as a host tool result and the final assistant answered the marker. Changed/missing content was refused by the probe's pre-use guard. |
| Exact managed revision composed with choice | failed | Trusted-plugin runtime requests and authenticated HTTP/RPC dispatch both rejected this command context. CLI library save returned `SKILL_LIBRARY_IDENTITY_REQUIRED`. No managed skill/revision was attached or loaded. The separate guard/read is not race-free immutable loading. |
| Human approval allow/deny | not_run | No initiating interactive human approval surface was supplied. No programmatic approval response is counted as an operator decision. |

The integration decision is partial. #187/#195/#197 may rely on the existence of
the public pre-model command surface, host application/session context and
native search/read primitives demonstrated here. They cannot rely on a
verified person, command-to-library revision attachment or human mutation
approval. A read-only trial still needs an explicitly supported identity and
content-pinning design in #187 before implementation. Full M10 approval remains
open. A separately qualified authenticated Control UI plugin HTTP/RPC route with
a durable user profile is a candidate follow-up; it has not passed here. Do not
patch the host or impersonate an operator to bridge these gaps.

The command fixture declares `contracts.gatewayMethodDispatch` and registers no
recommendation tool, so it needs no `contracts.tools` entry. The existing sandbox
host-tool plugin declares its own echo/write tool ownership. Both contracts and
all inspected source hashes appear in `report.json`.

Reproduce command and native skill evidence from the checkout:

```bash
cd integrations/openclaw
npm ci
npm run build
cd ../..
PYTHONPATH=src python integrations/openclaw/test/host-command-qualification/qualify.py \
  --work-dir /tmp/relaymind-command-new \
  --out /tmp/relaymind-command-capture.json
```

Manual acquisition/install/dependency checks remain operator prerequisites for
real skills; this probe loads only its local sandbox skill. Managed library
operations additionally need durable Gateway sign-in and authorized session
access. A shared token or plugin command context alone supplies neither. No
market skill from the separate #188 inventory was executed.

`report.json` is the reviewable capability summary and source/integrity manifest.
The compressed captures retain the full sanitized commands, responses, model
requests, host context and run traces; decompress with `gzip -dc <capture>`.
The model double is scripted to answer the marker after the real host skill
read, so the result proves transport/loading, not model reasoning or skill
quality. Immutable revision use remains failed regardless of that marker.

For operator-interactive allow/deny, reuse the sandbox plugin and
`scripts/openclaw_qualification.py host --scenario tool-approve|tool-deny` with
an actual initiating approval-capable host surface. Check exactly one/zero
sandbox writes and zero gateway execution; host `ok` alone is insufficient.
No such human run is recorded here. Existing headless fail-closed observations
remain separately recorded in
[`2026-10-28-openclaw-vps-functional-137`](../2026-10-28-openclaw-vps-functional-137/README.md).

The separate `headless.py` driver (instructions in the probe README) reproduced
that refusal on 29 October with default native tool search and explicit sandbox
tool admission. Its `headless-capture.json.gz` records two model-double calls,
`Plugin approval unavailable: non-interactive CLI runs have no approval-capable
initiating surface`, an empty sandbox and zero gateway tool execution. The host
returned exit 0 despite refusing the tool. This is a fail-closed characterization,
not a human allow/deny pass. `tools.exec.mode=full` was not used.

Review correction R1: the historical capture and its `report.json` source hashes
are preserved. `regrade.json` identifies the corrected current grader separately
and regrades the saved capture without a host rerun. The actual final assistant
marker passes; a counterexample changing only that final assistant to `wrong
answer` is rejected even though the skill marker remains in tool history. The
runtime now uses this same capture-grading function. Original raw `checks` are
historical outputs; the separately identified regrade is the corrected check.

Review correction R2: the grader now reads the skill marker only from the
`native_skill_use` exchange's own model requests, and re-checks that the
recorded `choose` event names choice 2 and that both command events carry the
selected skill's sha256. The saved capture still passes every check unchanged,
without a host rerun; `regrade.json` records the R2 grader hashes and three
rejected counterexamples (marker moved to another exchange, choice 3, changed
digest). A setup failure in the driver now writes a `status: failed` export, and
a host that never listens raises a startup timeout. Neither changes the saved
capture.
