import { createServer } from "node:http";
import { createRequire } from "node:module";
import { afterEach, expect, it } from "vitest";
// @ts-expect-error browser fixture is loaded unchanged as an init script
import { installNativeSocketObserver } from "./browser-wire.mjs";

const { WebSocketServer } = createRequire(import.meta.url)("ws");
const cleanup: (() => Promise<void>)[] = [];
afterEach(async () => { for (const close of cleanup.splice(0).reverse()) await close(); });

async function fixture(extraMethods: string[] = []) {
  const server = createServer();
  const wss = new WebSocketServer({ server });
  const requests: any[] = [];
  wss.on("connection", (socket: any) => {
    socket.send(JSON.stringify({ type: "res", id: "native-connect", ok: true, payload: { type: "hello-ok" } }));
    socket.on("message", (raw: Buffer) => {
      const frame = JSON.parse(raw.toString()); requests.push(frame);
      socket.send(JSON.stringify({ type: "res", id: frame.id, ok: true, payload: { method: frame.method } }));
    });
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address() as { port: number };
  const origin = `http://127.0.0.1:${address.port}`;
  const listeners = new Map<string, () => void>();
  const target: any = { WebSocket, location: { origin }, crypto, setTimeout, clearTimeout,
    addEventListener: (name: string, fn: () => void) => listeners.set(name, fn) };
  installNativeSocketObserver(target, extraMethods);
  const socket = new target.WebSocket(origin.replace("http:", "ws:"));
  cleanup.push(async () => { for (const ws of wss.clients) ws.terminate(); socket.close(); await new Promise<void>(resolve => server.close(() => resolve())); });
  for (let i = 0; i < 100 && !target.__rmPersonWire.state().ready; i++) await new Promise(resolve => setTimeout(resolve, 5));
  expect(target.__rmPersonWire.state().ready).toBe(true);
  return { target, socket, origin, requests, listeners };
}

it("RM39/AC30 transports only an allowed fixture probe on the actual established socket", async () => {
  const f = await fixture();
  const result = await f.target.__rmPersonWire.rpc("rmqualification.person", { sessionKey: "native-fixture-key" });
  expect(result.frame.payload.method).toBe("rmqualification.person");
  expect(f.requests).toEqual([{ type: "req", id: result.id, method: "rmqualification.person", params: { sessionKey: "native-fixture-key" } }]);
  expect(f.requests.some(frame => frame.method === "connect")).toBe(false);
});

it("refuses handshake replay and a changed page origin", async () => {
  const f = await fixture();
  await expect(f.target.__rmPersonWire.rpc("connect", {})).rejects.toThrow();
  f.target.location.origin = "http://foreign.test";
  await expect(f.target.__rmPersonWire.rpc("rmqualification.person", {})).rejects.toThrow();
  expect(f.requests).toEqual([]);
});

it("never silently retries through a replacement socket", async () => {
  const f = await fixture();
  const second = new f.target.WebSocket(f.origin.replace("http:", "ws:"));
  cleanup.push(async () => second.close());
  await expect(f.target.__rmPersonWire.rpc("rmqualification.person", {})).rejects.toThrow();
  expect(f.requests).toEqual([]);
});

it("page lifetime end retires the captured connection", async () => {
  const f = await fixture(); f.listeners.get("pagehide")!();
  await expect(f.target.__rmPersonWire.rpc("rmqualification.person", {})).rejects.toThrow();
  expect(f.requests).toEqual([]);
});

it("correlates create and person responses on one actual browser socket", async () => {
  const f = await fixture();
  const created = await f.target.__rmPersonWire.rpc("sessions.create", { key: "fixture-key" });
  const person = await f.target.__rmPersonWire.rpc("rmqualification.person", { sessionKey: "fixture-key" });
  expect(created.socket_id).toBeTypeOf("string");
  expect(created.socket_id).toBe(person.socket_id);
  expect(created.id).not.toBe(person.id);
});

it("RM37/RM39 permits only the two explicitly requested production-factory fixture methods", async () => {
  const f = await fixture(["relaymind.recommendations", "relaymind.skill"]);
  const result = await f.target.__rmPersonWire.rpc("relaymind.recommendations", {});
  expect(result.frame.payload.method).toBe("relaymind.recommendations");
  await expect(f.target.__rmPersonWire.rpc("connect", {})).rejects.toThrow();
  await expect(f.target.__rmPersonWire.rpc("exec", {})).rejects.toThrow();
});

it("a send that throws clears its timer and pending entry at once", async () => {
  const f = await fixture();
  const timers: unknown[] = [];
  const cleared: unknown[] = [];
  f.target.setTimeout = (fn: () => void, ms: number) => { const t = setTimeout(fn, ms); timers.push(t); return t; };
  f.target.clearTimeout = (t: any) => { cleared.push(t); clearTimeout(t); };
  f.socket.send = () => { throw new Error("socket closing"); };
  const id = f.target.__rmPersonWire.startRpc("rmqualification.person", {});
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(timers).toHaveLength(1);
  expect(cleared).toEqual(timers);
  expect(f.target.__rmPersonWire.outcome(id)).toEqual({ status: "retired_or_timeout" });
  expect(f.requests).toEqual([]);
  // No pending entry is left behind: a later retirement has nothing to clear.
  f.listeners.get("pagehide")!();
  expect(cleared).toEqual(timers);
});

it("#228 ignores null and non-object frames instead of throwing", async () => {
  const f = await fixture();
  const errors: unknown[] = [];
  const handler = (error: unknown) => errors.push(error);
  process.on("uncaughtException", handler);
  try {
    for (const raw of ["null", "1", "\"text\"", "[]"]) {
      const event = new MessageEvent("message", { data: raw });
      f.socket.dispatchEvent(event);
    }
    const result = await f.target.__rmPersonWire.rpc("rmqualification.person", { sessionKey: "k" });
    expect(result.frame.ok).toBe(true);
  } finally {
    process.off("uncaughtException", handler);
  }
  expect(errors).toEqual([]);
});

it("#197 permits the Skill Library methods the ordinary qualification calls", async () => {
  const f = await fixture(["relaymind.recommendations", "relaymind.skill", "skills.library.save", "skills.library.list"]);
  for (const method of ["skills.library.save", "skills.library.list"]) {
    const result = await f.target.__rmPersonWire.rpc(method, {});
    expect(result.frame.payload.method).toBe(method);
  }
  await expect(f.target.__rmPersonWire.rpc("skills.library.delete", {})).rejects.toThrow();
  expect(() => installNativeSocketObserver(f.target, ["skills.library.delete"])).toThrow("invalid_fixture_method");
});

it("RM39/AC30 observes terminal chat events only for turns sent by this fixture", async () => {
  const methods = ["sessions.messages.subscribe", "chat.send", "skills.library.read", "skills.library.mutate", "skills.library.activate"];
  const f = await fixture(methods);
  for (const method of methods) await f.target.__rmPersonWire.rpc(method, method === "chat.send" ? { sessionKey: "s", idempotencyKey: "owned-run" } : {});
  const emit = (payload: unknown) => f.socket.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "event", event: "chat", payload }) }));
  emit({ runId: "foreign", sessionKey: "s", state: "final" });
  emit({ runId: "owned-run", sessionKey: "foreign", state: "final" });
  emit({ runId: "owned-run", sessionKey: "s", state: "delta" });
  emit({ runId: "owned-run", sessionKey: "s", state: "final", message: { role: "assistant", content: "done" } });
  expect(f.target.__rmPersonWire.turnEvents("owned-run")).toEqual([{ socket_id: expect.any(String), runId: "owned-run", sessionKey: "s", state: "final", message: { role: "assistant", content: "done" } }]);
  expect(f.target.__rmPersonWire.turnEvents("foreign")).toEqual([]);
});

