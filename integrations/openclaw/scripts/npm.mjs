// Shared by the pack check and the release build (#145; RM33, section 7.5).
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));

// npm through node when run by npm: a Windows npm.cmd shim cannot be spawned
// without a shell (Node refuses .cmd files since 20.12).
export function npm(args) {
  const cli = process.env.npm_execpath;
  const [file, argv] = cli ? [process.execPath, [cli, ...args]] : ["npm", args];
  return execFileSync(file, argv, {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "inherit"],
    shell: !cli && process.platform === "win32",
  });
}

// The one package report of npm pack --json: npm prints an array, or an object
// keyed by package name when run from a script.
export function packReport(stdout) {
  const packed = JSON.parse(stdout);
  const [report] = Array.isArray(packed) ? packed : Object.values(packed);
  return report;
}
