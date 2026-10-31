import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, it, vi } from "vitest";
vi.mock("openclaw/plugin-sdk/plugin-entry", () => ({ definePluginEntry: (entry: unknown) => entry }));
// @ts-expect-error qualification observer is a native JavaScript plugin
import plugin from "./probe.mjs";

it("RM39/AC30 passively retains the actual skills_read bytes and native context across reloads", () => {
  const work = mkdtempSync(join(tmpdir(), "rm-observer-"));
  const path = join(work, "events.jsonl");
  writeFileSync(path, "");
  const original = globalThis.fetch;
  const key = Symbol.for("relaymind.ordinary.fixture.fetch-observer");
  const previous = (globalThis as any)[key];
  try {
    for (let index = 0; index < 2; index++) {
      const hooks = new Map<string, (...args: any[]) => void>();
      plugin.register({ pluginConfig: { eventFile: path }, on: (name: string, callback: (...args: any[]) => void) => hooks.set(name, callback) });
      expect(hooks.has("after_tool_call")).toBe(true);
      const context = { runId: "host-run", sessionKey: "s", sessionId: "sid", toolCallId: "call", requester: { secret: "omit" } };
      hooks.get("after_tool_call")!({ toolName: "other", result: "ignore" }, context);
      hooks.get("after_tool_call")!({ toolName: "skills_read", toolCallId: "call", runId: "host-run", params: { name: "selected" }, result: { content: [{ type: "text", text: "Whole bytes 中文\n" }] } }, context);
    }
    const records = readFileSync(path, "utf8").trim().split("\n").map(line => JSON.parse(line));
    expect(records).toHaveLength(2);
    expect(records[0].event.result.content[0].text).toBe("Whole bytes 中文\n");
    expect(records[0].context).toEqual({ runId: "host-run", sessionKey: "s", sessionId: "sid", toolCallId: "call" });
  } finally {
    globalThis.fetch = original;
    (globalThis as any)[key] = previous;
    rmSync(work, { recursive: true, force: true });
  }
});

it("RM39/AC30 refuses oversized retained tool content before appending any bytes", () => {
  const work = mkdtempSync(join(tmpdir(), "rm-observer-bound-"));
  const path = join(work, "events.jsonl");
  writeFileSync(path, "");
  const original = globalThis.fetch;
  const key = Symbol.for("relaymind.ordinary.fixture.fetch-observer");
  const previous = (globalThis as any)[key];
  try {
    const hooks = new Map<string, (...args: any[]) => void>();
    plugin.register({ pluginConfig: { eventFile: path }, on: (name: string, callback: (...args: any[]) => void) => hooks.set(name, callback) });
    expect(() => hooks.get("after_tool_call")!({ toolName: "skills_read", result: { content: "x".repeat(1048576) } }, {})).toThrow("capture_bound");
    expect(readFileSync(path, "utf8")).toBe("");
    writeFileSync(path, "x".repeat(8388600));
    expect(() => hooks.get("after_tool_call")!({ toolName: "skills_read", result: {} }, {})).toThrow("capture_bound");
    expect(readFileSync(path).length).toBe(8388600);
  } finally {
    globalThis.fetch = original;
    (globalThis as any)[key] = previous;
    rmSync(work, { recursive: true, force: true });
  }
});