it("RM39/AC30 caps native observation at four turns and refuses unused chat history", async () => {
  const f = await fixture(["chat.send"]);
  for (let i = 0; i < 4; i++) await f.target.__rmPersonWire.rpc("chat.send", { sessionKey: `s${i}`, idempotencyKey: `run${i}` });
  await expect(f.target.__rmPersonWire.rpc("chat.send", { sessionKey: "s4", idempotencyKey: "run4" })).rejects.toThrow("native_turn_bound");
  expect(f.requests).toHaveLength(4);
  expect(() => installNativeSocketObserver(f.target, ["chat.history"])).toThrow("invalid_fixture_method");
});

it("RM39/AC30 bounds terminal records by UTF-8 bytes including multibyte content", async () => {
  const f = await fixture(["chat.send"]);
  await f.target.__rmPersonWire.rpc("chat.send", { sessionKey: "s", idempotencyKey: "run" });
  const emit = (content: string) => f.socket.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "event", event: "chat", payload: { runId: "run", sessionKey: "s", state: "final", message: { content } } }) }));
  emit("中".repeat(400000));
  expect(f.target.__rmPersonWire.turnEvents("run").length).toBe(0);
  emit("中文");
  expect(f.target.__rmPersonWire.turnEvents("run")).toHaveLength(1);
});

it.each([undefined, null, 1, "text", []])("RM39/AC30 refuses malformed chat params %s with a closed error", async params => {
  const f = await fixture(["chat.send"]);
  await expect(f.target.__rmPersonWire.rpc("chat.send", params)).rejects.toThrow("native_turn_bound");
  expect(f.requests).toEqual([]);
});
