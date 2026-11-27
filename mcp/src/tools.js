// The tools the MCP server offers, and the SQL behind each one.
//
// Every answer is bounded (limits, pagination, a character budget for page text), carries the ids and URLs
// needed to cite the source, and keeps the record's own conclusion apart from the machine assessment.

import { toFtsQuery } from "./fts.js";

export class ToolInputError extends Error {}

// Label vocabularies, as fixed by the analysis stage (ufo_pipeline/analyze.py) and the build's consistency rules.
const SHAPES = ["disc", "sphere-orb", "cylinder-cigar", "triangle", "oval-egg", "light-point", "fireball", "rectangle", "cone", "irregular", "multiple-shapes", "other", "not-described"];
const MODES = ["visual-ground", "visual-air", "visual-sea", "radar-ground", "radar-air", "radar-sea", "infrared", "photograph", "film-video", "sonar", "satellite", "audio", "other"];
const STATUSES = ["identified", "probably-identified", "unidentified", "insufficient-information", "not-evaluated"];
const EXPLANATIONS = ["aircraft", "balloon", "astronomical", "meteor-fireball", "satellite-space-debris", "atmospheric-optical", "radar-artifact", "sensor-artifact", "missile-rocket", "drone-uas", "birds-wildlife", "hoax", "misperception", "foreign-technology", "other", "none-identified"];
const HYNEK = ["NL", "DD", "RV", "CE1", "CE2", "CE3", "not-applicable"];
const BANDS = ["strong", "moderate", "limited", "minimal"];
const MEDIA = ["pdf", "image", "video", "audio"];
const DISPOSITIONS = ["explained-in-record", "likely-explained", "insufficient-information", "unexplained-in-record", "not-a-sighting-record", "contains-incidents"];
const KINDS = ["incident-report", "case-file", "analysis-or-study", "policy-or-procedure", "correspondence", "sensor-media", "audio-recording", "photograph", "administrative", "press-or-public", "other"];
const THEMES = ["Military encounters", "Aviation safety", "Radar tracking", "Infrared and EO sensor video", "Nuclear sites", "Space and astronaut reports", "Maritime and undersea", "Foreign reports", "Intelligence assessment", "Policy and procedure", "Public and press", "Physical evidence", "Photographic evidence", "Scientific study", "Hoax or misidentification", "Law enforcement", "Congressional and oversight", "Cold War air defense"];
const ENTITY_KINDS = ["person", "organization", "place", "equipment", "program", "file-number"];
const REPORT_SECTIONS = ["summary", "provenance", "incidents", "records", "key_passages", "entities", "related", "media"];

const READING_NOTES = [
  "Summaries, labels and assessments are machine-generated research aids. Check the cited page (get_pages) before quoting or relying on a claim.",
  "status_in_record is what the record itself concludes. 'not-evaluated' means the record offers no evaluation; it does not mean officially unidentified or anomalous.",
  "machine_explanation and machine_likelihood are the analysis model's own assessment, never the record's conclusion.",
  "documentation_score (0-100) measures how well an incident is documented, not how anomalous it is.",
  "position_basis says how a map position was obtained: 'stated' or 'city' are confirmed, 'estimated' is the model's approximation, 'region' and 'country' are centroids.",
  "The same event can appear in several files. Searches count one account per event unless include_repeat_accounts is set; the count of distinct events is an estimate.",
];

/** D1 wrapper that adds up the rows each statement read. */
export class Db {
  constructor(d1) {
    this.d1 = d1;
    this.rowsRead = 0;
  }

  /** @param {Array<[string, ...any]>} statements  @returns {Promise<any[][]>} one row list per statement */
  async batch(statements) {
    const results = await this.d1.batch(statements.map(([sql, ...params]) => this.d1.prepare(sql).bind(...params)));
    for (const r of results) this.rowsRead += r.meta?.rows_read || 0;
    return results.map((r) => r.results || []);
  }

  async all(sql, ...params) {
    return (await this.batch([[sql, ...params]]))[0];
  }
}

// ---------- argument helpers ----------

function text(args, key, { max = 300, required = false } = {}) {
  const v = args[key];
  if (v === undefined || v === null || v === "") {
    if (required) throw new ToolInputError(`"${key}" is required.`);
    return undefined;
  }
  if (typeof v !== "string") throw new ToolInputError(`"${key}" must be a string.`);
  const s = v.trim();
  if (!s) {
    if (required) throw new ToolInputError(`"${key}" is required.`);
    return undefined;
  }
  if (s.length > max) throw new ToolInputError(`"${key}" is too long (at most ${max} characters).`);
  return s;
}

