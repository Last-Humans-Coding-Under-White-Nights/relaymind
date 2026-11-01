# Row setup for the early installed-skill trial (DRAFT for owner review)

Every planned row runs once, headless, on the owner's Apple M1 8 GiB MacBook:

    openclaw agent exec "<frozen case task_text>" --model <arm model ref> --json \
      --config <arm host config> --cwd <new empty dir> --state-dir <new empty dir> \
      --timeout 300

- Host: pinned OpenClaw 2026.10.7 (npm ci of integrations/openclaw/package-lock.json),
  isolated OPENCLAW_HOME / OPENCLAW_STATE_DIR under the operator root.
- Arm host configuration: the frozen `host-<arm>.json`; native uses its own provider
  entry for the external model, provider_only and recommendations use the RelayMind
  plugin (relaymind/relaymind-client-tools) against their own gateway.
- Before each RelayMind row the operator stores that arm's current gateway application
  token with `openclaw models auth paste-api-key --provider relaymind` (standard input).
- Recommendations arm: before the turn the operator, as the person, asks for a
  recommendation and records an explicit choice through the host (Control UI
  RelayMind recommendations page); the choice receipt is row evidence. Exact skill
  loaded use stays disabled (pending negative record
  m10-negative-skill-loaded-use-openclaw-2026.10.7): the host loads skills natively.
- No retry outside the frozen budget; a failed or timed-out turn stays as it is.
