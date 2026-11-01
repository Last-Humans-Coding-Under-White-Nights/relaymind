// RM08–RM12, RM16, RM39; AC35 (spec 0.65 section 7.6.2; section 14.5 native
// request row): the conditional direct native MCP use path. Only OpenClaw owns
// MCP credentials, transports, servers, execution and approvals: this adapter
// reaches a server solely through an injected, separately qualified host route.
// No production route exists on the pinned host, so the shipped composition
// never registers `relaymind.mcp` and this handler refuses as unqualified.
import { createHash, randomUUID } from "node:crypto";
import { object, identifier } from "./recommendation-contract.ts";
import { wellFormed } from "./recommendation-json.ts";
import type {
  RecommendationClient,
  UserActionClient,
  ReadOptions,
} from "./recommendations.ts";
import {
  nativeRequestWithinLimit,
  prepareNativeAuthority,
} from "./recommendation-native-authority.ts";
import type {
  NativeActionOptions,
  NativeActionBinding,
  NativeScopeDependencies,
} from "./recommendation-native-authority.ts";
import type { QualificationResolver } from "./recommendation-native-registry.ts";

/** The 60-second adapter deadline for preparation and dispatch (approval wait excluded). */
export const MCP_OPERATION_MS = 60000;
export type McpOutcome =
  "not_dispatched" | "completed" | "failed" | "indeterminate";
export type McpCode =
  | "invalid_request"
  | "unqualified"
  | "unauthorized"
  | "choice_unavailable"
  | "binding_changed"
  | "unavailable"
  | "approval_denied"
  | "timeout"
  | "cancelled"
  | "idempotency_conflict"
  | "host_failed"
  | "transport_unknown"
  | "ok";
export interface McpUseReceipt {
  readonly operationId: string;
  readonly recommendationId: string;
  readonly assetId: string;
  readonly contentHash: string;
  readonly outcome: McpOutcome;
  readonly code: McpCode;
  readonly nativeRunId: string | null;
  readonly nativeCallId: string | null;
}
/** A genuine native binding obtained from the host's own execution object. */
export interface NativeMcpBinding {
  readonly serverId: string;
  readonly toolName: string;
  readonly exposedName: string;
  /** The complete host-normalized input schema. */
  readonly inputSchema: unknown;
  /** The native approval deadline, in milliseconds from the approval request. */
  readonly approvalDeadlineMs: number;
  /** Throws when the native handle is no longer current (disconnect, list change, removal). */
  assertCurrent(): void;
  /** Releases the handle through its supported owner. */
  release(): void;
}
export interface NativeApprovalDecision {
  readonly decision: string;
  readonly approvalId: string;
  readonly operationId: string;
  readonly argumentsDigest: string;
}
export interface NativeMcpInvocation {
  readonly ok: boolean;
  readonly nativeRunId: string | null;
  readonly nativeCallId: string | null;
}
/**
 * The separately qualified public host route. Its declared route, transport
 * and tool class must equal the reviewed registry entry exactly.
 */
export interface NativeMcpRoute {
  readonly route: string;
  readonly transport: string;
  readonly toolScope: string;
  bind(
    request: {
      sessionKey: string;
      serverId: string;
      toolName: string;
      exposedName: string;
    },
    control: { signal: AbortSignal; timeoutMs: number },
  ): Promise<NativeMcpBinding>;
  requestApproval(
    binding: NativeMcpBinding,
    request: {
      operationId: string;
      arguments: Record<string, unknown>;
      argumentsDigest: string;
    },
    control: { signal: AbortSignal; timeoutMs: number },
  ): Promise<NativeApprovalDecision>;
  invoke(
    binding: NativeMcpBinding,
    request: {
      operationId: string;
      approvalId: string;
      arguments: Record<string, unknown>;
    },
    control: { signal: AbortSignal; timeoutMs: number },
  ): Promise<NativeMcpInvocation>;
}
interface Dependencies extends NativeScopeDependencies {
  qualification: QualificationResolver;
  route?: NativeMcpRoute;
  recommendationClient: (
    binding: NativeActionBinding,
    options: ReadOptions,
  ) => Promise<RecommendationClient>;
  actionClient: (
    binding: NativeActionBinding,
    options: ReadOptions,
  ) => Promise<UserActionClient>;
  wallNow: () => number;
  operationId?: () => string;
}

