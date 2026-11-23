"""Static, crawlable HTML pages: one per report at /files/<id>/ and an index at /files/.

The interactive dashboard routes with "#/report/<id>", which search engines treat as the home page.
These plain pages carry each report's summary, incidents, key passages and transcript as ordinary HTML
so every file can be found and indexed on its own, and they link into the interactive report.
"""

from __future__ import annotations

import hashlib
import html
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from .util import atomic_write_text, read_json

SITE_NAME = "Declassified UFO Files"
PAGES_DIR = "files"
TRANSCRIPT_CHARS = 150_000
_CSS_VERSION = [""]  # "?v=<hash of app.css>", set by write_pages so a changed stylesheet is not served from cache  # longer transcripts are cut here; the interactive report has the rest

DISPOSITIONS = {
    "explained-in-record": "Explained in the record", "likely-explained": "Likely explained",
    "insufficient-information": "Insufficient information", "unexplained-in-record": "Unexplained in the record",
    "not-a-sighting-record": "Not a sighting record", "contains-incidents": "Contains incidents",
}
MEDIA = {"pdf": "Document", "image": "Image", "video": "Video", "audio": "Audio"}


def _e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:.")
    return cut + "…"


def _pages_label(pages: List[int]) -> str:
    if not pages:
        return ""
    return f"p. {pages[0]}" if len(pages) == 1 else f"pp. {pages[0]}–{pages[-1]}"


def _shell(title: str, description: str, url: str, image: str, body: str, json_ld: Dict[str, Any]) -> str:
    css_version = _CSS_VERSION[0]
    ld = json.dumps(json_ld, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<meta name="description" content="{_e(description)}">
<link rel="canonical" href="{_e(url)}">
<meta property="og:site_name" content="{SITE_NAME}">
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(description)}">
<meta property="og:type" content="article">
<meta property="og:url" content="{_e(url)}">
<meta property="og:image" content="{_e(image)}">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/assets/app.css{css_version}">
<script type="application/ld+json">{ld}</script>
</head>
<body>
<header class="topbar">
  <a class="brand" href="/"><span class="brand-mark" aria-hidden="true"></span><span>Declassified <em>UFO Files</em></span></a>
  <nav class="nav" aria-label="Sections">
    <a href="/">Overview</a><a href="/{PAGES_DIR}/">All files</a><a href="/#/incidents">Incidents</a><a href="/#/connections">Connections</a><a href="/#/search">Full-text search</a><a href="/#/about">Methods</a>
  </nav>
</header>
<main class="static-page">
<p class="disclaimer small"><b>Not a government website.</b> An independent project, not affiliated with or endorsed by the U.S. Department of War or any government agency. Source files come from the Department of War's public <a href="https://www.war.gov/ufo/" rel="noopener">PURSUE release</a>.</p>
{body}
</main>
<footer class="footer">
  <p>Independent analysis of public records from the <a href="https://www.war.gov/ufo/" rel="noopener">U.S. Department of War PURSUE release</a>. Not affiliated with or endorsed by any government agency.
  Summaries, classifications and assessments are machine-generated research aids — verify against the original files before citing.</p>
