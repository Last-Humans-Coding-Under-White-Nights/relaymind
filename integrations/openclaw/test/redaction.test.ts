// No credential in diagnostic output (RM12, section 7.5).
import { describe, expect, it } from "vitest";

import { redact, redactingLogger } from "../src/redaction.ts";

const TOKEN = "rm-app-token-4f9c2a7e1b3d5c6a8e0f2b4d6c8a0e1f";

describe("redact", () => {
  it("replaces the token value wherever it appears", () => {
    expect(redact(`failed with ${TOKEN} twice ${TOKEN}`, [TOKEN])).toBe(
      "failed with <redacted> twice <redacted>",
    );
  });

  it("replaces any bearer credential, known or not", () => {
    expect(redact("Authorization: Bearer abc.def-123", [])).toBe("Authorization: Bearer <redacted>");
  });

  it("leaves text without a credential unchanged", () => {
    expect(redact("gateway unreachable at http://127.0.0.1:8080/v1", [undefined])).toBe(
      "gateway unreachable at http://127.0.0.1:8080/v1",
    );
  });
});

describe("redactingLogger", () => {
  it("scrubs every level before the host logger sees the message", () => {
    const seen: string[] = [];
    const host = {
      debug: (m: string) => seen.push(`debug ${m}`),
      info: (m: string) => seen.push(`info ${m}`),
      warn: (m: string) => seen.push(`warn ${m}`),
      error: (m: string) => seen.push(`error ${m}`),
    };
    const logger = redactingLogger(host, () => [TOKEN]);
    logger.debug?.(`a ${TOKEN}`);
    logger.info(`b Bearer ${TOKEN}`);
    logger.warn(`c ${TOKEN}`);
    logger.error(`d ${TOKEN}`);
    expect(seen.join("\n")).not.toContain(TOKEN);
    expect(seen).toEqual([
      "debug a <redacted>",
      "info b Bearer <redacted>",
      "warn c <redacted>",
      "error d <redacted>",
    ]);
  });

  it("works with a host logger that has no debug level", () => {
    const seen: string[] = [];
    const logger = redactingLogger(
      { info: (m) => seen.push(m), warn: (m) => seen.push(m), error: (m) => seen.push(m) },
      () => [TOKEN],
    );
    logger.debug?.(`x ${TOKEN}`);
    expect(seen).toEqual([]);
  });
});