class McpRefusal extends Error {
  readonly code: McpCode;
  constructor(code: McpCode) {
    super(code);
    this.code = code;
  }
}
function refuse(code: McpCode): never {
  throw new McpRefusal(code);
}
const MAX_DEPTH = 64;
const MAX_HISTORY = 4096;
const HEX64 = /^[a-f0-9]{64}$/;
function nativeIdentity(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    wellFormed(value) &&
    !value.includes("\0") &&
    Buffer.byteLength(value, "utf8") <= 256
  );
}
function plainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}
function codePointCompare(a: string, b: string): number {
  const left = Array.from(a, (ch) => ch.codePointAt(0)!);
  const right = Array.from(b, (ch) => ch.codePointAt(0)!);
  for (let i = 0; i < Math.min(left.length, right.length); i++)
    if (left[i] !== right[i]) return left[i]! - right[i]!;
  return left.length - right.length;
}
/**
 * One canonical JSON text. `portable` restricts numbers to forms whose text
 * is identical in the RelayMind gateway's canonical_json (sorted keys, no
 * spaces, non-ASCII kept); anything else is undefined rather than guessed.
 */
function canonicalText(
  value: unknown,
  portable: boolean,
  depth = 0,
): string | undefined {
  if (depth > MAX_DEPTH) return undefined;
  if (value === null || typeof value === "boolean")
    return JSON.stringify(value);
  if (typeof value === "string")
    return wellFormed(value) ? JSON.stringify(value) : undefined;
  if (typeof value === "number") {
    if (!Number.isFinite(value) || Object.is(value, -0)) return undefined;
    if (Number.isInteger(value))
      return Number.isSafeInteger(value) ? String(value) : undefined;
    if (portable && (Math.abs(value) < 1e-4 || Math.abs(value) >= 1e16))
      return undefined;
    return String(value);
  }
  if (Array.isArray(value)) {
    const items: string[] = [];
    for (const item of value) {
      const text = canonicalText(item, portable, depth + 1);
      if (text === undefined) return undefined;
      items.push(text);
    }
    return "[" + items.join(",") + "]";
  }
  if (plainObject(value)) {
    const parts: string[] = [];
    for (const key of Object.keys(value).sort(codePointCompare)) {
      if (!wellFormed(key)) return undefined;
      const text = canonicalText(value[key], portable, depth + 1);
      if (text === undefined) return undefined;
      parts.push(JSON.stringify(key) + ":" + text);
    }
    return "{" + parts.join(",") + "}";
  }
  return undefined;
}
function sha256(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}
/** RelayMind canonical schema_hash (RM07), recomputed; never a JSON.stringify byte hash. */
export function canonicalSchemaHash(schema: unknown): string | undefined {
  const text = canonicalText(schema, true);
  return text === undefined ? undefined : sha256(text);
}

// A closed subset of JSON Schema that this adapter can enforce exactly. An
// exposed schema using any other keyword is unavailable, never loosely checked.
const KEYWORDS = new Set([
  "type",
  "properties",
  "required",
  "additionalProperties",
  "items",
  "enum",
  "const",
  "minimum",
  "maximum",
  "exclusiveMinimum",
  "exclusiveMaximum",
  "minLength",
  "maxLength",
  "minItems",
  "maxItems",
  "pattern",
  "description",
  "title",
  "default",
  "$schema",
]);
const TYPES = new Set([
  "object",
  "array",
  "string",
  "number",
  "integer",
  "boolean",
  "null",
]);
class UnsupportedSchema extends Error {}
function checkSchema(schema: unknown, depth = 0): void {
  if (depth > MAX_DEPTH || !plainObject(schema)) throw new UnsupportedSchema();
  for (const key of Object.keys(schema))
    if (!KEYWORDS.has(key)) throw new UnsupportedSchema();
  const types =
    schema.type === undefined
      ? []
      : Array.isArray(schema.type)
        ? schema.type
        : [schema.type];
  if (types.some((t) => typeof t !== "string" || !TYPES.has(t)))
    throw new UnsupportedSchema();
  if (schema.properties !== undefined) {
    if (!plainObject(schema.properties)) throw new UnsupportedSchema();
    for (const child of Object.values(schema.properties))
      checkSchema(child, depth + 1);
  }
  if (
    schema.required !== undefined &&
    (!Array.isArray(schema.required) ||
      schema.required.some((item) => typeof item !== "string"))
  )
    throw new UnsupportedSchema();
  if (
    schema.additionalProperties !== undefined &&
    typeof schema.additionalProperties !== "boolean"
  )
    checkSchema(schema.additionalProperties, depth + 1);
  if (schema.items !== undefined) checkSchema(schema.items, depth + 1);
  if (schema.enum !== undefined && !Array.isArray(schema.enum))
    throw new UnsupportedSchema();
  for (const key of [
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
  ])
    if (schema[key] !== undefined && !Number.isFinite(schema[key]))
      throw new UnsupportedSchema();
  for (const key of ["minLength", "maxLength", "minItems", "maxItems"])
    if (
      schema[key] !== undefined &&
      (!Number.isSafeInteger(schema[key]) || (schema[key] as number) < 0)
    )
      throw new UnsupportedSchema();
  if (schema.pattern !== undefined) {
    if (typeof schema.pattern !== "string") throw new UnsupportedSchema();
    try {
      new RegExp(schema.pattern, "u");
    } catch {
      throw new UnsupportedSchema();
    }
  }
}
/**
 * Validates arguments against the exact exposed schema. An object field that
 * the schema does not declare is refused unless additionalProperties is
 * explicitly true or a schema: no unknown field outside the exact schema.
 */