function number(args, key, { min, max, integer = true } = {}) {
  let v = args[key];
  if (v === undefined || v === null || v === "") return undefined;
  if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) v = Number(v);
  if (typeof v !== "number" || !Number.isFinite(v) || (integer && !Number.isInteger(v))) {
    throw new ToolInputError(`"${key}" must be ${integer ? "a whole number" : "a number"}.`);
  }
  if ((min !== undefined && v < min) || (max !== undefined && v > max)) throw new ToolInputError(`"${key}" must be between ${min} and ${max}.`);
  return v;
}

function choice(args, key, allowed) {
  const v = text(args, key, { max: 80 });
  if (v === undefined) return undefined;
  const hit = allowed.find((a) => a.toLowerCase() === v.toLowerCase());
  if (!hit) throw new ToolInputError(`"${key}" must be one of: ${allowed.join(", ")}.`);
  return hit;
}

function flag(args, key) {
  const v = args[key];
  if (v === undefined || v === null) return undefined;
  if (typeof v === "boolean") return v;
  if (v === "true" || v === "false") return v === "true";
  throw new ToolInputError(`"${key}" must be true or false.`);
}

function paging(args, defaultLimit, maxLimit) {
  return { limit: number(args, "limit", { min: 1, max: maxLimit }) ?? defaultLimit, offset: number(args, "offset", { min: 0, max: 100000 }) ?? 0 };
}

function likeContains(value) {
  return "%" + value.replace(/[\\%_]/g, "\\$&") + "%";
}

function paged(total, offset, rows) {
  const out = { total, offset, returned: rows.length };
  if (offset + rows.length < total) out.next_offset = offset + rows.length;
  return out;
}

function parse(json, fallback = {}) {
  try {
    return JSON.parse(json);
  } catch {
    return fallback;
  }
}

function urls(site) {
  const report = (id) => `${site}/#/report/${encodeURIComponent(id)}`;
  return {
    report,
    page: (id, n) => `${report(id)}?page=${n}`,
    incident: (reportId, id) => `${report(reportId)}?incident=${encodeURIComponent(id)}`,
  };
}

