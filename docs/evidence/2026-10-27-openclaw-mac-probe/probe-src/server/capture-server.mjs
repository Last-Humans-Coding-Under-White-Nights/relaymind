// Loopback capture server for the RelayMind <-> OpenClaw Mac probe.
// Records every request (Authorization redacted) and answers in the
// OpenAI chat-completions shape, never sending a `usage` field.
//
// Usage: node capture-server.mjs <out-dir>
// Control: GET /__run?name=<label> sets the subdirectory for later captures.
//          GET /__mode?slow=1 makes the next completion hold the connection
//          open (for the SIGINT probe) and log when the client closes it.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const OUT = path.resolve(process.argv[2] ?? "./captures");
const PORT = 8089;
const MODEL = "relaymind-client-tools";
const EXPECTED = (process.env.EXPECTED_TOKENS ?? "").split(",").filter(Boolean);
let run = "unlabeled";
let seq = 0;
let slow = false;

const SECRET_HEADERS = new Set(["authorization", "x-api-key", "api-key", "cookie", "proxy-authorization"]);

function redactHeaders(headers) {
  const out = {};
  for (const [k, v] of Object.entries(headers)) {
    if (SECRET_HEADERS.has(k.toLowerCase())) {
      const s = String(v);
      const sp = s.indexOf(" ");
      const scheme = sp > 0 ? s.slice(0, sp) : "";
      const token = sp > 0 ? s.slice(sp + 1) : s;
      out[k] = `${scheme ? scheme + " " : ""}<redacted>`;
      out[`${k}__token_length`] = token.length;
      out[`${k}__matches_expected_index`] = EXPECTED.indexOf(token);
    } else {
      out[k] = v;
    }
  }
  return out;
}

function record(entry) {
  const dir = path.join(OUT, run);
  fs.mkdirSync(dir, { recursive: true });
  seq += 1;
  const file = path.join(dir, `${String(seq).padStart(3, "0")}-${entry.method}-${entry.path.replace(/[^a-z0-9]+/gi, "_")}.json`);
  fs.writeFileSync(file, JSON.stringify(entry, null, 2));
  return file;
}

function logEvent(event) {
  const dir = path.join(OUT, run);
  fs.mkdirSync(dir, { recursive: true });
  fs.appendFileSync(path.join(dir, "events.jsonl"), JSON.stringify({ t: new Date().toISOString(), ...event }) + "\n");
}

function findReadTool(tools) {
  const names = (tools ?? []).map((t) => t?.function?.name).filter(Boolean);
  return names.find((n) => n === "read") ?? names.find((n) => /read/i.test(n) && !/thread|ready/i.test(n));
}

function findWriteTool(tools) {
  const names = (tools ?? []).map((t) => t?.function?.name).filter(Boolean);
  return names.find((n) => n === "write") ?? names.find((n) => /write/i.test(n));
}

function textOf(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((p) => p?.text ?? "").join("");
  return "";
}

// Decide the reply: { text } or { toolCall: { name, arguments } }.
function plan(body) {
  const messages = body.messages ?? [];
  const last = messages[messages.length - 1];
  if (last?.role === "tool") {
    const result = textOf(last.content);
    const firstLine = result.split("\n").find((l) => l.trim()) ?? "";
    return { text: `The tool returned. First line: ${firstLine.trim()}` };
  }
  const userText = textOf([...messages].reverse().find((m) => m.role === "user")?.content);
  if (/write/i.test(userText) && /probe-out\.txt/.test(userText)) {
    const name = findWriteTool(body.tools);
    if (name) return { toolCall: { name, arguments: { path: "probe-out.txt", content: "written by probe\n" } } };
  }
  if (/probe-exec\.txt/.test(userText)) {
    const names = (body.tools ?? []).map((t) => t?.function?.name);
    if (names.includes("exec")) return { toolCall: { name: "exec", arguments: { title: "probe", command: /touch/i.test(userText) ? "touch probe-exec.txt" : "echo probe > probe-exec.txt" } } };
  }
  if (/notes\.txt/.test(userText)) {
    const name = findReadTool(body.tools);
    if (name) return { toolCall: { name, arguments: { path: "notes.txt" } } };
  }
  return { text: "hello from probe" };
}

function chunk(id, delta, finish = null) {
  return { id, object: "chat.completion.chunk", created: Math.floor(Date.now() / 1000), model: MODEL, choices: [{ index: 0, delta, finish_reason: finish }] };
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  if (url.pathname === "/__run") {
    run = url.searchParams.get("name") ?? "unlabeled";
    seq = 0;
    res.end(`run=${run}\n`);
    return;
  }
  if (url.pathname === "/__mode") {
    slow = url.searchParams.get("slow") === "1";
    res.end(`slow=${slow}\n`);
    return;
  }
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    const raw = Buffer.concat(chunks).toString("utf8");
    let body = null;
    try { body = raw ? JSON.parse(raw) : null; } catch { body = { __unparsed: raw }; }
    const entry = { received_at: new Date().toISOString(), method: req.method, path: url.pathname + url.search, headers: redactHeaders(req.headers), body_bytes: Buffer.byteLength(raw), body };
    const file = record(entry);
    logEvent({ event: "request", method: req.method, path: url.pathname, file: path.basename(file) });

    if (req.method === "GET" && url.pathname.endsWith("/models")) {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ object: "list", data: [{ id: MODEL, object: "model" }] }));
      return;
    }
    if (req.method === "POST" && url.pathname.endsWith("/chat/completions")) {
      const id = `chatcmpl-probe-${seq}`;
      if (slow) {
        // Hold the request open; log when the client goes away.
        logEvent({ event: "slow_hold_start", file: path.basename(file) });
        res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
        res.write(`data: ${JSON.stringify(chunk(id, { role: "assistant", content: "" }))}\n\n`);
        const timer = setInterval(() => res.write(": keep-alive\n\n"), 1000);
        res.on("close", () => { clearInterval(timer); logEvent({ event: "client_closed_connection", file: path.basename(file), writableEnded: res.writableEnded }); });
        req.socket.on("close", () => logEvent({ event: "socket_closed", file: path.basename(file) }));
        return;
      }
      const p = plan(body ?? {});
      logEvent({ event: "reply_plan", plan: p, stream: body?.stream === true });
      if (body?.stream === true) {
        res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
        const send = (obj) => res.write(`data: ${JSON.stringify(obj)}\n\n`);
        send(chunk(id, { role: "assistant", content: "" }));
        if (p.toolCall) {
          send(chunk(id, { tool_calls: [{ index: 0, id: "call_probe_1", type: "function", function: { name: p.toolCall.name, arguments: JSON.stringify(p.toolCall.arguments) } }] }));
          send(chunk(id, {}, "tool_calls"));
        } else {
          for (const piece of p.text.match(/.{1,6}/g)) send(chunk(id, { content: piece }));
          send(chunk(id, {}, "stop"));
        }
        res.write("data: [DONE]\n\n");
        res.end();
      } else {
        const message = p.toolCall
          ? { role: "assistant", content: null, tool_calls: [{ id: "call_probe_1", type: "function", function: { name: p.toolCall.name, arguments: JSON.stringify(p.toolCall.arguments) } }] }
          : { role: "assistant", content: p.text };
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ id, object: "chat.completion", created: Math.floor(Date.now() / 1000), model: MODEL, choices: [{ index: 0, message, finish_reason: p.toolCall ? "tool_calls" : "stop" }] }));
      }
      return;
    }
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { message: "not found" } }));
  });
});

server.listen(PORT, "127.0.0.1", () => console.log(`capture server on 127.0.0.1:${PORT}, out=${OUT}`));
