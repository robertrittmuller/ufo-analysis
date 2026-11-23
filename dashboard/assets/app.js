/* Declassified UFO Files — overview, explorers, entities, full-text search.
   Data: data/index.json (compact corpus index) + data/reports/<id>.json loaded on demand (report.js). */
"use strict";
const SITE_NAME = "Declassified UFO Files";
const SITE_TITLE = "Declassified UFO Files: Search Every Government UAP Report";

const App = { index: null, reportById: new Map(), incidentsByReport: new Map(), land: null, shards: null };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
const cap = (s) => String(s || "").replace(/-/g, " ").replace(/^./, (c) => c.toUpperCase());

const LABELS = {
  disposition: {
    "explained-in-record": "Explained in the record", "likely-explained": "Likely explained", "insufficient-information": "Insufficient information",
    "unexplained-in-record": "Unexplained in the record", "not-a-sighting-record": "Not a sighting record",
  },
  status: { identified: "Identified", "probably-identified": "Probably identified", unidentified: "Unidentified", "insufficient-information": "Insufficient information", "not-evaluated": "Not evaluated" },
  hynek: { NL: "Nocturnal light", DD: "Daylight disc", RV: "Radar-visual", CE1: "Close encounter I", CE2: "Close encounter II", CE3: "Close encounter III", "not-applicable": "Not applicable" },
  media: { pdf: "Document", image: "Image", video: "Video", audio: "Audio" },
  band: { strong: "Strong", moderate: "Moderate", limited: "Limited", minimal: "Minimal" },
};
// Colour follows the category, never its rank (validated categorical order on the dark surface).
const COLORS = {
  disposition: { "explained-in-record": "var(--s3)", "likely-explained": "var(--s1)", "insufficient-information": "var(--s4)", "unexplained-in-record": "var(--s5)", "not-a-sighting-record": "var(--neutral)" },
  status: { identified: "var(--s3)", "probably-identified": "var(--s1)", "insufficient-information": "var(--s4)", unidentified: "var(--s5)", "not-evaluated": "var(--neutral)" },
};
const label = (kind, key) => (LABELS[kind] && LABELS[kind][key]) || cap(key) || "—";
const badge = (kind, key) => (key ? `<span class="badge"><i style="background:${(COLORS[kind] || {})[key] || "var(--neutral)"}"></i>${esc(label(kind, key))}</span>` : "");
const dateLabel = (iso) => {
  if (!iso) return "";
  const [y, m, d] = iso.split("-");
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return d ? `${+d} ${months[+m - 1]} ${y}` : m ? `${months[+m - 1]} ${y}` : y;
};

/* ---------- tooltip ---------- */
const tip = {
  el: null,
  show(html, ev) { this.el.innerHTML = html; this.el.hidden = false; this.move(ev); },
  move(ev) {
    const r = this.el.getBoundingClientRect();
    let x = ev.clientX + 14, y = ev.clientY + 14;
    if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
    this.el.style.left = Math.max(4, x) + "px"; this.el.style.top = Math.max(4, y) + "px";
  },
  hide() { this.el.hidden = true; },
};
function bindTips(root) {
  $$("[data-tip]", root).forEach((el) => {
    el.addEventListener("mouseenter", (ev) => tip.show(el.dataset.tip, ev));
    el.addEventListener("mousemove", (ev) => tip.move(ev));
    el.addEventListener("mouseleave", () => tip.hide());
    el.addEventListener("focus", () => { const r = el.getBoundingClientRect(); tip.show(el.dataset.tip, { clientX: r.right, clientY: r.top }); });
    el.addEventListener("blur", () => tip.hide());
  });
}

/* ---------- routing ---------- */
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  return { parts: path.split("/").filter(Boolean).map(decodeURIComponent), params: new URLSearchParams(query) };
}
function setQuery(view, state) {
  const params = new URLSearchParams();
  Object.entries(state).forEach(([k, v]) => { if (v !== "" && v != null) params.set(k, v); });
  const next = `#/${view}${params.toString() ? "?" + params : ""}`;
  if (next !== location.hash) history.replaceState(null, "", next);
}
function route() {
  tip.hide();
  const { parts, params } = parseHash();
  const view = parts[0] || "overview";
  $$(".nav a").forEach((a) => a.classList.toggle("on", a.dataset.nav === (view === "report" ? "reports" : view === "events" ? "connections" : view)));
  const main = $("#main");
  const views = { overview: viewOverview, reports: viewReports, incidents: viewIncidents, connections: window.viewConnections, events: window.viewConnections, entities: viewEntities, search: viewSearch, about: viewAbout };
  if (view === "report") return window.viewReport(main, parts[1], params);
  (views[view] || viewOverview)(main, params);
  document.title = view === "overview" ? SITE_TITLE : `${view === "events" ? "Connections" : cap(view)} | ${SITE_NAME}`;
  if (!params.toString()) window.scrollTo(0, 0);
}

