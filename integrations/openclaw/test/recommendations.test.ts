// RM36/RM37/RM39; AC27/AC28/AC30: bounded dormant clients and lossless evidence.
import { readFileSync } from "node:fs";
import { expect, it, vi } from "vitest";
import * as implementation from "../src/recommendations.ts";
import * as protocol from "../src/recommendation-contract.ts";
const api: Record<string, any> = implementation;
const contract: Record<string, any> = protocol;
const fixture = readFileSync(
  new URL("./fixtures/recommendation-view.v1.json", import.meta.url),
  "utf8",
);
const ID = "4774d146-17bd-4ec9-8507-f528ccea51fa",
  APP = "private-application-token",
  ACTION = "private-action-token";
const options = () => ({
  signal: new AbortController().signal,
  deadlineMs: performance.now() + 5000,
});
const request = {
  schema_version: 1,
  task_text: "greeting",
  requested_kinds: ["skill"],
  selection_scope: "discovery",
};
const json = (text: string, status = 200) =>
  new Response(text, {
    status,
    headers: { "Content-Type": "application/json" },
  });
function client(fetcher: typeof fetch, extra: object = {}) {
  return api.createRecommendationClient({
    baseUrl: "http://127.0.0.1:8080/v1",
    applicationToken: async () => APP,
    fetch: fetcher,
    monotonicNow: () => performance.now(),
    maxResponseBytes: 1048576,
    ...extra,
  });
}
function action(fetcher: typeof fetch) {
  return api.createUserActionClient({
    baseUrl: "http://127.0.0.1:8080/v1",
    applicationToken: async () => APP,
    actionToken: async () => ACTION,
    fetch: fetcher,
    monotonicNow: () => performance.now(),
    maxResponseBytes: 1048576,
  });
}
it("supplies the published strict protocol and clients", () => {
  expect(api.createRecommendationClient).toBeTypeOf("function");
  expect(contract.decodeRecommendationView).toBeTypeOf("function");
});
it("preserves SQLite revisions above Number.MAX_SAFE_INTEGER and unknown usage", () => {
  const view = contract.decodeRecommendationView(
    fixture.replace('"state_version": 1', '"state_version": 9007199254740993'),
  );
  expect(view.state_version).toBe(9007199254740993n);
  expect(view.result.attempts[0].result.usage_claims).toBeNull();
  expect(view.result.attempts[0].cost_observations[0].value.cpu_ms).toBeNull();
});
it.each([
  fixture.replace('"state_version": 1', '"state_version":1,"state_version":2'),
  fixture.replace('"state_version": 1', '"state_version":9223372036854775808'),
  fixture.replace('"state_version": 1', '"state_version":1.5'),
  fixture.replace('"state": "ready"', '"state":"computing"'),
  fixture.replace(
    '"host_snapshot_revision": null',
    '"host_snapshot_revision":"asserted"',
  ),
  fixture.replace('"schema_version": 1', '"schema_version":3'),
  fixture.replace('"state_version": 1', '"state_version":1,"actor":"user"'),
  fixture.replace('"asset_id": "skill:', '"asset_id":"different:'),
  '{"x":'.repeat(70) + "null" + "}".repeat(70),
])("rejects malformed, duplicate, unknown or inconsistent evidence", (text) => {
  expect(() => contract.decodeRecommendationView(text)).toThrow();
});
it("uses exact ordinary path, stable key and bearer without action credential", async () => {
  const calls: any[] = [];
  const c = client(async (url, init) => {
    calls.push({ url: String(url), init });
    return json(fixture);
  });
  expect(
    (await c.create(request, { ...options(), idempotencyKey: "same-key" }))
      .recommendation_id,
  ).toBe(ID);
  expect(calls).toHaveLength(1);
  expect(calls[0].url).toBe("http://127.0.0.1:8080/relay/v1/recommendations");
  const h = new Headers(calls[0].init.headers);
  expect(h.get("Authorization")).toBe("Bearer " + APP);
  expect(h.has("X-RelayMind-User-Action-Token")).toBe(false);
  expect(h.get("Idempotency-Key")).toBe("same-key");
  expect(calls[0].init.redirect).toBe("error");
  expect(calls[0].init.body).not.toContain(APP);
});
it("encodes bigint action bodies and privacy query exactly", async () => {
  const calls: any[] = [];
  const c = action(async (url, init) => {
    calls.push({ url: String(url), init });
    return json(fixture);
  });
  await c.choose(
    ID,
    {
      schema_version: 1,
      expected_version: 9007199254740993n,
      choice: { kind: "none" },
    },
    { ...options(), idempotencyKey: "choice" },
  );
  expect(calls[0].init.body).toBe(
    '{"schema_version":1,"expected_version":9007199254740993,"choice":{"kind":"none"}}',
  );
  expect(
    new Headers(calls[0].init.headers).get("X-RelayMind-User-Action-Token"),
  ).toBe(ACTION);
  const d = action(async (url, init) => {
    calls.push({ url: String(url), init });
    return json(
      '{"schema_version":1,"feedback_revision":9007199254740994,"deleted":true}',
    );
  });
  const receipt = await d.deleteFeedback(ID, 9007199254740993n, {
    ...options(),
    idempotencyKey: "delete",
  });
  expect(calls[1].url).toBe(
    `http://127.0.0.1:8080/relay/v1/feedback/${ID}?expected_feedback_revision=9007199254740993`,
  );
  expect(calls[1].init.body).toBeUndefined();
  expect(receipt.feedback_revision).toBe(9007199254740994n);
});
it.each(["stale_version", "idempotency_conflict"])(
  "preserves safe %s without gateway text or retries",
  async (code) => {
    let calls = 0;
    const c = action(async () => {
      calls++;
      return json(
        JSON.stringify({
          error: {
            code,
            message: APP + ACTION,
            run_id: null,
            recommendation_id: ID,
          },
        }),
        409,
      );
    });
    const error = await c
      .choose(
        ID,
        { schema_version: 1, expected_version: 1n, choice: { kind: "none" } },
        { ...options(), idempotencyKey: "stable" },
      )
      .catch((e: unknown) => e);
    expect(error).toMatchObject({ code, status: 409, recommendationId: ID });
    expect(String(error) + JSON.stringify(error)).not.toContain(APP);
    expect(String(error) + JSON.stringify(error)).not.toContain(ACTION);
    expect(calls).toBe(1);
  },
);
it("rejects redirects and redacts credential-bearing network exceptions", async () => {
  const e = await client(async () => {
    throw Error(APP + ACTION);
  })
    .get(ID, options())
    .catch((e: unknown) => e);
  expect(e.code).toBe("transport_error");
  expect(String(e) + JSON.stringify(e)).not.toContain(APP);
  await expect(
    client(
      async () =>
        new Response(null, {
          status: 302,
          headers: { location: "http://evil.test" },
        }),
    ).get(ID, options()),
  ).rejects.toMatchObject({ code: "protocol_error" });
});
it("aborts create transport with no hidden retry", async () => {
  const ac = new AbortController();
  let calls = 0,
    aborted = false;
  const c = client(async (_url, init) => {
    calls++;
    return await new Promise<Response>((_resolve, reject) => {
      init!.signal!.addEventListener("abort", () => {
        aborted = true;
        reject(Error(APP));
      });
    });
  });
  const pending = c.create(request, {
    ...options(),
    signal: ac.signal,
    idempotencyKey: "create",
  });
  await new Promise((r) => setTimeout(r, 5));
  ac.abort(APP);
  await expect(pending).rejects.toMatchObject({ code: "cancelled" });
  expect(aborted).toBe(true);
  expect(calls).toBe(1);
});
it("bounds credential lookup and stalled body by the same deadline", async () => {
  await expect(
    client(
      async () =>
        new Response(new ReadableStream({ start() {} }), {
          headers: { "Content-Type": "application/json" },
        }),
    ).get(ID, { ...options(), deadlineMs: performance.now() + 20 }),
  ).rejects.toMatchObject({ code: "deadline_exceeded" });
  let calls = 0;
  const c = client(
    async () => {
      calls++;
      return json(fixture);
    },
    { applicationToken: () => new Promise(() => {}) },
  );
  await expect(
    c.get(ID, { ...options(), deadlineMs: performance.now() + 20 }),
  ).rejects.toMatchObject({ code: "deadline_exceeded" });
  expect(calls).toBe(0);
});
it("refuses oversize, invalid UTF-8 and invalid protocol bodies", async () => {
  await expect(
    client(async () => json(fixture), { maxResponseBytes: 32 }).get(
      ID,
      options(),
    ),
  ).rejects.toMatchObject({ code: "protocol_error" });
  await expect(
    client(async () => new Response(new Uint8Array([255]))).get(ID, options()),
  ).rejects.toMatchObject({ code: "protocol_error" });
  await expect(
    client(async () => json('{"state":"ready"}')).get(ID, options()),
  ).rejects.toMatchObject({ code: "protocol_error" });
});
it("rejects forged snapshots and unsafe IDs before network", async () => {
  let calls = 0;
  const c = client(async () => {
    calls++;
    return json(fixture);
  });
  await expect(
    c.create(
      { ...request, host_snapshot_revision: "forged" },
      { ...options(), idempotencyKey: "ok" },
    ),
  ).rejects.toMatchObject({ code: "invalid_request" });
  await expect(c.get("../feedback", options())).rejects.toMatchObject({
    code: "invalid_request",
  });
  expect(calls).toBe(0);
});
it("validates exact event cursor and request query", async () => {
  const payload = {
    schema_version: 1,
    recommendation_id: ID,
    events: [],
    next_after_seq: 0,
    has_more: false,
  };
  const c = client(async (url) => {
    expect(String(url)).toContain("after_seq=9007199254740993&limit=3");
    return json(
      JSON.stringify(payload).replace(
        '"next_after_seq":0',
        '"next_after_seq":9007199254740993',
      ),
    );
  });
  expect(
    (await c.events(ID, { afterSeq: 9007199254740993n, limit: 3 }, options()))
      .next_after_seq,
  ).toBe(9007199254740993n);
  await expect(
    client(async () =>
      json(JSON.stringify({ ...payload, next_after_seq: 1 })),
    ).events(ID, { afterSeq: 0n, limit: 3 }, options()),
  ).rejects.toMatchObject({ code: "protocol_error" });
});
it("rejects bad UTF-8 and BOM as protocol errors, while accepting property reordering", async () => {
  await expect(
    client(
      async () =>
        new Response(new Uint8Array([255]), {
          headers: { "Content-Type": "application/json" },
        }),
    ).get(ID, options()),
  ).rejects.toMatchObject({ code: "protocol_error" });
  await expect(
    client(async () => json("\ufeff" + fixture)).get(ID, options()),
  ).rejects.toMatchObject({ code: "protocol_error" });
  const reordered = JSON.parse(fixture);
  reordered.displayed_candidates[0].candidate = Object.fromEntries(
    Object.entries(reordered.displayed_candidates[0].candidate).reverse(),
  );
  expect(
    (
      await client(async () => json(JSON.stringify(reordered))).get(
        ID,
        options(),
      )
    ).recommendation_id,
  ).toBe(ID);
});
it("rejects secret echoes hidden by JSON escaping in successful evidence", async () => {
  const value = fixture.replace("Greeting skill", APP);
  const escaped = value.replace(APP, APP.replace("p", "\\u0070"));
  await expect(
    client(async () => json(escaped)).get(ID, options()),
  ).rejects.toMatchObject({ code: "protocol_error" });
});
it("rejects wrong event identity, duplicates and unvalidated payloads", async () => {
  const event = {
    schema_version: 1,
    recommendation_id: ID,
    seq: 1,
    timestamp: "2026-10-30T00:00:00Z",
    elapsed_ms: 0,
    event_type: "feedback.recorded",
    actor: "adapter",
    erased: false,
    payload: { feedback_id: ID, feedback_revision: 1 },
  };
  for (const events of [
    [event, event],
    [{ ...event, recommendation_id: "00000000-0000-0000-0000-000000000000" }],
    [{ ...event, payload: { ...event.payload, actor: "user" } }],
  ]) {
    const page = {
      schema_version: 1,
      recommendation_id: ID,
      events,
      next_after_seq: 1,
      has_more: false,
    };
    await expect(
      client(async () => json(JSON.stringify(page))).events(
        ID,
        { afterSeq: 0n, limit: 3 },
        options(),
      ),
    ).rejects.toMatchObject({ code: "protocol_error" });
  }
});
it("refuses inconsistent readiness and feedback records", async () => {
  const readiness = {
    schema_version: 1,
    state: "ready",
    reason: "usable_catalog",
    catalog_revision: 1,
    catalog_size: 1,
    lexical_ready: true,
    semantic_coverage: { ready: 2, total: 1 },
    pending_jobs: 0,
    failed_jobs: 0,
  };
  await expect(
    client(async () => json(JSON.stringify(readiness))).readiness(options()),
  ).rejects.toMatchObject({ code: "protocol_error" });
  const page = {
    schema_version: 1,
    feedback_revision: 1,
    records: [{ schema_version: 1, feedback_id: ID }],
    next_after_id: null,
  };
  await expect(
    action(async () => json(JSON.stringify(page))).listFeedback(
      { limit: 3 },
      options(),
    ),
  ).rejects.toMatchObject({ code: "protocol_error" });
});
it("checks caller cancellation before deadline and credential loading", async () => {
  const ac = new AbortController();
  ac.abort(APP);
  let calls = 0;
  const c = client(
    async () => {
      calls++;
      return json(fixture);
    },
    {
      applicationToken: async () => {
        calls++;
        return APP;
      },
    },
  );
  await expect(
    c.create(request, {
      signal: ac.signal,
      deadlineMs: -1,
      idempotencyKey: "abort",
    }),
  ).rejects.toMatchObject({ code: "cancelled" });
  expect(calls).toBe(0);
});
it("returns discriminated erased and original event payloads with exact SQLite sequence", async () => {
  const page = {
    schema_version: 1,
    recommendation_id: ID,
    events: [
      {
        schema_version: 1,
        recommendation_id: ID,
        seq: 1,
        timestamp: "2026-10-30T00:00:00Z",
        elapsed_ms: 0,
        event_type: "feedback.recorded",
        actor: "adapter",
        erased: true,
        payload: null,
      },
    ],
    next_after_seq: 1,
    has_more: false,
  };
  const result = await client(async () => json(JSON.stringify(page))).events(
    ID,
    { afterSeq: 0n, limit: 3 },
    options(),
  );
  expect(result.events[0].seq).toBe(1n);
  expect(result.events[0].payload).toBeNull();
});
it("rejects inconsistent terminal preference receipts and bad candidate evidence pointers", () => {
  const view = JSON.parse(fixture);
  view.state = "cancelled";
  expect(() =>
    contract.decodeRecommendationView(JSON.stringify(view)),
  ).toThrow();
  const pointer = JSON.parse(fixture);
  for (const entry of [
    pointer.displayed_candidates[0].candidate,
    ...pointer.result.base_candidates,
    ...pointer.result.final_candidates,
    ...pointer.result.attempts[0].result.candidates,
  ])
    entry.evidence_refs = ["/missing_field"];
  expect(() =>
    contract.decodeRecommendationView(JSON.stringify(pointer)),
  ).toThrow();
});
it("checks the absolute deadline immediately before sending credentials", async () => {
  let clock = -10,
    calls = 0;
  const c = client(
    async () => {
      calls++;
      return json(fixture);
    },
    { monotonicNow: () => (clock += 10) },
  );
  await expect(
    c.get(ID, { signal: new AbortController().signal, deadlineMs: 25 }),
  ).rejects.toMatchObject({ code: "deadline_exceeded" });
  expect(calls).toBe(0);
});
it("interrupts a long JSON string during decoding under the same budget", async () => {
  const { decodeJson } = await import("../src/recommendation-json.ts");
  let checks = 0;
  expect(() =>
    decodeJson('"' + "x".repeat(131072) + '"', () => {
      if (++checks === 2) throw Error("budget_expired");
    }),
  ).toThrow("budget_expired");
});
it("never normalizes caller keys or coerces feedback cursors into wire strings", async () => {
  let calls = 0;
  const fetcher: typeof fetch = async () => {
    calls++;
    return json(fixture);
  };
  await expect(
    client(fetcher).create(request, {
      ...options(),
      idempotencyKey: " padded ",
    }),
  ).rejects.toMatchObject({ code: "invalid_request" });
  await expect(
    action(fetcher).listFeedback({ afterId: 123, limit: 1 }, options()),
  ).rejects.toMatchObject({ code: "invalid_request" });
  expect(calls).toBe(0);
});

