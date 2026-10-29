// Recording loopback proxy for the #136 Linux VPS functional check.
// Usage: node record_proxy.mjs <upstream origin> <port> <out.json>
// Forwards every request unchanged to the gateway and records it with the
// package's own recorder (Authorization is never recorded). Writes the
// exchanges to <out.json> after every response and on SIGTERM.
import { writeFileSync } from "node:fs";
import { startRecorder } from "../../../../integrations/openclaw/test/host-transport/recorder.mjs";

const [upstream, port, out] = process.argv.slice(2);
const recorder = await startRecorder({ upstream, port: Number(port) });
const flush = () => writeFileSync(out, JSON.stringify(recorder.exchanges, null, 2) + "\n");
const timer = setInterval(flush, 200);
process.on("SIGTERM", async () => {
  clearInterval(timer);
  flush();
  await recorder.close();
  process.exit(0);
});
console.log(recorder.origin);
