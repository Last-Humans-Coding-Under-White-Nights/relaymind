# Native person-ingress fixture

This opt-in Ubuntu x86-64 harness tests disposable account login through
AuthCrunch, the bundled OpenClaw Control UI's native device pairing, and a
registered read-only RPC on that same authenticated browser socket. It uses
OpenClaw 2026.10.7 and the AuthCrunch 1.5.0 Linux distribution. The archive's
pinned SHA-256 is
`86a1ed8446bca803149bdcf1335005b840033a17c41680a067d7de11dfbaeca9`.
The [official release](https://github.com/authcrunch/authcrunch/releases/tag/v1.5.0)
and [local-store walkthrough](https://docs.authcrunch.com/docs/start/first-app)
describe the distribution and supported account provisioning/login flow.

Run after installing the repository's Python development dependencies and pinned
OpenClaw npm dependencies:

```bash
playwright install --with-deps --only-shell chromium
python integrations/openclaw/test/host-person-qualification/qualify.py \
  --work-dir /var/tmp/relaymind-person-fixture-new \
  --out ci-evidence/openclaw-person-ingress.json
```

The work directory must be new. The harness detects an actual UP interface with
an assigned RFC1918 IPv4 address and binds the proxy only to that address. The
native gateway listens on loopback. The browser connects to the private proxy
using a fixture hostname; the proxy derives identity from the verified signed
session and forwarding from the actual TCP peer. No supplied address or forged
forwarding value can stand in for this setup. If private networking is absent,
the requested integration exits 2 with `setup_unavailable` and all planned
observations retained as `not_run`. It starts no listener in that case.

Two randomly credentialed, operator-provisioned local accounts perform the real
username/password portal flow. Each receives its own browser context and native
profile/session. The test checks unauthenticated/forged-cookie/forged-identity
refusal, distinct native profiles, cross-account session refusal, and native
connection-currentness refusal after a real browser disconnect during a bounded
held RPC. The socket observer does not construct another client, modify or replay
connect/auth/device frames, extract keys, disable CSP, or silently retry through
a replacement connection. The exact observation is **programmatic fixture RPC on
a native authenticated Control UI socket**.

Execution has a 570-second active deadline and bounded child cleanup. The CI step
allows 12 minutes so failure evidence and cleanup can finish. Original console,
provider store, keys, cookies, configuration and browser state stay in the private
work directory. Upload only the sanitized output JSON through `ci-evidence/`;
never upload that work directory or unrestricted browser traces. A credential
export refusal retains safe step outcomes and discards the rejected payload.

Successful fixture mechanics can support the separately reviewed authentication
contract. They do not establish signup, a genuine customer/human preference or
approval, direct-command UX, exact selected Skill Library or MCP use, a qualified
production adapter, task-quality/cost success, or native customer-platform
acceptance. Connection retirement here is distinct from full profile/grant
revocation, reset, erasure and restart coverage. Those observations remain
separate; `production_adapter_qualified` stays false. No production provider,
command registration, managed selection, runtime snapshot or action authority is
enabled by this fixture.
