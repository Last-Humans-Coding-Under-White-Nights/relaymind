// A recording loopback endpoint for host qualification (RM33, #135).
//
// Two modes share one recorder:
// - proxy: forward every request unchanged to an upstream gateway and record the
//   request and the response bytes. A client disconnect closes the upstream
//   request, so an abort reaches the gateway as it would without the proxy.
// - scripted: answer every request itself from a list of scripted replies (the
//   controlled endpoint a real OpenClaw host is pointed at to capture its requests).
//
// Sanitized: the Authorization value is never recorded, and no token is written.
import http from "node:http";

export const REDACTED = "<redacted>";

// Headers that carry a credential or a host-specific value that changes per run.
const SECRET_HEADERS = new Set(["authorization", "x-api-key", "api-key", "cookie"]);

export function sanitizeHeaders(headers) {
  const out = {};
  for (const [name, value] of Object.entries(headers)) {
    const key = name.toLowerCase();
    out[key] = SECRET_HEADERS.has(key) ? REDACTED : value;
  }
  return out;
}

function parseBody(text) {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** The SSE data lines of a response body, or null when it is not an event stream. */
export function sseData(text) {
  const out = [];
  for (const block of text.split("\n\n")) {
    for (const line of block.split("\n")) {
      if (line.startsWith("data: ")) {
        const data = line.slice("data: ".length);
        out.push(data === "[DONE]" ? data : parseBody(data));
      } else if (line.startsWith(":")) {
        out.push({ comment: line.slice(1).trim() });
      }
    }
  }
  return out;
}

function readAll(stream) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    stream.on("data", (chunk) => chunks.push(chunk));
    stream.on("end", () => resolve(Buffer.concat(chunks)));
    stream.on("error", reject);
  });
}

/**
 * Start a recorder on 127.0.0.1 (options.port, or a free port). options.upstream (an origin such as
 * http://127.0.0.1:8080) selects proxy mode; options.replies (a list of
 * {status, headers, body} or a function of the request) selects scripted mode.
 */
export async function startRecorder(options) {
  const exchanges = [];
  let replyIndex = 0;
  const server = http.createServer(async (req, res) => {
    const raw = await readAll(req);
    const exchange = {
      request: {
        method: req.method,
        path: req.url,
        headers: sanitizeHeaders(req.headers),
        body: parseBody(raw.toString("utf8")),
      },
      response: null,
      client_closed_early: false,
    };
    exchanges.push(exchange);
    if (options.upstream) {
      proxy(options.upstream, req, raw, res, exchange);
    } else {
      const reply =
        typeof options.replies === "function"
          ? options.replies(exchange.request, replyIndex++)
          : options.replies[Math.min(replyIndex++, options.replies.length - 1)];
      const body = typeof reply.body === "string" ? reply.body : JSON.stringify(reply.body);
      const status = reply.status ?? 200;
      const headers = reply.headers ?? { "content-type": "application/json" };
      res.writeHead(status, headers);
      res.end(body);
      exchange.response = { status, headers, body: parseResponse(headers, body) };
    }
  });
  await new Promise((resolve) => server.listen(options.port ?? 0, "127.0.0.1", resolve));
  const { port } = server.address();
  return {
    origin: `http://127.0.0.1:${port}`,
    exchanges,
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections?.();
        server.close(() => resolve());
      }),
  };
}

function parseResponse(headers, text) {
  const type = String(headers?.["content-type"] ?? "");
  if (type.startsWith("text/event-stream")) return { sse: sseData(text) };
  return parseBody(text);
}

function proxy(upstream, req, raw, res, exchange) {
  const target = new URL(req.url, upstream);
  const headers = { ...req.headers, host: target.host };
  const chunks = [];
  let finished = false;
  const upstreamRequest = http.request(
    target,
    { method: req.method, headers },
    (upstreamResponse) => {
      res.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.headers);
      upstreamResponse.on("data", (chunk) => {
        chunks.push(chunk);
        res.write(chunk);
      });
      upstreamResponse.on("end", () => {
        finished = true;
        res.end();
        record();
      });
      upstreamResponse.on("error", () => record());
      exchange.response = {
        status: upstreamResponse.statusCode,
        headers: sanitizeHeaders(upstreamResponse.headers),
        body: null,
      };
    },
  );
  const record = () => {
    if (!exchange.response) exchange.response = { status: null, headers: {}, body: null };
    exchange.response.body = parseResponse(
      exchange.response.headers,
      Buffer.concat(chunks).toString("utf8"),
    );
  };
  upstreamRequest.on("error", () => record());
  // The client went away before the response ended: close the upstream request,
  // so the gateway sees the disconnect (section 7.4).
  res.on("close", () => {
    if (!finished) {
      exchange.client_closed_early = true;
      upstreamRequest.destroy();
      record();
    }
  });
  upstreamRequest.end(raw);
}
