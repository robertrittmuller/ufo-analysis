"""Stage 3: build one deep research report per source file.

Every page of every document is read: text is split into page-aligned chunks, each chunk
is mined for constituent records, incidents, entities and key passages (map), then a
document-level synthesis is written (reduce). Nothing is sampled or truncated - an
over-long answer makes the chunk split in two rather than being dropped, and a chunk that
fails on every endpoint marks the report incomplete so the next run retries it.

Quotes are checked against the extracted page text, dates and places are normalized
deterministically, and a transparent documentation-strength score is computed.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import llm
from .config import Settings
from .geo import Gazetteer
from .util import (
    atomic_write_json, as_list, clean_str, log, normalize_for_match, normalize_space, now_iso, read_json,
    slugify, stable_hash, str_list,
)

ANALYSIS_VERSION = 6
CHUNK_CHARS = 12000
TRANSCRIPT_UNIT_CHARS = 3500

SHAPES = ["disc", "sphere-orb", "cylinder-cigar", "triangle", "oval-egg", "light-point", "fireball", "rectangle",
          "cone", "irregular", "multiple-shapes", "other", "not-described"]
MODES = ["visual-ground", "visual-air", "visual-sea", "radar-ground", "radar-air", "radar-sea", "infrared",
         "photograph", "film-video", "sonar", "satellite", "audio", "other"]
INSTRUMENT_MODES = {"radar-ground", "radar-air", "radar-sea", "infrared", "photograph", "film-video", "sonar", "satellite"}
WITNESS_CATEGORIES = ["military-aircrew", "military-ground", "military-naval", "civil-pilot", "astronaut", "police",
                      "scientist-engineer", "government-official", "air-traffic-control", "civilian", "unknown"]
TRAINED = {"military-aircrew", "military-naval", "civil-pilot", "astronaut", "police", "scientist-engineer", "air-traffic-control"}
RECORD_STATUS = ["identified", "probably-identified", "unidentified", "insufficient-information", "not-evaluated"]
EXPLANATION_CATEGORIES = ["aircraft", "balloon", "astronomical", "meteor-fireball", "satellite-space-debris",
                          "atmospheric-optical", "radar-artifact", "sensor-artifact", "missile-rocket", "drone-uas",
                          "birds-wildlife", "hoax", "misperception", "foreign-technology", "other", "none-identified"]
LIKELIHOODS = ["almost no chance", "very unlikely", "unlikely", "roughly even chance", "likely", "very likely", "almost certain"]
HYNEK = ["NL", "DD", "RV", "CE1", "CE2", "CE3", "not-applicable"]
DISPOSITIONS = ["explained-in-record", "likely-explained", "insufficient-information", "unexplained-in-record", "not-a-sighting-record"]
RECORD_KINDS = ["incident-report", "case-file", "analysis-or-study", "policy-or-procedure", "correspondence",
                "sensor-media", "audio-recording", "photograph", "administrative", "press-or-public", "other"]
REDACTION_LEVELS = ["none", "light", "moderate", "heavy"]
OBSERVABLES = ["instantaneous-acceleration", "hypersonic-velocity-without-signatures", "low-observability",
               "transmedium-travel", "lift-without-apparent-propulsion"]
THEMES = ["Military encounters", "Aviation safety", "Radar tracking", "Infrared and EO sensor video", "Nuclear sites",
          "Space and astronaut reports", "Maritime and undersea", "Foreign reports", "Intelligence assessment",
          "Policy and procedure", "Public and press", "Physical evidence", "Photographic evidence", "Scientific study",
          "Hoax or misidentification", "Law enforcement", "Congressional and oversight", "Cold War air defense"]

SYSTEM = (
    "You are a careful archival analyst preparing research notes on declassified U.S. government UAP/UFO records. "
    "Report only what the record says. Never guess at redacted content, never invent names, numbers, dates or quotes, "
    "and leave a field empty (\"\" / [] / null) when the record does not state it. Keep the record's own conclusions "
    "separate from your assessment. Quotes must be copied verbatim from the page they cite. Respond with one JSON object only."
)

EXTRACT_PROMPT = """SOURCE FILE CONTEXT (publisher metadata - context only, not evidence):
{context}

