import { describe, expect, it, vi } from "vitest";
// @ts-expect-error plain JS fixture loaded unchanged by the pinned native host
import { createPersonProbe } from "./probe.mjs";
// @ts-expect-error pinned SDK's internal guard has no public type declaration
import { o as withSessionMutationCommitGuard } from "../../node_modules/openclaw/dist/session-mutation-guards-CPbqDZs2.mjs";

// RM37/RM39 AC28/AC30: controlled host-shaped fixtures are not native person proof.
function fixture() {
  const events: unknown[] = [];
  const respond = vi.fn();
  const current = vi.fn();
  const connection = new AbortController();
  const target = { agentId: "main", sessionKey: "native-key", sessionId: "native-id", lifecycleRevision: "native-life" };
  const mutation = withSessionMutationCommitGuard(undefined, current, undefined);
  const options = {
    req: { id: "request-a" }, params: { sessionKey: target.sessionKey, expectedSessionId: target.sessionId, expectedLifecycleRevision: target.lifecycleRevision },
    client: { authenticatedUserId: "fixture@example.test", preparedSessionProfile: { profileId: "native-profile" },
      internal: { authenticatedControlUi: true },
      browserOrigin: { origin: "https://fixture.test:8443" }, presenceKey: "native-connection",
      connect: { device: { id: "native-device" } }, connectionSignal: connection.signal },
    hasCurrentClientAuthority: () => true,
    sessionMutationAuthorization: mutation,
    sessionAccessAuthority: { target, assertCurrent: current }, respond,
  };
  const dispatch = vi.fn(async () => ({ ok: true }));
  const probe = createPersonProbe({ origin: "https://fixture.test:8443", record: (event: unknown) => events.push(event), dispatch, pause: async () => {} });
  return { options, events, respond, current, dispatch, probe, connection };
}

