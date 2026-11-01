// RM39; AC30/AC35 (spec 0.65 section 7.6.1 and 7.6.2 conditions 1 and 6): the
// durable use ledger. An admitted key is bound to its request digest at
// admission; a dispatch marker and a consumed allow-once approval are written
// and synced before native dispatch, so a restart can neither replay a
// possible mutation nor reuse an approval, and a blocked conversation stays
// blocked. One live owner per path (exclusive per-owner lock files). History is never
// evicted: a full or unreadable ledger refuses (fail closed); blocks use
// reserved capacity, and managed use refuses once no block could be recorded.
// It stores digests and closed receipts only, never arguments, tool output,
// instructions or credentials.
import * as fs from "node:fs";
import { randomUUID } from "node:crypto";
import { basename, dirname, join } from "node:path";
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
/**
 * The filesystem and process operations the ledger uses. Production uses
 * node:fs and this process; tests pass a double to control interleavings and
 * short writes.
 */
export interface LedgerIo {
  readonly pid: number;
  alive(pid: number): boolean;
  readonly closeSync: typeof fs.closeSync;
  readonly existsSync: typeof fs.existsSync;
  readonly fsyncSync: typeof fs.fsyncSync;
  readonly ftruncateSync: typeof fs.ftruncateSync;
  readonly openSync: typeof fs.openSync;
  readonly readFileSync: typeof fs.readFileSync;
  readonly readdirSync: typeof fs.readdirSync;
  readonly unlinkSync: typeof fs.unlinkSync;
  readonly writeSync: typeof fs.writeSync;
}
type Record =
  | { t: "claim"; k: string; d: string; b: UseBase }
  | { t: "dispatch"; k: string }
  | { t: "settle"; k: string; r: McpUseReceipt }
  | { t: "approval"; a: string }
  | { t: "block"; i: string };

function lineBytes(record: Record): number {
  return Buffer.byteLength(JSON.stringify(record), "utf8") + 1;
}
function markerLine(key: string): Record {
  return { t: "dispatch", k: key };
}
// A bounded native identity is at most 256 UTF-8 bytes; JSON escapes a
// control byte to six, so this is the longest encoded native ID.
const LONGEST_NATIVE_ID = "\u0001".repeat(256);
/** The longest settle line a receipt over this claimed base can have. */
function settleBound(key: string, base: UseBase): number {
  return lineBytes({
    t: "settle",
    k: key,
    r: {
      operationId: base.operationId,
      recommendationId: base.recommendationId,
      assetId: base.assetId,
      contentHash: base.contentHash,
      outcome: "not_dispatched",
      code: "idempotency_conflict",
      nativeRunId: LONGEST_NATIVE_ID,
      nativeCallId: LONGEST_NATIVE_ID,
    },
  });
}