it("keeps long absolute deadlines across the native timer range", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(0);
  try {
    let settled: string | undefined;
    const c = client(async () => new Promise<Response>(() => {}), {
      monotonicNow: () => Date.now(),
    });
    const pending = c
      .get(ID, { signal: new AbortController().signal, deadlineMs: 2147483667 })
      .catch((e: any) => {
        settled = e.code;
      });
    await vi.advanceTimersByTimeAsync(2147483647);
    expect(settled).toBeUndefined();
    await vi.advanceTimersByTimeAsync(20);
    await pending;
    expect(settled).toBe("deadline_exceeded");
  } finally {
    vi.useRealTimers();
  }
});
it("binds recorded action event identity and lifecycle revision to its envelope", async () => {
  const actionEvidence = {
    adapter_id: "fixture",
    scope: { user_id: "u", workspace_id: "w" },
    recommendation_id: "00000000-0000-0000-0000-000000000000",
    task_id: "a".repeat(64),
    choice: { kind: "none" },
    catalog_revision: 1,
    host_snapshot_revision: null,
    recorded_at: "2026-10-30T00:00:00Z",
    state_version: 2,
  };
  const event = {
    schema_version: 1,
    recommendation_id: ID,
    seq: 1,
    timestamp: "2026-10-30T00:00:00Z",
    elapsed_ms: 0,
    event_type: "chosen",
    actor: "adapter",
    erased: false,
    payload: { state_version: 2, action: actionEvidence },
  };
  const page = {
    schema_version: 1,
    recommendation_id: ID,
    events: [event],
    next_after_seq: 1,
    has_more: false,
  };
  await expect(
    client(async () => json(JSON.stringify(page))).events(
      ID,
      { afterSeq: 0n, limit: 3 },
      options(),
    ),
  ).rejects.toMatchObject({ code: "protocol_error" });
});

