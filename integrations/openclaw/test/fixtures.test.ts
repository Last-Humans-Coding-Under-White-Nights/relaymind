// The frozen wire fixtures of the pinned host transport (RM33, AC24, #135).
//
// Captured by `scripts/openclaw_qualification.py fixtures` from the pinned
// @openclaw/ai transport against the real gateway over model doubles. Every
// request the plugin's switches produce stays inside the section 7.1 subset,
// and no fixture holds a credential.
import { readdirSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { PINNED_OPENCLAW } from "../src/compat.ts";

const ROOT = new URL(`./fixtures/v${PINNED_OPENCLAW}/`, import.meta.url);
const TRANSPORT = new URL("./transport/", ROOT);

// Section 7.1: the request fields, and the API defaults it also accepts.
const FIELDS = new Set([
  "model",
  "messages",
  "stream",
  "n",
  "max_completion_tokens",
  "max_tokens",
  "temperature",
  "top_p",
  "tools",
  "tool_choice",
  "parallel_tool_calls",
  "response_format",
  "user",
]);
const DEFAULTS: Record<string, unknown> = {
  store: false,
  presence_penalty: 0,
  frequency_penalty: 0,
  logprobs: false,
};
const MESSAGE_FIELDS: Record<string, Set<string>> = {
  system: new Set(["role", "content"]),
  developer: new Set(["role", "content"]),
  user: new Set(["role", "content"]),
  assistant: new Set(["role", "content", "tool_calls"]),
  tool: new Set(["role", "content", "tool_call_id"]),
};
// Requests that deliberately carry a field 7.1 rejects, and the field.
const DELIBERATE: Record<string, string[]> = {
  "host-default-loopback": ["stream_options"],
  "host-default-reasoning": ["stream_options", "reasoning_effort"],
};

interface Exchange {
  request: { method: string; path: string; headers: Record<string, string>; body: any };
  response: { status: number | null; headers: Record<string, string>; body: any } | null;
  client_closed_early: boolean;
}

interface Fixture {
  fixture_version: number;
  scenario: string;
  covers: string;
  compat: "plugin" | "plugin-modified" | "host-default";
  pins: Record<string, string>;
  exchanges: Exchange[];
  results: { stop_reason: string; text: string; error_message: string | null }[];
  host_tool_executions: unknown[];
  gateway_runs: { run: string; state: string }[];
}

const fixtures: Fixture[] = readdirSync(TRANSPORT)
  .filter((name) => name.endsWith(".json"))
  .sort()
  .map((name) => JSON.parse(readFileSync(new URL(name, TRANSPORT), "utf8")));

function outside71(body: Record<string, unknown>): string[] {
  return Object.entries(body)
    .filter(([key, value]) => value !== null && !FIELDS.has(key))
    .filter(([key, value]) => !(key in DEFAULTS && DEFAULTS[key] === value))
    .map(([key]) => key)
    .sort();
}

describe("the transport fixtures (#135)", () => {
  it("exist and carry the pinned identity", () => {
    expect(fixtures.length).toBeGreaterThanOrEqual(10);
    const versions = JSON.parse(readFileSync(new URL("VERSIONS.json", ROOT), "utf8"));
    expect(versions.openclaw).toBe(PINNED_OPENCLAW);
    expect(versions["@openclaw/ai"]).toBe(PINNED_OPENCLAW);
    for (const fixture of fixtures) {
      expect(fixture.fixture_version).toBe(1);
      expect(fixture.pins.openclaw).toBe(PINNED_OPENCLAW);
      expect(fixture.pins["@openclaw/ai"]).toBe(PINNED_OPENCLAW);
      expect(fixture.covers.length).toBeGreaterThan(0);
    }
  });

  it("hold no credential", () => {
    for (const fixture of fixtures) {
      for (const exchange of fixture.exchanges) {
        expect(exchange.request.headers.authorization).toBe("<redacted>");
      }
      expect(JSON.stringify(fixture)).not.toMatch(/Bearer [A-Za-z0-9_-]{8,}/);
    }
  });

  it("send only section 7.1 fields with the declared switches", () => {
    for (const fixture of fixtures) {
      for (const exchange of fixture.exchanges) {
        const body = exchange.request.body;
        const expected = DELIBERATE[fixture.scenario] ?? [];
        const extra = outside71(body);
        expect(extra, fixture.scenario).toEqual([...expected].sort());
        if (fixture.compat === "plugin") {
          for (const message of body.messages) {
            const allowed = MESSAGE_FIELDS[message.role];
            expect(allowed, `${fixture.scenario} role ${message.role}`).toBeDefined();
            for (const key of Object.keys(message)) {
              expect(allowed.has(key), `${fixture.scenario} ${message.role}.${key}`).toBe(true);
            }
          }
          for (const tool of body.tools ?? []) {
            expect("strict" in tool.function, fixture.scenario).toBe(false);
          }
        }
      }
    }
  });

  it("never send a developer message after the first user message with the declared switches", () => {
    for (const fixture of fixtures.filter((f) => f.compat !== "plugin-modified")) {
      for (const exchange of fixture.exchanges) {
        const roles: string[] = exchange.request.body.messages.map((m: any) => m.role);
        const firstUser = roles.indexOf("user");
        expect(roles.slice(firstUser).includes("developer"), fixture.scenario).toBe(false);
      }
    }
  });

  it("turn every gateway error into a host failure, never an empty success", () => {
    for (const fixture of fixtures) {
      fixture.exchanges.forEach((exchange, index) => {
        const status = exchange.response?.status;
        if (status !== undefined && status !== null && status >= 400) {
          expect(fixture.results[index].stop_reason, fixture.scenario).toBe("error");
          expect(fixture.results[index].text, fixture.scenario).toBe("");
        }
      });
    }
  });

  it("show why the developer switch stays off", () => {
    const on = fixtures.find((f) => f.scenario === "developer-switch-on");
    expect(on).toBeDefined();
    const roles = on!.exchanges[0].request.body.messages.map((m: any) => m.role);
    expect(roles.slice(roles.indexOf("user"))).toContain("developer");
    expect(on!.exchanges[0].response?.status).toBe(400);
  });

  it("let only the host execute a tool, once per returned call", () => {
    const roundTrip = fixtures.find((f) => f.scenario === "tool-round-trip");
    expect(roundTrip).toBeDefined();
    expect(roundTrip!.host_tool_executions).toHaveLength(1);
    expect(roundTrip!.results.map((r) => r.stop_reason)).toEqual(["toolUse", "stop"]);
    const [, second] = roundTrip!.exchanges;
    const tool = second.request.body.messages.at(-1);
    expect(tool).toEqual({
      role: "tool",
      content: "echo: lantern",
      tool_call_id: "call_echo_1",
    });
  });

  it("record an abort as a closed request and a cancelled run", () => {
    const abort = fixtures.find((f) => f.scenario === "abort-buffered");
    expect(abort).toBeDefined();
    expect(abort!.exchanges[0].client_closed_early).toBe(true);
    expect(abort!.results[0].stop_reason).toBe("aborted");
    expect(abort!.gateway_runs).toEqual([{ run: "<run_1>", state: "cancelled" }]);
  });
});