function syncDirectory(io: LedgerIo, path: string): void {
  try {
    const fd = io.openSync(dirname(path), "r");
    try {
      io.fsyncSync(fd);
    } finally {
      io.closeSync(fd);
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
export const nodeLedgerIo: LedgerIo = {
  pid: process.pid,
  alive,
  closeSync: fs.closeSync,
  existsSync: fs.existsSync,
  fsyncSync: fs.fsyncSync,
  ftruncateSync: fs.ftruncateSync,
  openSync: fs.openSync,
  readFileSync: fs.readFileSync,
  readdirSync: fs.readdirSync,
  unlinkSync: fs.unlinkSync,
  writeSync: fs.writeSync,
};
/**
 * The lock names this process currently holds, shared by every copy of this
 * module in the process. Each name carries a fresh random token, so a lock
 * naming this PID that is not listed here was left by an exited owner that
 * had the same PID (PID 1 in a container, or PID reuse after a crash).
 */
const HELD: Set<string> = ((globalThis as any)[
  Symbol.for("relaymind.native-use-ledger.held-locks")
] ??= new Set<string>());
/**
 * Exclusive ownership without ever removing a live owner's lock. Each opener
 * creates its own lock file, named by its process and a random token and
 * never reused, and only then inspects the others: any other lock of a live
 * (or unknown) owner makes it withdraw its own and refuse, and only a lock
 * whose named owner no longer exists is removed. Of two openers, the later
 * inspection always sees the earlier lock, so two can never both proceed
 * (both may refuse; a later open succeeds). A lock's name is its whole
 * content, so the lock removed is exactly the stale one inspected. Returns
 * the lock this owner holds and releases. A lock naming this process's own
 * PID is live only while this process holds it (HELD).
 */
function acquire(io: LedgerIo, path: string): string {
  const directory = dirname(path);
  const prefix = basename(path) + ".lock";
  const mine = `${prefix}.${io.pid}.${randomUUID()}`;
  const own = join(directory, mine);
  HELD.add(mine);
  try {
    io.closeSync(io.openSync(own, "wx", 0o600));
  } catch (error) {
    HELD.delete(mine);
    throw error;
  }
  syncDirectory(io, own);
  try {
    for (const name of io.readdirSync(directory)) {
      if (name === mine) continue;
      let owner: number;
      if (name === prefix) {
        // The single fixed lock name of an earlier revision.
        let text: string;
        try {
          text = io.readFileSync(join(directory, name), "utf8");
        } catch (error) {
          // Already removed by another opener as stale.
          if ((error as NodeJS.ErrnoException).code === "ENOENT") continue;
          throw error;
        }
        owner = Number(text.trim());
      } else if (name.startsWith(prefix + ".")) {
        const match = /^(\d+)\.[A-Za-z0-9-]+$/.exec(
          name.slice(prefix.length + 1),
        );
        owner = match ? Number(match[1]) : Number.NaN;
      } else continue;
      // A live owner keeps the path; this process is a live owner only of
      // the locks it holds now.
      if (
        !Number.isSafeInteger(owner) ||
        owner <= 0 ||
        (owner === io.pid ? HELD.has(name) : io.alive(owner))
      )
        throw Error("ledger_owned");
      try {
        io.unlinkSync(join(directory, name));
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      }
    }
  } catch (error) {
    try {
      io.unlinkSync(own);
    } catch {
      /* Already removed. */
    }
    HELD.delete(mine);
    throw error;
  }
  return own;
}

/** A private append-only JSON-lines ledger at an operator/host-owned path. */
export function createFileUseLedger(
  path: string,
  limits: { maxRecords?: number; maxBytes?: number; maxBlocks?: number } = {},
  io: LedgerIo = nodeLedgerIo,
): NativeUseLedger {
  const maxRecords = limits.maxRecords ?? 100000;
  const maxBytes = limits.maxBytes ?? 64 * 1024 * 1024;
  // Blocks never compete with bindings and approvals for capacity.
  const maxBlocks = limits.maxBlocks ?? 10000;
  let lock: string | undefined;
  const history = new Map<string, UseHistory>();
  const approvals = new Set<string>();
  const blocked = new Set<string>();
  // Records and bytes of bindings and approvals; markers, receipts and
  // blocks use the capacity a binding reserves or the separate block reserve.
  let records = 0;
  let bytes = 0;
  let usable = true;
  let fd: number | undefined;
  const created = !io.existsSync(path);
  try {
    lock = acquire(io, path);
    fd = io.openSync(path, "a+", 0o600);
    if (created) syncDirectory(io, path);
    const text = io.readFileSync(path, "utf8");
    const end = text.lastIndexOf("\n") + 1;
    // An unterminated final record was never synced and acknowledged. It is
    // cut through a separate read-write handle: an append-mode handle cannot
    // change the file length on every platform (on Windows it holds append
    // access only, and truncation needs write access).
    if (end < text.length) {
      const writer = io.openSync(path, "r+");
      try {
        io.ftruncateSync(writer, Buffer.byteLength(text.slice(0, end), "utf8"));
        io.fsyncSync(writer);
      } finally {
        io.closeSync(writer);
      }
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
  /**
   * A binding reserves the exact room for its dispatch marker and its
   * longest possible receipt, so the byte cap holds for every record it may
   * still write.
   */
  function reserve(size: number, record: Record): number {
    return record.t === "claim"
      ? size + lineBytes(markerLine(record.k)) + settleBound(record.k, record.b)
      : size;
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
    // A receipt longer than its binding reserved would break the byte cap;
    // the binding then stays unresolved (indeterminate on recovery).
    if (record.t === "settle") {
      const claimed = history.get(record.k)!;
      if (size > settleBound(record.k, claimed.base))
        throw Error("ledger_full");
    }
    // A block is never refused for capacity: new managed use refuses at the
    // block limit instead (isBlocked), so a required stop is never lost.
    try {
      // writeSync may accept fewer bytes than asked: write the rest, and
      // refuse when a write makes no progress.
      const buffer = Buffer.from(line, "utf8");
      let offset = 0;
      while (offset < buffer.length) {
        const written = io.writeSync(
          handle,
          buffer,
          offset,
          buffer.length - offset,
        );
        if (!(written > 0)) throw Error("ledger_write_stalled");
        offset += written;
      }
      io.fsyncSync(handle);
    } catch (error) {
      // An unknown write state cannot prove history either way.
      usable = false;
      throw error;
    }
    // Applied and acknowledged only once the whole record is synced.
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
        io.closeSync(handle);
      } catch {
        /* Already released. */
      }
    }
    if (lock !== undefined) {
      // Only this owner's own lock, which no other opener removes while
      // this process lives.
      const held = lock;
      lock = undefined;
      try {
        io.unlinkSync(held);
      } catch {
        /* Already removed. */
      }
      HELD.delete(basename(held));
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
