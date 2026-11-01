// RM37/RM39; AC28/AC30: in-process native authority, never request-asserted identity.
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";
import { RecommendationClientError } from "./recommendation-transport.ts";
type Handler = Parameters<OpenClawPluginApi["registerGatewayMethod"]>[1];
export type NativeActionOptions = Parameters<Handler>[0];
export interface NativeActionBinding {
  readonly profileId: string;
  readonly authenticatedUserId: string;
  readonly agentId: string;
  readonly workspaceDir: string;
  readonly userId: string;
  readonly workspaceId: string;
}
export interface NativeScopeDependencies {
  bindings: readonly NativeActionBinding[];
  controlUiOrigin: string;
  resolveWorkspace: (
    config: ReturnType<NativeActionOptions["context"]["getRuntimeConfig"]>,
    agentId: string,
  ) => string;
  monotonicNow: () => number;
}
/**
 * RM16: an honest class for a gateway failure. Only the gateway's own
 * refusals mean revoked authority/registration or an unavailable choice; a
 * transport, protocol, overload or unknown failure is unavailable, and the
 * client's deadline is a timeout.
 */
export type GatewayFailure =
  | "unauthorized"
  | "choice_unavailable"
  | "deadline"
  | "cancelled"
  | "unavailable";
export function classifyGatewayError(error: unknown): GatewayFailure {
  if (!(error instanceof RecommendationClientError)) return "unavailable";
  switch (error.code) {
    case "unauthenticated":
    case "user_action_required":
    case "scope_mismatch":
      return "unauthorized";
    case "recommendation_not_found":
    case "recommendation_expired":
    case "asset_changed":
    case "invalid_state":
      return "choice_unavailable";
    case "deadline_exceeded":
      return "deadline";
    case "cancelled":
      return "cancelled";
    default:
      return "unavailable";
  }
}
/** Spec 0.65 section 14.5: every section 7.6.1–7.6.2 native request is at most 512 KiB UTF-8. */
export const MAX_NATIVE_REQUEST_BYTES = 512 * 1024;
/** Measures the request frame with its fixed fields; any stricter host transport cap still applies. */
export function nativeRequestWithinLimit(
  options: NativeActionOptions,
): boolean {
  try {
    const frame =
      typeof options.req === "object" && options.req !== null
        ? { ...options.req, params: options.params }
        : { params: options.params };
    const text = JSON.stringify(frame);
    return (
      typeof text === "string" &&
      Buffer.byteLength(text, "utf8") <= MAX_NATIVE_REQUEST_BYTES
    );
  } catch {
    return false;
  }
}
/** Require the restrictive personal-session profile, independently of native write scope. */
function personalPolicy(
  options: NativeActionOptions,
  allowedAgents: ReadonlySet<string>,
): void {
  const config = options.context.getRuntimeConfig();
  const roles = config.gateway?.roles;
  const profile = options.client?.preparedSessionProfile;
  if (
    !roles ||
    !roles.default ||
    !Object.hasOwn(roles.definitions, roles.default) ||
    !profile ||
    profile.profileId === "gateway-owner" ||
    options.client?.internal?.controlUiAdmin ||
    options.client?.connect.scopes?.includes("operator.admin")
  )
    throw Error("personal_policy_required");
  // Spec 7.6.1 (0.63): every assignable role must keep sessions private, have
  // no admin scope and name only registered agents. The caller's own write
  // privilege and target-agent access are resolved by the host guards, never
  // demanded of unrelated roles (a read-only or other-agent role stays valid).
  for (const role of Object.values(roles.definitions)) {
    if (
      role.sessions.others !== "none" ||
      role.scopes.includes("operator.admin") ||
      !Array.isArray(role.agents) ||
      role.agents.some((agent) => !allowedAgents.has(agent))
    )
      throw Error("personal_policy_required");
  }
}

