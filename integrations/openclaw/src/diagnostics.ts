// What the plugin states about itself (RM33, section 7.5).
//
// The statement claims only what the buffered client-owned backend does: text
// input, at most one host-owned function call per response, the answer delivered
// whole. It never claims vision, audio, Responses, parallel tool calls, live token
// forwarding, managed skill selection, or managed device-aware placement, and it
// reports usage as unknown because the gateway's chat responses carry none.

export const CAPABILITIES = Object.freeze({
  input: Object.freeze(["text"]) as readonly ["text"],
  toolCallsPerResponse: 1,
  toolExecution: "host",
  answerDelivery: "buffered",
  liveTokenStreaming: false,
  parallelToolCalls: false,
  vision: false,
  audio: false,
  responsesApi: false,
  managedSkillSelection: false,
  managedPlacement: false,
  usage: "unknown",
} as const);

export function describeCapabilities(): string {
  return (
    "RelayMind provider (relaymind-client-tools): text input; at most one function call " +
    "per response, and OpenClaw executes its own tools and applies its own approvals; " +
    "the answer is buffered by the gateway and delivered whole, so there is no live token " +
    "streaming; no parallel tool calls; no vision, audio, or Responses API; no managed skill " +
    "selection, prefetch, or skill stage; no managed device-aware placement; usage is unknown " +
    "(the gateway's run trace is authoritative for model calls)."
  );
}

/** How the operator hands the gateway-generated token to the host (section 7.5). */
export const TOKEN_HANDOVER =
  "Start the gateway with `relaymind serve --config <file>`: it prints a new application " +
  "token each time it starts. Store that token for OpenClaw with " +
  "`openclaw models auth paste-api-key --provider relaymind` (paste it at the prompt or pipe " +
  "it on standard input, never as a command-line argument), or set RELAYMIND_APP_TOKEN in the " +
  "host's environment.";

/** The reconnect path after a gateway restart: a stale token is 401 (section 7.5). */
export const RECONNECT =
  "RelayMind rejected the stored application token (HTTP 401 unauthenticated). The gateway " +
  "generates a new application token every time it starts, so a restart makes the stored one " +
  "stale. Copy the token the running `relaymind serve` printed and store it with " +
  "`openclaw models auth paste-api-key --provider relaymind`.";
