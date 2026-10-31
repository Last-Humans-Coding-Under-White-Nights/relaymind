// Controlled host-shaped fixtures only; never native qualification evidence.
import type { UserActionClient } from "../src/recommendations.js";
import { createNativeActionHandler } from "../src/recommendation-native-actions.js";
export function fixture() {
  let current = true;
  let workspace = "/fixture/native-workspace";
  const calls: unknown[] = [];
  const replies: unknown[][] = [];
  const binding = Object.freeze({
    profileId: "prepared-profile",
    authenticatedUserId: "fixture@example.test",
    agentId: "main",
    workspaceDir: "/fixture/native-workspace",
    userId: "registered-user",
    workspaceId: "registered-workspace",
  });
  const assertCurrent = () => {
    if (!current) throw Error("retired");
  };
  const options: any = {
    req: { id: "rpc-request" },
    params: {
      sessionKey: "native-key",
      expectedSessionId: "native-id",
      expectedLifecycleRevision: "native-life",
      input: { kind: "resetFeedback", expectedFeedbackRevision: "0" },
      idempotencyKey: "explicit-user-request",
    },
    client: {
      authenticatedUserId: binding.authenticatedUserId,
      preparedSessionProfile: { profileId: binding.profileId },
      presenceKey: "native-connection",
      internal: { authenticatedControlUi: true },
      browserOrigin: { origin: "https://fixture.test" },
      connect: {
        scopes: ["operator.read", "operator.write"],
        device: { id: "paired-device" },
      },
      connectionSignal: new AbortController().signal,
    },
    hasCurrentClientAuthority: () => current,
    sessionMutationAuthorization: { assertCurrent },
    sessionAccessAuthority: {
      target: {
        agentId: "main",
        sessionKey: "native-key",
        sessionId: "native-id",
        lifecycleRevision: "native-life",
      },
      assertCurrent,
    },
    context: {
      getRuntimeConfig: () => ({
        gateway: {
          roles: {
            default: "person",
            definitions: {
              person: {
                sessions: { others: "none" },
                scopes: ["operator.read", "operator.write"],
                agents: ["main"],
              },
            },
          },
        },
      }),
    },
    respond: (...args: unknown[]) => replies.push(args),
  };
  const dependencies = {
    bindings: [binding],
    controlUiOrigin: "https://fixture.test",
    resolveWorkspace: () => workspace,
    actionClient: async () =>
      ({
        resetFeedback: async (...args: unknown[]) => {
          calls.push(args);
          return { schema_version: 1, feedback_revision: 1n, removed_count: 0 };
        },
      }) as UserActionClient,
    monotonicNow: () => 1,
  };
  const handler = createNativeActionHandler(dependencies);
  return {
    options,
    handler,
    dependencies,
    calls,
    replies,
    binding,
    retire: () => {
      current = false;
    },
    changeWorkspace: () => {
      workspace = "/other";
    },
  };
}