export function prepareNativeAuthority(
  options: NativeActionOptions,
  dependencies: NativeScopeDependencies,
  operationMs = 10000,
) {
  const allowedAgents = new Set(
    dependencies.bindings.map((binding) => binding.agentId),
  );
  const { client } = options;
  const access = options.sessionAccessAuthority;
  const mutation = options.sessionMutationAuthorization;
  const target = access?.target;
  if (
    !client ||
    !client.authenticatedUserId ||
    !client.preparedSessionProfile?.profileId ||
    client.internal?.authenticatedControlUi !== true ||
    client.internal.agentToolCaller ||
    client.internal.syntheticClient ||
    client.browserOrigin?.origin !== dependencies.controlUiOrigin ||
    !client.connect.device?.id ||
    !client.presenceKey ||
    !client.connectionSignal ||
    !target ||
    !target.lifecycleRevision ||
    !access ||
    !mutation ||
    options.params.sessionKey !== target.sessionKey ||
    options.params.expectedSessionId !== target.sessionId ||
    options.params.expectedLifecycleRevision !== target.lifecycleRevision ||
    (options.params.agentId !== undefined &&
      options.params.agentId !== target.agentId)
  )
    throw Error("authority_unavailable");
  const profileId = client.preparedSessionProfile.profileId;
  const authenticatedUserId = client.authenticatedUserId;
  const workspaceDir = dependencies.resolveWorkspace(
    options.context.getRuntimeConfig(),
    target.agentId,
  );
  const binding = dependencies.bindings.find(
    (item) =>
      item.profileId === profileId &&
      item.authenticatedUserId === authenticatedUserId &&
      item.agentId === target.agentId &&
      item.workspaceDir === workspaceDir,
  );
  if (!binding) throw Error("unregistered_person");
  const controller = new AbortController();
  const signal = AbortSignal.any([
    controller.signal,
    ...[client.connectionSignal, options.signal].filter(
      (value): value is AbortSignal => value !== undefined,
    ),
  ]);
  let deadlineMs = dependencies.monotonicNow() + operationMs;
  let suspendedAt: number | undefined;
  const arm = (ms: number) => {
    const value = setTimeout(() => controller.abort(), Math.max(0, ms));
    value.unref();
    return value;
  };
  let timer = arm(operationMs);
  const expired = () =>
    suspendedAt === undefined && dependencies.monotonicNow() >= deadlineMs;
  const current = () => {
    if (
      signal.aborted ||
      client.invalidated ||
      options.hasCurrentClientAuthority?.() !== true ||
      client.preparedSessionProfile?.profileId !== profileId ||
      client.authenticatedUserId !== authenticatedUserId ||
      expired()
    )
      throw Error("authority_retired");
    access.assertCurrent();
    mutation.assertCurrent();
    personalPolicy(options, allowedAgents);
    if (
      dependencies.resolveWorkspace(
        options.context.getRuntimeConfig(),
        target.agentId,
      ) !== binding.workspaceDir
    )
      throw Error("workspace_changed");
  };
  try {
    current();
  } catch (error) {
    clearTimeout(timer);
    throw error;
  }
  return {
    binding,
    signal,
    /** The current deadline, including any excluded approval wait. */
    get deadlineMs() {
      return deadlineMs;
    },
    assertCurrent: current,
    /** The current operation deadline (monotonic), after excluded waits. */
    deadline: () => deadlineMs,
    expired,
    /** Spec 0.65 7.6.2: the host approval wait is excluded from the operation deadline. */
    suspendDeadline: () => {
      if (suspendedAt !== undefined) return;
      clearTimeout(timer);
      suspendedAt = dependencies.monotonicNow();
    },
    resumeDeadline: () => {
      if (suspendedAt === undefined) return;
      deadlineMs += dependencies.monotonicNow() - suspendedAt;
      suspendedAt = undefined;
      timer = arm(deadlineMs - dependencies.monotonicNow());
    },
    close: () => {
      clearTimeout(timer);
      controller.abort();
    },
  };
}