// RM36/RM37, AC27/AC28: Python StrictValidator distinguishes int from float tokens.
it.each(["1.0", "1e0", "1.00000000000000001"])(
  "rejects a noninteger token %s at integer schema constants",
  (token) => {
    expect(() =>
      contract.decodeRecommendationView(
        fixture.replace('"schema_version": 1', '"schema_version": ' + token),
      ),
    ).toThrow();
  },
);
it.each(["1.0", "1e0", "1.00000000000000001"])(
  "rejects a noninteger token %s at ordinary integer counts",
  (token) => {
    expect(() =>
      contract.decodeRecommendationView(
        fixture.replace('"attempt": 1', '"attempt": ' + token),
      ),
    ).toThrow();
  },
);
it.each(["0.0", "0e0", "1e-1000"])(
  "rejects floating and underflow token %s at integer usage counts",
  (token) => {
    expect(() =>
      contract.decodeRecommendationView(
        fixture.replace('"calls": 0', '"calls": ' + token),
      ),
    ).toThrow();
  },
);
it("retains legitimate fractional score and duration fields as numbers", () => {
  const view = contract.decodeRecommendationView(
    fixture.replaceAll('"score": 2', '"score": 2.25'),
  );
  expect(view.displayed_candidates[0].candidate.score).toBe(2.25);
  expect(typeof view.result.observed_ms).toBe("number");
});
it.each([
  " ".repeat(16384) + "0",
  "[0," + " ".repeat(16384) + "1]",
  "0" + " ".repeat(16384),
])(
  "checks the original budget periodically throughout every whitespace position",
  async (payload) => {
    const { decodeJson } = await import("../src/recommendation-json.ts");
    let checks = 0;
    expect(() =>
      decodeJson(payload, () => {
        if (++checks === 8) throw Error("deadline_exceeded");
      }),
    ).toThrow("deadline_exceeded");
  },
);
it("reproduces the review deadline probe during trailing whitespace", async () => {
  const { decodeJson } = await import("../src/recommendation-json.ts");
  const payload = "0" + " ".repeat(4 * 1024 * 1024);
  const started = performance.now();
  let checks = 0,
    expired = false;
  try {
    decodeJson(payload, () => {
      checks++;
      if (performance.now() - started >= 5) throw Error("deadline_exceeded");
    });
  } catch (error) {
    expect((error as Error).message).toBe("deadline_exceeded");
    expired = true;
  }
  // A faster machine may finish inside the budget. It must still have checked
  // during the scan, rather than merely succeeding after unobserved work.
  if (!expired) expect(checks).toBeGreaterThan(4096);
});

