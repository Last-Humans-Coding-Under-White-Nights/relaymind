// Catalog discovery and diagnostics (RM08, RM19, RM26, RM33; spec sections 7.2, 7.4, 7.5).
//
// The catalog asks the configured loopback gateway, with the host credential,
// for its aliases (GET /v1/models) and for the readiness of the configured mode
// (GET /readyz). It publishes the one client-owned alias, relaymind-client-tools,
// only when the gateway lists it; it never publishes the managed alias. The
// diagnosis tells apart: no stored token, an unreachable gateway, a rejected
// token (with the reconnect path), another gateway error, an absent alias, and
// the readiness of the configured mode alone, so an aggregate /readyz 503 never
// reads as "every mode is unavailable" (an external_only setup stays usable when
// the local model is absent).
//
// No redirect is followed: a redirect could carry the credential to another host.

import { MODEL_ID, PROVIDER_ID, relaymindModelEntry, type RelayMindModelEntry } from "./compat.ts";
import type { ExecutionMode, RelayMindPluginConfig } from "./config.ts";
import { RECONNECT, TOKEN_HANDOVER } from "./diagnostics.ts";

export const MODE_HEADER = "X-RelayMind-Execution-Mode";
/**
 * How long each discovery request waits for the gateway. The pinned host gives
 * catalog.run no abort signal (macOS host probe, OpenClaw 2026.10.7), so this
 * timeout is what ends a hung gateway; a host signal, when given, also applies.
 */
export const DISCOVERY_TIMEOUT_MS = 5_000;

export type DiagnosisKind =
  | "ready"
  | "no_credential"
  | "gateway_unreachable"
  | "auth_failed"
  | "gateway_error"
  | "alias_absent"
  | "mode_not_ready"
  | "mode_forbidden"
  | "readiness_unknown";

export interface CatalogDiagnosis {
  kind: DiagnosisKind;
  message: string;
}

export interface RelayMindCatalogModel extends RelayMindModelEntry {
  headers: Record<string, string>;
}

export interface Discovery {
  diagnosis: CatalogDiagnosis;
  /** The published entries: the client-owned alias, or none. */
  models: RelayMindCatalogModel[];
}

export interface DiscoverParams {
  config: RelayMindPluginConfig;
  /** The credential for the discovery requests; undefined when none is stored. */
  token: string | undefined;
  fetch?: typeof fetch;
  /** The host's acquisition signal, when it gives one; its abort propagates. */
  signal?: AbortSignal;
  /** The per-request timeout; DISCOVERY_TIMEOUT_MS when not set (tests shorten it). */
  timeoutMs?: number;
}

/**
 * The advertised context window: the larger limit the mode can use. The host
 * refuses a request over it before sending, so a local window in auto would
 * refuse requests the gateway sends to the external model.
 */
function advertisedWindow(config: RelayMindPluginConfig): { window: number | undefined; local: boolean } {
  const local = config.executionMode === "local_only";
  const { externalContextLimit, localContextLimit } = config.limits;
  return { window: local ? localContextLimit : externalContextLimit, local };
}

/** The model entry of the alias for the configured mode and limits (section 7.5). */
export function catalogModel(config: RelayMindPluginConfig): RelayMindCatalogModel {
  const { maxCompletionTokens } = config.limits;
  const { window } = advertisedWindow(config);
  const entry = relaymindModelEntry({
    baseUrl: config.baseUrl,
    ...(window !== undefined ? { contextWindow: window } : {}),
    ...(maxCompletionTokens !== undefined ? { maxTokens: maxCompletionTokens } : {}),
  });
  return {
    ...entry,
    name: `RelayMind client-owned tools (${config.executionMode})`,
    headers: { [MODE_HEADER]: config.executionMode },
  };
}

function describeLimits(config: RelayMindPluginConfig): string {
  const mode = config.executionMode;
  const { window, local } = advertisedWindow(config);
  const which = local ? "local limit" : "external limit";
  const windowText =
    window === undefined
      ? `no context window advertised (no ${which} configured for mode ${mode})`
      : `context window ${window} (${which}, mode ${mode})`;
  const output = config.limits.maxCompletionTokens;
  const outputText =
    output === undefined ? "no output limit advertised" : `output limit ${output} (chat.max_completion_tokens)`;
  return `${windowText}; ${outputText}`;
}

