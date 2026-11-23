"""Stage 5: publish the static site data.

Writes dashboard/data/index.json (compact overview of every report and incident),
dashboard/data/reports/<id>.json (the full deep report with page text, loaded on demand),
full-text search shards, video frame thumbnails, static per-report pages (pages.py), sitemap and robots.
The HTML/JS/CSS in dashboard/ are hand-written source files and are not generated.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import io
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import connections, consistency, llm
from .analyze import _release_year, documentation_score, load_units
from .config import REPO_ROOT, Settings
from .download import find_orphans, load_manifest
from .embed import _decode, incident_text, load_reports
from .geo import Gazetteer
from .pages import write_pages
from .util import atomic_write_bytes, atomic_write_json, atomic_write_text, clean_str, log, now_iso, read_json, stable_hash

SITE_DATA_VERSION = 1
SUMMARY_PROMPT_VERSION = 2  # bump to regenerate the corpus synthesis after changing its prompts or statistics
SHARD_CHARS = 1_500_000

# Published data names the kind of model that produced each result, never the specific model.
_MODEL_LABEL = re.compile(r"^(remote|local):\S+$")


def model_kinds(settings: Settings) -> Dict[str, str]:
    """Map each configured endpoint label ("remote:<model>") to its model type (no model name, no remote/local)."""
    kinds: Dict[str, str] = {}
    for endpoint in settings.review_chain():
        kinds[endpoint.label] = "vision-language model"
    for endpoint in settings.audio_chain():
        kinds[endpoint.label] = "speech-recognition model"
    if settings.local_embedding and settings.local_embedding.configured:
        kinds[settings.local_embedding.label] = "text-embedding model"
    return kinds


def _model_kind(label: str, kinds: Dict[str, str]) -> str:
    if label in kinds:
        return kinds[label]
    name = label.partition(":")[2].lower()
    if re.search(r"asr|whisper|speech|transcri", name):
        return "speech-recognition model"
    if "embed" in name:
        return "text-embedding model"
    return "vision-language model"


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
Below are {count} per-file research summaries (id | title | kind | disposition | summary). They are batch {batch} of {batches}: a slice of a corpus of {total} files, not the whole corpus.

Return ONE JSON object:
{{"observations": [{{"text": "a specific, supportable observation about what these files show (patterns in what was reported, how agencies handled it, what explanations recur, what is missing from these files)", "report_ids": ["ids that support it"]}}],  // 4-8
  "notable": [{{"id": "", "why": "why a researcher should read this file first"}}]}}  // up to 5

Because you see only a slice of the corpus:
- do not say how many files, videos or incidents the corpus contains, and do not give totals for anything wider than the files you cite;
- do not say what the corpus lacks, which years or places it does not cover, or that something is true of "all", "every", "no" or "exclusively" some kind of file;
- do not call a file the only, first, largest, longest or most substantial of its kind.
Use only the ids given. Do not speculate beyond the summaries.

SUMMARIES:
{rows}"""

