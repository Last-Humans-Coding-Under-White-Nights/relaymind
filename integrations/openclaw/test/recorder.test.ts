// The recorder reads the SSE data lines of a captured response body (RM33, #135).
import { describe, expect, it } from "vitest";

// @ts-expect-error recorder.mjs is plain JavaScript without declarations (#135)
import { sseData } from "./host-transport/recorder.mjs";

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
