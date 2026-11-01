// RM39; AC30/AC35 (spec 0.65 section 7.6.1 and 7.6.2 conditions 1 and 6): the
// durable use ledger. An admitted key is bound to its request digest at
// admission; a dispatch marker and a consumed allow-once approval are written
// and synced before native dispatch, so a restart can neither replay a
// possible mutation nor reuse an approval, and a blocked conversation stays
// blocked. One live owner per path (an exclusive lock file). History is never
// evicted: a full or unreadable ledger refuses (fail closed); blocks use
// reserved capacity, and managed use refuses once no block could be recorded.
// It stores digests and closed receipts only, never arguments, tool output,
// instructions or credentials.
import {
  closeSync,
  existsSync,
  unlinkSync,
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
  /** True once a dispatch marker was synced: native dispatch may have begun. */
  readonly dispatched: boolean;
  readonly receipt: McpUseReceipt | undefined;
}
export interface NativeUseLedger {
  lookup(key: string): UseHistory | undefined;
  /** Binds an admitted key durably; throws when it exists or the ledger is full. */
  claim(key: string, digest: string, base: UseBase): void;
  /** Durable before native dispatch may begin; once per bound key. */
  markDispatched(key: string): void;
  settle(key: string, receipt: McpUseReceipt): void;
  /** True once only, durably, for each approval identity. */
  consumeApproval(approvalId: string): boolean;
  block(incarnation: string): void;
  isBlocked(incarnation: string): boolean;
  /** Releases the descriptor; every later operation refuses. Idempotent. */
  close(): void;
}
type Record =
  | { t: "claim"; k: string; d: string; b: UseBase }
  | { t: "dispatch"; k: string }
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

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}
/** Exclusive ownership: a lock file naming a live owner refuses a second one. */
function acquire(lock: string): void {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const fd = openSync(lock, "wx", 0o600);
      try {
        writeSync(fd, String(process.pid) + "\n");
        fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
      syncDirectory(lock);
      return;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      const owner = Number.parseInt(readFileSync(lock, "utf8"), 10);
      // A live owner, including this process, keeps the path.
      if (!Number.isSafeInteger(owner) || owner <= 0 || alive(owner))
        throw Error("ledger_owned");
      unlinkSync(lock);
    }
  }
  throw Error("ledger_owned");
}

/** A private append-only JSON-lines ledger at an operator/host-owned path. */
export function createFileUseLedger(
  path: string,
  limits: { maxRecords?: number; maxBytes?: number; maxBlocks?: number } = {},
): NativeUseLedger {
  const maxRecords = limits.maxRecords ?? 100000;
  const maxBytes = limits.maxBytes ?? 64 * 1024 * 1024;
  // Blocks never compete with bindings and approvals for capacity.
  const maxBlocks = limits.maxBlocks ?? 10000;
  const lock = path + ".lock";
  let locked = false;
  const history = new Map<string, UseHistory>();
  const approvals = new Set<string>();
  const blocked = new Set<string>();
  // Records and bytes of bindings and approvals; markers, receipts and
  // blocks use the capacity a binding reserves or the separate block reserve.
  let records = 0;
  let bytes = 0;
  let usable = true;
  let fd: number | undefined;
  const created = !existsSync(path);
  try {
    acquire(lock);
    locked = true;
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
      const record = JSON.parse(line) as Record;
      apply(record);
      if (counted(record)) {
        records += cost(record);
        bytes += reserve(Buffer.byteLength(line, "utf8") + 1, record);
      }
    }
  } catch {
    usable = false;
    close();
  }
  function counted(record: Record): boolean {
    return record.t === "claim" || record.t === "approval";
  }
  /** A binding reserves its dispatch marker and its receipt. */
  function cost(record: Record): number {
    return record.t === "claim" ? 3 : 1;
  }
  function reserve(size: number, record: Record): number {
    return record.t === "claim" ? size + 2048 : size;
  }
  /** Throws for a record that recovery would reject; changes nothing. */
  function validate(record: Record): void {
    switch (record?.t) {
      case "claim":
        if (
          typeof record.k !== "string" ||
          typeof record.d !== "string" ||
          history.has(record.k)
        )
          throw Error("ledger_claimed");
        return;
      case "dispatch": {
        const previous = history.get(record.k);
        if (!previous) throw Error("ledger_unclaimed");
        if (previous.dispatched) throw Error("ledger_dispatched");
        if (previous.receipt) throw Error("ledger_settled");
        return;
      }
      case "settle": {
        const previous = history.get(record.k);
        if (!previous) throw Error("ledger_unclaimed");
        if (previous.receipt) throw Error("ledger_settled");
        return;
      }
      case "approval":
        if (typeof record.a !== "string") throw Error("ledger_invalid");
        return;
      case "block":
        if (typeof record.i !== "string") throw Error("ledger_invalid");
        return;
      default:
        throw Error("ledger_invalid");
    }
  }
  function apply(record: Record): void {
    try {
      validate(record);
    } catch {
      throw Error("ledger_unreadable");
    }
    switch (record.t) {
      case "claim":
        history.set(record.k, {
          digest: record.d,
          base: record.b,
          dispatched: false,
          receipt: undefined,
        });
        return;
      case "dispatch":
        history.set(record.k, { ...history.get(record.k)!, dispatched: true });
        return;
      case "settle":
        history.set(record.k, { ...history.get(record.k)!, receipt: record.r });
        return;
      case "approval":
        approvals.add(record.a);
        return;
      case "block":
        blocked.add(record.i);
        return;
    }
  }
  function ready(): number {
    if (!usable || fd === undefined) throw Error("ledger_unavailable");
    return fd;
  }
  function append(record: Record): void {
    const handle = ready();
    // Never write a record that recovery would reject.
    validate(record);
    const line = JSON.stringify(record) + "\n";
    const size = Buffer.byteLength(line, "utf8");
    if (
      counted(record) &&
      (records + cost(record) > maxRecords ||
        bytes + reserve(size, record) > maxBytes)
    )
      throw Error("ledger_full");
    // A block is never refused for capacity: new managed use refuses at the
    // block limit instead (isBlocked), so a required stop is never lost.
    try {
      writeSync(handle, line);
      fsyncSync(handle);
    } catch (error) {
      // An unknown write state cannot prove history either way.
      usable = false;
      throw error;
    }
    if (counted(record)) {
      records += cost(record);
      bytes += reserve(size, record);
    }
    apply(record);
  }
  function close(): void {
    usable = false;
    if (fd !== undefined) {
      const handle = fd;
      fd = undefined;
      try {
        closeSync(handle);
      } catch {
        /* Already released. */
      }
    }
    if (locked) {
      locked = false;
      try {
        unlinkSync(lock);
      } catch {
        /* Already removed. */
      }
    }
  }
  return {
    lookup(key) {
      ready();
      return history.get(key);
    },
    claim(key, digest, base) {
      append({ t: "claim", k: key, d: digest, b: base });
    },
    markDispatched(key) {
      append({ t: "dispatch", k: key });
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
      if (blocked.has(incarnation)) return true;
      // A later fence failure could not be recorded: refuse managed use.
      if (blocked.size >= maxBlocks) throw Error("ledger_full");
      return false;
    },
    close,
  };
}
