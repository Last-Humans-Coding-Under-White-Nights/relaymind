// RM16/RM37/RM39; AC28/AC30 (spec 0.65 section 7.6.1): the loaded-use fence and
// immutable pins. Controlled host-shaped doubles only; never qualification evidence.
import { createHash } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, it } from "vitest";
import { createFileUseLedger } from "../src/recommendation-native-ledger.js";
import { fixture } from "./recommendation-native-fixture.js";
import {
  createSkillLoadedUseFence,
  NativeUseRefusal,
} from "../src/recommendation-native-use.js";

const content = "# Pinned fixture\r\nUse the exact selected bytes. \n";
const hash = createHash("sha256").update(content, "utf8").digest("hex");
const skillId = "4774d146-17bd-4ec9-8507-f528ccea51fa";
const R0 = "b".repeat(64);
const R1 = "c".repeat(64);
const QUALIFIED = { adapterRevision: "a".repeat(64) } as any;

function ledgerPath(): string {
  return join(mkdtempSync(join(tmpdir(), "relaymind-ledger-")), "use.jsonl");
}
function setup(qualified = true, ledger: string | null = ledgerPath()) {
  const f = fixture();
  const view: any = {
    recommendation_id: skillId,
    task_id: "task",
    state: "chosen",
    choice_expires_at: "2030-01-01T00:00:00Z",
    selected_action: {
      recommendation_id: skillId,
      task_id: "task",
      scope: { user_id: f.binding.userId, workspace_id: f.binding.workspaceId },
      choice: { kind: "asset", asset_id: "skill:fixture", content_hash: hash },
    },
    displayed_candidates: [
      {
        asset: {
          asset_id: "skill:fixture",
          kind: "skill",
          content_hash: hash,
          source_version: null,
          stale: false,
        },
      },
    ],
  };
  const calls: unknown[] = [];
  const gateway: string[] = [];
  let library: any = {
    entry: { skillId, revision: R0 },
    content,
    revisions: [{ revision: R0 }],
  };
  let skills = [
    {
      assetId: "skill:fixture",
      contentHash: hash,
      sourceVersion: null,
      skillId,
      revision: R0,
    },
  ];
  const deps: any = {
    bindings: [f.binding],
    controlUiOrigin: "https://fixture.test",
    resolveWorkspace: f.dependencies.resolveWorkspace,
    monotonicNow: () => 1,
    wallNow: () => Date.parse("2026-10-31T00:00:00Z"),
    qualification: (operation: string) =>
      qualified && operation === "skill_loaded_use" ? QUALIFIED : undefined,
    skills: () => skills,
    recommendationClient: async () => ({
      get: async () => {
        gateway.push("get");
        return view;
      },
    }),
    actionClient: async () => ({
      listFeedback: async () => {
        gateway.push("registration");
        return {
          schema_version: 1,
          records: [],
          feedback_revision: 0n,
          next_after_id: null,
        };
      },
    }),
    ledger: ledger === null ? undefined : createFileUseLedger(ledger),
    dispatch: async (method: string, params: unknown) => {
      calls.push([method, params]);
      return { ok: true, payload: library };
    },
  };
  const use = {
    sessionKey: "native-key",
    sessionId: "native-id",
    lifecycleRevision: "native-life",
    recommendationId: skillId,
    assetId: "skill:fixture",
    contentHash: hash,
    skillId,
    revision: R0,
  };
  return {
    ...f,
    view,
    calls,
    gateway,
    deps,
    use,
    fence: createSkillLoadedUseFence(deps),
    setLibrary: (value: any) => {
      library = value;
    },
    setSkills: (value: any) => {
      skills = value;
    },
  };
}
async function refused(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(NativeUseRefusal);
    return (error as NativeUseRefusal).code;
  }
  throw Error("expected a refusal");
}

it("stays refused with zero gateway or native calls while skill_loaded_use is unqualified", async () => {
  const f = setup(false);
  expect(await refused(f.fence.load(f.options, f.use))).toBe("unqualified");
  expect(await refused(f.fence.deliver(f.options, f.use, content))).toBe(
    "unqualified",
  );
  expect(f.calls).toHaveLength(0);
  expect(f.gateway).toHaveLength(0);
});

it("admits only the exact immutable revision bytes when a fixture registry qualifies it", async () => {
  const f = setup();
  const loaded = await f.fence.load(f.options, f.use);
  expect(loaded).toEqual({ skillId, revision: R0, content });
  expect(f.calls).toEqual([
    [
      "skills.library.read",
      { sessionKey: "native-key", skillId, revision: R0 },
    ],
  ]);
  await f.fence.deliver(f.options, f.use, content);
  // Every boundary rechecks registration and the retained choice.
  expect(f.gateway.filter((item) => item === "registration")).toHaveLength(2);
  expect(f.calls).toHaveLength(2);
});

it("keeps R0 after a library head update to R1 and never silently delivers R1", async () => {
  const f = setup();
  f.setLibrary({
    entry: { skillId, revision: R0 },
    content,
    revisions: [{ revision: R1, head: true }, { revision: R0 }],
  });
  expect(await f.fence.load(f.options, f.use)).toMatchObject({ revision: R0 });
  f.setLibrary({
    entry: { skillId, revision: R1 },
    content: "# R1 bytes\n",
  });
  expect(await refused(f.fence.deliver(f.options, f.use, content))).toBe(
    "binding_changed",
  );
});