/* ---------- charts ---------- */
// Horizontal bars: one series, value-sorted, direct value labels; each row is a link target.
function barChart(rows, opts = {}) {
  if (!rows.length) return `<p class="muted small">No data.</p>`;
  const W = 520, rowH = 26, labelW = opts.labelW || 190, valW = 48;
  const max = Math.max(...rows.map((r) => r.value), 1);
  const plot = W - labelW - valW;
  const body = rows.map((r, i) => {
    const y = i * rowH, w = Math.max(2, (r.value / max) * plot);
    const tipHtml = `<b>${esc(r.label)}</b>${fmt(r.value)} ${esc(opts.unit || "")}${r.note ? `<div class="muted">${esc(r.note)}</div>` : ""}`;
    return `<g class="row" ${r.href ? `data-href="${esc(r.href)}" tabindex="0" role="link"` : ""} data-tip="${esc(tipHtml)}" aria-label="${esc(r.label)}: ${r.value}">
      <rect class="hit" x="0" y="${y}" width="${W}" height="${rowH}"></rect>
      <text class="lbl" x="${labelW - 8}" y="${y + 17}" text-anchor="end">${esc(r.label.length > 30 ? r.label.slice(0, 29) + "…" : r.label)}</text>
      <path class="mark" fill="${r.color || "var(--s1)"}" d="M${labelW},${y + 6} h${Math.max(0, w - 4)} a4,4 0 0 1 4,4 v6 a4,4 0 0 1 -4,4 h-${Math.max(0, w - 4)} z"></path>
      <text class="val" x="${labelW + w + 6}" y="${y + 17}">${fmt(r.value)}</text></g>`;
  }).join("");
  return `<div class="chart"><svg viewBox="0 0 ${W} ${rows.length * rowH}" role="img" aria-label="${esc(opts.title || "Bar chart")}"><line class="axisl" x1="${labelW}" x2="${labelW}" y1="0" y2="${rows.length * rowH}"></line>${body}</svg></div>`;
}
// Stacked columns by year; series keep fixed colours; legend above; per-column tooltip.
function timeline(incidents) {
  const years = incidents.filter((i) => i.year).map((i) => i.year);
  if (!years.length) return `<p class="muted small">No dated incidents.</p>`;
  const series = ["identified", "probably-identified", "insufficient-information", "unidentified", "not-evaluated"];
  // A few early outliers (e.g. one 1800 account) would stretch the axis across decades of empty years:
  // start where the record becomes continuous (no gap over MAX_GAP years) and fold anything earlier into one "before" column.
  const MAX_GAP = 10, distinct = [...new Set(years)].sort((a, b) => a - b);
  let start = distinct[distinct.length - 1];
  for (let k = distinct.length - 1; k > 0 && distinct[k] - distinct[k - 1] <= MAX_GAP; k--) start = distinct[k - 1];
  const early = distinct[0] < start;
  const lo = early ? start - 1 : start, hi = Math.max(...years);
  const counts = new Map();
  incidents.forEach((i) => { if (!i.year) return; const yr = Math.max(i.year, lo); const c = counts.get(yr) || {}; c[i.status] = (c[i.status] || 0) + 1; counts.set(yr, c); });
  const span = hi - lo + 1, W = 1100, H = 230, padL = 36, padB = 24, padT = 8;
  const slot = (W - padL) / span, bw = Math.max(2, Math.min(24, slot - 2));
  const max = Math.max(...Array.from(counts.values()).map((c) => Object.values(c).reduce((a, b) => a + b, 0)));
  const ticks = niceTicks(max);
  const sy = (v) => H - padB - (v / ticks[ticks.length - 1]) * (H - padB - padT);
  let out = ticks.map((t) => `<line class="gridl" x1="${padL}" x2="${W}" y1="${sy(t)}" y2="${sy(t)}"></line><text x="${padL - 6}" y="${sy(t) + 4}" text-anchor="end">${t}</text>`).join("");
  const step = span > 60 ? 10 : span > 25 ? 5 : 1;
  for (let y = lo; y <= hi; y++) {
    const x = padL + (y - lo) * slot + (slot - bw) / 2;
    const before = early && y === lo;
    if (before) out += `<text x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">&lt;${start}</text>`;
    else if (y % step === 0 && !(early && y - lo < 3)) out += `<text x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">${y}</text>`;
    const c = counts.get(y);
    if (!c) continue;
    let acc = 0;
    const total = Object.values(c).reduce((a, b) => a + b, 0);
    const lines = series.filter((s) => c[s]).map((s) => `${esc(label("status", s))}: ${c[s]}`).join("<br>");
    let marks = "";
    series.forEach((s) => {
      if (!c[s]) return;
      const y1 = sy(acc + c[s]), y0 = sy(acc);
      marks += `<rect fill="${COLORS.status[s]}" x="${x}" y="${y1}" width="${bw}" height="${Math.max(1, y0 - y1 - (acc ? 1.5 : 0))}" rx="${acc + c[s] === total ? 2 : 0}"></rect>`;
      acc += c[s];
    });
    out += `<g class="row" data-href="${before ? `#/incidents?to=${start - 1}` : `#/incidents?from=${y}&to=${y}`}" tabindex="0" role="link" aria-label="${before ? `Before ${start}` : y}: ${total} incidents" data-tip="${esc(`<b>${before ? `Before ${start}` : y}</b>${total} incident${total === 1 ? "" : "s"}<div class="muted">${lines}</div>`)}">
      <rect class="hit" x="${padL + (y - lo) * slot}" y="${padT}" width="${slot}" height="${H - padB - padT}"></rect><g class="mark">${marks}</g></g>`;
  }
  out += `<line class="axisl" x1="${padL}" x2="${W}" y1="${H - padB}" y2="${H - padB}"></line>`;
  const legend = `<div class="legend">${series.map((s) => `<span><i style="background:${COLORS.status[s]}"></i>${esc(label("status", s))}</span>`).join("")}</div>`;
  return `${legend}<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Incidents per year by status in the record">${out}</svg></div>`;
}
function niceTicks(max) {
  const raw = max / 4, mag = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1))));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag;
  const ticks = []; for (let v = 0; v <= max + step - 1e-9; v += step) ticks.push(Math.round(v));
  return ticks.length > 1 ? ticks : [0, 1];
}
function bindChartLinks(root) {
  $$("[data-href]", root).forEach((el) => {
    const go = () => { location.hash = el.dataset.href; };
    el.addEventListener("click", go);
    el.addEventListener("keydown", (ev) => { if (ev.key === "Enter") go(); });
  });
}
const countBy = (items, fn) => { const m = new Map(); items.forEach((it) => [].concat(fn(it) ?? []).forEach((k) => { if (k !== "" && k != null) m.set(k, (m.get(k) || 0) + 1); })); return m; };
const topRows = (map, n, mk) => Array.from(map.entries()).sort((a, b) => b[1] - a[1]).slice(0, n).map(([k, v]) => mk(k, v));

