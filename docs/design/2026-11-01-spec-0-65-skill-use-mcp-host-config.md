# Spec 0.65: skill use, MCP use and host configuration evidence

Date: 1 November 2026. Status: non-normative rationale for the proposed
specification baseline. The specification is authoritative; this document
records decisions, not newly measured host support or an acceptance result.

## Purpose and requirements

Resolve the #239 loaded-use ambiguity, define the #204 MCP activation conditions,
and make #240 configuration validation meaningful. The mapping is RM16, RM34,
RM37, RM39, RM40 and RM44; AC25, AC28, AC30, AC31 and AC35–AC37. Existing direct
actions and exact skill selection remain distinct from instruction use and tool
execution. Provider ownership, managed selection, catalog breadth, native
baseline, privacy and every full M10 gate remain unchanged.

The investigation and independent cross-check briefs for #239 and #204 inspected
OpenClaw 2026.10.7 and the current adapter. They supplied source-level findings,
not new native runtime observations. The existing [ordinary-host record](../evidence/m10/openclaw-native-actions-20261101-skill.json)
proves selection/read with instructions:not_observed. The earlier [#190
record](../evidence/2026-10-29-openclaw-mcp-qualification/README.md) proves bounded
real-host MCP round trips with model doubles; it does not prove an exact
RelayMind choice-to-execution bridge or human approval.

## Decisions and alternatives

### Immutable skill pins and later use — sections 1, 7.6 and 7.6.1; AC30

A library head update could have been defined to revoke every earlier pin. That
would fight deliberate immutable version selection and confuse a changed head
with changed selected bytes. The chosen rule preserves R0 while its exact
RelayMind choice, mapping and authority remain current; a subsequent managed
load must deliver R0, never silently R1.

Native removal retains revision bytes for already pinned sessions. Treating
that retention as permission would defeat RelayMind removal and erasure rules.
The chosen rule therefore blocks further RelayMind-managed use after entry
removal, choice expiry/erasure/remapping, changed eligibility or mapping,
registration retirement, session reset or lost authority. It does not claim to
revoke the host's independent native pin or erase past conversation content.
A fresh session is required where old delivered text cannot be unloaded.

Selection advertises a catalog entry; the actual native skills_read/read result
must reach the next actual model request before instruction loading is observed.
The contract compares whole instruction bytes, binds native revision and read
target, and requires session incarnation, native run, tool-call, provider request,
RelayMind run and terminal task correlation. A marker, initial prompt path or
skill.used event alone is not enough. A prior manifest-verified RPC does not
prove that a later mutable-file read delivered the same bytes.

Deterministic models can verify native loading and authority controls without
claiming model capability. The revision explicitly labels that functional tier
and the separate live-model capability tier. Both need their own actual results;
neither supplies missing platform/human/whole-task acceptance.

A previous RPC scope is closed before a later turn. Hooks do not preserve it,
and a logged hook error need not stop execution. Loaded-use activation therefore
requires a separately qualified real authority/fence at loading and model
delivery, with observed intervening invalidation controls. Until that exists,
selection/read remains useful and loaded-use remains disabled.

### Explicit MCP use — sections 4.6 and 7.6.2; AC35

An implicit future model turn would require an additional delegation protocol.
The chosen initial contract is a separate explicit person request after a
committed discovery choice. The reserved relaymind.mcp request carries explicit
arguments and expected session incarnation, not invented server/schema identity
or a model-origin action. It has closed receipts/errors, a finite 60-second
operation deadline including approval, at-most-once dispatch recovery and honest
indeterminate outcomes. That deadline is a design limit, not a measurement; no
existing gateway, model or direct-action budget changes.

Public authenticated dispatch is a verified ingress facility, not proof of an
exact MCP endpoint. Name-only tools.invoke cannot bind the required tuple.
mcp.app.callTool has genuine requester/view/session and approval guards but is
App-specific; no view is fabricated and ordinary independent tools are not
silently replaced with App-only fixtures. A passing qualification must identify
the actual supported public route and native handle, ownership, invalidation,
release and final-I/O enforcement. If the pinned host cannot supply that
composition, activation remains blocked.

The ten normative conditions cover operation delivery, genuine ingress, public
route, exact immutable identity, native lifetime, use/approval fences, choice and
registration lifetime, runtime discovery, qualification/privacy, and supported
scope. Schema changes invalidate the binding even when old arguments remain
valid. Canonical RelayMind schema hashes remain distinct from captured
JSON.stringify hashes and native authorization identities. All waits, including
approval and reconnect, require currentness checks and actual final host
fencing; earlier HTTP validation is never called a distributed transaction.

Native Control UI and CLI approval mechanisms exist, but their availability does
not prove the required person authority or MCP composition. Driving the actual
native UI automatically can yield useful functional evidence of decisions and
effects. It cannot supply a human allow/deny observation. Transport, App versus
ordinary tool, model and platform evidence remain separately scoped.

The revision defines the contract while retaining the passing-qualification
gate. It changes no product RPC implementation or qualification registry. Runtime
snapshots and runtime recommendations still need a separate normative contract
and qualification. No gateway MCP client/executor, fork/upgrade, private import,
synthetic identity or milestone reduction is authorized.

### Native configuration validation — section 12.6.5

Digest equality and JSON/TOML parsing prove neither a valid host configuration
nor its execution-effective settings. A generic schema validator is also
insufficient where regex dialect, refinements, plugins or defaults differ.
The chosen requirement uses the pinned OpenClaw config validate path and captures
its schema, validator, input, result and extraction identities. The schema uses
ECMAScript regex semantics; Python re or skipped unsupported patterns cannot be
called equivalent validation.

Private validation and source-bound redaction must establish how the exported
preimage relates to the actual validated input. Secrets and secret hashes remain
excluded. If that relation cannot be verified, the input stays unverified.
Provider, sampling and native budget documents have no universal standalone
schema supplied by this revision; each needs its actual parser/schema identity
or a verified extraction from the validated parent. Arbitrary settings bags stay
unverified and block qualified acceptance.

## Implementation and verification boundary

The only production-code edit in this revision adds the exact revised spec bytes
as a reviewed generator anchor, retaining all previous anchors. A focused
RM40/AC31 verifier test checks current protocol inventory acceptance and rejects
unreviewed spec bytes; the existing comparison suite exercises report behavior.
The test is run against the revised spec before the anchor is added, then after.
Requested comparison/evidence tests and lint/format checks run before the local
commit, with full-suite results and any environment limits recorded in the worker
report. No host-use implementation, host launch or new measured evidence is part
of this revision.
