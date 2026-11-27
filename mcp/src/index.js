// MCP server for the Declassified UFO Files corpus: a stateless, read-only, tools-only server over
// Streamable HTTP, written without an SDK so it has no dependencies and no build step.
//
// It serves two generations of the protocol on one endpoint:
//   - 2026-07-28 ("modern"): no handshake; every request carries its protocol version in params._meta and in
//     headers, clients discover the server with server/discover, every result has a resultType.
//   - 2025-11-25 and earlier ("legacy"): the initialize handshake, then tools/list and tools/call.
// A request that carries the modern _meta is served by the modern rules; anything else by the legacy rules.

import { QueryError } from "./fts.js";
import { chargeRequest, chargeRows, clientAllowed } from "./limits.js";
import { Db, TOOLS, ToolInputError, hasTool, runTool } from "./tools.js";

const SERVER = { name: "declassified-ufo-files", title: "Declassified UFO Files", version: "1.0.0" };
const MODERN = ["2026-07-28"];
const LEGACY = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];
const META_VERSION = "io.modelcontextprotocol/protocolVersion";
const META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities";
const META_SERVER = "io.modelcontextprotocol/serverInfo";
const MAX_BODY_BYTES = 64 * 1024;
const LIST_TTL_MS = 3_600_000;   // the tool list only changes with a deploy

const INSTRUCTIONS =
  "Research access to the U.S. Department of War PURSUE UAP/UFO file releases: one machine-written, page-cited report per released file, " +
  "the incidents extracted from them, and the full extracted text. This is an independent project, not a government service. " +
  "Call corpus_overview first. Summaries, labels and assessments are machine-generated: verify a claim with get_pages before quoting it, " +
  "and cite the report and page URL. status_in_record is the record's own conclusion ('not-evaluated' means the record gives none, not " +
  "that the event is unidentified or anomalous); machine_explanation is the analysis model's assessment.";

// Any website may call this server: it is public, read-only and uses no cookies or credentials, so there is no
// origin to protect and nothing for a DNS-rebinding attack to reach. Every Origin is therefore valid.
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Accept, Authorization, MCP-Protocol-Version, Mcp-Method, Mcp-Name, Mcp-Session-Id, Last-Event-ID",
  "Access-Control-Max-Age": "86400",
};

function json(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...CORS, ...headers } });
}

function rpcResult(id, result) {
  return json({ jsonrpc: "2.0", id, result });
}

function rpcError(id, code, message, status = 200, data, headers) {
  return json({ jsonrpc: "2.0", id: id ?? null, error: data === undefined ? { code, message } : { code, message, data } }, status, headers);
}

function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Header values that are not plain ASCII arrive as =?base64?...?= (2026-07-28). */
function headerValue(value) {
  const m = /^=\?base64\?([A-Za-z0-9+/=]*)\?=$/.exec(value ?? "");
  if (!m) return value;
  try {
    return new TextDecoder().decode(Uint8Array.from(atob(m[1]), (c) => c.charCodeAt(0)));
  } catch {
    return value;
  }
}

/** The 2026-07-28 per-request checks. Returns an error Response, or null when the request is well formed. */
function checkModern(request, id, method, params) {
  const meta = params._meta;
  const version = meta[META_VERSION];
  if (!MODERN.includes(version)) {
    return rpcError(id, -32022, "Unsupported protocol version", 400, { supported: [...MODERN, ...LEGACY], requested: version });
  }
  const mismatch = (text) => rpcError(id, -32020, `Header mismatch: ${text}`, 400);
  const headerVersion = request.headers.get("MCP-Protocol-Version");
  if (headerVersion !== version) return mismatch(`MCP-Protocol-Version header value '${headerVersion ?? ""}' does not match body value '${version}'`);
  const headerMethod = request.headers.get("Mcp-Method");
  if (headerMethod !== method) return mismatch(`Mcp-Method header value '${headerMethod ?? ""}' does not match body value '${method}'`);
  if (method === "tools/call") {
    const headerName = headerValue(request.headers.get("Mcp-Name"));
    if (headerName !== params.name) return mismatch(`Mcp-Name header value '${headerName ?? ""}' does not match body value '${params.name ?? ""}'`);
  }
  if (!isObject(meta[META_CAPABILITIES])) {
    return rpcError(id, -32602, `Invalid params: _meta["${META_CAPABILITIES}"] is required`, 400);
  }
  return null;
}

function toolText(message, isError, modern) {
  const result = { content: [{ type: "text", text: message }], isError };
  if (modern) result.resultType = "complete";
  return result;
}

