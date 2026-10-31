// Single-attempt loopback JSON transport. Credentials and gateway text never enter errors.
import { parsePluginConfig } from "./config.ts";
import {
  encodeJson,
  object,
  parseProtocol,
  identifier,
} from "./recommendation-contract.ts";
export interface ReadOptions {
  signal: AbortSignal;
  deadlineMs: number;
  /** In-process native owner fence; never serialized or accepted from request JSON. */
  assertCurrent?: () => void;
}
export interface WriteOptions extends ReadOptions {
  idempotencyKey: string;
}
export interface ClientConfig {
  baseUrl: string;
  applicationToken: () => Promise<string>;
  fetch: typeof fetch;
  monotonicNow: () => number;
  maxResponseBytes: number;
}
export interface ActionClientConfig extends ClientConfig {
  actionToken: () => Promise<string>;
}
const statuses: Record<string, number> = {
  unauthenticated: 401,
  internal_error: 500,
  invalid_request: 400,
  user_action_required: 403,
  recommendation_not_found: 404,
  feedback_not_found: 404,
  stale_version: 409,
  idempotency_conflict: 409,
  recommendation_in_progress: 409,
  recommendation_expired: 409,
  asset_changed: 409,
  invalid_state: 409,
  overloaded: 429,
  recommendation_unavailable: 503,
  deadline_exceeded: 504,
};
export class RecommendationClientError extends Error {
  readonly code: string;
  readonly status: number | null;
  readonly recommendationId: string | null;
  constructor(
    code: string,
    status: number | null = null,
    recommendationId: string | null = null,
  ) {
    super(code);
    this.name = "RecommendationClientError";
    this.code = code;
    this.status = status;
    this.recommendationId = recommendationId;
  }
}
export function input<T>(fn: () => T): T {
  try {
    return fn();
  } catch {
    throw new RecommendationClientError("invalid_request");
  }
}
function key(
  options: ReadOptions | WriteOptions,
  method: string,
): string | undefined {
  if (method === "GET") return undefined;
  const value = (options as WriteOptions).idempotencyKey;
  if (
    typeof value !== "string" ||
    value !== value.trim() ||
    !/^[\x20-\x7e]{1,255}$/.test(value)
  )
    throw new RecommendationClientError("invalid_request");
  return value;
}
function hasSecret(
  value: unknown,
  secrets: string[],
  check: () => void = () => {},
): boolean {
  check();
  if (typeof value === "string") {
    for (const secret of secrets) {
      if (secret.length > value.length) continue;
      // Overlap each bounded search by the credential length, so a secret
      // spanning chunk boundaries is checked without scanning the full value.
      for (let offset = 0; offset < value.length; offset += 4096) {
        check();
        if (
          value
            .slice(offset, offset + 4096 + secret.length - 1)
            .includes(secret)
        )
          return true;
      }
    }
    check();
    return false;
  }
  if (Array.isArray(value)) {
    for (const child of value)
      if (hasSecret(child, secrets, check)) return true;
  } else if (typeof value === "object" && value !== null) {
    for (const key in value) {
      check();
      if (!Object.hasOwn(value, key)) continue;
      if (
        hasSecret(key, secrets, check) ||
        hasSecret((value as Record<string, unknown>)[key], secrets, check)
      )
        return true;
    }
  }
  return false;
}
export function transport(
  config: ClientConfig,
  actionToken?: () => Promise<string>,
) {
  let origin: string;
  try {
    origin = new URL(parsePluginConfig({ baseUrl: config.baseUrl }).baseUrl)
      .origin;
  } catch {
    throw new RecommendationClientError("invalid_request");
  }
  if (
    !Number.isSafeInteger(config.maxResponseBytes) ||
    config.maxResponseBytes < 1 ||
    config.maxResponseBytes > 32 * 1024 * 1024
  )
    throw new RecommendationClientError("invalid_request");
  return async function request<T>(
    method: string,
    path: string,
    body: unknown,
    options: ReadOptions | WriteOptions,
    decode: (text: string, check: () => void) => T,
  ): Promise<T> {
    const idempotencyKey = key(options, method);
    if (!options.signal || !Number.isFinite(options.deadlineMs))
      throw new RecommendationClientError("invalid_request");
    const controller = new AbortController();
    let timedOut = false;
    const check = () => {
      options.assertCurrent?.();
      if (options.signal.aborted)
        throw new RecommendationClientError("cancelled");
      if (timedOut || config.monotonicNow() >= options.deadlineMs)
        throw new RecommendationClientError("deadline_exceeded");
    };
    check();
    let rejectStop: (error: RecommendationClientError) => void = () => {};
    const stopped = new Promise<never>((_, reject) => {
      rejectStop = reject;
    });
    const cancel = () => {
      controller.abort();
      rejectStop(new RecommendationClientError("cancelled"));
    };
    options.signal.addEventListener("abort", cancel, { once: true });
    // Node timers cannot represent an arbitrary absolute deadline in one delay.
    const delay = Math.min(
      2147483647,
      Math.max(1, options.deadlineMs - config.monotonicNow()),
    );
    let timer: ReturnType<typeof setTimeout>;
    const expire = () => {
      const remaining = options.deadlineMs - config.monotonicNow();
      if (remaining > 0 && !options.signal.aborted) {
        timer = setTimeout(
          expire,
          Math.min(2147483647, Math.max(1, remaining)),
        );
        return;
      }
      timedOut = true;
      controller.abort();
      rejectStop(
        new RecommendationClientError(
          options.signal.aborted ? "cancelled" : "deadline_exceeded",
        ),
      );
    };
    timer = setTimeout(expire, delay);
    const wait = async <T>(promise: Promise<T>) => {
      const value = await Promise.race([promise, stopped]);
      check();
      return value;
    };
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      const app = await wait(Promise.resolve().then(config.applicationToken));
      const action = actionToken
        ? await wait(Promise.resolve().then(actionToken))
        : undefined;
      const secrets = [app, ...(action === undefined ? [] : [action])];
      if (
        secrets.some(
          (s) => typeof s !== "string" || !s || !/^[\x21-\x7e]+$/.test(s),
        )
      )
        throw new RecommendationClientError("unauthenticated");
      if (
        hasSecret(body, secrets, check) ||
        hasSecret(path, secrets, check) ||
        (idempotencyKey !== undefined &&
          hasSecret(idempotencyKey, secrets, check))
      )
        throw new RecommendationClientError("invalid_request");
      const headers = new Headers({
        Authorization: "Bearer " + app,
        Accept: "application/json",
      });
      if (action !== undefined)
        headers.set("X-RelayMind-User-Action-Token", action);
      if (idempotencyKey !== undefined)
        headers.set("Idempotency-Key", idempotencyKey);
      let serialized: string | undefined;
      if (body !== undefined) {
        serialized = encodeJson(body);
        if (new TextEncoder().encode(serialized).length > 524288)
          throw new RecommendationClientError("invalid_request");
        headers.set("Content-Type", "application/json");
      }
      const response = await wait(
        Promise.resolve().then(() => {
          check();
          return config.fetch(origin + path, {
            method,
            headers,
            redirect: "error",
            signal: controller.signal,
            cache: "no-store",
            credentials: "omit",
            ...(serialized === undefined ? {} : { body: serialized }),
          });
        }),
      );
      if (
        response.redirected ||
        (response.status >= 300 && response.status < 400)
      ) {
        void response.body?.cancel().catch(() => {});
        throw new RecommendationClientError("protocol_error");
      }
      if (
        response.headers
          .get("Content-Type")
          ?.split(";")[0]
          .trim()
          .toLowerCase() !== "application/json"
      ) {
        void response.body?.cancel().catch(() => {});
        throw new RecommendationClientError("protocol_error");
      }
      const length = response.headers.get("Content-Length");
      if (
        length !== null &&
        (length.length > 20 ||
          !/^\d+$/.test(length) ||
          BigInt(length) > BigInt(config.maxResponseBytes))
      ) {
        void response.body?.cancel().catch(() => {});
        throw new RecommendationClientError("protocol_error");
      }
      if (!response.body) throw new RecommendationClientError("protocol_error");
      reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8", {
        fatal: true,
        ignoreBOM: true,
      });
      let text = "",
        size = 0;
      while (true) {
        check();
        const part = await wait(reader.read());
        if (part.done) break;
        size += part.value.byteLength;
        if (size > config.maxResponseBytes)
          throw new RecommendationClientError("protocol_error");
        try {
          for (let offset = 0; offset < part.value.length; offset += 16384) {
            check();
            text += decoder.decode(
              part.value.subarray(offset, offset + 16384),
              { stream: true },
            );
          }
        } catch {
          check();
          throw new RecommendationClientError("protocol_error");
        }
      }
      try {
        text += decoder.decode();
      } catch {
        throw new RecommendationClientError("protocol_error");
      }
      check();
      if (response.status !== 200) {
        try {
          const value = parseProtocol(text, check);
          object(value, ["error"]);
          object(value.error, [
            "code",
            "message",
            "run_id",
            "recommendation_id",
          ]);
          const e = value.error;
          if (
            typeof e.code !== "string" ||
            statuses[e.code] !== response.status ||
            typeof e.message !== "string" ||
            e.run_id !== null
          )
            throw Error();
          if (e.recommendation_id !== null) identifier(e.recommendation_id);
          if (
            hasSecret(
              { code: e.code, recommendation_id: e.recommendation_id },
              secrets,
              check,
            )
          )
            throw Error();
          throw new RecommendationClientError(
            e.code,
            response.status,
            e.recommendation_id,
          );
        } catch (error) {
          if (error instanceof RecommendationClientError) throw error;
          check();
          throw new RecommendationClientError("protocol_error");
        }
      }
      let value: T;
      try {
        value = decode(text, check);
        if (hasSecret(value, secrets, check)) throw Error();
      } catch (error) {
        check();
        if (error instanceof RecommendationClientError) throw error;
        throw new RecommendationClientError("protocol_error");
      }
      check();
      return value;
    } catch (error) {
      check();
      if (error instanceof RecommendationClientError) throw error;
      throw new RecommendationClientError("transport_error");
    } finally {
      clearTimeout(timer);
      options.signal.removeEventListener("abort", cancel);
      if (reader) {
        void reader.cancel().catch(() => {});
        try {
          reader.releaseLock();
        } catch {
          /* Pending read is being cancelled. */
        }
      }
      controller.abort();
    }
  };
}
