// Request accounting for the public endpoint: a per-client rate limit, and a daily budget kept in the
// database's `usage` table (one row per UTC day).

const DEFAULT_REQUESTS = 100_000;
const DEFAULT_ROWS_READ = 400_000_000;

// Per-isolate memory of an exhausted day.
let exhaustedDay = "";
let exhaustedWhy = "";

function budget(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 ? n : fallback;
}

export function utcDay(now = new Date()) {
  return now.toISOString().slice(0, 10);
}

export function secondsToUtcMidnight(now = new Date()) {
  const next = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1);
  return Math.max(1, Math.ceil((next - now.getTime()) / 1000));
}

/** Per-client limit. Returns true when the caller may proceed. */
export async function clientAllowed(env, request) {
  if (!env.CLIENT_LIMITER) return true;
  const key = request.headers.get("CF-Connecting-IP") || "unknown";
  try {
    const { success } = await env.CLIENT_LIMITER.limit({ key });
    return success;
  } catch {
    return true;
  }
}

/**
 * Count one request against today's budget.
 * @returns {Promise<{ ok: true, day: string } | { ok: false, reason: string }>}
 */
export async function chargeRequest(env) {
  const day = utcDay();
  if (exhaustedDay === day) return { ok: false, reason: exhaustedWhy };
  const maxRequests = budget(env.DAILY_REQUEST_BUDGET, DEFAULT_REQUESTS);
  const maxRows = budget(env.DAILY_ROWS_READ_BUDGET, DEFAULT_ROWS_READ);
  let row;
  try {
    row = await env.DB.prepare(
      "INSERT INTO usage (day, requests, rows_read) VALUES (?1, 1, 0) " +
      "ON CONFLICT(day) DO UPDATE SET requests = requests + 1 RETURNING requests, rows_read",
    ).bind(day).first();
  } catch {
    return { ok: false, reason: "The data service is temporarily unavailable. Try again in a few minutes." };
  }
  if (row.requests > maxRequests || row.rows_read >= maxRows) {
    exhaustedDay = day;
    exhaustedWhy = `This server's daily usage budget is used up. It resets at 00:00 UTC (in about ${Math.ceil(secondsToUtcMidnight() / 3600)} h). ` +
      "Do not retry before then; the dashboard itself stays available.";
    return { ok: false, reason: exhaustedWhy };
  }
  return { ok: true, day };
}

/** Add the rows a tool call read to today's count. Runs after the response has been sent. */
export function chargeRows(env, ctx, day, rows) {
  if (!(rows > 0)) return;
  ctx.waitUntil(
    env.DB.prepare("UPDATE usage SET rows_read = rows_read + ?2 WHERE day = ?1").bind(day, rows).run().catch(() => {}),
  );
}
