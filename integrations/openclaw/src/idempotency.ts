// The Idempotency-Key of an inference attempt (RM17, RM21, RM33; spec sections
// 7.4 and 7.5, #138).
//
// Section 7.5 asks for a stable key for the same inference payload, a new key
// for the next conversation turn, and the section 7.4 replay rules. Two things
// the pinned host does decide how the key is built:
//
// - The transport builds the request headers before the body (the client is
//   made first, then the body, then onPayload), so a header cannot be computed
//   from the final body.
// - resolveTransportTurnState, which the macOS host probe saw called for this
//   transport, is passed a new random turnId and attempt 1 on every call of the
//   stream function (@openclaw/ai 2026.10.7, createManagedCompletionsClient), so
//   the host's turn ID is different on every retry and cannot name one.
//
// The key is therefore the SHA-256 of the canonical JSON of what the body is
// built from: the model entry (its ID and the X-RelayMind-Execution-Mode
// header; not the base URL, since a key is scoped to the gateway that stores it
// and a restart may move its port), the host session, the system prompt, every message (with the
// host's timestamps), the tools, and the request options that change the body.
// The same conversation sent again gives the same base key (KeyLedger below
// moves it on once its run ended failed or cancelled); the next turn (a new
// user message or a tool result) gives a new one by construction. The
// credential is not part of it: a key belongs to the gateway's one caller, not
// to its token, so a key survives a gateway restart (section 7.4).
//
// Limit: an identical conversation sent again in the same session within the
// gateway's seven-day key retention replays the stored response. The host's
// timestamps on each message make that unlikely, but the key cannot tell a
// deliberate repeat from a retry.
import { createHash } from "node:crypto";

import type { Context, StreamFn } from "./stream.ts";

type Model = Parameters<StreamFn>[0];
type Options = Parameters<StreamFn>[2];

export const IDEMPOTENCY_HEADER = "Idempotency-Key";
const PREFIX = "relaymind-openclaw-v1-";

/** The model fields that shape the request: the route, the mode header, the limits. */
const MODEL_FIELDS = [
  "id",
  "provider",
  "api",
  "headers",
  "compat",
  "maxTokens",
  "reasoning",
  "params",
] as const;

/** The request options that change the body; never the credential, the signal, or hooks. */
const OPTION_FIELDS = [
  "streaming",
  "maxTokens",
  "temperature",
  "topP",
  "toolChoice",
  "reasoning",
  "reasoningEffort",
] as const;

/** One canonical JSON text: sorted keys; undefined values and functions left out. */
export function canonicalJson(value: unknown): string {
  return JSON.stringify(canonical(value));
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => (typeof item === "function" ? null : canonical(item)));
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value).sort()) {
      const item = (value as Record<string, unknown>)[key];
      if (item === undefined || typeof item === "function") continue;
      out[key] = canonical(item);
    }
    return out;
  }
  return value;
}

function pick(source: unknown, fields: readonly string[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (!source || typeof source !== "object") return out;
  for (const field of fields) out[field] = (source as Record<string, unknown>)[field];
  return out;
}

const sha256 = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");

export interface KeyInput {
  model: Model;
  context: Context;
  options?: Options;
}

/** The key of one inference payload: relaymind-openclaw-v1-<64 hex>, 86 printable ASCII characters. */
export function idempotencyKey({ model, context, options }: KeyInput): string {
  const identity = {
    model: pick(model, MODEL_FIELDS),
    session: (options as { sessionId?: unknown } | undefined)?.sessionId ?? null,
    options: pick(options, OPTION_FIELDS),
    systemPrompt: context.systemPrompt ?? null,
    messages: context.messages,
    tools: context.tools ?? [],
  };
  return PREFIX + sha256(canonicalJson(identity));
}

/**
 * The key of the n-th plugin retry after a run that ended failed (504): section
 * 7.4 answers the first key with 409 run_terminal, so a retry is a new run under
 * a new key. A retry after 429 keeps the key: nothing was stored.
 */
export function retryKey(key: string, n: number): string {
  return `${key}-retry${n}`;
}

/** Whether a payload's key may be sent again: kept, or spent by a run that ended without an answer. */
export type KeyFate = "keep" | "spent";

const LEDGER_SIZE = 256;

/**
 * The key each payload sends next, in this process (section 7.4). The same
 * payload keeps its key while the gateway can still replay it or stored
 * nothing (a completed answer, a 429). A key whose run ended failed or
 * cancelled is spent: the gateway answers it with 409 run_terminal, and a retry
 * after a failure uses a new key, so the next request of that payload, the
 * host's retry or the plugin's, sends <key>-retry<n+1>. A key that succeeded
 * after a plugin retry stays the payload's key, so a host retry replays it.
 * A new process starts empty: a spent key first gets 409 run_terminal, which
 * spends it, and the retry after that gets a new key. At most 256 payloads are
 * held; the oldest is dropped first.
 */
export class KeyLedger {
  private readonly entries = new Map<string, { n: number; spent: boolean }>();

  /** The key to send for a payload, and its retry number (0 for the base key). */
  next(baseKey: string): { key: string; n: number } {
    const entry = this.entries.get(baseKey);
    const n = entry === undefined ? 0 : entry.spent ? entry.n + 1 : entry.n;
    return { key: n === 0 ? baseKey : retryKey(baseKey, n), n };
  }

  /** What the attempt under retry number n left behind. */
  record(baseKey: string, n: number, fate: KeyFate): void {
    this.entries.delete(baseKey);
    this.entries.set(baseKey, { n, spent: fate === "spent" });
    if (this.entries.size > LEDGER_SIZE) this.entries.delete(this.entries.keys().next().value!);
  }
}

/**
 * One OpenClaw turn: the session and the conversation up to its last user
 * message. Every request of a turn (the first one and each tool-result request
 * after it) has the same turn ID; a run ID is recorded against it.
 */
export function turnId(context: Context, sessionId?: string): string {
  const messages = context.messages;
  let last = -1;
  messages.forEach((message, index) => {
    if (message.role === "user") last = index;
  });
  const prefix = messages.slice(0, last + 1);
  return `turn-${sha256(canonicalJson({ session: sessionId ?? null, messages: prefix })).slice(0, 16)}`;
}