/* ---------- map ---------- */
async function drawMap(host, incidents) {
  const pts = incidents.filter((i) => i.lat != null && i.lon != null);
  if (!App.land) { try { App.land = await (await fetch("assets/land.geojson")).json(); } catch (e) { App.land = { features: [] }; } }
  const W = 1000, H = 500, px = (lon) => ((lon + 180) / 360) * W, py = (lat) => ((90 - lat) / 180) * H;
  const ring = (r) => "M" + r.map((p) => `${px(p[0]).toFixed(1)},${py(p[1]).toFixed(1)}`).join("L") + "Z";
  const land = App.land.features.map((f) => { const g = f.geometry; const polys = g.type === "Polygon" ? [g.coordinates] : g.coordinates; return polys.map((p) => p.map(ring).join("")).join(""); }).join("");
  // group co-located points so a dot's size shows how many incidents share that spot
  const groups = new Map();
  pts.forEach((i) => { const k = `${i.lat.toFixed(1)},${i.lon.toFixed(1)}`; (groups.get(k) || groups.set(k, []).get(k)).push(i); });
  const dots = Array.from(groups.values()).sort((a, b) => b.length - a.length).map((g) => {
    const i = g[0], coarse = g.every((x) => x.place_precision === "country" || x.place_precision === "region");
    const names = g.slice(0, 4).map((x) => esc(x.title)).join("<br>") + (g.length > 4 ? `<br>… and ${g.length - 4} more` : "");
    const href = g.length === 1 ? `#/report/${encodeURIComponent(i.report)}?incident=${encodeURIComponent(i.id)}` : `#/incidents?near=${i.lat.toFixed(1)},${i.lon.toFixed(1)}`;
    const tipHtml = `<b>${esc(i.place || "Unnamed place")}</b>${g.length} incident${g.length === 1 ? "" : "s"} · ${coarse ? "approximate (region/country centroid)" : "placed at " + esc(i.place_precision) + " level"}<div class="muted">${names}</div>`;
    const r = (2.4 + Math.sqrt(g.length) * 1.3).toFixed(1);
    return `<circle class="dot${coarse ? " coarse" : ""}" cx="${px(i.lon).toFixed(1)}" cy="${py(i.lat).toFixed(1)}" r="${r}" data-r="${r}" data-href="${esc(href)}" tabindex="0" data-tip="${esc(tipHtml)}"></circle>`;
  }).join("");
  host.innerHTML = `<div class="map"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="World map of located incidents"><path class="land" d="${land}"></path>${dots}</svg>
    <div class="mapctl"><button class="btn small" data-z="1.6" aria-label="Zoom in">+</button><button class="btn small" data-z="0.625" aria-label="Zoom out">−</button><button class="btn small" data-z="0" aria-label="Reset map">⟲</button></div></div>
    <p class="muted small" style="margin-top:8px">${fmt(pts.length)} of ${fmt(incidents.length)} incidents could be placed. Filled dots are located to a city or a document-supported position; hollow rings are region or country centroids. Drag to pan; zoom with the buttons or Ctrl/⌘ + scroll.</p>`;
  const svg = $("svg", host); let vb = [0, 0, W, H];
  const apply = () => { svg.setAttribute("viewBox", vb.join(" ")); const k = vb[2] / W; $$(".dot", svg).forEach((d) => d.setAttribute("r", (+d.dataset.r * Math.max(k, 0.25)).toFixed(2))); };
  const zoom = (f, cx = vb[0] + vb[2] / 2, cy = vb[1] + vb[3] / 2) => {
    if (!f) { vb = [0, 0, W, H]; return apply(); }
    const w = Math.min(W, Math.max(W / 24, vb[2] / f)), h = w / 2;
    vb = [Math.min(W - w, Math.max(0, cx - (cx - vb[0]) * (w / vb[2]))), Math.min(H - h, Math.max(0, cy - (cy - vb[1]) * (h / vb[3]))), w, h]; apply();
  };
  $$("[data-z]", host).forEach((b) => b.addEventListener("click", () => zoom(+b.dataset.z)));
  const toSvg = (ev) => { const r = svg.getBoundingClientRect(); return [vb[0] + ((ev.clientX - r.left) / r.width) * vb[2], vb[1] + ((ev.clientY - r.top) / r.height) * vb[3]]; };
  svg.addEventListener("wheel", (ev) => { if (!ev.ctrlKey && !ev.metaKey) return; ev.preventDefault(); const [x, y] = toSvg(ev); zoom(ev.deltaY < 0 ? 1.3 : 1 / 1.3, x, y); }, { passive: false });
  let drag = null;
  svg.addEventListener("pointerdown", (ev) => { if (ev.target.classList.contains("dot")) return; drag = { x: ev.clientX, y: ev.clientY, vb: vb.slice() }; svg.classList.add("drag"); svg.setPointerCapture(ev.pointerId); });
  svg.addEventListener("pointermove", (ev) => { if (!drag) return; const r = svg.getBoundingClientRect(); vb[0] = Math.min(W - vb[2], Math.max(0, drag.vb[0] - ((ev.clientX - drag.x) / r.width) * vb[2])); vb[1] = Math.min(H - vb[3], Math.max(0, drag.vb[1] - ((ev.clientY - drag.y) / r.height) * vb[3])); apply(); });
  svg.addEventListener("pointerup", () => { drag = null; svg.classList.remove("drag"); });
  bindTips(host); bindChartLinks(host);
}

