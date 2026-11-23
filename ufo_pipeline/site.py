"""Stage 5: publish the static site data.

Writes dashboard/data/index.json (compact overview of every report and incident),
dashboard/data/reports/<id>.json (the full deep report with page text, loaded on demand),
full-text search shards, video frame thumbnails, sitemap and robots.
The HTML/JS/CSS in dashboard/ are hand-written source files and are not generated.
"""

from __future__ import annotations

import datetime as _dt
import io
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import llm
from .analyze import DISPOSITIONS, documentation_score, load_units
from .config import REPO_ROOT, Settings
from .download import find_orphans, load_manifest
from .embed import load_reports
from .geo import Gazetteer
from .util import atomic_write_bytes, atomic_write_json, atomic_write_text, clean_str, log, now_iso, read_json, stable_hash

SITE_DATA_VERSION = 1
SHARD_CHARS = 1_500_000

# Published data names the kind of model that produced each result, never the specific model.
_MODEL_LABEL = re.compile(r"^(remote|local):\S+$")


def model_kinds(settings: Settings) -> Dict[str, str]:
    """Map each configured endpoint label ("remote:<model>") to a generic description of its model type."""
    kinds: Dict[str, str] = {}
    for endpoint in settings.review_chain():
        kinds[endpoint.label] = f"{endpoint.name} vision-language model"
    for endpoint in settings.audio_chain():
        kinds[endpoint.label] = f"{endpoint.name} speech-recognition model"
    if settings.local_embedding and settings.local_embedding.configured:
        kinds[settings.local_embedding.label] = f"{settings.local_embedding.name} text-embedding model"
    return kinds


def _model_kind(label: str, kinds: Dict[str, str]) -> str:
    if label in kinds:
        return kinds[label]
    where, _, name = label.partition(":")
    name = name.lower()
    if re.search(r"asr|whisper|speech|transcri", name):
        return f"{where} speech-recognition model"
    if "embed" in name:
        return f"{where} text-embedding model"
    return f"{where} vision-language model"


def scrub_model_names(obj: Any, kinds: Dict[str, str]) -> Any:
    """Replace every model label recorded in a provider field with its model type, wherever it appears
    (provider fields, counts keyed by provider, error messages). Page text is untouched unless it
    contains a recorded label verbatim."""
    found: set = set()

    def collect(value: Any, in_provider: bool) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                hit = in_provider or "provider" in str(key)
                if in_provider and _MODEL_LABEL.match(str(key)):
                    found.add(str(key))
                collect(item, hit)
        elif isinstance(value, list):
            for item in value:
                collect(item, in_provider)
        elif in_provider and isinstance(value, str) and _MODEL_LABEL.match(value):
            found.add(value)

    collect(obj, False)
    found.update(kinds)
    if not found:
        return obj
    pattern = re.compile("|".join(re.escape(label) for label in sorted(found, key=len, reverse=True)) + r"(?![\w.-])")

    def fix(value: Any) -> Any:
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for key, item in value.items():
                new_key = fix(key) if isinstance(key, str) else key
                item = fix(item)
                if new_key in out and isinstance(item, (int, float)) and isinstance(out[new_key], (int, float)):
                    out[new_key] += item  # two models of the same type: merge their counts
                else:
                    out[new_key] = item
            return out
        if isinstance(value, list):
            return [fix(item) for item in value]
        if isinstance(value, str) and ":" in value:
            return pattern.sub(lambda m: _model_kind(m.group(0), kinds), value)
        return value

    return fix(obj)


