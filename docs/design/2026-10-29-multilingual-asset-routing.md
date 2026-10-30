# M10 multilingual asset routing

29 October 2026. Design rationale for specification 0.51, subordinate to
[SPECIFICATION.md](../SPECIFICATION.md). The specification defines normative
fields, limits, states, errors and acceptance. This document explains the
choices and qualification boundaries; it records no completed product feature
or measured M10 quality, latency or cost gate.

## Customer decisions and scope

The customer approved three interpretations on 29 October 2026:

- Consumption covers the whole host task, including added inference, retries,
  failures and user interactions, as well as the recommender's own resources.
- One-second readiness measures gateway process start to a valid lexical
  recommendation over already committed full-catalog metadata. Cold OpenClaw
  startup is measured separately.
- The early trial uses explicit imports. Scheduled refresh of a permitted,
  qualified external source is required before M10 completes.

These replace the pending interpretations in issue #187's older planning
snapshot. The single active scope, numeric parser/transport/scheduler limits,
repetition rule and backoff policy are builder decisions frozen in 0.51,
not additional customer agreements or measured improvements.

M10 helps users find skills and independent MCP tools for Simplified Chinese,
English and mixed-language tasks. It keeps selection replaceable, supports
explicit choices and local correction adaptation, and serves lexical results
while richer representations are built. Traditional Chinese is diagnostic.
There is one milestone. The early installed-skill trial is delivery order;
it does not remove hybrid, MCP, source refresh, whole-task or native Windows
requirements.

Manual acquisition, review, installation and dependency setup remain visible
host/user work. Cataloging does not install or execute anything. Outside scope
are automatic installation, arbitrary market crawling, a gateway MCP client or
server spawning, a new executor, multi-asset planning, an asset-need classifier,
online fine-tuning, cloud feedback and silent host prompt/tool pruning.

## Adopted core freeze and its cost

Specification 0.51 freezes the independent M10 recommendation core and
preserves every full-milestone gate. Qualifications #188–#190 are reconciled
per capability below. The blockers MATRIX-COHORT, MATRIX-OVERLAP,
MATRIX-RIGHTS, MATRIX-REFRESH, HOST-ACTION-AUTHORITY, HOST-EXACT-SKILL-USE,
HOST-MCP-BINDING and HOST-MCP-CURRENTNESS remain named. Their unqualified adapter
contracts are not implementation-ready; the core fails closed at those
boundaries. Further supported qualification and a subsequent normative adapter
revision are required before host snapshots or use are enabled. No host upgrade,
alternate dataset or relaxed acceptance is included.

This allows precise local/catalog/discovery/v2/API/backend-security work after
actual specification merge. A spec merely present in a stacked branch does not
authorize dependent product edits. Final RM/AC mappings must also reach the
implementation issues first. Qualification issues can finish with an evidenced
negative finding; that does not make blocked product acceptance pass.

The cost is a later adapter revision and some independently implementable
portions whose issues remain open. The alternative would be to invent host
identity/currentness or stop independent work without improving those external
facts. This decision preserves full completion scope. Actual dependencies and
issue-by-issue review still apply; partial delivery cannot auto-close missing
acceptance. Required data/profile/interactive inputs are blockers, not consent
to assume success.

## Qualification record

### Matrix source: repeatable capture, incomplete cohort and rights