function distanceKm(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const a = Math.sin(((lat2 - lat1) * rad) / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(((lon2 - lon1) * rad) / 2) ** 2;
  return 12742 * Math.asin(Math.min(1, Math.sqrt(a)));
}

async function requireReport(db, id) {
  const rows = await db.all("SELECT id, title, page_lo, page_hi, card, detail FROM reports WHERE id = ?1", id);
  if (!rows.length) throw new ToolInputError(`No report with id "${id}". Find report ids with search_reports or search_text.`);
  return rows[0];
}

// ---------- tools ----------

async function corpusOverview(_args, { db, site }) {
  const rows = await db.all("SELECT json FROM meta WHERE key = 'overview'");
  return { site, ...parse(rows[0]?.json), reading_notes: READING_NOTES };
}

async function searchText(args, { db, site }) {
  const { match } = toFtsQuery(text(args, "query", { max: 400, required: true }));
  const reportId = text(args, "report_id");
  const { limit, offset } = paging(args, 10, 20);
  const link = urls(site);

  let range = "";
  const bounds = [];
  if (reportId) {
    const report = await requireReport(db, reportId);
    if (report.page_lo === null) return { query: match, ...paged(0, offset, []), results: [] };
    range = " AND pages_fts.rowid BETWEEN ?4 AND ?5";
    bounds.push(report.page_lo, report.page_hi);
  }
  const [hits, count] = await db.batch([
    ["SELECT p.report, r.title, p.page, p.label, snippet(pages_fts, 0, '«', '»', ' … ', 40) AS snip " +
      "FROM pages_fts JOIN pages p ON p.n = pages_fts.rowid JOIN reports r ON r.id = p.report " +
      `WHERE pages_fts MATCH ?1${range} ORDER BY pages_fts.rank LIMIT ?2 OFFSET ?3`, match, limit, offset, ...bounds],
    [`SELECT COUNT(*) AS n FROM pages_fts WHERE pages_fts MATCH ?1${range.replace("?4", "?2").replace("?5", "?3")}`, match, ...bounds],
  ]);
  return {
    query: match,
    ...paged(count[0]?.n ?? 0, offset, hits),
    results: hits.map((h) => ({ report: h.report, report_title: h.title, page: h.page, label: h.label, snippet: h.snip, url: link.page(h.report, h.page) })),
  };
}

async function searchIncidents(args, { db, site }) {
  const link = urls(site);
  const { limit, offset } = paging(args, 15, 50);
  const where = [];
  const params = [];
  const add = (sql, ...values) => {
    where.push(sql);
    params.push(...values);
  };

  const query = text(args, "query", { max: 400 });
  let from = "incidents i";
  if (query) {
    from = "incidents_fts JOIN incidents i ON i.n = incidents_fts.rowid";
    add("incidents_fts MATCH ?", toFtsQuery(query).match);
  }

  const yearFrom = number(args, "year_from", { min: 1000, max: 2100 });
  const yearTo = number(args, "year_to", { min: 1000, max: 2100 });
  if (yearFrom !== undefined) add("i.year >= ?", yearFrom);
  if (yearTo !== undefined) add("i.year <= ?", yearTo);
  const country = text(args, "country", { max: 2 });
  if (country) add("i.country = ?", country.toUpperCase());
  const region = text(args, "region", { max: 40 });
  if (region) add("i.region = ?", region.toUpperCase());
  for (const [key, column, allowed] of [["shape", "shape", SHAPES], ["status_in_record", "status", STATUSES], ["machine_explanation", "explanation", EXPLANATIONS],
    ["hynek", "hynek", HYNEK], ["documentation_band", "band", BANDS], ["media", "media", MEDIA]]) {
    const v = choice(args, key, allowed);
    if (v) add(`i.${column} = ?`, v);
  }
  const minScore = number(args, "min_documentation_score", { min: 0, max: 100 });
  if (minScore !== undefined) add("i.score >= ?", minScore);
  const mode = choice(args, "observed_by", MODES);
  if (mode) add("i.modes LIKE ?", `%|${mode}|%`);
  const agency = text(args, "agency", { max: 80 });
  if (agency) add("i.agency LIKE ? ESCAPE '\\'", likeContains(agency));
  const reportId = text(args, "report_id");
  if (reportId) add("i.report = ?", reportId);
  const eventId = text(args, "event_id");
  if (eventId) add("i.event = ?", eventId);
  const clusterId = text(args, "cluster_id", { max: 20 });
  if (clusterId) add("i.cluster = ?", clusterId);

  // Repeat accounts of one event are hidden unless asked for, or unless the filter is about accounts (one file, one event).
  const repeats = flag(args, "include_repeat_accounts") ?? Boolean(reportId || eventId);
  if (!repeats) add("i.repeat = 0");

  const nearLat = number(args, "near_lat", { min: -90, max: 90, integer: false });
  const nearLon = number(args, "near_lon", { min: -180, max: 180, integer: false });
  if ((nearLat === undefined) !== (nearLon === undefined)) throw new ToolInputError('"near_lat" and "near_lon" must be given together.');
  const radius = number(args, "radius_km", { min: 1, max: 2000, integer: false }) ?? 100;
  const counting = repeats ? "accounts" : "events";

  if (nearLat !== undefined) {
    // Box in SQL, exact great-circle distance here, then only the requested page of cards is fetched.
    const dLat = radius / 111.0;
    const dLon = Math.min(180, radius / (111.32 * Math.max(0.01, Math.cos((nearLat * Math.PI) / 180))));
    add("i.lat BETWEEN ? AND ?", nearLat - dLat, nearLat + dLat);
    add("i.lon BETWEEN ? AND ?", nearLon - dLon, nearLon + dLon);
    const box = await db.all(`SELECT i.id, i.lat, i.lon FROM ${from} WHERE ${where.join(" AND ")} LIMIT 4000`, ...params);
    const inside = box.map((r) => ({ id: r.id, km: distanceKm(nearLat, nearLon, r.lat, r.lon) })).filter((r) => r.km <= radius).sort((a, b) => a.km - b.km);
    const page = inside.slice(offset, offset + limit);
    let cards = [];
    if (page.length) {
      const rows = await db.all(`SELECT id, report, card FROM incidents WHERE id IN (${page.map(() => "?").join(",")})`, ...page.map((r) => r.id));
      const byId = new Map(rows.map((r) => [r.id, r]));
      cards = page.filter((r) => byId.has(r.id)).map((r) => ({ ...parse(byId.get(r.id).card), distance_km: Math.round(r.km), url: link.incident(byId.get(r.id).report, r.id) }));
    }
    return { counting, sorted_by: "distance", ...paged(inside.length, offset, cards), incidents: cards };
  }

  const sort = choice(args, "sort", ["relevance", "documentation", "date", "date_desc"]) ?? (query ? "relevance" : "documentation");
  if (sort === "relevance" && !query) throw new ToolInputError('"sort": "relevance" needs a "query".');
  const order = {
    relevance: "bm25(incidents_fts), i.n",
    documentation: "i.score DESC, i.n",
    date: "i.date IS NULL, i.date, i.n",
    date_desc: "i.date IS NULL, i.date DESC, i.n",
  }[sort];
  // The count is a second statement in the same round trip: a window count cannot be combined with FTS ranking.
  const clause = where.length ? " WHERE " + where.join(" AND ") : "";
  const [rows, count] = await db.batch([
    [`SELECT i.id, i.report, i.card FROM ${from}${clause} ORDER BY ${order} LIMIT ? OFFSET ?`, ...params, limit, offset],
    [`SELECT COUNT(*) AS n FROM ${from}${clause}`, ...params],
  ]);
  const cards = rows.map((r) => ({ ...parse(r.card), url: link.incident(r.report, r.id) }));
  const total = count[0]?.n ?? 0;
  return { counting, sorted_by: sort, ...paged(total, offset, cards), incidents: cards };
}

async function getIncident(args, { db, site }) {
  const id = text(args, "incident_id", { required: true });
  const link = urls(site);
  const [found, accounts, linked] = await db.batch([
    ["SELECT i.report, i.event, i.cluster, i.card, i.detail, r.title AS report_title, c.size AS cluster_size " +
      "FROM incidents i JOIN reports r ON r.id = i.report LEFT JOIN clusters c ON c.id = i.cluster WHERE i.id = ?1", id],
    ["SELECT id, report, title, date FROM incidents WHERE event = (SELECT event FROM incidents WHERE id = ?1) AND id <> ?1 ORDER BY n LIMIT 25", id],
    ["SELECT l.factors, l.why, l.other, o.report, o.title, o.date, o.place, h.kind, h.label FROM " +
      "(SELECT b AS other, factors, why FROM links WHERE a = ?1 UNION ALL SELECT a AS other, factors, why FROM links WHERE b = ?1) l " +
      "LEFT JOIN incidents o ON o.id = l.other LEFT JOIN hubs h ON h.id = l.other LIMIT 60", id],
  ]);
  if (!found.length) throw new ToolInputError(`No incident with id "${id}". Find incident ids with search_incidents.`);
  const row = found[0];
  const card = parse(row.card);
  const detail = parse(row.detail);
  const connection = (l) => ({ factors: l.factors ? l.factors.split(",") : [], why: parse(l.why, []) });
  return {
    id,
    url: link.incident(row.report, id),
    report: { id: row.report, title: row.report_title, url: link.report(row.report) },
    position_basis: card.position_basis,
    ...detail,
    other_accounts_of_this_event: accounts.map((a) => ({ id: a.id, report: a.report, title: a.title, date: a.date ?? undefined })),
    connections: {
      note: "Links are made only by the named factors (same time and place, matching description, same account in another file, same named person, same mission). A link is not a finding that the events are related.",
      cluster: row.cluster ? { id: row.cluster, incidents: row.cluster_size, list_with: `search_incidents {"cluster_id": "${row.cluster}"}` } : undefined,
      incidents: linked.filter((l) => l.title).slice(0, 25).map((l) => ({ id: l.other, title: l.title, date: l.date ?? undefined, place: l.place || undefined, ...connection(l) })),
      people_and_missions: linked.filter((l) => l.label).map((l) => ({ kind: l.kind, name: l.label, ...connection(l) })),
    },
  };
}

async function searchReports(args, { db, site }) {
  const link = urls(site);
  const { limit, offset } = paging(args, 10, 30);
  const where = [];
  const params = [];
  const add = (sql, ...values) => {
    where.push(sql);
    params.push(...values);
  };
  const query = text(args, "query", { max: 400 });
  let from = "reports r";
  if (query) {
    from = "reports_fts JOIN reports r ON r.n = reports_fts.rowid";
    add("reports_fts MATCH ?", toFtsQuery(query).match);
  }
  for (const [key, column, allowed] of [["media", "media", MEDIA], ["kind", "kind", KINDS], ["disposition", "disposition", DISPOSITIONS]]) {
    const v = choice(args, key, allowed);
    if (v) add(`r.${column} = ?`, v);
  }
  const theme = choice(args, "theme", THEMES);
  if (theme) add("r.themes LIKE ?", `%|${theme}|%`);
  const agency = text(args, "agency", { max: 80 });
  if (agency) add("r.agency LIKE ? ESCAPE '\\'", likeContains(agency));
  const release = text(args, "release", { max: 10 });
  if (release) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(release)) throw new ToolInputError('"release" must be a date like 2026-05-08 (see corpus_overview for the release dates).');
    add("r.release = ?", release);
  }
  const yearFrom = number(args, "year_from", { min: 1000, max: 2100 });
  const yearTo = number(args, "year_to", { min: 1000, max: 2100 });
  if (yearFrom !== undefined) add("r.year >= ?", yearFrom);
  if (yearTo !== undefined) add("r.year <= ?", yearTo);
  const hasIncidents = flag(args, "has_incidents");
  if (hasIncidents !== undefined) add(hasIncidents ? "r.incidents > 0" : "r.incidents = 0");

  const sort = choice(args, "sort", ["relevance", "research_value", "year", "year_desc", "pages"]) ?? (query ? "relevance" : "research_value");
  if (sort === "relevance" && !query) throw new ToolInputError('"sort": "relevance" needs a "query".');
  const order = {
    relevance: "bm25(reports_fts, 4.0, 1.0), r.n",
    research_value: "r.value DESC, r.incidents DESC, r.n",
    year: "r.year IS NULL, r.year, r.n",
    year_desc: "r.year IS NULL, r.year DESC, r.n",
    pages: "r.pages DESC, r.n",
  }[sort];
  const clause = where.length ? " WHERE " + where.join(" AND ") : "";
  const [rows, count] = await db.batch([
    [`SELECT r.id, r.card FROM ${from}${clause} ORDER BY ${order} LIMIT ? OFFSET ?`, ...params, limit, offset],
    [`SELECT COUNT(*) AS n FROM ${from}${clause}`, ...params],
  ]);
  const total = count[0]?.n ?? 0;
  const cards = rows.map((r) => ({ ...parse(r.card), url: link.report(r.id) }));
  return { sorted_by: sort, ...paged(total, offset, cards), reports: cards };
}