ORG_ALIASES = [
    (r"^(u\.?s\.?|united states)?\s*air\s*force$|^usaf$|^department of the air force$|^army air forces?$|^aaf$|^usaaf$", "U.S. Air Force"),
    (r"^(u\.?s\.?|united states)?\s*navy$|^usn$|^department of the navy$", "U.S. Navy"),
    (r"^(u\.?s\.?|united states)?\s*army$|^department of the army$", "U.S. Army"),
    (r"^fbi$|^federal bureau of investigation$|^the bureau$", "Federal Bureau of Investigation (FBI)"),
    (r"^cia$|^central intelligence agency$|^the agency$", "Central Intelligence Agency (CIA)"),
    (r"^aaro$|^all[- ]domain anomaly resolution office.*$", "All-domain Anomaly Resolution Office (AARO)"),
    (r"^nasa$|^national aeronautics and space administration$", "NASA"),
    (r"^(us)?centcom$|^(u\.?s\.?|united states) central command$", "U.S. Central Command (CENTCOM)"),
    (r"^dod$|^department of defense$|^department of war$|^dow$|^war department$", "Department of War / Defense"),
    (r"^odni$|^office of the director of national intelligence$", "Office of the Director of National Intelligence (ODNI)"),
    (r"^(air )?(materiel|material) command$|^amc$", "Air Materiel Command"),
    (r"^atic$|^air technical intelligence center$", "Air Technical Intelligence Center (ATIC)"),
    (r"^osi$|^(air force )?office of special investigations$|^afosi$", "Air Force Office of Special Investigations (OSI)"),
    (r"^project blue ?book$|^blue ?book$", "Project Blue Book"),
    (r"^doe$|^department of energy$", "Department of Energy"),
    (r"^(department of state|state department|dos)$", "Department of State"),
    (r"^faa$|^federal aviation administration$", "Federal Aviation Administration (FAA)"),
    (r"^caa$|^civil aeronautics administration$", "Civil Aeronautics Administration (CAA)"),
    (r"^norad$|^north american (air|aerospace) defense command$", "NORAD"),
]
_ORG_RULES = [(re.compile(pattern, re.IGNORECASE), name) for pattern, name in ORG_ALIASES]

CORPUS_BATCH_PROMPT = """You are helping write an evidence-based overview of a corpus of declassified U.S. government UAP/UFO records.
Below are {count} per-file research summaries (id | title | kind | disposition | summary).

Return ONE JSON object:
{{"observations": [{{"text": "a specific, supportable observation about what these files show (patterns in what was reported, how agencies handled it, what explanations recur, what is missing)", "report_ids": ["ids that support it"]}}],  // 4-8
  "notable": [{{"id": "", "why": "why a researcher should read this file first"}}]}}  // up to 5

Use only the ids given. Do not speculate beyond the summaries.

SUMMARIES:
{rows}"""

CORPUS_FINAL_PROMPT = """You are writing the corpus-level overview for a research dashboard of {reports} declassified U.S. government UAP/UFO files ({incidents} extracted incidents) released through the Department of War PURSUE program.

CORPUS STATISTICS:
{stats}

OBSERVATIONS FROM BATCHES OF FILES (with supporting report ids):
{observations}

Write ONE JSON object:
{{"overview": "3 paragraphs: what the corpus contains, what the records collectively show, and the limits of what can be concluded. Sober, precise, no hype; distinguish what records claim from what is established.",
  "findings": [{{"title": "short headline", "text": "2-4 sentences", "report_ids": ["supporting ids"]}}],  // 6-9 cross-cutting findings
  "caveats": ["specific limitations of this corpus and of machine-assisted analysis"],  // 3-6
  "notable": [{{"id": "", "why": ""}}]}}  // 8-12 files most worth reading
Use only report ids that appear above."""


def canonical_org(name: str) -> str:
    text = clean_str(name)
    if not text:
        return ""
    for rule, canonical in _ORG_RULES:
        if rule.match(text):
            return canonical
    return text


def release_iso(value: Any) -> str:
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", str(value or ""))
    if not match:
        return ""
    year = int(match.group(3))
    year = year + 2000 if year < 100 else year
    try:
        return _dt.date(year, int(match.group(1)), int(match.group(2))).isoformat()
    except ValueError:
        return ""


