# OpenClaw MCP provenance and tool exposure — 29 October 2026

Issue #190 supplies bounded evidence for #187 under spec 0.50 (RM08, RM09,
RM12, RM16, RM33; AC24). The run uses real OpenClaw 2026.10.7 on Linux x86-64,
Node 24.22.1, MCP SDK 1.32.0, the unchanged RelayMind provider and real gateway,
and deterministic model doubles. It supplies functional integration evidence;
live-model quality, customer completion and supported-platform acceptance are
not measured here. No production behavior or specification changes.

| Surface | Result | Evidence and limit |
| --- | --- | --- |
| Configured servers | passed | Host `mcp status --json` reports two disposable stdio definitions. Configuration alone is not reachability. |
| MCP connection/listing | passed | Host `mcp probe --json` opens real connections and lists both tools. Probe alone is not execution. |
| Host-owned round trips | passed | Embedded Gateway host executes each tool through the provider, returns its server-specific marker to the model double, and publishes that marker. Server invocation records, actual model requests and gateway traces agree; gateway execution count is zero. |
| Duplicate original names and sanitization collision | passed | `alpha.one` and `alpha?one` both expose original `echo`. Authentic metadata maps exact exposed names `alpha-one__echo` and `alpha-one-2__echo` to distinct servers. Each intended server executes once initially. No identity is reconstructed by splitting names. |
| Public same-instance schema/provenance | passed | Public provider `inspectToolSchemas` receives concrete tools. Public `getPluginToolMeta` on those same instances provides server/original-tool metadata alongside normalized parameters. Exact schemas and derived content hashes are captured. |
| Schema refresh | passed | First server changes required `text:string` to `count:integer` and sends `tools/list_changed`; next host generation exposes the changed schema and executes `count:2` successfully. Original listed schemas remain in server events. |
| Disconnect | unavailable as observed | Host retains the last normalized schema and offers its name; an actual proposal gets a transport failure and causes zero server invocations. Inventory includes a transport diagnostic. A visible schema is not current availability. |
| Host policy/removal | passed, bounded | Denying `bundle-mcp` hides both tools; restoring policy and removing the second server keeps its exact tool absent. Neither test invokes the excluded server. First-after-change and subsequent inventory results are preserved; no atomic update guarantee is inferred. |
| Operator effective inventory | passed | Authenticated `tools.effective` for the actual session returns server/original/exposed mapping and notices. Published RPC shape omits full schemas and catalog revision. Its underlying session catalog retains stale tools during recovery. |
| Third-party plugin inventory access | refused | With explicit conversation-hook permission, real `before_prompt_build` context supplies the session. `api.runtime.gateway.request` refuses the plugin as neither bundled nor trusted official. No operator scope is impersonated or borrowed. |
| Atomic session/turn/current revision | unsupported by qualified composition | The schema hook has no authoritative session/turn/catalog-revision field. Separate hook/CLI observations and capture ordering do not prove a use-time availability guarantee. Schema observations keep session and host revision null and availability unknown. |
| Host credentials | passed, bounded | OpenClaw supplies a configured random environment credential to its server children; only presence is recorded. Both host/gateway tokens and that credential are absent from the scanned export. HTTP header/OAuth transport was not run. |
| Interactive approval | not_run | Fixtures are read-only and annotated as such. #189's separate headless refusal is not a human allow/deny pass; later #204 approval acceptance remains open. |

The integration decision is partial. #187 can rely on real host-managed MCP
execution, collision-safe names, and supported same-instance normalized
schema/provenance observation. It cannot treat those observations as a current,
scoped recommendation-to-use snapshot. Before #192/#204 MCP implementation,
provide a supported host integration exposing an immutable tool snapshot with
session/requester/turn identity and a host-issued revision whose availability
and permissions are revalidated at use. The existing third-party plugin also
needs a supported authorized route to that capability. Qualification does not
establish that a host version upgrade alone supplies it.

`report.json` contains the capability summary, commands, source hashes and raw
capture digest. `mcp-capture.json.gz` contains the complete sanitized capture:
original schemas, passive observations, CLI inventory/probe responses, model
requests, host history/log, fixture execution records and RelayMind traces.
Source hashes identify the actual driver, fixture, built provider, dependency
lock and inspected pinned host surfaces used in the final capture. The separate
offline grader has its own source hash in `report.json` and re-reads raw final
history, model results, trace proposals, captured provenance and server events;
the original runtime source hashes and checks are preserved. After review,
`regrade.json` re-ran the hardened grader on the unchanged capture with the
same checks; it records the current grader and harness source hashes, and the
host was not re-run. Derived
schema/capture hashes are not host-issued availability revisions.

Reproduce using the [fixture instructions](../../../integrations/openclaw/test/host-mcp-qualification/README.md).
The driver uses authenticated Gateway `chat.send` to keep the actual embedded
session alive across catalog changes and read that same session's inventory.
The host owns all MCP connections, credentials, execution and policy. No
marketplace tools, gateway MCP client, new production dependency or private host
import is introduced. The fixtures do not run mutations or answer approvals.