async function getReport(args, { db, site }) {
  const id = text(args, "report_id", { required: true });
  const link = urls(site);
  let sections = args.sections;
  if (sections === undefined || sections === null) sections = ["summary"];
  if (typeof sections === "string") sections = [sections];
  if (!Array.isArray(sections) || !sections.length || sections.some((s) => !REPORT_SECTIONS.includes(s))) {
    throw new ToolInputError(`"sections" must be a list drawn from: ${REPORT_SECTIONS.join(", ")}.`);
  }
  const want = new Set(sections);
  const { limit, offset } = paging(args, 20, 50);

  const statements = [["SELECT id, card, detail, page_lo, page_hi FROM reports WHERE id = ?1", id]];
  const slots = {};
  const queue = (name, ...statement) => {
    slots[name] = statements.length;
    statements.push(statement);
  };
  if (want.has("incidents")) queue("incidents", "SELECT id, card FROM incidents WHERE report = ?1 ORDER BY n LIMIT ?2 OFFSET ?3", id, limit, offset);
  if (want.has("records")) {
    queue("records", "SELECT json FROM records WHERE report = ?1 ORDER BY idx LIMIT ?2 OFFSET ?3", id, limit, offset);
    queue("records_n", "SELECT COUNT(*) AS n FROM records WHERE report = ?1", id);
  }
  if (want.has("key_passages")) {
    queue("passages", "SELECT json FROM passages WHERE report = ?1 ORDER BY idx LIMIT ?2 OFFSET ?3", id, limit, offset);
    queue("passages_n", "SELECT COUNT(*) AS n FROM passages WHERE report = ?1", id);
  }
  if (want.has("entities")) {
    queue("entities", "SELECT kind, name, role, org FROM entities WHERE report = ?1 ORDER BY kind, n LIMIT ?2 OFFSET ?3", id, limit * 4, offset);
    queue("entities_n", "SELECT COUNT(*) AS n FROM entities WHERE report = ?1", id);
  }
  const results = await db.batch(statements);
  if (!results[0].length) throw new ToolInputError(`No report with id "${id}". Find report ids with search_reports or search_text.`);
  const row = results[0][0];
  const card = parse(row.card);
  const detail = parse(row.detail);
  const synthesis = detail.synthesis || {};

  const out = {
    id, title: card.title, url: link.report(id), plain_page_url: `${site}/files/${encodeURIComponent(id)}/`,
    original_file_url: detail.publisher?.original_source_url,
    agency: card.agency, media: card.media, kind: card.kind, release: card.release, year: card.year,
    pages: row.page_lo === null ? 0 : row.page_hi - row.page_lo + 1, incidents: card.incidents,
    analysis_status: detail.analysis_status,
  };
  if (want.has("summary")) {
    out.summary = {
      text: synthesis.summary, key_points: synthesis.key_points, document_type: synthesis.document_type, document_date: synthesis.document_date,
      period_covered: synthesis.period_covered, themes: synthesis.themes,
      machine_assessment: synthesis.assessment, ratings: synthesis.ratings,
    };
  }
  if (want.has("provenance")) {
    out.provenance = {
      ...synthesis.provenance, completeness: synthesis.completeness, redaction: synthesis.redaction, official_handling: synthesis.official_handling,
      publisher: detail.publisher, text_extraction: detail.extraction, quote_checks: detail.quote_checks, analysis_gaps: detail.analysis_gaps,
    };
  }
  if (want.has("incidents")) {
    const rows = results[slots.incidents];
    out.incidents = { ...paged(card.incidents ?? rows.length, offset, rows), items: rows.map((r) => ({ ...parse(r.card), url: link.incident(id, r.id) })) };
  }
  if (want.has("records")) {
    const rows = results[slots.records];
    out.records = { note: "Constituent documents inside this file.", ...paged(results[slots.records_n][0].n, offset, rows), items: rows.map((r) => parse(r.json)) };
  }
  if (want.has("key_passages")) {
    const rows = results[slots.passages];
    out.key_passages = {
      note: "check is 'verified' (exact match in the page text) or 'approximate' (close match). Passages not found in the page text are not published.",
      ...paged(results[slots.passages_n][0].n, offset, rows), items: rows.map((r) => parse(r.json)),
    };
  }
  if (want.has("entities")) {
    const rows = results[slots.entities];
    out.entities = {
      ...paged(results[slots.entities_n][0].n, offset, rows),
      items: rows.map((r) => ({ kind: r.kind, name: r.name, role: r.role || undefined, org: r.org || undefined })),
    };
  }
  if (want.has("related")) out.related = (detail.related || []).map((r) => ({ ...r, url: link.report(r.id) }));
  if (want.has("media")) out.media = detail.frame_review ? { frame_review: detail.frame_review } : { note: "No video frame review for this file." };
  return out;
}

