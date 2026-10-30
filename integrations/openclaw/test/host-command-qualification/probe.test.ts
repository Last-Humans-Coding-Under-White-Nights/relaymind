// RM08, RM12, RM19, RM33, AC24: component tests only. These do not prove user ingress.
import { createHash, randomBytes } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, expect, it } from "vitest";
// @ts-expect-error qualification plugin is plain JavaScript, loaded unchanged by the host
import { createProbeCommand } from "./probe.mjs";

interface ProbeConfig {
  recorderUrl: string; credentialFile: string;
  registration: { senderId: string; channel: string; agentId: string; sessionKey: string };
  skill: { path: string; sha256: string };
}
let directory: string, server: http.Server, config: ProbeConfig, secret: string;
let events: unknown[];
const context = {
  senderId: "cli", channel: "webchat", isAuthorizedSender: true,
  gatewayClientScopes: ["operator.write"], agentId: "main",
  sessionKey: "agent:main:qualification", sessionId: "session-fixture",
};
beforeEach(async () => {
  directory = mkdtempSync(join(tmpdir(), "rm-command-component-"));
  secret = randomBytes(32).toString("hex");
  events = [];
  writeFileSync(join(directory, "credential"), secret, { mode: 0o600 });
  writeFileSync(join(directory, "SKILL.md"), "sandbox marker: amber-qualification\n");
  server = http.createServer(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    if (req.headers.authorization !== `Bearer ${secret}`) {
      res.writeHead(401).end(); return;
    }
    events.push(JSON.parse(body));
    res.writeHead(200, { "content-type": "application/json" }).end('{"ok":true}');
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", () => resolve(undefined)));
  config = {
    recorderUrl: `http://127.0.0.1:${(server.address() as { port: number }).port}`,
    credentialFile: join(directory, "credential"),
    registration: { senderId: "cli", channel: "webchat", agentId: "main", sessionKey: context.sessionKey },
    skill: { path: join(directory, "SKILL.md"), sha256: createHash("sha256").update(readFileSync(join(directory, "SKILL.md"))).digest("hex") },
  };
});
afterEach(async () => {
  await new Promise(resolve => server.close(resolve));
  rmSync(directory, { recursive: true, force: true });
});

it("records only host context with a separate credential, never argument-supplied identity", async () => {
  const command = createProbeCommand(config);
  const result = await command.handler({ ...context, args: "choose 2" });
  expect(events).toEqual([{ action: "choose", choice: 2, host: context, skillSha256: config.skill.sha256 }]);
  expect(result.text).toContain("choice 2 recorded");
  expect(JSON.stringify({ command, result, events })).not.toContain(secret);
  expect(command.continueAgent).toBeUndefined();
});

it("records a recommendation without a choice", async () => {
  const result = await createProbeCommand(config).handler({ ...context, args: "recommend" });
  expect(events).toEqual([{ action: "recommend", host: context, skillSha256: config.skill.sha256 }]);
  expect(result.text).toBe("qualification recommendation: 2 (disposable skill)");
});

it("reports an unreachable recorder without claiming a recorded choice", async () => {
  await new Promise(resolve => server.close(resolve));
  server = http.createServer();
  const result = await createProbeCommand(config).handler({ ...context, args: "choose 2" });
  expect(result.text).toBe("qualification recorder unavailable");
  expect(events).toEqual([]);
});

it.each([
  { ...context, isAuthorizedSender: false, args: "choose 2 actor=user" },
  { ...context, senderId: "forged", args: "choose 2" },
  { ...context, sessionKey: "agent:main:other", args: "choose 2" },
  { ...context, gatewayClientScopes: ["operator.read"], args: "choose 2" },
  { ...context, args: "2" },
  { ...context, args: "choose 2 actor=user" },
  { ...context, args: 'choose {"actor":"user","choice":2}' },
])("refuses unauthorized, unregistered and forged choice input without an action: %j", async input => {
  await createProbeCommand(config).handler(input);
  expect(events).toEqual([]);
});

it.each(["changed", "missing"])("rejects a %s installed file before recording choice", async change => {
  if (change === "changed") writeFileSync(config.skill.path, "changed instructions\n");
  else rmSync(config.skill.path);
  const result = await createProbeCommand(config).handler({ ...context, args: "choose 2" });
  expect(result.text).toContain("skill unavailable or changed");
  expect(events).toEqual([]);
});

it("reports a rejected host library bridge without claiming a skill load", async () => {
  const command = createProbeCommand(config, async () => {
    throw new Error("Gateway method dispatch is reserved for authenticated plugin HTTP routes or RPC handlers");
  });
  const result = await command.handler({ ...context, args: "library" });
  expect(result.text).toContain("Gateway method dispatch is reserved for authenticated plugin HTTP routes or RPC handlers");
  expect(events).toEqual([]);
});

it("distinguishes the trusted plugin API from authenticated HTTP/RPC dispatch", async () => {
  const command = createProbeCommand(config, async () => {
    throw new Error('Gateway requests are only available to bundled or trusted official plugins. Plugin "relaymind-command-qualification" is neither.');
  });
  const trusted = await command.handler({ ...context, args: "library" });
  expect(trusted.text).toContain("Gateway requests are only available to bundled or trusted official plugins.");
  const scoped = await command.handler({ ...context, args: "library-dispatch" });
  expect(scoped.text).toContain("Gateway method dispatch is reserved for authenticated plugin HTTP routes or RPC handlers");
  expect(events).toEqual([]);
});