def _primary_year(report: Dict[str, Any]) -> Optional[int]:
    years = [i["date"]["year"] for i in report.get("incidents") or [] if i["date"]["year"]]
    if years:
        return Counter(years).most_common(1)[0][0]
    synthesis = report.get("synthesis") or {}
    year = (synthesis.get("document_date") or {}).get("year")
    if year and year < 2026:
        return year
    start = (synthesis.get("period_covered") or {}).get("start") or ""
    return int(start[:4]) if start[:4].isdigit() else year


def compact_report(report: Dict[str, Any], frames: List[str]) -> Dict[str, Any]:
    synthesis = report.get("synthesis") or {}
    manifest = report.get("manifest") or {}
    extraction = report.get("extraction") or {}
    assessment = synthesis.get("assessment") or {}
    incidents = report.get("incidents") or []
    quality = report.get("quality") or {}
    scores = [i["documentation"]["score"] for i in incidents]
    summary = synthesis.get("summary") or manifest.get("description_blurb") or ""
    return {
        "id": report["id"],
        "title": synthesis.get("title") or manifest.get("title") or report["filename"],
        "pub_title": manifest.get("title") or "",
        "file": report["filename"],
        "media": manifest.get("media_type") or "",
        "agency": manifest.get("agency") or "",
        "release": release_iso(manifest.get("release_date")),
        "kind": synthesis.get("record_kind") or "other",
        "doc_type": synthesis.get("document_type") or "",
        "year": _primary_year(report),
        "doc_date": (synthesis.get("document_date") or {}).get("iso") or "",
        "disposition": assessment.get("disposition") or "",
        "hynek": (synthesis.get("ratings") or {}).get("hynek") or "",
        "themes": synthesis.get("themes") or [],
        "redaction": (synthesis.get("redaction") or {}).get("level") or "",
        "pages": extraction.get("page_count") if manifest.get("media_type") in ("pdf", "image") else None,
        "duration": extraction.get("duration"),
        "incidents": len(incidents),
        "value": assessment.get("research_value"),
        "doc_score": max(scores) if scores else None,
        "status": report.get("status"),
        "snippet": re.sub(r"\s+", " ", summary)[:280],
        "thumb": frames[0] if frames else "",
        "countries": sorted({i["location"]["country"] for i in incidents if i["location"]["country"]}),
        "unverified_quotes": quality.get("quotes_unverified", 0),
        "upstream": manifest.get("upstream_status") or "listed",
    }


