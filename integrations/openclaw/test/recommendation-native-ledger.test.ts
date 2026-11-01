// RM39; AC30/AC35 (spec 0.65 7.6.1, 7.6.2 conditions 1 and 6): the durable use
// ledger survives a restart and fails closed when its history is unreadable.
import { spawnSync } from "node:child_process";
import {
  appendFileSync,
  existsSync,
  mkdtempSync,
  readFileSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, it } from "vitest";
import { createFileUseLedger } from "../src/recommendation-native-ledger.js";

const base = {
  operationId: "operation-1",
  recommendationId: "4774d146-17bd-4ec9-8507-f528ccea51fa",
  assetId: "mcp:a",
  contentHash: "c".repeat(64),
};
const receipt = {
  ...base,
  outcome: "completed",
  code: "ok",
  nativeRunId: "run",
  nativeCallId: "call",
} as const;
const D = "d".repeat(64);
function path(): string {
  return join(mkdtempSync(join(tmpdir(), "relaymind-ledger-")), "use.jsonl");
}

it("persists bindings, dispatch markers, receipts, approvals and blocks across a restart", () => {
  const file = path();
  const ledger = createFileUseLedger(file);
  ledger.claim("k1", D, base);
  ledger.claim("k2", "e".repeat(64), base);
  ledger.markDispatched("k2");
  ledger.settle("k2", receipt);
  ledger.claim("k3", D, base);
  ledger.markDispatched("k3");
  expect(ledger.consumeApproval("approval-1")).toBe(true);
  expect(ledger.consumeApproval("approval-1")).toBe(false);
  ledger.block("incarnation");
  ledger.close();
  const reopened = createFileUseLedger(file);
  expect(reopened.lookup("k1")).toEqual({
    digest: D,
    base,
    dispatched: false,
    receipt: undefined,
  });
  expect(reopened.lookup("k2")).toMatchObject({ dispatched: true, receipt });
  expect(reopened.lookup("k3")).toMatchObject({
    dispatched: true,
    receipt: undefined,
  });
  expect(reopened.lookup("k4")).toBeUndefined();
  expect(reopened.consumeApproval("approval-1")).toBe(false);
  expect(reopened.isBlocked("incarnation")).toBe(true);
  expect(() => reopened.claim("k1", D, base)).toThrow();
  expect(() => reopened.markDispatched("k2")).toThrow("ledger_dispatched");
  expect(() => reopened.markDispatched("missing")).toThrow("ledger_unclaimed");
  if (process.platform !== "win32")
    expect(statSync(file).mode & 0o777).toBe(0o600);
  expect(readFileSync(file, "utf8")).not.toContain("arguments");
  reopened.close();
});

it("drops only a torn final record, which was never acknowledged", () => {
  const file = path();
  const first = createFileUseLedger(file);
  first.claim("k1", D, base);
  first.close();
  appendFileSync(file, '{"t":"claim","k":"k2"');
  const reopened = createFileUseLedger(file);
  expect(reopened.lookup("k1")).toBeDefined();
  expect(reopened.lookup("k2")).toBeUndefined();
  reopened.claim("k3", D, base);
  reopened.close();
  expect(createFileUseLedger(file).lookup("k3")).toBeDefined();
});

it("fails closed on every operation when a complete record is unreadable", () => {
  const file = path();
  writeFileSync(file, "not json\n");
  const ledger = createFileUseLedger(file);
  expect(() => ledger.lookup("k1")).toThrow();
  expect(() => ledger.claim("k1", D, base)).toThrow();
  expect(() => ledger.consumeApproval("a")).toThrow();
  expect(() => ledger.isBlocked("i")).toThrow();
});

it("refuses new bindings when full instead of evicting history", () => {
  // A binding reserves room for its dispatch marker and its receipt.
  const ledger = createFileUseLedger(path(), { maxRecords: 5 });
  ledger.claim("k1", D, base);
  expect(() => ledger.claim("k2", D, base)).toThrow("ledger_full");
  ledger.markDispatched("k1");
  ledger.settle("k1", receipt);
  expect(ledger.lookup("k1")?.receipt).toEqual(receipt);
  ledger.close();
});

