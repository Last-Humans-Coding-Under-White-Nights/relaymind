// Copy the Mac qualification artifacts into the evidence dir, sanitized.
// Usage: SANITIZE_HOSTS=<a,b,c> node sanitize-qual.mjs <scratch> <evidence-dir>
// Gateway tokens are read from the serve logs; they never appear in the output.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const [S, OUT] = process.argv.slice(2);
const Q = path.join(S, "qual");
const home = os.homedir();
const user = path.basename(home);
const tokens = ["serve1.out", "serve2.out"]
  .map((f) => fs.readFileSync(path.join(Q, "gw", f), "utf8").match(/^Application token: (.+)$/m)?.[1]?.trim())
  .filter(Boolean);
tokens.push(fs.readFileSync(path.join(S, "probe_token.txt"), "utf8").trim());

const rules = [
  ...tokens.map((t) => [t, "<redacted>"]),
  [/(Authorization["']?\s*[:=]\s*["']?Bearer )[^"'\s]+/gi, "$1<redacted>"],
  [S, "<scratch>"],
  [S.replace(/^\/private/, ""), "<scratch>"],
  [/\/(private\/)?var\/folders\/[^/\s"]+\/[^/\s"]+\/T/g, "<os-tmpdir>"],
  [home, "<home>"],
  // Host names, comma separated (computer name, local host name, host name).
  // Each also in its JSON \uXXXX-escaped form (ensure_ascii output).
  ...(process.env.SANITIZE_HOSTS ?? "").split(",").filter(Boolean).flatMap((h) => [
    [h, "<host>"],
    [h.replace(/[^\x00-\x7f]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0")), "<host>"],
  ]),
  [new RegExp(user, "gi"), "<user>"],
  [/"apiKey":\s*"[^"]*"/g, '"apiKey": "<redacted>"'],
  [/\x1b\[[0-9;?]*[a-zA-Z]/g, ""],
];

function clean(text) {
  for (const [from, to] of rules) text = typeof from === "string" ? text.split(from).join(to) : text.replace(from, to);
  return text;
}

function copy(src, rel) {
  if (!fs.existsSync(src)) return console.log(`missing ${src}`);
  const dest = path.join(OUT, rel);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, clean(fs.readFileSync(src, "utf8")));
}

const dir = (d, rel, filter = () => true) => {
  for (const f of fs.readdirSync(d).sort()) if (fs.statSync(path.join(d, f)).isFile() && filter(f)) copy(path.join(d, f), `${rel}/${f}`);
};

dir(path.join(Q, "step1"), "step1");
dir(path.join(Q, "cli"), "cli");
for (const f of ["inspect.json", "inspect-nonloopback.json", "inspect-hosttools.json"]) copy(path.join(Q, f), f);
copy(path.join(Q, "config-good.json"), "configs/qual-config.json");
copy(path.join(S, "qoc", "config-row.json"), "configs/qual-config-row.json");
copy(path.join(S, "qoc", "capture.json"), "configs/capture.json");
for (const f of ["config-row.json", "config-row-direct.json", "config-tool-deny.json", "config-tool-approve.json"]) copy(path.join(S, "hoc", f), `configs/harness-${f}`);
copy(path.join(Q, "gw", "relaymind.toml"), "configs/relaymind.toml");
dir(path.join(Q, "host"), "host");
dir(path.join(Q, "exports"), "exports");
copy(path.join(Q, "request-size.json"), "request-size.json");
copy(path.join(Q, "request-size-probe.json"), "request-size-probe-requests.json");
copy(path.join(Q, "runs-real-hello.json"), "live/exec-plugin-catalog-only.json");
copy(path.join(Q, "runs-real-hello.err"), "live/exec-plugin-catalog-only.err");
copy(path.join(Q, "runs-real-hello-row.json"), "live/exec-config-row.json");
copy(path.join(Q, "runs-real-hello-row.err"), "live/exec-config-row.err");
copy(path.join(Q, "run-real-hello.json"), "live/gateway-run.json");
copy(path.join(Q, "run-real-hello-events.txt"), "live/gateway-run-events.txt");
copy(path.join(Q, "gw", "serve1.out"), "live/serve1.out");
copy(path.join(Q, "gw", "serve2.out"), "live/serve2.out");
copy(path.join(Q, "abort_run.py"), "scaffold/abort_run.py");
copy(path.join(Q, "sanitize-qual.mjs"), "scaffold/sanitize-qual.mjs");
console.log("done");
