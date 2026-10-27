// No credential in diagnostic output (RM12, section 7.5).
//
// Every message the plugin writes goes through the redacting logger: the known
// token values and any bearer credential are replaced before the host logger
// sees the text.

export const REDACTED = "<redacted>";

export interface Logger {
  debug?: (message: string) => void;
  info: (message: string) => void;
  warn: (message: string) => void;
  error: (message: string) => void;
}

/** text with every secret and every bearer credential replaced by <redacted>. */
export function redact(text: string, secrets: readonly (string | undefined)[]): string {
  let out = text;
  for (const secret of secrets) {
    if (secret) out = out.replaceAll(secret, REDACTED);
  }
  return out.replace(/\b(Bearer)\s+(?!<redacted>)\S+/gi, `$1 ${REDACTED}`);
}

/** A logger that redacts before writing; secrets() is read on every message. */
export function redactingLogger(
  logger: Logger,
  secrets: () => readonly (string | undefined)[],
): Logger {
  const scrub = (message: string) => redact(message, secrets());
  const out: Logger = {
    info: (message) => logger.info(scrub(message)),
    warn: (message) => logger.warn(scrub(message)),
    error: (message) => logger.error(scrub(message)),
  };
  if (logger.debug) out.debug = (message) => logger.debug?.(scrub(message));
  return out;
}
