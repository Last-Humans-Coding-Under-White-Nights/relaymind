import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, it } from "vitest";
// @ts-expect-error native host loads this plain JS fixture unchanged
import observer from "../host-ordinary-qualification/observer/probe.mjs";

it.each([undefined, null])("RM39/AC30 records missing tool context %s without inventing authority", context => {
  const dir = mkdtempSync(join(tmpdir(), "ordinary-observer-"));
  const eventFile = join(dir, "events.jsonl");
  writeFileSync(eventFile, "");
  const original = globalThis.fetch;
  const key = Symbol.for("relaymind.ordinary.fixture.fetch-observer");
  let hook: (event: unknown, context?: unknown) => void;
  try {
    observer.register({ pluginConfig: { eventFile }, on: (name: string, handler: typeof hook) => {
      if (name === "after_tool_call") hook = handler;
    } });
    hook!({ toolName: "skills_read", toolCallId: "call", runId: "run", result: { content: "bytes" } }, context);
    const record = JSON.parse(readFileSync(eventFile, "utf8"));
    expect(record.context).toEqual({});
    expect(record.event.toolName).toBe("skills_read");
    expect(record.event.result).toEqual({ content: "bytes" });
  } finally {
    globalThis.fetch = original;
    delete (globalThis as any)[key];
    rmSync(dir, { recursive: true });
  }
});
