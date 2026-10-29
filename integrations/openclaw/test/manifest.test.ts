// The package and plugin manifests (RM33, section 7.5: pinned release, public SDK only).
import { readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { MODEL_ID, PINNED_OPENCLAW, PROVIDER_ID } from "../src/compat.ts";
import { EXECUTION_MODES } from "../src/config.ts";

const root = new URL("../", import.meta.url);
const read = (path: string) => JSON.parse(readFileSync(new URL(path, root), "utf8"));
const manifest = read("openclaw.plugin.json");
const pkg = read("package.json");
const host = read("node_modules/openclaw/package.json");

describe("openclaw.plugin.json", () => {
  it("declares the relaymind provider", () => {
    expect(manifest.id).toBe(PROVIDER_ID);
    expect(manifest.providers).toEqual([PROVIDER_ID]);
    expect(typeof manifest.name).toBe("string");
    expect(typeof manifest.description).toBe("string");
  });

  it("declares the token's environment variable and the api-key auth choice", () => {
    expect(manifest.setup.providers).toEqual([{ id: PROVIDER_ID, envVars: ["RELAYMIND_APP_TOKEN"] }]);
    const [choice] = manifest.providerAuthChoices;
    expect(choice.provider).toBe(PROVIDER_ID);
    expect(choice.method).toBe("api-key");
    expect(choice.cliFlag).toBe("--relaymind-app-token");
    expect(manifest.providerAuthChoices).toHaveLength(1);
  });

  it("has a closed config schema that matches the plugin's own checks", () => {
    const schema = manifest.configSchema;
    expect(schema.type).toBe("object");
    expect(schema.additionalProperties).toBe(false);
    expect(schema.required).toEqual(["baseUrl"]);
    expect(Object.keys(schema.properties).sort()).toEqual([
      "baseUrl",
      "diagnosticsPath",
      "executionMode",
      "limits",
      "maxRetries",
    ]);
    expect(schema.properties.diagnosticsPath).toMatchObject({ type: "string", minLength: 1 });
    expect(schema.properties.maxRetries).toMatchObject({ type: "integer", minimum: 0, maximum: 2, default: 0 });
    // parallel_demo is not a mode of this alias (section 7.5).
    expect(schema.properties.executionMode.enum).toEqual([...EXECUTION_MODES]);
    expect(schema.properties.executionMode.default).toBe("auto");
    const limits = schema.properties.limits;
    expect(limits.additionalProperties).toBe(false);
    expect(Object.keys(limits.properties).sort()).toEqual([
      "externalContextLimit",
      "localContextLimit",
      "maxCompletionTokens",
    ]);
    for (const limit of Object.values(limits.properties) as { type: string; minimum: number }[]) {
      expect(limit).toMatchObject({ type: "integer", minimum: 1 });
    }
  });

  it("leaves parallel_demo out of the executionMode enum, so the host refuses it first (section 7.5)", () => {
    // The pinned host validates plugins.entries.relaymind.config against this
    // schema before any plugin code runs: `config set ... executionMode
    // parallel_demo` is declined by the host's own validation, and the
    // plugin's own refusal (src/config.ts) is the second line only.
    const mode = manifest.configSchema.properties.executionMode;
    expect(mode.type).toBe("string");
    expect(mode.enum).not.toContain("parallel_demo");
    expect(mode.enum).toEqual(["auto", "local_only", "external_only"]);
  });

  it("names no credential field in the config schema (RM12)", () => {
    expect(JSON.stringify(manifest.configSchema)).not.toMatch(/apiKey|appToken|secret|password/i);
  });
});

describe("package.json", () => {
  it("loads the built entry, not TypeScript source", () => {
    expect(pkg.type).toBe("module");
    expect(pkg.openclaw.extensions).toEqual(["./dist/index.js"]);
    expect(pkg.openclaw.providers).toEqual([PROVIDER_ID]);
    expect(pkg.files).toEqual(expect.arrayContaining(["dist", "openclaw.plugin.json", "README.md"]));
  });

  it("pins the qualified OpenClaw release and declares its compatibility", () => {
    expect(pkg.openclaw.build.openclawVersion).toBe(PINNED_OPENCLAW);
    expect(pkg.openclaw.build.pluginSdkVersion).toBe(PINNED_OPENCLAW);
    // Only the qualified release is declared compatible (section 7.5): a later
    // host is a new qualification (#135 capture), not an assumption.
    expect(pkg.openclaw.compat.pluginApi).toBe(PINNED_OPENCLAW);
    expect(pkg.openclaw.compat.minGatewayVersion).toBe(PINNED_OPENCLAW);
    expect(pkg.devDependencies.openclaw).toBe(PINNED_OPENCLAW);
    expect(pkg.devDependencies["@openclaw/ai"]).toBe(PINNED_OPENCLAW);
    expect(host.version).toBe(PINNED_OPENCLAW);
    // The host provides openclaw/plugin-sdk at runtime.
    expect(pkg.peerDependencies.openclaw).toBe(PINNED_OPENCLAW);
    expect(pkg.engines.node).toBe(host.engines.node);
  });

  it("is accepted by the pinned host's own range check only on the qualified release", async () => {
    // The host's package-compat module decides whether a plugin loads.
    const dist = new URL("node_modules/openclaw/dist/", root);
    const [file] = readdirSync(dist).filter((name) => /^package-compat-.*\.mjs$/.test(name));
    expect(file).toBeDefined();
    const compat = await import(new URL(file!, dist).href);
    const satisfies = compat.n as (version: string, range: string) => boolean;
    const range = pkg.openclaw.compat.pluginApi as string;
    expect(satisfies(PINNED_OPENCLAW, range)).toBe(true);
    // A numeric correction of the same release is the same release.
    expect(satisfies(`${PINNED_OPENCLAW}-1`, range)).toBe(true);
    for (const later of ["2026.10.8", "2026.11.1", "2027.1.1"]) {
      expect(satisfies(later, range)).toBe(false);
    }
    expect(satisfies("2026.10.6", range)).toBe(false);
  });

  it("has no runtime dependency the host would have to install", () => {
    expect(pkg.dependencies ?? {}).toEqual({});
  });

  it("has the build, typecheck, test, and pack check scripts", () => {
    for (const script of ["build", "typecheck", "test", "pack:check"]) {
      expect(typeof pkg.scripts[script]).toBe("string");
    }
  });
});

describe("source imports (public SDK only)", () => {
  const exported = new Set(Object.keys(host.exports));
  const sources = readdirSync(new URL("src/", root)).filter((f) => f.endsWith(".ts"));

  it.each(sources)("%s imports openclaw only through published plugin-sdk subpaths", (file) => {
    const text = readFileSync(new URL(`src/${file}`, root), "utf8");
    const specifiers = [...text.matchAll(/(?:from|import)\s*\(?\s*(["'])([^"']+)\1/g)].map((m) => m[2]!);
    for (const specifier of specifiers) {
      expect(specifier).not.toMatch(/node_modules|\/dist\//);
      if (specifier === "openclaw" || specifier.startsWith("openclaw/")) {
        expect(specifier.startsWith("openclaw/plugin-sdk/")).toBe(true);
        expect(exported.has(`./${specifier.slice("openclaw/".length)}`)).toBe(true);
      }
      expect(specifier.startsWith("@openclaw/")).toBe(false);
    }
  });

  it("serves the one alias", () => {
    expect(MODEL_ID).toBe("relaymind-client-tools");
  });
});
