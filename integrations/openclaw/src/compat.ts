// The host compatibility switches and model entry of the RelayMind provider
// (RM33, spec section 7.5).
//
// The pinned OpenClaw host builds each Chat Completions request from defaults it
// derives from the base URL and from the per-model `compat` object a catalog
// entry declares. For a loopback URL the defaults send stream_options on every
// streamed request, which section 7.1 rejects. These switches keep the host
// inside the section 7.1 subset. They are a host-side adaptation: the gateway
// contract is unchanged and the gateway keeps rejecting what 7.1 rejects.
//
// #135 confirmed each switch on the wire against the real gateway; see
// test/fixtures/v2026.10.7/ and docs/design/2026-10-27-openclaw-integration.md.

/** The OpenClaw release this package is built and qualified against. */
export const PINNED_OPENCLAW = "2026.10.7";

/** The host provider ID and the gateway's client-owned alias (section 7.2). */
export const PROVIDER_ID = "relaymind";
export const MODEL_ID = "relaymind-client-tools";

/** The per-model switches of the pinned host's ModelCompatSchema that the plugin sets. */
export interface RelayMindModelCompat {
  supportsUsageInStreaming: false;
  supportsStrictMode: false;
  supportsReasoningEffort: false;
  supportsDeveloperRole: false;
  maxTokensField: "max_completion_tokens";
  supportsJsonSchemaResponseFormat: false;
  supportsPromptCacheKey: false;
  supportsStore: false;
  strictMessageKeys: false;
  requiresToolResultName: false;
  requiresAssistantAfterToolResult: false;
  requiresStringContent: false;
  supportsTools: true;
}

/** The declared switches; a new object on every call so no caller can change them. */
export function relaymindModelCompat(): RelayMindModelCompat {
  return {
    // Section 7.1 rejects stream_options; the host sends it for a loopback URL.
    supportsUsageInStreaming: false,
    // No strict flag on a tool; 7.1 rejects strict true.
    supportsStrictMode: false,
    // No reasoning_effort field.
    supportsReasoningEffort: false,
    // A mid-conversation runtime context message is sent as user, not developer;
    // 7.1 accepts developer messages only before the first user message.
    supportsDeveloperRole: false,
    maxTokensField: "max_completion_tokens",
    // Structured output is not part of 7.1.
    supportsJsonSchemaResponseFormat: false,
    // prompt_cache_key is not part of 7.1.
    supportsPromptCacheKey: false,
    // store false is accepted by 7.1; the plugin does not need to send it.
    supportsStore: false,
    // True would strip tool_calls and tool_call_id, breaking a tool round trip.
    strictMessageKeys: false,
    // 7.1 rejects a name field on a tool message.
    requiresToolResultName: false,
    // 7.1 rejects an assistant message the host invents after tool results.
    requiresAssistantAfterToolResult: false,
    // Content stays a string or text parts; both are accepted.
    requiresStringContent: false,
    supportsTools: true,
  };
}

/** The limits the operator copies from the gateway's configuration (section 7.5). */
export interface RelayMindModelSettings {
  baseUrl: string;
  contextWindow?: number;
  maxTokens?: number;
}

export interface RelayMindModelEntry {
  id: typeof MODEL_ID;
  name: string;
  baseUrl: string;
  reasoning: false;
  input: ["text"];
  compat: RelayMindModelCompat;
  contextWindow?: number;
  maxTokens?: number;
}

/**
 * The model entry of the client-owned alias: text only, reasoning off, the
 * declared switches, and only the limits the operator set; a limit that is not
 * set is omitted, never estimated (section 7.5).
 */
export function relaymindModelEntry(settings: RelayMindModelSettings): RelayMindModelEntry {
  const entry: RelayMindModelEntry = {
    id: MODEL_ID,
    name: "RelayMind (client-owned tools)",
    baseUrl: settings.baseUrl,
    reasoning: false,
    input: ["text"],
    compat: relaymindModelCompat(),
  };
  if (settings.contextWindow !== undefined) entry.contextWindow = settings.contextWindow;
  if (settings.maxTokens !== undefined) entry.maxTokens = settings.maxTokens;
  return entry;
}