</footer>
</body>
</html>
"""


def _report_page(report: Dict[str, Any], compact: Dict[str, Any], base: str) -> str:
    rid = report["id"]
    url = f"{base}/{PAGES_DIR}/{rid}/"
    app_url = f"/#/report/{rid}"
    synthesis = report.get("synthesis") or {}
    manifest = report.get("manifest") or {}
    title_text = report.get("title") or compact.get("title") or rid
    summary = str(synthesis.get("summary") or compact.get("snippet") or "").strip()
    description = _clip(summary, 155) or f"{title_text}: declassified U.S. government UAP record, transcribed and analysed."
    media = compact.get("media") or manifest.get("media_type") or ""
    image = f"{base}/{compact['thumb']}" if compact.get("thumb") else f"{base}/assets/hero-classified-saucer.png"

    facts = [
        ("Releasing agency", compact.get("agency")),
        ("Released", compact.get("release")),
        ("Record type", MEDIA.get(media, media)),
        ("Pages", compact.get("pages")),
        ("Length", f"{round(compact['duration'] / 60)} min" if compact.get("duration") else ""),
        ("What it is", synthesis.get("document_type") or compact.get("doc_type")),
        # a corrected date (the record shows a camera placeholder) is given by its year, not the placeholder text
        ("Date of the record", (synthesis.get("document_date") or {}).get("iso" if (synthesis.get("document_date") or {}).get("corrected") else "text")),
        ("Disposition", DISPOSITIONS.get(compact.get("disposition") or "", "")),
        ("Incidents extracted", compact.get("incidents")),
        ("Themes", ", ".join(compact.get("themes") or [])),
    ]
    facts_html = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in facts if v not in (None, "", 0))

    parts = [f"""<nav class="crumbs small"><a href="/">Home</a> › <a href="/{PAGES_DIR}/">All files</a> › {_e(_clip(title_text, 80))}</nav>
<article>
<header class="panel">
  <div class="eyebrow">{_e(MEDIA.get(media, "Record"))} · source: {_e(compact.get("agency") or "U.S. government")} · released {_e(compact.get("release"))}</div>
  <h1>{_e(title_text)}</h1>
  <div class="cta"><a class="btn primary" href="{_e(app_url)}">Open the interactive report</a>
  {f'<a class="btn" href="{_e(manifest["original_source_url"])}" rel="noopener">Original file ↗</a>' if manifest.get("original_source_url") else ""}</div>
</header>"""]
    if summary:
        parts.append(f'<section class="panel prose"><h2>Summary</h2>{"".join(f"<p>{_e(p)}</p>" for p in summary.split(chr(10)) if p.strip())}'
                     f'<dl class="kv">{facts_html}</dl></section>')
    elif facts_html:
        parts.append(f'<section class="panel"><h2>About this file</h2><dl class="kv">{facts_html}</dl></section>')

    points = [p for p in synthesis.get("key_points") or [] if isinstance(p, dict) and p.get("text")]
    if points:
        parts.append('<section class="panel prose"><h2>Key points</h2><ul class="tight">'
                     + "".join(f"<li>{_e(p['text'])}</li>" for p in points) + "</ul></section>")

    incidents = report.get("incidents") or []
    if incidents:
        rows = []
        for inc in incidents:
            date = (inc.get("date") or {}).get("text") or (inc.get("date") or {}).get("iso") or ""
            place = (inc.get("location") or {}).get("label") or ""
            assess = inc.get("assessment") or {}
            meta = " · ".join(x for x in (date, place, _pages_label(inc.get("pages") or [])) if x)
            explanation = assess.get("best_explanation")
            rows.append(f"""<div class="finding"><h3>{_e(inc.get("title"))}</h3><p class="small muted">{_e(meta)}</p>