it.each([
  [
    "altered R0 bytes",
    (f: any) =>
      f.setLibrary({
        entry: { skillId, revision: R0 },
        content: content + " ",
      }),
  ],
  [
    "a removed library entry with retained revision files",
    (f: any) =>
      f.setLibrary({
        entry: { skillId, revision: R0, removed: true },
        content,
      }),
  ],
  [
    "a missing native revision",
    (f: any) => {
      f.deps.dispatch = async () => ({ ok: false });
    },
  ],
  ["a removed operator association", (f: any) => f.setSkills([])],
  [
    "a remapped operator association",
    (f: any) =>
      f.setSkills([
        {
          assetId: "skill:fixture",
          contentHash: hash,
          sourceVersion: null,
          skillId,
          revision: R1,
        },
      ]),
  ],
  [
    "a stale offered asset",
    (f: any) => {
      f.view.displayed_candidates[0].asset.stale = true;
    },
  ],
  [
    "a changed source version",
    (f: any) => {
      f.view.displayed_candidates[0].asset.source_version = "v2";
    },
  ],
  [
    "an expired choice",
    (f: any) => {
      f.view.choice_expires_at = "2020-01-01T00:00:00Z";
    },
  ],
  [
    "an erased choice",
    (f: any) => {
      f.view.state = "expired";
      f.view.selected_action = null;
    },
  ],
  [
    "a remapped choice",
    (f: any) => {
      f.view.selected_action.choice.content_hash = "d".repeat(64);
    },
  ],
  [
    "revoked gateway registration",
    (f: any) => {
      f.deps.actionClient = async () => ({
        listFeedback: async () => {
          throw Error("user_action_required");
        },
      });
    },
  ],
  ["retired native authority", (f: any) => f.retire()],
  [
    "a reset session (new incarnation)",
    (f: any) => {
      f.options.sessionAccessAuthority.target.lifecycleRevision =
        "native-life-2";
      f.options.params.expectedLifecycleRevision = "native-life-2";
    },
  ],
  ["a changed workspace", (f: any) => f.changeWorkspace()],
])(
  "blocks delivery after %s and keeps that conversation blocked",
  async (_label, change) => {
    const f = setup();
    await f.fence.load(f.options, f.use);
    change(f);
    const code = await refused(f.fence.deliver(f.options, f.use, content));
    expect([
      "binding_changed",
      "choice_unavailable",
      "unauthorized",
      "unavailable",
    ]).toContain(code);
    // A fresh choice cannot resume use in the same session incarnation.
    const again = setup();
    again.fence = f.fence;
    expect(await refused(f.fence.load(again.options, again.use))).toBe(
      "binding_changed",
    );
  },
);

it("refuses partial, truncated or normalized payloads byte for byte", async () => {
  for (const payload of [
    content.slice(0, -1),
    content.replace("\r\n", "\n"),
    content.normalize("NFKC"),
    "",
  ]) {
    const f = setup();
    await f.fence.load(f.options, f.use);
    expect(await refused(f.fence.deliver(f.options, f.use, payload))).toBe(
      "binding_changed",
    );
  }
});

it("requires fresh authority for the same registered person and session incarnation", async () => {
  const f = setup();
  await f.fence.load(f.options, f.use);
  const foreign = { ...f.use, sessionId: "other-session" };
  expect(await refused(f.fence.deliver(f.options, foreign, content))).toBe(
    "unauthorized",
  );
  const synthetic = setup();
  synthetic.options.client.internal.syntheticClient = true;
  expect(
    await refused(synthetic.fence.load(synthetic.options, synthetic.use)),
  ).toBe("unauthorized");
  expect(synthetic.calls).toHaveLength(0);
});

it("blocks a choice that expires while the native read is awaited", async () => {
  const f = setup();
  const base = Date.parse("2026-10-31T00:00:00Z");
  let now = base;
  f.view.choice_expires_at = new Date(base + 500).toISOString();
  f.deps.wallNow = () => now;
  const read = f.deps.dispatch;
  f.deps.dispatch = async (...args: any[]) => {
    now = base + 1000;
    return read(...args);
  };
  expect(await refused(f.fence.load(f.options, f.use))).toBe(
    "choice_unavailable",
  );
});

/**
 * Spec 0.65 7.6.1: "If unload is unavailable, stop further RelayMind-managed
 * use in that conversation and require a fresh session with fresh
 * authority/choice before resuming." A restart does not lift that stop.
 */
it("keeps a blocked conversation blocked across a restart", async () => {
  const path = ledgerPath();
  const f = setup(true, path);
  await f.fence.load(f.options, f.use);
  f.setSkills([]);
  expect(await refused(f.fence.deliver(f.options, f.use, content))).toBe(
    "binding_changed",
  );
  const restarted = setup(true, path);
  expect(
    await refused(restarted.fence.load(restarted.options, restarted.use)),
  ).toBe("binding_changed");
  expect(restarted.calls).toHaveLength(0);
});

it("refuses managed use without a durable ledger before any call", async () => {
  const f = setup(true, null);
  expect(await refused(f.fence.load(f.options, f.use))).toBe("unavailable");
  expect(f.calls).toHaveLength(0);
  expect(f.gateway).toHaveLength(0);
});
