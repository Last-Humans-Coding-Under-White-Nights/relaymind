// The pinned host's own failover classification (RM16, RM20, RM33; spec section
// 7.5, #138).
//
// Read from node_modules/openclaw/dist of openclaw@2026.10.7 (the package's
// peer pin), so a test sees the class the real host derives from a failed
// message with this plugin as its provider, and the classes the host retries
// on the same model. The chunk names are those of the pinned build: a new pin
// renames them, and these helpers then fail loudly instead of guessing.
import { readFileSync } from "node:fs";

const DIST = new URL("../node_modules/openclaw/dist/", import.meta.url);
/** classifyFailoverSignal (export n): the host's classifier with a provider plugin. */
const CLASSIFY = "classify-CY8UzOuJ.mjs";
/** buildAssistantFailoverSignal (export t): the signal the host builds from a failed message. */
const SIGNAL = "assistant-message-failures-CF-JUhOf.mjs";
/** The embedded runner whose maybeRetryTransient decides a same-model retry. */
const RUNNER = "embedded-agent-DAqZgJpR.mjs";

interface ProviderHooks {
  id: string;
  classifyFailoverReason?: (ctx: never) => unknown;
  matchesContextOverflowError?: (ctx: never) => unknown;
}

/**
 * The reasons the pinned host's maybeRetryTransient retries on the same model:
 * the condition just before its `recordDecision("rejected", "non_transient")`.
 */
export function transientRetryReasons(): string[] {
  const source = readFileSync(new URL(RUNNER, DIST), "utf8");
  const match =
    /if \(((?:retry\.reason !== "[a-z_]+"(?: && )?)+)\) \{\s*recordDecision\("rejected", "non_transient"\)/.exec(
      source,
    );
  if (!match) throw new Error(`maybeRetryTransient's non_transient check not found in ${RUNNER}`);
  return [...match[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]).sort();
}

/** The class the pinned host gives a failed assistant message from this provider. */
export async function hostFailoverReason(
  message: unknown,
  provider: ProviderHooks,
): Promise<string | null> {
  const { n: classifyFailoverSignal } = await import(new URL(CLASSIFY, DIST).href);
  const { t: buildAssistantFailoverSignal } = await import(new URL(SIGNAL, DIST).href);
  const classification = classifyFailoverSignal(buildAssistantFailoverSignal(message), {
    providerPlugin: provider,
  });
  if (!classification) return null;
  return classification.kind === "reason" ? classification.reason : classification.kind;
}
