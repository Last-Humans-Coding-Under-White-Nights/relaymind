# Pinned-host MCP qualification (#190)

Test-only qualification for RM08, RM09, RM12, RM16, RM33 and AC24 under spec
0.50. This fixture does not implement M10 recommendation behavior.

`qualify.py` starts an isolated real OpenClaw Gateway and a real RelayMind
gateway over deterministic model doubles. OpenClaw starts and owns both
stdio MCP servers. Only OpenClaw's embedded tool executor calls their `echo`
tools. The Python harness never creates an MCP client or launches an MCP
server itself. The fixture imports the already installed MCP SDK 1.32.0;
there is no additional product dependency.

`probe.mjs` registers the existing `createRelayMindProvider` with a passive
`inspectToolSchemas` decorator. It delegates existing hooks unchanged and uses
public `getPluginToolMeta` from `openclaw/plugin-sdk/agent-harness-runtime`
on the same tool object as its normalized `.parameters`. Generic names have
`source: unproven`. The provider is otherwise unchanged. Explicit fixture
`hooks.allowConversationAccess: true` enables a second passive hook that
attempts `api.runtime.gateway.request("tools.effective", {sessionKey})` using
its actual host context. The third-party plugin's refusal is recorded separately
from the authenticated operator CLI's successful inventory reads.

Both configured servers expose original tool `echo`, with names `alpha.one`
and `alpha?one` that collide after sanitization. The double reads the exact
host-offered tool name from the captured provenance, proposes one call, and
answers its marker only from the current call's returned result. Each server
records invocation count, arguments, marker and original listed schema in the
disposable sandbox. A server changes its schema and emits `tools/list_changed`;
then disconnects. The driver also changes host deny policy and removes a
server. Captures retain raw CLI responses, traces, schemas, transport diagnostics
and per-exchange model requests. No `tools.invoke` policy bypass is used.

Run from the repository on Linux with pinned npm dependencies installed:

```bash
npm run build --prefix integrations/openclaw
mkdir -p /var/tmp/relaymind-mcp-tmp
TMPDIR=/var/tmp/relaymind-mcp-tmp PYTHONPATH=src \
  python integrations/openclaw/test/host-mcp-qualification/qualify.py \
  --work-dir /var/tmp/relaymind-mcp-new \
  --out /var/tmp/relaymind-mcp-capture.json
```

The work directory must be new and outside the checkout. It contains private
runtime credentials and raw logs; only the credential-scanned export belongs
in evidence. The export replaces machine and sandbox paths and refuses to
write if any generated gateway token or configured MCP environment credential
occurs anywhere in it. Component tests also exercise configured header values
in that export guard. No HTTP MCP credential flow was exercised here.

A `schema_sha256` hashes the UTF-8 `JSON.stringify` bytes of the captured schema.
A tool without `parameters` records `schema` and `schema_sha256` as null, never
an invented empty schema.
It is a derived content identity. It is never a host-issued catalog or
availability revision. `availability`, `host_revision`, and the schema hook's
`session` remain unknown/null even after a successful call. `capture_window`
records serial harness correlation only, not an atomic host session binding.

The [recorded result](../../../../docs/evidence/2026-10-29-openclaw-mcp-qualification/README.md)
separates proven transport/provenance/schema behavior from the unqualified
current availability/session/revision contract and approval coverage.
