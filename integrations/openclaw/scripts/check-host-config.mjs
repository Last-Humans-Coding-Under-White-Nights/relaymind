// Spec 0.65 section 12.6.5; RM16/RM39/RM40/RM44, AC31/AC35/AC36/AC37.
// Native preflight only: this verdict is not a qualified evidence attestation.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const unavailable = { status: "unavailable", validator: null, issue_path: null };
const write = process.stdout.write.bind(process.stdout);
// Native diagnostics may contain private config values. Emit only our verdict.
process.stdout.write = () => true;
process.stderr.write = () => true;
let finished = false;
function finish(verdict) {
  if (finished) return;
  finished = true;
  write(JSON.stringify(verdict) + "\n", () => process.exit(0));
}
setTimeout(() => finish(unavailable), 10_000);

try {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 4 * 1024 * 1024) {
      finish(unavailable);
      break;
    }
    chunks.push(chunk);
  }
  if (!finished) {
    const config = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    const root = resolve(process.argv[2]);
    const pkg = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));
    if (pkg.name !== "openclaw" || pkg.version !== "2026.10.7") throw Error();
    // Exact pinned distribution entry points; no glob choosing an arbitrary build.
    const validatorFile = "dist/validation-ongxOmrj.mjs";
    const validator = {
      host_version: pkg.version,
      validator_file: validatorFile,
      validator_sha256: createHash("sha256").update(readFileSync(resolve(root, validatorFile))).digest("hex"),
    };
    const { r: withDefaults, n: rawWithPlugins } = await import(pathToFileURL(resolve(root, validatorFile)).href);
    const { r: finishCliValidation } = await import(pathToFileURL(resolve(root, "dist/config-cli-validation-Dnu71lrA.mjs")).href);
    const options = { semanticValidation: "strict", pluginValidation: "full" };
    const raw = rawWithPlugins(config, options);
    const effective = raw.ok ? withDefaults(config, options) : raw;
    // Same final static secret-provider checks as `openclaw config validate`.
    const snapshot = effective.ok ? await finishCliValidation({
      snapshot: { valid: true, exists: true, runtimeConfig: effective.config },
      strictIssues: [],
    }) : { valid: false, issues: effective.issues };
    if (snapshot.valid) finish({ status: "valid", validator, issue_path: null });
    else {
      const issuePath = snapshot.issues?.[0]?.path;
      if (typeof issuePath !== "string" || Buffer.byteLength(issuePath) > 1024) throw Error();
      finish({ status: "invalid", validator, issue_path: issuePath });
    }
  }
} catch {
  finish(unavailable);
}
