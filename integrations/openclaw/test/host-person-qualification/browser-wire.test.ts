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

it("RM39/AC30 transports only an allowed read-only probe on the actual established socket", async () => {
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
