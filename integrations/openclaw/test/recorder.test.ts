// The recorder reads the SSE data lines of a captured response body (RM33, #135).
import http from "node:http";
import type { AddressInfo } from "node:net";
import { describe, expect, it } from "vitest";

// @ts-expect-error recorder.mjs is plain JavaScript without declarations (#135)
import { sseData, startRecorder } from "./host-transport/recorder.mjs";

describe("sseData", () => {
  const events = [{ choices: [{ delta: { content: "Hi" } }] }, "[DONE]"];

  it("reads LF-framed events and keep-alive comments", () => {
    const body = `: keep-alive\n\ndata: ${JSON.stringify(events[0])}\n\ndata: [DONE]\n\n`;
    expect(sseData(body)).toEqual([{ comment: "keep-alive" }, ...events]);
  });

  it("reads CRLF-framed events the same way", () => {
    const body = `: keep-alive\r\n\r\ndata: ${JSON.stringify(events[0])}\r\n\r\ndata: [DONE]\r\n\r\n`;
    expect(sseData(body)).toEqual([{ comment: "keep-alive" }, ...events]);
  });
});

describe("startRecorder", () => {
  async function closedOrigin() {
    const server = http.createServer();
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const { port } = server.address() as AddressInfo;
    await new Promise((resolve) => server.close(resolve));
    return `http://127.0.0.1:${port}`;
  }

  it("answers 502 when the upstream gateway cannot be reached", async () => {
    const recorder = await startRecorder({ upstream: await closedOrigin() });
    try {
      const response = await fetch(`${recorder.origin}/v1/chat/completions`, {
        method: "POST",
        body: "{}",
        signal: AbortSignal.timeout(2000),
      });
      expect(response.status).toBe(502);
      expect(recorder.exchanges[0].response.status).toBeNull();
    } finally {
      await recorder.close();
    }
  });

  it("keeps serving after a client aborts in the middle of its request body", async () => {
    const recorder = await startRecorder({ replies: [{ body: { ok: true } }] });
    const origin = new URL(recorder.origin);
    try {
      await new Promise<void>((resolve) => {
        const req = http.request({
          host: origin.hostname,
          port: origin.port,
          method: "POST",
          path: "/v1/chat/completions",
          headers: { "content-length": "1000" },
        });
        req.on("error", () => resolve());
        req.write("{", () => setTimeout(() => req.destroy(), 50));
      });
      await new Promise((resolve) => setTimeout(resolve, 100));
      const response = await fetch(`${recorder.origin}/v1/models`);
      expect(await response.json()).toEqual({ ok: true });
    } finally {
      await recorder.close();
    }
  });
});