it("checks the budget during UTF-8 size preprocessing before protocol parsing", async () => {
  const { parseProtocol } = await import("../src/recommendation-contract.ts");
  let checks = 0;
  expect(() =>
    parseProtocol(" ".repeat(4 * 1024 * 1024) + "0", () => {
      if (++checks === 8) throw Error("deadline_exceeded");
    }),
  ).toThrow("deadline_exceeded");
});
it("does not reflect synthetic UUID-shaped credentials through error metadata", async () => {
  const secret = ID;
  const c = client(
    async () =>
      json(
        JSON.stringify({
          error: {
            code: "recommendation_unavailable",
            message: "safe",
            run_id: null,
            recommendation_id: secret,
          },
        }),
        503,
      ),
    { applicationToken: async () => secret },
  );
  const error = await c.readiness(options()).catch((e: unknown) => e);
  expect(String(error) + JSON.stringify(error)).not.toContain(secret);
  expect(error).toMatchObject({ code: "protocol_error" });
});
it.each([
  ['"\\n\\t\\r\\b\\f\\\\\\/"', "\n\t\r\b\f\\/"],
  ['"\\u0061\\uD83D\\uDE00"', "a😀"],
  ['"问候😀"', "问候😀"],
])(
  "preserves valid JSON string escapes with bounded decoding",
  async (text, expected) => {
    const { decodeJson } = await import("../src/recommendation-json.ts");
    expect(decodeJson(text)).toBe(expected);
  },
);
it.each(['"\\uZZZZ"', '"\\uD800"', '"\\x20"', '"raw\u0001"', '"unfinished\\'])(
  "rejects malformed string encodings",
  async (text) => {
    const { decodeJson } = await import("../src/recommendation-json.ts");
    expect(() => decodeJson(text)).toThrow();
  },
);
it.each([
  ["2026-10-30T14:20:03.123456789+01:30", true],
  ["2026-10-30t14:20:03z", true],
  ["2026-02-30T14:20:03Z", false],
  ["2026-10-30T24:20:03Z", false],
  ["2026-10-30T14:20:03.+01:30", false],
  ["2026-10-30T14:20:03.123+24:00", false],
])(
  "preserves bounded RFC3339 date admission while auditing scanners",
  async (value, valid) => {
    const { validateSchema } = await import("../src/recommendation-schema.ts");
    if (valid)
      expect(() =>
        validateSchema(value, { type: "string", format: "date-time" }),
      ).not.toThrow();
    else
      expect(() =>
        validateSchema(value, { type: "string", format: "date-time" }),
      ).toThrow();
  },
);