CORPUS_FINAL_PROMPT = """You are writing the corpus-level overview for a research dashboard of {reports} declassified U.S. government UAP/UFO files released through the Department of War PURSUE program. The files hold {incidents} incident accounts describing about {events} distinct events (the same event is often reported in several files).

CORPUS STATISTICS (computed from the data; authoritative):
{stats}

OBSERVATIONS FROM BATCHES OF FILES (with supporting report ids). Each was written from a batch of about 40 files, not from the corpus:
{observations}

Write ONE JSON object:
{{"overview": "3 paragraphs: what the corpus contains, what the records collectively show, and the limits of what can be concluded. Sober, precise, no hype; distinguish what records claim from what is established.",
  "findings": [{{"title": "short headline", "text": "2-4 sentences", "report_ids": ["supporting ids"]}}],  // 6-9 cross-cutting findings
  "caveats": ["specific limitations of this corpus and of machine-assisted analysis"],  // 3-6
  "notable": [{{"id": "", "why": ""}}]}}  // 8-12 files most worth reading

RULES FOR ACCURACY
- Every count, total, share and year range you state about the corpus must appear in CORPUS STATISTICS. Never take a count from an observation: "the 40 video releases" or "no coverage of 1958-2007" there describes one batch, not the corpus.
- Before describing coverage over time, geography, media or agencies, check events_by_decade, events_by_us_state, events_by_country, files_by_release and video_files_by_agency. Drop any claim the statistics do not support.
- No absolutes or superlatives about the corpus (the only, the most substantial, exclusively, overwhelmingly, no file, none, every) unless the statistics prove them. Say "many", "several" or "in the files cited" instead.
- Units: every statistic whose name starts with "events_" counts DISTINCT EVENTS (out of distinct_events) and must be written as events, never as accounts. Only "incidents" (all accounts) and "repeat_accounts" count accounts. Never put an events figure over the accounts total or the reverse.
- "status" is what each record itself concludes; most accounts were never evaluated in the released record. That is not the same as officially unidentified.
- In "notable", the facts in brackets after each flagged file (agency; size; incident accounts) are authoritative. Do not state any other size, do not compare a file's size with the rest of the corpus, and do not copy the bracketed facts into "why" (the dashboard shows them already).
- Write for a reader of the dashboard, who has not seen this prompt. Never mention batches, slices, observations, these rules or "the statistics". Use plain words for categories ("not evaluated in the record", "insufficient information", "meteor or fireball"), not the hyphenated codes.
- Image files are not audio or video; keep the media types apart.
- The original video and audio recordings ARE part of the public release. This analysis reviewed them through derived material (the publisher's description, sampled frames, captions and machine transcripts). Present that as a limit of this machine analysis; never say or imply that recordings are unavailable, withheld or released only as summaries, and do not attribute that limit to AARO or the publisher.
- Use plain words for file dispositions as well ("explained in the record", "unexplained in the record", "not a sighting record").
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
    event = incident.get("event") or {}
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
        # accounts of one event in several files: every account is listed, one per event is counted
        "repeat": bool(event.get("repeat")), "accounts": event.get("accounts", 1), "event": event.get("primary") or incident["id"],
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


def _summary_rows(reports: List[Dict[str, Any]]) -> List[tuple]:
    rows = []
    for report in reports:
        synthesis = report.get("synthesis") or {}
        if synthesis.get("summary"):
            rows.append((report["id"], f"{report['id']} | {synthesis.get('title')} | {synthesis.get('record_kind')} | "
                         f"{(synthesis.get('assessment') or {}).get('disposition')} | {re.sub(chr(10), ' ', synthesis['summary'])[:700]}"))
    return rows


def _summary_signature(rows: List[tuple], stats: Dict[str, Any]) -> str:
    """What the cached synthesis was written from. The final prompt's own text is included, so editing it rewrites
    the synthesis (from the cached batch observations); so do changed statistics or per-file summaries."""
    return stable_hash([SITE_DATA_VERSION, SUMMARY_PROMPT_VERSION, CORPUS_FINAL_PROMPT, stats, [r[1] for r in rows]], 24)


def _whole_sentences(text: str, limit: int) -> str:
    """Shorten to at most `limit` characters without publishing half a sentence."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[:end + 1] if end > limit // 3 else cut[:cut.rfind(" ")].rstrip(",;:") + "…"