async function getPages(args, { db, site }) {
  const id = text(args, "report_id", { required: true });
  const fromPage = number(args, "from_page", { min: 1, max: 100000 }) ?? 1;
  const toPage = number(args, "to_page", { min: 1, max: 100000 }) ?? fromPage;
  if (toPage < fromPage) throw new ToolInputError('"to_page" must not be before "from_page".');
  if (toPage - fromPage >= 10) throw new ToolInputError("Ask for at most 10 pages per call.");
  const maxChars = number(args, "max_chars", { min: 500, max: 40000 }) ?? 12000;
  const link = urls(site);

  const [found, rows] = await db.batch([
    ["SELECT id, title, page_lo, page_hi FROM reports WHERE id = ?1", id],
    ["SELECT page, label, method, flags, text FROM pages WHERE report = ?1 AND page BETWEEN ?2 AND ?3 ORDER BY page", id, fromPage, toPage],
  ]);
  if (!found.length) throw new ToolInputError(`No report with id "${id}". Find report ids with search_reports or search_text.`);
  const report = found[0];
  const pageCount = report.page_lo === null ? 0 : report.page_hi - report.page_lo + 1;

  const pages = [];
  let left = maxChars;
  let continueFrom;
  for (const row of rows) {
    if (left <= 0) {
      continueFrom = row.page;
      break;
    }
    const page = { page: row.page, label: row.label, text_source: row.method, flags: row.flags ? row.flags.split(",") : undefined, url: link.page(id, row.page), text: row.text };
    if (row.text.length > left) {
      page.text = row.text.slice(0, left);
      page.truncated = true;
      page.chars_omitted = row.text.length - left;
    }
    left -= page.text.length;
    pages.push(page);
  }
  const out = {
    report: { id, title: report.title, url: link.report(id) },
    pages_with_text: pageCount,
    note: "text_source says how each page's text was obtained (for example llm_ocr = vision-model transcription, native = the PDF's own text, publisher = the publisher's description, frames = machine description of sampled video frames). Bracketed notes in the text are the transcriber's.",
    pages,
  };
  if (!rows.length) out.note = pageCount ? `No text for pages ${fromPage}-${toPage}; this report has ${pageCount} text unit(s), numbered from 1.` : "This report has no extracted text.";
  if (continueFrom !== undefined) out.continue_from_page = continueFrom;
  return out;
}

