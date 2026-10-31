// RM37/RM39; AC28/AC30: in-process native authority, never request-asserted identity.
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";
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
/** Require the restrictive personal-session profile, independently of native write scope. */
function personalPolicy(
  options: NativeActionOptions,
  agentId: string,
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
  // All assignable roles must preserve this initial personal-session profile.
  // Host guards still resolve/recheck the actual role and grants themselves.
  for (const role of Object.values(roles.definitions)) {
    if (
      role.sessions.others !== "none" ||
      role.scopes.includes("operator.admin") ||
      !role.scopes.includes("operator.write") ||
      !Array.isArray(role.agents) ||
      !role.agents.includes(agentId) ||
      role.agents.some((agent) => !allowedAgents.has(agent))
    )
      throw Error("personal_policy_required");
  }
}

export function prepareNativeAuthority(
  options: NativeActionOptions,
  dependencies: NativeScopeDependencies,
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
  const deadlineMs = dependencies.monotonicNow() + 10000;
  const timer = setTimeout(() => controller.abort(), 10000);
  timer.unref();
  const current = () => {
    if (
      signal.aborted ||
      client.invalidated ||
      options.hasCurrentClientAuthority?.() !== true ||
      client.preparedSessionProfile?.profileId !== profileId ||
      client.authenticatedUserId !== authenticatedUserId ||
      dependencies.monotonicNow() >= deadlineMs
    )
      throw Error("authority_retired");
    access.assertCurrent();
    mutation.assertCurrent();
    personalPolicy(options, target.agentId, allowedAgents);
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
    deadlineMs,
    assertCurrent: current,
    close: () => {
      clearTimeout(timer);
      controller.abort();
    },
  };
}
