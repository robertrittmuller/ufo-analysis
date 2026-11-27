// Turns free text from an agent into a safe SQLite FTS5 MATCH expression.
//
// Accepted: plain words (all must match), "quoted phrases", a trailing * for a prefix (radar*), and the
// upper-case operators AND, OR, NOT. Everything else is treated as text, so no input can produce an FTS5
// syntax error. Very common words are dropped from bare terms: on their own they match most of the corpus.

const STOPWORDS = new Set(("a about after all also an and any are as at be been but by can could did do does for from had has have he her his " +
  "how i if in into is it its may more no not of on one or other our out she so some such than that the their them then there these they this " +
  "to up was we were what when which who will with would you your").split(" "));

const MAX_TERMS = 16;

export class QueryError extends Error {}

function words(text) {
  return text.toLowerCase().match(/[\p{L}\p{N}]+/gu) || [];
}

/** @returns {{ match: string, terms: string[] }} */
export function toFtsQuery(input) {
  const text = String(input ?? "").slice(0, 400);
  const parts = [];   // { op } | { term, plain }
  for (const m of text.matchAll(/"([^"]*)"(\*?)|(\S+)/g)) {
    if (m[1] !== undefined) {
      const w = words(m[1]);
      if (w.length) parts.push({ term: `"${w.join(" ")}"${m[2] && w.length === 1 ? "*" : ""}`, plain: w.join(" ") });
      continue;
    }
    const token = m[3];
    if (token === "AND" || token === "OR" || token === "NOT") { parts.push({ op: token }); continue; }
    const prefix = token.endsWith("*");
    const w = words(token);
    if (!w.length) continue;
    if (w.length === 1 && !prefix && STOPWORDS.has(w[0])) continue;
    // "F-16" or "AFR-200-2" becomes a phrase, as the tokenizer splits it
    parts.push({ term: `"${w.join(" ")}"${prefix ? "*" : ""}`, plain: w.join(" ") });
  }

  // keep operators only where they sit between two terms
  const out = [];
  for (const part of parts) {
    if (part.op) {
      if (out.length && !out[out.length - 1].op) out.push(part);
    } else {
      out.push(part);
    }
  }
  while (out.length && out[out.length - 1].op) out.pop();

  const terms = out.filter((p) => p.term);
  if (!terms.length) {
    throw new QueryError("The query has no searchable words. Use specific words, names or a \"quoted phrase\"; very common words such as \"the\" or \"of\" are ignored.");
  }
  if (terms.length > MAX_TERMS) throw new QueryError(`The query has too many terms (${terms.length}); use at most ${MAX_TERMS}.`);
  return { match: out.map((p) => p.op || p.term).join(" "), terms: terms.map((p) => p.plain) };
}
