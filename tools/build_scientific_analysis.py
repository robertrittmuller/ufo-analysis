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
from statistics import median
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
    {
        "id": "aaro_imagery_catalog",
        "title": "AARO UAP imagery and analytical descriptions",
        "publisher": "AARO",
        "year": 2026,
        "url": "https://www.aaro.mil/Next-AARO-Home-redesign/Next-Parent/Next-AARO-UAP-Imagery-Acc-Table/",
        "role": "Primary descriptions of released videos, including sensor motion, loss of contrast, and resolved comparison cases.",
    },
    {
        "id": "aaro_western_aircraft",
        "title": "Western United States UAP case resolution",
        "publisher": "AARO",
        "year": 2026,
        "url": "https://www.aaro.mil/Portals/136/PDFs/case_resolution_reports/Case_Resolution_of%20_Western_United_States_Uap_508-02262024.pdf",
        "role": "Boresight and air-traffic analysis identifying distant commercial aircraft and sensor-induced shape changes.",
    },
    {
        "id": "aaro_eglin",
        "title": "Eglin UAP case resolution",
        "publisher": "AARO",
        "year": 2026,
        "url": "https://www.aaro.mil/Portals/136/PDFs/case_resolution_reports/Case_Resolution_of_Eglin_UAP_2_508_.pdf",
        "role": "Geometry, wind, lighting, and object testing supporting a lighter-than-air identification.",
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


def phenomenon_profile(record: dict[str, Any]) -> dict[str, str]:
    """Describe what was reported before attempting to identify it."""
    text = normalized_text(record)
    morph = set(record.get("morphology_normalized") or [])
    corroboration = set(record.get("corroboration_types") or [])
    mode = str(record.get("primary_observation_mode") or "unknown")
    event_type = str(record.get("event_record_type") or "unknown")
    day = str(record.get("day_night_context") or "unknown")

    if event_type in CONTEXT_RECORD_TYPES or str(record.get("resolution_status")) in CONTEXT_RESOLUTIONS:
        group = "Context, testimony, or non-event"
        observed = "No discrete airborne phenomenon is recoverable from this row."
    elif "physical_trace" in corroboration or any(token in text for token in ("debris", "fragment", "alloy", "material specimen", "ground trace")):
        group = "Material or physical-trace claim"
        observed = "A recovered substance, surface mark, or alleged interaction is part of the claim."
    elif day == "space_or_orbit" or any(token in text for token in ("apollo ", "gemini ", "mercury-atlas", "space shuttle", "sts-80", "skylab")):
        group = "Spaceborne light or object"
        observed = "A light, speck, object, or verbal observation was recorded during a crewed space mission."
    elif mode in {"radar", "rf_sigint"} and not ({"photo_or_video", "multiple_witnesses"} & corroboration):
        group = "Radar or electronic target"
        observed = "The phenomenon exists primarily as an electronic return rather than a resolved visible object."
    elif "triangle" in morph:
        group = "Triangle or three-light form"
        observed = "Witnesses or imagery describe a triangular outline or three-light arrangement."
    elif "cylinder_cigar_tictac" in morph:
        group = "Cylinder, cigar, or tic-tac form"
        observed = "The object is described as elongated, oblong, cylindrical, or tic-tac-like."
    elif "fireball" in morph:
        group = "Fireball or meteor-like event"
        observed = "A bright, short-lived luminous body or trail dominates the observation."
    elif "disc_or_saucer" in morph:
        group = "Disc or oval form"
        observed = "The witness described a disc, saucer, oval, or flattened circular body."
    elif "sphere_or_orb" in morph or "light_point" in morph:
        group = "Light, orb, or sphere"
        observed = "The observation is principally a point-like light or round object with little recoverable surface detail."
    elif "irregular_blob_or_area_of_contrast" in morph or mode in {"ir_thermal", "eo_video", "photo_image"}:
        group = "Indistinct sensor target"
        observed = "The released image resolves an area of contrast, but not enough structure to identify an object."
    elif "formation_only" in morph or str(record.get("object_count_bucket")) in {"three_to_five", "many_or_swarm"}:
        group = "Formation or multiple objects"
        observed = "Multiple lights or targets are reported in a pattern or coordinated-looking group."
    else:
        group = "Unspecified aerial observation"
        observed = "The public record does not preserve a stable morphology."

    condition = "daylight" if day == "daylight" else "low-light" if day in {"night", "dawn_or_dusk"} else day.replace("_", " ")
    return {"group": group, "observed": observed, "condition": condition}


def working_hypothesis(record: dict[str, Any], phenomenon: dict[str, str]) -> dict[str, str]:
    """Assign the narrowest defensible model without treating missing data as proof."""
    text = normalized_text(record)
    title = str(record.get("title") or "")
    title_lower = title.lower()
    resolution = str(record.get("resolution_status") or "unknown")
    mundane = str(record.get("mundane_explanation_present") or "unclear")
    group = phenomenon["group"]
    mode = str(record.get("primary_observation_mode") or "unknown")
    year = safe_year(record)

    def result(bucket: str, model: str, confidence: str, status: str, ref: str) -> dict[str, str]:
        return {
            "mechanism_group": bucket,
            "working_model": model,
            "model_confidence": confidence,
            "model_status": status,
            "model_ref": ref,
        }

    if is_derivative(record):
        return result(
            "Derivative visualization",
            "A rendering or recreation of testimony, useful for morphology but not an independent observation.",
            "High",
            "Context",
            "nasa_uap_study",
        )
    if "fosterbrook" in title_lower or "mrs. fosterbrook" in text:
        return result(
            "Fabrication or cultural contamination",
            "Constructed juvenile hoax made from discarded phonograph/jukebox and radio components.",
            "High",
            "Resolved across related records",
            "fbi_ufo_files",
        )
    if "maury island" in text:
        return result(
            "Fabrication or ordinary material",
            "A likely fabricated or exaggerated disc story paired with ordinary smelter slag.",
            "Moderate-to-high",
            "Resolved in related investigation records",
            "fbi_ufo_files",
        )
    if "ica uap d001" in title_lower or ("colorado springs" in text and "backscatter" in text):
        return result(
            "Atmospheric or optical effect",
            "Sunlight backscattered from snow-covered terrain into low cloud, producing a stationary luminous form.",
            "High",
            "Source-resolved",
            "nasa_uap_study",
        )
    if "egl" in title_lower and "uap" in title_lower:
        return result(
            "Balloon or lighter-than-air object",
            "A windborne lighter-than-air object; geometry, wind, lighting, and balloon tests match the report.",
            "Moderate",
            "Resolved by AARO",
            "aaro_eglin",
        )
    if "western united states event" in text or "orbs launching" in text:
        return result(
            "Military activity plus unresolved visual residual",
            "Countermeasure flares plausibly explain roughly 60% of the activity; the remainder lacks technical capture.",
            "Moderate",
            "Partially resolved",
            "pursue_western_update",
        )
    if "pr027" in title_lower or "pr034" in title_lower:
        return result(
            "Sensor motion and unresolved target",
            "A real area of contrast was tracked, but platform/sensor motion accounts for much of the apparent erratic path; object identity remains open.",
            "Moderate",
            "Behavior substantially constrained; identity unresolved",
            "aaro_imagery_catalog",
        )
    if "pr035" in title_lower:
        return result(
            "Contrast loss near a changing background",
            "A small target became indistinguishable as the background changed from water to land; the video does not demonstrate water entry.",
            "Moderate-to-high",
            "Apparent transmedium behavior not supported",
            "aaro_imagery_catalog",
        )
    if "pr028" in title_lower:
        return result(
            "Sensor-band-specific target",
            "A SWIR-only target with uncertain independent range; balloon, atmospheric target, or sensor-specific contrast remain viable.",
            "Low-to-moderate",
            "Unresolved",
            "aaro_imagery_catalog",
        )
    if "gulf of oman" in text and year == 2021:
        return result(
            "Unresolved small airborne objects",
            "Small balloons, drones, aircraft-related objects, and range/sensor effects remain viable; reported responsiveness is not demonstrated by released kinematics.",
            "Low-to-moderate",
            "Unresolved residual",
            "nasa_uap_study",
        )
    if "socorro" in text and "zamora" in text:
        return result(
            "Unresolved close-range event",
            "A landed or ascending object was reported with site marks; prototype activity, misinterpretation, or fabrication remain unexcluded.",
            "Low",
            "Unresolved residual",
            "blue_book_archive",
        )

    source_resolved = resolution == "explained" or mundane == "yes_source_resolved"
    if source_resolved:
        if any(token in text for token in ("hoax", "fabricat", "prank")):
            return result("Fabrication or cultural contamination", "A fabrication or hoax documented by the source investigation.", "High", "Source-resolved", "fbi_ufo_files")
        if any(token in text for token in ("balloon", "radiosonde", "lighter-than-air")):
            return result("Balloon or lighter-than-air object", "A balloon or other lighter-than-air object recorded by the source investigation.", "Moderate-to-high", "Source-resolved", "aaro_al_taqaddum")
        if any(token in text for token in ("meteor", "bolide", "fireball", "re-entry", "reentry")):
            return result("Astronomical or re-entry event", "A meteor, fireball, or re-entering object recorded by the source investigation.", "Moderate-to-high", "Source-resolved", "nasa_uap_study")
        if any(token in text for token in ("aircraft", "airplane", "helicopter", "yf-12")):
            return result("Conventional aircraft or military activity", "A conventional aircraft or known military activity recorded by the source investigation.", "Moderate-to-high", "Source-resolved", "aaro_annual_2024")
        if any(token in text for token in ("reflection", "refraction", "cloud", "backscatter", "atmospheric", "temperature inversion")):
            return result("Atmospheric or optical effect", "An atmospheric, lighting, or optical effect recorded by the source investigation.", "Moderate-to-high", "Source-resolved", "nasa_uap_study")
        if any(token in text for token in ("debris", "ice", "equipment", "instrument glitch")):
            return result("Mission debris or equipment effect", "Mission-related debris, ice, reflection, or equipment behavior.", "Moderate-to-high", "Source-resolved", "nasa_uap_study")
        return result("Ordinary object or process", "The source investigation records a conventional identification.", "Moderate", "Source-resolved", "aaro_history_2024")

    if group == "Context, testimony, or non-event":
        return result("Context rather than a discrete event", "Administrative, historical, or testimonial material without a recoverable airborne event.", "High", "Context", "aaro_history_2024")
    if group == "Radar or electronic target":
        return result("Radar target or propagation ambiguity", "Aircraft, balloon, anomalous propagation, interference, or clutter remain viable without raw plots and calibration.", "Low-to-moderate", "Identity unresolved", "aaro_annual_2024")
    if group == "Spaceborne light or object":
        return result("Mission debris, ice, reflection, or imaging effect", "Nearby debris or ice, window reflections, illumination geometry, and imaging artifacts are the leading classes.", "Low-to-moderate", "Analyst best fit", "nasa_uap_study")
    if group == "Triangle or three-light form":
        return result("Aircraft lighting, formation, or perceptual closure", "Conventional aircraft/formation lighting and the visual completion of an outline remain more economical than a solid triangular craft.", "Low", "Analyst best fit", "faa_vision")
    if group == "Fireball or meteor-like event":
        return result("Meteor, fireball, rocket, or re-entry", "A fast luminous atmospheric or re-entering body is the leading class.", "Moderate", "Analyst best fit", "nasa_uap_study")
    if group == "Indistinct sensor target":
        return result("Distant object plus sensor/viewing geometry", "A distant aircraft, balloon, drone, bird, or atmospheric target rendered ambiguous by range and sensor processing.", "Low-to-moderate", "Analyst best fit", "aaro_gofast")
    if group == "Light, orb, or sphere":
        if str(record.get("day_night_context")) in {"night", "dawn_or_dusk"}:
            return result("Distant light source", "Aircraft lights, flares, satellites, or celestial objects remain the leading classes when range is unknown.", "Low", "Analyst best fit", "faa_vision")
        return result("Balloon, small airborne object, or distant aircraft", "A balloon, drone, bird, or distant aircraft is more likely than a structured sphere when surface detail and range are absent.", "Low", "Analyst best fit", "aaro_al_taqaddum")
    if group == "Cylinder, cigar, or tic-tac form":
        return result("Elongated balloon, aircraft, or unresolved range", "An elongated lighter-than-air object, aircraft aspect, or unresolved point-spread shape remains plausible.", "Low", "Analyst best fit", "aaro_eglin")
    if group == "Disc or oval form":
        return result(
            "Aircraft, balloon, astronomy, or perceptual ambiguity",
            "Conventional aircraft, balloons, astronomical sources, experimental systems, and viewing ambiguity remain the leading historical classes.",
            "Low",
            "Analyst best fit",
            "aaro_history_2024",
        )
    if group == "Material or physical-trace claim":
        return result("Ordinary material, fabrication, or unresolved site cause", "Terrestrial material and fabrication must be excluded by laboratory chain-of-custody before a novel-material claim is supportable.", "Low", "Analyst best fit", "ornl_material")
    if mode in {"naked_eye_visual", "ground_visual", "cockpit_visual"}:
        return result("Perceptual and range ambiguity", "A conventional distant object or light remains indeterminate because angular appearance does not recover physical size or speed.", "Low", "Analyst best fit", "faa_vision")
    return result("Indeterminate conventional possibilities", "The public details do not discriminate among common object, environmental, perceptual, and sensor explanations.", "Low", "Indeterminate", "nasa_uap_study")


def behavior_interpretation(record: dict[str, Any]) -> str:
    motions = set(record.get("apparent_motion_class") or [])
    mode = str(record.get("primary_observation_mode") or "unknown")
    measured = str(record.get("measurement_quality") or "unknown") in {"instrument_derived_values", "calibrated_telemetry"}
    if "water_interaction" in motions:
        return "A water crossing is directly alleged, but independent range and continuous tracking are required to establish a transmedium path."
    if "erratic_or_abrupt" in motions and mode in {"eo_video", "ir_thermal", "photo_image"} and not measured:
        return "Sensor slew, stabilization, changing field of view, and unknown range can create large apparent course changes."
    if "accelerating_or_departing" in motions and not measured:
        return "The record preserves apparent acceleration, not a calibrated acceleration measurement."
    if "stationary_hover" in motions:
        return "A distant light or windborne object can remain nearly stationary in angle without physically hovering."
    if "disappearing_or_dissipating" in motions:
        return "Occlusion, contrast loss, glare, cloud, or the sensor threshold can mimic disappearance."
    if "straight_line_transit" in motions:
        return "Straight transit is compatible with aircraft, balloons, satellites, birds, drones, and other ordinary motion."
    return "The released record does not preserve enough geometry to turn apparent motion into a physical trajectory."


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

    phenomenon = phenomenon_profile(record)
    hypothesis = working_hypothesis(record, phenomenon)
    explanation = hypothesis["working_model"]
    refs.append(hypothesis["model_ref"])

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
        "phenomenon_group": phenomenon["group"],
        "observed_phenomenon": phenomenon["observed"],
        "observation_condition": phenomenon["condition"],
        "mechanism_group": hypothesis["mechanism_group"],
        "working_model": hypothesis["working_model"],
        "model_confidence": hypothesis["model_confidence"],
        "model_status": hypothesis["model_status"],
        "motion_interpretation": behavior_interpretation(record),
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
        phenomenon_counts = Counter(child["phenomenon_group"] for child in children)
        mechanism_counts = Counter(child["mechanism_group"] for child in children)
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
                "phenomenon_group": phenomenon_counts.most_common(1)[0][0],
                "phenomenon_counts": dict(phenomenon_counts),
                "mechanism_group": mechanism_counts.most_common(1)[0][0],
                "mechanism_counts": dict(mechanism_counts),
                "working_model": strongest["working_model"],
                "model_confidence": strongest["model_confidence"],
                "model_status": strongest["model_status"],
                "observed_phenomenon": strongest["observed_phenomenon"],
                "motion_interpretation": strongest["motion_interpretation"],
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


PHENOMENON_INTERPRETATIONS = {
    "Disc or oval form": {
        "plain": "The classic postwar “flying disc” is principally a visual-era report form.",
        "likely": "Aircraft aspect, balloons, astronomical sources, experimental systems, reflections, and period-shaped interpretation.",
        "caveat": "A shape word without range or resolved surface detail does not establish a common vehicle class.",
    },
    "Light, orb, or sphere": {
        "plain": "Most orb reports are unresolved lights or small round targets rather than resolved spherical craft.",
        "likely": "Aircraft lights, flares, satellites, planets/stars, balloons, drones, or point-spread imagery.",
        "caveat": "Unknown distance makes apparent size, hovering, and acceleration unreliable.",
    },
    "Indistinct sensor target": {
        "plain": "Modern military videos most often preserve a contrast target, not a visually resolved object.",
        "likely": "Distant aircraft, balloons, drones, birds, or atmospheric targets combined with sensor geometry and processing.",
        "caveat": "Reticles, zoom, stabilization, sensor slews, and contrast modes can dominate apparent motion and shape.",
    },
    "Cylinder, cigar, or tic-tac form": {
        "plain": "Elongated silhouettes recur, but the public imagery rarely resolves surfaces or true proportions.",
        "likely": "Elongated balloons, aircraft aspect, blur/point-spread shape, or an ordinary object at uncertain range.",
        "caveat": "A two-dimensional image cannot independently determine length, distance, or speed.",
    },
    "Triangle or three-light form": {
        "plain": "Triangle reports are largely low-light eyewitness accounts or later renderings.",
        "likely": "Aircraft/formation lighting, a partially visible platform, or perceptual closure between separate lights.",
        "caveat": "No released triangle case combines resolved structure with calibrated multisensor kinematics.",
    },
    "Radar or electronic target": {
        "plain": "Radar cases are more testable than testimony but remain identification problems without raw plots and system state.",
        "likely": "Aircraft, balloons, anomalous propagation, interference, side lobes, clutter, or a genuine unidentified target.",
        "caveat": "A radar return establishes a detection; it does not by itself establish a solid craft or exotic motion.",
    },
    "Material or physical-trace claim": {
        "plain": "Trace and debris claims are the most direct-sounding evidence—and the most dependent on chain of custody.",
        "likely": "Ordinary industrial material, environmental damage, fabrication, or an unresolved terrestrial cause.",
        "caveat": "Novel-material claims require controlled recovery, comparison samples, and independent laboratory replication.",
    },
    "Spaceborne light or object": {
        "plain": "Space-mission records mostly contain lights, specks, debris references, and ambiguous photographs.",
        "likely": "Ice, mission debris, window reflections, illumination geometry, satellites, or camera artifacts.",
        "caveat": "Relative motion near a spacecraft is counterintuitive and range is usually unconstrained from a single view.",
    },
    "Fireball or meteor-like event": {
        "plain": "Brief luminous events form a recognizable atmospheric class.",
        "likely": "Meteors, bolides, rockets, re-entry debris, lightning, or aircraft illumination.",
        "caveat": "Exact time, direction, and duration are needed for astronomical or launch correlation.",
    },
    "Formation or multiple objects": {
        "plain": "Multiple targets can look coordinated even when perspective compresses independent motion.",
        "likely": "Aircraft formations, satellites, balloon groups, birds, or multiple unrelated lights.",
        "caveat": "Angular spacing alone does not demonstrate communication or coordinated control.",
    },
    "Unspecified aerial observation": {
        "plain": "The report records an unexplained observation but preserves too little morphology to compare identities.",
        "likely": "Several ordinary object, environmental, perceptual, and sensor classes remain viable.",
        "caveat": "These records should not be pooled as though “unspecified” were a physical category.",
    },
    "Context, testimony, or non-event": {
        "plain": "Some records preserve policy, correspondence, contact claims, or discussion rather than an observable event.",
        "likely": "Historical and cultural context, not a class of aerial object.",
        "caveat": "They are excluded from object-level inference but retained in the explorer.",
    },
}


def build_phenomenon_profiles(incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = len(incidents)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for incident in incidents:
        grouped[incident["phenomenon_group"]].append(incident)
    profiles: list[dict[str, Any]] = []
    for label, rows in sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True):
        info = PHENOMENON_INTERPRETATIONS[label]
        mechanisms = Counter(row["mechanism_group"] for row in rows)
        modes = Counter(row["observation_mode"] for row in rows)
        years = [row["year"] for row in rows if row["year"]]
        resolved = sum(
            row["model_status"] in {"Source-resolved", "Resolved across related records", "Resolved in related investigation records", "Resolved by AARO"}
            for row in rows
        )
        profiles.append(
            {
                "label": label,
                "count": len(rows),
                "share": ratio(len(rows), total),
                "median_evidence_score": round(median(row["evidence_score"] for row in rows)),
                "source_resolved": resolved,
                "top_mechanisms": [{"label": key, "count": value} for key, value in mechanisms.most_common(3)],
                "top_modes": [{"label": key, "count": value} for key, value in modes.most_common(3)],
                "year_start": min(years) if years else None,
                "year_end": max(years) if years else None,
                **info,
            }
        )
    return profiles


def build_behavior_claims(raw_incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    definitions = [
        (
            "instant_acceleration",
            "Instant acceleration",
            "Most entries preserve a witness or analyst label. A calibrated acceleration needs range, time, platform motion, and continuous tracking.",
        ),
        (
            "abrupt_vector_change",
            "Abrupt turns",
            "Sensor pans, reticle behavior, stabilization, and unknown range can convert image-plane movement into an apparent turn.",
        ),
        (
            "transmedium",
            "Transmedium behavior",
            "Loss against water, cloud, horizon, or land is not a continuous trajectory through a medium boundary.",
        ),
        (
            "stationary_hover",
            "Stationary hover",
            "Near-zero angular motion can describe a distant light, a windborne object, or matched relative motion.",
        ),
        (
            "physical_trace",
            "Physical trace",
            "A trace becomes diagnostic only with controlled collection, comparison samples, and an intact chain of custody.",
        ),
    ]
    rows: list[dict[str, Any]] = []
    for key, label, interpretation in definitions:
        records = [row for row in raw_incidents if key in (row.get("capability_keys") or [])]
        rows.append(
            {
                "key": key,
                "label": label,
                "reported": len(records),
                "instrument_grade": sum(
                    str(row.get("measurement_quality")) in {"instrument_derived_values", "calibrated_telemetry"}
                    for row in records
                ),
                "multiple_sensors": sum("multiple_sensors" in (row.get("corroboration_types") or []) for row in records),
                "direct_water_motion": sum("water_interaction" in (row.get("apparent_motion_class") or []) for row in records),
                "interpretation": interpretation,
            }
        )
    return rows


def build_era_comparison(incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eras = [
        ("1940–1969", lambda year: year is not None and 1940 <= year < 1970),
        ("1970–2009", lambda year: year is not None and 1970 <= year < 2010),
        ("2010–2026", lambda year: year is not None and 2010 <= year <= 2026),
    ]
    output: list[dict[str, Any]] = []
    for label, predicate in eras:
        rows = [row for row in incidents if predicate(row["year"])]
        counts = Counter(row["phenomenon_group"] for row in rows)
        output.append(
            {
                "era": label,
                "total": len(rows),
                "groups": [{"label": group, "count": count, "share": ratio(count, len(rows))} for group, count in counts.most_common()],
            }
        )
    return output


def find_incident(incidents: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
    needle = needle.lower()
    return next((row for row in incidents if needle in row["title"].lower()), None)


def build_case_dossiers(incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs = [
        {
            "needle": "DOW UAP D077",
            "title": "Western U.S. orbs: one event, two conclusions",
            "tag": "Visual / multi-witness",
            "claim": "Six federal agents described recurring orange and red orbs near a sensitive site over two dusk periods.",
            "reconstruction": "Flight, radar, and ADS-B correlation plausibly matches about 60% of the activity to military aircraft dispensing infrared countermeasure flares. The reported long-duration red light and remaining sightings lack imagery or technical measurements.",
            "conclusion": "Partially explained. The residual is a collection target, not evidence that unrecognized technology was measured.",
            "refs": ["pursue_western_update", "faa_vision"],
        },
        {
            "needle": "DOW-UAP-PR034",
            "title": "The “90-degree turns” are not established kinematics",
            "tag": "Infrared video / Greece",
            "claim": "A mission report described a target near the ocean making multiple right-angle turns at roughly 80 mph.",
            "reconstruction": "The released description says the sensor pans, centers, designates, and synchronizes with an area of contrast. Without independent range and platform geometry, motion across the display cannot be converted into the target’s physical vector.",
            "conclusion": "A target remains unidentified, but the extraordinary turn claim is not demonstrated by the public video.",
            "refs": ["aaro_imagery_catalog", "aaro_gofast"],
        },
        {
            "needle": "DOW-UAP-PR035",
            "title": "Disappearing at the coast is not entering the water",
            "tag": "Infrared video / Greece",
            "claim": "A small circular target moved over an ocean background and vanished as the view reached land.",
            "reconstruction": "The object remained an area of contrast until the background changed. It then became visually indistinguishable; the released description does not show a continuous trajectory through the water surface.",
            "conclusion": "Unresolved small target. The available frames do not support a transmedium interpretation.",
            "refs": ["aaro_imagery_catalog", "aaro_puerto_rico"],
        },
        {
            "needle": "ICA UAP D001",
            "title": "Colorado Springs: a large “object” made by light and cloud",
            "tag": "Five witnesses / resolved",
            "claim": "Soldiers reported a large, stationary, angular object above Cheyenne Mountain.",
            "reconstruction": "The source analysis reproduced the geometry as sunlight reflected from snow-covered terrain and backscattered through low cloud, producing an apparently solid luminous area.",
            "conclusion": "Resolved atmospheric-optical event; no anomalous motion or adversarial capability was recorded.",
            "refs": ["nasa_uap_study", "faa_vision"],
        },
        {
            "needle": "DOW-UAP-PR070",
            "title": "Eglin: an anomalous-looking object consistent with a balloon",
            "tag": "Aircrew / resolved",
            "claim": "Aircrew reported a rounded object and associated a radar circuit-breaker trip with the encounter.",
            "reconstruction": "AARO compared geometry, wind, sun angle, pilot accounts, and commercial lighting-balloon tests. The object’s direction and slow speed matched a lighter-than-air target; the electrical fault had a prior history.",
            "conclusion": "Very likely a balloon-like object; the radar malfunction was probably coincidental.",
            "refs": ["aaro_eglin"],
        },
        {
            "needle": "Fosterbrook Disc Recovery",
            "title": "Twin Falls: the recovered “disc” was a planted device",
            "tag": "Physical recovery / hoax",
            "claim": "An initial FBI memo described a constructed disc with domes, wiring, coils, and radio tubes.",
            "reconstruction": "A separate file in the same release records that four boys assembled and planted it from discarded phonograph/jukebox and radio parts; Army inspection found ordinary components.",
            "conclusion": "Resolved fabrication. The case shows why later records can reverse an impressive initial report.",
            "refs": ["fbi_ufo_files", "aaro_history_2024"],
        },
        {
            "needle": "Radar Tracking of Unconventional Target near Fort Monmouth",
            "title": "Fort Monmouth: a genuine radar anomaly without an identity",
            "tag": "Two radar systems / 1951",
            "claim": "Operators reported strong returns, rapid azimuth changes, vertical ascent, and speeds above 700 mph over two days.",
            "reconstruction": "Multiple radar sets make the detection more interesting than a lone visual report, but the public record lacks raw plots, calibration state, propagation analysis, and an independently resolved object.",
            "conclusion": "A high-value unresolved radar case. Aircraft, propagation, interference, or an unknown target cannot be discriminated publicly.",
            "refs": ["blue_book_archive", "aaro_annual_2024"],
        },
        {
            "needle": "DOW UAP D101",
            "title": "Gulf of Oman: the strongest modern residual still lacks released kinematics",
            "tag": "AC-130 EO/IR / 2021",
            "claim": "An AC-130 crew reported roughly 25 cold orbs moving in formation and apparently reacting to cannon fire.",
            "reconstruction": "The official report preserves multiple witnesses, EO/IR observation, and estimated quantities. Yet the public material does not provide the original telemetry needed to test range, four-foot size, acceleration, or causal response to firing.",
            "conclusion": "Priority unresolved case. Balloons, drones, small objects, and sensor/range effects remain viable alongside a genuinely unidentified target.",
            "refs": ["nasa_uap_study", "aaro_annual_2024"],
        },
        {
            "needle": "Socorro Police Officer Lonnie Zamora",
            "title": "Socorro: a close-range residual with imperfect trace evidence",
            "tag": "Ground visual / 1964",
            "claim": "Officer Lonnie Zamora described an oval object, two figures, ascent with flame, and ground marks later examined by officials.",
            "reconstruction": "The event is unusually close-range and includes site effects, but the primary observation is essentially one witness and the public trace record lacks a controlled forensic chain. Prototype activity, misinterpretation, or fabrication remain unexcluded.",
            "conclusion": "One of the archive’s more interesting historical residuals, but not a verified nonhuman craft.",
            "refs": ["blue_book_archive", "aaro_history_2024"],
        },
        {
            "needle": "NASA-UAP-D030",
            "title": "STS-80: an orbital speck without recoverable range",
            "tag": "Space image / 1996",
            "claim": "A bright elongated object appears near Earth’s limb in a shuttle photograph.",
            "reconstruction": "A single image cannot determine whether the object is nearby debris, ice, a reflection, a distant satellite, or something farther away. Relative motion and illumination near a spacecraft can make small nearby material look unusual.",
            "conclusion": "Unidentified image feature; mission debris or optical effects are the leading classes.",
            "refs": ["nasa_uap_study"],
        },
    ]
    dossiers: list[dict[str, Any]] = []
    for spec in specs:
        record = find_incident(incidents, spec["needle"])
        if record:
            dossiers.append({**spec, "record": record})
    return dossiers


def build_residual_cases(incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    preferred = [
        "Radar Tracking of Unconventional Target near Fort Monmouth",
        "Radar Contact over Oak Ridge",
        "Rotating Colored Object Sighted Over Godman",
        "DOW UAP D101",
        "DOW UAP D038",
        "DOW-UAP-PR028",
        "FBI UAP D032",
        "Socorro Police Officer Lonnie Zamora",
        "DOW-UAP-PR022",
        "DOW-UAP-PR026",
    ]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for needle in preferred:
        row = find_incident(incidents, needle)
        if row and row["id"] not in seen:
            result.append(row)
            seen.add(row["id"])
    return result


def build_connection_insights(
    raw_incidents: list[dict[str, Any]],
    assessed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pre = [row for row in assessed if row["year"] and row["year"] < 1970]
    modern = [row for row in assessed if row["year"] and row["year"] >= 2010]
    naked = [row for row in assessed if row["observation_mode"] == "naked_eye_visual"]
    video = [row for row in assessed if row["observation_mode"] in {"eo_video", "ir_thermal"}]
    disc_pre = sum(row["phenomenon_group"] == "Disc or oval form" for row in pre)
    sensor_modern = sum(row["phenomenon_group"] == "Indistinct sensor target" for row in modern)
    naked_disc = sum(row["phenomenon_group"] == "Disc or oval form" for row in naked)
    video_blob = sum(row["phenomenon_group"] == "Indistinct sensor target" for row in video)
    transmedium = [row for row in raw_incidents if "transmedium" in (row.get("capability_keys") or [])]
    direct_water = sum("water_interaction" in (row.get("apparent_motion_class") or []) for row in transmedium)
    acceleration = [row for row in raw_incidents if "instant_acceleration" in (row.get("capability_keys") or [])]
    acceleration_instrument = sum(
        row.get("measurement_quality") in {"instrument_derived_values", "calibrated_telemetry"} for row in acceleration
    )
    return [
        {
            "kind": "Connect",
            "title": "The observation system shapes the reported object",
            "evidence": (
                f"{disc_pre} pre-1970 rows are disc/oval reports, while {sensor_modern} records since 2010 are indistinct sensor targets. "
                f"Naked-eye observations produce {naked_disc} disc cases; EO/IR video produces {video_blob} contrast-target cases."
            ),
            "meaning": "The changing morphology tracks human instruments and vocabulary more closely than a stable vehicle design.",
        },
        {
            "kind": "Connect",
            "title": "Apparent performance repeatedly depends on missing range",
            "evidence": (
                f"{len(acceleration)} rows carry an instant-acceleration label, but only {acceleration_instrument} has instrument-grade values. "
                "Worked resolutions such as GoFast show how motion parallax converts ordinary speed into dramatic apparent speed."
            ),
            "meaning": "The archive establishes many impressions of acceleration, but almost no reproducible acceleration measurements.",
        },
        {
            "kind": "Connect",
            "title": "“Transmedium” is usually an inference at an occlusion boundary",
            "evidence": (
                f"{len(transmedium)} rows carry a transmedium capability label; only {direct_water} records a direct water-interaction motion class. "
                "The Greece PR-035 target simply loses contrast when the background changes."
            ),
            "meaning": "Disappearances near water or cloud should not be promoted to medium crossings without continuous range-resolved tracking.",
        },
        {
            "kind": "Do not connect",
            "title": "Repeated shapes do not establish a common craft",
            "evidence": "“Orb,” “disc,” and “tic-tac” pool events separated by decades, sensors, distances, lighting, and levels of detail.",
            "meaning": "Morphology is useful for organizing reports, but it is too underdetermined to infer one manufacturer, operator, or technology.",
        },
        {
            "kind": "Do not connect",
            "title": "Operational hotspots are also sensor-and-mission hotspots",
            "evidence": "Modern clusters concentrate around CENTCOM, maritime patrol, training ranges, and sensitive installations where observing systems and reporting channels are active.",
            "meaning": "A map of reports is not automatically a map of object origins or intent.",
        },
        {
            "kind": "Connect",
            "title": "Later records can reverse an early mystery",
            "evidence": "Fosterbrook and Maury Island look stronger when the initial recovery or debris memo is read alone; related files supply admissions, ordinary components, or slag findings.",
            "meaning": "Cross-record chronology is often more probative than the most dramatic single page.",
        },
    ]


def build_analysis(
    analysis: dict[str, Any],
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    raw_incidents = list(analysis.get("incidents") or [])
    raw_documents = list(analysis.get("documents") or [])
    assessed_incidents = [assess_record(record) for record in raw_incidents]
    assessed_reports = aggregate_source_reports(raw_documents, assessed_incidents)
    phenomenon_profiles = build_phenomenon_profiles(assessed_incidents)
    behavior_claims = build_behavior_claims(raw_incidents)
    case_dossiers = build_case_dossiers(assessed_incidents)
    residual_cases = build_residual_cases(assessed_incidents)
    connection_insights = build_connection_insights(raw_incidents, assessed_incidents)
    era_comparison = build_era_comparison(assessed_incidents)

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
    source_resolved = sum(
        record["model_status"] in {
            "Source-resolved",
            "Resolved across related records",
            "Resolved in related investigation records",
            "Resolved by AARO",
        }
        for record in assessed_incidents
    )
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
            "source_resolved": source_resolved,
            "residual_case_count": len(residual_cases),
            "verified_exotic_technology": 0,
        },
        "verdict": {
            "headline": "The reports do not describe one phenomenon; they describe recurring ways ordinary objects and ambiguous observations become extraordinary-looking.",
            "summary": (
                "Historical discs are primarily visual-era reports; modern military cases are usually unresolved contrast targets; night orbs are distant lights without range; "
                "and many spectacular motion claims weaken when sensor motion, parallax, contrast loss, or missing geometry is considered. "
                "Some radar, close-range, and multisensor cases remain worth investigating, but no released case verifies exotic technology."
            ),
            "confidence": "Moderate-to-high for the corpus-level conclusion; low-to-moderate for many individual identities.",
        },
        "phenomenon_story": {
            "answer": (
                "The best-supported explanation is a heterogeneous mixture: aircraft and military activity, balloons and small airborne objects, "
                "astronomical and atmospheric lights, sensor/viewing effects, mission debris, and occasional fabrication. "
                "The remaining residue is heterogeneous too; it does not converge on one craft shape, behavior, geography, or operating signature."
            ),
            "profiles": phenomenon_profiles,
            "behavior_claims": behavior_claims,
            "era_comparison": era_comparison,
            "connections": connection_insights,
            "case_dossiers": case_dossiers,
            "residual_cases": residual_cases,
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
            "phenomenon_group": count_values(assessed_incidents, "phenomenon_group"),
            "mechanism_group": count_values(assessed_incidents, "mechanism_group"),
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
                "section": "Reported phenomena",
                "question": "What types of objects or effects did witnesses and sensors actually describe?",
                "family": "Composition",
                "type": "stacked bar",
                "fields": ["phenomenon_group", "count", "share"],
                "takeaway": "The reports divide into distinct observation families rather than one stable craft type.",
                "palette": "relaxed multi-category, five roots plus neutral Other",
            },
            {
                "section": "Extraordinary behavior",
                "question": "How often do extraordinary-performance labels have instrument-grade support?",
                "family": "Comparison",
                "type": "paired horizontal bars",
                "fields": ["behavior", "reported", "instrument_grade"],
                "takeaway": "Reported extraordinary behavior greatly exceeds physically measured behavior.",
                "palette": "hard two-root cap: gold reports, teal instrument-grade subset",
            },
            {
                "section": "Timeline",
                "question": "How does the reported phenomenon change with era and observing system?",
                "family": "Composition over time",
                "type": "grouped decade columns",
                "fields": ["decade", "phenomenon_group", "count"],
                "takeaway": "Visual-era discs give way to modern sensor-only contrast targets.",
                "palette": "same phenomenon palette as the composition chart",
            },
            {
                "section": "Leading mechanisms",
                "question": "Which physical or observational explanation families best fit the reports?",
                "family": "Comparison & ranking",
                "type": "horizontal bar",
                "fields": ["mechanism_group", "count"],
                "takeaway": "Conventional objects plus range, sensor, and perceptual ambiguity explain the largest groups.",
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
