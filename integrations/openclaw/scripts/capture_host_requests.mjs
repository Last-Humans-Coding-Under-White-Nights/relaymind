// The controlled endpoint for capturing a real OpenClaw host's requests (RM33, #135).
//
// A loopback Chat Completions endpoint that records every request the host sends
// and answers with a scripted reply, so the host's own system prompt, tool
// catalog, history, and request fields can be frozen as fixtures and measured
// (scripts/openclaw_request_size.py). It is not the gateway and runs no model.
//
// Usage:
//   node scripts/capture_host_requests.mjs --port 8089 \
//     --out test/fixtures/v2026.10.7/host/<name>.json [--tool-call relaymind_test_echo]
//
// The first reply is a call to the named host tool when --tool-call is given
// (arguments {"text": "lantern"} unless --tool-arguments '<json>'), then text.
// The fixture is written after every request and on Ctrl-C. Authorization is
// never recorded; the home directory is replaced by <home>, in its raw and its
// JSON-escaped form (capture-sanitize.mjs). Review a host
// fixture before committing it: the host's system prompt can name local paths.
import { writeFileSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname } from "node:path";
import { parseArgs } from "node:util";

import { startRecorder } from "../test/host-transport/recorder.mjs";
import { sanitizeCapture } from "./capture-sanitize.mjs";

const { values } = parseArgs({
  options: {
    port: { type: "string", default: "8089" },
    out: { type: "string" },
    "tool-call": { type: "string" },
    "tool-arguments": { type: "string", default: '{"text":"lantern"}' },
    text: { type: "string", default: "Captured by the RelayMind qualification endpoint." },
  },
});
if (!values.out) {
  console.error("--out <fixture.json> is required");
  process.exit(2);
}

const MODEL = "relaymind-client-tools";

function chunk(delta, finish = null) {
  return {
    id: "chatcmpl-capture",
    object: "chat.completion.chunk",
    created: 0,
    model: MODEL,
    choices: [{ index: 0, delta, finish_reason: finish }],
  };
}

function reply(request, index) {
  const call =
    values["tool-call"] && index === 0
      ? {
          id: "call_capture_1",
          type: "function",
          function: { name: values["tool-call"], arguments: values["tool-arguments"] },
        }
      : null;
  const finish = call ? "tool_calls" : "stop";
  if (request.body?.stream) {
    const events = [
      chunk({ role: "assistant", content: call ? null : "" }),
      chunk(call ? { tool_calls: [{ index: 0, ...call }] } : { content: values.text }),
      chunk({}, finish),
    ];
    const body = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") + "data: [DONE]\n\n";
    return { status: 200, headers: { "content-type": "text/event-stream" }, body };
  }
  const message = call
    ? { role: "assistant", content: null, tool_calls: [call] }
    : { role: "assistant", content: values.text };
  return {
    status: 200,
    headers: { "content-type": "application/json" },
    body: {
      id: "chatcmpl-capture",
      object: "chat.completion",
      created: 0,
      model: MODEL,
      choices: [{ index: 0, message, finish_reason: finish }],
    },
  };
}

const home = homedir();
function write(recorder) {
  const fixture = {
    fixture_version: 1,
    captured_by: "integrations/openclaw/scripts/capture_host_requests.mjs",
    note: "A real OpenClaw host against a scripted endpoint; no gateway, no model.",
    exchanges: recorder.exchanges,
  };
  const text = sanitizeCapture(JSON.stringify(fixture, null, 2), home);
  mkdirSync(dirname(values.out), { recursive: true });
  writeFileSync(values.out, `${text}\n`);
}

const recorder = await startRecorder({
  replies: (request, index) => {
    const answer = reply(request, index);
    setImmediate(() => write(recorder));
    return answer;
  },
  port: Number(values.port),
});
console.error(`capturing on ${recorder.origin}/v1; Ctrl-C to stop`);
process.on("SIGINT", async () => {
  write(recorder);
  await recorder.close();
  process.exit(0);
});