<p>{_e(inc.get("narrative"))}</p>{f'<p class="small"><b>Most plausible explanation:</b> {_e(explanation)}</p>' if explanation else ""}</div>""")
        parts.append(f'<section class="panel"><h2>Incidents in this file ({len(incidents)})</h2>{"".join(rows)}</section>')

    passages = [p for p in report.get("key_passages") or []
                if isinstance(p, dict) and p.get("text") and p.get("check") != "unverified"]
    if passages:
        parts.append('<section class="panel prose"><h2>Key passages</h2>' + "".join(
            f'<blockquote><p>“{_e(p["text"])}”</p><p class="small muted">p. {_e(p.get("page"))}{" · close match to page text" if p.get("check") == "approximate" else ""}{" — " + _e(p["why"]) if p.get("why") else ""}</p></blockquote>'
            for p in passages) + "</section>")

    related = report.get("related") or []
    if related:
        items = []
        for r in related:
            why = f' — <span class="muted">{_e(r["why"])}</span>' if r.get("why") else ""
            items.append(f'<li><a href="/{PAGES_DIR}/{_e(r["id"])}/">{_e(r.get("title") or r["id"])}</a>{why}</li>')
        parts.append('<section class="panel"><h2>Related files</h2><ul class="tight">' + "".join(items) + "</ul></section>")

    text_pages = [p for p in report.get("pages") or [] if str(p.get("text") or "").strip()]
    if text_pages:
        used, blocks, cut = 0, [], False
        for page in text_pages:
            text = str(page["text"])
            if used + len(text) > TRANSCRIPT_CHARS:
                cut = True
                break
            used += len(text)
            blocks.append(f'<h3 class="sub-h">{_e(page.get("label") or page.get("n"))}</h3><pre>{_e(text)}</pre>')
        more = f'<p class="muted small">The transcript continues in the <a href="{_e(app_url)}">interactive report</a>.</p>' if cut else ""
        parts.append(f'<section class="panel pagetext"><h2>Transcript</h2><p class="sub">Machine transcription of the released file; check the original before quoting.</p>{"".join(blocks)}{more}</section>')

    parts.append("</article>")
    json_ld = {
        "@context": "https://schema.org", "@type": "Article", "headline": _clip(title_text, 110), "description": description,
        "url": url, "image": image, "datePublished": compact.get("release") or None,
        "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": base + "/"},
        "about": ["Unidentified flying objects", "Unidentified anomalous phenomena"],
    }
    return _shell(f"{_clip(title_text, 70)} | {SITE_NAME}", description, url, image, "\n".join(parts), json_ld)


def _index_page(compacts: List[Dict[str, Any]], base: str) -> str:
    url = f"{base}/{PAGES_DIR}/"
    rows = sorted(compacts, key=lambda r: (r.get("release") or "", r.get("title") or ""), reverse=True)
    items = "".join(
        f'<li><a href="/{PAGES_DIR}/{_e(r["id"])}/">{_e(r.get("title") or r["id"])}</a>'
        f' <span class="muted small">— {_e(r.get("agency"))} · {_e(MEDIA.get(r.get("media") or "", ""))} · released {_e(r.get("release"))}</span></li>'
        for r in rows)
    title = f"All Declassified UFO Files: {len(rows)} Government UAP Records"
    description = (f"Every one of the {len(rows)} declassified U.S. government UFO/UAP files in the PURSUE release, "
                   "with a summary, incidents and transcript for each.")
    body = f"""<section class="panel"><div class="eyebrow">Independent analysis of public government records</div><h1>All declassified UFO files</h1>
<p class="sub">{len(rows)} documents, images, videos and audio recordings, newest release first. Each page has a summary, the incidents it describes and its transcript.
For filters and charts, use the <a href="/#/reports">interactive report browser</a>.</p><ul class="tight file-list">{items}</ul></section>"""
    json_ld = {"@context": "https://schema.org", "@type": "CollectionPage", "name": title, "description": description, "url": url}
    return _shell(title, description, url, f"{base}/assets/hero-classified-saucer.png", body, json_ld)


def write_pages(site: Path, base: str, prune: bool = True) -> List[str]:
    """Write /files/<id>/index.html for every report in the published data plus /files/index.html.
    Returns the page URLs (for the sitemap). Pages for reports no longer published are removed."""
    data_dir = site / "data"
    index = read_json(data_dir / "index.json") or {}
    compacts = {r["id"]: r for r in index.get("reports") or []}
    out = site / PAGES_DIR
    out.mkdir(parents=True, exist_ok=True)
    css = site / "assets" / "app.css"
    _CSS_VERSION[0] = "?v=" + hashlib.sha256(css.read_bytes()).hexdigest()[:10] if css.exists() else ""
    urls = []
    for rid, compact in compacts.items():
        report = read_json(data_dir / "reports" / f"{rid}.json")
        if not report:
            continue
        atomic_write_text(out / rid / "index.html", _report_page(report, compact, base))
        urls.append(f"{base}/{PAGES_DIR}/{rid}/")
    atomic_write_text(out / "index.html", _index_page(list(compacts.values()), base))
    for path in out.iterdir() if prune else ():
        if path.is_dir() and path.name not in compacts:
            shutil.rmtree(path)
    return [f"{base}/{PAGES_DIR}/"] + urls
