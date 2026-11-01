// RM39/AC35 fixture unit test. These direct fixture calls are not host qualification.
import {mkdtempSync, readFileSync, rmSync, writeFileSync} from "node:fs";
import {setTimeout as sleep} from "node:timers/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {fileURLToPath} from "node:url";
import {Client} from "@modelcontextprotocol/sdk/client/index.js";
import {StdioClientTransport} from "@modelcontextprotocol/sdk/client/stdio.js";
import {expect, test} from "vitest";

test("sandbox append records exactly one effect and rejects paths and oversized input", async () => {
  const work = mkdtempSync(join(tmpdir(), "rm-mcp-unit-"));
  const control = join(work, "control.json"), events = join(work, "events.jsonl");
  const sandbox = join(work, "sandbox.jsonl");
  writeFileSync(control, JSON.stringify({server: "alpha.one", revision: 1}));
  const client = new Client({name: "fixture-unit", version: "1"});
  const transport = new StdioClientTransport({command: "node", args: [
    fileURLToPath(new URL("./server.mjs", import.meta.url)), control, events, sandbox,
  ]});
  try {
    await client.connect(transport);
    const list = await client.listTools();
    expect(list.tools[0]?.name).toBe("append");
    expect(list.tools[0]?.annotations?.readOnlyHint).toBe(false);
    const result = await client.callTool({name: "append", arguments: {text: "one"}});
    expect(result.content).toEqual([{type: "text", text: "alpha.one:1"}]);
    expect(readFileSync(sandbox, "utf8")).toBe('"one"\n');
    const refused = await client.callTool({name: "append", arguments: {text: "two", path: "/outside"}});
    expect(refused.isError).toBe(true);
    expect((await client.callTool({name: "append", arguments: {text: "x".repeat(257)}})).isError).toBe(true);
    expect(readFileSync(sandbox, "utf8")).toBe('"one"\n');
    const records = readFileSync(events, "utf8").trim().split("\n").map(line => JSON.parse(line));
    expect(records.filter(row => row.kind === "call")).toHaveLength(3);
    expect(records.filter(row => row.kind === "effect")).toHaveLength(1);
  } finally {
    await client.close();
    rmSync(work, {recursive: true, force: true});
  }
});

test("survives a transient control read and keeps the last good state", async () => {
  const work = mkdtempSync(join(tmpdir(), "rm-mcp-control-"));
  const control = join(work, "control.json"), events = join(work, "events.jsonl");
  const sandbox = join(work, "sandbox.jsonl");
  const state = JSON.stringify({server: "alpha.one", revision: 1});
  writeFileSync(control, state);
  const client = new Client({name: "fixture-unit", version: "1"});
  const transport = new StdioClientTransport({command: "node", args: [
    fileURLToPath(new URL("./server.mjs", import.meta.url)), control, events, sandbox,
  ]});
  try {
    await client.connect(transport);
    // A concurrent rewrite can expose a partial or absent control file.
    writeFileSync(control, "{not json");
    await sleep(300);
    rmSync(control);
    await sleep(300);
    writeFileSync(control, state);
    const result = await client.callTool({name: "append", arguments: {text: "one"}});
    expect(result.content).toEqual([{type: "text", text: "alpha.one:1"}]);
  } finally {
    await client.close();
    rmSync(work, {recursive: true, force: true});
  }
});

test("revision two bounds the optional tag field", async () => {
  const work = mkdtempSync(join(tmpdir(), "rm-mcp-tag-"));
  const control = join(work, "control.json"), events = join(work, "events.jsonl");
  const sandbox = join(work, "sandbox.jsonl");
  writeFileSync(control, JSON.stringify({server: "alpha.one", revision: 2}));
  const client = new Client({name: "fixture-unit", version: "1"});
  const transport = new StdioClientTransport({command: "node", args: [
    fileURLToPath(new URL("./server.mjs", import.meta.url)), control, events, sandbox,
  ]});
  try {
    await client.connect(transport);
    const list = await client.listTools();
    expect(list.tools[0]?.inputSchema.properties?.tag).toMatchObject({
      type: "string", maxLength: 256,
    });
    const oversized = await client.callTool({
      name: "append", arguments: {text: "one", tag: "x".repeat(257)},
    });
    expect(oversized.isError).toBe(true);
    expect((await client.callTool({
      name: "append", arguments: {text: "one", tag: "x"},
    })).isError).toBeFalsy();
  } finally {
    await client.close();
    rmSync(work, {recursive: true, force: true});
  }
});