function conforms(value: unknown, schema: Record<string, any>): boolean {
  const kinds = (v: unknown): string[] =>
    v === null
      ? ["null"]
      : Array.isArray(v)
        ? ["array"]
        : typeof v === "number"
          ? Number.isInteger(v)
            ? ["integer", "number"]
            : ["number"]
          : plainObject(v)
            ? ["object"]
            : [typeof v];
  if (schema.type !== undefined) {
    const allowed = Array.isArray(schema.type) ? schema.type : [schema.type];
    if (!kinds(value).some((kind) => allowed.includes(kind))) return false;
  }
  const text = canonicalText(value, false);
  if (text === undefined) return false;
  if (
    schema.enum !== undefined &&
    !schema.enum.some((item: unknown) => canonicalText(item, false) === text)
  )
    return false;
  if (
    Object.hasOwn(schema, "const") &&
    canonicalText(schema.const, false) !== text
  )
    return false;
  if (typeof value === "number") {
    if (schema.minimum !== undefined && value < schema.minimum) return false;
    if (schema.maximum !== undefined && value > schema.maximum) return false;
    if (
      schema.exclusiveMinimum !== undefined &&
      value <= schema.exclusiveMinimum
    )
      return false;
    if (
      schema.exclusiveMaximum !== undefined &&
      value >= schema.exclusiveMaximum
    )
      return false;
  }
  if (typeof value === "string") {
    const length = Array.from(value).length;
    if (schema.minLength !== undefined && length < schema.minLength)
      return false;
    if (schema.maxLength !== undefined && length > schema.maxLength)
      return false;
    if (
      schema.pattern !== undefined &&
      !new RegExp(schema.pattern, "u").test(value)
    )
      return false;
  }
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems)
      return false;
    if (schema.maxItems !== undefined && value.length > schema.maxItems)
      return false;
    if (schema.items !== undefined)
      if (!value.every((item) => conforms(item, schema.items))) return false;
  }
  if (plainObject(value)) {
    for (const name of schema.required ?? [])
      if (!Object.hasOwn(value, name)) return false;
    const properties: Record<string, any> = schema.properties ?? {};
    for (const [name, child] of Object.entries(value)) {
      if (Object.hasOwn(properties, name)) {
        if (!conforms(child, properties[name])) return false;
      } else if (schema.additionalProperties === true) {
        continue;
      } else if (plainObject(schema.additionalProperties)) {
        if (!conforms(child, schema.additionalProperties)) return false;
      } else return false;
    }
  }
  return true;
}
function validArguments(
  value: Record<string, unknown>,
  schema: unknown,
): boolean {
  try {
    checkSchema(schema);
  } catch {
    refuse("unavailable");
  }
  return conforms(value, schema as Record<string, any>);
}