/* ---------- overview ---------- */
function viewOverview(main) {
  const ix = App.index, s = ix.stats, q = ix.quality, sum = ix.summary || {};
  const reports = ix.reports, incidents = ix.incidents;
  const title = (id) => { const r = App.reportById.get(id); return r ? `<a href="#/report/${encodeURIComponent(id)}">${esc(r.title)}</a>` : ""; };
  const sighting = reports.filter((r) => r.disposition && r.disposition !== "not-a-sighting-record").length;
  const dispRows = Object.keys(LABELS.disposition).map((k) => ({ label: label("disposition", k), value: reports.filter((r) => r.disposition === k).length, color: COLORS.disposition[k], href: `#/reports?disposition=${k}` })).filter((r) => r.value);
  const explRows = topRows(countBy(incidents, (i) => i.explanation), 12, (k, v) => ({ label: cap(k), value: v, href: `#/incidents?explanation=${k}` }));
  const shapeRows = topRows(countBy(incidents.filter((i) => i.shape !== "not-described"), (i) => i.shape), 10, (k, v) => ({ label: cap(k), value: v, href: `#/incidents?shape=${k}` }));
  const modeRows = topRows(countBy(incidents, (i) => i.modes), 12, (k, v) => ({ label: cap(k), value: v, href: `#/incidents?mode=${k}` }));
  const agencyRows = topRows(countBy(reports, (r) => r.agency), 12, (k, v) => ({ label: k, value: v, href: `#/reports?agency=${encodeURIComponent(k)}` }));
  const kindRows = topRows(countBy(reports, (r) => r.kind), 12, (k, v) => ({ label: cap(k), value: v, href: `#/reports?kind=${k}` }));
  const themeRows = topRows(countBy(reports, (r) => r.themes), 12, (k, v) => ({ label: k, value: v, href: `#/reports?theme=${encodeURIComponent(k)}` }));
  const bandRows = ["strong", "moderate", "limited", "minimal"].map((k) => ({ label: label("band", k), value: incidents.filter((i) => i.band === k).length, href: `#/incidents?band=${k}` }));
  const quotePct = q.quotes_total ? Math.round(((q.quotes_verified + q.quotes_approximate) / q.quotes_total) * 100) : null;
  const ocrTotal = Object.values(q.ocr_methods || {}).reduce((a, b) => a + b, 0);
  const modelTypes = (list) => [...new Set((Array.isArray(list) ? list : [list]).filter(Boolean).map((m) => String(m).replace(/^(remote|local) /, "")))].map((m) => m.charAt(0).toUpperCase() + m.slice(1)).join(", ") || "—";
  const methodName = { llm_ocr: "Vision-model OCR", native: "Born-digital text", tesseract: "Tesseract fallback", text_layer: "Embedded text layer fallback", failed: "Unreadable", publisher: "Publisher description", frames: "Frame analysis", captions: "Official captions", asr: "Machine transcript" };

  main.innerHTML = `
  <section class="hero">
    <div class="eyebrow">Independent analysis of public government records</div>
    <h1>THE DECLASSIFIED UFO FILES</h1>
    <p class="lead">Every released document, image, video and audio recording has been transcribed, analysed and cross-referenced. Start with the overview below, then open any file's deep report for page-cited incidents, provenance, official handling and an explanation assessment.</p>
    <div class="tiles">
      <div class="tile"><b>${fmt(s.reports)}</b><span>source files analysed</span></div>
      <div class="tile"><b>${fmt(s.incidents)}</b><span>incidents extracted</span></div>
      <div class="tile"><b>${fmt(s.pages)}</b><span>pages read</span></div>
      <div class="tile"><b>${fmt(s.media_minutes)}</b><span>minutes of audio/video</span></div>
      <div class="tile"><b>${fmt(ix.releases.length)}</b><span>releases, latest ${esc(dateLabel((ix.releases[ix.releases.length - 1] || {}).date))}</span></div>
    </div>
    <div class="cta"><a class="btn primary" href="#/reports">Browse the reports</a><a class="btn" href="#/incidents">Explore incidents</a><a class="btn" href="#/search">Search the full text</a></div>
    <p class="disclaimer"><b>Not a government website.</b> This is an independent project, not affiliated with or endorsed by the U.S. Department of War or any government agency. The source files come from the Department of War's public <a href="https://www.war.gov/ufo/" rel="noopener">PURSUE release</a>.</p>
  </section>
  ${ix.partial ? `<div class="warnbox"><b>Partial build.</b> This site was built from a subset of the corpus.</div>` : ""}
  ${ix.pending.length ? `<div class="warnbox"><b>${ix.pending.length} source file${ix.pending.length === 1 ? " is" : "s are"} not analysed yet</b> and ${ix.pending.length === 1 ? "is" : "are"} missing from the figures below. <a href="#/about">See data quality</a>.</div>` : ""}

  <div class="overview-split${sum.overview ? "" : " no-narrative"}">
  ${sum.overview ? `<section class="panel prose narrative"><div class="eyebrow">Corpus synthesis · machine-written from the per-file reports</div><h2>What the files show</h2>
     <div style="margin-top:10px">${sum.overview.split(/\n+/).map((p) => `<p>${esc(p)}</p>`).join("")}</div></section>` : ""}
  <div class="charts">
    <section class="panel"><h2>How the files resolve</h2><p class="sub">Analyst disposition per file (${fmt(sighting)} of ${fmt(reports.length)} files concern sightings).</p>${barChart(dispRows, { unit: "files", labelW: 200 })}</section>
    <section class="panel"><h2>Most plausible explanations</h2><p class="sub">Best-fit conventional explanation category per incident, on the evidence in the record.</p>${barChart(explRows, { unit: "incidents" })}</section>
    <section class="panel"><h2>How incidents were observed</h2><p class="sub">Observation modes named in the record; an incident can have several.</p>${barChart(modeRows, { unit: "incidents" })}</section>
    <section class="panel"><h2>Reported shapes</h2><p class="sub">Normalised from witness wording; undescribed objects omitted.</p>${barChart(shapeRows, { unit: "incidents" })}</section>
    <section class="panel"><h2>Documentation strength</h2><p class="sub">How well each incident is documented (sensors, witnesses, specificity, verifiable quotes) — not how anomalous it is.</p>${barChart(bandRows, { unit: "incidents" })}</section>
    <section class="panel"><h2>Releasing agencies</h2><p class="sub">Files by the agency credited in the release.</p>${barChart(agencyRows, { unit: "files" })}</section>
    <section class="panel"><h2>Kinds of record</h2><p class="sub">What each file is.</p>${barChart(kindRows, { unit: "files" })}</section>
    <section class="panel"><h2>Themes</h2><p class="sub">A file can carry several themes.</p>${barChart(themeRows, { unit: "files", labelW: 210 })}</section>
  </div>
  </div>

  <section class="panel"><h2>Incidents over time</h2><p class="sub">Each column is the number of extracted incidents dated to that year, split by the status the record itself gives. Select a column to list them.</p>${timeline(incidents)}</section>
  <section class="panel"><h2>Where incidents were reported</h2><p class="sub">Select a dot to open the incident. Position precision varies — see the note under the map.</p><div id="map"></div></section>

  ${sum.findings && sum.findings.length ? `<section class="panel"><h2>Cross-cutting findings</h2><p class="sub">Each finding links to the reports that support it.</p>
     ${sum.findings.map((f) => `<div class="finding"><h3>${esc(f.title)}</h3><p>${esc(f.text)}</p><div class="small">${f.report_ids.map(title).filter(Boolean).join(" · ")}</div></div>`).join("")}</section>` : ""}


  ${sum.notable && sum.notable.length ? `<section class="panel"><h2>Start here</h2><p class="sub">Files flagged as most worth reading first.</p><ul class="tight">${sum.notable.map((n) => `<li>${title(n.id)} — <span class="muted">${esc(n.why)}</span></li>`).join("")}</ul></section>` : ""}
  ${sum.caveats && sum.caveats.length ? `<section class="panel"><h2>Limits of this analysis</h2><ul class="tight">${sum.caveats.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></section>` : ""}

  <section class="panel"><h2>Data quality</h2><p class="sub">How the text was obtained and how far the machine output could be checked.</p>
    <div class="grid g3" style="margin-bottom:0">
      <div><p class="sub-h">Text extraction (${fmt(ocrTotal)} pages)</p>${barChart(Object.entries(q.ocr_methods || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ label: methodName[k] || k, value: v })), { unit: "pages", labelW: 200 })}</div>
      <div><p class="sub-h">Checks</p><dl class="kv">
        <dt>Quotes matched to page text</dt><dd>${quotePct == null ? "—" : quotePct + "%"} <span class="muted">(${fmt(q.quotes_verified)} exact, ${fmt(q.quotes_approximate)} approximate, ${fmt(q.quotes_unverified)} not found of ${fmt(q.quotes_total)}; quotes not found are hidden from the reports)</span></dd>
        <dt>Reports complete</dt><dd>${fmt(q.reports_complete)} of ${fmt(reports.length)}</dd>
        <dt>Incidents dated</dt><dd>${fmt(q.dated_incidents)} of ${fmt(incidents.length)}</dd>
        <dt>Incidents placed on the map</dt><dd>${fmt(q.located_incidents)} of ${fmt(incidents.length)}</dd>
        <dt>Sources awaiting analysis</dt><dd>${fmt(q.pending_sources)}</dd></dl></div>
      <div><p class="sub-h">Model types</p><dl class="kv"><dt>OCR and analysis</dt><dd>${esc(modelTypes(q.models.review))}</dd><dt>Audio</dt><dd>${esc(modelTypes(q.models.audio))}</dd><dt>Embeddings</dt><dd>${esc(modelTypes(q.models.embedding))}</dd></dl></div>
    </div>
    ${q.issues.length ? `<details class="more"><summary>${q.issues.length} report${q.issues.length === 1 ? "" : "s"} with known gaps</summary><ul class="tight">${q.issues.map((i) => `<li><a href="#/report/${encodeURIComponent(i.id)}">${esc(i.title)}</a> — <span class="muted">${esc(i.problems.join("; "))}</span></li>`).join("")}</ul></details>` : ""}
  </section>`;
  drawMap($("#map", main), incidents);
  bindTips(main); bindChartLinks(main);
}

/* ---------- generic filtered list ---------- */
function selectHtml(name, labelText, options, value) {
  return `<label>${esc(labelText)}<select name="${name}"><option value="">All</option>${options.map(([v, l]) => `<option value="${esc(v)}"${String(v) === String(value) ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></label>`;
}
const optionsFrom = (map, labeller = (k) => k) => Array.from(map.entries()).sort((a, b) => b[1] - a[1]).map(([k, v]) => [k, `${labeller(k)} (${v})`]);
const matchesText = (hay, q) => q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w));

function viewReports(main, params) {
  const all = App.index.reports;
  const state = Object.fromEntries(["q", "media", "agency", "kind", "disposition", "release", "decade", "redaction", "theme", "country", "status", "sort"].map((k) => [k, params.get(k) || ""]));
  let shown = 30;
  const decade = (r) => (r.year ? Math.floor(r.year / 10) * 10 : "");
  main.innerHTML = `<h1>Reports</h1><p class="muted" style="margin:6px 0 16px">One deep analysis report per released file. Filter, then open a report.</p>
  <form class="filters" id="f">
    <label>Search titles and summaries<input type="search" name="q" value="${esc(state.q)}" placeholder="e.g. Roswell, radar, Kabul"></label>
    ${selectHtml("media", "Media", optionsFrom(countBy(all, (r) => r.media), (k) => label("media", k)), state.media)}
    ${selectHtml("agency", "Agency", optionsFrom(countBy(all, (r) => r.agency)), state.agency)}
    ${selectHtml("kind", "Record kind", optionsFrom(countBy(all, (r) => r.kind), cap), state.kind)}
    ${selectHtml("disposition", "Disposition", optionsFrom(countBy(all, (r) => r.disposition), (k) => label("disposition", k)), state.disposition)}
    ${selectHtml("decade", "Decade", Array.from(countBy(all, decade).entries()).sort((a, b) => a[0] - b[0]).map(([k, v]) => [k, `${k}s (${v})`]), state.decade)}
    ${selectHtml("release", "Release", App.index.releases.map((r) => [r.date, `${dateLabel(r.date)} (${r.count})`]), state.release)}
    ${selectHtml("theme", "Theme", optionsFrom(countBy(all, (r) => r.themes)), state.theme)}
    ${selectHtml("redaction", "Redaction", optionsFrom(countBy(all, (r) => r.redaction), cap), state.redaction)}
    ${selectHtml("country", "Country", optionsFrom(countBy(all, (r) => r.countries)), state.country)}
    <label>Sort<select name="sort">${[["", "Research value"], ["year", "Year (oldest first)"], ["yeard", "Year (newest first)"], ["release", "Newest release"], ["incidents", "Most incidents"], ["score", "Best documented"], ["title", "Title"]].map(([v, l]) => `<option value="${v}"${v === state.sort ? " selected" : ""}>${l}</option>`).join("")}</select></label>
    <button type="reset" class="btn">Clear</button>
  </form><div class="resultbar"><span id="count"></span></div><div class="cards" id="list"></div><p style="text-align:center;margin-top:16px"><button class="btn" id="more">Show more</button></p>`;
  const hay = new Map(all.map((r) => [r.id, `${r.title} ${r.pub_title} ${r.file} ${r.snippet} ${r.doc_type} ${r.themes.join(" ")}`.toLowerCase()]));
  const sorters = {
    "": (a, b) => (b.value || 0) - (a.value || 0) || (b.doc_score || 0) - (a.doc_score || 0) || a.title.localeCompare(b.title),
    year: (a, b) => (a.year || 9999) - (b.year || 9999), yeard: (a, b) => (b.year || 0) - (a.year || 0), release: (a, b) => b.release.localeCompare(a.release) || a.title.localeCompare(b.title),
    incidents: (a, b) => b.incidents - a.incidents, score: (a, b) => (b.doc_score ?? -1) - (a.doc_score ?? -1), title: (a, b) => a.title.localeCompare(b.title),
  };
  function render() {
    const rows = all.filter((r) => (!state.q || matchesText(hay.get(r.id), state.q)) && (!state.media || r.media === state.media) && (!state.agency || r.agency === state.agency)
      && (!state.kind || r.kind === state.kind) && (!state.disposition || r.disposition === state.disposition) && (!state.release || r.release === state.release)
      && (!state.decade || String(decade(r)) === state.decade) && (!state.redaction || r.redaction === state.redaction) && (!state.theme || r.themes.includes(state.theme))
      && (!state.country || r.countries.includes(state.country)) && (!state.status || r.status === state.status)).sort(sorters[state.sort] || sorters[""]);
    $("#count").textContent = `${fmt(rows.length)} of ${fmt(all.length)} reports`;
    $("#list").innerHTML = rows.slice(0, shown).map(reportCard).join("") || `<p class="empty">No reports match these filters.</p>`;
    $("#more").hidden = rows.length <= shown;
    setQuery("reports", state);
  }
  const form = $("#f");
  form.addEventListener("input", () => { new FormData(form).forEach((v, k) => { state[k] = v; }); shown = 30; render(); });
  form.addEventListener("reset", () => { Object.keys(state).forEach((k) => { state[k] = ""; }); setTimeout(() => { shown = 30; render(); }); });
  form.addEventListener("submit", (ev) => ev.preventDefault());
  $("#more").addEventListener("click", () => { shown += 30; render(); });
  render();
}
function reportCard(r) {
  const size = r.pages ? `${fmt(r.pages)} page${r.pages === 1 ? "" : "s"}` : r.duration ? `${Math.floor(r.duration / 60)}:${String(Math.round(r.duration % 60)).padStart(2, "0")}` : "";
  return `<a class="card" href="#/report/${encodeURIComponent(r.id)}">
    ${r.thumb ? `<img src="${esc(r.thumb)}" alt="" loading="lazy">` : ""}
    <div class="meta"><span>${esc(label("media", r.media))}</span><span>${esc(r.agency)}</span>${r.year ? `<span>${r.year}</span>` : ""}${size ? `<span>${size}</span>` : ""}${r.incidents ? `<span>${r.incidents} incident${r.incidents === 1 ? "" : "s"}</span>` : ""}</div>
    <h3>${esc(r.title)}</h3><p>${esc(r.snippet)}${r.snippet.length >= 280 ? "…" : ""}</p>
    <div class="chips">${badge("disposition", r.disposition)}${r.value ? `<span class="chip gold" title="Research value 1–5">Value ${r.value}/5</span>` : ""}${r.status !== "complete" ? `<span class="chip" style="border-color:var(--warn);color:var(--warn)">Incomplete</span>` : ""}</div></a>`;
}

/* ---------- incidents ---------- */
function viewIncidents(main, params) {
  const all = App.index.incidents;
  const keys = ["q", "from", "to", "country", "shape", "mode", "status", "hynek", "explanation", "band", "witness", "agency", "near", "sort", "dir"];
  const state = Object.fromEntries(keys.map((k) => [k, params.get(k) || ""]));
  let shown = 100;
  main.innerHTML = `<h1>Incidents</h1><p class="muted" style="margin:6px 0 16px">Every specific observation event extracted from the files. Select a row to open it inside its source report.</p>
  <form class="filters" id="f">
    <label>Search<input type="search" name="q" value="${esc(state.q)}" placeholder="place, object, keyword"></label>
    <label>Year from<input type="number" name="from" value="${esc(state.from)}" min="1800" max="2100"></label>
    <label>Year to<input type="number" name="to" value="${esc(state.to)}" min="1800" max="2100"></label>
    ${selectHtml("country", "Country", optionsFrom(countBy(all, (i) => i.country)), state.country)}
    ${selectHtml("shape", "Shape", optionsFrom(countBy(all, (i) => i.shape), cap), state.shape)}
    ${selectHtml("mode", "Observed by", optionsFrom(countBy(all, (i) => i.modes), cap), state.mode)}
    ${selectHtml("witness", "Witness type", optionsFrom(countBy(all, (i) => i.witnesses), cap), state.witness)}
    ${selectHtml("status", "Status in record", optionsFrom(countBy(all, (i) => i.status), (k) => label("status", k)), state.status)}
    ${selectHtml("explanation", "Best explanation", optionsFrom(countBy(all, (i) => i.explanation), cap), state.explanation)}
    ${selectHtml("hynek", "Hynek class", optionsFrom(countBy(all, (i) => i.hynek), (k) => label("hynek", k)), state.hynek)}
    ${selectHtml("band", "Documentation", optionsFrom(countBy(all, (i) => i.band), (k) => label("band", k)), state.band)}
    ${selectHtml("agency", "Agency", optionsFrom(countBy(all, (i) => i.agency)), state.agency)}
    <button type="reset" class="btn">Clear</button>
  </form>
  <div class="resultbar"><span id="count"></span><span><button class="btn small" id="csv">Download CSV</button> <button class="btn small" id="json">Download JSON</button></span></div>
  <div class="tablewrap"><table><thead><tr>${[["date", "Date"], ["title", "Incident"], ["place", "Place"], ["shape", "Shape"], ["status", "Status in record"], ["explanation", "Best explanation"], ["strangeness", "Strangeness"], ["score", "Documentation"]]
    .map(([k, l]) => `<th${k === "score" || k === "strangeness" ? ' class="num"' : ""}><button data-sort="${k}">${l}</button></th>`).join("")}</tr></thead><tbody id="rows"></tbody></table></div>
  <p style="text-align:center;margin-top:16px"><button class="btn" id="more">Show more</button></p>`;
  const hay = new Map(all.map((i) => [i.id, `${i.title} ${i.place} ${i.snippet} ${i.shape} ${i.date_text}`.toLowerCase()]));
  let current = [];
  function render() {
    const near = state.near ? state.near.split(",").map(Number) : null;
    current = all.filter((i) => (!state.q || matchesText(hay.get(i.id), state.q)) && (!state.from || (i.year && i.year >= +state.from)) && (!state.to || (i.year && i.year <= +state.to))
      && (!state.country || i.country === state.country) && (!state.shape || i.shape === state.shape) && (!state.mode || i.modes.includes(state.mode)) && (!state.witness || i.witnesses.includes(state.witness))
      && (!state.status || i.status === state.status) && (!state.hynek || i.hynek === state.hynek) && (!state.explanation || i.explanation === state.explanation) && (!state.band || i.band === state.band)
      && (!state.agency || i.agency === state.agency) && (!near || (i.lat != null && i.lat.toFixed(1) == near[0].toFixed(1) && i.lon.toFixed(1) == near[1].toFixed(1))));
    const key = state.sort || "date", dir = state.dir === "desc" ? -1 : 1;
    current.sort((a, b) => { const x = a[key] ?? "", y = b[key] ?? ""; if (x === "" && y !== "") return 1; if (y === "" && x !== "") return -1; return (typeof x === "number" ? x - y : String(x).localeCompare(String(y))) * dir; });
    $("#count").textContent = `${fmt(current.length)} of ${fmt(all.length)} incidents${near ? " at the selected map position" : ""}`;
    $("#rows").innerHTML = current.slice(0, shown).map((i) => `<tr class="click" data-href="#/report/${encodeURIComponent(i.report)}?incident=${encodeURIComponent(i.id)}" tabindex="0">
      <td style="white-space:nowrap">${esc(dateLabel(i.date) || i.date_text || "undated")}</td><td><b>${esc(i.title)}</b><div class="muted small">${esc(i.snippet)}${i.snippet.length >= 240 ? "…" : ""}</div></td>
      <td>${esc(i.place || "—")}${i.place_precision === "country" || i.place_precision === "region" ? ' <span class="dim small">(approx.)</span>' : ""}</td><td>${esc(i.shape === "not-described" ? "—" : cap(i.shape))}</td>
      <td>${badge("status", i.status)}</td><td>${esc(cap(i.explanation))}${i.likelihood ? `<div class="muted small">${esc(i.likelihood)}</div>` : ""}</td><td class="num">${i.strangeness}/5</td><td class="num">${i.score}<div class="muted small">${esc(label("band", i.band))}</div></td></tr>`).join("")
      || `<tr><td colspan="8" class="empty">No incidents match these filters.</td></tr>`;
    $("#more").hidden = current.length <= shown;
    bindChartLinks($("#rows")); setQuery("incidents", state);
  }
  const form = $("#f");
  form.addEventListener("input", () => { new FormData(form).forEach((v, k) => { state[k] = v; }); shown = 100; render(); });
  form.addEventListener("reset", () => { keys.forEach((k) => { state[k] = ""; }); setTimeout(render); });
  form.addEventListener("submit", (ev) => ev.preventDefault());
  $$("[data-sort]", main).forEach((b) => b.addEventListener("click", () => { state.dir = state.sort === b.dataset.sort && state.dir !== "desc" ? "desc" : ""; state.sort = b.dataset.sort; render(); }));
  $("#more").addEventListener("click", () => { shown += 200; render(); });
  $("#json").addEventListener("click", () => download("uap-incidents.json", JSON.stringify(current, null, 1), "application/json"));
  $("#csv").addEventListener("click", () => {
    const cols = ["id", "report", "title", "date", "date_text", "date_precision", "place", "lat", "lon", "place_precision", "country", "shape", "modes", "witnesses", "witness_count", "status", "hynek", "explanation", "likelihood", "strangeness", "score", "band", "agency", "media", "snippet"];
    const cell = (v) => `"${String(Array.isArray(v) ? v.join("; ") : v ?? "").replace(/"/g, '""')}"`;
    download("uap-incidents.csv", [cols.join(",")].concat(current.map((i) => cols.map((c) => cell(i[c])).join(","))).join("\n"), "text/csv");
  });
  render();
}
function download(name, text, type) {
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type })); a.download = name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/* ---------- entities ---------- */
function viewEntities(main, params) {
  const ent = App.index.entities;
  const tabs = [["people", "People"], ["organizations", "Organizations"], ["programs", "Programs and projects"], ["equipment", "Aircraft and equipment"]];
  const state = { tab: params.get("tab") || "people", q: params.get("q") || "", open: params.get("open") || "" };
  main.innerHTML = `<h1>People and organizations</h1><p class="muted" style="margin:6px 0 16px">Names as they appear in the records, with the reports that mention them. Spelling follows the documents; the same person may appear under more than one form.</p>
    <div class="tabs">${tabs.map(([k, l]) => `<button data-tab="${k}">${l} (${fmt(ent[k].length)})</button>`).join("")}</div>
    <form class="filters" id="f"><label>Filter names<input type="search" name="q" value="${esc(state.q)}"></label></form><div id="out"></div>`;
  function render() {
    $$("[data-tab]", main).forEach((b) => b.classList.toggle("on", b.dataset.tab === state.tab));
    const rows = ent[state.tab].filter((e) => !state.q || matchesText(`${e.name} ${e.role || ""} ${e.org || ""}`.toLowerCase(), state.q)).slice(0, 400);
    $("#out").innerHTML = `<div class="tablewrap"><table><thead><tr><th>Name</th>${state.tab === "people" ? "<th>Role / affiliation</th>" : ""}<th class="num">Reports</th><th>Mentioned in</th></tr></thead><tbody>${rows.map((e) => `<tr>
      <td><b>${esc(e.name)}</b></td>${state.tab === "people" ? `<td>${esc([e.role, e.org].filter(Boolean).join(" — "))}</td>` : ""}<td class="num">${e.reports.length}</td>
      <td class="small">${e.reports.slice(0, state.open === e.name ? 999 : 4).map((id) => { const r = App.reportById.get(id); return r ? `<a href="#/report/${encodeURIComponent(id)}">${esc(r.title)}</a>` : ""; }).filter(Boolean).join(" · ")}${e.reports.length > 4 && state.open !== e.name ? ` · <a href="#" data-open="${esc(e.name)}">+${e.reports.length - 4} more</a>` : ""}</td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">Nothing matches.</td></tr>`}</tbody></table></div>`;
    $$("[data-open]", main).forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); state.open = a.dataset.open; render(); }));
    setQuery("entities", { tab: state.tab === "people" ? "" : state.tab, q: state.q });
  }
  $$("[data-tab]", main).forEach((b) => b.addEventListener("click", () => { state.tab = b.dataset.tab; render(); }));
  $("#f").addEventListener("input", (ev) => { state.q = ev.target.value; render(); });
  $("#f").addEventListener("submit", (ev) => ev.preventDefault());
  render();
}

