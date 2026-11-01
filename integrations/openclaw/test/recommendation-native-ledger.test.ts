// RM39; AC30/AC35 (spec 0.65 7.6.1, 7.6.2 conditions 1 and 6): the durable use
// ledger survives a restart and fails closed when its history is unreadable.
import {
  mkdtempSync,
  readFileSync,
  statSync,
  writeFileSync,
  appendFileSync,
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
function path(): string {
  return join(mkdtempSync(join(tmpdir(), "relaymind-ledger-")), "use.jsonl");
}

it("persists claims, receipts, consumed approvals and blocks across a reopen", () => {
  const file = path();
  const ledger = createFileUseLedger(file);
  ledger.claim("k1", "d".repeat(64), base);
  ledger.claim("k2", "e".repeat(64), base);
  ledger.settle("k2", receipt);
  expect(ledger.consumeApproval("approval-1")).toBe(true);
  expect(ledger.consumeApproval("approval-1")).toBe(false);
  ledger.block("incarnation");
  const reopened = createFileUseLedger(file);
  expect(reopened.lookup("k1")).toEqual({
    digest: "d".repeat(64),
    base,
    receipt: undefined,
  });
  expect(reopened.lookup("k2")?.receipt).toEqual(receipt);
  expect(reopened.lookup("k3")).toBeUndefined();
  expect(reopened.consumeApproval("approval-1")).toBe(false);
  expect(reopened.isBlocked("incarnation")).toBe(true);
  expect(() => reopened.claim("k1", "d".repeat(64), base)).toThrow();
  if (process.platform !== "win32")
    expect(statSync(file).mode & 0o777).toBe(0o600);
  expect(readFileSync(file, "utf8")).not.toContain("arguments");
});

it("drops only a torn final record, which was never acknowledged", () => {
  const file = path();
  createFileUseLedger(file).claim("k1", "d".repeat(64), base);
  appendFileSync(file, '{"t":"claim","k":"k2"');
  const reopened = createFileUseLedger(file);
  expect(reopened.lookup("k1")).toBeDefined();
  expect(reopened.lookup("k2")).toBeUndefined();
  reopened.claim("k3", "d".repeat(64), base);
  expect(createFileUseLedger(file).lookup("k3")).toBeDefined();
});

it("fails closed on every operation when a complete record is unreadable", () => {
  const file = path();
  writeFileSync(file, "not json\n");
  const ledger = createFileUseLedger(file);
  expect(() => ledger.lookup("k1")).toThrow();
  expect(() => ledger.claim("k1", "d".repeat(64), base)).toThrow();
  expect(() => ledger.consumeApproval("a")).toThrow();
  expect(() => ledger.isBlocked("i")).toThrow();
});

it("refuses new claims when full instead of evicting history", () => {
  const ledger = createFileUseLedger(path(), { maxRecords: 2 });
  ledger.claim("k1", "d".repeat(64), base);
  ledger.claim("k2", "d".repeat(64), base);
  expect(() => ledger.claim("k3", "d".repeat(64), base)).toThrow("ledger_full");
  expect(ledger.lookup("k1")).toBeDefined();
});

// Review 4435447955: a record that recovery would reject is never written.
it("refuses a second settle or an unclaimed settle without writing anything", () => {
  const file = path();
  const ledger = createFileUseLedger(file);
  ledger.claim("k1", "d".repeat(64), base);
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
  // The ledger stays usable in process and after a restart.
  ledger.claim("k2", "d".repeat(64), base);
  const reopened = createFileUseLedger(file);
  expect(reopened.lookup("k1")?.receipt).toEqual(receipt);
  expect(reopened.lookup("k2")).toBeDefined();
});

it("still refuses every operation when recovery finds a duplicate settle", () => {
  const file = path();
  const ledger = createFileUseLedger(file);
  ledger.claim("k1", "d".repeat(64), base);
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
  expect(() => ledger.claim("k1", "d".repeat(64), base)).toThrow();
  expect(() => ledger.consumeApproval("a")).toThrow();
});