The committed [#188 record](../evidence/2026-10-29-matrix-qualification/README.md)
and [report](../evidence/2026-10-29-matrix-qualification/report.json) establish a
documented per-name ZIP route:
`GET /api/registry/skill/{name}/install?format=zip`. The supplied
`hmos-arkui-develop-skill` version 1.1.1 was downloaded twice as 99,960 original
bytes, SHA-256
`b11b85a8aecfb4c0743b254de9bd98183cd218f6cf988731ffa5d46393fa15e1`.
One original Chinese skill body was observed; its description was English.
No market asset was installed/executed. Original archives remain outside the
repository; the probe-only capture manifest is not a Matrix export API.

The default HMOS UI reported 56 skills and rendered 40 cards. Changing filter
counts cannot prove 100 distinct original Chinese assets. No equivalent-function
pair was qualified. The sampled archive had no license/notice filename and
upstream rights remain unknown. Public download is not redistribution or
scheduled-inventory permission. The source terms require per-author rights
handling; this documentation makes no new rights determination.

The supported fallback request is an operator export from the same Matrix
source: original versioned bytes, IDs/URLs/capture times/hashes, license/NOTICE
material, distinct inventory, original-language review and reviewed English
functional pairs. No such package is presumed supplied. A permitted export can
unblock import/development; it does not alone prove automatic source refresh.

| Gate | Current status and required input |
| --- | --- |
| MATRIX-COHORT | Blocked: 100 genuine distinct original Chinese Matrix assets with reviewed provenance. |
| MATRIX-OVERLAP | Blocked: at least 25 independently reviewed equivalent-function Chinese/English pairs. |
| MATRIX-RIGHTS | Blocked: per-asset permitted use/retention/redistribution and required attribution/NOTICE. |
| MATRIX-REFRESH | Blocked: permitted configured inventory/acquisition path, cadence, versions, complete generations and add/update/removal semantics. |

The finite product source bounds in spec 14.5 are new builder limits. They do
not retroactively change the #188 probe's 10 MiB-per-file/50 MiB capture bounds,
qualify safe archive extraction, or authorize a bulk crawler. An alternative
source or smaller cohort needs a separate customer/spec decision before freeze.

### Direct host actions: command ingress works, exact choice-to-use blocked

The [#189 record](../evidence/2026-10-29-openclaw-command-qualification/README.md),
[report](../evidence/2026-10-29-openclaw-command-qualification/report.json) and
[corrected regrade](../evidence/2026-10-29-openclaw-command-qualification/regrade.json)
cover real OpenClaw 2026.10.7/provider/gateway on Linux x86-64 with model doubles.
They are functional evidence, not supported-platform/live-quality acceptance.

| Capability | Outcome and limit |
| --- | --- |
| Registered pre-model command ingress | Passed: authenticated chat.send yielded one recommendation and one choice action, zero command inference calls. |
| Application/session context and private credential | Passed within fixture: host supplies CLI/channel/agent/session context; three random credentials absent from scanned exports. Application identity is not verified person/workspace ownership. |
| Forged source/model/chat input | Passed: wrong token, actor=user, ordinary number, quoted command and actual forged tool result caused zero choice actions. |
| Intact native search/read | Passed: tool_search found skills_search/skills_read; actual host read and final assistant returned an authored sandbox marker. A scripted double proves loading transport, not model competence. |
| Exact immutable revision composed with choice | Failed: trusted runtime bridge rejects third-party plugin; separate authenticated dispatcher rejects this command context; library save requires durable profile (`SKILL_LIBRARY_IDENTITY_REQUIRED`). Separate hash guard/read leaves a race. |
| Human interactive allow/deny | not_run; separate headless refusal made zero sandbox writes and zero gateway tool executions, which is not human approval. |

CLI `skills library attach` invokes `skills.library.activate`; there is no
`skills.library.attach` RPC. The trusted runtime bridge and the SDK's declared
authenticated HTTP/RPC dispatcher have distinct authority boundaries. Their
existence cannot be composed into command authority by assumption. Shared-token
CLI profileId is null.

HOST-ACTION-AUTHORITY and HOST-EXACT-SKILL-USE remain blocked for host completion
in #195/#197 and the #198 trial. Read-only status removes mutation approval
from an early trial, not authentic actions or exact intended content. A
supported durable-profile Control UI HTTP/RPC path is a further qualification
candidate, not selected production support. It needs actual authorized scope,
exact immutable activation and a later normative contract. No impersonation,
host patch or version change is approved. Gateway registered-adapter backend
checks can be tested with trusted doubles; that cannot qualify this real path.

### MCP: genuine provenance and execution, incomplete atomic currentness

The [#190 record](../evidence/2026-10-29-openclaw-mcp-qualification/README.md) and
[report](../evidence/2026-10-29-openclaw-mcp-qualification/report.json) use real
OpenClaw 2026.10.7 embedded Gateway, Node 24.22.1, MCP SDK 1.32.0, unchanged
RelayMind provider/gateway and deterministic model doubles on Linux x86-64.

| Capability | Outcome and limit |
| --- | --- |
| Same-instance public schema/provenance | Passed: inspectToolSchemas plus getPluginToolMeta captures exact exposed name, original server/tool and normalized parameters on the same concrete tool. Canonical hash is locally derived, not a host availability revision. |
| Colliding server names and actual execution | Passed: alpha.one/alpha?one original echo map to alpha-one__echo/alpha-one-2__echo. Distinct markers/counters identify each server. |
| Changed schema | Passed: first server text:string→count:integer, actual count:2 execution. Three successful invocations overall; ten gateway runs completed, zero gateway tool execution. |
| Disconnect | Retained offered schema/name; actual call failed with transport error, zero server invocations. Presence is not current availability. |
| Host deny/removal | Observed pass: policy hid both tools; removing second server excluded its name/calls. First-after-change and subsequent results do not prove atomic updates. |
| Operator tools.effective | Actual embedded session inventory has genuine mapping/notices, omits full schemas/catalog revision and can retain stale cached tools. |
| Third-party authorized access | Runtime request refused on all six actual session-hook observations, despite explicit conversation grant. No operator authority was borrowed. |
| Atomic session/requester/turn/revision/currentness | Unsupported by qualified composition. Schema-hook session/turn/revision absent, availability unknown. Separate hook ordering is not atomic binding. |

Fixture uses coding profile, `tools.toolSearch=false` and an explicit disposable
plugin conversation grant. It is not a customer default or intact-native
comparison. Random host/gateway/environment credentials were guarded/scanned;
HTTP header coverage is synthetic only. HTTP/OAuth transport, live models,
customer completion, macOS/native Windows and human approval are not_run.

HOST-MCP-BINDING and HOST-MCP-CURRENTNESS remain blocked: an authorized supported
route must provide an immutable session/requester/turn/server/tool/schema/policy
binding and host-issued current revision, revalidated at use. No exact envelope
is frozen from these partial hooks. 0.51 common host_snapshot_revision is null,
public snapshot payload/non-null override is rejected, and runtime create
returns recommendation_unavailable before selection. Imported discovery can
retain genuine provenance with unknown/needs_setup/unavailable status. Neither
schema/capture digest nor successful download is live availability. The report
does not establish that upgrading the host alone solves this.

## Architecture and alternatives

| Approach | Benefit | Limitation and decision |
| --- | --- | --- |
| Lexical baseline/floor | Small, deterministic, no generation; useful before vector/model warm-up | Shared tokens are not semantic translation; measure separately and retain fallback uncertainty. |
| Local hybrid | Multilingual semantics with bounded pool and rank fusion | Selected direction, subject to development-pinned artifact/license/quality/resource evidence. Embedding remains measured inference. |
| Model-per-query | Flexible comparator using existing concepts | Catalog/input costs grow; only separately configured compatible comparisons, no claim that compact recommendation fixes full host context. |

Ownership follows spec sections 3/4.6. Catalog owns provenance/revisions and
lexical service; primary owns ranking/representations; coordinator owns budget,
ordered attempts/fallback/admission/persistence; adaptation is a named optional
stage; host owns loading/execution/permission/approval. No selector executes or
grants access. Managed v1, its none-fallthrough and AC22 remain unchanged;
recommendation v2 has no execution run ID. Provider keeps
`managedSkillSelection:false` and AC24 ownership. A future read-only recommend
tool needs qualification/manifest declaration and model-origin labeling; it
cannot write choices or feedback.

One primary plus one attributed lexical fallback is sufficient. Valid no_match
is preserved; only failed/not-ready/stale/invalid/timed-out attempts fall back
within one total clock. Do not repeat lexical as its own fallback. Eligibility,
primary validation, optional adaptation and final admission have separate
recorded ownership. Third-party primary receives the same bounded full catalog
and no feedback corpus; service feedback revision remains private.

The independent ingestion lane reads ordinary configured root/immediate-child
SKILL.md folders without conversion or a required managed manifest. Bounded
data-only frontmatter/body parsing preserves original-byte hashes, explicit
version/language/dependency metadata and unknown host availability; it never
follows script/resource declarations. Local complete scans require confirmed
absence tombstones before removal. The separate metadata_import lane uses the
closed AssetMetadataImportV1 document, configured source binding, relative
original paths and staged byte/schema digest checks. Partial imports cannot
remove; complete inventories still require explicit tombstones. Neither format
is a live host snapshot, a claimed Matrix export or permission to acquire data.

V2 HTTP is one UTF-8 JSON POST per isolated attempt, no retry; only a valid
application/json 200 object can succeed. Fixed transport/status/invalid/stale/
timeout outcomes explicitly govern bounded lexical fallback. no_match and
cancellation never trigger another attempt; infrastructure failures remain
terminal. Section 4.6 specifies the full outcome table without changing v1.

Catalog original bytes/hashes remain separate from NFC safe projections.
Identity is source-namespaced, not display-name deduplication. Oversize/control/
active-markup/repetition rejection records exclusions, never fake full coverage.
Opaque acquisition is not unrestricted archive extraction. Publish metadata and
lexical projection atomically, then compatible vectors asynchronously; filter
old/deleted content even while rebuilding. Last usable generation survives
failure. One background worker yields to inference/warm-up; batch drain and
contention are measured, not unsupported preemption promises. Exact limits and
configuration live once authoritatively in spec 14.5.

## Actions, feedback and privacy

One active local configured user/workspace scope fits the existing single-user
bearer boundary without claiming verified person identity or multi-tenancy.
Local operator registration gives a separate protected action credential; the
ordinary bearer cannot enroll itself through HTTP. Rotation/revocation checks
apply to replays. Host production registration remains disabled until its
qualified contract exists; deterministic backend doubles prove rejection/CAS,
not host authority.

Feedback's configured_primary name/version is frozen at recommendation
acceptance as configuration provenance. It is not the actual producer: failed
primary plus lexical floor stays separately recorded, and empty_catalog has
zero attempts/no producer even when targetless usefulness is submitted.

Choice CASes recommendation state_version only and persists immutable direct
input evidence. Asset/none becomes chosen; explicit cancel becomes cancelled,
distinct from compute disconnect. Choice creates no FeedbackRecord, changes no
feedback_revision/adaptation and implies no success, correction, rejection or
negative usefulness. Explicit correction/rejection/usefulness uses the feedback
API and expected_feedback_revision; only correction records feed adaptation.
Unrelated feedback and choice can commit independently. Early trial collects
choices and optional explicit corrections/ratings without learning until the
adaptation stage is delivered.

Keep ordered lifecycle/action evidence, versions, original offer/task/scope and
replay receipts. Choice receipt is historical, never renewed execution grant.
Ready choice expiry and longer request retention differ. Feedback after expiry
is possible on retained ready-derived evidence. Changed asset requires a fresh
offer; genuine mutation approval is separate. Deleting an asset stops new use,
cannot undo a host side effect or erase already loaded context; fresh sessions
may be necessary.

Request-bearing storage is local/redacted with resolved private paths. Feedback
delete/reset removes payload and derived/event/replay copies, leaving only
content-free tombstones for remaining original replay retention. Reset removes
feedback/adaptation, not every historical terminal choice. Purge cannot restore
erased text through retries. Fixed correction history/vote/rank limits never
insert missing candidates or promote confidence; changed top requires choice.
Semantic reset matches baseline bytes under identical deterministic settings.

## Evaluation, delivery and documentation

Spec 12.6 freezes the actual 200-skill development baseline, single 300-asset
acceptance catalog, independent native/proficient prompt authors and two
annotators, family-separated labels, five required lanes and hard cross-language
subsets. It preserves original Matrix requirements and licensing/overlap gates.
Discovery quality and executable runtime tasks have separate denominators.
Failures/timeouts/unknowns remain visible. Wilson lower bounds, confident
coverage, ambiguity rubric and adaptation/control/reset gates are required;
toy doubles and repeated prompts cannot inflate effective samples.

The planned shared command is
`python scripts/asset_selection_benchmark.py run --config PATH --output PATH`,
then `compare A B --output PATH --markdown PATH`. It does not exist yet. Built-in
and HTTP replacement use the same production protocol, same offered input and
same locked cases/settings. Primary-only and service-level reports stay apart;
adaptation defaults off for comparisons. `--quality-only` relaxes machine
identity alone and never changes labels/protocol or proves speed. A deterministic
HTTP example proves replacement, not language quality.

Whole-task evaluation compares intact native/direct external, provider with
recommendations off and provider plus explicit recommendations. At least 100
preregistered paired tasks count all attempts/costs/human interactions; versus
native, gains must be at least losses and external tokens/calls per planned
task must not increase. Unknown usage cannot pass. A failed gate triggers a
recorded scope decision or separately qualified/spec-revised pruning follow-up;
retrieval speed alone cannot close it. M9's actual 144 successful Mac attempts
with zero local turns remain historical evidence, not M10 gains.

Delivery follows [ROADMAP.md](../ROADMAP.md): qualifications and merged core;
permitted 200-skill inputs; catalog/v2/API/security/lifecycle; qualified exact
installed-skill commands and actual Apple M1 8 GiB trial; 300-asset held-out
freeze/benchmark; hybrid/adaptation/permitted scheduled refresh; qualified MCP;
whole-task gates and actual packaged macOS/native x86-64 Windows acceptance.
Required dependencies still block work that depends on unavailable trial/data.
Linux fixtures, CI, WSL2 and old M9 acceptance cannot substitute for native
Windows, real human approvals or a qualified current MCP use path.

Documentation ships with each real feature. Future work includes schema/migration
and third-party launch/run/compare examples; source locks/NOTICE/authorship and
annotation instructions; operator first-import/degraded/refresh/recovery guides;
privacy storage/retention/inspection/reset limits; manual host installation and
bilingual command/skill/MCP walkthroughs. Component paths and runnable commands
must be marked planned until implemented. Final guides and evidence record
cannot describe blocked customer walkthroughs as completed.
