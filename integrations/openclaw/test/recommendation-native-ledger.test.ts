// RM39; AC30/AC35 (spec 0.65 7.6.1, 7.6.2 conditions 1 and 6): the durable use
// ledger survives a restart and fails closed when its history is unreadable.
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import {
  appendFileSync,
  existsSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, it } from "vitest";
import {
  createFileUseLedger,
  type LedgerIo,
  type NativeUseLedger,
  nodeLedgerIo,
} from "../src/recommendation-native-ledger.js";

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

/**
 * A filesystem and process double over the real filesystem: it runs as the
 * given process identity, can run a hook before its n-th operation (to
 * interleave a second owner at a controlled point) and can cap the bytes one
 * write accepts (a short write).
 */
function double(
  pid: number,
  live: ReadonlySet<number>,
  options: {
    before?: (step: number, name: string) => void;
    writeCap?: number;
  } = {},
): LedgerIo & { steps: number } {
  const io = { ...nodeLedgerIo, pid, alive: (p: number) => live.has(p) };
  let steps = 0;
  const wrapped = { steps: 0 } as LedgerIo & { steps: number };
  for (const [name, value] of Object.entries(io)) {
    if (typeof value !== "function" || name === "alive") {
      Object.assign(wrapped, { [name]: value });
      continue;
    }
    Object.assign(wrapped, {
      [name]: (...args: unknown[]) => {
        options.before?.(steps, name);
        steps += 1;
        wrapped.steps = steps;
        if (name === "writeSync" && options.writeCap !== undefined) {
          const [fd, data, offset, length] = args as [
            number,
            string | Buffer,
            number | undefined,
            number | undefined,
          ];
          const buffer = typeof data === "string" ? Buffer.from(data) : data;
          const start = typeof data === "string" ? 0 : (offset ?? 0);
          const size =
            typeof data === "string"
              ? buffer.length
              : (length ?? buffer.length - start);
          return fs.writeSync(
            fd,
            buffer,
            start,
            Math.min(size, options.writeCap),
          );
        }
        return (value as (...a: unknown[]) => unknown)(...args);
      },
    });
  }
  wrapped.alive = io.alive;
  return wrapped;
}
function usable(ledger: NativeUseLedger): boolean {
  try {
    ledger.lookup("probe");
    return true;
  } catch {
    return false;
  }
}

/**
 * Review (stale-lock takeover): two restarting processes both find the lock
 * of an owner that no longer exists. Whatever the interleaving, at most one
 * may own the ledger, or the same key and the same approval could be
 * consumed twice; once both close, the path is free again.
 */
it.each([
  ["the fixed lock name of an earlier revision", ".lock", "1000\n"],
  ["a per-owner lock", ".lock.1000.0b6a1c1e-stale", ""],
])(
  "admits one owner when two processes take over %s left by an exited owner",
  (_, suffix, content) => {
    const live = new Set([1001, 1002, 1003]);
    // The number of operations one uncontended open performs.
    const probe = path();
    writeFileSync(probe + suffix, content);
    const counter = double(1001, live);
    createFileUseLedger(probe, {}, counter).close();
    const total = counter.steps;
    expect(total).toBeGreaterThan(3);
    let interleaved = 0;
    for (let step = 0; step < total; step++) {
      const file = path();
      writeFileSync(file + suffix, content);
      let second: NativeUseLedger | undefined;
      const first = createFileUseLedger(
        file,
        {},
        double(1001, live, {
          before: (n) => {
            if (n === step && !second)
              second = createFileUseLedger(file, {}, double(1002, live));
          },
        }),
      );
      // Past the end of this interleaving's shorter path.
      if (!second) {
        first.close();
        continue;
      }
      interleaved += 1;
      const owners = [first, second].filter(usable);
      expect(owners.length, `interleaving at step ${step}`).toBe(1);
      // The one owner consumes an approval exactly once.
      expect(owners[0]!.consumeApproval("approval")).toBe(true);
      first.close();
      second.close();
      const third = createFileUseLedger(file, {}, double(1003, live));
      expect(usable(third), `released after step ${step}`).toBe(true);
      expect(third.consumeApproval("approval")).toBe(false);
      third.close();
      expect(existsSync(file + suffix)).toBe(false);
    }
    expect(interleaved).toBeGreaterThan(total - 3);
  },
);

it("never removes a lock it did not inspect as stale", () => {
  const live = new Set([1001, 1002]);
  const file = path();
  const owner = createFileUseLedger(file, {}, double(1001, live));
  expect(usable(owner)).toBe(true);
  const intruder = createFileUseLedger(file, {}, double(1002, live));
  expect(usable(intruder)).toBe(false);
  intruder.close();
  // The refused opener released nothing of the live owner's.
  const later = createFileUseLedger(file, {}, double(1002, live));
  expect(usable(later)).toBe(false);
  later.close();
  owner.close();
  expect(
    readdirSync(join(file, "..")).filter((n) => n.includes(".lock")),
  ).toEqual([]);
});

/**
 * Review (short writes): writeSync may accept fewer bytes than asked. A
 * record is acknowledged only once all of it is written and synced, so
 * recovery keeps every acknowledged dispatch marker and block.
 */
it("writes a dispatch marker completely across short writes", () => {
  const file = path();
  const ledger = createFileUseLedger(
    file,
    {},
    double(process.pid, new Set([process.pid]), { writeCap: 5 }),
  );
  ledger.claim("k1", D, base);
  ledger.markDispatched("k1");
  ledger.close();
  const reopened = createFileUseLedger(file);
  expect(reopened.lookup("k1")).toMatchObject({ dispatched: true });
  expect(() => reopened.markDispatched("k1")).toThrow("ledger_dispatched");
  reopened.close();
});

it("writes a conversation block completely across short writes", () => {
  const file = path();
  const ledger = createFileUseLedger(
    file,
    {},
    double(process.pid, new Set([process.pid]), { writeCap: 3 }),
  );
  ledger.block("incarnation");
  expect(ledger.isBlocked("incarnation")).toBe(true);
  ledger.close();
  const reopened = createFileUseLedger(file);
  expect(reopened.isBlocked("incarnation")).toBe(true);
  reopened.close();
});

it("refuses and acknowledges nothing when a write makes no progress", () => {
  const file = path();
  const ledger = createFileUseLedger(
    file,
    {},
    double(process.pid, new Set([process.pid]), { writeCap: 0 }),
  );
  expect(() => ledger.block("incarnation")).toThrow("ledger_write_stalled");
  // The ledger can no longer prove its history: every operation refuses.
  expect(() => ledger.isBlocked("incarnation")).toThrow("ledger_unavailable");
  expect(() => ledger.consumeApproval("a")).toThrow("ledger_unavailable");
  ledger.close();
});
