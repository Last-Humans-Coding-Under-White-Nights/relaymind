# Direct-command qualification probe (test only)

This disposable fixture qualifies existing OpenClaw surfaces for #189 against
RM08, RM11, RM12, RM16, RM19, RM33 and AC24. It is never packed with the provider.
It adds no RelayMind product command or feedback authorization design.

With the pinned dependencies installed and provider built (`npm ci` and
`npm run build` in `integrations/openclaw`), run from the checkout:

```bash
PYTHONPATH=src python integrations/openclaw/test/host-command-qualification/qualify.py \
  --work-dir /tmp/relaymind-command-new \
  --out /tmp/relaymind-command-capture.json
```

`--work-dir` must not exist. The probe creates its own home, config, workspace,
Gateway, application credential and recorder credential. It uses the real
OpenClaw `gateway call chat.send` ingress and the real RelayMind gateway over
model doubles. No acquired skill runs. The only skill read is an authored
sandbox marker; it has no binaries, dependencies, network use or mutations.
The host's own skill catalog and default tool search stay enabled. The script
removes the sandbox skill to exercise the missing-file guard and stops its host.
The disposable directory retains private credentials; keep it local and remove
it after inspection. The export rejects any occurrence of those credentials.

`/rmqualification recommend` and `/rmqualification choose 2` record one action
using a credential captured privately by the handler. Host context must match
an explicit fixture registration. The credential is absent from the command
arguments, descriptions and model tool schemas. This demonstrates a separate
credential channel within a trusted host process. It provides no isolation
from that process or an unrestricted host shell. The CLI application ID is not
a verified person's identity.

The two `library` actions test different host APIs and retain the exact upstream
boundary error only for the two recognized static error prefixes. Unrecognized
errors stay generic. `api.runtime.gateway.request` is restricted to bundled or
trusted official plugins. `dispatchGatewayMethod` instead needs an authenticated
plugin HTTP/RPC request scope and `contracts.gatewayMethodDispatch`. Declaring
that contract does not grant a slash-command handler the request scope.

The file hash guard rejects changed or missing content before recording a
choice. A later independent native skill invocation proves file loading with a
model double. These steps do not prove that the choice command attaches a
managed immutable revision, or that a name-only read is atomic with the guard.
The library identity failure remains a failed qualification.

`probe.test.ts` exercises the plugin component with a local recorder; it is
separate from real ingress evidence. `tests/test_openclaw_command_qualification.py`
checks that the evidence grader requires observed action/session/call facts and
an actual assistant answer. Neither suite is a real-person approval test.

Observed outcomes and source identities are in
[`docs/evidence/2026-10-29-openclaw-command-qualification`](../../../../docs/evidence/2026-10-29-openclaw-command-qualification/README.md).

To characterize headless approval independently, after the command probe:

```bash
PYTHONPATH=src python integrations/openclaw/test/host-command-qualification/headless.py \
  --host-home /tmp/relaymind-command-new \
  --work-dir /tmp/relaymind-headless-new \
  --out /tmp/relaymind-headless-capture.json
```

This creates another disposable home/state/sandbox, explicitly admits the two
sandbox tools and keeps native tool search enabled. It observes headless refusal;
it cannot answer an approval or turn that refusal into a human deny decision.
