// Build the release artifact an operator installs (#145; RM33, section 7.5).
//
// Usage: npm run pack:release -- <destination directory>
//
// From a clean dist/ (npm run build), after the pack check, npm pack writes the
// tarball into the destination with SHA256SUMS and release.json: the package,
// its version, the tarball's SHA-256 and npm integrity, the packed files, the
// pinned OpenClaw release, the declared Node range, and the Node that built it.
// npm pack fixes file dates and order, and the build is deterministic, so two
// builds of one checkout are the same bytes (tests/test_openclaw_install.py).
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const dest = process.argv[2];
if (!dest) {
  console.error("usage: npm run pack:release -- <destination directory>");
  process.exit(2);
}
const out = resolve(process.env.INIT_CWD ?? process.cwd(), dest);
// Under the package, the build would delete it (dist/) or npm pack could ship it.
const inside = relative(root, out);
if (!inside.startsWith("..") && !isAbsolute(inside)) {
  console.error(`the destination must be outside the package: ${out}`);
  process.exit(2);
}
mkdirSync(out, { recursive: true });

// npm through node: a Windows npm.cmd shim cannot be spawned without a shell.
function npm(args) {
  const cli = process.env.npm_execpath;
  const [file, argv] = cli ? [process.execPath, [cli, ...args]] : ["npm", args];
  return execFileSync(file, argv, {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "inherit"],
    shell: !cli && process.platform === "win32",
  });
}

npm(["run", "--silent", "pack:check"]);
const packed = JSON.parse(npm(["pack", "--json", "--ignore-scripts", "--pack-destination", out]));
const [report] = Array.isArray(packed) ? packed : Object.values(packed);
const pkg = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));
const host = JSON.parse(readFileSync(resolve(root, "node_modules/openclaw/package.json"), "utf8"));
const tarball = readFileSync(resolve(out, report.filename));
const sha256 = createHash("sha256").update(tarball).digest("hex");

const record = {
  package: pkg.name,
  version: pkg.version,
  filename: report.filename,
  sha256,
  integrity: report.integrity,
  files: report.files.map((f) => f.path).sort(),
  openclaw: {
    peer: pkg.peerDependencies.openclaw,
    pluginApi: pkg.openclaw.compat.pluginApi,
    built_with: host.version,
  },
  engines: pkg.engines,
  node: process.version,
};
writeFileSync(resolve(out, "SHA256SUMS"), `${sha256}  ${report.filename}\n`);
writeFileSync(resolve(out, "release.json"), `${JSON.stringify(record, null, 2)}\n`);
console.log(`release artifact: ${resolve(out, report.filename)} sha256 ${sha256}`);