describe("test-only native person RPC probe", () => {
  it.each(["no-person", "no-profile", "model-origin", "wrong-origin", "stale-session", "foreign-params", "retired"])("denies %s before inherited dispatch", async kind => {
    const f = fixture();
    if (kind === "no-person") f.options.client.authenticatedUserId = "";
    if (kind === "no-profile") f.options.client.preparedSessionProfile.profileId = "";
    if (kind === "model-origin") Object.assign(f.options.client.internal, { agentToolCaller: {} });
    if (kind === "wrong-origin") f.options.client.browserOrigin.origin = "https://foreign.test";
    if (kind === "stale-session") f.options.params.expectedSessionId = "old";
    if (kind === "foreign-params") Object.assign(f.options.params, { profileId: "invented" });
    if (kind === "retired") f.options.hasCurrentClientAuthority = () => false;
    await f.probe(f.options);
    expect(f.dispatch).not.toHaveBeenCalled();
    expect(f.respond.mock.calls[0][0]).toBe(false);
    expect(f.events.at(-1)).toMatchObject({ phase: "guard_denied", requestId: "request-a" });
  });

  it.each(["missing-control-ui", "false-control-ui"])("denies %s before inherited dispatch", async kind => {
    const f = fixture();
    if (kind === "missing-control-ui") Object.assign(f.options.client, { internal: {} });
    else Object.assign(f.options.client.internal, { authenticatedControlUi: false });
    await f.probe(f.options);
    expect(f.dispatch).not.toHaveBeenCalled();
    expect(f.respond.mock.calls[0][0]).toBe(false);
    expect(f.events.at(-1)).toMatchObject({ phase: "guard_denied", reason: "admission_refused" });
  });

  it("captures only native prepared profile/session and inherited dispatch", async () => {
    const f = fixture();
    expect(Object.hasOwn(f.options.sessionMutationAuthorization, "admittedTarget")).toBe(false);
    await f.probe(f.options);
    expect(f.dispatch).toHaveBeenCalledWith("tools.effective", { sessionKey: "native-key", agentId: "main" }, { timeoutMs: 3000 });
    expect(f.respond.mock.calls[0][0]).toBe(true);
    expect(f.respond.mock.calls[0][1]).toMatchObject({ profileId: "native-profile", authenticatedUserId: "fixture@example.test", authenticatedControlUi: true, sessionId: "native-id", lifecycleRevision: "native-life", connectionId: "native-connection", deviceId: "native-device" });
    expect(f.current).toHaveBeenCalledTimes(6);
  });

  it.each(["access", "target", "access-assertion", "mutation", "mutation-assertion"])("denies missing %s before inherited dispatch", async kind => {
    const f = fixture();
    if (kind === "access") Object.assign(f.options, { sessionAccessAuthority: undefined });
    if (kind === "target") Object.assign(f.options.sessionAccessAuthority, { target: undefined });
    if (kind === "access-assertion") Object.assign(f.options.sessionAccessAuthority, { assertCurrent: undefined });
    if (kind === "mutation") Object.assign(f.options, { sessionMutationAuthorization: undefined });
    if (kind === "mutation-assertion") Object.assign(f.options.sessionMutationAuthorization, { assertCurrent: undefined });
    await f.probe(f.options);
    expect(f.dispatch).not.toHaveBeenCalled();
    expect(f.respond.mock.calls[0][0]).toBe(false);
  });

  it.each(["key", "id", "lifecycle", "agent", "target-lifecycle"])("denies mismatched native %s before inherited dispatch", async kind => {
    const f = fixture();
    if (kind === "key") f.options.params.sessionKey = "foreign-key";
    if (kind === "id") f.options.params.expectedSessionId = "foreign-id";
    if (kind === "lifecycle") f.options.params.expectedLifecycleRevision = "foreign-life";
    if (kind === "agent") Object.assign(f.options.params, { agentId: "foreign-agent" });
    if (kind === "target-lifecycle") f.options.sessionAccessAuthority.target.lifecycleRevision = "";
    await f.probe(f.options);
    expect(f.dispatch).not.toHaveBeenCalled();
    expect(f.respond.mock.calls[0][0]).toBe(false);
  });

  it.each(["client-invalidated", "connection-aborted", "request-aborted", "mutation-assertion", "access-assertion"])("denies initially retired %s", async kind => {
    const f = fixture();
    if (kind === "client-invalidated") Object.assign(f.options.client, { invalidated: true });
    if (kind === "connection-aborted") Object.assign(f.options.client, { connectionSignal: AbortSignal.abort() });
    if (kind === "request-aborted") Object.assign(f.options, { signal: AbortSignal.abort() });
    if (kind === "mutation-assertion") Object.assign(f.options.sessionMutationAuthorization, { assertCurrent: () => { throw new Error("retired"); } });
    if (kind === "access-assertion") Object.assign(f.options.sessionAccessAuthority, { assertCurrent: () => { throw new Error("retired"); } });
    await f.probe(f.options);
    expect(f.dispatch).not.toHaveBeenCalled();
    expect(f.respond.mock.calls[0][0]).toBe(false);
    expect(f.events.at(-1)).toMatchObject({ phase: "guard_denied", reason: "initial_authority_retired" });
  });

  it("rechecks both authorities after inherited inventory await", async () => {
    const f = fixture();
    const probe = createPersonProbe({ origin: "https://fixture.test:8443", record: (event: unknown) => f.events.push(event),
      dispatch: async () => { f.options.hasCurrentClientAuthority = () => false; return { ok: true }; } });
    await probe(f.options);
    expect(f.respond.mock.calls[0][0]).toBe(false);
    expect(f.events.at(-1)).toMatchObject({ phase: "guard_denied", reason: "post_await_authority_retired" });
  });

  it("rejects authority lost during the actual awaited phase", async () => {
    const f = fixture();
    const probe = createPersonProbe({ origin: "https://fixture.test:8443", record: (event: unknown) => f.events.push(event), dispatch: f.dispatch, pause: async () => { f.options.hasCurrentClientAuthority = () => false; } });
    await probe(f.options);
    expect(f.dispatch).not.toHaveBeenCalled();
    expect(f.respond.mock.calls[0][0]).toBe(false);
  });
});

