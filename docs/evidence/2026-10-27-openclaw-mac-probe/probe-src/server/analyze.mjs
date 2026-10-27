// Summarize captured chat-completions requests for the probe report.
// Usage: node analyze.mjs <run-dir>...
import fs from "node:fs";
import path from "node:path";

const WATCH = ["stream_options", "strict", "reasoning_effort", "store", "prompt_cache_key", "parallel_tool_calls"];

function typeOf(v) {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  return typeof v;
}

function textLen(content) {
  if (typeof content === "string") return content.length;
  if (Array.isArray(content)) return content.reduce((n, p) => n + (p?.text?.length ?? 0), 0);
  return 0;
}

const summary = {};
for (const dir of process.argv.slice(2)) {
  const files = fs.readdirSync(dir).filter((f) => f.endsWith(".json")).sort();
  const runOut = [];
  for (const f of files) {
    const e = JSON.parse(fs.readFileSync(path.join(dir, f), "utf8"));
    const b = e.body;
    if (!b || !Array.isArray(b.messages)) {
      runOut.push({ file: f, method: e.method, path: e.path, body_bytes: e.body_bytes });
      continue;
    }
    const topLevel = Object.fromEntries(Object.entries(b).map(([k, v]) => [k, typeOf(v)]));
    const watched = Object.fromEntries(WATCH.map((k) => [k, k in b ? b[k] : "<absent>"]));
    const toolStrict = (b.tools ?? []).map((t) => t?.function?.strict).filter((s) => s !== undefined);
    const roles = b.messages.map((m) => m.role);
    const extraKeys = new Set();
    for (const m of b.messages) for (const k of Object.keys(m)) if (!["role", "content", "tool_calls", "tool_call_id"].includes(k)) extraKeys.add(`${m.role}.${k}`);
    const toolMsgs = b.messages.filter((m) => m.role === "tool");
    const afterTool = [];
    b.messages.forEach((m, i) => {
      if (b.messages[i - 1]?.role === "tool" && m.role === "assistant") afterTool.push({ index: i, content: m.content, keys: Object.keys(m) });
    });
    const sys = b.messages.filter((m) => m.role === "system" || m.role === "developer");
    runOut.push({
      file: f,
      body_bytes: e.body_bytes,
      headers_of_interest: Object.fromEntries(Object.entries(e.headers).filter(([k]) => /^(authorization|x-relaymind|idempotency|user-agent|content-type|accept|x-stainless|openai|x-)/i.test(k))),
      top_level: topLevel,
      watched,
      tools_with_function_strict: toolStrict.length ? toolStrict : "<none>",
      roles,
      system_or_developer: sys.map((m) => ({ role: m.role, chars: textLen(m.content), content_type: typeOf(m.content) })),
      tool_message_keys: toolMsgs.map((m) => Object.keys(m)),
      tool_message_has_name: toolMsgs.map((m) => "name" in m),
      assistant_after_tool_result: afterTool,
      message_keys_beyond_basic: [...extraKeys],
      tool_count: (b.tools ?? []).length,
      tool_names: (b.tools ?? []).map((t) => t?.function?.name),
      tool_choice: b.tool_choice ?? "<absent>",
    });
  }
  summary[path.basename(dir)] = runOut;
}
console.log(JSON.stringify(summary, null, 2));
