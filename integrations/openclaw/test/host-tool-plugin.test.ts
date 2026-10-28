// The test-only host tool plugin (#137; RM08, RM33, AC24).
//
// The host CLI tier installs it next to the provider so a real OpenClaw host
// executes the sandbox tools under its own approval. Here only its shape is
// checked: it registers the two tools and the approval hook, the write tool
// stays inside the sandbox, and nothing in it talks to RelayMind.
import { mkdtempSync, readdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

// @ts-expect-error index.js is plain JavaScript without declarations
import plugin, { beforeToolCall, echoTool, writeTool } from "./host-tool-plugin/index.js";

const ROOT = new URL("./host-tool-plugin/", import.meta.url);

afterEach(() => {
  delete process.env.RELAYMIND_TEST_SANDBOX;
});

describe("the test host tool plugin", () => {
  it("registers the echo and write tools and the approval hook", () => {
    const tools: { name: string }[] = [];
    const hooks: string[] = [];
    plugin.register({
      registerTool: (tool: { name: string }) => tools.push(tool),
      on: (name: string) => hooks.push(name),
    });
    expect(tools.map((t) => t.name)).toEqual(["relaymind_test_echo", "relaymind_test_write"]);
    expect(hooks).toEqual(["before_tool_call"]);
  });

  it("declares in its manifest every tool it registers (contracts.tools)", () => {
    // The pinned host refuses registerTool from a plugin whose manifest lacks
    // contracts.tools: "plugin must declare contracts.tools before registering
    // agent tools" (macOS qualification at a52f8bf, Q9).
    const manifest = JSON.parse(readFileSync(new URL("openclaw.plugin.json", ROOT), "utf8"));
    const tools: { name: string }[] = [];
    plugin.register({
      registerTool: (tool: { name: string }) => tools.push(tool),
      on: () => undefined,
    });
    expect(manifest.contracts?.tools).toEqual(tools.map((t) => t.name));
  });

  it("puts every write behind the host's approval and leaves echo alone", () => {
    const request = beforeToolCall({ toolName: "relaymind_test_write", params: { name: "n" } });
    expect(request.requireApproval.allowedDecisions).toEqual(["allow-once", "deny"]);
    expect(beforeToolCall({ toolName: "relaymind_test_echo", params: {} })).toBeUndefined();
  });

  it("echo returns its argument", async () => {
    const result = await echoTool.execute("call_1", { text: "lantern" });
    expect(result.content).toEqual([{ type: "text", text: "echo: lantern" }]);
  });

  it("write appends one line inside the sandbox only", async () => {
    const sandbox = mkdtempSync(join(tmpdir(), "rm-host-tools-"));
    process.env.RELAYMIND_TEST_SANDBOX = sandbox;
    await writeTool.execute("call_1", { name: "../escape.txt", line: "one" });
    expect(readdirSync(sandbox)).toEqual(["escape.txt"]);
    expect(readFileSync(join(sandbox, "escape.txt"), "utf8")).toBe("one\n");
  });

  it("write refuses to run without a sandbox", async () => {
    await expect(writeTool.execute("call_1", { name: "n", line: "x" })).rejects.toThrow(
      /RELAYMIND_TEST_SANDBOX/,
    );
  });

  it("is a separate, test-only package that never talks to the gateway", () => {
    const pkg = JSON.parse(readFileSync(new URL("package.json", ROOT), "utf8"));
    expect(pkg.private).toBe(true);
    expect(pkg.openclaw.extensions).toEqual(["./index.js"]);
    const source = readFileSync(new URL("index.js", ROOT), "utf8");
    expect(source).not.toMatch(/fetch\(|RELAYMIND_APP_TOKEN|RELAYMIND_BASE_URL/);
  });
});
