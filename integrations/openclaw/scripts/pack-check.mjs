// Check the package an operator installs (RM33, section 7.5; run after npm run build).
//
// npm pack --dry-run lists the tarball: it must hold the built entry, the plugin
// manifest, and the README, and nothing else from the checkout (no sources, tests,
// fixtures, or node_modules). The built JavaScript may import only Node built-ins,
// the public openclaw/plugin-sdk subpaths, and its own relative .js files.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const root = new URL("../", import.meta.url);
const pkg = JSON.parse(readFileSync(new URL("package.json", root), "utf8"));
const host = JSON.parse(readFileSync(new URL("node_modules/openclaw/package.json", root), "utf8"));
const npm = process.platform === "win32" ? "npm.cmd" : "npm";
const packed = JSON.parse(
  execFileSync(npm, ["pack", "--dry-run", "--json", "--ignore-scripts"], {
    cwd: root,
    encoding: "utf8",
  }),
);
// npm prints an array, or an object keyed by package name when run from a script.
const [report] = Array.isArray(packed) ? packed : Object.values(packed);
const files = report.files.map((f) => f.path).sort();
const problems = [];

for (const required of ["dist/index.js", "openclaw.plugin.json", "README.md", "package.json"]) {
  if (!files.includes(required)) problems.push(`missing ${required}`);
}
for (const file of files) {
  const allowed =
    ["openclaw.plugin.json", "README.md", "package.json"].includes(file) ||
    (file.startsWith("dist/") && file.endsWith(".js"));
  if (!allowed) problems.push(`unexpected file ${file}`);
}
for (const entry of pkg.openclaw.extensions) {
  if (!files.includes(entry.replace(/^\.\//, ""))) problems.push(`extension ${entry} is not packed`);
}

const exported = new Set(Object.keys(host.exports));
for (const file of files.filter((f) => f.startsWith("dist/"))) {
  const text = readFileSync(new URL(file, root), "utf8");
  for (const [, , specifier] of text.matchAll(/(?:from|import)\s*\(?\s*(["'])([^"']+)\1/g)) {
    if (specifier.startsWith("node:")) continue;
    if (specifier.startsWith("./") || specifier.startsWith("../")) {
      if (!specifier.endsWith(".js")) problems.push(`${file} imports ${specifier} (not built JavaScript)`);
      continue;
    }
    const sub = specifier.startsWith("openclaw/") ? `./${specifier.slice("openclaw/".length)}` : null;
    if (!sub || !sub.startsWith("./plugin-sdk/") || !exported.has(sub)) {
      problems.push(`${file} imports ${specifier} (only public openclaw/plugin-sdk subpaths)`);
    }
  }
}

if (problems.length) {
  console.error(`pack check failed:\n  ${problems.join("\n  ")}`);
  process.exit(1);
}
console.log(`pack check passed: ${report.filename} (${files.length} files): ${files.join(", ")}`);