/* ---------- full-text search ---------- */
async function loadShards(progress) {
  if (App.shards) return App.shards;
  const out = [], list = App.index.search_shards || [];
  for (let n = 0; n < list.length; n++) { progress(n, list.length); out.push(...(await (await fetch(list[n])).json())); }
  App.shards = out; return out;
}
function viewSearch(main, params) {
  const state = { q: params.get("q") || "" };
  main.innerHTML = `<h1>Full-text search</h1><p class="muted" style="margin:6px 0 16px">Searches the extracted text of every page and transcript (${fmt(App.index.stats.pages)} pages). The text is downloaded on first use. Use quotes for an exact phrase.</p>
    <form class="filters" id="f"><label>Search the page text<input type="search" name="q" value="${esc(state.q)}" placeholder='e.g. "green fireball" or Kirtland' style="min-width:340px"></label><button class="btn primary">Search</button></form><div id="out"></div>`;
  async function run() {
    const out = $("#out"); const q = state.q.trim(); setQuery("search", state);
    if (q.length < 3) { out.innerHTML = `<p class="empty">Enter at least three characters.</p>`; return; }
    let pages;
    try { pages = await loadShards((n, t) => { out.innerHTML = `<p class="loading">Loading page text… ${n}/${t}</p>`; }); }
    catch (e) { out.innerHTML = `<p class="empty">The page text could not be loaded (${esc(e.message)}).</p>`; return; }
    const phrases = []; const rest = q.replace(/"([^"]+)"/g, (_, p) => { phrases.push(p.toLowerCase()); return " "; });
    const terms = phrases.concat(rest.toLowerCase().split(/\s+/).filter((w) => w.length > 1));
    const hits = [];
    for (const p of pages) {
      const low = p.t.toLowerCase(); let score = 0, first = -1;
      for (const term of terms) { const at = low.indexOf(term); if (at < 0) { score = 0; break; } if (first < 0 || at < first) first = at; let c = 0, i = at; while (i >= 0 && c < 20) { c++; i = low.indexOf(term, i + term.length); } score += c; }
      if (score) hits.push({ p, score, first });
    }
    hits.sort((a, b) => b.score - a.score);
    const reports = new Set(hits.map((h) => h.p.r));
    const re = new RegExp("(" + terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
    out.innerHTML = `<div class="resultbar"><span>${fmt(hits.length)} page${hits.length === 1 ? "" : "s"} in ${fmt(reports.size)} report${reports.size === 1 ? "" : "s"}${hits.length > 200 ? " — showing the best 200" : ""}</span></div>
      <div class="panel hitlist" style="padding:0">${hits.slice(0, 200).map(({ p, first }) => {
        const r = App.reportById.get(p.r); const start = Math.max(0, first - 110); const snip = (start ? "…" : "") + p.t.slice(start, first + 230).replace(/\s+/g, " ") + "…";
        return `<a href="#/report/${encodeURIComponent(p.r)}?page=${p.p}&find=${encodeURIComponent(terms[0])}"><b>${esc(r ? r.title : p.r)}</b> <span class="muted small">· ${esc(p.l)}</span><div class="snip">${esc(snip).replace(re, "<mark>$1</mark>")}</div></a>`;
      }).join("") || `<p class="empty">No page contains all of those terms.</p>`}</div>`;
  }
  $("#f").addEventListener("submit", (ev) => { ev.preventDefault(); state.q = new FormData(ev.target).get("q"); run(); });
  if (state.q) run();
}