async function findEntity(args, { db }) {
  const { match } = toFtsQuery(text(args, "name", { max: 200, required: true }));
  const kind = choice(args, "kind", ENTITY_KINDS);
  const limit = number(args, "limit", { min: 1, max: 40 }) ?? 15;
  const cap = 600;
  const rows = await db.all(
    "SELECT e.kind, e.name, e.role, e.org, e.report FROM entities_fts JOIN entities e ON e.n = entities_fts.rowid " +
    `WHERE entities_fts MATCH ?1${kind ? " AND e.kind = ?3" : ""} LIMIT ?2`, match, cap, ...(kind ? [kind] : []),
  );
  const groups = new Map();
  for (const r of rows) {
    const key = r.kind + "|" + r.name.toLowerCase();
    let g = groups.get(key);
    if (!g) groups.set(key, (g = { kind: r.kind, name: r.name, roles: new Set(), orgs: new Set(), reports: new Set() }));
    if (r.role) g.roles.add(r.role);
    if (r.org) g.orgs.add(r.org);
    g.reports.add(r.report);
  }
  const ranked = [...groups.values()].sort((a, b) => b.reports.size - a.reports.size || a.name.localeCompare(b.name));
  return {
    query: match,
    matches: ranked.length,
    truncated: rows.length >= cap ? "Many mentions matched; only the first 600 were grouped. Use a more specific name." : undefined,
    note: "Names are as written in each file, so one person or office can appear under several spellings. reports lists report ids (up to 12); read one with get_report.",
    entities: ranked.slice(0, limit).map((g) => ({
      kind: g.kind, name: g.name,
      roles: g.roles.size ? [...g.roles].slice(0, 3) : undefined,
      orgs: g.orgs.size ? [...g.orgs].slice(0, 3) : undefined,
      report_count: g.reports.size,
      reports: [...g.reports].slice(0, 12),
    })),
  };
}