{unit_note}
Below is part {part} of {parts} of the extracted text, pages {first}-{last}. Each page starts with a marker like [[PAGE 12]]; always cite those exact page numbers.
{carry}
Read ALL of it and return ONE JSON object. To keep the answer compact: write minified JSON (no indentation), and OMIT any key whose value would be empty (\"\", [], null, false, \"unknown\", \"not-described\", \"none\"). The template below shows every key you may use:

{{
 "segment_summary": "4-8 sentences: what these pages contain, who wrote them, what they report or decide",
 "records": [  // each distinct constituent document in these pages (memo, teletype, letter, form, report...)
   {{"pages": [first, last], "type": "", "date": "as written", "from": "", "to": "", "subject": "", "file_numbers": [""], "classification": ""}}
 ],
 "incidents": [  // every specific reported observation event (a particular time and place). Policy discussion, statistics and general commentary are NOT incidents. Do not merge different events; do not skip brief ones.
   {{
     "title": "short neutral label: place, date, what was reported",
     "pages": [page numbers that describe it],
     "continues_previous": false,  // true only if this continues an incident begun before these pages
     "date_text": "date as written", "date_iso": "YYYY-MM-DD or YYYY-MM or YYYY, or \\"\\"",
     "time_text": "", "duration_text": "",
     "location": {{"name": "most specific place named", "region": "state/province", "country": "", "lat": null, "lon": null, "refers_to": "observer|object|unclear"}},  // lat/lon: use coordinates printed in the record, otherwise your best approximate decimal coordinates for any identifiable named place (town, base, test range, sea area); null only when the place is too vague to locate
     "narrative": "3-6 sentences in your own words: who observed what, how, and what happened",
     "witnesses": [{{"description": "role/rank/occupation, name if given", "category": "{witness_categories}", "count": 1, "firsthand": true}}],
     "observation": {{"modes": ["{modes}"], "sensors": "specific radar/camera/instrument named", "platform": "aircraft/ship/site observing"}},
     "object": {{"count": "", "shape": "{shapes}", "shape_text": "witness wording", "size": "", "color": "", "luminosity": "", "sound": "", "altitude": "", "speed": "", "distance": "", "motion": "course, maneuvers", "disappearance": ""}},
     "conditions": {{"weather": "", "light": "day|night|dawn-dusk|unknown"}},
     "effects": "physical, electromagnetic or physiological effects reported, or \\"\\"",
     "physical_evidence": "photos, film, radar records, fragments, traces - and whether they are in this file",
     "measurements_from": "witness-estimate|instrument|both|none",
     "official_action": "interception, investigation, reporting chain",
     "official_explanation": "what the record itself concludes, or \\"\\"",
     "status_in_record": "{statuses}",
     "assessment": {{
        "hynek": "{hynek}",
        "best_explanation": "most plausible conventional explanation on this evidence, or \\"none identified\\"",
        "explanation_category": "{explanations}",
        "likelihood": "{likelihoods}",
        "reasoning": "1-3 sentences: evidence for and against, and what is missing",
        "strangeness": 1  // 1 (ordinary) to 5 (hard to reconcile with known phenomena AS REPORTED)
     }},
     "quotes": [{{"page": page number, "text": "verbatim sentence(s) from that page, max 40 words"}}]  // 1-3 most important
   }}
 ],
 "entities": {{
   "people": [{{"name": "", "role": "", "org": ""}}],
   "organizations": [""], "places": [""], "aircraft_equipment": [""], "programs": [""], "file_numbers": [""]
 }},
 "key_passages": [{{"page": page number, "text": "verbatim, max 50 words", "why": "why a researcher would care"}}],  // up to 6
 "notes": {{"referenced_but_missing": ["attachments, enclosures, photos or pages referred to but not present"], "redactions": "what kind of content is withheld, if apparent", "legibility": ""}}
}}

TEXT:
{text}"""

SYNTH_PROMPT = """SOURCE FILE CONTEXT (publisher metadata - context only, not evidence):
{context}

EXTRACTION STATISTICS:
{stats}

{unit_note}
The whole file has already been read in {parts} part(s). Notes from every part follow. Page numbers are the numbers shown below; cite only those.

PART SUMMARIES:
{summaries}

CONSTITUENT RECORDS:
{records}

INCIDENTS FOUND (index: title | date | place | pages | status in record | best explanation):
{incidents}

KEY PASSAGES (verbatim, with page):
{passages}

REFERENCED BUT MISSING / REDACTION NOTES:
{notes}

Write the document-level research report as ONE minified JSON object (no indentation; omit keys whose value would be empty):

{{
 "title": "clear descriptive title of the SUBJECT of this file, max 14 words (not the filename, not a description of the extraction)",
 "summary": "2-3 paragraphs for a researcher: what this file is, what it reports, how it was handled, why it matters. Plain, precise, no hype.",
 "key_points": [{{"text": "one specific fact or finding", "pages": [page numbers]}}],  // 4-8, each tied to pages
 "record_kind": "{record_kinds}",
 "document_type": "e.g. FBI teletype series, Air Force incident summary sheets, CIA memorandum, FLIR video clip",
 "document_date_text": "date the document was written / the recording was made, as stated (NOT the release date)", "document_date_iso": "YYYY-MM-DD / YYYY-MM / YYYY / \\"\\"",
 "period_covered": {{"start": "YYYY...", "end": "YYYY..."}},  // span of the events and documents in the file, excluding the 2026 release
 "provenance": {{"originating_agency": "", "originating_office": "", "authors": [""], "recipients": [""], "file_numbers": [""], "record_series": "record group / file series if stated", "original_classification": "", "declassification": "authority/date if stated", "distribution": ""}},
 "completeness": {{"appears_complete": null, "missing": ["specific items referenced but absent"], "notes": ""}},
 "redaction": {{"level": "{redaction_levels}", "exemptions": ["FOIA codes as printed"], "withheld": "kinds of information withheld (names, sources, locations...) - never guess the content"}},
 "official_handling": {{"investigating_bodies": [""], "actions": ["what officials did"], "conclusion": "the record's own conclusion, or \\"\\"", "conclusion_pages": [page numbers]}},
 "assessment": {{
   "disposition": "{dispositions}",
   "rationale": "3-5 sentences explaining the disposition from the evidence in the file",
   "candidate_explanations": [{{"explanation": "", "category": "{explanations}", "likelihood": "{likelihoods}", "supporting": ["evidence in the file"], "contradicting": ["evidence in the file"]}}],  // 1-4, most likely first; [] for records that are not about a sighting
   "claimed_characteristics": [{{"name": "{observables}", "basis": "witness-estimate|instrument", "pages": [page numbers]}}],  // only if the record itself claims them
   "data_gaps": ["specific missing data that would settle the case"],
   "follow_up": ["specific records, people or data a researcher should seek next"],
   "research_value": 3, "research_value_reason": "1 (routine/duplicative) to 5 (essential primary source) and why"
 }},
 "ratings": {{
   "hynek": "{hynek}",
   "vallee": "AN1-5, FB1-5, MA1-5 or CE1-5, or \\"\\"",
   "svp": {{"source": 0, "visit": 0, "explanations": 0}},  // Vallee SVP digits 0-4 each
   "source_reliability": "A-F (NATO Admiralty code) for the reporting source",
   "information_credibility": "1-6 (NATO Admiralty code) for the reported information",
   "notes": "one sentence justifying the ratings; use not-applicable / empty for non-sighting records"
 }},
 "themes": ["choose from: {themes}"],
 "duplicate_incidents": [[0, 1]]  // groups of incident indexes that are the SAME event described twice; [] if none
}}"""

_PAGE_MARK = "[[PAGE {n}]]"


# --------------------------------------------------------------------------- building units

def doc_id_for(filename: str) -> str:
    return slugify(Path(filename).stem, 90)


def load_units(entry: Dict[str, Any], settings: Settings) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (units, extraction info). A unit is one citable page or media section."""
    name = str(entry["filename"])
    stem = Path(name).stem
    media_type = str(entry.get("media_type"))
    units: List[Dict[str, Any]] = []
    info: Dict[str, Any] = {"media_type": media_type}
    if media_type in ("pdf", "image"):
        cache = read_json(settings.pages_dir / f"{stem}.json")
        if not cache:
            return [], {"error": "not extracted"}
        pages = cache.get("pages") or {}
        methods: Dict[str, int] = {}
        providers: Dict[str, int] = {}
        markings: Dict[str, int] = {}
        legibility: Dict[str, int] = {}
        flagged: List[int] = []
        failed: List[int] = []
        redactions = handwriting = 0
        for number in sorted(pages, key=int):
            page = pages[number]
            method = page.get("method") or "failed"
            methods[method] = methods.get(method, 0) + 1
            if page.get("provider"):
                providers[page["provider"]] = providers.get(page["provider"], 0) + 1
            if method == "failed":
                failed.append(int(number))
            if page.get("flags"):
                flagged.append(int(number))
            redactions += int(page.get("redactions") or 0)
            handwriting += 1 if page.get("handwriting") else 0
            for marking in page.get("markings") or []:
                markings[marking] = markings.get(marking, 0) + 1
            if page.get("legibility"):
                legibility[page["legibility"]] = legibility.get(page["legibility"], 0) + 1
            text = (page.get("text") or "").strip()
            if page.get("visual"):
                text = (text + f"\n[Visual content: {page['visual']}]").strip()
            units.append({"n": int(number), "label": f"p. {number}", "text": text, "method": method,
                          "provider": page.get("provider") or "", "flags": page.get("flags") or [],
                          "legibility": page.get("legibility") or "", "redactions": int(page.get("redactions") or 0),
                          "handwriting": bool(page.get("handwriting")), "visual": page.get("visual") or ""})
        extracted = len(pages)
        info.update({
            "page_count": cache.get("page_count") or extracted, "pages_extracted": extracted,
            "methods": methods, "providers": providers, "failed_pages": failed, "flagged_pages": flagged,
            "redaction_marks": redactions, "handwritten_pages": handwriting, "legibility": legibility,
            "markings": dict(sorted(markings.items(), key=lambda kv: -kv[1])[:25]),
            "pages_with_redactions": sum(1 for u in units if u["redactions"]),
        })
        if extracted < (cache.get("page_count") or extracted):
            info["partial"] = True
        return units, info

    cache = read_json(settings.media_dir / f"{stem}.json")
    if not cache:
        return [], {"error": "not extracted"}
    n = 0

    def add(label: str, text: str, kind: str) -> None:
        nonlocal n
        n += 1
        units.append({"n": n, "label": label, "text": text.strip(), "method": kind, "provider": "", "flags": [],
                      "legibility": "", "redactions": 0, "handwriting": False, "visual": ""})

    narrative = normalize_space(entry.get("dvids_description") or entry.get("narrative") or entry.get("description_blurb"))
    if narrative:
        add("Publisher description", f"PUBLISHER'S DESCRIPTION (war.gov / DVIDS):\n{narrative}", "publisher")
    review = cache.get("frame_review")
    if review:
        lines = [f"FRAME ANALYSIS (machine description of {review.get('frames_reviewed')} sampled frames):",
                 f"Summary: {review.get('summary')}", f"Sensor type: {review.get('sensor_type')}",
                 f"On-screen text: {review.get('overlay_text') or 'none legible'}",
                 f"Object: {review.get('object_description') or 'none discernible'}",
                 f"Apparent motion: {review.get('object_motion') or 'n/a'}", f"Background: {review.get('background') or 'n/a'}",
                 f"Limits: {review.get('limits') or 'n/a'}"]
        lines += [f"  t={f.get('t')}s: {f.get('description')}" for f in review.get("frames") or []]
        add("Frame analysis", "\n".join(lines), "frames")
    captions = (cache.get("captions") or {}).get("text") or ""
    asr = (cache.get("asr") or {}).get("text") or ""
    for label, heading, body, kind in (
        ("Captions", "OFFICIAL CAPTIONS (from the publisher):", captions, "captions"),
        ("Transcript", "MACHINE TRANSCRIPT (automatic speech recognition; may contain errors):", asr, "asr"),
    ):
        if not body:
            continue
        lines = body.split("\n")
        block: List[str] = []
        size = 0
        part = 1
        for line in lines + [None]:
            if line is None or size + len(line) > TRANSCRIPT_UNIT_CHARS:
                if block:
                    stamp = re.match(r"\[([\d:]+)\]", block[0])
                    add(f"{label} {stamp.group(1) if stamp else part}", heading + "\n" + "\n".join(block), kind)
                    part += 1
                block, size = [], 0
            if line is not None:
                block.append(line)
                size += len(line) + 1
    probe = cache.get("probe") or {}
    asr_info = cache.get("asr") or {}
    info.update({
        "duration": probe.get("duration"), "has_video": probe.get("has_video"), "has_audio": probe.get("has_audio"),
        "resolution": f"{probe.get('width')}x{probe.get('height')}" if probe.get("width") else "",
        "frames_sampled": len(cache.get("frames") or []), "frame_review_provider": (review or {}).get("provider", ""),
        "sensor_type": (review or {}).get("sensor_type", ""), "captions": bool(captions),
        "transcript_status": asr_info.get("status", ""), "transcript_providers": asr_info.get("providers", []),
        "transcript_chunks": len(asr_info.get("chunks") or []),
        "transcript_rejected_chunks": sum(1 for c in asr_info.get("chunks") or [] if c.get("status") == "rejected"),
        "issues": cache.get("issues") or [], "page_count": len(units), "pages_extracted": len(units),
    })
    return units, info


def make_chunks(units: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    size = 0
    for unit in units:
        length = len(unit["text"]) + 20
        if current and size + length > CHUNK_CHARS:
            chunks.append(current)
            current, size = [], 0
        current.append(unit)
        size += length
    if current:
        chunks.append(current)
    return chunks


def _unit_note(entry: Dict[str, Any]) -> str:
    media_type = str(entry.get("media_type"))
    if media_type in ("video", "audio"):
        return (f"NOTE: this source is a {media_type.upper()} FILE, not a paper document. The numbered \"pages\" below are sections of "
                "material derived from it: the publisher's description, a machine description of sampled video frames, official "
                "captions and/or a machine transcript. Describe the recording itself; call the sections by what they are, and "
                "never describe the source as a multi-page file. The recording was reviewed only through this derived material.")
    if media_type == "image":
        return "NOTE: this source is a single IMAGE file (photograph, rendering or scanned page); page 1 is its transcription and visual description."
    return ""


def _context(entry: Dict[str, Any]) -> str:
    rows = [
        ("Published title", entry.get("title")), ("Releasing agency", entry.get("agency")),
        ("Release date", entry.get("release_date")), ("Media type", entry.get("media_type")),
        ("Published incident date", entry.get("incident_date")), ("Published incident location", entry.get("incident_location")),
        ("Publisher description", entry.get("description_blurb")),
    ]
    return "\n".join(f"- {k}: {normalize_space(v)}" for k, v in rows if normalize_space(v) and normalize_space(v).upper() != "N/A")


# --------------------------------------------------------------------------- map step

def _enum(value: Any, allowed: List[str], default: str = "") -> str:
    text = clean_str(value).lower().replace("_", "-").replace(" ", "-")
    for option in allowed:
        if text == option.lower().replace(" ", "-"):
            return option
    return default


def _pages(value: Any, valid: set) -> List[int]:
    out: List[int] = []
    for item in as_list(value):
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number in valid and number not in out:
            out.append(number)
    return sorted(out)


def _extract_units(units: List[Dict[str, Any]], entry: Dict[str, Any], chain: llm.Chain, part: int, parts: int,
                   carry: str) -> Dict[str, Any]:
    """Run the map prompt on some units; split and recurse when the answer is too long."""
    text = "\n\n".join(_PAGE_MARK.format(n=u["n"]) + "\n" + (u["text"] or "[no text on this page]") for u in units)
    prompt = EXTRACT_PROMPT.format(
        context=_context(entry), unit_note=_unit_note(entry), part=part, parts=parts, first=units[0]["n"], last=units[-1]["n"],
        carry=f"Summary of the preceding pages, for continuity only: {carry}\n" if carry else "",
        witness_categories="|".join(WITNESS_CATEGORIES), modes="|".join(MODES), shapes="|".join(SHAPES),
        statuses="|".join(RECORD_STATUS), hynek="|".join(HYNEK), explanations="|".join(EXPLANATION_CATEGORIES),
        likelihoods="|".join(LIKELIHOODS), text=text,
    )
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    try:
        data, provider = chain.json(messages, max_tokens=8000, max_tokens_ceiling=16000, required_keys=("segment_summary",), task="extract")
        data["_provider"] = provider
        data["_units"] = [u["n"] for u in units]
        return data
    except llm.TruncatedError:
        if len(units) == 1:
            unit = units[0]
            lines = unit["text"].split("\n")
            if len(lines) < 2 or len(unit["text"]) < 2000:
                raise
            middle = len(lines) // 2
            halves = [dict(unit, text="\n".join(lines[:middle])), dict(unit, text="\n".join(lines[middle:]))]
        else:
            middle = len(units) // 2
            halves = [units[:middle], units[middle:]]
            first = _extract_units(halves[0], entry, chain, part, parts, carry)
            second = _extract_units(halves[1], entry, chain, part, parts, clean_str(first.get("segment_summary"), 600))
            return _merge_raw(first, second)
        first = _extract_units([halves[0]], entry, chain, part, parts, carry)
        second = _extract_units([halves[1]], entry, chain, part, parts, clean_str(first.get("segment_summary"), 600))
        return _merge_raw(first, second)


def _merge_raw(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {
        "segment_summary": (clean_str(a.get("segment_summary")) + " " + clean_str(b.get("segment_summary"))).strip(),
        "_provider": a.get("_provider"), "_units": sorted(set(a.get("_units", []) + b.get("_units", []))),
    }
    for key in ("records", "incidents", "key_passages"):
        merged[key] = [x for x in as_list(a.get(key)) + as_list(b.get(key)) if isinstance(x, dict)]
    ents_a, ents_b = a.get("entities") or {}, b.get("entities") or {}
    merged["entities"] = {k: as_list(ents_a.get(k)) + as_list(ents_b.get(k))
                          for k in ("people", "organizations", "places", "aircraft_equipment", "programs", "file_numbers")}
    notes_a, notes_b = a.get("notes") or {}, b.get("notes") or {}
    merged["notes"] = {
        "referenced_but_missing": as_list(notes_a.get("referenced_but_missing")) + as_list(notes_b.get("referenced_but_missing")),
        "redactions": " ".join(x for x in (clean_str(notes_a.get("redactions")), clean_str(notes_b.get("redactions"))) if x),
        "legibility": " ".join(x for x in (clean_str(notes_a.get("legibility")), clean_str(notes_b.get("legibility"))) if x),
    }
    return merged


# --------------------------------------------------------------------------- normalization

_YEAR_NOW = _dt.date.today().year


def normalize_date(iso: Any, text: Any, latest_year: int = _YEAR_NOW) -> Dict[str, Any]:
    """Return {text, iso, year, precision}. Rejects impossible dates instead of guessing."""
    raw_text = clean_str(text)
    iso_text = clean_str(iso)
    match = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$", iso_text)
    year = month = day = None
    if match and 1800 <= int(match.group(1)) <= latest_year:
        year = int(match.group(1))
        month = int(match.group(2)) if match.group(2) else None
        day = int(match.group(3)) if match.group(3) else None
    elif raw_text:  # no usable ISO date from the model: parse what the record says
        decade = re.fullmatch(r"(?:the\s+)?(?:early\s+|mid-?\s*|late\s+)?((?:18|19|20)\d0)s", raw_text.lower())
        if decade:
            year = int(decade.group(1))
        else:
            try:
                from dateutil import parser as date_parser

                a = date_parser.parse(raw_text, default=_dt.datetime(1904, 2, 29), fuzzy=True)
                b = date_parser.parse(raw_text, default=_dt.datetime(1907, 7, 13), fuzzy=True)
                year = a.year if a.year == b.year else None
                month = a.month if a.month == b.month else None
                day = a.day if a.day == b.day else None
                if year is not None and year > latest_year and not re.search(r"(?<!\d)\d{4}(?!\d)", raw_text):
                    year -= 100  # a two-digit year read into the future ("7/6/47", "8 Jul 47" are 1947)
            except (ValueError, OverflowError, TypeError):
                year = month = day = None
    if year is not None and not (1800 <= year <= latest_year):
        year = month = day = None
    if month is not None and not 1 <= month <= 12:
        month = day = None
    if year and month and day:
        try:
            _dt.date(year, month, day)
        except ValueError:
            day = None
    if not year:
        return {"text": raw_text, "iso": "", "year": None, "precision": "none"}
    if month and day:
        return {"text": raw_text, "iso": f"{year:04d}-{month:02d}-{day:02d}", "year": year, "precision": "day"}
    if month:
        return {"text": raw_text, "iso": f"{year:04d}-{month:02d}", "year": year, "precision": "month"}
    return {"text": raw_text, "iso": f"{year:04d}", "year": year, "precision": "year"}


def verify_quote(quote: str, page: int, norm_pages: Dict[int, str]) -> Tuple[str, int]:
    """Return (status, page). status: verified | approximate | unverified."""
    needle = normalize_for_match(quote)
    if len(needle) < 12:
        return "unverified", page
    order = [page] + [n for n in norm_pages if n != page]
    for number in order:
        if number in norm_pages and needle in norm_pages[number]:
            return "verified", number
    for number in order[:1] + order[1:]:
        haystack = norm_pages.get(number, "")
        if not haystack:
            continue
        matcher = difflib.SequenceMatcher(None, needle, haystack, autojunk=False)
        matched = sum(block.size for block in matcher.get_matching_blocks() if block.size >= 4)
        if matched / len(needle) >= 0.85:
            return "approximate", number
        if number == page and len(norm_pages) > 40:
            break  # keep big documents fast: fuzzy-match only the cited page
    return "unverified", page


def _clean_quotes(items: Any, valid: set, norm_pages: Dict[int, str], with_why: bool = False) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in as_list(items):
        if not isinstance(item, dict):
            continue
        text = clean_str(item.get("text"), 600)
        pages = _pages(item.get("page"), valid)
        if not text or not pages:
            continue
        status, page = verify_quote(text, pages[0], norm_pages)
        row = {"page": page, "text": text, "check": status}
        if with_why:
            row["why"] = clean_str(item.get("why"), 300)
        out.append(row)
    return out


def _clean_incident(raw: Dict[str, Any], valid: set, norm_pages: Dict[int, str], gazetteer: Gazetteer,
                    latest_year: int) -> Optional[Dict[str, Any]]:
    title = clean_str(raw.get("title"), 200)
    narrative = clean_str(raw.get("narrative"), 2500)
    if not title and not narrative:
        return None
    location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    place = gazetteer.resolve(clean_str(location.get("name")), clean_str(location.get("region")),
                              clean_str(location.get("country")), location.get("lat"), location.get("lon"))
    place["refers_to"] = _enum(location.get("refers_to"), ["observer", "object", "unclear"], "unclear")
    # Keep what the model said so the site build can re-resolve places when the gazetteer logic improves.
    place["raw"] = {"name": clean_str(location.get("name"), 200), "region": clean_str(location.get("region"), 100),
                    "country": clean_str(location.get("country"), 100), "lat": location.get("lat"), "lon": location.get("lon")}
    observation = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
    obj = raw.get("object") if isinstance(raw.get("object"), dict) else {}
    conditions = raw.get("conditions") if isinstance(raw.get("conditions"), dict) else {}
    assessment = raw.get("assessment") if isinstance(raw.get("assessment"), dict) else {}
    witnesses = []
    for w in as_list(raw.get("witnesses")):
        if not isinstance(w, dict):
            continue
        try:
            count = max(1, int(w.get("count") or 1))
        except (TypeError, ValueError):
            count = 1
        witnesses.append({"description": clean_str(w.get("description"), 200), "category": _enum(w.get("category"), WITNESS_CATEGORIES, "unknown"),
                          "count": count, "firsthand": bool(w.get("firsthand"))})
    try:
        strangeness = min(5, max(1, int(assessment.get("strangeness") or 1)))
    except (TypeError, ValueError):
        strangeness = 1
    modes = [m for m in (_enum(x, MODES) for x in as_list(observation.get("modes"))) if m]
    return {
        "title": title or narrative[:90],
        "pages": _pages(raw.get("pages"), valid),
        "continues_previous": bool(raw.get("continues_previous")),
        "date": normalize_date(raw.get("date_iso"), raw.get("date_text"), latest_year),
        "time_text": clean_str(raw.get("time_text"), 80), "duration_text": clean_str(raw.get("duration_text"), 80),
        "location": place,
        "narrative": narrative,
        "witnesses": witnesses[:12],
        "observation": {"modes": sorted(set(modes)), "sensors": clean_str(observation.get("sensors"), 240),
                        "platform": clean_str(observation.get("platform"), 240)},
        "object": {
            "count": clean_str(obj.get("count"), 60), "shape": _enum(obj.get("shape"), SHAPES, "not-described"),
            **{key: clean_str(obj.get(key), 240) for key in ("shape_text", "size", "color", "luminosity", "sound", "altitude",
                                                               "speed", "distance", "motion", "disappearance")},
        },
        "conditions": {"weather": clean_str(conditions.get("weather"), 200), "light": _enum(conditions.get("light"), ["day", "night", "dawn-dusk", "unknown"], "unknown")},
        "effects": clean_str(raw.get("effects"), 400), "physical_evidence": clean_str(raw.get("physical_evidence"), 400),
        "measurements_from": _enum(raw.get("measurements_from"), ["witness-estimate", "instrument", "both", "none"], "none"),
        "official_action": clean_str(raw.get("official_action"), 500),
        "official_explanation": clean_str(raw.get("official_explanation"), 500),
        "status_in_record": _enum(raw.get("status_in_record"), RECORD_STATUS, "not-evaluated"),
        "assessment": {
            "hynek": _enum(assessment.get("hynek"), HYNEK, "not-applicable"),
            "best_explanation": clean_str(assessment.get("best_explanation"), 300),
            "explanation_category": _enum(assessment.get("explanation_category"), EXPLANATION_CATEGORIES, "none-identified"),
            "likelihood": _enum(assessment.get("likelihood"), LIKELIHOODS, ""),
            "reasoning": clean_str(assessment.get("reasoning"), 700),
            "strangeness": strangeness,
        },
        "quotes": _clean_quotes(raw.get("quotes"), valid, norm_pages)[:4],
    }


def _merge_incident(base: Dict[str, Any], extra: Dict[str, Any]) -> None:
    """Fold `extra` into `base` (continuation or duplicate): union lists, keep the fuller value."""
    base["pages"] = sorted(set(base["pages"]) | set(extra["pages"]))
    if extra["narrative"] and extra["narrative"] not in base["narrative"]:
        base["narrative"] = (base["narrative"] + " " + extra["narrative"]).strip()[:3500]
    for key in ("time_text", "duration_text", "effects", "physical_evidence", "official_action", "official_explanation"):
        if len(extra.get(key) or "") > len(base.get(key) or ""):
            base[key] = extra[key]
    for group in ("object", "conditions"):
        for key, value in extra[group].items():
            current = base[group].get(key)
            if value and (not current or current in ("not-described", "unknown")):
                base[group][key] = value
    base["observation"]["modes"] = sorted(set(base["observation"]["modes"]) | set(extra["observation"]["modes"]))
    for key in ("sensors", "platform"):
        if len(extra["observation"][key]) > len(base["observation"][key]):
            base["observation"][key] = extra["observation"][key]
    known = {w["description"].lower() for w in base["witnesses"]}
    base["witnesses"] += [w for w in extra["witnesses"] if w["description"].lower() not in known]
    if base["date"]["precision"] == "none" or (len(extra["date"]["iso"]) > len(base["date"]["iso"])):
        if extra["date"]["precision"] != "none":
            base["date"] = extra["date"]
    order = {"city": 4, "estimated": 3, "region": 2, "country": 1, "none": 0}
    if order[extra["location"]["precision"]] > order[base["location"]["precision"]]:
        base["location"] = extra["location"]
    if base["status_in_record"] == "not-evaluated":
        base["status_in_record"] = extra["status_in_record"]
    if extra["assessment"]["strangeness"] > base["assessment"]["strangeness"] or not base["assessment"]["best_explanation"]:
        base["assessment"] = extra["assessment"]
    seen = {q["text"] for q in base["quotes"]}
    base["quotes"] = (base["quotes"] + [q for q in extra["quotes"] if q["text"] not in seen])[:5]
    if base["measurements_from"] == "none":
        base["measurements_from"] = extra["measurements_from"]


def documentation_score(incident: Dict[str, Any]) -> Dict[str, Any]:
    """Transparent 0-100 score for how well an incident is DOCUMENTED (not how anomalous it is)."""
    modes = set(incident["observation"]["modes"])
    instruments = modes & INSTRUMENT_MODES
    visual = any(m.startswith("visual") for m in modes)
    sensing = (12 if len(instruments) == 1 else 20 if len(instruments) >= 2 else 0) + (5 if instruments and visual else 0)
    total_witnesses = sum(w["count"] for w in incident["witnesses"])
    witness = (12 if total_witnesses >= 5 else 8 if total_witnesses >= 2 else 3 if total_witnesses == 1 else 0)
    witness += 8 if any(w["category"] in TRAINED for w in incident["witnesses"]) else 0
    witness += 5 if any(w["firsthand"] for w in incident["witnesses"]) else 0
    spec = {"day": 6, "month": 3, "year": 1, "none": 0}[incident["date"]["precision"]]
    spec += 4 if incident["time_text"] else 0
    spec += {"city": 6, "estimated": 5, "region": 3, "country": 1, "none": 0}[incident["location"]["precision"]]
    spec += 2 if incident["duration_text"] else 0
    described = sum(1 for k in ("shape_text", "size", "color", "altitude", "speed", "motion", "disappearance") if incident["object"].get(k))
    spec += min(7, described)
    quotes = incident["quotes"]
    verified = sum(1 for q in quotes if q["check"] != "unverified")
    doc = round(10 * verified / len(quotes)) if quotes else 0
    doc += 5 if incident["official_action"] else 0
    doc += 5 if incident["measurements_from"] in ("instrument", "both") else 0
    doc += 5 if incident["physical_evidence"] and not re.match(r"^(none|no\b)", incident["physical_evidence"].lower()) else 0
    components = {"sensing": min(25, sensing), "witnesses": min(25, witness), "specificity": min(25, spec), "documentation": min(25, doc)}
    score = sum(components.values())
    band = "strong" if score >= 70 else "moderate" if score >= 50 else "limited" if score >= 30 else "minimal"
    return {"score": score, "band": band, "components": components}


# --------------------------------------------------------------------------- reduce step

def _synthesize(entry: Dict[str, Any], info: Dict[str, Any], results: List[Dict[str, Any]], incidents: List[Dict[str, Any]],
                passages: List[Dict[str, Any]], chain: llm.Chain) -> Tuple[Dict[str, Any], str]:
    per_summary = max(350, min(1400, 60000 // max(1, len(results))))
    summaries = "\n".join(f"[Part {i + 1}, pages {r['_units'][0]}-{r['_units'][-1]}] {clean_str(r.get('segment_summary'), per_summary)}"
                          for i, r in enumerate(results))
    record_rows: List[str] = []
    for result in results:
        for record in as_list(result.get("records")):
            if isinstance(record, dict):
                record_rows.append(" | ".join(x for x in (
                    f"pp. {'-'.join(str(p) for p in as_list(record.get('pages'))[:2])}", clean_str(record.get("type"), 60),
                    clean_str(record.get("date"), 40), "from " + clean_str(record.get("from"), 80) if clean_str(record.get("from")) else "",
                    "to " + clean_str(record.get("to"), 80) if clean_str(record.get("to")) else "", clean_str(record.get("subject"), 140),
                    "; ".join(str_list(record.get("file_numbers"), 4)), clean_str(record.get("classification"), 40)) if x))
    per_incident = max(120, min(420, 40000 // max(1, len(incidents))))
    incident_rows = [
        f"{i}: {inc['title']} | {inc['date']['iso'] or inc['date']['text'] or 'undated'} | {inc['location']['label'] or 'no place'} | "
        f"pp. {','.join(map(str, inc['pages'][:6]))} | {inc['status_in_record']} | {inc['assessment']['best_explanation'] or 'none'} | "
        f"{inc['narrative'][:per_incident]}"
        for i, inc in enumerate(incidents)
    ]
    notes: List[str] = []
    for result in results:
        note = result.get("notes") or {}
        notes += [f"missing: {m}" for m in str_list(note.get("referenced_but_missing"), 8)]
        if clean_str(note.get("redactions")):
            notes.append(f"redactions: {clean_str(note.get('redactions'), 300)}")
    stats = {k: info.get(k) for k in ("page_count", "pages_extracted", "methods", "redaction_marks", "pages_with_redactions",
                                      "handwritten_pages", "legibility", "markings", "failed_pages", "duration", "sensor_type",
                                      "captions", "transcript_status") if info.get(k) not in (None, "", [], {})}
    prompt = SYNTH_PROMPT.format(
        context=_context(entry), unit_note=_unit_note(entry), stats="\n".join(f"- {k}: {v}" for k, v in stats.items()), parts=len(results),
        summaries=summaries, records="\n".join(record_rows[:400]) or "(none listed)",
        incidents="\n".join(incident_rows) or "(no specific incidents found)",
        passages="\n".join(f"p.{p['page']}: \"{p['text']}\" ({p.get('why', '')})" for p in passages[:60]) or "(none)",
        notes="\n".join(dict.fromkeys(notes[:80])) or "(none)",
        record_kinds="|".join(RECORD_KINDS), redaction_levels="|".join(REDACTION_LEVELS), dispositions="|".join(DISPOSITIONS),
        explanations="|".join(EXPLANATION_CATEGORIES), likelihoods="|".join(LIKELIHOODS), observables="|".join(OBSERVABLES),
        hynek="|".join(HYNEK), themes="; ".join(THEMES),
    )
    return chain.json([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                      max_tokens=10000, max_tokens_ceiling=20000, required_keys=("summary",), task="synthesis")


def _clean_synthesis(data: Dict[str, Any], valid: set, latest_year: int) -> Dict[str, Any]:
    prov = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
    comp = data.get("completeness") if isinstance(data.get("completeness"), dict) else {}
    red = data.get("redaction") if isinstance(data.get("redaction"), dict) else {}
    handling = data.get("official_handling") if isinstance(data.get("official_handling"), dict) else {}
    assess = data.get("assessment") if isinstance(data.get("assessment"), dict) else {}
    ratings = data.get("ratings") if isinstance(data.get("ratings"), dict) else {}
    svp = ratings.get("svp") if isinstance(ratings.get("svp"), dict) else {}
    period = data.get("period_covered") if isinstance(data.get("period_covered"), dict) else {}

    def digit(value: Any, low: int, high: int) -> Optional[int]:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if low <= number <= high else None

    explanations = []
    for item in as_list(assess.get("candidate_explanations")):
        if isinstance(item, dict) and clean_str(item.get("explanation")):
            explanations.append({
                "explanation": clean_str(item.get("explanation"), 300),
                "category": _enum(item.get("category"), EXPLANATION_CATEGORIES, "other"),
                "likelihood": _enum(item.get("likelihood"), LIKELIHOODS, ""),
                "supporting": str_list(item.get("supporting"), 6), "contradicting": str_list(item.get("contradicting"), 6),
            })
    claims = []
    for item in as_list(assess.get("claimed_characteristics")):
        if isinstance(item, dict) and _enum(item.get("name"), OBSERVABLES):
            claims.append({"name": _enum(item.get("name"), OBSERVABLES), "basis": _enum(item.get("basis"), ["witness-estimate", "instrument"], "witness-estimate"),
                           "pages": _pages(item.get("pages"), valid)})
    complete = comp.get("appears_complete")
    reliability = clean_str(ratings.get("source_reliability")).upper()[:1]
    credibility = clean_str(ratings.get("information_credibility"))[:1]
    vallee = clean_str(ratings.get("vallee")).upper().replace(" ", "")
    return {
        "title": clean_str(data.get("title"), 220),
        "summary": str(data.get("summary") or "").strip()[:6000],
        "key_points": [{"text": clean_str(k.get("text"), 500), "pages": _pages(k.get("pages"), valid)}
                       for k in as_list(data.get("key_points")) if isinstance(k, dict) and clean_str(k.get("text"))][:10],
        "record_kind": _enum(data.get("record_kind"), RECORD_KINDS, "other"),
        "document_type": clean_str(data.get("document_type"), 160),
        "document_date": normalize_date(data.get("document_date_iso"), data.get("document_date_text"), latest_year),
        "period_covered": {"start": normalize_date(period.get("start"), "", latest_year)["iso"],
                           "end": normalize_date(period.get("end"), "", latest_year)["iso"]},
        "provenance": {
            "originating_agency": clean_str(prov.get("originating_agency"), 160), "originating_office": clean_str(prov.get("originating_office"), 200),
            "authors": str_list(prov.get("authors"), 12), "recipients": str_list(prov.get("recipients"), 12),
            "file_numbers": str_list(prov.get("file_numbers"), 20), "record_series": clean_str(prov.get("record_series"), 240),
            "original_classification": clean_str(prov.get("original_classification"), 120),
            "declassification": clean_str(prov.get("declassification"), 240), "distribution": clean_str(prov.get("distribution"), 240),
        },
        "completeness": {"appears_complete": complete if isinstance(complete, bool) else None,
                         "missing": str_list(comp.get("missing"), 15), "notes": clean_str(comp.get("notes"), 500)},
        "redaction": {"level": _enum(red.get("level"), REDACTION_LEVELS, ""), "exemptions": str_list(red.get("exemptions"), 12),
                      "withheld": clean_str(red.get("withheld"), 500)},
        "official_handling": {"investigating_bodies": str_list(handling.get("investigating_bodies"), 10),
                              "actions": str_list(handling.get("actions"), 10), "conclusion": clean_str(handling.get("conclusion"), 700),
                              "conclusion_pages": _pages(handling.get("conclusion_pages"), valid)},
        "assessment": {
            "disposition": _enum(assess.get("disposition"), DISPOSITIONS, "insufficient-information"),
            "rationale": clean_str(assess.get("rationale"), 1500), "candidate_explanations": explanations[:5],
            "claimed_characteristics": claims, "data_gaps": str_list(assess.get("data_gaps"), 10),
            "follow_up": str_list(assess.get("follow_up"), 10),
            "research_value": digit(assess.get("research_value"), 1, 5),
            "research_value_reason": clean_str(assess.get("research_value_reason"), 500),
        },
        "ratings": {
            "hynek": _enum(ratings.get("hynek"), HYNEK, "not-applicable"),
            "vallee": vallee if re.match(r"^(AN|FB|MA|CE)[1-5]$", vallee) else "",
            "svp": {"source": digit(svp.get("source"), 0, 4), "visit": digit(svp.get("visit"), 0, 4), "explanations": digit(svp.get("explanations"), 0, 4)},
            "source_reliability": reliability if reliability in "ABCDEF" and reliability else "",
            "information_credibility": credibility if credibility in "123456" and credibility else "",
            "notes": clean_str(ratings.get("notes"), 500),
        },
        "themes": [t for t in (_enum(x, THEMES) for x in as_list(data.get("themes"))) if t][:6],
        "_duplicates": [g for g in as_list(data.get("duplicate_incidents")) if isinstance(g, list)],
    }


# --------------------------------------------------------------------------- per-document driver

def _release_year(entry: Dict[str, Any]) -> int:
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", str(entry.get("release_date") or ""))
    if match:
        year = int(match.group(3))
        return year + 2000 if year < 100 else year
    return _YEAR_NOW


def analyze_document(entry: Dict[str, Any], settings: Settings, chain: llm.Chain, chunk_pool: ThreadPoolExecutor,
                     gazetteer: Gazetteer, force: bool = False) -> Dict[str, Any]:
    name = str(entry["filename"])
    doc_id = doc_id_for(name)
    units, info = load_units(entry, settings)
    out_path = settings.analysis_dir / f"{doc_id}.json"
    if not units:
        return {"id": doc_id, "filename": name, "status": "not-extracted", "error": info.get("error", "no content")}
    context_fields = {k: entry.get(k) for k in ("title", "agency", "release_date", "incident_date", "incident_location", "description_blurb", "media_type")}
    input_key = stable_hash([ANALYSIS_VERSION, CHUNK_CHARS, context_fields, [(u["n"], u["text"]) for u in units]], 24)
    cached = read_json(out_path)
    if cached and not force and cached.get("input_key") == input_key and cached.get("status") == "complete":
        return cached

    latest_year = _release_year(entry)
    valid = {u["n"] for u in units}
    norm_pages = {u["n"]: normalize_for_match(u["text"]) for u in units}
    chunks = make_chunks(units)
    chunk_cache_path = settings.analysis_dir / "chunks" / f"{doc_id}.json"
    chunk_cache: Dict[str, Any] = {} if force else (read_json(chunk_cache_path) or {})
    lock = threading.Lock()
    failed_chunks: List[Dict[str, Any]] = []

    def run_chunk(index: int) -> Optional[Dict[str, Any]]:
        chunk = chunks[index]
        key = stable_hash([ANALYSIS_VERSION, context_fields, [(u["n"], u["text"]) for u in chunk]], 24)
        with lock:
            if key in chunk_cache:
                return chunk_cache[key]
        if not any(u["text"].strip() for u in chunk):
            return {"segment_summary": "These pages contain no extractable text.", "_units": [u["n"] for u in chunk], "_provider": ""}
        try:
            # Chunks run in parallel, so continuity comes from the model's own
            # continues_previous flag rather than a carried summary.
            data = _extract_units(chunk, entry, chain, index + 1, len(chunks), "")
        except llm.LLMError as exc:
            with lock:
                failed_chunks.append({"pages": [chunk[0]["n"], chunk[-1]["n"]], "error": str(exc)[:300]})
            log(f"WARNING: {name} pages {chunk[0]['n']}-{chunk[-1]['n']} could not be analyzed: {str(exc)[:160]}")
            return None
        with lock:
            chunk_cache[key] = data
            atomic_write_json(chunk_cache_path, chunk_cache)
        return data

    results = [r for r in chunk_pool.map(run_chunk, range(len(chunks))) if r]

    incidents: List[Dict[str, Any]] = []
    passages: List[Dict[str, Any]] = []
    people: Dict[str, Dict[str, str]] = {}
    entity_lists: Dict[str, List[str]] = {k: [] for k in ("organizations", "places", "aircraft_equipment", "programs", "file_numbers")}
    records: List[Dict[str, Any]] = []
    providers: Dict[str, int] = {}
    for result in results:
        if result.get("_provider"):
            providers[result["_provider"]] = providers.get(result["_provider"], 0) + 1
        for raw in as_list(result.get("incidents")):
            if not isinstance(raw, dict):
                continue
            incident = _clean_incident(raw, valid, norm_pages, gazetteer, latest_year)
            if not incident:
                continue
            if incident["continues_previous"] and incidents:
                _merge_incident(incidents[-1], incident)
            else:
                incidents.append(incident)
        passages += _clean_quotes(result.get("key_passages"), valid, norm_pages, with_why=True)
        entities = result.get("entities") if isinstance(result.get("entities"), dict) else {}
        for person in as_list(entities.get("people")):
            if isinstance(person, dict) and clean_str(person.get("name")):
                key = clean_str(person.get("name")).lower()
                row = people.setdefault(key, {"name": clean_str(person.get("name"), 120), "role": "", "org": ""})
                for field in ("role", "org"):
                    if len(clean_str(person.get(field))) > len(row[field]):
                        row[field] = clean_str(person.get(field), 160)
        for key in entity_lists:
            entity_lists[key] += str_list(entities.get(key), 200)
        for record in as_list(result.get("records")):
            if isinstance(record, dict):
                records.append({
                    "pages": _pages(record.get("pages"), valid), "type": clean_str(record.get("type"), 80),
                    "date": clean_str(record.get("date"), 60), "from": clean_str(record.get("from"), 160),
                    "to": clean_str(record.get("to"), 160), "subject": clean_str(record.get("subject"), 300),
                    "file_numbers": str_list(record.get("file_numbers"), 8), "classification": clean_str(record.get("classification"), 80),
                })

    synthesis: Dict[str, Any] = {}
    synth_provider = ""
    synth_error = ""
    if results:
        try:
            raw_synthesis, synth_provider = _synthesize(entry, info, results, incidents, passages, chain)
            synthesis = _clean_synthesis(raw_synthesis, valid, latest_year)
        except llm.LLMError as exc:
            synth_error = str(exc)[:300]
            log(f"WARNING: {name}: synthesis failed: {synth_error[:160]}")

    # Merge duplicates the synthesis identified (same event described twice in the file).
    drop: set = set()
    for group in synthesis.pop("_duplicates", []) if synthesis else []:
        indexes = sorted({i for i in group if isinstance(i, int) and 0 <= i < len(incidents)} - drop)
        for other in indexes[1:]:
            _merge_incident(incidents[indexes[0]], incidents[other])
            drop.add(other)
    incidents = [inc for i, inc in enumerate(incidents) if i not in drop]
    incidents.sort(key=lambda inc: (inc["pages"][0] if inc["pages"] else 10 ** 6))
    for number, incident in enumerate(incidents, start=1):
        incident.pop("continues_previous", None)
        incident["id"] = f"{doc_id}--i{number:03d}"
        incident["documentation"] = documentation_score(incident)

    seen_passages: set = set()
    unique_passages = []
    for passage in passages:
        if passage["text"] not in seen_passages:
            seen_passages.add(passage["text"])
            unique_passages.append(passage)
    all_quotes = [q for inc in incidents for q in inc["quotes"]] + unique_passages
    status = "complete" if results and not failed_chunks and synthesis and not info.get("failed_pages") and not info.get("partial") else "incomplete"
    report = {
        "id": doc_id, "filename": name, "input_key": input_key, "analysis_version": ANALYSIS_VERSION,
        "status": status, "analyzed_at": now_iso(),
        "manifest": {k: entry.get(k) for k in (
            "title", "media_type", "agency", "release_date", "incident_date", "incident_location", "description_blurb",
            "original_source_url", "source_page_url", "dvids_video_url", "thumbnail_url", "modal_image_url", "video_pairing",
            "pdf_pairing", "duration_seconds", "size_bytes", "sha256", "upstream_status")},
        "extraction": info,
        "synthesis": synthesis,
        "incidents": incidents,
        "records": records,
        "key_passages": unique_passages[:40],
        "entities": {"people": sorted(people.values(), key=lambda p: p["name"])[:300],
                     **{k: str_list(v, 300) for k, v in entity_lists.items()}},
        "quality": {
            "chunks": len(chunks), "chunks_failed": failed_chunks, "synthesis_error": synth_error,
            "providers": providers, "synthesis_provider": synth_provider,
            "quotes_total": len(all_quotes),
            "quotes_verified": sum(1 for q in all_quotes if q["check"] == "verified"),
            "quotes_approximate": sum(1 for q in all_quotes if q["check"] == "approximate"),
            "quotes_unverified": sum(1 for q in all_quotes if q["check"] == "unverified"),
        },
    }
    atomic_write_json(out_path, report)
    return report


def run(settings: Settings, only: Optional[List[str]] = None, limit: Optional[int] = None, force: bool = False) -> Dict[str, Any]:
    from .extract import list_sources

    entries = list_sources(settings, only, limit)
    chain = llm.Chain(settings.review_chain(), timeout=settings.request_timeout)
    gazetteer = Gazetteer()
    stats = {"sources": len(entries), "complete": 0, "incomplete": 0, "not_extracted": 0, "incidents": 0, "issues": {}}
    done = [0]
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=settings.analysis_concurrency) as chunk_pool, \
            ThreadPoolExecutor(max_workers=max(2, settings.analysis_concurrency // 2)) as doc_pool:

        def work(entry: Dict[str, Any]) -> None:
            name = str(entry["filename"])
            try:
                report = analyze_document(entry, settings, chain, chunk_pool, gazetteer, force)
            except Exception as exc:
                report = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                log(f"WARNING: {name}: analysis crashed: {type(exc).__name__}: {exc}")
            with lock:
                done[0] += 1
                status = report.get("status")
                if status == "complete":
                    stats["complete"] += 1
                elif status == "not-extracted":
                    stats["not_extracted"] += 1
                    stats["issues"][name] = [report.get("error", "not extracted")]
                else:
                    stats["incomplete"] += 1
                    quality = report.get("quality") or {}
                    problems = [f"pages {c['pages'][0]}-{c['pages'][1]} not analyzed: {c['error']}" for c in quality.get("chunks_failed", [])]
                    if quality.get("synthesis_error"):
                        problems.append(f"synthesis failed: {quality['synthesis_error']}")
                    if report.get("error"):
                        problems.append(report["error"])
                    if (report.get("extraction") or {}).get("failed_pages"):
                        problems.append(f"pages without text: {report['extraction']['failed_pages'][:20]}")
                    stats["issues"][name] = problems or ["incomplete extraction"]
                stats["incidents"] += len(report.get("incidents") or [])
                log(f"[analyze {done[0]}/{len(entries)}] {name}: {status}, {len(report.get('incidents') or [])} incidents")

        list(doc_pool.map(work, entries))
    log(f"Analyze finished: {stats['complete']} complete, {stats['incomplete']} incomplete, "
        f"{stats['not_extracted']} not extracted, {stats['incidents']} incidents")
    return stats
