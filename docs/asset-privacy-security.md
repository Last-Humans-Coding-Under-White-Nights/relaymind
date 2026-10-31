# Asset sources, privacy and recovery

[English setup](asset-selection.md) · [简体中文入口](asset-selection.zh-CN.md)

This reference covers independent recommendations and their current backend
boundaries (RM34/RM36–RM38/RM42/RM43; AC25/AC27–AC29/AC33/AC34).
The [specification](SPECIFICATION.md) is authoritative. A recommendation,
benchmark choice receipt or source's instructions never grant host authority.

## Bring a library without executing it

Use an explicit `local_directory` source for an ordinary root/immediate-child
SKILL.md library, or `metadata_import` for a closed, original-hash-verified skill
and MCP tool inventory. Paths resolve from the configuration directory for these
sources. Imports retain original identities, schema hashes and provenance; they
do not fetch locator URLs or unpack/install/execute archives. See the
[complete local/import examples and bounds](TESTING.md#independent-asset-catalog-discovery-192-rm34ac25).

Local scan defaults to 30 seconds. Explicit metadata imports are captured at
startup or by the internal owner, with failed-job backoff; there is no public
rescan/rebuild API. Failed, incomplete, unsafe or cancelled stages retain the
last good catalog with actual stale/exclusion evidence. Deletion requires
source-owned removal evidence, not an absent page in a failed fetch. Keep
originals and private source proof in authorized storage.

Only installed, qualified remote profiles accept automatic remote acquisition.
The current fixed `github.relaymind-skills.v1` profile is described in
[source refresh](TESTING.md#qualified-public-source-refresh-spec-059-rm34rm38rm43).
It does not authorize arbitrary repositories, URLs or adapter imports. Its
mandatory polling floor survives restart/rename; configured cadence and failure
backoff also apply. Matrix development imports do not qualify complete market
refresh. Safe transport/projection checks do not establish rights for unknown
sources; do not publish restricted originals because a hash was verified.

## Know what leaves each boundary

The v2 selector receives the full eligible safe projection plus task/conversation,
scope, permissions, revision and deadline. Whole private skill bodies, labels,
credentials and correction history are not a v2 public projection. Public source
metadata may still identify packages/tools, so treat complete benchmark exports
as private unless their redistribution is authorized. Formal companions use
explicit resolver access for restricted material; null paths are not permission
to guess a local file. See [export handling](ASSET_BENCHMARK.md#built-in-team-a-and-team-b-on-the-same-fixture).

The store retains recommendation requests, views, events and historical receipts;
it is not a public artifact. Protect its directory and the separate deterministic
`<resolved store path>.erasure-key` sibling. The key is not exported with SQLite;
restore it consistently with its instance store. Missing, replaced or unsafe keys
fail closed, with no automatic rotation or unkeyed fallback. Logical deletion is
not forensic wiping of disk, backups or someone else's already-loaded context.

## Credentials and actual action availability

Every recommendation request needs the current application bearer printed at
startup. Restart rotates that token. Backend choice/feedback/inspection/delete/
reset additionally require a separate `X-RelayMind-User-Action-Token` bound to an
operator registration. The bearer cannot enroll that channel; no public
registration or model-callable action helper exists.

The current recommendation backend refuses enabled production action
registrations: no qualified person/session/host action contract is activated.
Writing a contract reference into a JSON file is insufficient. Do not treat the
benchmark `choose` CLI as enrollment or backend feedback. Its stdout receipt is
separate from the report and executes nothing. The later installed skill/MCP,
allow/deny and correction walkthrough must use its delivered host contract.

[Backend operator details](TESTING.md#backend-direct-actions-and-feedback-privacy-195-rm37ac28)
describe the existing protected registration format and deterministic-test seam.
Use owner-only regular files (0600 on POSIX; verified owner/DACL on native
Windows), distinct high-entropy action credentials and hashed gateway records.
Never reuse the application bearer, log tokens or put them in model context.
Revocation/rotation requires stopping the old gateway, replacing its protected
registration and restarting; it does not revoke copies held by another process.

## Expiry, corrections and recovery

| Situation | Current meaning |
| --- | --- |
| Same idempotency key/body | Historical POST receipt, not renewed authorization |
| 600 seconds after ready | Choice expires; GET current state before using a candidate |
| Source/candidate changed | Current exact content/source/schema/policy checks still apply |
| Interrupted computation at restart | Becomes failed once; no selector retry hidden in recovery |
| 30 days from creation | Parent, events, feedback and owned receipts become absent and are purged |
| Explicit correction | Separate scoped feedback; choice alone is not correction or success |
| Delete/reset feedback | Independent feedback-revision CAS, synchronous owned derived invalidation, content-free audit/tombstones |

Backend feedback delete/reset does not undo a host side effect, unload a copied
conversation or erase an independently retained parent request. Reset retains
chosen/cancelled lifecycle state. Erased feedback aliases cannot recover erased
payloads through idempotency replay. Inspect the current state/feedback revision
before a qualified client issues a mutation; this guide supplies no enabled
production credential recipe.

For a normal restart, stop the gateway cleanly, retain the same protected store
and key, start the same configuration, use the newly printed bearer and GET the
existing recommendation ID. Read readiness's state/reason and poll the existing
[JSON lifecycle/events](TESTING.md#recommendation-json-lifecycle-194-rm36ac27).
There is no public recommendation compute-cancel endpoint at this baseline.

## Evidence and remaining work

[Source diagnostics](SOURCE_BENCHMARK.md#recorded-development-results) retain
actual misses, primary timeouts, service floors and unknown costs. The
[versioned public-source record](evidence/2026-10-30-github-skill-source/README.md)
separates acquisition from the actual two scheduled product jobs and fixture
failure/recovery tests. None establishes a whole-market or platform/resource pass.
The production semantic qualification registry contains only the two exact
reviewed Linux CPU identities for the pinned multilingual bundle; other
artifacts, runtimes and devices remain unqualified. The native benchmark
comparison records that exact scope. The M10 installed host/approval/correction
walkthrough, independent quality
and whole-task evaluation, and operational macOS/native Windows gates remain
specific separate deliverables in [ROADMAP](ROADMAP.md#m10--multilingual-asset-recommendations-spec-063).