it("uses a bounded fixture hold before the post-await authority recheck", async () => {
  const f = fixture();
  Object.assign(f.options.params, { hold: true });
  const pause = vi.fn(async () => { f.options.hasCurrentClientAuthority = () => false; });
  const probe = createPersonProbe({ origin: "https://fixture.test:8443", record: () => {}, dispatch: f.dispatch, pause });
  await probe(f.options);
  expect(pause).toHaveBeenCalledWith(true);
  expect(f.dispatch).not.toHaveBeenCalled();
  expect(f.respond.mock.calls[0][0]).toBe(false);
});

it.each(["fixture_hold_timeout", "inventory_unavailable"])("generic %s cannot become post-await retirement proof", async cause => {
  const f = fixture();
  const probe = createPersonProbe({ origin: "https://fixture.test:8443", record: (event: unknown) => f.events.push(event),
    dispatch: cause === "inventory_unavailable" ? async () => ({ ok: false }) : f.dispatch,
    pause: async () => { if (cause === "fixture_hold_timeout") throw new Error(cause); } });
  await probe(f.options);
  expect(f.events.at(-1)).toMatchObject({ phase: "probe_failed", requestId: "request-a", reason: cause });
  expect(f.events.some((event: any) => event.phase === "guard_denied" && event.reason === "post_await_authority_retired")).toBe(false);
});

it("only an authoritative post-await rejection records retirement with the native connection", async () => {
  const f = fixture();
  const probe = createPersonProbe({ origin: "https://fixture.test:8443", record: (event: unknown) => f.events.push(event), dispatch: f.dispatch,
    pause: async () => { f.options.hasCurrentClientAuthority = () => false; } });
  await probe(f.options);
  expect(f.events.at(-1)).toEqual({ phase: "guard_denied", requestId: "request-a", connectionId: "native-connection", reason: "post_await_authority_retired" });
});

it("observes the actual held connection abort before releasing the native guard", async () => {
  const f = fixture();
  Object.assign(f.options.params, { hold: true });
  let release!: () => void;
  const probe = createPersonProbe({ origin: "https://fixture.test:8443", record: (event: unknown) => f.events.push(event), dispatch: f.dispatch,
    pause: async () => new Promise<void>(resolve => { release = resolve; }) });
  const pending = probe(f.options);
  expect(f.events).toMatchObject([{ phase: "prepared", requestId: "request-a", connectionId: "native-connection" }]);
  f.connection.abort();
  expect(f.events.at(-1)).toMatchObject({ phase: "connection_retired", requestId: "request-a", connectionId: "native-connection", connection_aborted: true });
  release();
  await pending;
  expect(f.dispatch).not.toHaveBeenCalled();
  expect(f.events.at(-1)).toMatchObject({ phase: "guard_denied", reason: "post_await_authority_retired" });
  expect(f.respond.mock.calls[0][0]).toBe(false);
});

it("refuses held work without a native connection signal", async () => {
  const f = fixture();
  Object.assign(f.options.params, { hold: true });
  Object.assign(f.options.client, { connectionSignal: undefined });
  await f.probe(f.options);
  expect(f.dispatch).not.toHaveBeenCalled();
  expect(f.events).toEqual([{ phase: "guard_denied", requestId: "request-a", connectionId: "native-connection", reason: "admission_refused" }]);
});

it("removes the held abort observer when work finishes before retirement", async () => {
  const f = fixture();
  Object.assign(f.options.params, { hold: true });
  const remove = vi.spyOn(f.connection.signal, "removeEventListener");
  await f.probe(f.options);
  expect(remove).toHaveBeenCalledWith("abort", expect.any(Function));
  f.connection.abort();
  expect(f.events.some((event: any) => event.phase === "connection_retired")).toBe(false);
});

it("unexpected inventory exceptions retain a safe failure cause while authority is current", async () => {
  const f = fixture();
  const probe = createPersonProbe({ origin: "https://fixture.test:8443", record: (event: unknown) => f.events.push(event),
    dispatch: async () => { throw new Error("private upstream details"); } });
  await probe(f.options);
  expect(f.events.at(-1)).toMatchObject({ phase: "probe_failed", reason: "inventory_unavailable" });
  expect(JSON.stringify(f.events)).not.toContain("private upstream details");
});
