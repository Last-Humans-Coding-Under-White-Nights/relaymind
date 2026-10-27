// Copy probe artifacts into the evidence dir, sanitized.
// Usage: node sanitize-copy.mjs <scratch> <evidence-dir>
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const [S, OUT] = process.argv.slice(2);
const token = fs.readFileSync(path.join(S, "probe_token.txt"), "utf8").trim();
const home = os.homedir();
const user = path.basename(home);

const rules = [
  [token, "<redacted>"],
  [S, "<scratch>"],
  [S.replace(/^\/private/, ""), "<scratch>"],
  // A scratch path a CLI table cut short ("/private/tmp/<name>-<uid>...") never
  // matches the full path above.
  [/\/(private\/)?tmp\/[A-Za-z]+-\d+[^\s"│]*/g, "<scratch>..."],
  [/\/(private\/)?var\/folders\/[^/\s"]+\/[^/\s"]+\/T/g, "<os-tmpdir>"],
  [home, "~"],
  // Host names to hide, comma separated (computer name, local host name).
  ...(process.env.SANITIZE_HOSTS ?? "").split(",").filter(Boolean).map((h) => [h, "<host>"]),
  [new RegExp(user, "g"), "<user>"],
  [/"apiKey":\s*"[^"]*"/g, '"apiKey": "<redacted>"'],
  [/\x1b\[[0-9;?]*[a-zA-Z]/g, ""],
];

function clean(text) {
  for (const [from, to] of rules) text = typeof from === "string" ? text.split(from).join(to) : text.replace(from, to);
  return text;
}

function put(rel, text) {
  const dest = path.join(OUT, rel);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, clean(text));
}

function copyFile(src, rel) {
  if (fs.existsSync(src)) put(rel, fs.readFileSync(src, "utf8"));
}

// Captured requests, one dir per run.
for (const run of fs.readdirSync(path.join(S, "captures")).sort()) {
  const dir = path.join(S, "captures", run);
  for (const f of fs.readdirSync(dir)) copyFile(path.join(dir, f), `requests/${run}/${f}`);
}
// Exec envelopes and stderr per run.
for (const f of fs.readdirSync(S)) {
  if (/^(out|err)-.+\.(json|txt)$/.test(f)) copyFile(path.join(S, f), `runs/${f}`);
}
// CLI transcripts.
for (const f of ["refresh.out", "paste.out", "disable.out", "uninstall.out", "list.out", "inspect.err"]) copyFile(path.join(S, f), `cli/${f}`);
// Host configs used (temp dirs only).
for (const f of fs.readdirSync(path.join(S, "oc"))) if (f.endsWith(".json")) copyFile(path.join(S, "oc", f), `configs/${f}`);
// Plugin inspect output and hook log.
copyFile(path.join(S, "inspect.json"), "inspect.json");
copyFile(path.join(S, "inspect-0.1.0.json"), "inspect-0.1.0.json");
copyFile(path.join(S, "probe-hooks.jsonl"), "plugin-hooks.jsonl");
// Sources.
for (const f of ["server/capture-server.mjs", "server/analyze.mjs", "plugin/package.json", "plugin/openclaw.plugin.json", "plugin/dist/index.js"]) copyFile(path.join(S, "probe-src", f), `probe-src/${f}`);
copyFile(path.join(S, "sanitize-copy.mjs"), "probe-src/sanitize-copy.mjs");
console.log("done");
