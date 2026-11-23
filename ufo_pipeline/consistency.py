"""Deterministic consistency rules applied to every report when the site is built.

The model's per-incident labels are checked against the facts recorded beside them, so a published label never
contradicts its own record. Nothing here calls a model, and each rule is described on the site's Methods page:

- dates       a camera/system default timestamp (12/31/99 ...) or a bare "19th century" is not an incident date;
              the publisher's incident date is used instead when it gives a year
- status      "unidentified" is kept only where the record states a conclusion or the publisher designates the case
              unresolved; a report with no recorded evaluation is "not-evaluated"
- hynek       nocturnal light / daylight disc / radar-visual must agree with the recorded light and sensors
- likelihood  shown only for a named conventional explanation
- position    model coordinates that match coordinates printed in the record are "stated", not "estimated"
- disposition a file the model called "not a sighting record" that nevertheless holds incidents is labelled as
              containing incidents (they are assessed one by one)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .analyze import documentation_score, normalize_date
from .connections import PLACEHOLDER_DATES
from .geo import distance_km, find_coords

_CENTURY_ONLY = re.compile(r"\bcentur(?:y|ies)\b", re.I)
_YEAR = re.compile(r"(?<!\d)(?:1[89]|20)\d\d(?!\d)")
STATED_KM = 100  # a printed coordinate this close to the model's position confirms it

CONTAINS_INCIDENTS = "contains-incidents"


def _bad_date(date: Dict[str, Any]) -> bool:
    text = str(date.get("text") or "")
    if date.get("iso") in PLACEHOLDER_DATES:
        return True
    return date.get("precision") == "year" and bool(_CENTURY_ONLY.search(text)) and not _YEAR.search(text)


def fix_date(date: Dict[str, Any], publisher_date: Any, latest_year: int) -> Dict[str, Any]:
    """Replace a placeholder or century-only date with the publisher's incident date (year only), or leave it undated."""
    if not date or not _bad_date(date):
        return date
    fallback = normalize_date("", publisher_date, latest_year)
    if fallback["year"]:
        return {"text": date.get("text") or "", "iso": f"{fallback['year']:04d}", "year": fallback["year"], "precision": "year",
                "corrected": "publisher's incident date used; the date in the record is a placeholder"}
    return {"text": date.get("text") or "", "iso": "", "year": None, "precision": "none",
            "corrected": "the date in the record is a placeholder or too vague to place"}


def record_status(incident: Dict[str, Any], publisher_title: str) -> str:
    """Status the record itself gives. "unidentified" needs a stated conclusion or an official "unresolved" designation."""
    status = incident.get("status_in_record") or "not-evaluated"
    if status != "unidentified":
        return status
    if str(incident.get("official_explanation") or "").strip() or re.search(r"\bunresolved\b", publisher_title or "", re.I):
        return status
    return "not-evaluated"


def consistent_hynek(hynek: str, light: str, modes: List[str]) -> str:
    """Hynek's distant-sighting classes are defined by light and sensor, so they can be checked against the record."""
    if hynek not in ("NL", "DD", "RV"):
        return hynek
    radar_visual = any(m.startswith("radar") for m in modes) and any(m.startswith("visual") for m in modes)
    if hynek == "RV" and not radar_visual:
        return "DD" if light == "day" else "NL" if light == "night" else "not-applicable"
    if hynek == "NL" and light == "day":
        return "DD"
    if hynek == "DD" and light == "night":
        return "NL"
    return hynek


def stated_position(incident: Dict[str, Any], page_text: Dict[int, str]) -> Optional[Tuple[float, float]]:
    """The printed coordinate that confirms an estimated position, if the record contains one."""
    location = incident["location"]
    if location.get("precision") != "estimated" or location.get("lat") is None:
        return None
    here = (location["lat"], location["lon"])
    raw = location.get("raw") or {}
    texts = [str(raw.get("name") or ""), str(location.get("label") or "")] + [page_text.get(n, "") for n in incident.get("pages") or []]
    best: Optional[Tuple[float, Tuple[float, float]]] = None
    for text in texts:
        for point in find_coords(text):
            km = distance_km(here, point)
            if km <= STATED_KM and (best is None or km < best[0]):
                best = (km, point)
    return best[1] if best else None


def apply(report: Dict[str, Any], entry: Dict[str, Any], page_text: Dict[int, str], latest_year: int) -> Dict[str, int]:
    """Apply every rule to one report in place. Returns how many values each rule changed."""
    changed = {"dates": 0, "status": 0, "hynek": 0, "likelihood": 0, "stated_positions": 0, "disposition": 0}
    publisher_date = entry.get("incident_date")
    title = str(entry.get("title") or "")
    for incident in report.get("incidents") or []:
        date = fix_date(incident["date"], publisher_date, latest_year)
        if date is not incident["date"]:
            incident["date"] = date
            changed["dates"] += 1
        status = record_status(incident, title)
        if status != incident["status_in_record"]:
            incident["status_as_extracted"] = incident["status_in_record"]
            incident["status_in_record"] = status
            changed["status"] += 1
        assessment = incident["assessment"]
        hynek = consistent_hynek(assessment["hynek"], incident["conditions"]["light"], incident["observation"]["modes"])
        if hynek != assessment["hynek"]:
            assessment["hynek_as_extracted"] = assessment["hynek"]
            assessment["hynek"] = hynek
            changed["hynek"] += 1
        if assessment["explanation_category"] == "none-identified" and assessment.get("likelihood"):
            assessment["likelihood"] = ""  # a likelihood with no named explanation says nothing a reader can use
            changed["likelihood"] += 1
        point = stated_position(incident, page_text)
        if point:
            incident["location"].update({"lat": point[0], "lon": point[1], "precision": "stated"})
            changed["stated_positions"] += 1
        incident["documentation"] = documentation_score(incident)

    synthesis = report.get("synthesis") or {}
    if synthesis.get("document_date"):
        fixed = fix_date(synthesis["document_date"], publisher_date, latest_year)
        if fixed is not synthesis["document_date"]:
            synthesis["document_date"] = fixed
            changed["dates"] += 1
    period = synthesis.get("period_covered") or {}
    for key in ("start", "end"):
        if period.get(key) in PLACEHOLDER_DATES:
            period[key] = (synthesis.get("document_date") or {}).get("iso") or ""
    assessment = synthesis.get("assessment") or {}
    if assessment.get("disposition") == "not-a-sighting-record" and report.get("incidents"):
        assessment["disposition_as_extracted"] = assessment["disposition"]
        assessment["disposition"] = CONTAINS_INCIDENTS
        changed["disposition"] += 1
    return changed