// ---------- definitions ----------

const str = (description, extra = {}) => ({ type: "string", description, ...extra });
const int = (description, extra = {}) => ({ type: "integer", description, ...extra });
const LIMIT = (def, max) => int(`How many to return (default ${def}, at most ${max}).`, { minimum: 1, maximum: max });
const OFFSET = int("Skip this many results; use next_offset from the previous answer to page.", { minimum: 0 });
const QUERY_HELP = 'Plain words (all must match), "quoted phrases", a trailing * for a prefix (radar*), and upper-case OR / NOT.';
const readOnly = (title) => ({ title, readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false });

const DEFINITIONS = [
  {
    name: "corpus_overview",
    title: "Corpus overview",
    description: "Start here. Corpus statistics (files, incidents, events by decade, status, explanation, country), the release dates, a written synthesis with " +
      "cross-cutting findings and caveats, notable files, and the notes needed to read every other tool's answer correctly.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    run: corpusOverview,
  },
  {
    name: "search_text",
    title: "Full-text search",
    description: "Search the extracted text of every page and transcript in the released files. Returns the best-matching pages with a snippet (matches marked «like this»), " +
      "the page number and a URL that opens the page. Follow up with get_pages to read the full page before quoting it.",
    inputSchema: {
      type: "object",
      properties: {
        query: str(`Search text. ${QUERY_HELP}`),
        report_id: str("Search inside one report only."),
        limit: LIMIT(10, 20),
        offset: OFFSET,
      },
      required: ["query"],
      additionalProperties: false,
    },
    run: searchText,
  },
  {
    name: "search_incidents",
    title: "Search incidents",
    description: "Find extracted observation accounts (sightings) by text and structured filters. By default each event is listed once even when several files " +
      "describe it. status_in_record is the record's own conclusion; machine_explanation is the analysis model's assessment. Use get_incident for full detail.",
    inputSchema: {
      type: "object",
      properties: {
        query: str(`Text to match in the incident title, place and narrative. ${QUERY_HELP}`),
        year_from: int("Earliest incident year."),
        year_to: int("Latest incident year."),
        country: str("ISO 3166 two-letter country code, e.g. US, BR, GB."),
        region: str("State or province code as recorded, e.g. NM, CA (US states use postal codes)."),
        shape: str("Object shape.", { enum: SHAPES }),
        observed_by: str("How it was observed: one observation mode or sensor.", { enum: MODES }),
        status_in_record: str("What the record itself concludes. 'not-evaluated' means the record gives no evaluation.", { enum: STATUSES }),
        machine_explanation: str("Category of the analysis model's best conventional explanation ('none-identified' if none).", { enum: EXPLANATIONS }),
        hynek: str("Hynek classification.", { enum: HYNEK }),
        documentation_band: str("How well documented the incident is (not how anomalous).", { enum: BANDS }),
        min_documentation_score: int("Minimum documentation score, 0-100.", { minimum: 0, maximum: 100 }),
        agency: str("Agency whose file holds the account, e.g. FBI, NASA, CIA, Department of War."),
        media: str("Media type of the source file.", { enum: MEDIA }),
        near_lat: { type: "number", description: "Latitude of a point to search around (with near_lon). Results are sorted by distance." },
        near_lon: { type: "number", description: "Longitude of the point." },
        radius_km: { type: "number", description: "Search radius in km around near_lat/near_lon (default 100, at most 2000)." },
        report_id: str("Only incidents in this report."),
        event_id: str("All accounts of one event (the 'event' value of an incident, or the incident id when it has none)."),
        cluster_id: str("Only incidents in this Connections cluster (from get_incident)."),
        include_repeat_accounts: { type: "boolean", description: "List every account, including repeats of the same event in other files (default false)." },
        sort: str("Order of results (default: relevance with a query, otherwise documentation).", { enum: ["relevance", "documentation", "date", "date_desc"] }),
        limit: LIMIT(15, 50),
        offset: OFFSET,
      },
      additionalProperties: false,
    },
    run: searchIncidents,
  },
  {
    name: "get_incident",
    title: "Incident detail",
    description: "Everything recorded about one incident: date, time and place, narrative, witnesses, sensors, object description, weather, official action, " +
      "the record's own status and explanation, the separate machine assessment, page-checked quotes, other accounts of the same event, and linked incidents with the reason for each link.",
    inputSchema: {
      type: "object",
      properties: { incident_id: str("Incident id from search_incidents or get_report.") },
      required: ["incident_id"],
      additionalProperties: false,
    },
    run: getIncident,
  },
  {
    name: "search_reports",
    title: "Search reports",
    description: "Find released files (one report per file) by text and filters. Returns title, agency, media, kind, disposition, page and incident counts and a short summary. " +
      "The disposition is a machine label for what the file's record concludes; 'unexplained-in-record' means the file offers no resolution, not that the event is anomalous.",
    inputSchema: {
      type: "object",
      properties: {
        query: str(`Text to match in the report title and summary. ${QUERY_HELP}`),
        agency: str("Releasing agency, e.g. FBI, NASA, CIA, Department of War, Department of State."),
        media: str("Media type.", { enum: MEDIA }),
        kind: str("Kind of record.", { enum: KINDS }),
        disposition: str("File-level disposition.", { enum: DISPOSITIONS }),
        theme: str("Theme.", { enum: THEMES }),
        release: str("Publisher release date, YYYY-MM-DD (listed by corpus_overview)."),
        year_from: int("Earliest primary year of the file's content."),
        year_to: int("Latest primary year of the file's content."),
        has_incidents: { type: "boolean", description: "Only files with (true) or without (false) extracted incidents." },
        sort: str("Order of results (default: relevance with a query, otherwise research_value).", { enum: ["relevance", "research_value", "year", "year_desc", "pages"] }),
        limit: LIMIT(10, 30),
        offset: OFFSET,
      },
      additionalProperties: false,
    },
    run: searchReports,
  },
  {
    name: "get_report",
    title: "Report detail",
    description: "Read one report in sections, so large files stay manageable. 'summary' (default): summary, page-cited key points, machine assessment and ratings. " +
      "'provenance': originating office, file numbers, markings, redaction, completeness, how the text was extracted. 'incidents', 'records' (constituent documents), " +
      "'key_passages' (page-checked quotes) and 'entities' are paged with limit/offset. 'related': similar reports. 'media': video frame review.",
    inputSchema: {
      type: "object",
      properties: {
        report_id: str("Report id from search_reports, search_text or an incident."),
        sections: { type: "array", items: { type: "string", enum: REPORT_SECTIONS }, description: "Sections to return (default [\"summary\"])." },
        limit: LIMIT(20, 50),
        offset: OFFSET,
      },
      required: ["report_id"],
      additionalProperties: false,
    },
    run: getReport,
  },
  {
    name: "get_pages",
    title: "Read source pages",
    description: "Read the extracted source text of a report, page by page: OCR or native text for documents, the publisher description, frame analysis and transcript for video and audio. " +
      "Use this to verify a claim against the record before quoting it. Up to 10 pages per call, within a character budget.",
    inputSchema: {
      type: "object",
      properties: {
        report_id: str("Report id."),
        from_page: int("First page (default 1).", { minimum: 1 }),
        to_page: int("Last page (default: the same as from_page).", { minimum: 1 }),
        max_chars: int("Character budget for the text returned (default 12000, at most 40000). Text beyond it is cut and marked truncated.", { minimum: 500, maximum: 40000 }),
      },
      required: ["report_id"],
      additionalProperties: false,
    },
    run: getPages,
  },
  {
    name: "find_entity",
    title: "Find people, organisations and file numbers",
    description: "Look up a person, organisation, place, programme, piece of equipment or file number named in the files, and list the reports that mention it.",
    inputSchema: {
      type: "object",
      properties: {
        name: str("Name or part of a name, e.g. Hynek, Project Blue Book, Kirtland, AFR 200-2. A trailing * matches a prefix."),
        kind: str("Restrict to one kind of entity.", { enum: ENTITY_KINDS }),
        limit: LIMIT(15, 40),
      },
      required: ["name"],
      additionalProperties: false,
    },
    run: findEntity,
  },
];

const RUNNERS = new Map(DEFINITIONS.map((t) => [t.name, t.run]));

/** Tool definitions as sent to clients, in a fixed order. */
export const TOOLS = DEFINITIONS.map(({ run, title, ...tool }) => ({ ...tool, title, annotations: readOnly(title) }));

export function hasTool(name) {
  return RUNNERS.has(name);
}

/** Runs a tool. Throws ToolInputError (or the search QueryError) for bad arguments. */
export function runTool(name, args, context) {
  return RUNNERS.get(name)(args, context);
}
