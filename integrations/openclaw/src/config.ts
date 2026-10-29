// The plugin configuration (RM12, RM19, RM26, RM33; spec sections 7.1 and 7.5).
//
// Three settings, mirrored by the closed configSchema of openclaw.plugin.json:
// - baseUrl: the gateway's /v1 root on a loopback address only. The gateway is a
//   separately started process; the plugin never forwards to another host.
// - executionMode: auto, local_only, or external_only, sent on every request as
//   X-RelayMind-Execution-Mode. parallel_demo and unknown values are refused by
//   name, never changed into another mode.
// - limits: copies of the gateway's configured limits; a limit that is not set is
//   never estimated (section 7.5).
// The application token is not configuration: it lives in the host's credential
// store (the api-key auth method), so no error here can echo it.

export const EXECUTION_MODES = ["auto", "local_only", "external_only"] as const;
export type ExecutionMode = (typeof EXECUTION_MODES)[number];

export interface RelayMindLimits {
  /** external_model.context_limit of the gateway's launch file. */
  externalContextLimit?: number;
  /** The local context of the pinned model manifest's launch settings. */
  localContextLimit?: number;
  /** chat.max_completion_tokens of the gateway's launch file. */
  maxCompletionTokens?: number;
}

export interface RelayMindPluginConfig {
  /** The gateway's /v1 root, without a trailing slash. */
  baseUrl: string;
  executionMode: ExecutionMode;
  limits: RelayMindLimits;
}

/** A configuration the plugin refuses; the message names the setting, never a secret. */
export class RelayMindConfigError extends Error {
  override name = "RelayMindConfigError";
}

const KEYS = new Set(["baseUrl", "executionMode", "limits"]);
const LIMIT_KEYS = ["externalContextLimit", "localContextLimit", "maxCompletionTokens"] as const;
const SAFE_TO_ECHO = /^[a-z_]{1,32}$/;

function fail(message: string): never {
  throw new RelayMindConfigError(`RelayMind plugin configuration: ${message}`);
}

/** localhost, 127.0.0.0/8, or ::1, as the WHATWG URL parser normalizes them; no DNS. */
export function isLoopbackHostname(hostname: string): boolean {
  if (hostname === "localhost" || hostname === "[::1]") return true;
  const octets = hostname.split(".");
  return (
    octets.length === 4 &&
    octets[0] === "127" &&
    octets.every((o) => /^\d{1,3}$/.test(o) && Number(o) <= 255)
  );
}

function parseBaseUrl(raw: unknown): string {
  if (raw === undefined || raw === null || raw === "") {
    fail("baseUrl is required (the gateway's loopback /v1 URL, such as http://127.0.0.1:8080/v1)");
  }
  if (typeof raw !== "string") fail("baseUrl must be a string");
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    fail("baseUrl is not a valid URL");
  }
  if (url.username || url.password) {
    fail("baseUrl must not contain credentials; store the token with the host's credential setup");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") fail("baseUrl must use http or https");
  if (!isLoopbackHostname(url.hostname)) {
    fail(
      `baseUrl host ${url.hostname} is not a loopback address; ` +
        "the gateway serves only localhost, 127.0.0.0/8, or [::1]",
    );
  }
  if (url.search || url.hash) fail("baseUrl must not have a query or a fragment");
  const path = url.pathname.replace(/\/+$/, "");
  if (path !== "/v1") fail("baseUrl must be the gateway's /v1 root, such as http://127.0.0.1:8080/v1");
  return `${url.origin}/v1`;
}

function parseMode(raw: unknown): ExecutionMode {
  if (raw === undefined) return "auto";
  if (typeof raw === "string" && (EXECUTION_MODES as readonly string[]).includes(raw)) {
    return raw as ExecutionMode;
  }
  const shown = typeof raw === "string" && SAFE_TO_ECHO.test(raw) ? JSON.stringify(raw) : "the value";
  const why = raw === "parallel_demo" ? " (parallel_demo is not a mode of relaymind-client-tools)" : "";
  fail(`executionMode ${shown} is not allowed${why}; use one of auto, local_only, external_only`);
}

function parseLimits(raw: unknown): RelayMindLimits {
  if (raw === undefined) return {};
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) fail("limits must be an object");
  const limits: RelayMindLimits = {};
  for (const key of Object.keys(raw)) {
    if (!(LIMIT_KEYS as readonly string[]).includes(key)) {
      fail(`limits.${key} is not a setting; use ${LIMIT_KEYS.join(", ")}`);
    }
  }
  for (const key of LIMIT_KEYS) {
    const value = (raw as Record<string, unknown>)[key];
    if (value === undefined) continue;
    if (typeof value !== "number" || !Number.isInteger(value) || value < 1) {
      fail(`limits.${key} must be a positive integer`);
    }
    limits[key] = value;
  }
  return limits;
}

/** Check the plugin configuration; throw a RelayMindConfigError naming the first problem. */
export function parsePluginConfig(raw: unknown): RelayMindPluginConfig {
  if (raw === undefined || raw === null) parseBaseUrl(undefined);
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    fail("the configuration must be an object");
  }
  const record = raw as Record<string, unknown>;
  for (const key of Object.keys(record)) {
    if (!KEYS.has(key)) {
      fail(
        `${key} is not a setting (the settings are baseUrl, executionMode, limits); ` +
          "a credential belongs in the host's credential setup, not here",
      );
    }
  }
  return {
    baseUrl: parseBaseUrl(record.baseUrl),
    executionMode: parseMode(record.executionMode),
    limits: parseLimits(record.limits),
  };
}
