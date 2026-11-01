// RM39; AC30/AC35 (spec 0.65 section 7.6.1 and 7.6.2 conditions 1 and 6): the
// durable use ledger. A key claim and a consumed allow-once approval are
// written and synced before native dispatch, so a restart can neither replay a
// possible mutation nor reuse an approval; a blocked conversation stays
// blocked. History is never evicted: a full or unreadable ledger refuses
// (fail closed). It stores digests and closed receipts only, never
// arguments, tool output, instructions or credentials.
import {
  closeSync,
  existsSync,
  fstatSync,
  fsyncSync,
  ftruncateSync,
  openSync,
  readFileSync,
  writeSync,
} from "node:fs";
import { dirname } from "node:path";
import type { McpUseReceipt } from "./recommendation-native-mcp.ts";

export type UseBase = Pick<
  McpUseReceipt,
  "operationId" | "recommendationId" | "assetId" | "contentHash"
>;
export interface UseHistory {
  readonly digest: string;
  readonly base: UseBase;
  readonly receipt: McpUseReceipt | undefined;
}
export interface NativeUseLedger {
  lookup(key: string): UseHistory | undefined;
  /** Durable before return; throws when the key exists or the ledger is full. */
  claim(key: string, digest: string, base: UseBase): void;
  settle(key: string, receipt: McpUseReceipt): void;
  /** True once only, durably, for each approval identity. */
  consumeApproval(approvalId: string): boolean;
  block(incarnation: string): void;
  isBlocked(incarnation: string): boolean;
}
type Record =
  | { t: "claim"; k: string; d: string; b: UseBase }
  | { t: "settle"; k: string; r: McpUseReceipt }
  | { t: "approval"; a: string }
  | { t: "block"; i: string };

function syncDirectory(path: string): void {
  try {
    const fd = openSync(dirname(path), "r");
    try {
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
  } catch {
    /* Not supported for directories on every platform. */
  }
}

/** A private append-only JSON-lines ledger at an operator/host-owned path. */
export function createFileUseLedger(
  path: string,
  limits: { maxRecords?: number; maxBytes?: number } = {},
): NativeUseLedger {
  const maxRecords = limits.maxRecords ?? 100000;
  const maxBytes = limits.maxBytes ?? 64 * 1024 * 1024;
  const history = new Map<string, UseHistory>();
  const approvals = new Set<string>();
  const blocked = new Set<string>();
  let records = 0;
  let bytes = 0;
  let usable = true;
  let fd: number | undefined;
  const created = !existsSync(path);
  try {
    fd = openSync(path, "a+", 0o600);
    if (created) syncDirectory(path);
    const text = readFileSync(path, "utf8");
    const end = text.lastIndexOf("\n") + 1;
    // An unterminated final record was never synced and acknowledged.
    if (end < text.length) {
      ftruncateSync(fd, Buffer.byteLength(text.slice(0, end), "utf8"));
      fsyncSync(fd);
    }
    for (const line of text.slice(0, end).split("\n")) {
      if (!line) continue;
      apply(JSON.parse(line) as Record);
      records++;
    }
    bytes = fstatSync(fd).size;
  } catch {
    usable = false;
  }
  function apply(record: Record): void {
    switch (record?.t) {
      case "claim":
        if (
          typeof record.k !== "string" ||
          typeof record.d !== "string" ||
          history.has(record.k)
        )
          throw Error("ledger_unreadable");
        history.set(record.k, {
          digest: record.d,
          base: record.b,
          receipt: undefined,
        });
        return;
      case "settle": {
        const previous = history.get(record.k);
        if (!previous || previous.receipt) throw Error("ledger_unreadable");
        history.set(record.k, { ...previous, receipt: record.r });
        return;
      }
      case "approval":
        if (typeof record.a !== "string") throw Error("ledger_unreadable");
        approvals.add(record.a);
        return;
      case "block":
        if (typeof record.i !== "string") throw Error("ledger_unreadable");
        blocked.add(record.i);
        return;
      default:
        throw Error("ledger_unreadable");
    }
  }
  function ready(): number {
    if (!usable || fd === undefined) throw Error("ledger_unavailable");
    return fd;
  }
  function append(record: Record): void {
    const handle = ready();
    const line = JSON.stringify(record) + "\n";
    const size = Buffer.byteLength(line, "utf8");
    if (records + 1 > maxRecords || bytes + size > maxBytes)
      throw Error("ledger_full");
    try {
      writeSync(handle, line);
      fsyncSync(handle);
    } catch (error) {
      // An unknown write state cannot prove history either way.
      usable = false;
      throw error;
    }
    records++;
    bytes += size;
    apply(record);
  }
  return {
    lookup(key) {
      ready();
      return history.get(key);
    },
    claim(key, digest, base) {
      ready();
      if (history.has(key)) throw Error("ledger_claimed");
      append({ t: "claim", k: key, d: digest, b: base });
    },
    settle(key, receipt) {
      ready();
      append({ t: "settle", k: key, r: receipt });
    },
    consumeApproval(approvalId) {
      ready();
      if (approvals.has(approvalId)) return false;
      append({ t: "approval", a: approvalId });
      return true;
    },
    block(incarnation) {
      ready();
      if (!blocked.has(incarnation)) append({ t: "block", i: incarnation });
    },
    isBlocked(incarnation) {
      ready();
      return blocked.has(incarnation);
    },
  };
}
