import { describe, expect, it, vi } from "vitest";
// @ts-expect-error plain JS fixture loaded unchanged by the pinned native host
import { createPersonProbe } from "./probe.mjs";

// RM37/RM39 AC28/AC30: controlled host-shaped fixtures are not native person proof.
function fixture() {
  const events: unknown[] = [];
  const respond = vi.fn();
  const current = vi.fn();
  const target = { agentId: "main", sessionKey: "native-key", sessionId: "native-id", lifecycleRevision: "native-life" };
  const options = {
    req: { id: "request-a" }, params: { sessionKey: target.sessionKey, expectedSessionId: target.sessionId, expectedLifecycleRevision: target.lifecycleRevision },
    client: { authenticatedUserId: "fixture@example.test", preparedSessionProfile: { profileId: "native-profile" },
      browserOrigin: { origin: "https://fixture.test:8443" }, presenceKey: "native-connection",
      connect: { device: { id: "native-device" } }, connectionSignal: new AbortController().signal },
    hasCurrentClientAuthority: () => true,
    sessionMutationAuthorization: { admittedTarget: target, assertCurrent: current },
    sessionAccessAuthority: { target, assertCurrent: current }, respond,
  };
  const dispatch = vi.fn(async () => ({ ok: true }));
  const probe = createPersonProbe({ origin: "https://fixture.test:8443", record: (event: unknown) => events.push(event), dispatch, pause: async () => {} });
  return { options, events, respond, current, dispatch, probe };
}

describe("test-only native person RPC probe", () => {
  it.each(["no-person", "no-profile", "model-origin", "wrong-origin", "stale-session", "foreign-params", "retired"])("denies %s before inherited dispatch", async kind => {
    const f = fixture();
    if (kind === "no-person") f.options.client.authenticatedUserId = "";
    if (kind === "no-profile") f.options.client.preparedSessionProfile.profileId = "";
    if (kind === "model-origin") Object.assign(f.options.client, { internal: { agentToolCaller: {} } });
    if (kind === "wrong-origin") f.options.client.browserOrigin.origin = "https://foreign.test";
    if (kind === "stale-session") f.options.params.expectedSessionId = "old";
    if (kind === "foreign-params") Object.assign(f.options.params, { profileId: "invented" });
    if (kind === "retired") f.options.hasCurrentClientAuthority = () => false;
    await f.probe(f.options);
    expect(f.dispatch).not.toHaveBeenCalled();
    expect(f.respond.mock.calls[0][0]).toBe(false);
    expect(f.events.at(-1)).toMatchObject({ phase: "guard_denied", requestId: "request-a" });
  });

  it("captures only native prepared profile/session and inherited dispatch", async () => {
    const f = fixture();
    await f.probe(f.options);
    expect(f.dispatch).toHaveBeenCalledWith("tools.effective", { sessionKey: "native-key", agentId: "main" }, { timeoutMs: 3000 });
    expect(f.respond.mock.calls[0][0]).toBe(true);
    expect(f.respond.mock.calls[0][1]).toMatchObject({ profileId: "native-profile", authenticatedUserId: "fixture@example.test", sessionId: "native-id", lifecycleRevision: "native-life", connectionId: "native-connection", deviceId: "native-device" });
    expect(f.current).toHaveBeenCalled();
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

it("unexpected inventory exceptions retain a safe failure cause while authority is current", async () => {
  const f = fixture();
  const probe = createPersonProbe({ origin: "https://fixture.test:8443", record: (event: unknown) => f.events.push(event),
    dispatch: async () => { throw new Error("private upstream details"); } });
  await probe(f.options);
  expect(f.events.at(-1)).toMatchObject({ phase: "probe_failed", reason: "inventory_unavailable" });
  expect(JSON.stringify(f.events)).not.toContain("private upstream details");
});