/* ---------- methods ---------- */
function viewAbout(main) {
  const q = App.index.quality;
  main.innerHTML = `<h1>Methods and limits</h1>
  <section class="panel prose" style="margin-top:16px"><h2>How this was built</h2>
    <p><b>1. Collection.</b> Files are downloaded from the Department of War PURSUE release manifest and checked for completeness (size, file signature, checksum). ${fmt(App.index.stats.reports)} files are analysed here${q.pending_sources ? `; ${q.pending_sources} more are awaiting analysis` : ""}.</p>
    <p><b>2. Text extraction.</b> Every scanned page is transcribed by a vision language model, which also notes handwriting, redactions, markings and pictures. Embedded text layers in scans are not trusted. If the primary model fails, a second local model is tried, then Tesseract OCR, then the embedded text layer; the path used is recorded per page. Video is sampled into frames that are described by the model; audio is transcribed in full, with official captions used where the publisher provides them.</p>
    <p><b>3. Analysis.</b> The whole text of each file is read in page-aligned parts — nothing is sampled. Each part is mined for constituent records, incidents, people, organizations and key passages, then a file-level report is written. Quoted passages are checked against the extracted page text and labelled exact or approximate; quotes that cannot be found in the page text are not shown.</p>
    <p><b>4. Normalisation.</b> Dates are parsed conservatively (impossible dates are dropped, not guessed). Places are matched to a gazetteer and carry a precision level. Related reports and repeated accounts of the same event are linked with text embeddings.</p>
    <p><b>5. Connections.</b> Incidents are connected only by explicit factors, each stated on the link: same time and place (within a day and 150 km), matching description (same shape and colour within a week and 800 km), the same account in another file (a text match whose dates and places agree), a named person, or a mission or vessel. Text similarity alone never connects two incidents.</p></section>
  <section class="panel"><h2>Scales used in the reports</h2><dl class="kv">
    <dt>Disposition</dt><dd>The analyst's reading of the file: explained in the record, likely explained, insufficient information, unexplained in the record, or not a sighting record. "Unexplained in the record" means the file offers no resolution — not that the event is anomalous.</dd>
    <dt>Status in record</dt><dd>What the record itself says about an incident, kept separate from the analyst assessment.</dd>
    <dt>Likelihood wording</dt><dd>Almost no chance (1–5%), very unlikely (5–20%), unlikely (20–45%), roughly even chance (45–55%), likely (55–80%), very likely (80–95%), almost certain (95–99%) — the intelligence-community estimative scale.</dd>
    <dt>Hynek class</dt><dd>Nocturnal light, daylight disc, radar-visual, and close encounters of the first (within about 150 m), second (physical effects) and third (occupants) kind.</dd>
    <dt>Vallée class and SVP</dt><dd>Type AN (anomaly), FB (fly-by), MA (manoeuvre) or CE (close encounter), each 1–5. SVP rates source reliability, site visit and possible explanations 0–4 each; a higher last digit means fewer natural explanations fit.</dd>
    <dt>Admiralty code</dt><dd>Source reliability A (completely reliable) to F (cannot be judged); information credibility 1 (confirmed) to 6 (cannot be judged). The two are rated separately.</dd>
    <dt>Strangeness</dt><dd>1–5: how hard the incident <em>as reported</em> is to reconcile with known phenomena. It does not measure how reliable the report is.</dd>
    <dt>Documentation strength</dt><dd>0–100, computed (not model-judged) from four 25-point components: sensing (instrument data, visual plus instrument), witnesses (number, trained observers, first-hand), specificity (date, time, place, duration, description) and documentation (verified quotes, official action, instrument measurements, physical evidence). It measures how well an incident is documented, not how unusual it is.</dd>
    <dt>Claimed characteristics</dt><dd>The so-called "five observables" are a popular framing, not an official standard. They are listed only when the record itself makes the claim, with whether it rests on a witness estimate or an instrument.</dd></dl></section>
  <section class="panel prose"><h2>Limits</h2><ul class="tight">
    <li>Everything here is a machine-generated research aid. Check the cited page of the original file before quoting or relying on a claim.</li>
    <li>Transcription of poor scans, handwriting and teletype can be wrong; redacted text is never guessed.</li>
    <li>Sizes, speeds and altitudes in the records are usually witness estimates.</li>
    <li>Map positions are often approximate, and may mark the observer rather than the object.</li>
    <li>Counts depend on how files bundle or repeat incidents; the same event can appear in several files.</li></ul></section>
  ${App.index.pending.length ? `<section class="panel"><h2>Awaiting analysis</h2><ul class="tight">${App.index.pending.map((p) => `<li>${esc(p.title)} <span class="muted">(${esc(p.media)}, ${esc(p.agency)})</span></li>`).join("")}</ul></section>` : ""}`;
}

/* ---------- boot ---------- */
async function boot() {
  tip.el = $("#tip");
  try {
    const res = await fetch("data/index.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    App.index = await res.json();
  } catch (err) {
    $("#main").innerHTML = `<p class="empty">The corpus index could not be loaded (${esc(err.message)}).<br>If you opened this file directly, serve the folder instead: <code>python3 -m http.server 8000 --directory dashboard</code></p>`;
    return;
  }
  App.index.reports.forEach((r) => App.reportById.set(r.id, r));
  App.index.incidents.forEach((i) => { (App.incidentsByReport.get(i.report) || App.incidentsByReport.set(i.report, []).get(i.report)).push(i); });
  $("#generated").textContent = `Data built ${dateLabel(App.index.generated.slice(0, 10))}.`;
  window.addEventListener("hashchange", route);
  route();
}
Object.assign(window, { App, $, $$, esc, fmt, cap, label, badge, dateLabel, bindTips, bindChartLinks, download, LABELS, COLORS });
boot();
