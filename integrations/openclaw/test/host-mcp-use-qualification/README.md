# Disposable MCP use capability qualification (#204)

RM39 / AC35, specification sections 7.6.1 and 12.6.5. This separate tier leaves
the historical #190 harness and evidence unchanged. It changes no adapter,
qualification registry, gateway executor or runtime snapshot contract.

Run from the repository root after installing development dependencies,
`npm ci`, building a release tarball, and installing Playwright Chromium:

```sh
PYTHONPATH=src:tests python integrations/openclaw/test/host-mcp-use-qualification/qualify.py \
  --tarball /absolute/path/to/relaymind-openclaw-provider-0.1.0.tgz \
  --work-dir /absolute/path/to/new-disposable-directory \
  --out /absolute/path/to/capture.json
```

The person trial inherits the ordinary installed-entry harness: real disposable
signed-session accounts, paired native Control UI, default gateway registration,
the unchanged installed provider, and authenticated public gateway dispatch.
It requires Linux x86-64 with an assigned private IPv4 interface. Other machines
emit a blocked capture with the original setup reason. `--preflight-only --out
capture.json` writes an explicit no-run record without launching a host.
This tier limits the person trial to 180 seconds; the Ubuntu CI step has a
four-minute timeout and uploads a separate capture artifact. Existing person
captures took 43–65 seconds, with up to 30 seconds added for the MCP probe; the
outer limit leaves a minute for cleanup/export. An understood unavailable
environment or completed, healthy discovery with an unsupported exact-use route
returns zero with `status: blocked`; that exit code is not qualification.
Harness/fixture errors return nonzero and fail CI; missing artifacts also fail
the upload. A failed run names what failed: `person_setup_failed` when setup failed before discovery, `fixture_failed` when discovery failed, and `person_proof_failed` when discovery was healthy but the person proof failed. The nested capture keeps its own reason.

Two disposable stdio servers, `alpha.one` and `alpha?one`, both advertise
`append`. OpenClaw owns their launch, transport and inventory. The fixture logs
original schemas and every server call, including rejected calls; it appends at
most one bounded JSON string to its own sandbox per valid call and records its
counter/result in a separate `effect` record joined to the `call` by `call_id`.
Host-only exposed-name/schema/session/requester fields come from a separate
`host_calls` receipt, never from the server. Revision 2 adds a bounded optional field, so stale-schema tests can
distinguish exact authorization from argument validity. No input accepts a path.
The host CLI probes both servers. The pinned host lists MCP tools in
`tools.effective` only from the session's own MCP runtime; without one it
returns an `mcp-not-yet-connected` notice. So, in its original authenticated
request, the person probe first calls public `mcp.app.discover` with the same
session target. That acquires the session's MCP runtime and awaits its catalog;
it creates no App view and calls no tool. The probe records the outcome, with
only fixed host refusal messages, and a refusal never stops the inventory read.
For this route the fixture host enables MCP Apps and selects the shipped
`relaymind/relaymind-client-tools` model (the default `openai` model selects an
unregistered harness, which `mcp.app.discover` refuses), with a zero heartbeat.
No approval or tool policy changes. The person probe then calls
`tools.effective` through `dispatchGatewayMethod`. The pinned host returns `{groups: [{tools: [entry]}], notices?}`, with
entries keyed by `id` and MCP entries carrying `mcpServer`/`mcpToolName`. The
probe records only method, status, an explicit `inventory` outcome (`listed`,
`empty`, `malformed` or `none`), bounded tool IDs, the host's own MCP
server/original mapping (and `deniedBySession`) and notice IDs. Any other
shape is `malformed`, never an empty list. It does not parse an exposed name to
manufacture provenance. Discovery passes only when one `tools.effective`
inventory lists both colliding `append` tools under distinct exposed names, not
session-denied, and no inventory is malformed.
The same authenticated scope probes `mcp.app.listTools` with no view ID and
records names from its MCP `tools/list` shape; it does not invent a view for an independent tool. This
negative route probe cannot establish that all other host compositions fail.

The current driver stops at that discovery checkpoint. It has no qualified
public exact-use receipt resolver binding the independent tool to a native
runtime/session/requester and final server I/O. It does not manufacture an App
view, dispatch a name-only mutation, add permissive approval configuration, or
substitute an unrelated approval policy. The fixed reason
`no_qualified_exact_use_receipt_resolver` describes this qualification gap; it is
not a claim that the host has no approval or MCP APIs.

`approval_ui.py` supplies a bounded Playwright driver for an already genuine
native plugin approval card. It checks the request ID, session, exact displayed
detail, allowed decision and expiration, opens the native modal, and checks the
binding again in the same evaluated browser task that observes identity and
clicks Allow once or Deny. It requires the observed request to leave the queue.
It never calls an approval resolver or changes approval
state directly. Its action is labeled `automated_ui`; a click is not a native
resolution receipt. An unrun driver is labeled `not_run`, with the available
automation separately labeled `automated_ui`. Human accounting requires an
explicit observed interaction and does not establish acceptance by itself.
No exact bound request is available in the current driver,
so actual allow/deny, stale schema/server/session cases, replay/cancel/timeout,
and native/provider/recommendation baseline comparison remain `not_run` with
`exact_route_unqualified`. Human approval is always `not_run` in this tier.

`grade.py` reopens pinned source files, requires expected build outputs even if
`dist/` is absent, and binds the pre-launch tarball digest to the installation
receipt. These are local byte checks, not proof by themselves that those bytes
executed. Nested person/registration evidence must carry this tier's matching
fixture hashes and a checkout revision; unrelated historical captures earn no
credit. This source binding does not authenticate a capture or prevent replay
of a capture made with identical bytes.
The grader can count gateway execution when a capture supplies bounded run
traces, using the published TraceEvent types. This tier currently exports no run
traces, so `zero_gateway_execution` stays false. Missing traces cannot establish
zero execution. Accounting helpers independently
check both schema hash forms, provenance, intended server/file/result, one-shot
effects, and zero-call rejection for each negative control (stale binding,
policy, identity, replay, cancellation, timeout, expiry and revocation); their
synthetic unit inputs are not native receipts. The exact-use, policy/currentness,
approval and baseline terms
remain false until a reviewed raw native resolver exists. Consequently
`mcp_use_pass(capture)` currently cannot pass. Caller `checks`, `passed`,
`qualified` or summary counters cannot lift that block.

Raw private logs and disposable state stay in the work directory; only the
structured capture is uploaded after a final scan using the original person and
gateway secret exclusions. Intermediate captures stay in a private temporary
directory. The person evidence remains nested. Do not publish raw scratch logs or treat fixture automation as human,
whole-task, quality, M1/macOS or native Windows acceptance. No production MCP
activation follows from this tooling, even after a future functional pass.

Verification:

```sh
PYTHONPATH=src:tests:scripts RELAYMIND_OPENCLAW_NODE=1 python -m pytest \
  tests/test_openclaw_mcp_use_qualification.py -q -p no:cacheprovider -o addopts=""
```

The opt-in host smoke test launches both real servers through the pinned host
and requires original list evidence and zero calls/writes. Browser tests use a
DOM fixture to verify driver behavior; they do not claim a live native approval.
