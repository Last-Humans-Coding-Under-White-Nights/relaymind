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
the call. Status: the shape is unit-tested (`test/host-tool-plugin.test.ts`);
loading it into a real host is not run on this machine (operator or CI step,
see `integrations/openclaw/README.md`).