// Review 4435447955: a record that recovery would reject is never written.
it("refuses a second settle or an unclaimed settle without writing anything", () => {
  const file = path();
  const ledger = createFileUseLedger(file);
  ledger.claim("k1", D, base);
  ledger.settle("k1", receipt);
  const before = readFileSync(file, "utf8");
  expect(() =>
    ledger.settle("k1", {
      ...receipt,
      outcome: "indeterminate",
      code: "transport_unknown",
    }),
  ).toThrow("ledger_settled");
  expect(() => ledger.settle("missing", receipt)).toThrow("ledger_unclaimed");
  expect(readFileSync(file, "utf8")).toBe(before);
  ledger.claim("k2", D, base);
  ledger.close();
  const reopened = createFileUseLedger(file);
  expect(reopened.lookup("k1")?.receipt).toEqual(receipt);
  expect(reopened.lookup("k2")).toBeDefined();
  reopened.close();
});

it("still refuses every operation when recovery finds a duplicate settle", () => {
  const file = path();
  const ledger = createFileUseLedger(file);
  ledger.claim("k1", D, base);
  ledger.settle("k1", receipt);
  ledger.close();
  appendFileSync(
    file,
    JSON.stringify({ t: "settle", k: "k1", r: receipt }) + "\n",
  );
  expect(() => createFileUseLedger(file).lookup("k1")).toThrow();
});

// Review 4435447999: the ledger owns one descriptor and releases it.
it("closes its descriptor and refuses every later operation", () => {
  const ledger = createFileUseLedger(path());
  ledger.close();
  ledger.close();
  expect(() => ledger.lookup("k1")).toThrow("ledger_unavailable");
  expect(() => ledger.claim("k1", D, base)).toThrow();
  expect(() => ledger.consumeApproval("a")).toThrow();
});

/**
 * Spec 0.65 7.6.2 condition 1 ("never dispatch again") and condition 6
 * ("Allow-once is consumed at most once") need one live owner: a second
 * handle with its own in-memory view could claim the same key or consume the
 * same approval again.
 */
it("refuses a second live owner of the same ledger path", () => {
  const file = path();
  const first = createFileUseLedger(file);
  const second = createFileUseLedger(file);
  expect(() => second.lookup("same-key")).toThrow("ledger_unavailable");
  expect(() => second.consumeApproval("approval")).toThrow();
  first.claim("same-key", D, base);
  expect(first.consumeApproval("approval")).toBe(true);
  first.close();
  // The first owner's history is intact and a new owner may open it.
  const third = createFileUseLedger(file);
  expect(third.lookup("same-key")).toBeDefined();
  expect(third.consumeApproval("approval")).toBe(false);
  third.close();
  expect(existsSync(file + ".lock")).toBe(false);
});

it("takes over the lock of an owner process that no longer exists", () => {
  const file = path();
  const exited = spawnSync(process.execPath, ["-e", ""]).pid;
  writeFileSync(file + ".lock", String(exited) + "\n");
  const ledger = createFileUseLedger(file);
  ledger.claim("k1", D, base);
  ledger.close();
});

/**
 * Review: a full ledger must not lose a conversation block. Spec 0.65 7.6.1:
 * "If unload is unavailable, stop further RelayMind-managed use in that
 * conversation and require a fresh session". Blocks use reserved capacity,
 * and managed use refuses once no further block could be recorded.
 */
it("records a block even when bindings and approvals filled the ledger", () => {
  const file = path();
  const ledger = createFileUseLedger(file, { maxRecords: 1 });
  expect(ledger.consumeApproval("approval")).toBe(true);
  expect(() => ledger.consumeApproval("other")).toThrow("ledger_full");
  ledger.block("incarnation");
  expect(ledger.isBlocked("incarnation")).toBe(true);
  ledger.close();
  expect(
    createFileUseLedger(file, { maxRecords: 1 }).isBlocked("incarnation"),
  ).toBe(true);
});

it("refuses managed use once no further block could be recorded", () => {
  const ledger = createFileUseLedger(path(), { maxBlocks: 1 });
  expect(ledger.isBlocked("a")).toBe(false);
  ledger.block("a");
  expect(() => ledger.isBlocked("b")).toThrow("ledger_full");
  ledger.close();
});