class Unreachable extends Error {}

function causeCode(error: unknown): string {
  const cause = (error as { cause?: { code?: unknown } } | undefined)?.cause;
  const code = cause?.code ?? (error as { code?: unknown } | undefined)?.code;
  return typeof code === "string" && /^[A-Z_]{2,40}$/.test(code) ? ` (${code})` : "";
}

interface Requester {
  token: string;
  fetchImpl: typeof fetch;
  outer: AbortSignal | undefined;
  timeoutMs: number;
}

async function get(url: string, { token, fetchImpl, outer, timeoutMs }: Requester): Promise<Response> {
  const timeout = AbortSignal.timeout(timeoutMs);
  const signal = outer ? AbortSignal.any([outer, timeout]) : timeout;
  try {
    return await fetchImpl(url, {
      method: "GET",
      headers: { authorization: `Bearer ${token}`, accept: "application/json" },
      redirect: "manual",
      signal,
    });
  } catch (error) {
    if (outer?.aborted) throw error;
    if (timeout.aborted) {
      throw new Unreachable(`did not answer within ${timeoutMs / 1000} s`);
    }
    throw new Unreachable(`connection failed${causeCode(error)}`);
  }
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

function errorCode(body: unknown): string {
  const code = (body as { error?: { code?: unknown } } | undefined)?.error?.code;
  return typeof code === "string" && /^[a-z_]{1,64}$/.test(code) ? ` (${code})` : "";
}

const isRedirect = (response: Response) =>
  response.type === "opaqueredirect" || (response.status >= 300 && response.status < 400);

function modeReadiness(
  body: unknown,
  mode: ExecutionMode,
): { ready: boolean; reason: string | null } | "absent" | undefined {
  const modes = (body as { modes?: unknown } | undefined)?.modes;
  if (typeof modes !== "object" || modes === null) return undefined;
  const entry = (modes as Record<string, unknown>)[mode];
  if (entry === undefined) return "absent";
  const ready = (entry as { ready?: unknown }).ready;
  const reason = (entry as { reason?: unknown }).reason;
  if (typeof ready !== "boolean") return undefined;
  return { ready, reason: typeof reason === "string" ? reason : null };
}

/** A discovery that publishes nothing. */
const nothing = (kind: DiagnosisKind, message: string): Discovery => ({ diagnosis: { kind, message }, models: [] });

/** Ask the gateway what it serves and whether the configured mode is ready. */
export async function discoverRelayMind(params: DiscoverParams): Promise<Discovery> {
  const { config, token } = params;
  const where = `RelayMind gateway at ${config.baseUrl}`;
  if (!token) {
    return nothing(
      "no_credential",
      `No RelayMind application token is stored for the relaymind provider. ${TOKEN_HANDOVER}`,
    );
  }
  const requester: Requester = {
    token,
    fetchImpl: params.fetch ?? fetch,
    outer: params.signal,
    timeoutMs: params.timeoutMs ?? DISCOVERY_TIMEOUT_MS,
  };
  let listing: Response;
  try {
    listing = await get(`${config.baseUrl}/models`, requester);
  } catch (error) {
    if (!(error instanceof Unreachable)) throw error;
    return nothing(
      "gateway_unreachable",
      `RelayMind gateway unreachable at ${config.baseUrl}: ${error.message}. Start it with ` +
        "`relaymind serve --config <file>` and check the plugin's baseUrl.",
    );
  }
  if (isRedirect(listing)) {
    return nothing(
      "gateway_error",
      `${where} answered GET /v1/models with a redirect; the plugin never follows a redirect with the credential.`,
    );
  }
  if (listing.status === 401) {
    return nothing("auth_failed", RECONNECT);
  }
  const body = await readJson(listing);
  if (!listing.ok) {
    return nothing("gateway_error", `${where} answered GET /v1/models with HTTP ${listing.status}${errorCode(body)}.`);
  }
  const data = (body as { data?: unknown } | undefined)?.data;
  if (!Array.isArray(data)) {
    return nothing("gateway_error", `${where} did not return a model list.`);
  }
  if (!data.some((model) => (model as { id?: unknown })?.id === MODEL_ID)) {
    return nothing(
      "alias_absent",
      `${where} does not serve ${MODEL_ID}: client-owned execution is not configured on the ` +
        "gateway, so no model is published. `relaymind serve` serves it; check that baseUrl names " +
        "that gateway.",
    );
  }
  const diagnosis = await readiness(config, requester);
  // A token /readyz rejects is stale (the gateway restarted in between): publish nothing.
  return { diagnosis, models: diagnosis.kind === "auth_failed" ? [] : [catalogModel(config)] };
}

async function readiness(config: RelayMindPluginConfig, requester: Requester): Promise<CatalogDiagnosis> {
  const mode = config.executionMode;
  const readyzUrl = `${new URL(config.baseUrl).origin}/readyz`;
  const unknown = (why: string): CatalogDiagnosis => ({
    kind: "readiness_unknown",
    message:
      `RelayMind serves ${MODEL_ID}, but the readiness of mode ${mode} is unknown (${why}); ` +
      `a request reports the gateway's own error. ${describeLimits(config)}.`,
  });
  let response: Response;
  try {
    response = await get(readyzUrl, requester);
  } catch (error) {
    // get() rethrows only the host's abort; everything else is Unreachable.
    if (!(error instanceof Unreachable)) throw error;
    return unknown(`GET /readyz: ${error.message}`);
  }
  if (response.status === 401) return { kind: "auth_failed", message: RECONNECT };
  if (isRedirect(response) || (response.status !== 200 && response.status !== 503)) {
    return unknown(`GET /readyz answered HTTP ${response.status}`);
  }
  // 200 and 503 carry the same body; only the configured mode is read (section 7.4).
  const state = modeReadiness(await readJson(response), mode);
  if (state === undefined) return unknown(`GET /readyz answered HTTP ${response.status} without mode readiness`);
  if (state === "absent") {
    return {
      kind: "mode_forbidden",
      message:
        `The gateway's policy (policy.allowed_modes) does not permit ${mode}; requests in this mode ` +
        "fail with 403 execution_mode_forbidden. Change the plugin's executionMode or the gateway's policy.",
    };
  }
  if (!state.ready) {
    return {
      kind: "mode_not_ready",
      message:
        `RelayMind mode ${mode} is not ready: ${state.reason ?? "no reason given"}. ` +
        `Other modes are reported separately by the gateway. ${describeLimits(config)}.`,
    };
  }
  return {
    kind: "ready",
    message: `RelayMind gateway at ${config.baseUrl} serves ${MODEL_ID}; mode ${mode} is ready; ${describeLimits(config)}.`,
  };
}

type OutcomeStatus = "ready" | "auth-rejected" | "unavailable";

/** The host's per-provider catalog outcome status for a diagnosis. */
const OUTCOME: Record<Exclude<DiagnosisKind, "no_credential">, OutcomeStatus> = {
  ready: "ready",
  gateway_unreachable: "unavailable",
  gateway_error: "unavailable",
  auth_failed: "auth-rejected",
  // The listing succeeded: an absent alias is a successful empty result, and the
  // readiness of one mode is not a catalog acquisition failure.
  alias_absent: "ready",
  mode_not_ready: "ready",
  mode_forbidden: "ready",
  readiness_unknown: "ready",
};

export interface RelayMindCatalogResult {
  provider: {
    baseUrl: string;
    api: "openai-completions";
    apiKey?: string;
    models: RelayMindCatalogModel[];
  };
  outcomes: { provider: typeof PROVIDER_ID; status: OutcomeStatus }[];
}

/**
 * The host's catalog result: the provider with the host's own credential value
 * (the host resolved it; the plugin passes it back unchanged, as the bundled
 * self-hosted providers do), and one outcome. Nothing without a credential.
 */
export function catalogResult(
  config: RelayMindPluginConfig,
  discovery: Discovery,
  apiKey: string | undefined,
): RelayMindCatalogResult | null {
  if (discovery.diagnosis.kind === "no_credential") return null;
  return {
    provider: {
      baseUrl: config.baseUrl,
      api: "openai-completions",
      ...(apiKey !== undefined ? { apiKey } : {}),
      models: discovery.models,
    },
    outcomes: [{ provider: PROVIDER_ID, status: OUTCOME[discovery.diagnosis.kind] }],
  };
}