async function callTool(id, params, modern, env, ctx, charge) {
  const name = params.name;
  if (typeof name !== "string" || !hasTool(name)) return rpcError(id, -32602, `Unknown tool: ${name}`);
  if (params.arguments !== undefined && !isObject(params.arguments)) return rpcError(id, -32602, "Invalid params: arguments must be an object");
  if (!charge.ok) return rpcResult(id, toolText(charge.reason, true, modern));

  const db = new Db(env.DB);
  const site = String(env.SITE_URL || "").replace(/\/+$/, "");
  try {
    const data = await runTool(name, params.arguments || {}, { db, site });
    return rpcResult(id, toolText(JSON.stringify(data), false, modern));
  } catch (error) {
    if (error instanceof ToolInputError || error instanceof QueryError) return rpcResult(id, toolText(error.message, true, modern));
    console.error(`tool ${name} failed: ${error?.stack || error}`);
    return rpcResult(id, toolText("The query could not be completed. Try again, or simplify the request.", true, modern));
  } finally {
    chargeRows(env, ctx, charge.day, db.rowsRead);
  }
}

async function handlePost(request, env, ctx) {
  if (Number(request.headers.get("Content-Length") || 0) > MAX_BODY_BYTES) return rpcError(null, -32600, "Request too large", 413);
  const raw = await request.text();
  if (raw.length > MAX_BODY_BYTES) return rpcError(null, -32600, "Request too large", 413);

  let message;
  try {
    message = JSON.parse(raw);
  } catch {
    return rpcError(null, -32700, "Parse error", 400);
  }
  if (!isObject(message) || message.jsonrpc !== "2.0") {
    return rpcError(null, -32600, "Invalid Request: send one JSON-RPC 2.0 message per POST (batches are not supported)", 400);
  }
  if (typeof message.method !== "string") {
    // a client's response to a server request: this server never sends requests, so there is nothing to do with it
    if ("result" in message || "error" in message) return new Response(null, { status: 202, headers: CORS });
    return rpcError(message.id, -32600, "Invalid Request: method is required", 400);
  }
  if (message.id === undefined || message.id === null) return new Response(null, { status: 202, headers: CORS });   // a notification

  const { id, method } = message;
  const params = isObject(message.params) ? message.params : {};
  const modern = isObject(params._meta) && typeof params._meta[META_VERSION] === "string";

  if (modern) {
    const problem = checkModern(request, id, method, params);
    if (problem) return problem;
  } else {
    const headerVersion = request.headers.get("MCP-Protocol-Version");
    if (headerVersion && MODERN.includes(headerVersion)) {
      return rpcError(id, -32602, `Invalid params: _meta["${META_VERSION}"] is required for protocol ${headerVersion}`, 400);
    }
    if (headerVersion && !LEGACY.includes(headerVersion)) {
      return rpcError(id, -32022, "Unsupported protocol version", 400, { supported: [...MODERN, ...LEGACY], requested: headerVersion });
    }
  }

  if (!(await clientAllowed(env, request))) {
    const text = "Rate limit reached for this client. Wait 60 seconds before the next request.";
    if (method === "tools/call") return rpcResult(id, toolText(text, true, modern));   // as a tool result, so the model reads it
    return rpcError(id, -32000, text, 429, undefined, { "Retry-After": "60" });
  }
  const charge = await chargeRequest(env);

  const done = (result) => rpcResult(id, modern ? { resultType: "complete", ...result } : result);
  if (method === "tools/call") return callTool(id, params, modern, env, ctx, charge);
  if (method === "tools/list") return done(modern ? { tools: TOOLS, ttlMs: LIST_TTL_MS, cacheScope: "public" } : { tools: TOOLS });

  if (modern) {
    if (method === "server/discover") {
      return done({
        supportedVersions: MODERN, capabilities: { tools: {} }, _meta: { [META_SERVER]: SERVER },
        instructions: INSTRUCTIONS, ttlMs: LIST_TTL_MS, cacheScope: "public",
      });
    }
    return rpcError(id, -32601, `Method not found: ${method}`, 404);
  }

  if (method === "initialize") {
    const requested = params.protocolVersion;
    return done({
      protocolVersion: LEGACY.includes(requested) ? requested : LEGACY[0],
      capabilities: { tools: {} }, serverInfo: SERVER, instructions: INSTRUCTIONS,
    });
  }
  if (method === "ping") return done({});
  return rpcError(id, -32601, `Method not found: ${method}`);
}

export default {
  async fetch(request, env, ctx) {
    const path = new URL(request.url).pathname;
    if (path !== "/mcp" && path !== "/mcp/") return new Response("Not found\n", { status: 404 });
    try {
      if (request.method === "POST") return await handlePost(request, env, ctx);
      if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
      // GET would open a server-to-client stream and DELETE would end a session; this server has neither.
      const site = String(env.SITE_URL || "").replace(/\/+$/, "");
      return new Response(`This is a Model Context Protocol (MCP) endpoint for ${site || "the Declassified UFO Files"}. Add this URL to an MCP client; it answers POST only.\n`, {
        status: 405, headers: { Allow: "POST, OPTIONS", "Content-Type": "text/plain; charset=utf-8", ...CORS },
      });
    } catch (error) {
      console.error(`request failed: ${error?.stack || error}`);
      return rpcError(null, -32603, "Internal error", 500);
    }
  },
};
