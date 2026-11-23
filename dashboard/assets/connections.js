/* Connections: incidents linked by explicit factors (time and place, description, the same account in another file,
   a named person, a mission), drawn as a rotatable 3D network on a canvas. No libraries.
   Data: data/connections.json (built by ufo_pipeline/connections.py; positions are precomputed). */
"use strict";
(() => {
  const ORDER = ["time_place", "description", "same_event", "person", "mission"];
  const COLOR_VAR = { time_place: "--s1", description: "--s2", same_event: "--s3", person: "--s4", mission: "--s5" };
  const HUB_KIND = { person: "Named person", mission: "Mission or vessel" };
  let G = null; // loaded graph, shared across visits

  async function load() {
    if (G) return G;
    const g = await (await fetch((App.index.connections || {}).file || "data/connections.json")).json();
    const incById = new Map(App.index.incidents.map((i) => [i.id, i]));
    g.nodes.forEach((n, k) => {
      n.k = k; n.links = []; n.inc = n.kind === "incident" ? incById.get(n.id) : null;
      n.title = n.inc ? n.inc.title : n.label;
    });
    g.links.forEach((l, k) => { l.k = k; l.main = ORDER.find((f) => l.f.includes(f)); g.nodes[l.s].links.push(l); g.nodes[l.t].links.push(l); });
    g.nodes.forEach((n) => { n.r = n.kind === "incident" ? 2.6 : 2.4 + Math.sqrt(n.links.length) * 0.9; });
    const sizes = countBy(g.nodes, (n) => n.g);
    g.nodes.forEach((n) => { n.groupSize = sizes.get(n.g) || 1; });
    g.byId = new Map(g.nodes.map((n) => [n.id, n]));
    g.clusterById = new Map();
    g.clusters.forEach((c) => {
      c.nodes = c.members.map((k) => g.nodes[k]);
      const incs = c.nodes.map((n) => n.inc).filter(Boolean);
      c.lead = incs.slice().sort((a, b) => (b.date_precision === "day") - (a.date_precision === "day") || b.score - a.score)[0] || {};
      const dates = incs.map((i) => i.date).filter(Boolean).sort();
      c.first = dates[0] || ""; c.last = dates[dates.length - 1] || "";
      c.files = new Set(incs.map((i) => i.report)).size;
      c.hay = incs.map((i) => `${i.title} ${i.place}`).join(" ").toLowerCase();
      c.centre = [0, 1, 2].map((d) => c.nodes.reduce((s, n) => s + n.p[d], 0) / c.nodes.length);
      g.clusterById.set(c.id, c);
    });
    g.nodes.forEach((n) => { n.hay = `${n.title || ""} ${n.inc ? n.inc.place + " " + ((App.reportById.get(n.inc.report) || {}).title || "") : ""}`.toLowerCase(); });
    G = g;
    return g;
  }

  const span = (a, b) => (a && b && a !== b ? `${dateLabel(a)} – ${dateLabel(b)}` : dateLabel(a) || "Undated");
  const fileOf = (inc) => App.reportById.get(inc.report) || { title: inc.report, id: inc.report };
  const chip = (f, n) => `<span class="fchip"><i style="background:var(${COLOR_VAR[f]})"></i>${esc(G.factors[f].label)}${n != null ? ` <b>${fmt(n)}</b>` : ""}</span>`;

  window.viewConnections = async function (main, params) {
    window.scrollTo(0, 0);
    main.innerHTML = `<h1>Connections</h1><p class="loading">Loading the connection graph…</p>`;
    try { await load(); } catch (e) { main.innerHTML = `<h1>Connections</h1><p class="empty">The connection data could not be loaded (${esc(e.message)}).</p>`; return; }
    if (!main.isConnected || !location.hash.startsWith("#/connections") && !location.hash.startsWith("#/events")) return;
    const st = G.stats;
    const state = { min: params.get("min") || "3", q: params.get("q") || "", off:new Set((params.get("off") || "").split(",").filter((f) => ORDER.includes(f))), sel: params.get("sel") || "" };
    let shown = 30;

    main.innerHTML = `<h1>Connections</h1>
    <p class="muted" style="margin:6px 0 16px;max-width:860px">${fmt(st.incidents)} incidents are connected to at least one other by an explicit factor, forming ${fmt(st.clusters)} clusters of related events.
      Every link states why it exists. Larger coloured nodes are hubs: a person named in several accounts, or a mission the observations were made from.</p>
    <form class="filters" id="f" autocomplete="off">
      <label>Find an incident, person or mission<input type="search" name="q" value="${esc(state.q)}" placeholder="e.g. Arnold, Gemini 5, Washington"></label>
      ${selectHtml("min", "Show groups of", [["2", "2 or more"], ["3", "3 or more"], ["10", "10 or more"]], state.min).replace('<option value="">All</option>', "")}
      <fieldset class="factors"><legend>Linked by</legend>${ORDER.map((f) => `<label class="ftog" title="${esc(G.factors[f].detail)}"><input type="checkbox" name="factor" value="${f}"${state.off.has(f) ? "" : " checked"}><i style="background:var(${COLOR_VAR[f]})"></i>${esc(G.factors[f].label)} <span class="muted">${fmt(st.by_factor[f] || 0)}</span></label>`).join("")}</fieldset>
    </form>
    <div class="netgrid">
      <section class="panel netpanel">
        <div class="net3d" id="stage"><canvas role="img" aria-label="Rotatable 3D network of ${fmt(st.incidents)} connected incidents and ${fmt(st.hubs)} people and missions. The cluster table below lists the same groups."></canvas>
          <div class="mapctl"><button class="btn small" data-z="1.4" aria-label="Zoom in">+</button><button class="btn small" data-z="0.7" aria-label="Zoom out">−</button><button class="btn small" data-z="0" aria-label="Show everything">⟲</button><button class="btn small" id="spin" aria-pressed="true" aria-label="Pause rotation">❚❚</button></div>
        </div>
        <p class="muted small" style="margin:8px 0 0">Drag to rotate, Shift + drag to pan, Ctrl/⌘ + scroll or the buttons to zoom. Select a node to see its connections.</p>
      </section>
      <aside class="panel netside" id="side" aria-live="polite"></aside>
    </div>
    <section class="panel"><h2>Clusters of related events</h2>
      <p class="sub">Groups joined by time and place, matching descriptions or the same account in another file. People and missions connect clusters but never merge them.</p>
      <div class="tablewrap"><table><thead><tr><th>Cluster</th><th>Dates</th><th class="num">Incidents</th><th class="num">Files</th><th>Linked by</th></tr></thead><tbody id="rows"></tbody></table></div>
      <p style="text-align:center;margin-top:14px"><button class="btn" id="more">Show more</button></p>
    </section>
    <section class="panel prose"><h2>How connections are made</h2><dl class="kv small">
      ${ORDER.map((f) => `<dt>${chip(f)}</dt><dd>${esc(G.factors[f].detail)}.</dd>`).join("")}</dl>
      <p class="small muted" style="margin:12px 0 0">Positions must be at least city-level for time-and-place and description links; region and country centroids are never compared.
      A connection is a lead, not a conclusion: open both accounts before relying on it. Text matches between files count only when the dates and places agree.</p></section>`;

    /* ---------- 3D stage ---------- */
    const stage = $("#stage"), canvas = $("canvas", stage), ctx = canvas.getContext("2d"), side = $("#side");
    const css = getComputedStyle(document.documentElement);
    const color = Object.fromEntries(ORDER.map((f) => [f, css.getPropertyValue(COLOR_VAR[f]).trim()]));
    const INK = css.getPropertyValue("--ink-2").trim() || "#c9c4b6", GOLD = css.getPropertyValue("--gold").trim() || "#d6a84f";
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const R = Math.max(...G.nodes.map((n) => Math.hypot(...n.p))) || 100;
    const FOV = 0.9;
    const home = { yaw: 0.6, pitch: -0.35, dist: R / Math.sin(FOV / 2) * 0.82, target: [0, 0, 0] };
    const cam = { ...home, target: home.target.slice() };
    let W = 0, H = 0, dpr = 1, f = 1, proj = null, hover = null, spin = !reduce, dirty = true, fly = null, lit = null;

    function resize() {
      const r = canvas.getBoundingClientRect(); dpr = Math.min(devicePixelRatio || 1, 2);
      W = r.width; H = r.height; canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
      f = Math.min(W, H) / 2 / Math.tan(FOV / 2); dirty = true; // fit the narrower side, so nothing clips on a tall phone canvas
    }
    const ro = window.ResizeObserver ? new ResizeObserver(resize) : null;
    if (ro) ro.observe(canvas); resize();

    function project() {
      const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw), cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch);
      const [tx, ty, tz] = cam.target;
      proj = G.nodes.map((n) => {
        const x = n.p[0] - tx, y = n.p[1] - ty, z = n.p[2] - tz;
        const x1 = x * cy - z * sy, z1 = x * sy + z * cy;
        const y2 = y * cp - z1 * sp, z2 = y * sp + z1 * cp;
        const depth = z2 + cam.dist;
        if (depth < 2) return null;
        const s = f / depth;
        return { x: W / 2 + x1 * s, y: H / 2 - y2 * s, d: depth, s };
      });
    }
    const shownNode = (n) => n.groupSize >= +state.min;
    const visibleLink = (l) => !l.f.every((x) => state.off.has(x)) && shownNode(G.nodes[l.s]);
    const fog = (d) => Math.max(0.3, Math.min(1, 1.3 - (d - cam.dist * 0.6) / (cam.dist * 1.4)));
    const screenR = (n, p) => Math.max(n.kind === "incident" ? 2 : 3, Math.min(n.kind === "incident" ? 7 : 12, n.r * p.s * 0.9));

    function draw() {
      project();
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      // links: batched by factor and depth band
      const bands = 3, paths = {};
      G.links.forEach((l) => {
        if (!visibleLink(l)) return;
        const a = proj[l.s], b = proj[l.t];
        if (!a || !b) return;
        const on = lit && lit.links.has(l.k);
        const factor = ORDER.find((x) => l.f.includes(x) && !state.off.has(x));
        const band = on ? "on" : Math.min(bands - 1, Math.floor((1 - fog((a.d + b.d) / 2)) * bands));
        const key = factor + "|" + band;
        (paths[key] || (paths[key] = [])).push(a.x, a.y, b.x, b.y);
      });
      Object.entries(paths).forEach(([key, pts]) => {
        const [factor, band] = key.split("|");
        ctx.strokeStyle = color[factor];
        ctx.globalAlpha = band === "on" ? 0.95 : (lit ? 0.07 : [0.8, 0.55, 0.32][+band]);
        ctx.lineWidth = band === "on" ? 2 : 1.2;
        ctx.beginPath();
        for (let i = 0; i < pts.length; i += 4) { ctx.moveTo(pts[i], pts[i + 1]); ctx.lineTo(pts[i + 2], pts[i + 3]); }
        ctx.stroke();
      });
      // nodes: far to near
      const order = G.nodes.filter((n) => proj[n.k] && shownNode(n) && (n.kind === "incident" || n.links.some(visibleLink))).sort((a, b) => proj[b.k].d - proj[a.k].d);
      order.forEach((n) => {
        const p = proj[n.k], r = screenR(n, p);
        const active = n.links.some(visibleLink), on = lit && lit.nodes.has(n.k);
        ctx.globalAlpha = (lit ? (on ? 1 : 0.1) : active ? 1 : 0.15) * (on ? 1 : fog(p.d));
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
        ctx.fillStyle = n.kind === "incident" ? INK : color[n.kind];
        ctx.fill();
        ctx.lineWidth = n.kind === "incident" ? 1 : 1.5; ctx.strokeStyle = "#0a0e15"; ctx.stroke();
        if (n.id === state.sel || n === hover) { ctx.globalAlpha = 1; ctx.lineWidth = 2.5; ctx.strokeStyle = GOLD; ctx.beginPath(); ctx.arc(p.x, p.y, r + 2.5, 0, Math.PI * 2); ctx.stroke(); }
      });
      // labels: selected, hovered, and the biggest hubs when nothing is selected
      ctx.globalAlpha = 1; ctx.font = "12px " + (css.getPropertyValue("--sans") || "sans-serif"); ctx.textAlign = "center";
      // selected and hovered first so they always win; a label that would overlap one already drawn is skipped
      const sel = G.byId.get(state.sel);
      const wanted = [hover, sel].filter(Boolean);
      if (!lit) wanted.push(...G.nodes.filter((n) => n.kind !== "incident" && shownNode(n) && n.links.some(visibleLink)).sort((a, b) => b.links.length - a.links.length).slice(0, 10));
      const boxes = [];
      new Set(wanted).forEach((n) => {
        const p = proj[n.k]; if (!p) return;
        const text = (n.title || "").length > 56 ? n.title.slice(0, 55) + "…" : n.title || "";
        const w = ctx.measureText(text).width, x = Math.max(w / 2 + 4, Math.min(W - w / 2 - 4, p.x)), y = p.y - screenR(n, p) - 6;
        const box = [x - w / 2 - 2, y - 12, x + w / 2 + 2, y + 3];
        if (boxes.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1])) return;
        boxes.push(box);
        ctx.lineWidth = 3.5; ctx.strokeStyle = "#0a0e15"; ctx.strokeText(text, x, y);
        ctx.fillStyle = n === hover || n === sel ? "#f4efe1" : "#c9c4b6"; ctx.fillText(text, x, y);
      });
      dirty = false;
    }

    function frame(t) {
      if (!canvas.isConnected) { if (ro) ro.disconnect(); return; }
      if (fly) {
        const k = Math.min(1, (t - fly.t0) / 650), e = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
        cam.target = fly.from.target.map((v, i) => v + (fly.to.target[i] - v) * e);
        cam.dist = fly.from.dist + (fly.to.dist - fly.from.dist) * e;
        if (k >= 1) fly = null; dirty = true;
      }
      if (spin && !drag) { cam.yaw += 0.0016; dirty = true; }
      if (dirty) draw();
      requestAnimationFrame(frame);
    }
    function flyTo(target, dist) {
      const to = { target, dist: Math.max(40, dist) };
      if (reduce) { cam.target = to.target; cam.dist = to.dist; dirty = true; return; }
      fly = { t0: performance.now(), from: { target: cam.target.slice(), dist: cam.dist }, to };
    }
    function setSpin(on) { spin = on; const b = $("#spin"); b.setAttribute("aria-pressed", String(on)); b.textContent = on ? "❚❚" : "▶"; b.setAttribute("aria-label", on ? "Pause rotation" : "Rotate"); }

    // pointer: rotate / pan / pinch, click to select
    let drag = null; const pointers = new Map();
    const pick = (x, y) => {
      let best = null, bd = 1e9;
      G.nodes.forEach((n) => {
        const p = proj && proj[n.k]; if (!p || !shownNode(n)) return;
        if (lit && !lit.nodes.has(n.k)) return;
        const d = Math.hypot(p.x - x, p.y - y);
        if (d < screenR(n, p) + 4 && d < bd) { best = n; bd = d; }
      });
      return best;
    };
    const local = (ev) => { const r = canvas.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; };
    canvas.addEventListener("pointerdown", (ev) => {
      pointers.set(ev.pointerId, local(ev)); canvas.setPointerCapture(ev.pointerId);
      drag = { x: ev.clientX, y: ev.clientY, yaw: cam.yaw, pitch: cam.pitch, target: cam.target.slice(), moved: false, pan: ev.shiftKey || ev.button === 2, pinch: pointers.size === 2 ? pinchDist() : 0, dist: cam.dist };
      fly = null;
    });
    const pinchDist = () => { const [a, b] = [...pointers.values()]; return Math.hypot(a[0] - b[0], a[1] - b[1]); };
    canvas.addEventListener("pointermove", (ev) => {
      if (pointers.has(ev.pointerId)) pointers.set(ev.pointerId, local(ev));
      if (drag) {
        const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
        if (!drag.moved && Math.hypot(dx, dy) > 4) { drag.moved = true; tip.hide(); }
        if (!drag.moved) return;
        if (pointers.size === 2 && drag.pinch) { cam.dist = Math.max(40, drag.dist * drag.pinch / pinchDist()); }
        else if (drag.pan) {
          const k = cam.dist / f, cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw), cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch);
          const right = [cy, 0, -sy], up = [-sp * sy, cp, -sp * cy];
          cam.target = drag.target.map((v, i) => v - right[i] * dx * k + up[i] * dy * k);
        } else {
          cam.yaw = drag.yaw + dx * 0.006;
          cam.pitch = Math.max(-1.5, Math.min(1.5, drag.pitch + dy * 0.006));
        }
        dirty = true; return;
      }
      const [x, y] = local(ev), n = pick(x, y);
      if (n !== hover) { hover = n; dirty = true; canvas.style.cursor = n ? "pointer" : "grab"; }
      if (n) {
        const inc = n.inc;
        tip.show(inc ? `<b>${esc(inc.title)}</b>${esc(dateLabel(inc.date) || inc.date_text || "Undated")} · ${esc(inc.place || "")}<div class="muted">${esc(fileOf(inc).title)} · ${n.links.length} connection${n.links.length === 1 ? "" : "s"}</div>`
          : `<b>${esc(n.title)}</b>${esc(HUB_KIND[n.kind])}<div class="muted">named in ${n.links.length} accounts</div>`, ev);
      } else tip.hide();
    });
    const end = (ev) => {
      pointers.delete(ev.pointerId);
      if (!drag) return;
      const wasMove = drag.moved; drag = pointers.size ? Object.assign(drag, { moved: true }) : null;
      if (wasMove) { setSpin(false); return; }
      const [x, y] = local(ev), n = pick(x, y);
      select(n ? (n.id === state.sel ? "" : n.id) : "", true);
    };
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", (ev) => { pointers.delete(ev.pointerId); drag = null; });
    canvas.addEventListener("pointerleave", () => { if (hover) { hover = null; dirty = true; } tip.hide(); });
    canvas.addEventListener("contextmenu", (ev) => ev.preventDefault());
    canvas.addEventListener("wheel", (ev) => { if (!ev.ctrlKey && !ev.metaKey) return; ev.preventDefault(); fly = null; cam.dist = Math.max(40, cam.dist * (ev.deltaY < 0 ? 0.88 : 1.14)); dirty = true; }, { passive: false });
    $$("[data-z]", stage).forEach((b) => b.addEventListener("click", () => {
      const z = +b.dataset.z;
      if (!z) { select("", false); flyTo(home.target.slice(), home.dist); cam.pitch = home.pitch; return; }
      flyTo(cam.target.slice(), cam.dist / z);
    }));
    $("#spin").addEventListener("click", () => setSpin(!spin));

    /* ---------- selection, filters, panel, table ---------- */
    function computeLit() {
      const sel = state.sel;
      if (sel.startsWith("cluster:")) {
        const c = G.clusterById.get(sel.slice(8)); if (!c) return null;
        const nodes = new Set(c.members), links = new Set();
        G.links.forEach((l) => { if (nodes.has(l.s) && nodes.has(l.t) && visibleLink(l)) links.add(l.k); });
        return { nodes, links };
      }
      const n = G.byId.get(sel);
      if (n) {
        const nodes = new Set([n.k]), links = new Set();
        n.links.forEach((l) => { if (visibleLink(l)) { links.add(l.k); nodes.add(l.s); nodes.add(l.t); } });
        return { nodes, links };
      }
      if (state.q.trim()) {
        const nodes = new Set(), links = new Set();
        G.nodes.forEach((m) => { if (shownNode(m) && matchesText(m.hay, state.q)) nodes.add(m.k); });
        G.links.forEach((l) => { if (nodes.has(l.s) && nodes.has(l.t) && visibleLink(l)) links.add(l.k); });
        return nodes.size ? { nodes, links } : { nodes: new Set(), links: new Set() };
      }
      return null;
    }

    function connectionRow(l, self) {
      const other = G.nodes[l.s === self.k ? l.t : l.s];
      const factors = l.f.filter((x) => !state.off.has(x));
      if (!factors.length) return "";
      const whyText = l.f.map((x, i) => state.off.has(x) ? "" : l.why[i]).filter(Boolean).join("; ");
      const sub = other.inc ? `${esc(fileOf(other.inc).title)}${other.inc.report === (self.inc || {}).report ? ' <span class="dim">(same file)</span>' : ""}` : esc(HUB_KIND[other.kind]);
      return `<li><a href="#" data-sel="${esc(other.id)}" class="fl">${factors.map((x) => `<i style="background:var(${COLOR_VAR[x]})" title="${esc(G.factors[x].label)}"></i>`).join("")}${esc(other.title)}</a>
        <span class="small muted">${sub}</span><span class="small">${esc(whyText)}</span></li>`;
    }

    function renderSide() {
      const sel = state.sel;
      if (sel.startsWith("cluster:") && G.clusterById.get(sel.slice(8))) {
        const c = G.clusterById.get(sel.slice(8));
        const incs = c.nodes.map((n) => n).sort((a, b) => (a.inc.date || "9").localeCompare(b.inc.date || "9"));
        side.innerHTML = `<div class="eyebrow">Cluster · ${c.nodes.length} incidents in ${c.files} file${c.files === 1 ? "" : "s"}</div><h3>${esc(c.lead.place || c.lead.title || "Cluster")}</h3>
          <p class="small muted" style="margin:6px 0 10px">${esc(span(c.first, c.last))}</p>
          <div class="fchips">${ORDER.filter((x) => c.factors[x]).map((x) => chip(x, c.factors[x])).join("")}</div>
          <ul class="netlist">${incs.map((n) => `<li><a href="#" data-sel="${esc(n.id)}" class="fl">${esc(n.inc.title)}</a><span class="small muted">${esc(dateLabel(n.inc.date) || n.inc.date_text || "Undated")} · ${esc(fileOf(n.inc).title)}</span></li>`).join("")}</ul>`;
      } else if (G.byId.get(sel)) {
        const n = G.byId.get(sel);
        const rows = n.links.slice().sort((a, b) => ORDER.indexOf(a.main) - ORDER.indexOf(b.main)).map((l) => connectionRow(l, n)).filter(Boolean);
        if (n.inc) {
          const i = n.inc, r = fileOf(i), c = n.cluster && G.clusterById.get(n.cluster);
          side.innerHTML = `<div class="eyebrow">Incident · ${rows.length} connection${rows.length === 1 ? "" : "s"}</div><h3>${esc(i.title)}</h3>
            <dl class="kv small" style="margin-top:10px"><dt>Date</dt><dd>${esc(dateLabel(i.date) || i.date_text || "Undated")}</dd><dt>Place</dt><dd>${esc(i.place || "—")}</dd>
            <dt>Status in record</dt><dd>${badge("status", i.status)}</dd><dt>File</dt><dd><a href="#/report/${encodeURIComponent(i.report)}?incident=${encodeURIComponent(i.id)}">${esc(r.title)}</a></dd>
            ${c ? `<dt>Cluster</dt><dd><a href="#" data-sel="cluster:${esc(c.id)}">${c.nodes.length} related incidents</a></dd>` : ""}</dl>
            <ul class="netlist">${rows.join("") || `<li class="muted small">No connections under the selected factors.</li>`}</ul>`;
        } else {
          side.innerHTML = `<div class="eyebrow">${esc(HUB_KIND[n.kind])} · ${rows.length} accounts</div><h3>${esc(n.title)}</h3>
            <p class="small muted" style="margin:6px 0 0">${n.kind === "person" ? "Named in each of these accounts. The same name can belong to different people; check the records." : "Each of these observations was made from this mission or vessel."}</p>
            <ul class="netlist">${rows.join("")}</ul>`;
        }
      } else {
        const top = G.clusters.slice(0, 3);
        side.innerHTML = `<div class="eyebrow">Select a node or a cluster</div><h3>Why incidents are connected</h3>
          <div class="fchips" style="margin-top:10px">${ORDER.map((x) => chip(x, st.by_factor[x] || 0)).join("")}</div>
          <p class="small muted" style="margin-top:10px">Pale nodes are incidents; coloured nodes are people (amber) and missions (pink). Largest clusters:</p>
          <ul class="netlist">${top.map((c) => `<li><a href="#" data-sel="cluster:${esc(c.id)}" class="fl">${esc(c.lead.place || c.lead.title)}</a><span class="small muted">${esc(span(c.first, c.last))} · ${c.nodes.length} incidents in ${c.files} files</span></li>`).join("")}</ul>`;
      }
      $$("[data-sel]", side).forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); select(a.dataset.sel, true); }));
    }

    function renderTable() {
      const rows = G.clusters.filter((c) => shownNode(c.nodes[0]) && (!state.q.trim() || matchesText(c.hay, state.q)) && ORDER.some((x) => c.factors[x] && !state.off.has(x) && x !== "person" && x !== "mission"));
      $("#rows").innerHTML = rows.slice(0, shown).map((c) => `<tr class="click${state.sel === "cluster:" + c.id ? " cur" : ""}" data-sel="cluster:${esc(c.id)}" tabindex="0">
        <td><b>${esc(c.lead.place || "Unplaced")}</b><div class="muted small">${esc(c.lead.title || "")}</div></td><td style="white-space:nowrap">${esc(span(c.first, c.last))}</td>
        <td class="num">${c.nodes.length}</td><td class="num">${c.files}</td><td><div class="fchips">${ORDER.filter((x) => c.factors[x]).map((x) => chip(x, c.factors[x])).join("")}</div></td></tr>`).join("")
        || `<tr><td colspan="5" class="empty">No clusters match.</td></tr>`;
      $("#more").hidden = rows.length <= shown;
      $$("tr[data-sel]", main).forEach((tr) => {
        const go = () => { select(tr.dataset.sel, true); $(".netgrid").scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" }); };
        tr.addEventListener("click", go);
        tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter") go(); });
      });
    }

    function select(key, move) {
      state.sel = key && (G.byId.has(key) || (key.startsWith("cluster:") && G.clusterById.has(key.slice(8)))) ? key : "";
      // selecting something in a group the size filter hides (e.g. from a link) widens the filter
      const target = G.byId.get(state.sel) || (G.clusterById.get(state.sel.slice(8)) || { nodes: [] }).nodes[0];
      if (target && !shownNode(target)) { state.min = "2"; $("[name=min]", main).value = "2"; renderTable(); }
      lit = computeLit(); dirty = true;
      renderSide();
      $$("tr[data-sel]", main).forEach((tr) => tr.classList.toggle("cur", tr.dataset.sel === state.sel));
      if (move && state.sel) {
        setSpin(false);
        const pts = [...lit.nodes].map((k) => G.nodes[k].p);
        const c = [0, 1, 2].map((d) => pts.reduce((s, p) => s + p[d], 0) / pts.length);
        const rad = Math.max(...pts.map((p) => Math.hypot(p[0] - c[0], p[1] - c[1], p[2] - c[2])), 30);
        flyTo(c, rad / Math.sin(FOV / 2) * 1.15);
      }
      save();
    }
    const save = () => setQuery("connections", { min: state.min === "3" ? "" : state.min, q: state.q, off: [...state.off].join(","), sel: state.sel });

    const form = $("#f");
    form.addEventListener("input", () => {
      state.q = form.q.value; state.min = form.min.value;
      const s = G.byId.get(state.sel) || (G.clusterById.get(state.sel.slice(8)) || { nodes: [] }).nodes[0];
      if (s && !shownNode(s)) state.sel = "";
      state.off = new Set(ORDER.filter((x) => !$$("input[name=factor]", form).find((c) => c.value === x).checked));
      shown = 30; lit = computeLit(); dirty = true; renderSide(); renderTable(); save();
    });
    form.addEventListener("submit", (ev) => ev.preventDefault());
    $("#more").addEventListener("click", () => { shown += 50; renderTable(); });

    lit = computeLit(); renderSide(); renderTable();
    if (state.sel) select(state.sel, true);
    requestAnimationFrame(frame);
  };
})();