// RM36/AC27: periodic checks follow scan iterations, including surrogate pairs.
it.each([
  ["astral at even indices", "😀".repeat(8192), 32768],
  ["astral at odd indices", "a" + "😀".repeat(8192), 32769],
  ["BMP prefix at odd indices", "中" + "😀".repeat(8192), 32771],
  ["even mixed prefix", "a中" + "😀".repeat(8192), 32772],
  ["odd mixed prefix and BMP tail", "a中b" + "😀".repeat(8192) + "文", 32776],
  ["alternating BMP and astral", "中😀".repeat(8192), 57344],
  ["astral tail", "a" + "😀".repeat(8192) + "😀", 32773],
])(
  "interrupts both Unicode loops across %s",
  async (_label, text, expectedBytes) => {
    const { wellFormed, utf8Bytes } = await import(
      "../src/recommendation-json.ts"
    );
    for (const fn of [wellFormed, utf8Bytes]) {
      let checks = 0;
      expect(() =>
        fn(text as string, () => {
          if (++checks === 8) throw Error("deadline_exceeded");
        }),
      ).toThrow("deadline_exceeded");
    }
    expect(wellFormed(text as string)).toBe(true);
    expect(utf8Bytes(text as string)).toBe(expectedBytes);
  },
);
it("keeps the final Unicode checkpoint after a short surrogate-pair end case", async () => {
  const { wellFormed, utf8Bytes } = await import(
    "../src/recommendation-json.ts"
  );
  for (const fn of [wellFormed, utf8Bytes]) {
    let checks = 0;
    expect(() =>
      fn("a😀", () => {
        if (++checks === 2) throw Error("deadline_exceeded");
      }),
    ).toThrow("deadline_exceeded");
  }
  expect(wellFormed("a😀")).toBe(true);
  expect(utf8Bytes("a😀")).toBe(5);
});
// Spec 0.63 section 7.6 (RM37/RM39; AC28/AC30): the caller's expected scope.
it("sends a configured expected scope percent-encoded on every request", async () => {
  const calls: any[] = [];
  const c = client(
    async (url, init) => {
      calls.push({ url: String(url), init });
      return json(fixture);
    },
    { expectedScope: { userId: "ü ser/1", workspaceId: "w" } },
  );
  await c.create(request, { ...options(), idempotencyKey: "k" });
  await c.get(ID, options());
  for (const call of calls) {
    const h = new Headers(call.init.headers);
    expect(h.get("X-RelayMind-Expected-User")).toBe("%C3%BC%20ser%2F1");
    expect(h.get("X-RelayMind-Expected-Workspace")).toBe("w");
  }
});
it("sends no expected scope unless configured", async () => {
  const calls: any[] = [];
  const c = client(async (url, init) => {
    calls.push({ url: String(url), init });
    return json(fixture);
  });
  await c.get(ID, options());
  const h = new Headers(calls[0].init.headers);
  expect(h.has("X-RelayMind-Expected-User")).toBe(false);
  expect(h.has("X-RelayMind-Expected-Workspace")).toBe(false);
});
it.each([
  { userId: "", workspaceId: "w" },
  { userId: "u", workspaceId: "x".repeat(257) },
  { userId: "u" },
])("refuses an invalid expected scope before any request", async (scope) => {
  const fetcher = vi.fn();
  const c = client(fetcher as any, { expectedScope: scope });
  await expect(c.get(ID, options())).rejects.toMatchObject({
    code: "invalid_request",
  });
  expect(fetcher).not.toHaveBeenCalled();
});
it("reports a gateway scope mismatch with its fixed code", async () => {
  const c = action(async () =>
    json(
      JSON.stringify({
        error: {
          code: "scope_mismatch",
          message: "the expected scope is not the scope this gateway serves",
          run_id: null,
          recommendation_id: null,
        },
      }),
      403,
    ),
  );
  await expect(c.listFeedback({ limit: 10 }, options())).rejects.toMatchObject({
    code: "scope_mismatch",
  });
});
