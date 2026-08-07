#!/usr/bin/env python3

"""Build the evidence-audited scientific companion to the UFO dashboard.

This deliberately does not try to turn an unidentified observation into an
identity by keyword alone.  It scores whether the released record contains the
minimum information needed to discriminate hypotheses, records the most
economical conventional hypothesis class, and preserves unresolved cases as
unresolved when the public evidence cannot adjudicate them.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape as html_escape
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO_ROOT / "data" / "processed" / "ufo_dashboard_analysis.json"
DEFAULT_MANIFEST = REPO_ROOT / "data" / "processed" / "source_manifest.json"
DEFAULT_OUTPUT_DATA = REPO_ROOT / "data" / "processed" / "scientific_assessment.json"
DEFAULT_TEMPLATE = REPO_ROOT / "tools" / "templates" / "scientific_analysis.html"
DEFAULT_OUTPUT_HTML = REPO_ROOT / "dashboard" / "scientific-analysis.html"

ANALYSIS_VERSION = 1


REFERENCE_SOURCES = [
    {
        "id": "pursue_archive",
        "title": "PURSUE UAP release archive",
        "publisher": "U.S. Department of War",
        "year": 2026,
        "url": "https://www.war.gov/ufo/",
        "role": "Controlling source catalog for released files and the government's definition of unresolved.",
    },
    {
        "id": "pursue_western_update",
        "title": "AARO case update: Western U.S. “Orbs Launching Orbs”",
        "publisher": "U.S. Department of War / AARO",
        "year": 2026,
        "url": "https://www.war.gov/medialink/ufo/061226/release_03/documents/DOW-UAP-D077_Unresolved-Case-Analysis-Update_Western-United-States-Event.pdf",
        "role": "Primary case analysis: roughly 60% plausibly matches countermeasure flares; the remainder lacks technical corroboration.",
    },
    {
        "id": "nasa_uap_study",
        "title": "Unidentified Anomalous Phenomena Independent Study Team Report",
        "publisher": "NASA",
        "year": 2023,
        "url": "https://www.nasa.gov/wp-content/uploads/2023/09/uap-independent-study-team-final-report-0.pdf",
        "role": "Scientific framework emphasizing calibrated, reproducible, multisensor observations and complete metadata.",
    },
    {
        "id": "aaro_annual_2024",
        "title": "FY 2024 Consolidated Annual Report on UAP",
        "publisher": "ODNI / Department of Defense / AARO",
        "year": 2024,
        "url": "https://www.dni.gov/files/ODNI/documents/assessments/DOD-AARO-Consolidated-Annual-Report-on-UAP-Nov2024.pdf",
        "role": "Base-rate comparison: resolved cases were prosaic and most archived cases lacked enough actionable data.",
    },
    {
        "id": "aaro_history_2024",
        "title": "Historical Record Report on U.S. Government Involvement with UAP, Volume I",
        "publisher": "AARO",
        "year": 2024,
        "url": "https://www.aaro.mil/Portals/136/PDFs/AARO_Historical_Record_Report_Vol_1_2024.pdf",
        "role": "Historical baseline for ordinary-object misidentification, secret-program confusion, reporting waves, and evidence limitations.",
    },
    {
        "id": "aaro_gofast",
        "title": "GoFast case resolution and methodology",
        "publisher": "AARO",
        "year": 2025,
        "url": "https://www.aaro.mil/Portals/136/PDFs/case_resolution_reports/AARO_GoFast_Case_Resolution_Card_Methodology_Final.pdf",
        "role": "Worked example showing how range geometry, wind, and motion parallax remove apparent extraordinary speed.",
    },
    {
        "id": "aaro_puerto_rico",
        "title": "Puerto Rico UAP case resolution",
        "publisher": "AARO",
        "year": 2025,
        "url": "https://www.aaro.mil/Portals/136/PDFs/case_resolution_reports/AARO_Puerto_Rico_UAP_Case_Resolution.pdf",
        "role": "Worked example of parallax, thermal crossover, and two nearby objects creating apparent splitting/transmedium behavior.",
    },
    {
        "id": "aaro_mt_etna",
        "title": "Mt. Etna object case resolution",
        "publisher": "AARO",
        "year": 2025,
        "url": "https://www.aaro.mil/Portals/136/PDFs/case_resolution_reports/Mt-Etna-Object.pdf",
        "role": "Worked example of parallax, uncertain range, atmosphere, and sensor processing producing apparent high performance.",
    },
    {
        "id": "aaro_al_taqaddum",
        "title": "Al Taqaddum object case resolution",
        "publisher": "AARO",
        "year": 2025,
        "url": "https://www.aaro.mil/Portals/136/PDFs/case_resolution_reports/AARO_Al_Taqaddam_Case_Resolution_Final.pdf",
        "role": "Worked example using wind, line of sight, morphology, and thermal signature to identify a balloon cluster.",
    },
    {
        "id": "aaro_starlink",
        "title": "Correlations of Starlink Satellite Flaring with UAP Observations",
        "publisher": "AARO",
        "year": 2024,
        "url": "https://www.aaro.mil/Portals/136/PDFs/Information%20Papers/AARO_Satellite_Flaring_Paper.pdf",
        "role": "Geometry and timing of satellite flares that can appear as repeating or stationary lights near the horizon.",
    },
    {
        "id": "faa_vision",
        "title": "Pilot’s Handbook of Aeronautical Knowledge, Chapter 17",
        "publisher": "Federal Aviation Administration",
        "year": 2023,
        "url": "https://www.faa.gov/sites/faa.gov/files/pilot_handbook_1.pdf",
        "role": "Operational reference for night-vision limits, autokinesis, false horizons, and size–distance ambiguity.",
    },
    {
        "id": "blue_book_archive",
        "title": "Project BLUE BOOK — Unidentified Flying Objects",
        "publisher": "U.S. National Archives",
        "year": 2024,
        "url": "https://www.archives.gov/research/military/air-force/ufos",
        "role": "Primary archival context and the 12,618-report / 701-unidentified historical baseline.",
    },
    {
        "id": "fbi_ufo_files",
        "title": "FBI UFO files, Parts 1–16",
        "publisher": "Federal Bureau of Investigation",
        "year": 2011,
        "url": "https://vault.fbi.gov/UFO",
        "role": "Primary FBI records used to cross-check hoaxes, second-hand claims, and administrative forwarding.",
    },
    {
        "id": "roswell_report",
        "title": "The Roswell Report: Case Closed",
        "publisher": "U.S. Air Force",
        "year": 1997,
        "url": "https://www.af.mil/The-Roswell-Report/",
        "role": "Official records-based explanation of debris, balloon research, and later conflated human-test events.",
    },
    {
        "id": "ornl_material",
        "title": "Synopsis: Analysis of a Metallic Specimen",
        "publisher": "Oak Ridge National Laboratory / AARO",
        "year": 2024,
        "url": "https://www.aaro.mil/Portals/136/PDFs/Information%20Papers/ORNL-Synopsis_Analysis_of_a_Metallic_Specimen.pdf",
        "role": "Laboratory analysis finding terrestrial composition and no claimed anomalous waveguide property.",
    },
]


PROVENANCE_POINTS = {
    "original_or_near_original_report": 20,
    "official_summary_or_memo": 16,
    "official_media_release": 14,
    "archival_clipping_or_secondhand": 8,
    "uploaded_or_altered_media": 2,
    "unknown": 4,
}

MEASUREMENT_POINTS = {
    "calibrated_telemetry": 25,
    "instrument_derived_values": 20,
    "estimated_values": 10,
    "qualitative_only": 4,
    "no_measurements": 0,
    "unknown": 0,
}

TIME_POINTS = {
    "exact_utc": 5,
    "exact_local": 5,
    "full_date": 4,
    "month_year": 2,
    "year_only": 1,
    "range_only": 1,
    "unknown": 0,
}

LOCATION_POINTS = {
    "coordinates": 5,
    "site_or_facility": 4,
    "city_or_local_area": 4,
    "region_or_maritime_area": 2,
    "country": 1,
    "space_or_orbit": 3,
    "command_area": 1,
    "unknown_redacted": 0,
    "unknown": 0,
}

CORROBORATION_POINTS = {
    "multiple_sensors": 20,
    "radar_or_sensor": 14,
    "physical_trace": 12,
    "photo_or_video": 10,
    "multiple_witnesses": 8,
    "official_investigation": 6,
    "transcript_or_audio": 4,
    "none_stated": 0,
}

EXPLANATION_PATTERNS = [
    ("Fabrication / hoax", ("hoax", "prank", "fabricat", "constructed by", "teenage boys"), "fbi_ufo_files"),
    ("Balloon or lighter-than-air object", ("balloon", "aerostat", "radiosonde", "weather balloon", "lighter-than-air"), "aaro_al_taqaddum"),
    ("Uncrewed aircraft / drone", ("drone", "uas", "uav", "quadrotor", "unmanned aerial"), "aaro_annual_2024"),
    ("Conventional aircraft", ("aircraft", "airplane", "aeroplane", "commercial flight", "helicopter", "jet aircraft", "f-"), "aaro_annual_2024"),
    ("Satellite flare or orbital object", ("starlink", "satellite", "orbital", "spacecraft debris"), "aaro_starlink"),
    ("Rocket, missile, or re-entry", ("rocket", "missile", "re-entry", "reentry", "launch plume", "booster"), "aaro_history_2024"),
    ("Meteor / astronomical fireball", ("meteor", "bolide", "fireball", "shooting star"), "nasa_uap_study"),
    ("Celestial light source", ("venus", "planet", "star", "astronomical", "celestial"), "faa_vision"),
    ("Bird or other wildlife", ("bird", "birds", "insect"), "aaro_annual_2024"),
    ("Sensor or viewing-geometry artifact", ("parallax", "glare", "sensor artifact", "camera artifact", "thermal crossover", "autofocus", "pixelation", "compression artifact"), "aaro_gofast"),
    ("Atmospheric / optical phenomenon", ("temperature inversion", "atmospheric", "cloud", "ice crystal", "lightning", "reflection", "refraction"), "nasa_uap_study"),
    ("Ordinary terrestrial material", ("terrestrial alloy", "magnesium", "bismuth", "metal specimen", "ordinary material"), "ornl_material"),
]

CONTEXT_RECORD_TYPES = {"non_event_context", "policy_or_research_discussion"}
CONTEXT_RESOLUTIONS = {"informational_only", "not_an_event"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--output-data", type=Path, default=DEFAULT_OUTPUT_DATA)
    parser.add_argument("--output-html", type=Path, default=DEFAULT_OUTPUT_HTML)
    return parser.parse_args()


def normalized_text(record: dict[str, Any]) -> str:
    values: list[str] = []
    for key in (
        "title",
        "summary_narrative",
        "visual_observations",
        "media_authenticity_notes",
        "document_type",
        "event_record_type",
    ):
        value = record.get(key)
        if value:
            values.append(str(value))
    values.extend(str(value) for value in record.get("themes") or [])
    values.extend(str(value) for value in record.get("top_terms") or [])
    return " ".join(values).lower()


def safe_year(record: dict[str, Any]) -> int | None:
    for key in ("incident_year", "year", "source_year", "release_year"):
        value = record.get(key)
        if isinstance(value, int) and 1800 <= value <= 2100:
            return value
        if isinstance(value, str) and value.isdigit():
            parsed = int(value)
            if 1800 <= parsed <= 2100:
                return parsed
    for key in ("incident_date_label", "date_label", "source_date_label"):
        match = re.search(r"\b(18|19|20)\d{2}\b", str(record.get(key) or ""))
        if match:
            return int(match.group(0))
    return None


def decade_label(year: int | None) -> str:
    if year is None:
        return "Undated"
    return f"{year // 10 * 10}s"


def family_label(record: dict[str, Any]) -> str:
    text = normalized_text(record)
    title = str(record.get("title") or "")
    year = safe_year(record)
    if "western united states event" in text or "orbs launching" in text:
        return "Western U.S. October 2023 event"
    if "fosterbrook" in text:
        return "Twin Falls Fosterbrook hoax (1947)"
    if "gulf of oman" in text and year == 2021:
        return "Gulf of Oman 2021 release cluster"
    if "east china sea" in text:
        return f"East China Sea {year or 'undated'} cluster"
    if "south china sea" in text:
        return f"South China Sea {year or 'undated'} cluster"
    if "middle east" in text:
        return f"Middle East {year or 'undated'} cluster"
    if "project blue book" in text:
        return "Project BLUE BOOK historical record"
    if record.get("source_document_title"):
        return str(record["source_document_title"])
    return title or str(record.get("source_id") or "Unlabeled record")


def corroboration_score(record: dict[str, Any]) -> int:
    values = set(record.get("corroboration_types") or [])
    if "none_stated" in values and len(values) > 1:
        values.remove("none_stated")
    points = sorted((CORROBORATION_POINTS.get(value, 0) for value in values), reverse=True)
    if not points:
        return 0
    return min(25, points[0] + sum(points[1:]) // 3)


def evidence_score(record: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    provenance = PROVENANCE_POINTS.get(str(record.get("chain_of_custody_quality") or "unknown"), 4)
    measurement = MEASUREMENT_POINTS.get(str(record.get("measurement_quality") or "unknown"), 0)
    corroboration = corroboration_score(record)
    time_score = TIME_POINTS.get(str(record.get("event_time_precision") or "unknown"), 0)
    location_score = LOCATION_POINTS.get(str(record.get("location_precision") or "unknown"), 0)
    quantitative_fields = [value for value in record.get("quantitative_fields_present") or [] if value != "none"]
    quantitative = min(5, len(quantitative_fields) * 2)
    documentation = 5 if record.get("review_status") == "reviewed" else 2
    # Components total 90 raw points. Normalize to an intuitive 0–100 scale
    # without obscuring the auditable component values shown in the report.
    raw_score = provenance + measurement + corroboration + time_score + location_score + quantitative + documentation
    score = min(100, round(raw_score * (100 / 90)))
    components = [
        {"label": "Provenance", "value": provenance, "max": 20},
        {"label": "Measurement", "value": measurement, "max": 25},
        {"label": "Corroboration", "value": corroboration, "max": 25},
        {"label": "Time / place", "value": time_score + location_score, "max": 10},
        {"label": "Quantitative fields", "value": quantitative, "max": 5},
        {"label": "Documentation", "value": documentation, "max": 5},
    ]
    return score, components


def quality_band(score: int) -> str:
    if score >= 70:
        return "High-information"
    if score >= 50:
        return "Moderate-information"
    return "Limited-information"


def explanation_from_text(record: dict[str, Any]) -> tuple[str | None, str | None]:
    text = normalized_text(record)
    for label, patterns, reference_id in EXPLANATION_PATTERNS:
        if any(pattern in text for pattern in patterns):
            return label, reference_id
    return None, None


def default_explanation(record: dict[str, Any]) -> tuple[str, str]:
    mode = str(record.get("primary_observation_mode") or "unknown")
    context = str(record.get("day_night_context") or "unknown")
    media_type = str(record.get("media_type") or "unknown")
    year = safe_year(record)
    text = normalized_text(record)

    if mode in {"ir_thermal", "eo_video"} or media_type == "video":
        return "Conventional airborne object plus unresolved range/sensor geometry", "aaro_gofast"
    if mode in {"photo_image"} or media_type == "image":
        return "Ordinary object or imaging artifact; single frames rarely constrain range or motion", "nasa_uap_study"
    if mode in {"radar", "rf_sigint"}:
        return "Aircraft, balloon, clutter, or propagation effect; raw track data are required", "aaro_annual_2024"
    if context == "night" or any(token in text for token in ("light", "luminous", "glow", "orb")):
        return "Distant aircraft/satellite/celestial light or flare remains plausible", "faa_vision"
    if year is not None and year < 1970:
        return "Aircraft, balloon, astronomical object, experimental system, or reporting artifact", "aaro_history_2024"
    return "Conventional object or perceptual/sensor ambiguity", "nasa_uap_study"


def data_gaps(record: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    if str(record.get("measurement_quality") or "unknown") in {"unknown", "no_measurements", "qualitative_only"}:
        gaps.append("No instrument-grade kinematics")
    if str(record.get("event_time_precision") or "unknown") in {"unknown", "range_only", "year_only", "month_year"}:
        gaps.append("Time too imprecise for flight/astronomy correlation")
    if str(record.get("location_precision") or "unknown") in {"unknown", "unknown_redacted", "country", "command_area"}:
        gaps.append("Location too imprecise for reconstruction")
    corroboration = set(record.get("corroboration_types") or [])
    if not corroboration or corroboration == {"none_stated"}:
        gaps.append("No independent corroboration stated")
    if str(record.get("chain_of_custody_quality") or "unknown") in {"unknown", "archival_clipping_or_secondhand", "uploaded_or_altered_media"}:
        gaps.append("Weak or incomplete chain of custody")
    if str(record.get("primary_observation_mode") or "unknown") in {"unknown", "none_stated", "naked_eye_visual", "ground_visual", "cockpit_visual"}:
        gaps.append("Range/size cannot be recovered from testimony alone")
    if record.get("media_type") in {"video", "image"} and str(record.get("measurement_quality") or "unknown") != "calibrated_telemetry":
        gaps.append("Original sensor metadata/calibration unavailable")
    return list(dict.fromkeys(gaps))[:5]


def is_derivative(record: dict[str, Any]) -> bool:
    text = normalized_text(record)
    return any(token in text for token in ("digital rendering", "digital recreation", "artist's conception", "artistic interpretation", "notional map"))


def assess_record(record: dict[str, Any]) -> dict[str, Any]:
    score, components = evidence_score(record)
    band = quality_band(score)
    title = str(record.get("title") or "Untitled record")
    text = normalized_text(record)
    resolution = str(record.get("resolution_status") or "unknown")
    mundane = str(record.get("mundane_explanation_present") or "unclear")
    event_type = str(record.get("event_record_type") or "unknown")
    refs = ["pursue_archive"]
    special_note: str | None = None

    explanation, explanation_ref = explanation_from_text(record)
    if explanation_ref:
        refs.append(explanation_ref)

    if "fosterbrook disc recovery" in title.lower() or "mrs. fosterbrook" in text:
        assessment = "Explained — fabrication"
        explanation = "Teenage hoax assembled from phonograph/jukebox and radio parts"
        confidence = "High"
        refs.extend(["fbi_ufo_files", "aaro_history_2024"])
        special_note = (
            "The initial FBI memo is unresolved in this row, but another FBI file in the same corpus records the boys’ admission "
            "and the Army’s inspection. This is a cross-record correction to the generated label."
        )
    elif "western united states event" in text or "orbs launching" in text:
        refs.extend(["pursue_western_update", "faa_vision"])
        if is_derivative(record):
            assessment = "Context — derivative visualization"
            explanation = "Illustration/recreation of testimony; not an independent observation"
            confidence = "High"
            special_note = "Counted as documentation of a witness account, not as a second sensor or witness."
            score = max(8, score - 20)
            components = evidence_score({**record, "measurement_quality": "no_measurements", "corroboration_types": ["none_stated"]})[1]
            band = quality_band(score)
        else:
            assessment = "Mixed — partially explained, remainder unresolved"
            explanation = "Military infrared countermeasure flares plausibly explain about 60%; remaining reports rely on testimony alone"
            confidence = "Moderate"
            special_note = (
                "AARO found flight/radar/ADS-B alignment for roughly 60% of the activity. The residual 40% is worthy of follow-up "
                "but is not technical evidence of unrecognized technology."
            )
    elif is_derivative(record):
        assessment = "Context — derivative visualization"
        explanation = "Illustration or recreation derived from testimony"
        confidence = "High"
        refs.append("nasa_uap_study")
        special_note = "Derivative media cannot independently corroborate the event it depicts."
        score = max(8, score - 18)
        band = quality_band(score)
    elif resolution in CONTEXT_RESOLUTIONS or event_type in CONTEXT_RECORD_TYPES:
        assessment = "Context / not an event"
        explanation = explanation or "Administrative, research, or historical context rather than a discrete observation"
        confidence = "High" if resolution in CONTEXT_RESOLUTIONS else "Moderate"
        refs.append("aaro_history_2024")
    elif resolution == "explained" or mundane == "yes_source_resolved":
        assessment = "Explained / identified"
        explanation = explanation or "Conventional explanation recorded in the source review"
        confidence = "High" if mundane == "yes_source_resolved" else "Moderate"
    elif resolution == "partially_explained" or mundane == "yes_source_suggested":
        assessment = "Likely conventional / partially explained"
        explanation = explanation or default_explanation(record)[0]
        if not explanation_ref:
            refs.append(default_explanation(record)[1])
        confidence = "Moderate" if score >= 45 else "Low"
    elif resolution == "disputed":
        assessment = "Disputed / unverified claim"
        explanation = explanation or "Claim is contested and lacks decisive public evidence"
        confidence = "Low"
        refs.extend(["fbi_ufo_files", "aaro_history_2024"])
    else:
        strong_corroboration = bool(
            set(record.get("corroboration_types") or [])
            & {"multiple_sensors", "radar_or_sensor", "physical_trace"}
        )
        if score >= 68 and strong_corroboration:
            assessment = "Unresolved — stronger public evidence"
        elif score >= 50:
            assessment = "Unresolved — follow-up warranted"
        else:
            assessment = "Unresolved — insufficient data"
        if explanation is None:
            explanation, default_ref = default_explanation(record)
            refs.append(default_ref)
        confidence = "Low" if score < 50 else "Moderate"

    if record.get("primary_observation_mode") in {"ir_thermal", "eo_video"} or record.get("media_type") == "video":
        refs.extend(["aaro_gofast", "aaro_puerto_rico"])
    if str(record.get("day_night_context") or "unknown") == "night":
        refs.append("faa_vision")
    if safe_year(record) and safe_year(record) < 1970:
        refs.extend(["blue_book_archive", "aaro_history_2024"])
    if any(token in text for token in ("metal", "debris", "alloy", "material")):
        refs.append("ornl_material")
    if any(token in text for token in ("satellite", "orbital", "space")):
        refs.append("aaro_starlink")

    unresolved_bonus = 18 if assessment.startswith("Unresolved") or assessment.startswith("Mixed") else 0
    sensor_bonus = 8 if set(record.get("corroboration_types") or []) & {"multiple_sensors", "radar_or_sensor"} else 0
    no_mundane_bonus = 5 if mundane == "no" else 0
    priority = min(100, round(score * 0.68 + unresolved_bonus + sensor_bonus + no_mundane_bonus))
    if assessment.startswith(("Explained", "Context")):
        priority = min(priority, 25)

    gaps = data_gaps(record)
    summary = str(record.get("summary_narrative") or "No source summary is available.").strip()
    visual = str(record.get("visual_observations") or "").strip()
    location = record.get("location")
    if isinstance(location, dict):
        location_label = str(location.get("label") or "Location not resolved")
    else:
        location_label = str(location or "Location not resolved")

    return {
        "id": str(record.get("source_id") or record.get("filename") or title),
        "title": title,
        "filename": str(record.get("filename") or ""),
        "source_document_id": str(record.get("source_document_id") or record.get("parent_source_id") or record.get("source_id") or ""),
        "source_document_title": str(record.get("source_document_title") or title),
        "source_document_filename": str(record.get("source_document_filename") or record.get("parent_filename") or record.get("filename") or ""),
        "source_href": str(record.get("source_href") or record.get("original_source_url") or ""),
        "original_source_url": str(record.get("original_source_url") or ""),
        "summary": summary,
        "visual_observations": visual,
        "assessment": assessment,
        "best_fit_explanation": explanation,
        "confidence": confidence,
        "evidence_score": score,
        "evidence_band": band,
        "priority_score": priority,
        "score_components": components,
        "data_gaps": gaps,
        "special_note": special_note,
        "research_refs": list(dict.fromkeys(refs))[:6],
        "event_family": family_label(record),
        "year": safe_year(record),
        "decade": decade_label(safe_year(record)),
        "date_label": str(record.get("incident_date_label") or record.get("date_label") or "Undated"),
        "location": location_label,
        "media_type": str(record.get("media_type") or "unknown"),
        "document_type": str(record.get("document_type") or "Unknown"),
        "record_type": event_type,
        "observation_mode": str(record.get("primary_observation_mode") or "unknown"),
        "resolution_status": resolution,
        "measurement_quality": str(record.get("measurement_quality") or "unknown"),
        "chain_of_custody": str(record.get("chain_of_custody_quality") or "unknown"),
        "corroboration": record.get("corroboration_types") or [],
        "quantitative_fields": [value for value in record.get("quantitative_fields_present") or [] if value != "none"],
        "evidence_category": str(record.get("evidence_category") or "Unclassified"),
        "agencies": record.get("agencies") or [],
        "themes": record.get("themes") or [],
        "capabilities": record.get("capability_labels") or [],
        "is_derivative": is_derivative(record),
    }


def filename_key(record: dict[str, Any]) -> str:
    return str(
        record.get("source_document_filename")
        or record.get("parent_filename")
        or record.get("filename")
        or ""
    )


def aggregate_source_reports(
    documents: list[dict[str, Any]],
    incident_assessments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_filename: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for incident in incident_assessments:
        by_filename[incident.get("source_document_filename") or incident.get("filename")].append(incident)

    reports: list[dict[str, Any]] = []
    for document in documents:
        base = assess_record(document)
        children = by_filename.get(str(document.get("filename") or ""), [])
        if not children:
            children = [base]
        assessment_counts = Counter(child["assessment"] for child in children)
        explanation_counts = Counter(child["best_fit_explanation"] for child in children)
        reference_ids = list(dict.fromkeys(ref for child in children for ref in child["research_refs"]))[:8]
        gap_counts = Counter(gap for child in children for gap in child["data_gaps"])
        strongest = max(children, key=lambda child: (child["priority_score"], child["evidence_score"]))
        if len(assessment_counts) == 1:
            assessment = next(iter(assessment_counts))
        else:
            assessment = "Mixed evidence file"
        top_explanations = [label for label, _ in explanation_counts.most_common(3)]
        mean_score = round(sum(child["evidence_score"] for child in children) / len(children))
        conclusion_bits = ", ".join(f"{count} {label.lower()}" for label, count in assessment_counts.most_common(3))
        base.update(
            {
                "assessment": assessment,
                "best_fit_explanation": "; ".join(top_explanations),
                "confidence": strongest["confidence"],
                "evidence_score": mean_score,
                "evidence_band": quality_band(mean_score),
                "priority_score": strongest["priority_score"],
                "data_gaps": [gap for gap, _ in gap_counts.most_common(5)],
                "research_refs": reference_ids,
                "incident_count": len(children),
                "assessment_counts": dict(assessment_counts),
                "strongest_incident_id": strongest["id"],
                "strongest_incident_title": strongest["title"],
                "special_note": (
                    f"This physical source contains {len(children)} incident-level record(s): {conclusion_bits}. "
                    f"The highest follow-up priority is “{strongest['title']}.”"
                ),
            }
        )
        reports.append(base)
    return reports


def count_values(records: Iterable[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counts = Counter(str(record.get(field) or "unknown") for record in records)
    return [{"label": label, "count": count} for label, count in counts.most_common()]


def count_list_values(records: Iterable[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counts = Counter(value for record in records for value in record.get(field) or [])
    return [{"label": label, "count": count} for label, count in counts.most_common()]


def ratio(numerator: int, denominator: int) -> float:
    """Return a one-decimal percentage for display and audit exports."""
    return round(numerator / denominator * 100, 1) if denominator else 0.0


def build_timeline(incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for incident in incidents:
        buckets[incident["decade"]].append(incident)
    ordered = sorted(
        buckets.items(),
        key=lambda item: (9999 if item[0] == "Undated" else int(item[0][:-1])),
    )
    return [
        {
            "decade": decade,
            "count": len(items),
            "limited": sum(item["evidence_band"] == "Limited-information" for item in items),
            "moderate": sum(item["evidence_band"] == "Moderate-information" for item in items),
            "high": sum(item["evidence_band"] == "High-information" for item in items),
            "unresolved": sum(item["assessment"].startswith(("Unresolved", "Mixed")) for item in items),
        }
        for decade, items in ordered
    ]


def build_quality_findings(raw_incidents: list[dict[str, Any]], assessed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = len(raw_incidents)
    non_quantitative = sum(
        str(record.get("measurement_quality") or "unknown") in {"qualitative_only", "no_measurements", "unknown"}
        for record in raw_incidents
    )
    no_corroboration = sum(set(record.get("corroboration_types") or []) in (set(), {"none_stated"}) for record in raw_incidents)
    imprecise_location = sum(
        str(record.get("location_precision") or "unknown") in {"unknown_redacted", "unknown", "country", "command_area"}
        for record in raw_incidents
    )
    category_one_weak_measurement = sum(
        record.get("evidence_category") == "Category One"
        and str(record.get("measurement_quality") or "unknown") in {"qualitative_only", "no_measurements", "unknown"}
        for record in raw_incidents
    )
    resolved_mismatch = sum(
        str(record.get("resolution_status") or "unknown") == "explained"
        and str(record.get("mundane_explanation_present") or "unclear") == "no"
        for record in raw_incidents
    )
    family_counts = Counter(item["event_family"] for item in assessed)
    repeated_assets = sum(count for family, count in family_counts.items() if count > 2)
    return [
        {
            "severity": "High",
            "finding": "Most records cannot support a kinematic reconstruction",
            "evidence": f"{non_quantitative} of {total} incidents ({non_quantitative / total:.1%}) are qualitative-only, explicitly have no measurements, or have unknown measurement quality.",
            "risk": "Apparent speed, size, acceleration, silence, and distance cannot be independently calculated.",
        },
        {
            "severity": "High",
            "finding": "Corroboration is frequently absent or not stated",
            "evidence": f"{no_corroboration} of {total} incidents ({no_corroboration / total:.1%}) state no corroboration or provide none in the coded record.",
            "risk": "A compelling account may still be a single-sensor or single-observer ambiguity.",
        },
        {
            "severity": "High",
            "finding": "The existing evidence category is not a scientific certainty scale",
            "evidence": f"{category_one_weak_measurement} Category One records still have qualitative, absent, or unknown measurement quality; the Fosterbrook physical-trace entry is a documented hoax.",
            "risk": "Treating Category One as 'most anomalous' overstates what the underlying record establishes.",
        },
        {
            "severity": "Medium",
            "finding": "Release packaging inflates apparent independence",
            "evidence": f"{repeated_assets} incident/media rows belong to event families represented by more than two released records or derivative assets.",
            "risk": "Statements, renderings, clips, and follow-up memos from one event can look like independent corroboration if counted naively.",
        },
        {
            "severity": "Medium",
            "finding": "Time/place precision blocks external correlation",
            "evidence": f"{imprecise_location} of {total} incidents ({imprecise_location / total:.1%}) have country-level, command-area, unknown, or redacted locations.",
            "risk": "Weather, satellite, launch, ADS-B, and astronomical hypotheses cannot be tested reproducibly.",
        },
        {
            "severity": "Low" if not resolved_mismatch else "Medium",
            "finding": "Cross-field resolution coding needs automated checks",
            "evidence": f"{resolved_mismatch} rows are marked explained while also coded as having no mundane explanation.",
            "risk": "Downstream counts can silently contradict the narrative source.",
        },
    ]


def notable_cases(incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def find_one(predicate: Any) -> dict[str, Any] | None:
        return next((record for record in incidents if predicate(record)), None)

    western = find_one(lambda record: "DOW-UAP-D077" in record["id"])
    fosterbrook = find_one(lambda record: "fosterbrook" in record["title"].lower())
    high_sensor = max(
        (
            record
            for record in incidents
            if record["assessment"].startswith("Unresolved")
            and set(record["corroboration"]) & {"multiple_sensors", "radar_or_sensor"}
        ),
        key=lambda record: record["priority_score"],
        default=None,
    )
    derived = find_one(lambda record: record["is_derivative"] and "western united states" in normalized_text(record))

    result: list[dict[str, Any]] = []
    if western:
        result.append(
            {
                "slug": "western-us",
                "kicker": "Best current follow-up target",
                "title": "Western U.S. 2023: a real observation problem, not proof of exotic craft",
                "verdict": "Mixed / unresolved",
                "body": (
                    "Six agents gave broadly consistent dusk observations from multiple positions. AARO correlated roughly 60% of the reported activity "
                    "with aircraft dispensing infrared countermeasure flares. The residual observations—especially a light reported as stationary for hours—remain "
                    "unresolved, but no witness captured imagery or technical data. The rational conclusion is continued investigation, not a technological attribution."
                ),
                "record": western,
                "refs": ["pursue_western_update", "faa_vision"],
            }
        )
    if fosterbrook:
        result.append(
            {
                "slug": "fosterbrook",
                "kicker": "Cross-record correction",
                "title": "Fosterbrook 1947: “physical trace” collapsed under document linkage",
                "verdict": "Hoax",
                "body": (
                    "The initial memo describes a 30-inch disc with a plastic dome, tubes, wiring, and official recovery—features that made the generated record look unusually strong. "
                    "A separate FBI clipping in the same release reports that four teenagers built and planted it from discarded phonograph/jukebox and radio parts. "
                    "This is why chain-of-custody plus cross-document resolution outranks dramatic morphology."
                ),
                "record": fosterbrook,
                "refs": ["fbi_ufo_files", "aaro_history_2024"],
            }
        )
    if high_sensor:
        result.append(
            {
                "slug": "sensor-case",
                "kicker": "Highest-scoring public sensor case",
                "title": f"{high_sensor['title']}: worthy of analysis, still metadata-limited",
                "verdict": high_sensor["assessment"],
                "body": (
                    "This record ranks highly because radar/sensor corroboration and quantitative fields survive into the public release. "
                    "That makes it a priority for reconstruction, not a shortcut to an exotic conclusion. AARO’s resolved GoFast, Puerto Rico, Mt. Etna, "
                    "and Al Taqaddum cases show that range, wind, platform motion, look angle, and sensor processing can eliminate apparent anomalies."
                ),
                "record": high_sensor,
                "refs": ["aaro_gofast", "aaro_puerto_rico", "aaro_mt_etna", "aaro_al_taqaddum"],
            }
        )
    if derived:
        result.append(
            {
                "slug": "derivative-media",
                "kicker": "Independence trap",
                "title": "Renderings and recreations document memory; they do not corroborate it",
                "verdict": "Derivative evidence",
                "body": (
                    "Several released images and videos are explicitly artistic renderings or recreations based on witness statements. "
                    "They are valuable for understanding what a witness meant, but counting them as photo/video corroboration would duplicate the same testimony."
                ),
                "record": derived,
                "refs": ["nasa_uap_study", "pursue_western_update"],
            }
        )
    return result


def build_analysis(
    analysis: dict[str, Any],
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    raw_incidents = list(analysis.get("incidents") or [])
    raw_documents = list(analysis.get("documents") or [])
    assessed_incidents = [assess_record(record) for record in raw_incidents]
    assessed_reports = aggregate_source_reports(raw_documents, assessed_incidents)

    total = len(raw_incidents)
    instrument_grade = sum(
        str(record.get("measurement_quality") or "unknown") in {"instrument_derived_values", "calibrated_telemetry"}
        for record in raw_incidents
    )
    non_quantitative = sum(
        str(record.get("measurement_quality") or "unknown") in {"qualitative_only", "no_measurements", "unknown"}
        for record in raw_incidents
    )
    unresolved_or_unknown = sum(
        str(record.get("resolution_status") or "unknown") in {"unresolved", "unknown"}
        for record in raw_incidents
    )
    no_corroboration = sum(set(record.get("corroboration_types") or []) in (set(), {"none_stated"}) for record in raw_incidents)
    high_information = sum(record["evidence_band"] == "High-information" for record in assessed_incidents)
    follow_up = sum(
        record["assessment"] in {"Unresolved — stronger public evidence", "Unresolved — follow-up warranted", "Mixed — partially explained, remainder unresolved"}
        for record in assessed_incidents
    )

    family_counts = Counter(record["event_family"] for record in assessed_incidents)
    largest_families = [
        {"label": label, "count": count}
        for label, count in family_counts.most_common(12)
        if count > 1
    ]
    score_bins = [
        {"label": "0–24", "min": 0, "max": 24},
        {"label": "25–49", "min": 25, "max": 49},
        {"label": "50–69", "min": 50, "max": 69},
        {"label": "70–100", "min": 70, "max": 100},
    ]
    for bin_record in score_bins:
        bin_record["count"] = sum(
            bin_record["min"] <= record["evidence_score"] <= bin_record["max"]
            for record in assessed_incidents
        )

    source_release_counts: list[dict[str, Any]] = []
    if manifest:
        manifest_documents = list(manifest.get("documents") or [])
        source_release_counts = count_values(manifest_documents, "release_date")

    return {
        "meta": {
            "analysis_version": ANALYSIS_VERSION,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "source_analysis_generated_at": analysis.get("generated_at"),
            "corpus_scope": "Public U.S. government UAP/UFO files in the local PURSUE-derived corpus through the August 7, 2026 release",
            "method": "Deterministic evidence-sufficiency scoring plus source-specific overrides grounded in primary records; no identity is inferred from absence of data.",
        },
        "headline": {
            "physical_sources": len(raw_documents),
            "incident_records": total,
            "pages": int(analysis.get("total_pages") or 0),
            "instrument_grade": instrument_grade,
            "instrument_grade_rate": ratio(instrument_grade, total),
            "non_quantitative": non_quantitative,
            "non_quantitative_rate": ratio(non_quantitative, total),
            "unresolved_or_unknown": unresolved_or_unknown,
            "unresolved_or_unknown_rate": ratio(unresolved_or_unknown, total),
            "no_corroboration": no_corroboration,
            "no_corroboration_rate": ratio(no_corroboration, total),
            "high_information": high_information,
            "high_information_rate": ratio(high_information, total),
            "follow_up_candidates": follow_up,
            "verified_exotic_technology": 0,
        },
        "verdict": {
            "headline": "The corpus documents a real identification problem—not verified exotic technology.",
            "summary": (
                "Most public records remain unresolved because decisive geometry, calibration, metadata, or independent corroboration is missing. "
                "Where comparable cases have sufficient data, ordinary objects and sensor/viewing effects repeatedly explain the apparent anomaly. "
                "A small subset merits disciplined follow-up, but no public record in this corpus crosses the evidentiary threshold for a novel-technology claim."
            ),
            "confidence": "Moderate-to-high for the corpus-level conclusion; low-to-moderate for many individual identities.",
        },
        "definitions": [
            {
                "term": "Evidence score",
                "definition": "0–100 sufficiency score for provenance, measurements, corroboration, time/place precision, quantitative fields, and documentation. It is not a probability that an event is extraordinary.",
            },
            {
                "term": "Unresolved",
                "definition": "The released information does not support a definitive identity. It does not mean conventional explanations were excluded, and it is not evidence of extraterrestrial origin.",
            },
            {
                "term": "Incident",
                "definition": "An analytically flattened event/account row. Multiple incident rows or media assets may come from one physical source or one underlying event.",
            },
            {
                "term": "Follow-up candidate",
                "definition": "An unresolved record with comparatively better provenance/corroboration. Priority reflects information value, not exoticity.",
            },
        ],
        "distributions": {
            "assessment": count_values(assessed_incidents, "assessment"),
            "best_fit_explanation": count_values(assessed_incidents, "best_fit_explanation"),
            "evidence_band": count_values(assessed_incidents, "evidence_band"),
            "measurement_quality": count_values(raw_incidents, "measurement_quality"),
            "resolution_status": count_values(raw_incidents, "resolution_status"),
            "observation_mode": count_values(raw_incidents, "primary_observation_mode"),
            "corroboration": count_list_values(raw_incidents, "corroboration_types"),
            "media_type": count_values(raw_incidents, "media_type"),
            "score_bins": score_bins,
            "source_release": source_release_counts,
            "event_families": largest_families,
        },
        "timeline": build_timeline(assessed_incidents),
        "timeline_milestones": [
            {"year": 1947, "title": "Postwar sighting wave", "detail": "Flying-disc reports surge; the Fosterbrook device is quickly exposed as a juvenile hoax."},
            {"year": 1952, "title": "Project BLUE BOOK", "detail": "The Air Force formalizes investigation as Cold War reporting peaks."},
            {"year": 1969, "title": "BLUE BOOK closes", "detail": "12,618 reports were cataloged; 701 remained unidentified, not verified exotic."},
            {"year": 2013, "title": "Puerto Rico IR case", "detail": "Later reconstruction shows wind-speed objects, parallax, and thermal crossover—not transmedium flight."},
            {"year": 2015, "title": "GoFast", "detail": "Later geometry bounds the object to non-anomalous speed; identity remains uncertain."},
            {"year": 2023, "title": "Western U.S. event", "detail": "Six agents report repeating lights; AARO later finds ~60% plausibly attributable to countermeasure flares."},
            {"year": 2026, "title": "PURSUE releases", "detail": "Four-plus tranches publish unresolved and historical files; release selection strongly shapes this corpus."},
        ],
        "quality_findings": build_quality_findings(raw_incidents, assessed_incidents),
        "notable_cases": notable_cases(assessed_incidents),
        "reports": assessed_reports,
        "incidents": assessed_incidents,
        "sources": REFERENCE_SOURCES,
        "chart_map": [
            {
                "section": "Evidence sufficiency",
                "question": "How much of the corpus can support quantitative reconstruction?",
                "family": "Composition",
                "type": "stacked bar",
                "fields": ["measurement_quality", "count"],
                "takeaway": "Non-quantitative records dominate.",
                "palette": "gold / teal / neutral",
            },
            {
                "section": "Timeline",
                "question": "How do released incident records and information quality vary by decade?",
                "family": "Trend",
                "type": "stacked columns",
                "fields": ["decade", "limited", "moderate", "high"],
                "takeaway": "Volume tracks historical/reporting programs and modern releases, not an unbiased event rate.",
                "palette": "teal / gold / neutral",
            },
            {
                "section": "Assessment outcomes",
                "question": "What conclusions are supportable after applying the evidence gate?",
                "family": "Comparison & ranking",
                "type": "horizontal bar",
                "fields": ["assessment", "count"],
                "takeaway": "Insufficient-data unresolved cases are more common than stronger-evidence unresolved cases.",
                "palette": "single-root teal with gold focal",
            },
        ],
    }


def render_html(template: str, payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    generated_at = str(payload["meta"]["generated_at"])
    description = (
        "A source-backed scientific analysis of public UAP/UFO reports, with evidence scoring, competing hypotheses, "
        "case studies, timelines, uncertainty, and report-by-report assessments."
    )
    replacements = {
        "__SCIENTIFIC_DATA__": data_json,
        "__GENERATED_AT__": html_escape(generated_at),
        "__PAGE_DESCRIPTION__": html_escape(description, quote=True),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def main() -> int:
    args = parse_args()
    analysis = json.loads(args.input.read_text(encoding="utf-8"))
    manifest: dict[str, Any] | None = None
    if args.manifest.is_file():
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    payload = build_analysis(analysis, manifest)

    args.output_data.parent.mkdir(parents=True, exist_ok=True)
    args.output_data.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    template = args.template.read_text(encoding="utf-8")
    html = render_html(template, payload)
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.write_text(html, encoding="utf-8")

    print(
        json.dumps(
            {
                "output_html": str(args.output_html),
                "output_data": str(args.output_data),
                "reports": len(payload["reports"]),
                "incidents": len(payload["incidents"]),
                "sources": len(payload["sources"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