def _corpus_summary(reports: List[Dict[str, Any]], stats: Dict[str, Any], settings: Settings) -> Dict[str, Any]:
    """Model-written synthesis built from per-report summaries in batches (no single giant prompt)."""
    cache_path = settings.cache_dir / "corpus_summary.json"
    rows = _summary_rows(reports)
    signature = _summary_signature(rows, stats)
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
    batches = (len(rows) + 39) // 40
    for start in range(0, len(rows), 40):
        batch = rows[start:start + 40]
        key = stable_hash([SUMMARY_PROMPT_VERSION, len(rows), [r[1] for r in batch]], 20)
        data = batch_cache.get(key)
        if data is None:
            try:
                data, _provider = chain.json([{"role": "user", "content": CORPUS_BATCH_PROMPT.format(
                    count=len(batch), batch=start // 40 + 1, batches=batches, total=len(rows), rows="\n".join(r[1] for r in batch))}],
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
    facts = {}
    for report in reports:
        extraction, manifest = report.get("extraction") or {}, report.get("manifest") or {}
        size = f"{extraction.get('page_count')} pages" if manifest.get("media_type") in ("pdf", "image") else f"{manifest.get('media_type')} recording"
        facts[report["id"]] = f"{manifest.get('agency') or 'agency not stated'}; {size}; {len(report.get('incidents') or [])} incident accounts"
    obs_text += "\nFILES FLAGGED AS NOTABLE (id [agency; size; incident accounts]: why):\n" + "\n".join(
        f"- {n.get('id')} [{facts.get(n.get('id'), '')}]: {clean_str(n.get('why'), 200)}" for n in notable if n.get("id") in valid)
    try:
        data, provider = chain.json([{"role": "user", "content": CORPUS_FINAL_PROMPT.format(
            reports=stats["reports"], incidents=stats["incidents"], events=stats["distinct_events"],
            stats="\n".join(f"- {k}: {v}" for k, v in stats.items()),
            observations=obs_text[:90000])}], max_tokens=10000, required_keys=("overview",))
    except llm.LLMError as exc:
        log(f"WARNING: corpus summary failed: {str(exc)[:160]}")
        atomic_write_json(cache_path, {"signature": "", "batches": new_batches, "summary": (cached or {}).get("summary") or {}})
        return (cached or {}).get("summary") or {}
    summary = {
        "overview": str(data.get("overview") or "").strip(),
        "findings": [{"title": clean_str(f.get("title"), 160), "text": _whole_sentences(clean_str(f.get("text")), 900),
                      "report_ids": [i for i in f.get("report_ids") or [] if i in valid][:12]}
                     for f in data.get("findings") or [] if isinstance(f, dict) and clean_str(f.get("text"))][:10],
        "caveats": [_whole_sentences(clean_str(c), 500) for c in data.get("caveats") or [] if clean_str(c)][:8],
        "notable": [{"id": n["id"], "why": _whole_sentences(clean_str(re.sub(r"^\s*\[[^\]]*\]\s*", "", str(n.get("why") or ""))), 320)} for n in data.get("notable") or []
                    if isinstance(n, dict) and n.get("id") in valid][:12],
        "provider": provider, "generated": now_iso(),
    }
    unverified = audit_numbers(summary, stats, reports)
    for number, context in unverified:
        log(f"WARNING: corpus summary states {number}, which is not a corpus statistic - check: \"...{context}...\"")
    atomic_write_json(cache_path, {"signature": signature, "batches": new_batches, "summary": summary,
                                   "unverified_numbers": [{"number": n, "context": c} for n, c in unverified]})
    return summary


def audit_numbers(summary: Dict[str, Any], stats: Dict[str, Any], reports: List[Dict[str, Any]]) -> List[tuple]:
    """Numbers in the model-written synthesis that are neither a corpus statistic nor a fact about a cited file.

    A report for the person running the build, not a filter: a flagged number may be a legitimate sum or a figure
    quoted from a record, but every one deserves a look before publishing."""
    known: set = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                collect(key)
                collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)
        elif isinstance(value, bool):
            return
        elif isinstance(value, (int, float)):
            known.add(int(value))
        elif isinstance(value, str):
            known.update(int(n) for n in re.findall(r"\d+", value.replace(",", "")))

    collect(stats)
    for report in reports:
        known.add(int((report.get("extraction") or {}).get("page_count") or 0))
        known.add(len(report.get("incidents") or []))
    texts = [summary.get("overview") or ""] + [f"{f.get('title')} {f.get('text')}" for f in summary.get("findings") or []]
    texts += list(summary.get("caveats") or []) + [n.get("why") or "" for n in summary.get("notable") or []]
    flagged = []
    # unit mix-ups: only the account total, the repeat count and per-file counts may be called "accounts"
    account_counts = {int(stats.get("incidents") or 0), int(stats.get("repeat_accounts") or 0)} | {len(r.get("incidents") or []) for r in reports}
    for text in texts:
        for m in re.finditer(r"(\d[\d,]*)(?:\s+of\s+[\d,]+)?\s+(?:incident\s+|repeat\s+)?accounts\b", text):
            number = int(m.group(1).replace(",", ""))
            if number >= 13 and number not in account_counts:
                flagged.append((number, "counted as accounts but is not an account total: " + re.sub(r"\s+", " ", text[max(0, m.start() - 40):m.end() + 40])))
    for text in texts:
        for m in re.finditer(r"(?<![\w.$/-])\d[\d,]*(?![\w/-]|\.\d)", text):
            number = int(m.group(0).replace(",", ""))
            if number < 13 or 1800 <= number <= 2100 or number in known:
                continue
            flagged.append((number, re.sub(r"\s+", " ", text[max(0, m.start() - 60):m.end() + 40])))
    return flagged


def _incident_similarity(settings: Settings, reports: List[Dict[str, Any]]):
    """Cosine similarity of two incidents' cached embeddings, by incident id (None when either has no vector)."""
    import numpy as np

    vectors = (read_json(settings.embeddings_path) or {}).get("vectors") or {}
    keys = {inc["id"]: stable_hash(incident_text(inc), 24) for report in reports for inc in report.get("incidents") or []}
    unit: Dict[str, Any] = {}

    def vector(incident_id: str):
        if incident_id not in unit:
            raw = vectors.get(keys.get(incident_id, ""))
            value = None
            if raw:
                value = _decode(raw).astype("float32")
                value = value / max(float(np.linalg.norm(value)), 1e-9)
            unit[incident_id] = value
        return unit[incident_id]

    def similarity(a: str, b: str) -> Optional[float]:
        va, vb = vector(a), vector(b)
        return None if va is None or vb is None else float(va @ vb)

    return similarity


def hide_unmatched_quotes(report: Dict[str, Any]) -> Dict[str, Any]:
    """Drop quotes that could not be found in the page text (check == "unverified") from a published report.
    They are still counted in the quality figures; only matched (exact or approximate) quotes are shown."""
    keep = lambda quotes: [q for q in quotes or [] if not (isinstance(q, dict) and q.get("check") == "unverified")]
    report = dict(report)
    report["key_passages"] = keep(report.get("key_passages"))
    report["incidents"] = [dict(inc, quotes=keep(inc.get("quotes"))) if isinstance(inc, dict) else inc
                           for inc in report.get("incidents") or []]
    return report


def seo_description(stats: Dict[str, Any]) -> str:
    """Search-result description for the home page, with counts from the current build."""
    pages = int(stats.get("pages") or 0)
    pages_text = f"{pages // 1000 * 1000:,}+" if pages >= 1000 else f"{pages:,}"
    return (f"Search {stats.get('reports', 0):,} declassified U.S. government UFO/UAP files: {pages_text} pages, "
            "videos and audio from the Department of War PURSUE release, transcribed and analysed.")


def _update_page_meta(page: Path, description: str, base: str) -> None:
    """Refresh the description and canonical/social URLs in the hand-written index.html."""
    if not page.exists():
        return
    html = page.read_text(encoding="utf-8")
    safe = description.replace("&", "&amp;").replace('"', "&quot;")
    html = re.sub(r'(<meta (?:name="description"|property="og:description"|name="twitter:description") content=")[^"]*(")',
                  lambda m: m.group(1) + safe + m.group(2), html)
    html = re.sub(r'(<link rel="canonical" href="|<meta property="og:url" content=")[^"]*(")',
                  lambda m: m.group(1) + base + "/" + m.group(2), html)
    html = re.sub(r'(<meta (?:property="og:image"|name="twitter:image") content=")[^"]*?(/assets/[^"]+")',
                  lambda m: m.group(1) + base + m.group(2), html)
    html = stamp_asset_versions(html, page.parent)
    atomic_write_text(page, html)


def stamp_asset_versions(html: str, site: Path) -> str:
    """Add ?v=<content hash> to local CSS/JS links so browsers fetch a changed file instead of a cached one."""
    def stamp(m: "re.Match[str]") -> str:
        target = site / m.group(2).lstrip("/")
        if not target.exists():
            return m.group(0)
        version = hashlib.sha256(target.read_bytes()).hexdigest()[:10]
        return f'{m.group(1)}{m.group(2)}?v={version}"'
    return re.sub(r'((?:href|src)=")(/?assets/[\w.-]+\.(?:css|js))(?:\?v=[^"]*)?"', stamp, html)


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
    similarity = _incident_similarity(settings, reports)  # keyed on the text as embedded, so before anything is corrected
    analyzed = {r["filename"] for r in reports}
    pending = sorted(name for name in by_file if name not in analyzed)
    if not reports:
        raise SystemExit("No analyzed reports found. Run the extract and analyze stages first.")
    if pending and not partial:
        log(f"WARNING: {len(pending)} manifest sources have no analysis yet and will be listed as pending")

    index_reports: List[Dict[str, Any]] = []
    index_incidents: List[Dict[str, Any]] = []
    incident_titles: Dict[str, Dict[str, str]] = {}
    incident_by_id: Dict[str, Dict[str, Any]] = {}
    units_of: Dict[str, List[Dict[str, Any]]] = {}
    corrected = Counter()
    for report in reports:
        entry = by_file[report["filename"]]
        units_of[report["id"]], _info = load_units(entry, settings)
        for incident in report.get("incidents") or []:
            raw = incident["location"].get("raw")
            if raw:  # re-resolve with the current gazetteer logic; cheap and deterministic
                place = gazetteer.resolve(raw.get("name"), raw.get("region"), raw.get("country"), raw.get("lat"), raw.get("lon"))
                place.update({"refers_to": incident["location"].get("refers_to", "unclear"), "raw": raw})
                incident["location"] = place
                incident["documentation"] = documentation_score(incident)
        # labels must agree with the facts recorded beside them (see consistency.py and the Methods page)
        corrected.update(consistency.apply(report, entry, {u["n"]: u["text"] for u in units_of[report["id"]]}, _release_year(entry)))
        for incident in report.get("incidents") or []:
            incident_titles[incident["id"]] = {"id": incident["id"], "title": incident["title"], "report": report["id"]}
            incident_by_id[incident["id"]] = incident
    log(f"Consistency corrections: {dict(corrected)}")

    # Accounts of one event in several files: list them all, count the event once (its best-documented account).
    report_of = {i: row["report"] for i, row in incident_titles.items()}
    groups = connections.event_groups(list(incident_by_id.values()), report_of, same_event, similarity)
    member_of = {member: members for members in groups for member in members}
    for members in groups:
        primary = max(members, key=lambda i: (incident_by_id[i]["documentation"]["score"], -len(i), i))
        for member in members:
            incident_by_id[member]["event"] = {"primary": primary, "accounts": len(members), "repeat": member != primary}

    # Byte-identical source files published under different names are the same document.
    by_hash: Dict[str, List[str]] = {}
    for entry in manifest:
        if entry.get("sha256"):
            by_hash.setdefault(str(entry["sha256"]), []).append(str(entry["filename"]))
    identical = {name: [other for other in names if other != name] for names in by_hash.values() if len(names) > 1 for name in names}
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
        units = units_of[report["id"]]
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
        if report["filename"] in identical:
            problems.append("the publisher's file is byte-identical to " + ", ".join(identical[report["filename"]]) + " (the same document under another name)")
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
            # "Same event in other files": the other accounts in this incident's event group (as on the Connections view)
            incident["same_event"] = [incident_titles[i] for i in member_of.get(incident["id"], []) if i != incident["id"]]
            if incident["location"]["country"]:
                incident["location"]["country_name"] = gazetteer.country_name(incident["location"]["country"])
        atomic_write_json(reports_dir / f"{report['id']}.json", scrub_model_names(hide_unmatched_quotes(full), kinds))

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
    events = [i for i in index_incidents if not i["repeat"]]  # one account per event; categorical figures count events
    most = lambda counter, n=10: dict(counter.most_common(n))
    modern = [i for i in events if (i["year"] or 0) >= 2015]
    stats = {
        "reports": len(index_reports), "incidents": len(index_incidents), "distinct_events": len(events),
        "repeat_accounts": len(index_incidents) - len(events),
        "pages": sum(r["pages"] or 0 for r in index_reports),
        "media_minutes": round(sum(r["duration"] or 0 for r in index_reports) / 60),
        "by_media": dict(Counter(r["media"] for r in index_reports)),
        "by_agency": dict(Counter(r["agency"] for r in index_reports).most_common()),
        "video_files_by_agency": dict(Counter(r["agency"] for r in index_reports if r["media"] == "video").most_common()),
        "files_by_release": dict(sorted(releases.items())),
        "files_with_incidents": sum(1 for r in index_reports if r["incidents"]),
        "by_disposition": dict(Counter(r["disposition"] or "not-assessed" for r in index_reports).most_common()),
        "largest_files_by_pages": [f"{r['id']} ({r['agency']}, {r['pages']} pages)" for r in sorted(index_reports, key=lambda r: -(r["pages"] or 0))[:6]],
        "incident_years": f"{min((i['year'] for i in events if i['year']), default='?')}-{max((i['year'] for i in events if i['year']), default='?')}",
        "events_by_decade": dict(sorted(Counter(f"{i['year'] // 10 * 10}s" for i in events if i["year"]).items())),
        "events_undated": sum(1 for i in events if not i["year"]),
        "events_by_status": dict(Counter(i["status"] for i in events).most_common()),
        "events_by_explanation": dict(Counter(i["explanation"] for i in events).most_common()),
        "events_with_a_conventional_explanation": sum(1 for i in events if i["explanation"] != "none-identified"),
        "events_with_instrument_data": sum(1 for i in events if any(m.startswith(("radar", "infrared", "photo", "film", "sonar", "satellite")) for m in i["modes"])),
        "events_by_us_state": most(Counter(i["region"] for i in events if i["country"] == "US" and i["region"])),
        "events_by_country": most(Counter(i["country"] for i in events if i["country"])),
        "countries_with_events": len({i["country"] for i in events if i["country"]}),
        "events_2015_or_later": {
            "total": len(modern), "with_infrared": sum(1 for i in modern if "infrared" in i["modes"]),
            "visual_only": sum(1 for i in modern if i["modes"] and all(m.startswith("visual") for m in i["modes"])),
            "by_country": most(Counter(i["country"] or "not located" for i in modern), 6),
        },
    }
    graph = connections.build(reports, groups)
    atomic_write_json(data_dir / "connections.json", graph)
    log(f"Connections: {graph['stats']}")

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
        "connections": {"file": "data/connections.json", **graph["stats"]},
        "quality": {
            "ocr_methods": dict(methods), "ocr_providers": dict(providers), **dict(quality),
            "reports_complete": sum(1 for r in index_reports if r["status"] == "complete"),
            "reports_incomplete": sum(1 for r in index_reports if r["status"] != "complete"),
            "pending_sources": len(pending), "ignored_files": len(find_orphans(settings, manifest)),
            "located_incidents": sum(1 for i in index_incidents if i["lat"] is not None),
            "dated_incidents": sum(1 for i in index_incidents if i["year"]),
            "repeat_accounts": len(index_incidents) - len(events), "event_groups": len(groups),
            "positions": dict(Counter(i["place_precision"] for i in index_incidents)),
            "corrections": dict(corrected),
            "issues": issues,
            "models": {"review": sorted({kinds[e.label] for e in settings.review_chain()}), "audio": sorted({kinds[e.label] for e in settings.audio_chain()}),
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
    _update_page_meta(site / "index.html", seo_description(stats), base)
    atomic_write_text(site / "robots.txt", f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n")
    page_urls = write_pages(site, base, prune=not partial)
    today = _dt.date.today().isoformat()
    urls = "".join(f"  <url><loc>{url}</loc><lastmod>{today}</lastmod></url>\n" for url in [f"{base}/"] + page_urls)
    atomic_write_text(site / "sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                      f"{urls}</urlset>\n")
    result = {"reports": len(index_reports), "incidents": len(index_incidents), "pending": len(pending),
              "issues": len(issues), "shards": len(shard_files), "corpus_summary": bool(summary)}
    log(f"Build finished: {result}")
    return result
