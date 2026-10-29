// The host capture sanitizer replaces the home directory before a fixture is written (RM12, #135).
import { describe, expect, it } from "vitest";

// @ts-expect-error capture-sanitize.mjs is plain JavaScript without declarations (#135)
import { sanitizeCapture } from "../scripts/capture-sanitize.mjs";

describe("sanitizeCapture", () => {
  it("replaces a POSIX home in the fixture text", () => {
    const text = JSON.stringify({ prompt: "workspace /Users/x/project" }, null, 2);
    const out = sanitizeCapture(text, "/Users/x");
    expect(out).not.toContain("/Users/x");
    expect(JSON.parse(out).prompt).toBe("workspace <home>/project");
  });

  it("replaces a Windows home in its JSON-escaped form", () => {
    const home = "C:\\Users\\x";
    const text = JSON.stringify({ prompt: `workspace ${home}\\project` }, null, 2);
    expect(text).toContain("C:\\\\Users\\\\x");
    const out = sanitizeCapture(text, home);
    expect(out).not.toContain("C:\\\\Users\\\\x");
    expect(out).not.toContain(home);
    expect(JSON.parse(out).prompt).toBe("workspace <home>\\project");
  });

  it("replaces a Windows home that appears raw", () => {
    const home = "C:\\Users\\x";
    expect(sanitizeCapture(`log ${home}\\a`, home)).toBe("log <home>\\a");
  });

  it("leaves the text unchanged when there is no home", () => {
    expect(sanitizeCapture('{"a": "b"}', "")).toBe('{"a": "b"}');
  });
});
