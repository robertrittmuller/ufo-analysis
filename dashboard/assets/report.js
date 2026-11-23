/* Deep report view: one page per source file, loaded from data/reports/<id>.json. */
"use strict";
(() => {
  const cache = new Map();
  const LIKELIHOOD = ["almost no chance", "very unlikely", "unlikely", "roughly even chance", "likely", "very likely", "almost certain"];
  const SVP = {
    source: ["Unknown or unreliable source", "Report from a source of unknown reliability", "Credible source, second-hand", "Credible source, first-hand", "First-hand interview by a source of proven reliability"],
    visit: ["No site visit", "Visit by a casual person", "Visit by someone familiar with the phenomena", "Site visit by a reliable investigator", "Site visit by a skilled analyst"],
    explanations: ["Data consistent with a natural cause", "Natural explanation needs slight alteration of the data", "Needs gross alteration of one parameter", "Needs gross alteration of several parameters", "No natural explanation possible, given the evidence"],
  };
  const RELIABILITY = { A: "Completely reliable", B: "Usually reliable", C: "Fairly reliable", D: "Not usually reliable", E: "Unreliable", F: "Reliability cannot be judged" };
  const CREDIBILITY = { 1: "Confirmed by other sources", 2: "Probably true", 3: "Possibly true", 4: "Doubtful", 5: "Improbable", 6: "Truth cannot be judged" };
  const METHOD = { llm_ocr: "Vision-model OCR", native: "Born-digital text", tesseract: "Tesseract (fallback)", text_layer: "Embedded text layer (fallback)", failed: "Unreadable", publisher: "Publisher description", frames: "Machine frame description", captions: "Official captions", asr: "Machine transcript" };
  const CHECK = { verified: "matches page text", approximate: "close match to page text", unverified: "not found in page text" };

  const list = (items) => (items && items.length ? `<ul class="tight">${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<span class="muted">—</span>`);
  const kv = (rows) => { const r = rows.filter(([, v]) => v !== "" && v != null && v !== false); return r.length ? `<dl class="kv">${r.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>` : `<p class="muted">Not stated in the record.</p>`; };
  const pg = (pages, labels) => (pages || []).map((n) => `<button class="pg" data-page="${n}" title="Open this page in the source text">${esc(labels[n] || "p. " + n)}</button>`).join("");
  const meter = (n, of) => `<span class="meter" aria-label="${n} of ${of}">${Array.from({ length: of }, (_, i) => `<i class="${i < n ? "on" : ""}"></i>`).join("")}</span>`;
  const quote = (q, labels) => `<blockquote>“${esc(q.text)}”<footer>${pg([q.page], labels)}<span class="ck ${q.check}">${CHECK[q.check]}</span>${q.why ? ` — ${esc(q.why)}` : ""}</footer></blockquote>`;
  const duration = (s) => (s == null ? "" : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`);

  function incidentHtml(inc, labels, open) {
    const o = inc.object, a = inc.assessment, d = inc.documentation, loc = inc.location;
    const when = [dateLabel(inc.date.iso) || inc.date.text, inc.time_text].filter(Boolean).join(", ") || "Undated";
    const comp = Object.entries(d.components).map(([k, v]) => `<span>${esc(cap(k))}</span><div class="track"><div class="fill" style="width:${(v / 25) * 100}%"></div></div><span>${v}/25</span>`).join("");
    return `<details class="incident" id="${esc(inc.id)}"${open ? " open" : ""}>
      <summary><h3>${esc(inc.title)}</h3><span>${badge("status", inc.status_in_record)}</span>
        <span class="muted small">${esc(when)}${loc.label ? " · " + esc(loc.label) : ""} ${pg(inc.pages.slice(0, 6), labels)}</span><span class="muted small" style="text-align:right">Documentation ${d.score}/100</span></summary>
      <div class="body">
        <p>${esc(inc.narrative)}</p>
        <div class="cols">
          <div><p class="sub-h">When and where</p>${kv([["Date", esc(when) + (inc.date.text && inc.date.iso ? ` <span class="muted small">(as written: ${esc(inc.date.text)})</span>` : "") + (inc.date.corrected ? ` <span class="muted small">— ${esc(inc.date.corrected)}</span>` : "")], ["Duration", esc(inc.duration_text)],
            ["Place", loc.label ? `${esc(loc.label)} <span class="muted small">(${esc(loc.precision === "none" ? "not mapped" : ({ city: "confirmed position: named place", stated: "confirmed position: coordinates in the record", estimated: "estimated position, not confirmed", region: "state centroid only", country: "country centroid only" })[loc.precision] || loc.precision)}; refers to the ${esc(loc.refers_to === "unclear" ? "observer or object — unclear" : loc.refers_to)})</span>` : ""],
            ["Coordinates", loc.lat != null ? `${loc.lat}, ${loc.lon}` : ""], ["Light", inc.conditions.light !== "unknown" ? esc(cap(inc.conditions.light)) : ""], ["Weather", esc(inc.conditions.weather)]])}</div>
          <div><p class="sub-h">Observers and sensors</p>${kv([["Witnesses", inc.witnesses.length ? list(inc.witnesses.map((w) => `${w.description || cap(w.category)}${w.count > 1 ? ` (${w.count})` : ""} — ${cap(w.category)}, ${w.firsthand ? "first-hand" : "second-hand or unclear"}`)) : ""],
            ["Observed by", inc.observation.modes.length ? `<span class="chips">${inc.observation.modes.map((m) => `<span class="chip">${esc(cap(m))}</span>`).join("")}</span>` : ""], ["Sensors", esc(inc.observation.sensors)], ["Platform", esc(inc.observation.platform)],
            ["Measurements from", inc.measurements_from !== "none" ? esc(cap(inc.measurements_from)) : ""]])}</div>
          <div><p class="sub-h">Object as reported</p>${kv([["Count", esc(o.count)], ["Shape", o.shape !== "not-described" ? `${esc(cap(o.shape))}${o.shape_text ? ` <span class="muted small">(“${esc(o.shape_text)}”)</span>` : ""}` : esc(o.shape_text)], ["Size", esc(o.size)], ["Colour", esc(o.color)],
            ["Luminosity", esc(o.luminosity)], ["Sound", esc(o.sound)], ["Altitude", esc(o.altitude)], ["Speed", esc(o.speed)], ["Distance", esc(o.distance)], ["Motion", esc(o.motion)], ["Disappearance", esc(o.disappearance)]])}</div>
          <div><p class="sub-h">Effects and evidence</p>${kv([["Effects", esc(inc.effects)], ["Physical evidence", esc(inc.physical_evidence)]])}
            <p class="sub-h" style="margin-top:12px">Documentation strength: ${d.score}/100 (${esc(label("band", d.band))})</p><div class="bar4">${comp}</div></div>
        </div>
        <div class="split">
          <div class="record"><p class="sub-h">What the record says</p>${kv([["Status", badge("status", inc.status_in_record)], ["Official action", esc(inc.official_action)], ["Official explanation", esc(inc.official_explanation) || `<span class="muted">None given in the record</span>`]])}</div>
          <div class="analyst"><p class="sub-h">Analyst assessment (machine-generated)</p>${kv([["Best explanation", esc(a.best_explanation) || `<span class="muted">None identified</span>`],
            ["Likelihood of this explanation", a.likelihood ? `${meter(LIKELIHOOD.indexOf(a.likelihood) + 1, 7)} ${esc(a.likelihood)}` : ""], ["Hynek class", a.hynek !== "not-applicable" ? esc(`${a.hynek} — ${label("hynek", a.hynek)}`) : ""],
            ["Strangeness", `${meter(a.strangeness, 5)} ${a.strangeness}/5`], ["Reasoning", esc(a.reasoning)]])}</div>
        </div>
        ${inc.quotes.length ? `<div><p class="sub-h">From the record</p>${inc.quotes.map((q) => quote(q, labels)).join("")}</div>` : ""}
        ${inc.same_event && inc.same_event.length ? `<div><p class="sub-h">Same event in other files</p><ul class="tight">${inc.same_event.map((e) => `<li><a href="#/report/${encodeURIComponent(e.report)}?incident=${encodeURIComponent(e.id)}">${esc(e.title)}</a> <span class="muted small">— ${esc((App.reportById.get(e.report) || {}).title || "")}</span></li>`).join("")}</ul></div>` : ""}
      </div></details>`;
  }

  function render(main, R, params) {
    const S = R.synthesis || {}, M = R.manifest || {}, X = R.extraction || {}, Q = R.quality || {}, A = S.assessment || {}, P = S.provenance || {}, T = S.ratings || {}, E = R.entities || {};
    const isMedia = M.media_type === "video" || M.media_type === "audio";
    const labels = Object.fromEntries((R.pages || []).map((p) => [p.n, p.label]));
    const unit = isMedia ? "section" : "page";
    const problems = [];
    if ((X.failed_pages || []).length) problems.push(`${X.failed_pages.length} page(s) could not be read: ${X.failed_pages.slice(0, 15).join(", ")}`);
    (Q.chunks_failed || []).forEach((c) => problems.push(`Pages ${c.pages[0]}–${c.pages[1]} were not analysed`));
    if (Q.synthesis_error) problems.push("The file-level synthesis could not be generated");
    if (X.partial) problems.push(`Only ${X.pages_extracted} of ${X.page_count} pages have been extracted`);
    (X.issues || []).forEach((i) => problems.push(i));
    const citation = `"${M.title || R.title}," ${M.agency || "U.S. Government"}, PURSUE release of ${dateLabel(R.release) || M.release_date || "n.d."}, file ${R.filename}. ${M.original_source_url || M.source_page_url || ""}`;
    const svp = T.svp || {};
    const hasRatings = T.hynek && T.hynek !== "not-applicable" || T.vallee || svp.source != null || T.source_reliability || T.information_credibility;
    const period = S.period_covered || {};
    const sections = [["summary", "Summary"], ["incidents", `Incidents (${R.incidents.length})`], ["assessment", "Assessment"], ["provenance", "Provenance"]];
    if (isMedia || (R.frames || []).length) sections.push(["media", "Recording"]);
    sections.push(["entities", "People and entities"], ["related", "Related reports"], ["text", "Source text"], ["quality", "Extraction quality"]);

    main.innerHTML = `<div class="report">
    <nav class="toc" aria-label="Report sections"><a href="#/reports">← All reports</a>${sections.map(([k, l]) => `<a href="#sec-${k}" data-sec="${k}">${esc(l)}</a>`).join("")}</nav>
    <article>
      <header class="rhead"><div class="eyebrow">Deep report · ${esc(label("media", M.media_type))}${M.agency ? ` · source: ${esc(M.agency)}` : ""}</div><h1>${esc(R.title)}</h1>
        <p class="pub">Published as: ${esc(M.title || R.filename)} · released ${esc(dateLabel(R.release) || M.release_date || "—")}${M.upstream_status === "removed" ? ' · <b style="color:var(--warn)">no longer listed upstream</b>' : ""}</p>
        <div class="chips">${badge("disposition", A.disposition)}${S.record_kind ? `<span class="chip">${esc(cap(S.record_kind))}</span>` : ""}${(S.themes || []).map((t) => `<a class="chip" href="#/reports?theme=${encodeURIComponent(t)}">${esc(t)}</a>`).join("")}</div>
        <div class="actions">${M.original_source_url ? `<a class="btn primary" href="${esc(M.original_source_url)}" target="_blank" rel="noopener">Open original file ↗</a>` : ""}
          ${M.dvids_video_url ? `<a class="btn" href="${esc(M.dvids_video_url)}" target="_blank" rel="noopener">Publisher video page ↗</a>` : ""}
          <a class="btn" href="/files/${encodeURIComponent(R.id)}/">Shareable page</a><button class="btn" id="cite">Copy citation</button><button class="btn" id="dl">Download report JSON</button><button class="btn" id="print">Print</button></div></header>
      ${problems.length ? `<div class="warnbox"><b>This report has known gaps.</b><ul class="tight">${problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul></div>` : ""}

      <section class="sec" id="sec-summary"><h2>Summary</h2>
        <div class="prose">${(S.summary || M.description_blurb || "No summary is available for this file yet.").split(/\n+/).map((p) => `<p>${esc(p)}</p>`).join("")}</div>
        ${(S.key_points || []).length ? `<p class="sub-h" style="margin-top:14px">Key points</p><ul class="keypoints">${S.key_points.map((k) => `<li>${esc(k.text)}${pg(k.pages, labels)}</li>`).join("")}</ul>` : ""}
        <div class="glance" style="margin-top:16px">
          <div><span>Document date</span><b>${esc(dateLabel((S.document_date || {}).iso) || (S.document_date || {}).text || "Not stated")}</b></div>
          <div><span>Period covered</span><b>${esc(period.start ? (period.end && period.end !== period.start ? `${dateLabel(period.start)} – ${dateLabel(period.end)}` : dateLabel(period.start)) : "—")}</b></div>
          <div><span>${isMedia ? "Duration" : "Length"}</span><b>${isMedia ? esc(duration(X.duration) || "—") : `${fmt(X.page_count)} page${X.page_count === 1 ? "" : "s"}`}</b></div>
          <div><span>Incidents</span><b>${R.incidents.length}</b></div>
          <div><span>Redaction</span><b>${esc(cap((S.redaction || {}).level) || "—")}</b></div>
          <div><span>Research value</span><b>${A.research_value ? `${meter(A.research_value, 5)} ${A.research_value}/5` : "—"}</b></div>
        </div>${A.research_value_reason ? `<p class="muted small" style="margin-top:8px">${esc(A.research_value_reason)}</p>` : ""}
        ${M.description_blurb ? `<details class="more"><summary>Publisher's description</summary><p class="muted">${esc(M.description_blurb)}</p></details>` : ""}
      </section>

      <section class="sec" id="sec-incidents"><h2>Incidents in this file</h2>
        ${R.incidents.length ? `<p class="muted small" style="margin-bottom:10px">Each incident keeps the record's own account separate from the machine-generated assessment. Page chips open the source text.${R.incidents.length > 3 ? ' <button class="btn small" id="openall">Expand all</button>' : ""}</p>${R.incidents.map((i) => incidentHtml(i, labels, R.incidents.length <= 2)).join("")}`
          : `<p class="muted">No specific observation event is described in this file${A.disposition === "not-a-sighting-record" ? " — it is not a sighting record" : ""}.</p>`}
      </section>

      <section class="sec" id="sec-assessment"><h2>Assessment</h2>
        <p class="muted small" style="margin-bottom:10px">Machine-generated from the evidence in this file only. It is an aid to reading the record, not a finding.</p>
        <div class="split" style="margin-bottom:14px">
          <div class="record"><p class="sub-h">Official handling and conclusion</p>${kv([["Investigating bodies", (S.official_handling || {}).investigating_bodies && S.official_handling.investigating_bodies.length ? esc(S.official_handling.investigating_bodies.join("; ")) : ""],
            ["Actions", (S.official_handling || {}).actions && S.official_handling.actions.length ? list(S.official_handling.actions) : ""],
            ["Record's conclusion", ((S.official_handling || {}).conclusion ? esc(S.official_handling.conclusion) + pg(S.official_handling.conclusion_pages, labels) : `<span class="muted">None stated</span>`)]])}</div>
          <div class="analyst"><p class="sub-h">Disposition</p><p>${badge("disposition", A.disposition)}</p><p>${esc(A.rationale || "")}</p></div>
        </div>
        ${(A.candidate_explanations || []).length ? `<p class="sub-h">Candidate explanations</p>${A.candidate_explanations.map((x) => `<div class="expl"><h3>${esc(x.explanation)}</h3>
            <div class="small muted" style="margin-top:3px">${esc(cap(x.category))}${x.likelihood ? ` · ${meter(LIKELIHOOD.indexOf(x.likelihood) + 1, 7)} ${esc(x.likelihood)}` : ""}</div>
            <div class="procon"><div><p class="sub-h">Supporting</p>${list(x.supporting)}</div><div><p class="sub-h">Contradicting</p>${list(x.contradicting)}</div></div></div>`).join("")}` : ""}
        ${(A.claimed_characteristics || []).length ? `<p class="sub-h" style="margin-top:14px">Unusual characteristics claimed in the record</p><ul class="tight">${A.claimed_characteristics.map((c) => `<li>${esc(cap(c.name))} — based on ${esc(c.basis === "instrument" ? "instrument data" : "a witness estimate")}${pg(c.pages, labels)}</li>`).join("")}</ul>` : ""}
        <div class="cols" style="margin-top:14px"><div><p class="sub-h">Data gaps</p>${list(A.data_gaps)}</div><div><p class="sub-h">What to look for next</p>${list(A.follow_up)}</div></div>
        ${hasRatings ? `<p class="sub-h" style="margin-top:14px">Classification and reliability ratings</p>${kv([
          ["Hynek", T.hynek && T.hynek !== "not-applicable" ? esc(`${T.hynek} — ${label("hynek", T.hynek)}`) : ""], ["Vallée", esc(T.vallee)],
          ["SVP source", svp.source != null ? `${svp.source} — ${esc(SVP.source[svp.source])}` : ""], ["SVP site visit", svp.visit != null ? `${svp.visit} — ${esc(SVP.visit[svp.visit])}` : ""],
          ["SVP explanations", svp.explanations != null ? `${svp.explanations} — ${esc(SVP.explanations[svp.explanations])}` : ""],
          ["Source reliability", T.source_reliability ? `${esc(T.source_reliability)} — ${esc(RELIABILITY[T.source_reliability])}` : ""],
          ["Information credibility", T.information_credibility ? `${esc(T.information_credibility)} — ${esc(CREDIBILITY[T.information_credibility])}` : ""], ["Basis", esc(T.notes)]])}
          <p class="small"><a href="#/about">What these scales mean</a></p>` : ""}
      </section>

      <section class="sec" id="sec-provenance"><h2>Provenance and archival details</h2>
        <div class="cols">
          <div><p class="sub-h">Origin</p>${kv([["Document type", esc(S.document_type)], ["Originating agency", esc(P.originating_agency)], ["Office", esc(P.originating_office)], ["Authors", esc((P.authors || []).join("; "))], ["Recipients", esc((P.recipients || []).join("; "))],
            ["File numbers", esc((P.file_numbers || []).join("; "))], ["Record series", esc(P.record_series)], ["Distribution", esc(P.distribution)]])}</div>
          <div><p class="sub-h">Classification and release</p>${kv([["Original classification", esc(P.original_classification)], ["Declassification", esc(P.declassification)], ["Released", esc(dateLabel(R.release) || M.release_date)], ["Releasing agency", esc(M.agency)],
            ["Markings on pages", X.markings && Object.keys(X.markings).length ? `<span class="chips">${Object.entries(X.markings).slice(0, 14).map(([k, v]) => `<span class="chip">${esc(k)} ×${v}</span>`).join("")}</span>` : ""],
            ["File checksum", M.sha256 ? `<span class="mono">sha256 ${esc(M.sha256.slice(0, 20))}…</span>` : ""]])}</div>
          <div><p class="sub-h">Redaction</p>${kv([["Level", esc(cap((S.redaction || {}).level))], ["Redacted passages counted", X.redaction_marks ? `${fmt(X.redaction_marks)} on ${fmt(X.pages_with_redactions)} page(s)` : ""], ["Exemption codes", esc(((S.redaction || {}).exemptions || []).join("; "))], ["What is withheld", esc((S.redaction || {}).withheld)]])}</div>
          <div><p class="sub-h">Completeness</p>${kv([["Appears complete", (S.completeness || {}).appears_complete == null ? "" : (S.completeness.appears_complete ? "Yes" : "No")], ["Referenced but missing", ((S.completeness || {}).missing || []).length ? list(S.completeness.missing) : ""], ["Notes", esc((S.completeness || {}).notes)]])}</div>
        </div>
        ${(R.records || []).length ? `<details class="more"${R.records.length <= 12 ? " open" : ""}><summary>Constituent records in this file (${R.records.length})</summary><div class="tablewrap"><table><thead><tr><th>Pages</th><th>Type</th><th>Date</th><th>From</th><th>To</th><th>Subject</th><th>File no.</th></tr></thead><tbody>
          ${R.records.map((r) => `<tr><td style="white-space:nowrap">${pg(r.pages.slice(0, 1), labels)}${r.pages.length > 1 ? "–" + r.pages[r.pages.length - 1] : ""}</td><td>${esc(r.type)}</td><td>${esc(r.date)}</td><td>${esc(r.from)}</td><td>${esc(r.to)}</td><td>${esc(r.subject)}</td><td>${esc(r.file_numbers.join("; "))}</td></tr>`).join("")}</tbody></table></div></details>` : ""}
        ${(R.key_passages || []).length ? `<p class="sub-h" style="margin-top:14px">Key passages</p>${R.key_passages.slice(0, 12).map((q) => quote(q, labels)).join("")}` : ""}
      </section>

      ${isMedia || (R.frames || []).length ? `<section class="sec" id="sec-media"><h2>The recording</h2>
        ${kv([["Duration", esc(duration(X.duration))], ["Resolution", esc(X.resolution)], ["Sensor type (from frames)", esc(cap(X.sensor_type))], ["Official captions", X.captions ? "Yes" : "None with text"], ["Speech transcript", esc(X.transcript_status)],
          ["Transcribed by", esc((X.transcript_providers || []).join(", "))], ["Rejected transcript segments", X.transcript_rejected_chunks ? `${X.transcript_rejected_chunks} (likely recognition errors on noise)` : ""]])}
        ${(R.frames || []).length ? `<p class="sub-h" style="margin-top:14px">Sampled frames</p><div class="frames">${R.frames.map((f) => `<figure><img src="${esc(f.src)}" alt="Frame at ${f.t} seconds" loading="lazy"><figcaption>${f.t}s</figcaption></figure>`).join("")}</div>` : ""}
        ${R.frame_review ? `<p class="sub-h" style="margin-top:14px">Machine description of the frames</p>${kv([["Summary", esc(R.frame_review.summary)], ["Object", esc(R.frame_review.object_description)], ["Apparent motion", esc(R.frame_review.object_motion)], ["Background", esc(R.frame_review.background)],
          ["On-screen text", esc(R.frame_review.overlay_text)], ["Image quality", esc(cap(R.frame_review.image_quality))], ["Cannot be determined", esc(R.frame_review.limits)]])}` : ""}
      </section>` : ""}

      <section class="sec" id="sec-entities"><h2>People and entities</h2>
        ${(E.people || []).length ? `<details class="more" open><summary>People (${E.people.length})</summary><div class="tablewrap"><table><thead><tr><th>Name</th><th>Role</th><th>Organization</th></tr></thead><tbody>${E.people.slice(0, 150).map((p) => `<tr><td><a href="#/entities?q=${encodeURIComponent(p.name)}">${esc(p.name)}</a></td><td>${esc(p.role)}</td><td>${esc(p.org)}</td></tr>`).join("")}</tbody></table></div></details>` : `<p class="muted">No named people.</p>`}
        <div class="cols" style="margin-top:12px">${[["organizations", "Organizations"], ["places", "Places"], ["aircraft_equipment", "Aircraft and equipment"], ["programs", "Programs and projects"], ["file_numbers", "File and case numbers"]]
          .filter(([k]) => (E[k] || []).length).map(([k, l]) => `<div><p class="sub-h">${l} (${E[k].length})</p><div class="chips">${E[k].slice(0, 60).map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</div></div>`).join("")}</div>
      </section>

      <section class="sec" id="sec-related"><h2>Related reports</h2>
        ${(R.related || []).length ? `<ul class="tight">${R.related.map((r) => `<li><a href="#/report/${encodeURIComponent(r.id)}">${esc(r.title)}</a> <span class="muted small">— ${esc(r.why.join("; "))} (similarity ${r.score.toFixed(2)})</span></li>`).join("")}</ul>` : `<p class="muted">No closely related reports were found.</p>`}
        ${M.video_pairing || M.pdf_pairing ? `<p class="muted small" style="margin-top:8px">Publisher pairing: ${esc([M.video_pairing, M.pdf_pairing].filter(Boolean).join("; "))}</p>` : ""}
      </section>

      <section class="sec" id="sec-text"><h2>Source text</h2>
        <p class="muted small" style="margin-bottom:10px">${isMedia ? "The material derived from the recording, section by section." : "The extracted text of every page. Compare with the original before quoting."}</p>
        <form class="filters" id="find"><label>Find in this file<input type="search" name="q" placeholder="word or phrase"></label><span class="muted small" id="findcount"></span></form>
        <div class="reader"><div class="pagelist" id="pagelist"></div><div class="pagetext" id="pagetext"></div></div>
      </section>

      <section class="sec" id="sec-quality"><h2>Extraction quality</h2>
        ${kv([["Report status", R.status === "complete" ? "Complete" : `<b style="color:var(--warn)">Incomplete</b>`], [`${cap(unit)}s extracted`, `${fmt(X.pages_extracted)} of ${fmt(X.page_count)}`],
          ["How text was obtained", X.methods ? esc(Object.entries(X.methods).map(([k, v]) => `${METHOD[k] || k}: ${v}`).join("; ")) : ""], ["OCR models", X.providers ? esc(Object.entries(X.providers).map(([k, v]) => `${k}: ${v}`).join("; ")) : ""],
          ["Legibility", X.legibility ? esc(Object.entries(X.legibility).map(([k, v]) => `${k}: ${v}`).join("; ")) : ""], ["Handwritten pages", X.handwritten_pages || ""], ["Pages flagged for review", (X.flagged_pages || []).length ? esc(X.flagged_pages.join(", ")) : ""],
          ["Analysis models", esc(Object.entries(Q.providers || {}).map(([k, v]) => `${k}: ${v} part(s)`).concat(Q.synthesis_provider ? [`synthesis: ${Q.synthesis_provider}`] : []).join("; "))],
          ["Quotes checked", Q.quotes_total ? `${Q.quotes_verified} exact, ${Q.quotes_approximate} approximate, ${Q.quotes_unverified} not found and hidden (of ${Q.quotes_total})` : ""], ["Analysed", esc(dateLabel((R.analyzed_at || "").slice(0, 10)))]])}
      </section>
    </article></div>`;

    /* ----- source text reader ----- */
    const pages = R.pages || []; let current = null, term = (params.get("find") || "").trim();
    const listEl = $("#pagelist", main), textEl = $("#pagetext", main);
    const escapeRe = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    function drawList() {
      const low = term.toLowerCase(); let hits = 0;
      listEl.innerHTML = pages.map((p) => { const hit = low && p.text.toLowerCase().includes(low); if (hit) hits++; return `<button data-n="${p.n}" class="${p.n === current ? "on" : ""}${hit ? " hit" : ""}"><span>${esc(p.label)}</span><span class="dim">${p.method === "failed" ? "✕" : p.flags.length ? "⚑" : ""}</span></button>`; }).join("");
      $("#findcount", main).textContent = low ? `${hits} ${unit}${hits === 1 ? "" : "s"} contain it` : "";
      $$("button", listEl).forEach((b) => b.addEventListener("click", () => show(+b.dataset.n, false)));
    }
    function show(n, scroll) {
      const p = pages.find((x) => x.n === n) || pages[0]; if (!p) { textEl.innerHTML = `<p class="muted">No text was extracted.</p>`; return; }
      current = p.n; drawList();
      let body = esc(p.text || "[no text on this page]");
      if (term) body = body.replace(new RegExp("(" + escapeRe(esc(term)) + ")", "gi"), "<mark>$1</mark>");
      const src = M.original_source_url && M.media_type === "pdf" ? `<a href="${esc(M.original_source_url)}#page=${p.n}" target="_blank" rel="noopener">Open this page in the original ↗</a>` : "";
      textEl.innerHTML = `<div class="resultbar" style="margin:0"><span><b>${esc(p.label)}</b> · ${esc(METHOD[p.method] || p.method)}${p.provider && p.method === "llm_ocr" ? ` (${esc(p.provider)})` : ""}${p.legibility ? ` · legibility ${esc(p.legibility)}` : ""}${p.redactions ? ` · ${p.redactions} redaction${p.redactions === 1 ? "" : "s"}` : ""}${p.handwriting ? " · handwriting" : ""}${p.flags.length ? ` · <span style="color:var(--warn)">${esc(p.flags.join(", ").replace(/_/g, " "))}</span>` : ""}</span><span>${src}</span></div><pre>${body}</pre>`;
      const on = $("button.on", listEl); if (on) on.scrollIntoView({ block: "nearest" });
      if (scroll) $("#sec-text", main).scrollIntoView();
    }
    $("#find", main).addEventListener("input", (ev) => { term = ev.target.value.trim(); const low = term.toLowerCase(); const first = low && pages.find((p) => p.text.toLowerCase().includes(low)); show(first ? first.n : current, false); });
    $("#find", main).addEventListener("submit", (ev) => ev.preventDefault());
    if (term) $("#find input", main).value = term;
    main.addEventListener("click", (ev) => { const b = ev.target.closest(".pg"); if (b && main.contains(b)) { ev.preventDefault(); show(+b.dataset.page, true); } });
    show(+params.get("page") || (pages[0] || {}).n, Boolean(params.get("page")));

    /* ----- actions ----- */
    $$(".toc a[data-sec]", main).forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); $("#sec-" + a.dataset.sec, main).scrollIntoView(); }));
    $("#cite", main).addEventListener("click", async (ev) => { try { await navigator.clipboard.writeText(citation); ev.target.textContent = "Citation copied"; } catch (e) { prompt("Copy this citation:", citation); } });
    $("#dl", main).addEventListener("click", () => download(`${R.id}.json`, JSON.stringify(R, null, 1), "application/json"));
    $("#print", main).addEventListener("click", () => { $$("details.incident", main).forEach((d) => { d.open = true; }); window.print(); });
    const openAll = $("#openall", main); if (openAll) openAll.addEventListener("click", () => { const any = $$("details.incident", main).some((d) => !d.open); $$("details.incident", main).forEach((d) => { d.open = any; }); openAll.textContent = any ? "Collapse all" : "Expand all"; });
    const target = params.get("incident") && document.getElementById(params.get("incident"));
    if (target) { target.open = true; target.scrollIntoView(); } else if (!params.get("page")) window.scrollTo(0, 0);
    bindTips(main);
  }

  window.viewReport = async function (main, id, params) {
    if (!id || !App.reportById.has(id)) { main.innerHTML = `<p class="empty">That report does not exist. <a href="#/reports">Browse all reports</a>.</p>`; return; }
    document.title = `${App.reportById.get(id).title} | Declassified UFO Files`;
    if (!cache.has(id)) {
      main.innerHTML = `<p class="loading">Loading report…</p>`;
      try { const res = await fetch(`data/reports/${encodeURIComponent(id)}.json`); if (!res.ok) throw new Error(`HTTP ${res.status}`); cache.set(id, await res.json()); }
      catch (err) { main.innerHTML = `<p class="empty">The report could not be loaded (${esc(err.message)}). <a href="#/reports">Back to reports</a>.</p>`; return; }
    }
    render(main, cache.get(id), params);
  };
})();
