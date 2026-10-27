// Test only, never shipped (#137; RM08, RM33, AC24).
//
// Two disposable host tools for the host CLI tier of the RelayMind
// qualification (scripts/openclaw_qualification.py host --scenario ...):
//
// - relaymind_test_echo returns its text argument and does nothing else;
// - relaymind_test_write appends one line to a file in the directory named by
//   RELAYMIND_TEST_SANDBOX, the fresh empty directory the harness creates. It is
//   the mutation: a before_tool_call hook puts every call behind the host's own
//   approval (allow-once or deny), so the host, not RelayMind, decides.
//
// OpenClaw executes these tools; RelayMind only proposes the call. Plain
// JavaScript so the real host can load it from this directory without a build.
import { appendFileSync } from "node:fs";
import { basename, join } from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

export const ECHO = "relaymind_test_echo";
export const WRITE = "relaymind_test_write";

function text(value) {
  return { content: [{ type: "text", text: value }], details: {} };
}

export const echoTool = {
  name: ECHO,
  label: "RelayMind test echo",
  description: "Sandbox host tool: returns its text argument. Executes nothing else.",
  parameters: {
    type: "object",
    properties: { text: { type: "string", description: "The text to echo." } },
    required: ["text"],
    additionalProperties: false,
  },
  async execute(_toolCallId, params) {
    return text(`echo: ${params.text}`);
  },
};

export const writeTool = {
  name: WRITE,
  label: "RelayMind test write",
  description:
    "Sandbox host tool: appends one line to a file in the test sandbox (mutating; needs approval).",
  parameters: {
    type: "object",
    properties: {
      name: { type: "string", description: "The file name in the sandbox." },
      line: { type: "string", description: "The line to append." },
    },
    required: ["name", "line"],
    additionalProperties: false,
  },
  async execute(_toolCallId, params) {
    const sandbox = process.env.RELAYMIND_TEST_SANDBOX;
    if (!sandbox) throw new Error("RELAYMIND_TEST_SANDBOX is not set; nothing was written");
    // basename keeps the write inside the sandbox whatever the model proposed.
    const name = basename(params.name);
    appendFileSync(join(sandbox, name), `${params.line}\n`);
    return text(`wrote one line to ${name}`);
  },
};

/** The host's approval request for a write; RelayMind never answers it. */
export function beforeToolCall(event) {
  if (event.toolName !== WRITE) return undefined;
  return {
    requireApproval: {
      title: "RelayMind test write",
      description: `Append one line to ${String(event.params?.name)} in the test sandbox`,
      severity: "warning",
      allowedDecisions: ["allow-once", "deny"],
    },
  };
}

export default definePluginEntry({
  id: "relaymind-test-host-tools",
  name: "RelayMind test host tools",
  description: "Test only: disposable sandbox tools for the RelayMind host qualification",
  register(api) {
    api.registerTool(echoTool);
    api.registerTool(writeTool);
    api.on("before_tool_call", beforeToolCall);
  },
});
