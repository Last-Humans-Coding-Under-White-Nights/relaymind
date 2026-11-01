// Programmatic fixture RPC on the native UI's actual authenticated socket.
// This observer never sends/replaces connect, auth, device or signature frames.
export function installNativeSocketObserver(target = window, extraMethods = []) {
  const Original = target.WebSocket;
  const origin = target.location.origin;
  if (!Array.isArray(extraMethods) || extraMethods.some(method => !["relaymind.recommendations", "relaymind.skill", "skills.library.save", "skills.library.list", "sessions.messages.subscribe", "chat.send", "skills.library.read", "skills.library.mutate", "skills.library.activate"].includes(method))) throw new Error("invalid_fixture_method");
  const methods = new Set(["rmqualification.person", "sessions.create", "sessions.reset", ...extraMethods]);
  let socket, socketId, ready = false, retired = false;
  const pending = new Map(), used = new Set(), completed = new Map();
  const turns = new Map(), terminalEvents = new Map();
  const retire = () => {
    retired = true; ready = false;
    for (const item of pending.values()) {
      target.clearTimeout(item.timer); item.reject(new Error("native_socket_retired"));
    }
    pending.clear();
  };
  target.addEventListener("pagehide", retire);
  target.WebSocket = class extends Original {
    constructor(...args) {
      super(...args);
      const url = new URL(String(args[0]), origin);
      if (url.origin.replace(/^ws/, "http") !== origin) return;
      if (socket) { retire(); return; }
      socket = this; socketId = target.crypto.randomUUID();
      this.addEventListener("close", retire);
      this.addEventListener("error", retire);
      this.addEventListener("message", event => {
        if (retired || target.location.origin !== origin || typeof event.data !== "string") return;
        let frame;
        try { frame = JSON.parse(event.data); } catch { return; }
        if (!frame || typeof frame !== "object") return;
        if (frame.type === "event" && frame.event === "chat") {
          const value = frame.payload;
          if (value && turns.has(value.runId) && turns.get(value.runId) === value.sessionKey &&
              ["final", "error", "aborted"].includes(value.state)) {
            const events = terminalEvents.get(value.runId) ?? [];
            if (events.length < 2 && new TextEncoder().encode(event.data).length <= 1048576) events.push({...value, socket_id: socketId});
            terminalEvents.set(value.runId, events);
          }
          return;
        }
        if (frame.type !== "res") return;
        if (frame.ok === true && frame.payload?.type === "hello-ok") ready = true;
        const item = pending.get(frame.id);
        if (!item) return;
        pending.delete(frame.id); target.clearTimeout(item.timer);
        item.resolve({ id: frame.id, socket_id: socketId, frame });
      });
    }
  };
  const start = (method, params) => {
    if (!methods.has(method) || retired || !ready || target.location.origin !== origin ||
        socket?.readyState !== Original.OPEN || used.size >= 100) throw new Error("native_socket_unavailable");
    const id = target.crypto.randomUUID();
    if (used.has(id)) throw new Error("duplicate_native_request");
    used.add(id);
    if (method === "chat.send") {
      if (typeof params?.idempotencyKey !== "string" || typeof params?.sessionKey !== "string" || turns.size >= 4) throw new Error("native_turn_bound");
      turns.set(params.idempotencyKey, params.sessionKey);
    }
    const promise = new Promise((resolve, reject) => {
      const timer = target.setTimeout(() => { pending.delete(id); reject(new Error("native_rpc_timeout")); }, 15000);
      pending.set(id, { resolve, reject, timer });
      try { socket.send(JSON.stringify({ type: "req", id, method, params })); }
      catch { pending.delete(id); target.clearTimeout(timer); reject(new Error("native_send_failed")); }
    });
    // Retain only our probe outcomes. Native connect/auth responses are never stored.
    promise.then(value => completed.set(id, { status: "resolved", value }),
      () => completed.set(id, { status: "retired_or_timeout" }));
    return { id, promise };
  };
  target.__rmPersonWire = {
    state: () => ({ ready: ready && !retired, retired }),
    turnEvents: id => structuredClone(terminalEvents.get(id) ?? []),
    rpc: async (method, params) => start(method, params).promise,
    startRpc: (method, params) => start(method, params).id,
    outcome: id => completed.get(id) ?? null,
  };
}