def compact_incident(incident: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    location = incident["location"]
    assessment = incident["assessment"]
    return {
        "id": incident["id"], "report": report["id"], "title": incident["title"],
        "date": incident["date"]["iso"], "date_text": incident["date"]["text"], "year": incident["date"]["year"],
        "date_precision": incident["date"]["precision"],
        "place": location["label"], "lat": location["lat"], "lon": location["lon"], "place_precision": location["precision"],
        "country": location["country"], "region": location["region"],
        "shape": incident["object"]["shape"], "modes": incident["observation"]["modes"],
        "witnesses": sorted({w["category"] for w in incident["witnesses"]}),
        "witness_count": sum(w["count"] for w in incident["witnesses"]),
        "status": incident["status_in_record"], "hynek": assessment["hynek"],
        "explanation": assessment["explanation_category"], "likelihood": assessment["likelihood"],
        "strangeness": assessment["strangeness"], "score": incident["documentation"]["score"],
        "band": incident["documentation"]["band"], "agency": (report.get("manifest") or {}).get("agency") or "",
        "media": (report.get("manifest") or {}).get("media_type") or "", "light": incident["conditions"]["light"],
        "pages": incident["pages"][:2], "snippet": incident["narrative"][:240],
    }


def _thumbnails(report: Dict[str, Any], settings: Settings, data_dir: Path) -> List[str]:
    """Copy up to 6 sampled video frames as small JPEGs; return site-relative paths."""
    stem = Path(report["filename"]).stem
    cache = read_json(settings.media_dir / f"{stem}.json") or {}
    frames = cache.get("frames") or []
    if not frames:
        return []
    from PIL import Image

    source_dir = settings.media_dir / "frames" / stem
    target_dir = data_dir / "frames" / report["id"]
    step = max(1, len(frames) // 6)
    out: List[str] = []
    for frame in frames[::step][:6]:
        source = source_dir / frame["file"]
        target = target_dir / frame["file"]
        if not source.exists():
            continue
        if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
            with Image.open(source) as image:
                image = image.convert("RGB")
                image.thumbnail((640, 640))
                buffer = io.BytesIO()
                image.save(buffer, "JPEG", quality=72, optimize=True)
            atomic_write_bytes(target, buffer.getvalue())
        out.append(f"data/frames/{report['id']}/{frame['file']}|{frame['t']}")
    return out


def _corpus_summary(reports: List[Dict[str, Any]], stats: Dict[str, Any], settings: Settings) -> Dict[str, Any]:
    """Model-written synthesis built from per-report summaries in batches (no single giant prompt)."""
    cache_path = settings.cache_dir / "corpus_summary.json"
    rows = []
    for report in reports:
        synthesis = report.get("synthesis") or {}
        if synthesis.get("summary"):
            rows.append((report["id"], f"{report['id']} | {synthesis.get('title')} | {synthesis.get('record_kind')} | "
                         f"{(synthesis.get('assessment') or {}).get('disposition')} | {re.sub(chr(10), ' ', synthesis['summary'])[:700]}"))
    signature = stable_hash([SITE_DATA_VERSION, [r[1] for r in rows]], 24)
    cached = read_json(cache_path)
    if cached and cached.get("signature") == signature:
        return cached["summary"]
    chain = llm.Chain(settings.review_chain(), timeout=settings.request_timeout)
    if not chain or not rows:
        return (cached or {}).get("summary") or {}
    valid = {r[0] for r in rows}
    batch_cache = (cached or {}).get("batches") or {}
    new_batches: Dict[str, Any] = {}
    observations: List[Dict[str, Any]] = []
    notable: List[Dict[str, Any]] = []
    for start in range(0, len(rows), 40):
        batch = rows[start:start + 40]
        key = stable_hash([r[1] for r in batch], 20)
        data = batch_cache.get(key)
        if data is None:
            try:
                data, _provider = chain.json([{"role": "user", "content": CORPUS_BATCH_PROMPT.format(count=len(batch), rows="\n".join(r[1] for r in batch))}],
                                             max_tokens=8000, required_keys=("observations",))
            except llm.LLMError as exc:
                log(f"WARNING: corpus summary batch {start // 40 + 1} failed: {str(exc)[:160]}")
                continue
        new_batches[key] = data
        observations += [o for o in data.get("observations") or [] if isinstance(o, dict)]
        notable += [n for n in data.get("notable") or [] if isinstance(n, dict)]
    if not observations:
        return (cached or {}).get("summary") or {}
    obs_text = "\n".join(f"- {clean_str(o.get('text'), 500)} [{', '.join(i for i in o.get('report_ids') or [] if i in valid)[:400]}]" for o in observations)
    obs_text += "\nFILES FLAGGED AS NOTABLE:\n" + "\n".join(f"- {n.get('id')}: {clean_str(n.get('why'), 200)}" for n in notable if n.get("id") in valid)
    try:
        data, provider = chain.json([{"role": "user", "content": CORPUS_FINAL_PROMPT.format(
            reports=stats["reports"], incidents=stats["incidents"], stats="\n".join(f"- {k}: {v}" for k, v in stats.items()),
            observations=obs_text[:90000])}], max_tokens=10000, required_keys=("overview",))
    except llm.LLMError as exc:
        log(f"WARNING: corpus summary failed: {str(exc)[:160]}")
        atomic_write_json(cache_path, {"signature": "", "batches": new_batches, "summary": (cached or {}).get("summary") or {}})
        return (cached or {}).get("summary") or {}
    summary = {
        "overview": str(data.get("overview") or "").strip(),
        "findings": [{"title": clean_str(f.get("title"), 160), "text": clean_str(f.get("text"), 900),
                      "report_ids": [i for i in f.get("report_ids") or [] if i in valid][:12]}
                     for f in data.get("findings") or [] if isinstance(f, dict) and clean_str(f.get("text"))][:10],
        "caveats": [clean_str(c, 400) for c in data.get("caveats") or [] if clean_str(c)][:8],
        "notable": [{"id": n["id"], "why": clean_str(n.get("why"), 300)} for n in data.get("notable") or []
                    if isinstance(n, dict) and n.get("id") in valid][:12],
        "provider": provider, "generated": now_iso(),
    }
    atomic_write_json(cache_path, {"signature": signature, "batches": new_batches, "summary": summary})
    return summary


def run(settings: Settings, corpus_summary: bool = True, partial: bool = False) -> Dict[str, Any]:
    site = settings.site_dir
    data_dir = site / "data"
    reports_dir = data_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(settings)
    by_file = {str(e["filename"]): e for e in manifest}
    gazetteer = Gazetteer()
    kinds = model_kinds(settings)
    relations = read_json(settings.cache_dir / "relations.json") or {}
    related = relations.get("related_reports") or {}
    same_event = relations.get("same_event") or {}

    reports = [r for r in load_reports(settings) if r["filename"] in by_file]
    analyzed = {r["filename"] for r in reports}
    pending = sorted(name for name in by_file if name not in analyzed)
    if not reports:
        raise SystemExit("No analyzed reports found. Run the extract and analyze stages first.")
    if pending and not partial:
        log(f"WARNING: {len(pending)} manifest sources have no analysis yet and will be listed as pending")

    index_reports: List[Dict[str, Any]] = []
    index_incidents: List[Dict[str, Any]] = []
    incident_titles: Dict[str, Dict[str, str]] = {}
    for report in reports:
        for incident in report.get("incidents") or []:
            raw = incident["location"].get("raw")
            if raw:  # re-resolve with the current gazetteer logic; cheap and deterministic
                place = gazetteer.resolve(raw.get("name"), raw.get("region"), raw.get("country"), raw.get("lat"), raw.get("lon"))
                place.update({"refers_to": incident["location"].get("refers_to", "unclear"), "raw": raw})
                incident["location"] = place
                incident["documentation"] = documentation_score(incident)
            incident_titles[incident["id"]] = {"id": incident["id"], "title": incident["title"], "report": report["id"]}
    titles = {r["id"]: (r.get("synthesis") or {}).get("title") or (r.get("manifest") or {}).get("title") or r["filename"] for r in reports}

    people: Dict[str, Dict[str, Any]] = {}
    orgs: Dict[str, set] = {}
    programs: Dict[str, set] = {}
    equipment: Dict[str, set] = {}
    shard: List[Dict[str, Any]] = []
    shard_size = 0
    shard_files: List[str] = []
    search_dir = data_dir / "search"
    search_dir.mkdir(parents=True, exist_ok=True)
    quality = Counter()
    methods = Counter()
    providers = Counter()
    issues: List[Dict[str, Any]] = []

    def flush_shard() -> None:
        nonlocal shard, shard_size
        if shard:
            name = f"shard-{len(shard_files):03d}.json"
            atomic_write_json(search_dir / name, shard)
            shard_files.append(f"data/search/{name}")
            shard, shard_size = [], 0

    for report in reports:
        entry = by_file[report["filename"]]
        units, _info = load_units(entry, settings)
        frames = _thumbnails(report, settings, data_dir)
        compact = compact_report(report, [f.split("|")[0] for f in frames])
        index_reports.append(compact)
        for incident in report.get("incidents") or []:
            index_incidents.append(compact_incident(incident, report))

        entities = report.get("entities") or {}
        for person in entities.get("people") or []:
            key = person["name"].lower()
            row = people.setdefault(key, {"name": person["name"], "role": person.get("role") or "", "org": canonical_org(person.get("org") or ""), "reports": set()})
            row["reports"].add(report["id"])
        for name in entities.get("organizations") or []:
            orgs.setdefault(canonical_org(name), set()).add(report["id"])
        for name in entities.get("programs") or []:
            programs.setdefault(name, set()).add(report["id"])
        for name in entities.get("aircraft_equipment") or []:
            equipment.setdefault(name, set()).add(report["id"])

        extraction = report.get("extraction") or {}
        for method, count in (extraction.get("methods") or {}).items():
            methods[method] += count
        for provider, count in (extraction.get("providers") or {}).items():
            providers[provider] += count
        q = report.get("quality") or {}
        for key in ("quotes_total", "quotes_verified", "quotes_approximate", "quotes_unverified"):
            quality[key] += q.get(key, 0)
        problems = []
        if extraction.get("failed_pages"):
            problems.append(f"{len(extraction['failed_pages'])} page(s) could not be read by any OCR path")
        for chunk in q.get("chunks_failed") or []:
            problems.append(f"pages {chunk['pages'][0]}-{chunk['pages'][1]} were not analyzed")
        if q.get("synthesis_error"):
            problems.append("document-level synthesis failed")
        problems += list(extraction.get("issues") or [])
        if problems:
            issues.append({"id": report["id"], "title": compact["title"], "problems": problems})

        # full report file, with page text for the in-report reader
        full = dict(report)
        full.pop("input_key", None)
        full["title"] = compact["title"]
        full["release"] = compact["release"]
        full["pages"] = [{"n": u["n"], "label": u["label"], "text": u["text"], "method": u["method"], "provider": u["provider"],
                          "flags": u["flags"], "legibility": u["legibility"], "redactions": u["redactions"],
                          "handwriting": u["handwriting"]} for u in units]
        full["frames"] = [{"src": f.split("|")[0], "t": float(f.split("|")[1])} for f in frames]
        media_cache = read_json(settings.media_dir / f"{Path(report['filename']).stem}.json") if compact["media"] in ("video", "audio") else None
        if media_cache and media_cache.get("frame_review"):
            full["frame_review"] = media_cache["frame_review"]
        full["related"] = [{"id": link["id"], "title": titles.get(link["id"], link["id"]), "score": link["score"], "why": link["why"]}
                           for link in related.get(report["id"], []) if link["id"] in titles]
        for incident in full.get("incidents") or []:
            incident["same_event"] = [incident_titles[i] for i in same_event.get(incident["id"], []) if i in incident_titles]
            if incident["location"]["country"]:
                incident["location"]["country_name"] = gazetteer.country_name(incident["location"]["country"])
        atomic_write_json(reports_dir / f"{report['id']}.json", scrub_model_names(full, kinds))

        for unit in units:
            if unit["text"].strip():
                shard.append({"r": report["id"], "p": unit["n"], "l": unit["label"], "t": unit["text"]})
                shard_size += len(unit["text"])
                if shard_size >= SHARD_CHARS:
                    flush_shard()
    flush_shard()

    pending_rows = [{"file": name, "title": by_file[name].get("title") or name, "media": by_file[name].get("media_type"),
                     "agency": by_file[name].get("agency"), "release": release_iso(by_file[name].get("release_date"))} for name in pending]

    def top(table: Dict[str, set], limit: int, minimum: int = 1) -> List[Dict[str, Any]]:
        rows = [{"name": name, "reports": sorted(ids)} for name, ids in table.items() if name and len(ids) >= minimum]
        rows.sort(key=lambda r: (-len(r["reports"]), r["name"]))
        return rows[:limit]

    people_rows = [{"name": p["name"], "role": p["role"], "org": p["org"], "reports": sorted(p["reports"])} for p in people.values()]
    people_rows.sort(key=lambda r: (-len(r["reports"]), r["name"]))

    releases = Counter(r["release"] for r in index_reports if r["release"])
    stats = {
        "reports": len(index_reports), "incidents": len(index_incidents),
        "pages": sum(r["pages"] or 0 for r in index_reports),
        "media_minutes": round(sum(r["duration"] or 0 for r in index_reports) / 60),
        "by_media": dict(Counter(r["media"] for r in index_reports)),
        "by_agency": dict(Counter(r["agency"] for r in index_reports).most_common()),
        "by_disposition": {d: sum(1 for r in index_reports if r["disposition"] == d) for d in DISPOSITIONS},
        "incident_years": f"{min((i['year'] for i in index_incidents if i['year']), default='?')}-{max((i['year'] for i in index_incidents if i['year']), default='?')}",
        "incidents_by_status": dict(Counter(i["status"] for i in index_incidents)),
        "incidents_by_explanation": dict(Counter(i["explanation"] for i in index_incidents).most_common()),
        "incidents_with_instrument_data": sum(1 for i in index_incidents if any(m.startswith(("radar", "infrared", "photo", "film", "sonar", "satellite")) for m in i["modes"])),
    }
    summary = _corpus_summary(reports, stats, settings) if corpus_summary else ((read_json(settings.cache_dir / "corpus_summary.json") or {}).get("summary") or {})

    index = {
        "version": SITE_DATA_VERSION, "generated": now_iso(), "partial": partial,
        "source": {"name": "U.S. Department of War PURSUE release", "url": "https://www.war.gov/ufo/"},
        "stats": stats,
        "releases": [{"date": date, "count": count} for date, count in sorted(releases.items())],
        "reports": index_reports, "incidents": index_incidents, "pending": pending_rows,
        "entities": {"people": people_rows[:2500], "organizations": top(orgs, 600), "programs": top(programs, 300),
                     "equipment": top(equipment, 300)},
        "summary": summary,
        "search_shards": shard_files,
        "quality": {
            "ocr_methods": dict(methods), "ocr_providers": dict(providers), **dict(quality),
            "reports_complete": sum(1 for r in index_reports if r["status"] == "complete"),
            "reports_incomplete": sum(1 for r in index_reports if r["status"] != "complete"),
            "pending_sources": len(pending), "ignored_files": len(find_orphans(settings, manifest)),
            "located_incidents": sum(1 for i in index_incidents if i["lat"] is not None),
            "dated_incidents": sum(1 for i in index_incidents if i["year"]),
            "issues": issues,
            "models": {"review": [kinds[e.label] for e in settings.review_chain()], "audio": [kinds[e.label] for e in settings.audio_chain()],
                       "embedding": kinds.get(settings.local_embedding.label, "") if settings.local_embedding else ""},
        },
    }
    atomic_write_json(data_dir / "index.json", scrub_model_names(index, kinds))

    # remove outputs for sources that no longer exist (only when building the whole corpus)
    if not partial:
        live = {f"{r['id']}.json" for r in reports}
        for path in reports_dir.glob("*.json"):
            if path.name not in live:
                path.unlink()
        live_shards = {Path(s).name for s in shard_files}
        for path in search_dir.glob("shard-*.json"):
            if path.name not in live_shards:
                path.unlink()
        frames_root = data_dir / "frames"
        if frames_root.exists():
            ids = {r["id"] for r in reports}
            for path in frames_root.iterdir():
                if path.is_dir() and path.name not in ids:
                    shutil.rmtree(path)

    land = REPO_ROOT / "ufo_pipeline" / "assets" / "ne_110m_land.geojson"
    if land.exists():
        (site / "assets").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(land, site / "assets" / "land.geojson")
    base = settings.site_url.rstrip("/")
    atomic_write_text(site / "robots.txt", f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n")
    atomic_write_text(site / "sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                      f"  <url><loc>{base}/</loc><lastmod>{_dt.date.today().isoformat()}</lastmod></url>\n</urlset>\n")
    result = {"reports": len(index_reports), "incidents": len(index_incidents), "pending": len(pending),
              "issues": len(issues), "shards": len(shard_files), "corpus_summary": bool(summary)}
    log(f"Build finished: {result}")
    return result
