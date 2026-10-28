# RelayMind test host tools (test only)

Two disposable host tools for the host CLI tier of the RelayMind OpenClaw
qualification (#137; RM08, RM33, AC24). Never packed with the provider and
never installed outside a test host.

- `relaymind_test_echo`: returns its text argument.
- `relaymind_test_write`: appends one line to a file in `$RELAYMIND_TEST_SANDBOX`
  (the empty directory `scripts/openclaw_qualification.py host` creates). A
  `before_tool_call` hook puts every call behind the host's own approval
  (`allow-once` or `deny`).

OpenClaw executes both tools under its own policy; RelayMind only proposes
the call. The manifest declares both tools in `contracts.tools`: without it
the pinned host refuses to register them (macOS run at `a52f8bf`). The host
offers them to the model only with `tools.toolSearch false` and the two
names in `tools.alsoAllow` (or `tools.profile full`).

The approval needs an approval surface. Headless `openclaw agent exec` has
none: in the Linux VPS functional check (not a supported-platform
qualification,
`docs/evidence/2026-10-28-openclaw-vps-functional-137/`) the host blocked
every write with "Plugin approval unavailable: non-interactive CLI runs have
no approval-capable initiating surface", so the headless allow path uses the
host's own `exec` tool (`--scenario exec-full`). An approval answered by an
operator at a running OpenClaw Gateway is not_run. The shape is unit-tested
in `test/host-tool-plugin.test.ts`; install and remove steps are in
`integrations/openclaw/README.md`.