type Offered = {
  asset_id: string;
  kind: "mcp_tool";
  content_hash: string;
  source_version: string | null;
  stale: boolean;
  identity: {
    server_id: string;
    tool_name: string;
    exposed_name: string;
    input_schema: unknown;
    schema_hash: string;
  };
};
const TIMEOUT = Symbol("timeout");
const ABORTED = Symbol("aborted");
function aborted(signal: AbortSignal): Promise<typeof ABORTED> {
  return new Promise((resolve) => {
    if (signal.aborted) resolve(ABORTED);
    else
      signal.addEventListener("abort", () => resolve(ABORTED), { once: true });
  });
}

interface History {
  readonly digest: string;
  readonly base: Pick<
    McpUseReceipt,
    "operationId" | "recommendationId" | "assetId" | "contentHash"
  >;
  receipt: McpUseReceipt | undefined;
}
export function createNativeMcpHandler(dependencies: Dependencies) {
  // Same registered scope and session incarnation only; in-process history.
  const history = new Map<string, History>();
  const consumedApprovals = new Set<string>();
  return async (options: NativeActionOptions): Promise<void> => {
    let authority: ReturnType<typeof prepareNativeAuthority> | undefined;
    let native: NativeMcpBinding | undefined;
    let entry: History | undefined;
    let base: History["base"] | undefined;
    let dispatched = false;
    let nativeRunId: string | null = null;
    let nativeCallId: string | null = null;
    const closed = (code: McpCode) =>
      options.respond(false, undefined, {
        code: "INVALID_REQUEST",
        message: code,
      });
    const hostCancelled = () =>
      options.signal?.aborted === true ||
      options.client?.connectionSignal?.aborted === true;
    try {
      // 1. Closed request within the native row bound, before any identity use.
      if (!nativeRequestWithinLimit(options)) return closed("invalid_request");
      const params = options.params;
      try {
        object(
          params,
          [
            "sessionKey",
            "expectedSessionId",
            "expectedLifecycleRevision",
            "recommendationId",
            "assetId",
            "contentHash",
            "arguments",
            "idempotencyKey",
          ],
          ["agentId"],
        );
        identifier(params.recommendationId);
      } catch {
        return closed("invalid_request");
      }
      const key = params.idempotencyKey;
      if (
        !nativeIdentity(params.assetId) ||
        typeof params.contentHash !== "string" ||
        !HEX64.test(params.contentHash) ||
        typeof key !== "string" ||
        key !== key.trim() ||
        !/^[\x20-\x7e]{1,255}$/.test(key) ||
        !plainObject(params.arguments) ||
        canonicalText(params.arguments, false) === undefined
      )
        return closed("invalid_request");
      const args = params.arguments;
      // 9/10. Only a reviewed mcp_use entry naming this exact route qualifies.
      const qualified = dependencies.qualification("mcp_use");
      const route = dependencies.route;
      if (
        !qualified?.mcp ||
        !route ||
        route.route !== qualified.mcp.route ||
        route.transport !== qualified.mcp.transport ||
        route.toolScope !== qualified.mcp.toolScope
      )
        return closed("unqualified");
      const maxApprovalWaitMs = qualified.mcp.maxApprovalWaitMs;
      // 2. Ingress and scope: the original authenticated request authority.
      try {
        authority = prepareNativeAuthority(
          options,
          dependencies,
          MCP_OPERATION_MS,
        );
      } catch {
        return closed("unauthorized");
      }
      const live = authority;
      const { binding, signal } = live;
      const current = () => {
        try {
          live.assertCurrent();
        } catch {
          if (live.expired()) refuse("timeout");
          if (hostCancelled()) refuse("cancelled");
          refuse("unauthorized");
        }
      };
      const read = (): ReadOptions => ({
        signal,
        deadlineMs: live.deadline(),
        assertCurrent: current,
      });
      const registered = async () => {
        try {
          const actions = await dependencies.actionClient(binding, read());
          current();
          await actions.listFeedback({ limit: 1 }, read());
        } catch (error) {
          if (error instanceof McpRefusal) throw error;
          refuse("unauthorized");
        }
        current();
      };
      const target = options.sessionAccessAuthority!.target;
      const scopeKey = JSON.stringify([
        binding.userId,
        binding.workspaceId,
        target.sessionKey,
        target.sessionId,
        target.lifecycleRevision,
        key,
      ]);
      const digest = sha256(canonicalText(params, false)!);
      const previous = history.get(scopeKey);
      if (previous) {
        // Recover only the historical receipt, after current authorization;
        // never dispatch again. An unresolved history fails closed.
        await registered();
        if (previous.digest !== digest)
          return options.respond(
            true,
            {
              operationId: dependencies.operationId?.() ?? randomUUID(),
              recommendationId: params.recommendationId,
              assetId: params.assetId,
              contentHash: params.contentHash,
              outcome: "not_dispatched",
              code: "idempotency_conflict",
              nativeRunId: null,
              nativeCallId: null,
            } satisfies McpUseReceipt,
            undefined,
          );
        return options.respond(
          true,
          previous.receipt ?? {
            ...previous.base,
            outcome: "indeterminate",
            code: "transport_unknown",
            nativeRunId: null,
            nativeCallId: null,
          },
          undefined,
        );
      }
      if (history.size >= MAX_HISTORY) return closed("unavailable");
      base = {
        operationId: dependencies.operationId?.() ?? randomUUID(),
        recommendationId: params.recommendationId,
        assetId: params.assetId,
        contentHash: params.contentHash,
      };
      entry = { digest, base, receipt: undefined };
      history.set(scopeKey, entry);
      const operationId = base.operationId;
      // 7. The retained explicit choice and exact still-eligible offered asset.
      let chosen: Offered | undefined;
      const choice = async (): Promise<Offered> => {
        await registered();
        let view: Awaited<ReturnType<RecommendationClient["get"]>>;
        try {
          const client = await dependencies.recommendationClient(
            binding,
            read(),
          );
          current();
          view = await client.get(params.recommendationId as string, read());
        } catch (error) {
          if (error instanceof McpRefusal) throw error;
          refuse("choice_unavailable");
        }
        current();
        const action = view.selected_action;
        const expires = Date.parse(view.choice_expires_at ?? "");
        if (
          view.recommendation_id !== params.recommendationId ||
          view.state !== "chosen" ||
          !action ||
          action.recommendation_id !== params.recommendationId ||
          action.task_id !== view.task_id ||
          action.scope.user_id !== binding.userId ||
          action.scope.workspace_id !== binding.workspaceId ||
          action.choice.kind !== "asset" ||
          action.choice.asset_id !== params.assetId ||
          action.choice.content_hash !== params.contentHash ||
          !Number.isFinite(expires) ||
          dependencies.wallNow() >= expires
        )
          refuse("choice_unavailable");
        const offered = view.displayed_candidates.find(
          (item) => item.asset.asset_id === params.assetId,
        )?.asset as Offered | { kind: string } | undefined;
        if (
          !offered ||
          offered.kind !== "mcp_tool" ||
          (offered as Offered).content_hash !== params.contentHash ||
          (offered as Offered).stale
        )
          refuse("choice_unavailable");
        const exact = offered as Offered;
        if (
          chosen &&
          (exact.source_version !== chosen.source_version ||
            canonicalText(exact.identity, false) !==
              canonicalText(chosen.identity, false))
        )
          refuse("binding_changed");
        return exact;
      };
      const remaining = () =>
        Math.max(1, Math.floor(live.deadline() - dependencies.monotonicNow()));
      current();
      chosen = await choice();
      const identity = chosen.identity;
      // 4. Exact immutable binding from the host's own execution object.
      try {
        native = await route.bind(
          {
            sessionKey: target.sessionKey,
            serverId: identity.server_id,
            toolName: identity.tool_name,
            exposedName: identity.exposed_name,
          },
          { signal, timeoutMs: remaining() },
        );
      } catch {
        current();
        refuse("unavailable");
      }
      current();
      const bound = native;
      const nativeCurrent = () => {
        try {
          bound.assertCurrent();
        } catch {
          refuse("binding_changed");
        }
      };
      const schemaHash = canonicalSchemaHash(bound.inputSchema);
      if (
        bound.serverId !== identity.server_id ||
        bound.toolName !== identity.tool_name ||
        bound.exposedName !== identity.exposed_name ||
        schemaHash === undefined ||
        schemaHash !== identity.schema_hash ||
        canonicalSchemaHash(identity.input_schema) !== identity.schema_hash
      )
        refuse("binding_changed");
      nativeCurrent();
      if (!validArguments(args, bound.inputSchema)) refuse("invalid_request");
      const argumentsDigest = sha256(canonicalText(args, false)!);
      // 6. Fence before requesting native approval.
      await choice();
      nativeCurrent();
      current();
      if (
        !Number.isFinite(bound.approvalDeadlineMs) ||
        bound.approvalDeadlineMs <= 0
      )
        refuse("unavailable");
      const waitMs = Math.floor(
        Math.min(maxApprovalWaitMs, bound.approvalDeadlineMs),
      );
      if (waitMs < 1) refuse("timeout");
      const approvalController = new AbortController();
      const approvalSignal = AbortSignal.any([
        signal,
        approvalController.signal,
      ]);
      let timer: ReturnType<typeof setTimeout> | undefined;
      const started = dependencies.monotonicNow();
      live.suspendDeadline();
      let decision: NativeApprovalDecision | typeof TIMEOUT | typeof ABORTED;
      try {
        decision = await Promise.race([
          route.requestApproval(
            bound,
            { operationId, arguments: args, argumentsDigest },
            { signal: approvalSignal, timeoutMs: waitMs },
          ),
          new Promise<typeof TIMEOUT>((resolve) => {
            timer = setTimeout(() => resolve(TIMEOUT), waitMs);
            timer.unref?.();
          }),
          aborted(signal),
        ]);
      } catch {
        decision = ABORTED;
      } finally {
        clearTimeout(timer);
        approvalController.abort();
        live.resumeDeadline();
      }
      if (
        decision === TIMEOUT ||
        dependencies.monotonicNow() - started > waitMs
      )
        refuse("timeout");
      if (hostCancelled()) refuse("cancelled");
      if (decision === ABORTED) refuse("unavailable");
      if (
        !plainObject(decision) ||
        decision.decision !== "allow-once" ||
        !nativeIdentity(decision.approvalId) ||
        decision.operationId !== operationId ||
        decision.argumentsDigest !== argumentsDigest ||
        consumedApprovals.has(decision.approvalId)
      )
        refuse("approval_denied");
      // Allow-once is consumed at most once, even when later fences refuse.
      consumedApprovals.add(decision.approvalId);
      // 6. Fence after approval and immediately before native server I/O.
      current();
      await choice();
      nativeCurrent();
      current();
      if (!validArguments(args, bound.inputSchema)) refuse("invalid_request");
      if (sha256(canonicalText(args, false)!) !== argumentsDigest)
        refuse("binding_changed");
      dispatched = true;
      const invocation = await Promise.race([
        route.invoke(
          bound,
          { operationId, approvalId: decision.approvalId, arguments: args },
          { signal, timeoutMs: remaining() },
        ),
        aborted(signal),
      ]);
      if (invocation === ABORTED || !plainObject(invocation))
        throw Error("dispatch_unobserved");
      const runId: unknown = invocation.nativeRunId;
      const callId: unknown = invocation.nativeCallId;
      nativeRunId = nativeIdentity(runId) ? runId : null;
      nativeCallId = nativeIdentity(callId) ? callId : null;
      // A completed tool call is not task success; results stay in the host.
      const outcome: Pick<McpUseReceipt, "outcome" | "code"> =
        nativeCallId === null || typeof invocation.ok !== "boolean"
          ? { outcome: "indeterminate", code: "transport_unknown" }
          : invocation.ok
            ? { outcome: "completed", code: "ok" }
            : { outcome: "failed", code: "host_failed" };
      const receipt: McpUseReceipt = {
        ...base,
        ...outcome,
        nativeRunId,
        nativeCallId,
      };
      entry.receipt = receipt;
      options.respond(true, receipt, undefined);
    } catch (error) {
      if (!base || !entry)
        return closed(error instanceof McpRefusal ? error.code : "unavailable");
      let code: McpCode;
      if (dispatched)
        code = hostCancelled()
          ? "cancelled"
          : authority?.expired()
            ? "timeout"
            : "transport_unknown";
      else
        code =
          error instanceof McpRefusal
            ? error.code
            : authority?.expired()
              ? "timeout"
              : hostCancelled()
                ? "cancelled"
                : "unavailable";
      const receipt: McpUseReceipt = {
        ...base,
        outcome: dispatched ? "indeterminate" : "not_dispatched",
        code,
        nativeRunId: dispatched ? nativeRunId : null,
        nativeCallId: dispatched ? nativeCallId : null,
      };
      entry.receipt = receipt;
      options.respond(true, receipt, undefined);
    } finally {
      try {
        native?.release();
      } finally {
        authority?.close();
      }
    }
  };
}
