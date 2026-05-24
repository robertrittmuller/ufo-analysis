#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
from html import escape as html_escape
import io
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib import error as urllib_error
from urllib import request as urllib_request

import fitz
import geonamescache
import pytesseract
from dateutil import parser as date_parser
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = REPO_ROOT / "data" / "sources"
DEFAULT_OUTPUT_HTML = REPO_ROOT / "dashboard" / "index.html"
DEFAULT_OUTPUT_JSON = REPO_ROOT / "data" / "processed" / "ufo_dashboard_analysis.json"
DEFAULT_DOCUMENT_CACHE_DIR = REPO_ROOT / "data" / "processed" / "documents"
DEFAULT_TRANSCRIPT_CACHE_DIR = REPO_ROOT / "data" / "processed" / "transcripts"
DEFAULT_SOURCE_MANIFEST = REPO_ROOT / "data" / "processed" / "source_manifest.json"
DEFAULT_REVIEW_FILE = REPO_ROOT / "data" / "reviewed" / "document_reviews.json"
DEFAULT_EMBEDDING_CACHE = REPO_ROOT / "data" / "processed" / "source_embeddings.json"
DEFAULT_SITE_URL = "https://ufo-analysis.rittmuller.com"

SEO_TITLE = "UAP/UFO Research Dashboard | Public Government Records Analysis"
SEO_DESCRIPTION = (
  "Explore an AI-assisted analysis of public UAP and UFO government records, including source timelines, "
  "evidence classification, observed capabilities, agencies, locations, and source summaries."
)
SEO_IMAGE_PATH = "assets/hero-classified-saucer.png"
SEO_FAVICON_PATH = "favicon.svg"


DOCUMENT_CACHE_VERSION = 3
SOURCE_MANIFEST_VERSION = 1
EMBEDDING_CACHE_VERSION = 1
EXECUTIVE_SUMMARY_REVIEW_KEY = "__corpus_executive_summary__"
EXECUTIVE_SUMMARY_VERSION = 2

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav"}
SUPPORTED_SOURCE_EXTENSIONS = PDF_EXTENSIONS | IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
MEDIA_TYPES = {"image", "video", "audio"}
AUDIO_TRANSCRIPT_FAILURE_PATTERNS = (
  "cannot directly process audio",
  "cannot process audio",
  "can't process audio",
  "unable to process audio",
  "i cannot listen",
  "no actual audio",
  "no audio file",
  "no audio data",
  "no audio was provided",
  "haven't provided any audio",
  "language none",
  "<asr_text>",
  "без генерации",
  "текущий уровень",
)
MIN_ASR_TRANSCRIPT_ALNUM_CHARS = 8


EVIDENCE_CATEGORY_DEFINITIONS = (
  {
    "key": "category_one",
    "label": "Category One",
    "rank": 1,
    "description": "Detailed event reports with eyewitness depth and at least one hard-evidence signal that a real incident occurred.",
  },
  {
    "key": "category_two",
    "label": "Category Two",
    "rank": 2,
    "description": "Event records with some supporting evidence, but not enough to justify a Category One designation.",
  },
  {
    "key": "category_three",
    "label": "Category Three",
    "rank": 3,
    "description": "Records with limited evidence, weak corroboration, or only sparse eyewitness support.",
  },
)

EVIDENCE_CATEGORY_BY_KEY = {entry["key"]: entry for entry in EVIDENCE_CATEGORY_DEFINITIONS}
EVIDENCE_CATEGORY_BY_LABEL = {entry["label"].lower(): entry for entry in EVIDENCE_CATEGORY_DEFINITIONS}
EVIDENCE_CATEGORY_ALIASES = {
  "category 1": "category_one",
  "category one": "category_one",
  "level 1": "category_one",
  "level one": "category_one",
  "cat 1": "category_one",
  "cat one": "category_one",
  "category 2": "category_two",
  "category two": "category_two",
  "level 2": "category_two",
  "level two": "category_two",
  "cat 2": "category_two",
  "cat two": "category_two",
  "category 3": "category_three",
  "category three": "category_three",
  "level 3": "category_three",
  "level three": "category_three",
  "cat 3": "category_three",
  "cat three": "category_three",
}

EVENT_DOCUMENT_TYPES = {"Incident Summary", "Mission Report", "Debrief / Reporting Form", "Transcript", "UAP Report"}
EVENT_KEYWORDS = (
  "incident",
  "sighting",
  "sightings",
  "observed",
  "witness",
  "witnesses",
  "eyewitness",
  "encounter",
  "reported",
  "reporting",
  "object",
  "objects",
  "craft",
  "disc",
  "fireball",
  "phenomena",
)
DETAIL_KEYWORDS = (
  "altitude",
  "speed",
  "trajectory",
  "duration",
  "measurement",
  "measurements",
  "checklist",
  "specific",
  "detailed",
  "statement",
  "statements",
  "timeline",
  "searchlights",
)
WITNESS_KEYWORDS = (
  "multiple witnesses",
  "several witnesses",
  "witness",
  "witnesses",
  "eyewitness",
  "eyewitnesses",
  "observer",
  "observers",
  "pilot",
  "pilots",
  "crew",
  "civilian",
  "personnel",
)
HARD_EVIDENCE_KEYWORDS = (
  "radar",
  "sensor",
  "tracked",
  "tracking",
  "photograph",
  "photo",
  "image",
  "video",
  "film",
  "fragment",
  "fragments",
  "debris",
  "sample",
  "salvage",
  "recovered",
  "recovery",
  "physical evidence",
  "sketch",
)
SUPPORT_KEYWORDS = (
  "official",
  "investigation",
  "investigated",
  "intelligence",
  "memorandum",
  "report",
  "reporting form",
  "declassified",
  "air force",
  "navy",
  "army",
  "fbi",
)
REVIEW_FOCUS_KEYWORDS = (
  "uap",
  "ufo",
  "unidentified aerial",
  "unidentified anomal",
  "flying disc",
  "flying saucer",
  "sighting",
  "sightings",
  "encounter",
  "observed",
  "orb",
  "orbs",
  "sphere",
  "triangular",
  "metallic",
)


YEAR_PATTERN = re.compile(r"\b(19[0-9]{2}|20[0-2][0-9])\b")
MONTH_PATTERN = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"(?:\s+\d{1,2},)?\s+\d{4}\b",
    re.IGNORECASE,
)
NUMERIC_DATE_PATTERN = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b")
TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z'/-]{2,}")
SPACE_PATTERN = re.compile(r"\s+")

STOPWORDS = {
    "about",
    "after",
    "again",
    "agency",
    "air",
    "also",
    "analysis",
    "and",
    "any",
    "are",
    "armed",
    "been",
    "before",
    "being",
    "between",
    "box",
    "brief",
    "case",
    "chief",
    "code",
    "collection",
    "concerning",
    "copy",
    "date",
    "de",
    "declassified",
    "defense",
    "department",
    "document",
    "documents",
    "during",
    "email",
    "file",
    "files",
    "flying",
    "form",
    "from",
    "general",
    "government",
    "have",
    "headquarters",
    "incident",
    "incidents",
    "information",
    "intelligence",
    "into",
    "investigation",
    "july",
    "june",
    "made",
    "memo",
    "message",
    "mission",
    "more",
    "national",
    "navy",
    "note",
    "numeric",
    "object",
    "office",
    "operation",
    "page",
    "pages",
    "part",
    "photo",
    "press",
    "record",
    "records",
    "reference",
    "relating",
    "report",
    "reporting",
    "reports",
    "sighting",
    "sightings",
    "serial",
    "section",
    "should",
    "source",
    "statement",
    "subject",
    "summary",
    "there",
    "these",
    "this",
    "through",
    "transcript",
    "unidentified",
    "unresolved",
    "upon",
    "uap",
    "ufo",
    "usa",
    "using",
    "volume",
    "what",
    "when",
    "which",
    "with",
    "would",
}

THEME_KEYWORDS = {
    "Aerospace Operations": ["aircraft", "flight", "pilot", "crew", "sortie", "takeoff", "landing", "airborne"],
    "Sensors and Radar": ["radar", "sensor", "track", "infrared", "flir", "electro-optical", "signature"],
    "Maritime Theaters": ["sea", "gulf", "maritime", "naval", "ship", "vessel", "carrier", "coast"],
    "Launch and Strategic": ["launch", "missile", "rocket", "nuclear", "vandenberg", "strategic"],
    "Imagery and Visuals": ["photo", "image", "camera", "video", "sketch", "composite", "visual"],
    "Diplomatic and Intel": ["embassy", "consulate", "cable", "classified", "intelligence", "fbi", "state department"],
    "Spaceflight": ["apollo", "gemini", "skylab", "astronaut", "lunar", "nasa"],
    "Anomalous Objects": ["disc", "orb", "light", "sphere", "glow", "object", "unresolved"],
}

CAPABILITY_DEFINITIONS = (
  {
    "key": "instant_acceleration",
    "label": "Instant Acceleration",
    "group": "Maneuver",
    "description": "Observed rapid acceleration, sudden departure, or high-speed transit.",
    "keywords": (
      "instant acceleration",
      "instantaneous acceleration",
      "rapid acceleration",
      "accelerated rapidly",
      "sudden acceleration",
      "rapidly exits",
      "exits the frame",
      "fast moving",
      "high speed",
      "supersonic",
      "extreme speed",
      "shot across",
    ),
  },
  {
    "key": "abrupt_vector_change",
    "label": "Abrupt Vector Change",
    "group": "Maneuver",
    "description": "Observed sharp turns, corkscrews, erratic movement, or non-ballistic path changes.",
    "keywords": (
      "90-degree",
      "ninety-degree",
      "right-angle",
      "abrupt turn",
      "sharp turn",
      "directional change",
      "changed direction",
      "erratic movement",
      "corkscrew",
      "zigzag",
      "maneuver",
      "non-ballistic",
    ),
  },
  {
    "key": "stationary_hover",
    "label": "Stationary Hover",
    "group": "Maneuver",
    "description": "Observed hover, loiter, stationary hold, or slow movement without apparent lift surfaces.",
    "keywords": (
      "hover",
      "hovering",
      "stationary",
      "remained fixed",
      "motionless",
      "loiter",
      "slow moving",
      "sustained position",
      "held position",
    ),
  },
  {
    "key": "formation_behavior",
    "label": "Formation Behavior",
    "group": "Coordination",
    "description": "Observed multiple objects moving together, swarming, or holding a geometric arrangement.",
    "keywords": (
      "formation",
      "formations",
      "multiple objects",
      "several objects",
      "swarm",
      "swarmed",
      "t-configuration",
      "cluster",
      "group of",
      "dual uap",
      "three fast",
      "4 uap",
    ),
  },
  {
    "key": "transmedium",
    "label": "Transmedium / USO",
    "group": "Environment",
    "description": "Observed interaction with water or movement between aerial and maritime environments.",
    "keywords": (
      "uso",
      "transmedium",
      "in and out of water",
      "entered the water",
      "exited the water",
      "submerged",
      "underwater",
      "over water",
      "near sub",
      "maritime",
    ),
  },
  {
    "key": "obscuration_traversal",
    "label": "Obscuration Traversal",
    "group": "Environment",
    "description": "Observed movement through clouds, haze, or obscured environments while remaining trackable.",
    "keywords": (
      "cloud",
      "clouds",
      "in and out of clouds",
      "through clouds",
      "obscured",
      "haze",
      "low contrast",
      "intermittently passes in and out",
      "passes in and out",
    ),
  },
  {
    "key": "thermal_or_luminous",
    "label": "Thermal / Luminous Signature",
    "group": "Signature",
    "description": "Observed heat, infrared contrast, bright light, glow, pulsing, flame, or orb-like luminosity.",
    "keywords": (
      "infrared",
      "ir hot",
      "thermal",
      "bright object",
      "bright area",
      "area of contrast",
      "glow",
      "glowing",
      "orange orb",
      "orb",
      "orbs",
      "pulsing",
      "flame",
      "luminous",
      "light source",
    ),
  },
  {
    "key": "sensor_tracked",
    "label": "Sensor Tracked",
    "group": "Evidence",
    "description": "Observed through radar, FLIR, infrared, electro-optical, LiDAR, or other sensor tracking.",
    "keywords": (
      "radar",
      "sensor",
      "tracked",
      "tracking",
      "track",
      "flir",
      "tflir",
      "infrared sensor",
      "electro-optical",
      "lidar",
      "crosshair",
      "targeting pod",
    ),
  },
  {
    "key": "physical_trace",
    "label": "Physical Trace",
    "group": "Evidence",
    "description": "Observed physical residue, ground effect, indentation, debris, or other trace evidence.",
    "keywords": (
      "physical trace",
      "physical evidence",
      "indentations",
      "burned vegetation",
      "burned grass",
      "residue",
      "debris",
      "fragments",
      "ground evidence",
      "landing marks",
    ),
  },
  {
    "key": "low_observable",
    "label": "Low Observable",
    "group": "Signature",
    "description": "Observed faint, diffuse, low-contrast, silent, or intermittently visible behavior.",
    "keywords": (
      "low observable",
      "low-contrast",
      "low contrast",
      "diffuse",
      "faint",
      "no distinct object",
      "silent",
      "no sound",
      "intermittently",
      "vague area",
      "grainy",
    ),
  },
)

AGENCY_PATTERNS = {
    "FBI": ["fbi"],
    "NASA": ["nasa", "apollo", "gemini", "skylab"],
    "State Department": ["state department", "embassy", "consulate", "cable"],
    "Air Force": ["air force", "usaf", "usaf", "vandenberg"],
    "Navy": ["navy", "carrier", "fleet"],
    "CENTCOM": ["centcom", "uscentcom"],
    "INDOPACOM": ["indopacom"],
}

DOCUMENT_TYPE_PATTERNS = [
    ("Mission Report", ["mission report"]),
    ("Incident Summary", ["incident summary", "incident summaries"]),
    ("Transcript", ["transcript", "crew debriefing"]),
    ("Video", ["video"]),
    ("Audio", ["audio excerpt", "audio recording"]),
    ("Debrief / Reporting Form", ["debrief", "reporting form"]),
    ("Diplomatic Cable", ["cable"]),
    ("Email Correspondence", ["email correspondence", "email correspondance", "email"]),
    ("Imagery", ["photo", "composite sketch", "sketch"]),
    ("Statement", ["statement"]),
    ("Archival File", ["numeric file", "numerical file", "general", "records", "serial"]),
]

DOCUMENT_TYPE_LABELS = {label for label, _ in DOCUMENT_TYPE_PATTERNS} | {"Research File", "UAP Report"}

TREND_FIELD_DEFINITIONS: dict[str, dict[str, object]] = {
    "event_count": {
        "type": "integer_or_null",
        "description": "Estimated number of distinct UAP/UFO events or sightings described by this source; 0 for non-event material.",
    },
    "event_record_type": {
        "type": "enum",
        "values": [
            "sighting",
            "sensor_detection",
            "investigation_or_followup",
            "media_asset",
            "transcript_or_audio_account",
            "policy_or_research_discussion",
            "explanation_or_debunk",
            "non_event_context",
            "unknown",
        ],
    },
    "primary_observation_mode": {
        "type": "enum",
        "values": [
            "naked_eye_visual",
            "cockpit_visual",
            "ground_visual",
            "photo_image",
            "eo_video",
            "ir_thermal",
            "radar",
            "rf_sigint",
            "audio_testimony",
            "transcript",
            "mixed",
            "none_stated",
            "unknown",
        ],
    },
    "sensor_platform": {
        "type": "enum",
        "values": [
            "aircraft",
            "ground_station_or_tower",
            "ship_or_maritime",
            "spacecraft",
            "handheld_or_consumer_camera",
            "facility_security_system",
            "not_applicable",
            "unknown",
        ],
    },
    "observer_platform": {
        "type": "enum",
        "values": [
            "ground",
            "aircraft",
            "vehicle",
            "ship_or_boat",
            "spacecraft",
            "facility",
            "multiple",
            "not_applicable",
            "unknown",
        ],
    },
    "observer_roles": {
        "type": "enum_list",
        "values": [
            "pilot_or_aircrew",
            "sensor_operator",
            "intelligence_official",
            "contractor",
            "civilian",
            "law_enforcement",
            "astronaut",
            "military_personnel",
            "analyst_or_investigator",
            "diplomatic_or_government_reporter",
            "unknown",
            "not_applicable",
        ],
    },
    "witness_count_bucket": {
        "type": "enum",
        "values": ["none", "single", "two_to_three", "group", "multiple_independent_groups", "unknown"],
    },
    "corroboration_types": {
        "type": "enum_list",
        "values": [
            "none_stated",
            "multiple_witnesses",
            "multiple_sensors",
            "photo_or_video",
            "radar_or_sensor",
            "physical_trace",
            "official_investigation",
            "transcript_or_audio",
            "unclear",
        ],
    },
    "chain_of_custody_quality": {
        "type": "enum",
        "values": [
            "original_or_near_original_report",
            "official_summary_or_memo",
            "official_media_release",
            "uploaded_or_altered_media",
            "archival_clipping_or_secondhand",
            "compiled_multi_case_file",
            "unknown",
        ],
    },
    "redaction_level": {
        "type": "enum",
        "values": ["none", "minor", "moderate", "heavy", "unknown"],
    },
    "event_time_precision": {
        "type": "enum",
        "values": ["exact_utc", "exact_local", "full_date", "month_year", "year_only", "range_only", "unknown"],
    },
    "location_precision": {
        "type": "enum",
        "values": [
            "coordinates",
            "site_or_facility",
            "city_or_local_area",
            "region_or_maritime_area",
            "country",
            "command_area",
            "space_or_orbit",
            "unknown_redacted",
        ],
    },
    "environment_context_present": {"type": "boolean"},
    "day_night_context": {
        "type": "enum",
        "values": ["daylight", "night", "dawn_or_dusk", "space_or_orbit", "indoor_or_internal_visual_effect", "unknown"],
    },
    "object_count_bucket": {
        "type": "enum",
        "values": ["none", "one", "two", "three_to_five", "many_or_swarm", "unknown"],
    },
    "morphology_normalized": {
        "type": "enum_list",
        "values": [
            "sphere_or_orb",
            "disc_or_saucer",
            "cylinder_cigar_tictac",
            "triangle",
            "light_point",
            "fireball",
            "irregular_blob_or_area_of_contrast",
            "formation_only",
            "none_stated",
            "unknown",
        ],
    },
    "color_luminosity_normalized": {
        "type": "enum_list",
        "values": [
            "white_or_bright",
            "orange",
            "green",
            "red",
            "blue",
            "metallic",
            "dark",
            "transparent_or_translucent",
            "thermal_bright",
            "pulsing_or_flashing",
            "none_stated",
            "unknown",
        ],
    },
    "apparent_motion_class": {
        "type": "enum_list",
        "values": [
            "stationary_hover",
            "straight_line_transit",
            "ascending_or_descending",
            "erratic_or_abrupt",
            "accelerating_or_departing",
            "disappearing_or_dissipating",
            "obscured_or_cloud_traversal",
            "water_interaction",
            "no_motion_stated",
            "unknown",
        ],
    },
    "mundane_explanation_present": {
        "type": "enum",
        "values": ["yes_source_resolved", "yes_source_suggested", "no", "unclear"],
    },
    "resolution_status": {
        "type": "enum",
        "values": ["unresolved", "explained", "partially_explained", "disputed", "informational_only", "not_an_event", "unknown"],
    },
    "media_authenticity_notes": {
        "type": "string_or_null",
        "description": "Short note on redactions, enhancement, alteration, excerpts, chain-of-custody caveats, or null if none stated.",
    },
    "measurement_quality": {
        "type": "enum",
        "values": ["qualitative_only", "estimated_values", "instrument_derived_values", "calibrated_telemetry", "no_measurements", "unknown"],
    },
    "quantitative_fields_present": {
        "type": "enum_list",
        "values": [
            "altitude",
            "speed",
            "heading_or_bearing",
            "coordinates",
            "duration",
            "range_or_distance",
            "angular_size",
            "sensor_metadata",
            "none",
        ],
    },
}

TREND_FIELD_NAMES = tuple(TREND_FIELD_DEFINITIONS.keys())

MANUAL_LOCATION_COORDS = {
    "arabian gulf": (26.0000, 52.5000, "Arabian Gulf", "region"),
    "persian gulf": (26.0000, 52.5000, "Persian Gulf", "region"),
    "gulf of aden": (12.0000, 48.0000, "Gulf of Aden", "region"),
    "arabian sea": (15.0000, 64.0000, "Arabian Sea", "region"),
    "strait of hormuz": (26.5667, 56.2500, "Strait of Hormuz", "region"),
    "mediterranean sea": (34.5531, 18.0480, "Mediterranean Sea", "region"),
    "middle east": (29.2985, 42.5510, "Middle East", "region"),
    "western us": (39.5000, -116.0000, "Western United States", "region"),
    "western united states": (39.5000, -116.0000, "Western United States", "region"),
    "vandenberg afb": (34.7420, -120.5724, "Vandenberg AFB", "site"),
    "vandenberg": (34.7420, -120.5724, "Vandenberg AFB", "site"),
    "tbilisi": (41.7151, 44.8271, "Tbilisi, Georgia", "city"),
    "ashgabat": (37.9601, 58.3261, "Ashgabat, Turkmenistan", "city"),
    "papua new guinea": (-6.314993, 143.95555, "Papua New Guinea", "country"),
}


@dataclass
class ResolvedLocation:
    label: str
    latitude: float
    longitude: float
    match: str
    kind: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a self-contained UFO research dashboard from media in data/sources.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
    default=DEFAULT_SOURCE_DIR,
        help="Directory containing source PDFs, images, and video clips.",
    )
    parser.add_argument(
        "--output-html",
        type=Path,
    default=DEFAULT_OUTPUT_HTML,
        help="Dashboard HTML output path.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
    default=DEFAULT_OUTPUT_JSON,
        help="Structured JSON analysis output path.",
    )
    parser.add_argument(
        "--site-url",
        default=DEFAULT_SITE_URL,
        help="Canonical public URL for SEO metadata, robots.txt, and sitemap.xml.",
    )
    parser.add_argument(
        "--document-cache-dir",
        type=Path,
    default=DEFAULT_DOCUMENT_CACHE_DIR,
        help="Directory for per-document JSON cache files used to resume interrupted runs.",
    )
    parser.add_argument(
        "--transcript-cache-dir",
        type=Path,
        default=DEFAULT_TRANSCRIPT_CACHE_DIR,
        help="Directory for caption/transcript text extracted from media assets.",
    )
    parser.add_argument(
        "--audio-transcript-mode",
        choices=("off", "captions", "llm"),
        default="llm",
        help=(
          "How to extract media transcripts: off disables transcripts, captions uses only source captions, "
          "and llm also extracts audio tracks for the local model when captions are unavailable."
        ),
    )
    parser.add_argument(
        "--audio-transcript-model",
        default="Qwen3-ASR-0.6B-4bit",
        help="Local speech-to-text model name for audio transcription.",
    )
    parser.add_argument(
        "--audio-transcript-max-seconds",
        type=int,
        default=120,
        help="Maximum seconds of an audio track to send to the model for transcript extraction.",
    )
    parser.add_argument(
        "--audio-transcript-timeout",
        type=int,
        default=75,
        help="Timeout in seconds for each local model audio transcription request.",
    )
    parser.add_argument(
        "--audio-transcript-format",
        choices=("wav", "mp3"),
        default="wav",
        help="Audio format sent to the local model for transcript extraction.",
    )
    parser.add_argument(
      "--source-manifest",
      type=Path,
    default=DEFAULT_SOURCE_MANIFEST,
      help="Optional JSON manifest mapping local filenames to source metadata.",
    )
    parser.add_argument(
        "--refresh-document-cache",
        action="store_true",
        help="Ignore existing per-document JSON cache files and rebuild document analyses.",
    )
    parser.add_argument(
      "--review-file",
      type=Path,
      default=DEFAULT_REVIEW_FILE,
      help="Optional JSON file containing reviewed document overrides keyed by filename.",
    )
    parser.add_argument(
      "--review-mode",
      choices=("manual", "hybrid"),
      default="hybrid",
      help="Review strategy: manual uses only saved review-file overrides, hybrid also generates missing reviews with the local model.",
    )
    parser.add_argument(
      "--refresh-reviews",
      action="store_true",
      help="With --review-mode hybrid, rerun the local model review even for documents that already have saved reviews.",
    )
    parser.add_argument(
      "--refresh-audio-context-only",
      action="store_true",
      help="With --review-mode hybrid, only generate missing audio source and transcript-summary review fields, then exit.",
    )
    parser.add_argument(
      "--review-base-url",
      default="http://localhost:4321/v1",
      help="Base URL for the local OpenAI-compatible review model endpoint.",
    )
    parser.add_argument(
      "--review-model",
      default="Qwen3.6-35B-A3B-UD-MLX-4bit",
      help="Model name to use for local multimodal review generation.",
    )
    parser.add_argument(
      "--review-api-key",
      default="REDACTED",
      help="API key for authenticating with the local model endpoint.",
    )
    parser.add_argument(
      "--review-timeout",
      type=int,
      default=300,
      help="Timeout in seconds for each local model review request.",
    )
    parser.add_argument(
      "--review-max-images",
      type=int,
      default=2,
      help="Maximum number of rendered PDF page images to send with each multimodal review request.",
    )
    parser.add_argument(
      "--related-sources-mode",
      choices=("auto", "off"),
      default="auto",
      help="Use source embeddings to attach related_sources to each document. Use off to skip relationship generation.",
    )
    parser.add_argument(
      "--embedding-model",
      default="Qwen3-Embedding-0.6B-4bit-DWQ",
      help="OpenAI-compatible embedding model name used to identify related sources.",
    )
    parser.add_argument(
      "--embedding-base-url",
      default=None,
      help="Base URL for embeddings. Defaults to --review-base-url.",
    )
    parser.add_argument(
      "--embedding-api-key",
      default=None,
      help="API key for embeddings. Defaults to --review-api-key.",
    )
    parser.add_argument(
      "--embedding-timeout",
      type=int,
      default=180,
      help="Timeout in seconds for each embedding request.",
    )
    parser.add_argument(
      "--embedding-cache",
      type=Path,
      default=DEFAULT_EMBEDDING_CACHE,
      help="JSON cache for per-source embeddings.",
    )
    parser.add_argument(
      "--refresh-embeddings",
      action="store_true",
      help="Ignore cached embeddings and request fresh vectors.",
    )
    parser.add_argument(
      "--related-sources-limit",
      type=int,
      default=6,
      help="Maximum number of related sources to attach to each document.",
    )
    parser.add_argument(
      "--related-sources-min-score",
      type=float,
      default=0.25,
      help="Minimum cosine similarity required for a related source link.",
    )
    parser.add_argument(
        "--max-docs",
        type=int,
        default=None,
        help="Optional cap on the number of documents to process.",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=None,
        help="Optional per-document page cap for faster iteration.",
    )
    parser.add_argument(
        "--ocr-max-pages-per-document",
        type=int,
        default=18,
        help="Maximum low-text pages to OCR per document. Use 0 for unlimited.",
    )
    parser.add_argument(
        "--ocr-dpi",
        type=int,
        default=170,
        help="Render resolution for OCR pages.",
    )
    parser.add_argument(
        "--min-native-chars",
        type=int,
        default=120,
        help="Native page text below this threshold triggers OCR fallback.",
    )
    return parser.parse_args()


def _same_path(left: Path, right: Path) -> bool:
  return left.expanduser().resolve() == right.expanduser().resolve()


def validate_output_targets(args: argparse.Namespace) -> None:
  has_limited_scope = args.max_docs is not None or args.page_limit is not None
  if not has_limited_scope:
    return

  if _same_path(args.output_html, DEFAULT_OUTPUT_HTML) or _same_path(args.output_json, DEFAULT_OUTPUT_JSON):
    raise ValueError(
      "Limited runs with --max-docs or --page-limit must write to non-canonical outputs. "
      "Pass both --output-html and --output-json to a scratch path, or rerun without limits."
    )


def normalize_space(text: str) -> str:
    return SPACE_PATTERN.sub(" ", text.replace("\x00", " ")).strip()


def slug_title(stem: str) -> str:
    return normalize_space(stem.replace("_", " ").replace("-", " "))


def cleaned_char_count(text: str) -> int:
    return sum(char.isalnum() for char in text)


def load_review_overrides(review_file: Path) -> dict[str, dict[str, object]]:
  if not review_file.exists():
    return {}

  payload = json.loads(review_file.read_text(encoding="utf-8"))
  if not isinstance(payload, dict):
    raise ValueError(f"Review file must contain a JSON object keyed by filename: {review_file}")

  overrides: dict[str, dict[str, object]] = {}
  for filename, review in payload.items():
    if not isinstance(filename, str) or not isinstance(review, dict):
      continue
    overrides[filename] = review
  return overrides


def legacy_review_filename_candidates(filename: str) -> list[str]:
  candidates = [filename]
  replacements = (
    (r"(?<=DOW-UAP-D)0+(\d+)", r"\1"),
    (r"(?<=DOW-UAP-PR)0+(\d+)", r"\1"),
    (r"(?<=NASA-UAP-D)0+(\d+)", r"\1"),
    (r"(?<=NASA-UAP-VM)0+(\d+)", r"\1"),
    (r"(?<=FBI_Photo_[AB])0+(\d+)", r"\1"),
    (r"(?<=State_Department_UAP_Cable_)0+(\d+)", r"\1"),
    (r"(?<=_Section_)0+(\d+)", r"\1"),
    (r"(?<=_Serial_)0+(\d+)", r"\1"),
  )

  for pattern, replacement in replacements:
    for candidate in tuple(candidates):
      legacy_candidate = re.sub(pattern, replacement, candidate)
      if legacy_candidate not in candidates:
        candidates.append(legacy_candidate)

  return candidates


def find_review_override(review_overrides: dict[str, dict[str, object]], filename: str) -> dict[str, object] | None:
  for candidate in legacy_review_filename_candidates(filename):
    review = review_overrides.get(candidate)
    if review is not None:
      return review
  return None


def write_review_overrides(review_file: Path, overrides: dict[str, dict[str, object]]) -> None:
  review_file.parent.mkdir(parents=True, exist_ok=True)
  ordered = {filename: overrides[filename] for filename in sorted(overrides)}
  review_file.write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")


def document_cache_path(cache_dir: Path, source_dir: Path, path: Path) -> Path:
  return cache_dir / path.relative_to(source_dir).with_suffix(".json")


def build_document_cache_options(
    page_limit: int | None,
    ocr_max_pages_per_document: int | None,
    ocr_dpi: int,
    min_native_chars: int,
) -> dict[str, object]:
  return {
      "page_limit": page_limit,
      "ocr_max_pages_per_document": ocr_max_pages_per_document,
      "ocr_dpi": ocr_dpi,
      "min_native_chars": min_native_chars,
  }


def load_source_manifest(path: Path) -> dict[str, dict[str, object]]:
  if not path.exists():
    return {}

  try:
    payload = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError):
    return {}

  if not isinstance(payload, dict):
    return {}

  entries = payload.get("documents")
  if payload.get("manifest_version") != SOURCE_MANIFEST_VERSION or not isinstance(entries, list):
    return {}

  manifest: dict[str, dict[str, object]] = {}
  for entry in entries:
    if not isinstance(entry, dict):
      continue
    filename = entry.get("filename")
    if isinstance(filename, str) and filename:
      manifest[filename] = entry
  return manifest


def original_source_url(source_metadata: dict[str, object] | None) -> str | None:
  if not source_metadata:
    return None
  value = source_metadata.get("original_source_url")
  if not isinstance(value, str):
    return None
  cleaned = normalize_space(value)
  if not cleaned or cleaned.upper() == "N/A":
    return None
  return cleaned


def manifest_text(source_metadata: dict[str, object] | None, *keys: str) -> str:
  if not source_metadata:
    return ""
  values = []
  for key in keys:
    value = source_metadata.get(key)
    if value is None:
      continue
    cleaned = normalize_space(str(value))
    if cleaned and cleaned.upper() != "N/A":
      values.append(cleaned)
  return " ".join(dict.fromkeys(values))


def first_manifest_text(source_metadata: dict[str, object] | None, *keys: str) -> str | None:
  if not source_metadata:
    return None
  for key in keys:
    value = source_metadata.get(key)
    if value is None:
      continue
    cleaned = normalize_space(str(value))
    if cleaned and cleaned.upper() != "N/A":
      return cleaned
  return None


def source_thumbnail_url(source_metadata: dict[str, object] | None, media_type: str) -> str | None:
  if media_type in {"audio", "video"}:
    return first_manifest_text(source_metadata, "thumbnail_url", "modal_image_url")
  if media_type == "image":
    return first_manifest_text(source_metadata, "modal_image_url", "page_media_url", "original_source_url")
  return first_manifest_text(source_metadata, "modal_image_url")


def load_document_cache(
    cache_file: Path,
    path: Path,
    source_dir: Path,
    current_review: dict[str, object] | None,
    page_limit: int | None,
    ocr_max_pages_per_document: int | None,
    ocr_dpi: int,
    min_native_chars: int,
) -> dict[str, object] | None:
  if not cache_file.exists():
    return None

  try:
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError):
    return None

  if not isinstance(payload, dict) or payload.get("cache_version") != DOCUMENT_CACHE_VERSION:
    return None

  stat = path.stat()
  relative_path = str(path.relative_to(source_dir)).replace("\\", "/")
  source_info = payload.get("source_file")
  expected_source_info = {
      "relative_path": relative_path,
      "size": stat.st_size,
      "modified_ns": stat.st_mtime_ns,
  }
  if source_info != expected_source_info:
    return None

  options = payload.get("analysis_options")
  if options != build_document_cache_options(page_limit, ocr_max_pages_per_document, ocr_dpi, min_native_chars):
    return None

  cached_review = payload.get("applied_review")
  if cached_review is not None and not isinstance(cached_review, dict):
    return None
  if current_review is not None and cached_review != current_review:
    return None

  document = payload.get("document")
  if not isinstance(document, dict):
    return None

  payload["document"] = annotate_evidence_classification(ensure_trend_review_fields(document))

  return payload


def write_document_cache(
    cache_file: Path,
    path: Path,
    source_dir: Path,
    document: dict[str, object],
    applied_review: dict[str, object] | None,
    page_limit: int | None,
    ocr_max_pages_per_document: int | None,
    ocr_dpi: int,
    min_native_chars: int,
) -> None:
  stat = path.stat()
  cache_file.parent.mkdir(parents=True, exist_ok=True)
  payload = {
      "cache_version": DOCUMENT_CACHE_VERSION,
      "source_file": {
          "relative_path": str(path.relative_to(source_dir)).replace("\\", "/"),
          "size": stat.st_size,
          "modified_ns": stat.st_mtime_ns,
      },
      "analysis_options": build_document_cache_options(page_limit, ocr_max_pages_per_document, ocr_dpi, min_native_chars),
      "applied_review": applied_review,
      "document": document,
  }
  cache_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_enum_token(value: object) -> str:
  return normalize_space(str(value)).lower().replace(" ", "_").replace("-", "_")


def normalize_enum_value(value: object, allowed_values: list[str], default: str) -> str:
  if isinstance(value, str):
    normalized = normalize_enum_token(value)
    allowed_by_normalized = {normalize_enum_token(item): item for item in allowed_values}
    if normalized in allowed_by_normalized:
      return allowed_by_normalized[normalized]
  return default


def normalize_enum_list(value: object, allowed_values: list[str], default: str = "unknown") -> list[str]:
  raw_items = value if isinstance(value, list) else [value]
  cleaned: list[str] = []
  for item in raw_items:
    if not isinstance(item, str):
      continue
    normalized = normalize_enum_value(item, allowed_values, "")
    if normalized and normalized not in cleaned:
      cleaned.append(normalized)

  if cleaned:
    if len(cleaned) > 1:
      cleaned = [item for item in cleaned if item not in {"unknown", "none", "none_stated", "not_applicable", "unclear"}]
    return cleaned[:8] if cleaned else [default]
  return [default]


def normalize_trend_review_fields(payload: dict[str, object]) -> dict[str, object]:
  trend: dict[str, object] = {}
  for field_name, definition in TREND_FIELD_DEFINITIONS.items():
    field_type = str(definition.get("type") or "")
    raw_value = payload.get(field_name)

    if field_type == "integer_or_null":
      if isinstance(raw_value, bool):
        trend[field_name] = None
      elif isinstance(raw_value, int):
        trend[field_name] = max(0, min(raw_value, 1000))
      elif isinstance(raw_value, str) and raw_value.strip().isdigit():
        trend[field_name] = max(0, min(int(raw_value.strip()), 1000))
      else:
        trend[field_name] = None
      continue

    if field_type == "boolean":
      trend[field_name] = raw_value if isinstance(raw_value, bool) else False
      continue

    if field_type == "string_or_null":
      trend[field_name] = normalize_space(raw_value) if isinstance(raw_value, str) and normalize_space(raw_value) else None
      continue

    values = [str(value) for value in definition.get("values", []) if isinstance(value, str)]
    if field_type == "enum":
      default = values[-1] if values else "unknown"
      trend[field_name] = normalize_enum_value(raw_value, values, default)
      continue

    if field_type == "enum_list":
      default = "unknown"
      if "none" in values:
        default = "none"
      elif "none_stated" in values:
        default = "none_stated"
      trend[field_name] = normalize_enum_list(raw_value, values, default=default)

  return trend


def ensure_trend_review_fields(document: dict[str, object]) -> dict[str, object]:
  merged = dict(document)
  merged.update(normalize_trend_review_fields(merged))
  return merged


def review_has_trend_fields(review: dict[str, object] | None) -> bool:
  if not isinstance(review, dict):
    return False
  normalized = normalize_trend_review_fields(review)
  return all(field_name in review and normalized.get(field_name) == review.get(field_name) for field_name in TREND_FIELD_NAMES)


def review_has_required_fields(review: dict[str, object] | None) -> bool:
  return review_has_evidence_category(review) and review_has_trend_fields(review)


def trend_review_prompt_instructions() -> str:
  field_lines: list[str] = []
  for field_name, definition in TREND_FIELD_DEFINITIONS.items():
    field_type = str(definition.get("type") or "")
    values = definition.get("values")
    if isinstance(values, list):
      field_lines.append(f"- {field_name}: {field_type}; allowed values: {', '.join(str(value) for value in values)}.")
    else:
      description = normalize_space(str(definition.get("description") or field_type))
      field_lines.append(f"- {field_name}: {field_type}; {description}")
  return (
    "Also extract these high-confidence trend fields. Use only information supported by the source text, attached media, transcript, or manifest context. "
    "Prefer unknown, none, none_stated, unclear, or null instead of guessing. "
    "For list fields, return an array of allowed values. For environment_context_present, return true only when the material explicitly mentions weather, cloud, visibility, lighting, sea state, moon/night/day context, or similar environmental context.\n"
    + "\n".join(field_lines)
  )


def apply_review_override(document: dict[str, object], review: dict[str, object] | None) -> dict[str, object]:
  document.setdefault("visual_observations", None)
  document.setdefault("review_status", "generated")
  document.setdefault("review_source", "scripted_extraction")
  if not review:
    return annotate_evidence_classification(normalize_document_after_review(ensure_trend_review_fields(document)))

  merged = dict(document)
  merged.update(review)
  merged.setdefault("review_status", "reviewed")
  merged.setdefault("review_source", "manual_ai_review")
  return annotate_evidence_classification(normalize_document_after_review(ensure_trend_review_fields(merged)))


def normalize_document_after_review(document: dict[str, object]) -> dict[str, object]:
  media_type = str(document.get("media_type") or "").lower()
  if media_type == "audio":
    document["document_type"] = "Audio"
    if document.get("extraction_method") != "audio_transcript":
      document["extraction_method"] = "audio_metadata"
  elif media_type == "video":
    document["document_type"] = "Video"
    if document.get("extraction_method") != "video_audio_transcript":
      document["extraction_method"] = "video_metadata"
  return document


def normalize_evidence_category(value: object) -> dict[str, object] | None:
  if not isinstance(value, str):
    return None
  normalized = normalize_space(value).lower()
  if not normalized:
    return None
  if normalized in EVIDENCE_CATEGORY_BY_LABEL:
    return EVIDENCE_CATEGORY_BY_LABEL[normalized]
  alias_key = EVIDENCE_CATEGORY_ALIASES.get(normalized)
  if alias_key:
    return EVIDENCE_CATEGORY_BY_KEY[alias_key]
  if normalized in EVIDENCE_CATEGORY_BY_KEY:
    return EVIDENCE_CATEGORY_BY_KEY[normalized]
  return None


def review_has_evidence_category(review: dict[str, object] | None) -> bool:
  if not isinstance(review, dict):
    return False
  return normalize_evidence_category(
      review.get("evidence_category") or review.get("classification") or review.get("evidence_classification")
  ) is not None


def count_keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
  return sum(1 for keyword in keywords if keyword in text)


def infer_evidence_category(document: dict[str, object]) -> dict[str, object]:
  manual_category = normalize_evidence_category(
      document.get("evidence_category") or document.get("classification") or document.get("evidence_classification")
  )
  if manual_category is not None:
    return manual_category

  summary_text = normalize_space(str(document.get("summary_narrative") or "")).lower()
  visual_text = normalize_space(str(document.get("visual_observations") or "")).lower()
  combined_text = f"{summary_text} {visual_text}".strip()
  document_type = str(document.get("document_type") or "")
  themes = {theme for theme in document.get("themes", []) if isinstance(theme, str)}

  event_hits = count_keyword_hits(combined_text, EVENT_KEYWORDS)
  detail_hits = count_keyword_hits(combined_text, DETAIL_KEYWORDS)
  witness_hits = count_keyword_hits(combined_text, WITNESS_KEYWORDS)
  evidence_hits = count_keyword_hits(combined_text, HARD_EVIDENCE_KEYWORDS)
  support_hits = count_keyword_hits(combined_text, SUPPORT_KEYWORDS)

  if document_type in EVENT_DOCUMENT_TYPES:
    event_hits += 2
    detail_hits += 1
  if document_type == "Imagery":
    evidence_hits += 2
  if "Imagery and Visuals" in themes:
    evidence_hits += 1
  if "Sensors and Radar" in themes:
    evidence_hits += 2
  if document.get("location"):
    detail_hits += 1
  if document.get("date_label") or document.get("year"):
    detail_hits += 1
  if document.get("review_status") == "reviewed":
    support_hits += 1
  if document.get("agencies"):
    support_hits += 1
  if "multiple witnesses" in combined_text or "several witnesses" in combined_text:
    witness_hits += 2

  event_present = event_hits >= 2 or document_type in EVENT_DOCUMENT_TYPES
  strong_evidence = evidence_hits >= 2
  strong_detail = detail_hits >= 2
  strong_witness = witness_hits >= 2
  some_support = support_hits >= 2 or evidence_hits >= 1 or witness_hits >= 1 or detail_hits >= 2

  if event_present and strong_evidence and strong_detail and strong_witness:
    return EVIDENCE_CATEGORY_BY_KEY["category_one"]
  if event_present and some_support:
    return EVIDENCE_CATEGORY_BY_KEY["category_two"]
  return EVIDENCE_CATEGORY_BY_KEY["category_three"]


def annotate_evidence_classification(document: dict[str, object]) -> dict[str, object]:
  annotated = dict(document)
  category = infer_evidence_category(annotated)
  annotated["evidence_category"] = category["label"]
  annotated["evidence_category_rank"] = category["rank"]
  annotated["evidence_category_description"] = category["description"]
  return annotated


def document_capability_text(document: dict[str, object]) -> str:
  parts: list[str] = []
  for key in (
      "title",
      "summary_narrative",
      "visual_observations",
      "audio_source_characterization",
      "audio_transcript_summary",
  ):
    value = document.get(key)
    if isinstance(value, str):
      parts.append(value)
  for key in ("themes", "agencies", "top_terms"):
    value = document.get(key)
    if isinstance(value, list):
      parts.extend(str(item) for item in value if isinstance(item, str))
  return normalize_space(" ".join(parts)).lower()


def infer_document_capabilities(document: dict[str, object]) -> list[dict[str, object]]:
  text = document_capability_text(document)
  if not text:
    return []

  capabilities: list[dict[str, object]] = []
  for definition in CAPABILITY_DEFINITIONS:
    keywords = definition["keywords"]
    hits = [keyword for keyword in keywords if keyword in text]
    score = len(hits)
    if definition["key"] == "sensor_tracked" and "Sensors and Radar" in document.get("themes", []):
      score += 1
    if score <= 0:
      continue
    capabilities.append(
      {
        "key": definition["key"],
        "label": definition["label"],
        "group": definition["group"],
        "description": definition["description"],
        "score": score,
      }
    )

  return sorted(capabilities, key=lambda item: (-int(item["score"]), str(item["label"])))[:5]


def annotate_capabilities(document: dict[str, object]) -> dict[str, object]:
  annotated = dict(document)
  capabilities = infer_document_capabilities(annotated)
  annotated["capabilities"] = capabilities
  annotated["capability_labels"] = [str(capability["label"]) for capability in capabilities]
  annotated["capability_keys"] = [str(capability["key"]) for capability in capabilities]
  annotated["capability_profile"] = " + ".join(annotated["capability_labels"][:2]) if capabilities else "No capability label"
  return annotated


def build_capability_matrix(documents: list[dict[str, object]]) -> list[dict[str, object]]:
  matrix: list[dict[str, object]] = []
  for definition in CAPABILITY_DEFINITIONS:
    matching_documents = [
      document
      for document in documents
      if definition["key"] in document.get("capability_keys", [])
    ]
    if not matching_documents:
      continue
    evidence_counts = Counter(
      document.get("evidence_category")
      for document in matching_documents
      if isinstance(document.get("evidence_category"), str)
    )
    matrix.append(
      {
        "key": definition["key"],
        "label": definition["label"],
        "group": definition["group"],
        "description": definition["description"],
        "count": len(matching_documents),
        "category_counts": [
          {
            "label": evidence_definition["label"],
            "rank": evidence_definition["rank"],
            "count": evidence_counts.get(evidence_definition["label"], 0),
          }
          for evidence_definition in EVIDENCE_CATEGORY_DEFINITIONS
        ],
        "reviewed_count": sum(1 for document in matching_documents if document.get("review_status") == "reviewed"),
        "media_count": sum(1 for document in matching_documents if document.get("media_type") in MEDIA_TYPES),
      }
    )
  return sorted(matrix, key=lambda item: (-int(item["count"]), str(item["label"])))


def extract_json_object(text: str) -> dict[str, object] | None:
  candidate = text.strip()
  if not candidate:
    return None

  fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
  if fenced_match:
    candidate = fenced_match.group(1).strip()
  elif not candidate.startswith("{"):
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
      return None
    candidate = candidate[start : end + 1]

  try:
    payload = json.loads(candidate)
  except json.JSONDecodeError:
    return None
  return payload if isinstance(payload, dict) else None


@dataclass
class LocalModelReviewer:
  base_url: str
  model_name: str
  timeout_seconds: int
  max_images: int
  api_key: str | None = None
  audio_transcript_model: str | None = None
  audio_transcript_max_seconds: int = 120
  audio_transcript_timeout: int = 75
  audio_transcript_format: str = "wav"
  audio_transcription_disabled: bool = field(default=False, init=False)

  def _image_file_data_url(self, path: Path, max_dimension: int = 1280) -> str | None:
    try:
      with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((max_dimension, max_dimension))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=86, optimize=True)
    except (OSError, ValueError):
      return None
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"

  def _audio_file_data(self, path: Path, metadata: dict[str, object]) -> tuple[bytes, dict[str, object]] | None:
    audio_format = self.audio_transcript_format if self.audio_transcript_format in {"wav", "mp3"} else "wav"
    max_seconds = max(1, int(self.audio_transcript_max_seconds or 180))
    duration = metadata.get("duration_seconds")
    if isinstance(duration, str):
      try:
        duration = float(duration)
      except ValueError:
        duration = None

    with tempfile.TemporaryDirectory(prefix="ufo-audio-track-") as audio_dir:
      audio_path = Path(audio_dir) / f"audio.{audio_format}"
      command = ["ffmpeg", "-y"]
      if isinstance(duration, (int, float)) and duration > max_seconds:
        command.extend(["-t", str(max_seconds)])
      command.extend(["-i", str(path), "-vn", "-ac", "1", "-ar", "16000"])
      if audio_format == "mp3":
        command.extend(["-b:a", "64k"])
      command.append(str(audio_path))

      try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=max(60, max_seconds + 30),
        )
      except (FileNotFoundError, subprocess.SubprocessError, TimeoutError):
        return None

      try:
        audio_bytes = audio_path.read_bytes()
      except OSError:
        return None

    extraction_metadata: dict[str, object] = {
      "audio_transcript_format": audio_format,
      "audio_transcript_seconds": min(float(duration), max_seconds) if isinstance(duration, (int, float)) else max_seconds,
    }
    if isinstance(duration, (int, float)) and duration > max_seconds:
      extraction_metadata["audio_transcript_truncated"] = True
    return audio_bytes, extraction_metadata

  def _render_review_image(self, page: fitz.Page) -> str:
    pixmap = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2), alpha=False)
    encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
    return f"data:image/png;base64,{encoded}"

  def _extract_video_frame_data_urls(self, path: Path, metadata: dict[str, object]) -> list[str]:
    if self.max_images <= 0:
      return []

    duration = metadata.get("duration_seconds")
    if isinstance(duration, str):
      try:
        duration = float(duration)
      except ValueError:
        duration = None

    if isinstance(duration, (int, float)) and duration > 1:
      timestamps = [0.1, duration / 2, max(0.1, duration - 0.25)]
    else:
      timestamps = [0.1]

    frame_urls: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ufo-video-frames-") as frame_dir:
      for index, timestamp in enumerate(dict.fromkeys(round(value, 3) for value in timestamps)):
        if len(frame_urls) >= self.max_images:
          break
        frame_path = Path(frame_dir) / f"frame-{index}.jpg"
        try:
          subprocess.run(
              [
                  "ffmpeg",
                  "-y",
                  "-ss",
                  f"{timestamp:.3f}",
                  "-i",
                  str(path),
                  "-frames:v",
                  "1",
                  "-q:v",
                  "3",
                  str(frame_path),
              ],
              check=True,
              capture_output=True,
              text=True,
              timeout=30,
          )
        except (FileNotFoundError, subprocess.SubprocessError, TimeoutError):
          continue
        data_url = self._image_file_data_url(frame_path)
        if data_url:
          frame_urls.append(data_url)
    return frame_urls

  def _select_review_pages(self, doc: fitz.Document, document_type: str) -> list[int]:
    if doc.page_count == 0 or self.max_images <= 0:
      return []

    selected = [0]
    scan_limit = min(doc.page_count, 4)
    for page_index in range(scan_limit):
      if len(selected) >= self.max_images:
        break
      if page_index in selected:
        continue
      if document_type == "Imagery" or doc[page_index].get_images(full=True):
        selected.append(page_index)
    if document_type == "Imagery" and len(selected) < self.max_images and doc.page_count > 1:
      for page_index in range(1, min(doc.page_count, 3)):
        if page_index not in selected:
          selected.append(page_index)
        if len(selected) >= self.max_images:
          break
    return selected[: self.max_images]

  def _excerpt_review_page(self, page_text: str, max_chars: int = 900) -> str:
    text = normalize_space(page_text)
    if len(text) <= max_chars:
      return text

    lowered = text.lower()
    keyword_positions = [lowered.find(keyword) for keyword in REVIEW_FOCUS_KEYWORDS if keyword in lowered]
    if keyword_positions:
      start = max(0, min(keyword_positions) - max_chars // 3)
      end = min(len(text), start + max_chars)
      if end < len(text) and start > 0:
        start = max(0, end - max_chars)
    else:
      start = 0
      end = max_chars

    excerpt = text[start:end].strip()
    if start > 0:
      excerpt = f"…{excerpt}"
    if end < len(text):
      excerpt = f"{excerpt.rstrip()}…"
    return excerpt

  def _build_review_text_sample(self, combined_text: str, max_pages: int = 8) -> str:
    page_lines = [normalize_space(line) for line in combined_text.splitlines() if normalize_space(line)]
    if not page_lines:
      return "[no extracted text available]"

    keyword_ranked_pages: list[tuple[int, int]] = []
    for page_index, page_line in enumerate(page_lines):
      lowered = page_line.lower()
      hits = sum(lowered.count(keyword) for keyword in REVIEW_FOCUS_KEYWORDS)
      if hits:
        keyword_ranked_pages.append((hits, page_index))

    candidate_indices = [0]
    for _, page_index in sorted(keyword_ranked_pages, key=lambda item: (-item[0], item[1])):
      candidate_indices.append(page_index)

    if len(page_lines) > 2:
      candidate_indices.append(len(page_lines) // 2)
    if len(page_lines) > 4:
      candidate_indices.extend([len(page_lines) // 3, (2 * len(page_lines)) // 3])
    if len(page_lines) > 1:
      candidate_indices.append(len(page_lines) - 1)

    selected_indices: list[int] = []
    for page_index in candidate_indices:
      if page_index < 0 or page_index >= len(page_lines) or page_index in selected_indices:
        continue
      selected_indices.append(page_index)
      if len(selected_indices) >= max_pages:
        break

    selected_indices.sort()

    sampled_sections: list[str] = []
    for page_index in selected_indices:
      page_line = page_lines[page_index]
      match = re.match(r"^\[(Page\s+\d+)\]\s*(.*)$", page_line)
      if match:
        page_label = match.group(1)
        page_body = match.group(2)
      else:
        page_label = f"Page {page_index + 1}"
        page_body = page_line
      sampled_sections.append(f"[{page_label}] {self._excerpt_review_page(page_body)}")

    sampling_note = (
      "Systematic document sample: includes the opening page, broad coverage across the file, "
      "and pages with the strongest apparent UAP/UFO signals when present."
    )
    return f"{sampling_note}\n\n" + "\n\n".join(sampled_sections)

  def _chat(
      self,
      messages: list[dict[str, object]],
      max_tokens: int = 6000,
      timeout_seconds: int | None = None,
      response_format: dict[str, object] | None = None,
  ) -> dict[str, object]:
    payload = {
      "model": self.model_name,
      "messages": messages,
      "temperature": 0,
      "max_tokens": max_tokens,
    }
    if response_format is not None:
      payload["response_format"] = response_format
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if self.api_key:
      headers["Authorization"] = f"Bearer {self.api_key}"
    base_url = self.base_url.rstrip("/")
    if not base_url.endswith("/v1"):
      base_url = f"{base_url}/v1"
    request = urllib_request.Request(
      url=f"{base_url}/chat/completions",
      data=json.dumps(payload).encode("utf-8"),
      headers=headers,
      method="POST",
    )
    with urllib_request.urlopen(request, timeout=timeout_seconds or self.timeout_seconds) as response:
      return json.loads(response.read().decode("utf-8"))

  def _audio_transcription_request(
      self,
      audio_bytes: bytes,
      audio_format: str,
      model_name: str,
  ) -> dict[str, object] | None:
    base_url = self.base_url.rstrip("/")
    if not base_url.endswith("/v1"):
      base_url = f"{base_url}/v1"
    boundary = f"----ufo-audio-{hashlib.sha1(audio_bytes[:1024] + model_name.encode('utf-8')).hexdigest()}"
    filename = f"audio.{audio_format}"
    content_type = "audio/mpeg" if audio_format == "mp3" else "audio/wav"

    body = b"".join(
      [
        f"--{boundary}\r\n".encode("utf-8"),
        b'Content-Disposition: form-data; name="model"\r\n\r\n',
        model_name.encode("utf-8"),
        b"\r\n",
        f"--{boundary}\r\n".encode("utf-8"),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
        audio_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
      ]
    )
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if self.api_key:
      headers["Authorization"] = f"Bearer {self.api_key}"
    request = urllib_request.Request(
      url=f"{base_url}/audio/transcriptions",
      data=body,
      headers=headers,
      method="POST",
    )
    with urllib_request.urlopen(request, timeout=max(15, int(self.audio_transcript_timeout or 75))) as response:
      return json.loads(response.read().decode("utf-8"))

  def _parse_transcript_response(self, response: dict[str, object]) -> str | None:
    choices = response.get("choices") or []
    if not choices:
      return None
    message = choices[0].get("message") or {}
    content_text = message.get("content")
    reasoning_text = message.get("reasoning_content")
    combined_response = normalize_space(" ".join(text for text in (content_text, reasoning_text) if isinstance(text, str))).lower()
    if any(pattern in combined_response for pattern in AUDIO_TRANSCRIPT_FAILURE_PATTERNS):
      self.audio_transcription_disabled = True
      return None

    transcript = ""
    payload = extract_json_object(content_text) if isinstance(content_text, str) else None
    if not payload and isinstance(reasoning_text, str):
      payload = extract_json_object(reasoning_text)
    if payload:
      candidate = payload.get("transcript")
      if isinstance(candidate, str):
        transcript = candidate
    elif isinstance(content_text, str):
      transcript = content_text

    transcript = clean_transcript_text(transcript)
    if not transcript:
      return None
    if any(pattern in transcript.lower() for pattern in AUDIO_TRANSCRIPT_FAILURE_PATTERNS):
      self.audio_transcription_disabled = True
      return None
    return transcript

  def transcribe_audio_track(self, path: Path, metadata: dict[str, object]) -> tuple[str, dict[str, object]]:
    if self.audio_transcription_disabled:
      return "", {}

    audio_data = self._audio_file_data(path, metadata)
    if audio_data is None:
      return "", {}
    audio_bytes, extraction_metadata = audio_data
    audio_format = str(extraction_metadata["audio_transcript_format"])
    model_name = self.audio_transcript_model or self.model_name
    try:
      transcription_response = self._audio_transcription_request(audio_bytes, audio_format, model_name)
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError):
      transcription_response = None
    if transcription_response:
      transcript = clean_transcript_text(str(transcription_response.get("text") or ""))
      if (
          transcript
          and is_meaningful_asr_transcript(transcript)
          and not any(pattern in transcript.lower() for pattern in AUDIO_TRANSCRIPT_FAILURE_PATTERNS)
      ):
        metadata_out = dict(extraction_metadata)
        metadata_out.update(
          {
            "transcript_source": "asr_audio",
            "transcript_model": model_name,
            "transcript_characters": len(transcript),
          }
        )
        language = transcription_response.get("language")
        if isinstance(language, str) and normalize_space(language):
          metadata_out["transcript_language"] = normalize_space(language)
        return transcript, metadata_out
      return "", {
        **extraction_metadata,
        "transcript_source": "asr_audio",
        "transcript_model": model_name,
        "transcript_status": "no_useful_transcript",
        "transcript_characters": 0,
      }

    encoded_audio = base64.b64encode(audio_bytes).decode("ascii")
    prompt_text = (
      "Transcribe the spoken audio in this UFO/UAP media asset. "
      "Return only a JSON object with one key, transcript. "
      "Include intelligible radio calls, narration, cockpit audio, labels spoken aloud, and meaningful non-speech cues in brackets. "
      "If there is no intelligible speech or audio content, return {\"transcript\":\"\"}."
    )
    data_url = f"data:audio/{'mpeg' if audio_format == 'mp3' else 'wav'};base64,{encoded_audio}"
    content_variants = (
      [
        {"type": "text", "text": prompt_text},
        {"type": "input_audio", "input_audio": {"data": encoded_audio, "format": audio_format}},
      ],
      [
        {"type": "text", "text": prompt_text},
        {"type": "audio_url", "audio_url": {"url": data_url}},
      ],
    )

    original_model = self.model_name
    try:
      self.model_name = model_name
      for content in content_variants:
        try:
          response = self._chat(
            [{"role": "user", "content": content}],
            max_tokens=4000,
            timeout_seconds=max(15, int(self.audio_transcript_timeout or 75)),
          )
        except (urllib_error.URLError, TimeoutError, json.JSONDecodeError):
          self.audio_transcription_disabled = True
          return "", {}
        transcript = self._parse_transcript_response(response)
        if transcript:
          metadata_out = dict(extraction_metadata)
          metadata_out.update(
            {
              "transcript_source": "llm_audio",
              "transcript_model": model_name,
              "transcript_characters": len(transcript),
            }
          )
          return transcript, metadata_out
        if self.audio_transcription_disabled:
          return "", {}
    finally:
      self.model_name = original_model

    return "", {}

  def _parse_review_response(self, response: dict[str, object], resolver: "PlaceResolver") -> dict[str, object] | None:
    choices = response.get("choices") or []
    if not choices:
      return None
    message = choices[0].get("message") or {}
    content_text = message.get("content")
    payload = extract_json_object(content_text) if isinstance(content_text, str) else None
    if not payload:
      reasoning_text = message.get("reasoning_content")
      payload = extract_json_object(reasoning_text) if isinstance(reasoning_text, str) else None
    if not payload:
      return None

    review: dict[str, object] = {
      "review_status": "reviewed",
      "review_source": f"local_model:{self.model_name}",
    }

    summary = payload.get("summary_narrative")
    if isinstance(summary, str) and normalize_space(summary):
      review["summary_narrative"] = normalize_space(summary)

    visual = payload.get("visual_observations")
    if isinstance(visual, str) and normalize_space(visual):
      review["visual_observations"] = normalize_space(visual)
    else:
      review["visual_observations"] = None

    model_doc_type = payload.get("document_type")
    if isinstance(model_doc_type, str) and model_doc_type in DOCUMENT_TYPE_LABELS:
      review["document_type"] = model_doc_type

    model_themes = payload.get("themes")
    if isinstance(model_themes, list):
      review["themes"] = [theme for theme in model_themes if isinstance(theme, str) and theme in THEME_KEYWORDS]

    model_agencies = payload.get("agencies")
    if isinstance(model_agencies, list):
      cleaned_agencies = [normalize_space(item) for item in model_agencies if isinstance(item, str) and normalize_space(item)]
      review["agencies"] = list(dict.fromkeys(cleaned_agencies))[:8]

    model_evidence_category = normalize_evidence_category(payload.get("evidence_category"))
    if model_evidence_category is not None:
      review["evidence_category"] = model_evidence_category["label"]

    review.update(normalize_trend_review_fields(payload))

    audio_source = payload.get("audio_source_characterization")
    if isinstance(audio_source, str) and normalize_space(audio_source):
      review["audio_source_characterization"] = normalize_space(audio_source)

    audio_summary = payload.get("audio_transcript_summary")
    if isinstance(audio_summary, str) and normalize_space(audio_summary):
      review["audio_transcript_summary"] = normalize_space(audio_summary)

    location_label = payload.get("location_label")
    if isinstance(location_label, str) and normalize_space(location_label):
      resolved = resolver.resolve(location_label)
      if resolved:
        review["location"] = {
          "label": resolved.label,
          "latitude": resolved.latitude,
          "longitude": resolved.longitude,
          "match": resolved.match,
          "kind": resolved.kind,
        }

    return review if "summary_narrative" in review else None

  def review_document(
    self,
    document: dict[str, object],
    doc: fitz.Document,
    combined_text: str,
    resolver: "PlaceResolver",
  ) -> dict[str, object] | None:
    text_slice = self._build_review_text_sample(combined_text)
    attach_images = (
      document.get("document_type") == "Imagery"
      or int(document.get("text_characters") or 0) < 1200
      or any(doc[index].get_images(full=True) for index in range(min(doc.page_count, 3)))
    )

    prompt_text = (
      "You are reviewing a UFO/UAP archive document for a research dashboard. "
      "The extracted text may contain OCR noise. Use the text and any attached page images to infer what is actually present. "
      "Keep the document's general overview, but prioritize identifying any concrete UAP/UFO activity, sightings, incidents, or explicit lack of such activity. "
      "If the document is large or compiled from multiple items, synthesize systematically from the full sample instead of over-weighting the opening page. "
      "Return only a JSON object with these keys: summary_narrative, visual_observations, document_type, themes, agencies, location_label, evidence_category, "
      f"{', '.join(TREND_FIELD_NAMES)}. "
      f"document_type must be one of {sorted(DOCUMENT_TYPE_LABELS)}. "
      f"themes must be chosen only from {sorted(THEME_KEYWORDS)}. "
      f"evidence_category must be one of {[definition['label'] for definition in EVIDENCE_CATEGORY_DEFINITIONS]}. "
      "Use these category rules exactly: Category One is for reports with plenty of event detail, eyewitness depth, and at least one form of hard evidence showing that something legitimate happened. "
      "Category Two is for events with some supporting evidence but not enough to justify Category One. "
      "Category Three is for reports with little meaningful evidence or very sparse eyewitness support. "
      "agencies should be a short array of specific organizations supported by the material, not guesses. "
      "location_label should be a short place name if the material makes one reasonably clear, otherwise null. "
      "summary_narrative must be 4 sentences covering who, the document's overall subject, where, and significance. "
      "Within those 4 sentences, explicitly state what UAP/UFO activity is described, where in the document it appears if that can be inferred from the sampled pages or sections, and say clearly when no actual UAP/UFO activity is present. "
      "visual_observations should describe only visible imagery or scene evidence and be null when there is no meaningful visual evidence beyond text formatting.\n\n"
      f"{trend_review_prompt_instructions()}\n\n"
      f"Filename: {document['filename']}\n"
      f"Title: {document['title']}\n"
      f"Current document type hint: {document.get('document_type')}\n"
      f"Current date label hint: {document.get('date_label')}\n"
      f"Current location hint: {document.get('location', {}).get('label') if document.get('location') else None}\n"
      f"Current theme hints: {document.get('themes', [])}\n"
      f"Current agency hints: {document.get('agencies', [])}\n"
      f"Systematic extracted text sample:\n{text_slice or '[no extracted text available]'}"
    )

    content: str | list[dict[str, object]] = prompt_text

    if attach_images:
      content = [{"type": "text", "text": prompt_text}]
      for page_index in self._select_review_pages(doc, str(document.get("document_type") or "Research File")):
        content.append(
          {
            "type": "image_url",
            "image_url": {"url": self._render_review_image(doc[page_index])},
          }
        )

    try:
      response = self._chat(
        [
          {
            "role": "system",
            "content": "You are a JSON API. Return only the requested JSON object. Do not include reasoning, markdown, or explanatory text.",
          },
          {"role": "user", "content": content},
        ],
        response_format={"type": "json_object"},
      )
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError):
      return None

    return self._parse_review_response(response, resolver)

  def review_media_item(
    self,
    document: dict[str, object],
    path: Path,
    media_metadata: dict[str, object],
    manifest_narrative: str,
    extracted_text: str,
    resolver: "PlaceResolver",
  ) -> dict[str, object] | None:
    media_type = str(document.get("media_type") or "media")
    prompt_text = (
      "You are reviewing a UFO/UAP archive media asset for a research dashboard. "
      "Use the attached image or sampled video frames as primary evidence when present, and use transcript text as the primary evidence for audio-only assets when provided. "
      "If an audio-only asset has no transcript, use manifest text only as source context. "
      "For videos, the attached frames are samples from the clip; describe visible objects, sensor overlays, scene context, apparent motion cues only when the frames support them, and any uncertainty. "
      "When transcript text is provided, use it as evidence for audible narration, cockpit audio, captions, or spoken context, while still separating what is visible from what is spoken. "
      "Do not invent conclusions about identity, speed, altitude, intent, or authenticity. "
      "Return only a JSON object with these keys: summary_narrative, visual_observations, document_type, themes, agencies, location_label, evidence_category, audio_source_characterization, audio_transcript_summary, "
      f"{', '.join(TREND_FIELD_NAMES)}. "
      f"document_type must be one of {sorted(DOCUMENT_TYPE_LABELS)}. "
      f"themes must be chosen only from {sorted(THEME_KEYWORDS)}. "
      f"evidence_category must be one of {[definition['label'] for definition in EVIDENCE_CATEGORY_DEFINITIONS]}. "
      "summary_narrative must be 4 sentences covering who or source context, what is visible or audible in the asset, where, and significance. "
      "visual_observations must be a concrete visual narrative of what is actually visible in the attached media, not a restatement of the manifest. "
      "audio_source_characterization must be a short LLM-generated phrase describing the actual kind of audio source, such as mission radio transmission, cockpit intercom, recorded interview, press briefing narration, or archival mission audio; use null when no transcript text is provided. "
      "audio_transcript_summary must be a concise LLM-generated summary of what the transcript says, not a verbatim quote; use null when no transcript text is provided. "
      "If the image or frames do not show a meaningful anomalous object, say so clearly.\n\n"
      f"{trend_review_prompt_instructions()}\n\n"
      f"Filename: {document['filename']}\n"
      f"Title: {document['title']}\n"
      f"Media type: {media_type}\n"
      f"Current date label hint: {document.get('date_label')}\n"
      f"Current location hint: {document.get('location', {}).get('label') if document.get('location') else None}\n"
      f"Current theme hints: {document.get('themes', [])}\n"
      f"Current agency hints: {document.get('agencies', [])}\n"
      f"Local media metadata: {json.dumps(media_metadata, ensure_ascii=False)}\n"
      f"Manifest narrative/context: {manifest_narrative or '[no manifest narrative available]'}\n"
      f"OCR/transcript text extracted from the media: {extracted_text or '[no text extracted from media]'}"
    )

    image_urls: list[str] = []
    if media_type == "image":
      data_url = self._image_file_data_url(path)
      if data_url:
        image_urls.append(data_url)
    elif media_type == "video":
      image_urls.extend(self._extract_video_frame_data_urls(path, media_metadata))

    if not image_urls and media_type != "audio":
      return None

    content: list[dict[str, object]] = [{"type": "text", "text": prompt_text}]
    for data_url in image_urls[: self.max_images]:
      content.append({"type": "image_url", "image_url": {"url": data_url}})

    try:
      response = self._chat(
        [
          {
            "role": "system",
            "content": "You are a JSON API. Return only the requested JSON object. Do not include reasoning, markdown, or explanatory text.",
          },
          {"role": "user", "content": content},
        ],
        response_format={"type": "json_object"},
      )
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError):
      return None
    return self._parse_review_response(response, resolver)

  def review_audio_transcript_context(
      self,
      document: dict[str, object],
      manifest_narrative: str,
      transcript_text: str,
  ) -> dict[str, object] | None:
    transcript_sample = choose_excerpt(transcript_text, max_length=2500)
    manifest_sample = choose_excerpt(manifest_narrative, max_length=1000)
    prompt_text = (
      "You are preparing an audio context note for a UFO/UAP research dashboard. "
      "Use the title, manifest context, and transcript text to infer the actual kind of audio source and summarize what the audio transcript says. "
      "Do not quote the transcript verbatim. Do not mention cache files, extraction methods, model names, or implementation details. "
      "Return only a JSON object with exactly these keys: audio_source_characterization, audio_transcript_summary. "
      "audio_source_characterization must be a short noun phrase, for example mission radio transmission, cockpit intercom audio, recorded interview, crew debriefing audio, or archival mission audio. "
      "audio_transcript_summary must be 1 to 2 concise sentences summarizing the transcript's substance and relevance.\n\n"
      f"Filename: {document['filename']}\n"
      f"Title: {document['title']}\n"
      f"Media type: {document.get('media_type')}\n"
      f"Document type: {document.get('document_type')}\n"
      f"Manifest context: {manifest_sample or '[no manifest context available]'}\n"
      f"Transcript text: {transcript_sample or '[no transcript text available]'}"
    )
    try:
      response = self._chat(
        [
          {
            "role": "system",
            "content": "You are a JSON API. Return only the requested JSON object. Do not include reasoning, markdown, or explanatory text.",
          },
          {"role": "user", "content": prompt_text},
        ],
        max_tokens=2400,
        timeout_seconds=min(self.timeout_seconds, 180),
        response_format={"type": "json_object"},
      )
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError):
      return None

    choices = response.get("choices") or []
    if not choices:
      return None
    message = choices[0].get("message") or {}
    content_text = message.get("content")
    if not isinstance(content_text, str):
      return None
    content_text = content_text.strip()
    if not content_text.startswith("{"):
      return None
    try:
      payload = json.loads(content_text)
    except json.JSONDecodeError:
      return None
    if not isinstance(payload, dict):
      return None

    audio_source = payload.get("audio_source_characterization")
    audio_summary = payload.get("audio_transcript_summary")
    if not isinstance(audio_source, str) or not isinstance(audio_summary, str):
      return None
    audio_source = normalize_space(audio_source)
    audio_summary = normalize_space(audio_summary)
    if not audio_source or not audio_summary:
      return None
    return {
      "audio_source_characterization": audio_source,
      "audio_transcript_summary": audio_summary,
    }

  def review_corpus(self, documents: list[dict[str, object]]) -> dict[str, object] | None:
    summary_blocks: list[str] = []
    standout_sources = select_standout_category_one_sources(documents, limit=3)
    standout_blocks: list[str] = []
    for index, document in enumerate(standout_sources, start=1):
      themes = ", ".join(str(theme) for theme in document.get("themes", []) if isinstance(theme, str))
      location = document.get("location")
      location_label = location.get("label") if isinstance(location, dict) else None
      evidence_terms = extract_standout_evidence_terms(document)
      standout_blocks.append(
        "\n".join(
          [
            f"{index}. {document.get('title')}",
            f"Year: {document.get('year') or document.get('year_start') or 'unknown'}",
            f"Type: {document.get('document_type')}",
            f"Location: {location_label or 'unknown'}",
            f"Themes: {themes or 'none'}",
            f"Why it stands out: {', '.join(evidence_terms) if evidence_terms else 'Category One source with unusually detailed reporting.'}",
            f"Summary: {normalize_space(str(document.get('summary_narrative') or ''))}",
          ]
        )
      )
    for index, document in enumerate(documents, start=1):
      summary = normalize_space(str(document.get("summary_narrative") or ""))
      if not summary:
        continue
      themes = ", ".join(str(theme) for theme in document.get("themes", []) if isinstance(theme, str))
      location = document.get("location")
      location_label = location.get("label") if isinstance(location, dict) else None
      summary_blocks.append(
        "\n".join(
          [
            f"{index}. {document.get('title')}",
            f"Year: {document.get('year') or document.get('year_start') or 'unknown'}",
            f"Type: {document.get('document_type')}",
            f"Evidence category: {document.get('evidence_category') or 'unclassified'}",
            f"Location: {location_label or 'unknown'}",
            f"Themes: {themes or 'none'}",
            f"Summary: {summary}",
          ]
        )
      )

    if not summary_blocks:
      return None

    prompt_text = (
      "You are writing the Executive Summary for a UFO/UAP research dashboard. "
      "Use every document summary below as source material, and synthesize across the whole corpus rather than retelling one document at a time. "
      "Build a careful narrative around what the data might mean: historical development, institutional behavior, geography, evidence quality, recurring observation patterns, and outliers. "
      "Call out specific elements that stand out, including document titles or event clusters when useful. "
      "You must explicitly include the three supplied Standout Category One sources as the strongest high-detail/high-evidence examples, naming each source title and why it matters. "
      "Do not overstate certainty; distinguish concrete patterns in the summaries from interpretation. "
      "Return only a JSON object with key executive_summary_sections. "
      "executive_summary_sections must be an array of 3 to 5 polished paragraphs, each 2 to 4 sentences. "
      "Make one paragraph focus on the three Standout Category One sources. "
      "Avoid bullets and avoid generic caveats.\n\n"
      "Standout Category One sources:\n"
      + ("\n\n".join(standout_blocks) if standout_blocks else "[none selected]")
      + "\n\n"
      "Document summaries:\n"
      + "\n\n".join(summary_blocks)
    )

    try:
      response = self._chat([{"role": "user", "content": prompt_text}], max_tokens=6000)
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError):
      return None

    choices = response.get("choices") or []
    if not choices:
      return None
    message = choices[0].get("message") or {}
    content_text = message.get("content")
    payload = extract_json_object(content_text) if isinstance(content_text, str) else None
    if not payload:
      reasoning_text = message.get("reasoning_content")
      payload = extract_json_object(reasoning_text) if isinstance(reasoning_text, str) else None
    if not payload:
      return None

    raw_sections = payload.get("executive_summary_sections")
    sections: list[str] = []
    if isinstance(raw_sections, list):
      sections = [normalize_space(section) for section in raw_sections if isinstance(section, str) and normalize_space(section)]
    elif isinstance(payload.get("executive_summary"), str):
      sections = [normalize_space(payload["executive_summary"])]

    if not sections:
      return None

    return {
      "review_status": "reviewed",
      "review_source": f"local_model:{self.model_name}",
      "executive_summary_sections": sections[:5],
    }


def tokenize(text: str) -> list[str]:
    tokens = []
    for match in TOKEN_PATTERN.finditer(text.lower()):
        token = match.group(0).strip("-/'")
        if len(token) < 3 or token in STOPWORDS or token.isdigit():
            continue
        tokens.append(token)
    return tokens


def infer_document_type(title: str) -> str:
    lowered = title.lower()
    for label, needles in DOCUMENT_TYPE_PATTERNS:
        if any(needle in lowered for needle in needles):
            return label
    if "uap" in lowered or "ufo" in lowered:
        return "UAP Report"
    return "Research File"


def extract_year_info(
    text: str,
    minimum_year: int = 1930,
    maximum_year: int | None = None,
) -> tuple[int | None, int | None, int | None]:
    upper_bound = maximum_year or (datetime.utcnow().year + 1)
    years = sorted(
        {
            int(match.group(1))
            for match in YEAR_PATTERN.finditer(text)
            if minimum_year <= int(match.group(1)) <= upper_bound
        }
    )
    if not years:
        return None, None, None
    return years[0], years[-1], years[0]


def extract_date_label(text: str) -> str | None:
    for pattern in (MONTH_PATTERN, NUMERIC_DATE_PATTERN):
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(0)
        try:
            parsed = date_parser.parse(raw, fuzzy=True)
        except (ValueError, OverflowError):
            return raw
        if pattern is MONTH_PATTERN and parsed.day == 1 and "," not in raw:
            return parsed.strftime("%b %Y")
        return parsed.strftime("%b %d, %Y")
    return None


def parse_manifest_date_label(value: str) -> str | None:
    cleaned = normalize_space(value)
    if not cleaned or cleaned.upper() == "N/A":
        return None
    if re.fullmatch(r"(?:early|mid|late)\s+(19[0-9]{2}|20[0-2][0-9])", cleaned, re.IGNORECASE):
        return re.search(r"(19[0-9]{2}|20[0-2][0-9])", cleaned).group(1)
    if re.fullmatch(
        r"(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+"
        r"(?:19[0-9]{2}|20[0-2][0-9])",
        cleaned,
        re.IGNORECASE,
    ):
        parsed_month = date_parser.parse(cleaned, fuzzy=True)
        return parsed_month.strftime("%b %Y")
    try:
        parsed = date_parser.parse(cleaned, fuzzy=True)
    except (ValueError, OverflowError):
        return extract_date_label(cleaned)
    if parsed.year > datetime.utcnow().year + 1:
        parsed = parsed.replace(year=parsed.year - 100)
    if re.fullmatch(r"\d{4}", cleaned):
        return str(parsed.year)
    if parsed.day == 1 and not re.search(r"\b\d{1,2}\b", cleaned.replace(str(parsed.year), "")):
        return parsed.strftime("%b %Y")
    return parsed.strftime("%b %d, %Y")


def choose_excerpt(text: str, max_length: int = 320) -> str:
    if not text:
        return ""
    lines = []
    for raw_line in text.splitlines():
        line = normalize_space(raw_line)
        if len(line) < 30:
            continue
        if line.lower().startswith("page "):
            continue
        lines.append(line)
        if len(" ".join(lines)) >= max_length:
            break
    excerpt = " ".join(lines)
    if not excerpt:
        excerpt = normalize_space(text[:max_length])
    if len(excerpt) > max_length:
        excerpt = excerpt[: max_length - 1].rstrip() + "…"
    return excerpt


def join_labels(items: Iterable[str], limit: int = 3) -> str:
    values = [normalize_space(item) for item in items if item]
    values = list(dict.fromkeys(values))[:limit]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def infer_who_statement(document_type: str, agencies: list[str], themes: list[str]) -> str:
    if agencies:
        return f"Who: {join_labels(agencies)} personnel are the clearest reporting or institutional actors tied to this file."

    fallback_by_type = {
        "Mission Report": "Who: operational aircrews, command staff, and reporting personnel appear to be the primary actors in this record.",
        "Incident Summary": "Who: witnesses, reporting officers, and follow-on investigators appear to be the main actors in this case file.",
        "Transcript": "Who: crew members and mission support personnel are the central voices in this transcript.",
        "Video": "Who: aircrews, sensor operators, analysts, or mission personnel are the likely actors connected to this video item.",
        "Audio": "Who: crew members, mission support personnel, or narrators are the central voices connected to this audio item.",
        "Debrief / Reporting Form": "Who: pilots, range personnel, and debriefing officers appear to be the main actors in this file.",
        "Diplomatic Cable": "Who: diplomatic staff and government reporting channels are the main actors in this record.",
        "Email Correspondence": "Who: program staff and correspondence authors are the clearest actors in this exchange.",
        "Imagery": "Who: witnesses, photographers, or visual analysts appear to be the main actors associated with this item.",
        "Statement": "Who: a named witness or official source appears to be the principal voice in this statement.",
        "Archival File": "Who: archival intelligence or investigative staff appear to be the most likely actors in this file.",
        "UAP Report": "Who: witnesses and reporting analysts appear to be the main actors in this report.",
    }
    if document_type in fallback_by_type:
        return fallback_by_type[document_type]
    if themes:
        return f"Who: actors connected to {themes[0].lower()} appear to be central to this document."
    return "Who: the extracted text does not clearly identify named actors, but the file still preserves a reporting chain or witness context."


def infer_significance_statement(
    document_type: str,
    themes: list[str],
    location: ResolvedLocation | None,
    extraction_method: str,
) -> str:
    significance_clauses: list[str] = []
    if document_type == "Mission Report":
        significance_clauses.append("it connects anomalous reporting to operational flight activity")
    if document_type == "Incident Summary":
        significance_clauses.append("it gives researchers a comparable case summary for cross-incident pattern matching")
    if document_type == "Transcript":
        significance_clauses.append("it preserves near-contemporaneous testimony rather than later retellings")
    if document_type in {"Video", "Audio"}:
        significance_clauses.append("it preserves source media that can be compared against written reporting and witness descriptions")
    if document_type == "Diplomatic Cable":
        significance_clauses.append("it shows how unusual observations moved through official diplomatic channels")
    if document_type == "Debrief / Reporting Form":
        significance_clauses.append("it captures reporting structure close to the original observation window")
    if document_type == "Imagery":
        significance_clauses.append("it may provide a visual comparison point for object descriptions and witness claims")

    if "Sensors and Radar" in themes:
        significance_clauses.append("it may support comparisons between witness accounts and instrumented detections")
    if "Aerospace Operations" in themes:
        significance_clauses.append("it helps place the event in an operational aviation context")
    if "Diplomatic and Intel" in themes:
        significance_clauses.append("it broadens the archival view beyond military-only reporting")
    if "Spaceflight" in themes:
        significance_clauses.append("it is relevant for comparing anomalous observations across crewed spaceflight records")
    if "Imagery and Visuals" in themes:
        significance_clauses.append("it matters for comparing visual evidence, sketches, and image-derived interpretations")
    if location and location.kind in {"region", "site"}:
        significance_clauses.append(f"it strengthens geographic clustering around {location.label}")

    significance_clauses = list(dict.fromkeys(significance_clauses))
    if not significance_clauses:
        significance_clauses.append("it adds another comparable data point to the wider UFO and UAP archival record")

    significance = join_labels(significance_clauses, limit=4)
    sentence = f"Significance: Researchers may care about this file because {significance}."
    if extraction_method in {"ocr", "hybrid"}:
        sentence += " The wording should still be checked against the source PDF because at least part of the text was recovered through OCR."
    return sentence


def build_summary_narrative(
    document_type: str,
    date_label: str | None,
    location: ResolvedLocation | None,
    agencies: list[str],
    themes: list[str],
    top_terms: list[str],
    extraction_method: str,
    observation: str,
) -> str:
    cleaned_observation = re.sub(r"\[Page\s+\d+\]\s*", "", observation).strip()
    if len(cleaned_observation) > 220:
        cleaned_observation = cleaned_observation[:219].rstrip() + "…"

    who_statement = infer_who_statement(document_type, agencies, themes)
    date_prefix = f" on {date_label}" if date_label else ""

    if cleaned_observation:
        lowered_observation = cleaned_observation[0].lower() + cleaned_observation[1:]
        what_statement = f"What: This {document_type.lower()}{date_prefix} records {lowered_observation}."
    elif top_terms:
        focus = join_labels([term.replace('-', ' ') for term in top_terms[:4]], limit=4).lower()
        what_statement = f"What: This {document_type.lower()}{date_prefix} centers on {focus}."
    else:
        what_statement = f"What: This {document_type.lower()}{date_prefix} documents an anomalous observation or reporting sequence preserved in the archive."

    if location:
        where_statement = f"Where: The strongest location signal resolves to {location.label}."
    else:
        where_statement = "Where: No reliable location could be resolved from the title or extracted text."

    significance_statement = infer_significance_statement(document_type, themes, location, extraction_method)
    return " ".join([who_statement, what_statement, where_statement, significance_statement])


def detect_themes(text: str) -> list[str]:
    lowered = text.lower()
    themes = []
    for theme, terms in THEME_KEYWORDS.items():
        if any(term in lowered for term in terms):
            themes.append(theme)
    return themes


def detect_agencies(text: str) -> list[str]:
    lowered = text.lower()
    agencies = []
    for agency, needles in AGENCY_PATTERNS.items():
        if any(needle in lowered for needle in needles):
            agencies.append(agency)
    return agencies


def ocr_page(page: fitz.Page, dpi: int) -> str:
    scale = dpi / 72
    matrix = fitz.Matrix(scale, scale)
    pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csGRAY, alpha=False)
    image = Image.open(io.BytesIO(pixmap.tobytes("png")))
    return pytesseract.image_to_string(image, config="--psm 6")


def ocr_image(path: Path) -> tuple[str, dict[str, object]]:
  with Image.open(path) as image:
    metadata = {
        "image_width": image.width,
        "image_height": image.height,
        "image_mode": image.mode,
    }
    ocr_text = normalize_space(pytesseract.image_to_string(image.convert("RGB"), config="--psm 6"))
  return ocr_text, metadata


def probe_video(path: Path) -> dict[str, object]:
  try:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout)
  except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError, TimeoutError):
    return {}

  metadata: dict[str, object] = {}
  streams = payload.get("streams")
  if isinstance(streams, list):
    video_stream = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"), None)
    if isinstance(video_stream, dict):
      metadata["video_width"] = video_stream.get("width")
      metadata["video_height"] = video_stream.get("height")
      metadata["video_codec"] = video_stream.get("codec_name")
    if isinstance(audio_stream, dict):
      metadata["audio_codec"] = audio_stream.get("codec_name")

  media_format = payload.get("format")
  if isinstance(media_format, dict):
    duration = media_format.get("duration")
    if duration is not None:
      try:
        metadata["duration_seconds"] = float(duration)
      except (TypeError, ValueError):
        pass
    size = media_format.get("size")
    if size is not None:
      try:
        metadata["media_size_bytes"] = int(size)
      except (TypeError, ValueError):
        pass

  return metadata


def transcript_cache_path(cache_dir: Path, path: Path) -> Path:
  return cache_dir / f"{path.stem}.txt"


def transcript_metadata_cache_path(cache_dir: Path, path: Path) -> Path:
  return cache_dir / f"{path.stem}.json"


def clean_transcript_text(transcript_text: str) -> str:
  lines: list[str] = []
  for raw_line in transcript_text.replace("\ufeff", "").splitlines():
    line = normalize_space(raw_line)
    if line:
      lines.append(line)
  return normalize_space(" ".join(lines))


def is_meaningful_asr_transcript(transcript_text: str) -> bool:
  if cleaned_char_count(transcript_text) < MIN_ASR_TRANSCRIPT_ALNUM_CHARS:
    return False
  lowered = normalize_space(transcript_text.lower())
  if lowered in {"the.", "the", "uh", "um", "hmm"}:
    return False
  return True


def read_cached_transcript(cache_dir: Path, path: Path) -> tuple[str, dict[str, object]]:
  cache_path = transcript_cache_path(cache_dir, path)
  if not cache_path.exists():
    return "", {}

  transcript_text = clean_transcript_text(cache_path.read_text(encoding="utf-8"))
  if not transcript_text:
    return "", {}

  metadata: dict[str, object] = {
    "transcript_source": "transcript_cache",
    "transcript_characters": len(transcript_text),
  }
  metadata_path = transcript_metadata_cache_path(cache_dir, path)
  if metadata_path.exists():
    try:
      cached_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
      cached_metadata = None
    if isinstance(cached_metadata, dict):
      metadata.update(cached_metadata)
      metadata["transcript_source"] = f"{metadata.get('transcript_source', 'transcript')}_cache"
  return transcript_text, metadata


def read_transcript_metadata(cache_dir: Path, path: Path) -> dict[str, object]:
  metadata_path = transcript_metadata_cache_path(cache_dir, path)
  if not metadata_path.exists():
    return {}
  try:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
  except (json.JSONDecodeError, OSError):
    return {}
  return metadata if isinstance(metadata, dict) else {}


def should_skip_asr_transcript(cache_dir: Path, path: Path, model_name: str) -> bool:
  metadata = read_transcript_metadata(cache_dir, path)
  return (
    metadata.get("transcript_status") == "no_useful_transcript"
    and metadata.get("transcript_model") == model_name
  )


def write_transcript_metadata(cache_dir: Path, path: Path, metadata: dict[str, object]) -> None:
  cache_dir.mkdir(parents=True, exist_ok=True)
  transcript_metadata_cache_path(cache_dir, path).write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")


def write_cached_transcript(cache_dir: Path, path: Path, transcript_text: str, metadata: dict[str, object]) -> None:
  transcript_text = clean_transcript_text(transcript_text)
  if not transcript_text:
    return
  cache_dir.mkdir(parents=True, exist_ok=True)
  transcript_cache_path(cache_dir, path).write_text(transcript_text, encoding="utf-8")
  metadata_payload = dict(metadata)
  metadata_payload["transcript_characters"] = len(transcript_text)
  write_transcript_metadata(cache_dir, path, metadata_payload)


def closed_caption_url(source_metadata: dict[str, object]) -> str | None:
  urls = source_metadata.get("closed_caption_urls")
  if isinstance(urls, dict):
    for key in ("srt", "webvtt"):
      value = urls.get(key)
      if isinstance(value, str) and normalize_space(value):
        return value
  return None


def clean_caption_text(caption_text: str) -> str:
  lines: list[str] = []
  for raw_line in caption_text.replace("\ufeff", "").splitlines():
    line = normalize_space(raw_line)
    if not line:
      continue
    if line.upper() == "WEBVTT" or line.upper().startswith("NOTE"):
      continue
    if line.isdigit():
      continue
    if "-->" in line:
      continue
    line = re.sub(r"<[^>]+>", " ", line)
    line = normalize_space(line)
    if line:
      lines.append(line)

  deduped: list[str] = []
  for line in lines:
    if deduped and deduped[-1] == line:
      continue
    deduped.append(line)
  return normalize_space(" ".join(deduped))


def fetch_media_transcript(
    path: Path,
    source_metadata: dict[str, object],
    transcript_cache_dir: Path,
) -> tuple[str, dict[str, object]]:
  cached_transcript, cached_metadata = read_cached_transcript(transcript_cache_dir, path)
  if cached_transcript:
    return cached_transcript, cached_metadata

  caption_url = closed_caption_url(source_metadata)
  if not caption_url:
    return "", {}

  try:
    with urllib_request.urlopen(caption_url, timeout=120) as response:
      caption_text = response.read().decode("utf-8-sig", errors="replace")
  except (urllib_error.URLError, TimeoutError, UnicodeDecodeError):
    return "", {}

  transcript_text = clean_caption_text(caption_text)
  if not transcript_text:
    return "", {}

  metadata = {
    "transcript_source": "closed_caption",
    "transcript_url": caption_url,
    "transcript_characters": len(transcript_text),
  }
  write_cached_transcript(transcript_cache_dir, path, transcript_text, metadata)
  return transcript_text, metadata


class PlaceResolver:
    def __init__(self) -> None:
        self.aliases: dict[str, ResolvedLocation] = {}
        self._load_manual_aliases()
        self._load_country_aliases()

    def _add_alias(self, alias: str, label: str, latitude: float, longitude: float, kind: str) -> None:
        normalized = normalize_space(alias.lower())
        self.aliases[normalized] = ResolvedLocation(
            label=label,
            latitude=float(latitude),
            longitude=float(longitude),
            match=alias,
            kind=kind,
        )

    def _load_manual_aliases(self) -> None:
        for alias, (latitude, longitude, label, kind) in MANUAL_LOCATION_COORDS.items():
            self._add_alias(alias, label, latitude, longitude, kind)

    def _load_country_aliases(self) -> None:
        cache = geonamescache.GeonamesCache()
        capitals: dict[str, tuple[float, float]] = {}
        for city in cache.get_cities().values():
            population = int(city.get("population") or 0)
            name = city.get("name", "").lower()
            latitude = city.get("latitude")
            longitude = city.get("longitude")
            existing = capitals.get(name)
            if latitude is None or longitude is None:
                continue
            if existing is None or population > existing[0]:
                capitals[name] = (population, float(latitude), float(longitude))

        for country in cache.get_countries().values():
            capital_name = (country.get("capital") or "").lower()
            if capital_name not in capitals:
                continue
            _, latitude, longitude = capitals[capital_name]
            country_name = country.get("name")
            if not country_name:
                continue
            self._add_alias(country_name, country_name, latitude, longitude, "country")

    def resolve(self, *text_blocks: str) -> ResolvedLocation | None:
        haystack = normalize_space(" ".join(text_blocks).lower())
        if not haystack:
            return None

        best_match: tuple[int, ResolvedLocation] | None = None
        for alias, location in self.aliases.items():
            pattern = rf"(?<![a-z]){re.escape(alias)}(?![a-z])"
            if re.search(pattern, haystack) is None:
                continue
            score = len(alias)
            if haystack.startswith(alias):
                score += 8
            if location.kind in {"site", "city", "region"}:
                score += 6
            if best_match is None or score > best_match[0]:
                best_match = (score, location)
        return best_match[1] if best_match else None


def analyze_document(
    path: Path,
    resolver: PlaceResolver,
    source_dir: Path,
  source_manifest: dict[str, dict[str, object]],
    review_overrides: dict[str, dict[str, object]],
    reviewer: LocalModelReviewer | None,
  force_review_refresh: bool,
    page_limit: int | None,
    ocr_max_pages_per_document: int | None,
    ocr_dpi: int,
    min_native_chars: int,
) -> dict[str, object]:
    source_metadata = source_manifest.get(path.name)
    doc = fitz.open(path)
    processed_pages = min(doc.page_count, page_limit) if page_limit else doc.page_count
    native_pages = 0
    ocr_pages = 0
    ocr_skipped_pages = 0
    page_texts: list[str] = []

    for page_index in range(processed_pages):
        page = doc[page_index]
        native_text = normalize_space(page.get_text("text"))
        final_text = native_text
        use_ocr = cleaned_char_count(native_text) < min_native_chars
        ocr_allowed = ocr_max_pages_per_document is None or ocr_pages < ocr_max_pages_per_document

        if use_ocr and ocr_allowed:
            ocr_text = normalize_space(ocr_page(page, dpi=ocr_dpi))
            if cleaned_char_count(ocr_text) >= cleaned_char_count(native_text):
                final_text = ocr_text
                if final_text:
                    ocr_pages += 1
            elif final_text:
                native_pages += 1
        elif final_text:
            native_pages += 1
        elif use_ocr and not ocr_allowed:
            ocr_skipped_pages += 1

        if final_text:
            page_texts.append(f"[Page {page_index + 1}] {final_text}")

    combined_text = "\n".join(page_texts)
    title = slug_title(path.stem)
    date_label = extract_date_label(title) or extract_date_label(combined_text[:1200])
    title_year_start, title_year_end, title_primary_year = extract_year_info(title)
    if title_year_start is not None:
        year_start, year_end, primary_year = title_year_start, title_year_end, title_primary_year
    elif date_label:
        year_start, year_end, primary_year = extract_year_info(date_label)
    else:
        year_start, year_end, primary_year = extract_year_info(combined_text[:1200])
    document_type = infer_document_type(title)
    observation = choose_excerpt(combined_text)
    location = resolver.resolve(title, observation)
    themes = detect_themes(f"{title} {combined_text[:4000]}")
    agencies = detect_agencies(f"{title} {combined_text[:4000]}")
    top_terms = [term for term, _ in Counter(tokenize(f"{title} {combined_text[:7000]}" )).most_common(8)]

    if ocr_pages and native_pages:
        extraction_method = "hybrid"
    elif ocr_pages:
        extraction_method = "ocr"
    else:
        extraction_method = "native"

    summary_narrative = build_summary_narrative(
      document_type=document_type,
      date_label=date_label,
      location=location,
      agencies=agencies,
      themes=themes,
      top_terms=top_terms,
      extraction_method=extraction_method,
      observation=observation,
    )

    relative_path = path.relative_to(source_dir.parent.parent)
    document = {
        "filename": path.name,
        "title": title,
        "relative_path": str(relative_path).replace("\\", "/"),
        "media_type": "pdf",
        "original_source_url": original_source_url(source_metadata),
        "thumbnail_url": source_thumbnail_url(source_metadata, "pdf"),
        "source_href": f"../data/sources/{path.name}",
        "document_type": document_type,
        "date_label": date_label,
        "year": primary_year,
        "year_start": year_start,
        "year_end": year_end,
        "page_count": doc.page_count,
        "processed_pages": processed_pages,
        "extraction_method": extraction_method,
        "ocr_pages": ocr_pages,
        "native_pages": native_pages,
        "ocr_skipped_pages": ocr_skipped_pages,
        "text_characters": cleaned_char_count(combined_text),
        "summary_narrative": summary_narrative,
        "themes": themes,
        "agencies": agencies,
        "top_terms": top_terms,
        "location": {
            "label": location.label,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "match": location.match,
            "kind": location.kind,
        }
        if location
        else None,
    }
    review = find_review_override(review_overrides, path.name)
    if reviewer is not None and (review is None or force_review_refresh or not review_has_required_fields(review)):
      generated_review = reviewer.review_document(document, doc, combined_text, resolver)
      if generated_review:
        updated_review = dict(review) if isinstance(review, dict) else {}
        updated_review.update(generated_review)
        review_overrides[path.name] = updated_review
        review = updated_review
    return apply_review_override(document, review)


def analyze_media_item(
    path: Path,
    resolver: PlaceResolver,
    source_dir: Path,
    transcript_cache_dir: Path,
    audio_transcript_mode: str,
    source_manifest: dict[str, dict[str, object]],
    review_overrides: dict[str, dict[str, object]],
    reviewer: LocalModelReviewer | None,
    force_review_refresh: bool,
) -> dict[str, object]:
    source_metadata = source_manifest.get(path.name) or {}
    manifest_media_type = normalize_space(str(source_metadata.get("media_type") or "")).lower()
    if manifest_media_type in MEDIA_TYPES:
      media_type = manifest_media_type
    elif path.suffix.lower() in IMAGE_EXTENSIONS:
      media_type = "image"
    else:
      media_type = "video"

    title = manifest_text(source_metadata, "title") or slug_title(path.stem)
    narrative = manifest_text(
        source_metadata,
        "narrative",
        "description_blurb",
        "dvids_description",
        "dvids_title",
    )
    date_sources = [
        manifest_text(source_metadata, key)
        for key in ("incident_date", "date_taken", "release_date", "date_published")
    ]
    date_source = " ".join(source for source in date_sources if source)
    location_source = manifest_text(source_metadata, "incident_location")
    agency_source = manifest_text(source_metadata, "agency")
    media_metadata: dict[str, object] = {}
    extracted_text = ""
    visual_observations = None
    transcript_text = ""
    transcript_metadata: dict[str, object] = {}

    if media_type == "image":
      try:
        extracted_text, media_metadata = ocr_image(path)
      except (OSError, RuntimeError, ValueError):
        extracted_text = ""
        media_metadata = {}
      document_type = "Imagery"
      extraction_method = "image_ocr" if extracted_text else "image_metadata"
      if media_metadata.get("image_width") and media_metadata.get("image_height"):
        visual_observations = (
            f"Still image asset with dimensions {media_metadata['image_width']} x {media_metadata['image_height']} pixels. "
            f"{narrative}" if narrative else
            f"Still image asset with dimensions {media_metadata['image_width']} x {media_metadata['image_height']} pixels."
        )
    else:
      media_metadata = probe_video(path)
      if media_type == "audio" and not media_metadata.get("audio_codec"):
        media_metadata["audio_codec"] = path.suffix.lower().lstrip(".") or "audio"
      if audio_transcript_mode != "off":
        transcript_text, transcript_metadata = fetch_media_transcript(path, source_metadata, transcript_cache_dir)
      if (
          not transcript_text
          and audio_transcript_mode == "llm"
          and reviewer is not None
          and (media_type == "audio" or media_metadata.get("audio_codec"))
          and not should_skip_asr_transcript(
            transcript_cache_dir,
            path,
            reviewer.audio_transcript_model or reviewer.model_name,
          )
      ):
        transcript_text, transcript_metadata = reviewer.transcribe_audio_track(path, media_metadata)
        if transcript_text:
          write_cached_transcript(transcript_cache_dir, path, transcript_text, transcript_metadata)
        elif transcript_metadata.get("transcript_status") == "no_useful_transcript":
          write_transcript_metadata(transcript_cache_dir, path, transcript_metadata)
      if transcript_metadata:
        media_metadata.update(transcript_metadata)
        extracted_text = transcript_text
      if "duration_seconds" not in media_metadata and source_metadata.get("duration_seconds") is not None:
        media_metadata["duration_seconds"] = source_metadata.get("duration_seconds")
      document_type = "Audio" if media_type == "audio" else "Video"
      extraction_method = "audio_metadata" if media_type == "audio" else "video_metadata"
      if transcript_text:
        extraction_method = "audio_transcript" if media_type == "audio" else "video_audio_transcript"
      duration = media_metadata.get("duration_seconds")
      dimensions = ""
      if media_metadata.get("video_width") and media_metadata.get("video_height"):
        dimensions = f" at {media_metadata['video_width']} x {media_metadata['video_height']} pixels"
      if isinstance(duration, (int, float)):
        clip_label = "Audio clip" if media_type == "audio" else "Video clip"
        visual_observations = f"{clip_label}, {format_duration(float(duration))} long{dimensions}."
      elif dimensions:
        visual_observations = f"Short video clip{dimensions}."

    combined_text = normalize_space(
        " ".join(
            item
            for item in [
                title,
                narrative,
                agency_source,
                location_source,
                date_source,
                extracted_text,
            ]
            if item
        )
    )
    date_label = (
        next((label for label in (parse_manifest_date_label(source) for source in date_sources) if label), None)
        or extract_date_label(title)
        or extract_date_label(combined_text)
    )
    title_year_start, title_year_end, title_primary_year = extract_year_info(title)
    if title_year_start is not None:
      year_start, year_end, primary_year = title_year_start, title_year_end, title_primary_year
    elif date_label:
      year_start, year_end, primary_year = extract_year_info(date_label)
    else:
      year_start, year_end, primary_year = extract_year_info(combined_text)

    observation = choose_excerpt(narrative or extracted_text or combined_text)
    location = resolver.resolve(location_source) if location_source else None
    if location is None:
      location = resolver.resolve(title, observation)
    themes = detect_themes(f"{title} {combined_text}")
    if media_type in {"image", "video"} and "Imagery and Visuals" not in themes:
      themes.append("Imagery and Visuals")
    agencies = detect_agencies(f"{agency_source} {title} {combined_text}")
    if agency_source and agency_source not in agencies:
      agencies.insert(0, agency_source)
    agencies = list(dict.fromkeys(agencies))[:8]
    top_terms = [term for term, _ in Counter(tokenize(combined_text)).most_common(8)]

    summary_narrative = build_summary_narrative(
      document_type=document_type,
      date_label=date_label,
      location=location,
      agencies=agencies,
      themes=themes,
      top_terms=top_terms,
      extraction_method=extraction_method,
      observation=observation,
    )

    relative_path = path.relative_to(source_dir.parent.parent)
    document = {
        "filename": path.name,
        "title": title,
        "relative_path": str(relative_path).replace("\\", "/"),
        "media_type": media_type,
        "original_source_url": original_source_url(source_metadata),
        "thumbnail_url": source_thumbnail_url(source_metadata, media_type),
        "source_href": f"../data/sources/{path.name}",
        "source_page_url": manifest_text(source_metadata, "source_page_url") or None,
        "document_type": document_type,
        "date_label": date_label,
        "year": primary_year,
        "year_start": year_start,
        "year_end": year_end,
        "page_count": 0,
        "processed_pages": 0,
        "extraction_method": extraction_method,
        "ocr_pages": 0,
        "native_pages": 0,
        "ocr_skipped_pages": 0,
        "text_characters": cleaned_char_count(combined_text),
        "summary_narrative": summary_narrative,
        "visual_observations": visual_observations,
        "transcript_excerpt": choose_excerpt(transcript_text, max_length=900) if transcript_text else None,
        "transcript_source": transcript_metadata.get("transcript_source"),
        "transcript_characters": transcript_metadata.get("transcript_characters", 0),
        "audio_source_characterization": None,
        "audio_transcript_summary": None,
        "themes": themes,
        "agencies": agencies,
        "top_terms": top_terms,
        "media_metadata": media_metadata,
        "location": {
            "label": location.label,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "match": location.match,
            "kind": location.kind,
        }
        if location
        else None,
    }
    review = find_review_override(review_overrides, path.name)
    needs_transcript_review = bool(transcript_text) and not (isinstance(review, dict) and review.get("transcript_reviewed"))
    needs_audio_context_review = bool(transcript_text) and not (
      isinstance(review, dict)
      and normalize_space(str(review.get("audio_source_characterization") or ""))
      and normalize_space(str(review.get("audio_transcript_summary") or ""))
    )
    if reviewer is not None and needs_audio_context_review and isinstance(review, dict) and not force_review_refresh:
      generated_audio_context = reviewer.review_audio_transcript_context(
        document=document,
        manifest_narrative=narrative,
        transcript_text=transcript_text,
      )
      if generated_audio_context:
        updated_review = dict(review)
        updated_review.update(generated_audio_context)
        review_overrides[path.name] = updated_review
        review = updated_review

    if reviewer is not None and (review is None or force_review_refresh or not review_has_required_fields(review) or needs_transcript_review):
      generated_review = reviewer.review_media_item(
          document=document,
          path=path,
          media_metadata=media_metadata,
          manifest_narrative=narrative,
          extracted_text=extracted_text,
          resolver=resolver,
      )
      if generated_review:
        updated_review = dict(review) if isinstance(review, dict) else {}
        updated_review.update(generated_review)
        if transcript_text:
          updated_review["transcript_reviewed"] = True
          if "audio_source_characterization" not in updated_review or "audio_transcript_summary" not in updated_review:
            generated_audio_context = reviewer.review_audio_transcript_context(
              document=document,
              manifest_narrative=narrative,
              transcript_text=transcript_text,
            )
            if generated_audio_context:
              updated_review.update(generated_audio_context)
        review_overrides[path.name] = updated_review
        review = updated_review
    return apply_review_override(document, review)


def refresh_audio_context_reviews(
    source_paths: list[Path],
    transcript_cache_dir: Path,
    source_manifest: dict[str, dict[str, object]],
    review_overrides: dict[str, dict[str, object]],
    review_file: Path,
    reviewer: LocalModelReviewer,
) -> tuple[int, int]:
    candidates = 0
    updates = 0
    for path in source_paths:
      if path.suffix.lower() in PDF_EXTENSIONS:
        continue
      transcript_text, _ = read_cached_transcript(transcript_cache_dir, path)
      if not transcript_text:
        continue
      candidates += 1

      review = find_review_override(review_overrides, path.name)
      if isinstance(review, dict) and review.get("audio_source_characterization") and review.get("audio_transcript_summary"):
        continue

      source_metadata = source_manifest.get(path.name) or {}
      manifest_media_type = normalize_space(str(source_metadata.get("media_type") or "")).lower()
      media_type = manifest_media_type if manifest_media_type in MEDIA_TYPES else ("image" if path.suffix.lower() in IMAGE_EXTENSIONS else "video")
      title = manifest_text(source_metadata, "title") or slug_title(path.stem)
      narrative = manifest_text(
          source_metadata,
          "narrative",
          "description_blurb",
          "dvids_description",
          "dvids_title",
      )
      document = {
        "filename": path.name,
        "title": title,
        "media_type": media_type,
        "document_type": "Audio" if media_type == "audio" else "Video",
      }
      generated_audio_context = reviewer.review_audio_transcript_context(
        document=document,
        manifest_narrative=narrative,
        transcript_text=transcript_text,
      )
      if not generated_audio_context:
        continue

      updated_review = dict(review) if isinstance(review, dict) else {}
      updated_review.update(generated_audio_context)
      updated_review["transcript_reviewed"] = True
      review_overrides[path.name] = updated_review
      write_review_overrides(review_file, review_overrides)
      updates += 1

    return candidates, updates


def build_research_signals(documents: list[dict[str, object]]) -> list[str]:
    signals: list[str] = []
    years = [doc["year"] for doc in documents if isinstance(doc.get("year"), int)]
    if years:
        peak_year, peak_count = Counter(years).most_common(1)[0]
        signals.append(f"Peak document activity in the sampled corpus occurs in {peak_year}, with {peak_count} documents tied to that year.")

    locations = [doc["location"]["label"] for doc in documents if doc.get("location")]
    if locations:
        hotspot, hotspot_count = Counter(locations).most_common(1)[0]
        signals.append(f"The strongest geography cluster resolves around {hotspot}, appearing in {hotspot_count} documents.")

    themes = Counter(theme for doc in documents for theme in doc.get("themes", []))
    if themes:
      leading_themes = [name for name, _ in themes.most_common(2)]
      if len(leading_themes) == 1:
        signals.append(f"The dominant research theme is {leading_themes[0].lower()}, which indicates how the current corpus slice is clustering.")
      else:
        theme_a, theme_b = leading_themes
        signals.append(f"The two most persistent research themes are {theme_a.lower()} and {theme_b.lower()}, suggesting the corpus clusters around both operational reporting and anomaly characterization.")

    capabilities = Counter(label for doc in documents for label in doc.get("capability_labels", []))
    if capabilities:
      leading_capability, leading_count = capabilities.most_common(1)[0]
      signals.append(f"The most common observed capability label is {leading_capability.lower()}, appearing in {leading_count} sources.")

    evidence_categories = Counter(doc["evidence_category"] for doc in documents if isinstance(doc.get("evidence_category"), str))
    if evidence_categories:
      leading_label, leading_count = evidence_categories.most_common(1)[0]
      signals.append(f"{leading_count} documents currently fall into {leading_label.lower()}, giving a quick read on how much of the corpus has corroborated event support versus weakly supported claims.")

    return signals[:4]


def build_executive_summary_signature(documents: list[dict[str, object]]) -> str:
    signature_documents = []
    for document in documents:
      location = document.get("location")
      signature_documents.append(
        {
          "filename": document.get("filename"),
          "title": document.get("title"),
          "year": document.get("year"),
          "document_type": document.get("document_type"),
          "evidence_category": document.get("evidence_category"),
          "themes": document.get("themes", []),
          "trend_fields": {
            field_name: document.get(field_name)
            for field_name in TREND_FIELD_NAMES
            if field_name != "media_authenticity_notes"
          },
          "location": location.get("label") if isinstance(location, dict) else None,
          "summary_narrative": document.get("summary_narrative"),
        }
      )
    payload = json.dumps(signature_documents, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extract_standout_evidence_terms(document: dict[str, object]) -> list[str]:
    text = normalize_space(
      " ".join(
        str(document.get(key) or "")
        for key in ("title", "summary_narrative", "visual_observations")
      )
    ).lower()
    checks = [
      ("radar or sensor correlation", ("radar", "sensor", "tracked", "tracking", "tflir", "flir")),
      ("multiple trained observers", ("multiple", "pilots", "pilot", "crew", "teams", "law enforcement")),
      ("photographic or video evidence", ("photograph", "photo", "image", "imagery", "video", "film")),
      ("physical trace evidence", ("physical evidence", "ground evidence", "indentations", "burned grass", "residue")),
      ("specific maneuver detail", ("corkscrew", "90-degree", "formation", "rapid acceleration", "hovering", "land", "take off")),
      ("night vision or visual detail", ("night vision", "orange", "orb", "bright light", "flame")),
    ]
    terms: list[str] = []
    for label, needles in checks:
      if any(needle in text for needle in needles):
        terms.append(label)
    return terms[:4]


def category_one_standout_score(document: dict[str, object]) -> float:
    text = normalize_space(
      " ".join(
        str(document.get(key) or "")
        for key in ("title", "summary_narrative", "visual_observations")
      )
    ).lower()
    weighted_terms = {
      "physical evidence": 10,
      "ground evidence": 10,
      "indentations": 8,
      "burned grass": 8,
      "radar-confirmed": 9,
      "radar": 7,
      "sensor": 6,
      "tracked": 6,
      "tracking": 6,
      "night vision": 6,
      "photograph": 6,
      "photo": 5,
      "tflir": 5,
      "flir": 5,
      "multiple": 4,
      "pilot": 4,
      "pilots": 4,
      "crew": 3,
      "law enforcement": 4,
      "formation": 3,
      "rapid acceleration": 4,
      "corkscrew": 4,
      "90-degree": 4,
    }
    score = 0.0
    for term, weight in weighted_terms.items():
      if term in text:
        score += weight
    themes = set(str(theme) for theme in document.get("themes", []) if isinstance(theme, str))
    if "Sensors and Radar" in themes:
      score += 5
    if "Imagery and Visuals" in themes:
      score += 3
    if "Anomalous Objects" in themes:
      score += 2
    if document.get("visual_observations"):
      score += 4
    if document.get("location"):
      score += 2
    if document.get("document_type") in {"Incident Summary", "Mission Report", "Statement", "UAP Report", "Diplomatic Cable"}:
      score += 3
    if re.search(r"\bsection\b", str(document.get("title") or ""), re.IGNORECASE):
      score -= 4
    return score


def select_standout_category_one_sources(documents: list[dict[str, object]], limit: int = 3) -> list[dict[str, object]]:
    candidates = [
      document
      for document in documents
      if document.get("evidence_category") == "Category One"
    ]
    return sorted(
      candidates,
      key=lambda document: (
        category_one_standout_score(document),
        str(document.get("title") or ""),
      ),
      reverse=True,
    )[:limit]


def normalize_executive_summary_review(
    review: dict[str, object] | None,
    corpus_signature: str,
) -> dict[str, object] | None:
    if not isinstance(review, dict):
      return None
    if review.get("review_version") != EXECUTIVE_SUMMARY_VERSION:
      return None
    if review.get("corpus_signature") != corpus_signature:
      return None
    raw_sections = review.get("executive_summary_sections")
    if not isinstance(raw_sections, list):
      return None
    sections = [normalize_space(section) for section in raw_sections if isinstance(section, str) and normalize_space(section)]
    if not sections:
      return None
    return {
      "sections": sections[:5],
      "source": public_review_source(review.get("review_source")),
      "generated_at": review.get("generated_at"),
    }


def public_review_source(value: object) -> str | None:
    if not isinstance(value, str) or not normalize_space(value):
      return None
    normalized = normalize_space(value)
    lowered = normalized.lower()
    if lowered.startswith("local_model:"):
      return "Local AI review"
    if lowered in {"manual_ai_review", "manual ai review"}:
      return "Manual AI review"
    if lowered == "scripted_extraction":
      return "Scripted extraction"
    return normalized


def sanitize_dashboard_value(value: object) -> object:
    if isinstance(value, dict):
      cleaned: dict[str, object] = {}
      for key, nested in value.items():
        if key == "transcript_model":
          continue
        if key == "transcript_excerpt":
          continue
        if key == "transcript_source":
          continue
        if key == "review_source":
          public_source = public_review_source(nested)
          if public_source:
            cleaned[key] = public_source
          continue
        if key == "source":
          public_source = public_review_source(nested)
          cleaned[key] = public_source if public_source else sanitize_dashboard_value(nested)
          continue
        cleaned[key] = sanitize_dashboard_value(nested)
      return cleaned
    if isinstance(value, list):
      return [sanitize_dashboard_value(item) for item in value]
    if isinstance(value, str) and value.lower().startswith("local_model:"):
      return "Local AI review"
    return value


def sanitize_dashboard_document(document: dict[str, object]) -> dict[str, object]:
    return sanitize_dashboard_value(document)  # type: ignore[return-value]


@dataclass
class EmbeddingClient:
  base_url: str
  model_name: str
  timeout_seconds: int
  api_key: str | None = None

  def _embedding_request(self, input_value: str | list[str]) -> list[list[float]] | None:
    expected_count = len(input_value) if isinstance(input_value, list) else 1

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if self.api_key:
      headers["Authorization"] = f"Bearer {self.api_key}"
    base_url = self.base_url.rstrip("/")
    if not base_url.endswith("/v1"):
      base_url = f"{base_url}/v1"
    request = urllib_request.Request(
      url=f"{base_url}/embeddings",
      data=json.dumps({"model": self.model_name, "input": input_value}).encode("utf-8"),
      headers=headers,
      method="POST",
    )

    try:
      with urllib_request.urlopen(request, timeout=self.timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError):
      return None

    data = payload.get("data")
    if not isinstance(data, list):
      return None

    ordered_embeddings: list[tuple[int, list[float]]] = []
    for fallback_index, item in enumerate(data):
      if not isinstance(item, dict):
        return None
      raw_embedding = item.get("embedding")
      if not isinstance(raw_embedding, list):
        return None
      embedding: list[float] = []
      for value in raw_embedding:
        if not isinstance(value, (int, float)):
          return None
        embedding.append(float(value))
      if not embedding:
        return None
      raw_index = item.get("index")
      index = int(raw_index) if isinstance(raw_index, int) else fallback_index
      ordered_embeddings.append((index, embedding))

    ordered_embeddings.sort(key=lambda item: item[0])
    embeddings = [embedding for _, embedding in ordered_embeddings]
    return embeddings if len(embeddings) == expected_count else None

  def embed_batch(self, texts: list[str]) -> list[list[float]] | None:
    if not texts:
      return []

    embeddings = self._embedding_request(texts)
    if embeddings is not None:
      return embeddings

    fallback_embeddings: list[list[float]] = []
    for text in texts:
      single = self._embedding_request(text)
      if single is None or len(single) != 1:
        return None
      fallback_embeddings.append(single[0])
    return fallback_embeddings


def build_trend_field_counts(documents: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    trend_counts: dict[str, list[dict[str, object]]] = {}
    for field_name, definition in TREND_FIELD_DEFINITIONS.items():
      if definition.get("type") == "string_or_null":
        continue

      counter: Counter[object] = Counter()
      for document in documents:
        value = document.get(field_name)
        if isinstance(value, list):
          counter.update(item for item in value if item is not None)
        elif value is not None:
          counter[value] += 1

      trend_counts[field_name] = [
        {"label": str(label).lower() if isinstance(label, bool) else label, "count": count}
        for label, count in counter.most_common()
      ]
    return trend_counts


RELATED_IGNORE_TOKENS = {
    "",
    "false",
    "not_applicable",
    "none",
    "none_stated",
    "unclear",
    "unknown",
    "unknown_redacted",
}
RELATED_GENERIC_THEMES = {
    "anomalous_objects",
    "imagery_and_visuals",
}
RELATED_METADATA_TREND_FIELDS = (
    "sensor_platform",
    "observer_platform",
    "observer_roles",
    "corroboration_types",
    "object_count_bucket",
    "morphology_normalized",
    "color_luminosity_normalized",
    "apparent_motion_class",
    "transmedium_capabilities",
    "anomalous_signatures",
    "equipment_malfunction",
    "biological_observer_effects",
    "quantitative_fields_present",
)


def related_signal_token(value: object) -> str:
    if value is None or value is False:
      return ""
    if isinstance(value, bool):
      return "true" if value else ""
    return normalize_enum_token(str(value))


def is_related_signal_value(value: object) -> bool:
    return related_signal_token(value) not in RELATED_IGNORE_TOKENS


def related_string_values(document: dict[str, object], field_name: str) -> set[str]:
    if field_name == "capability_labels":
      value = document.get("capability_labels")
      if isinstance(value, list):
        return {str(item) for item in value if isinstance(item, str) and is_related_signal_value(item)}
      return {
        str(capability["label"])
        for capability in infer_document_capabilities(document)
        if is_related_signal_value(capability.get("label"))
      }

    value = document.get(field_name)
    if isinstance(value, list):
      return {str(item) for item in value if isinstance(item, str) and is_related_signal_value(item)}
    if isinstance(value, str) and is_related_signal_value(value):
      return {value}
    return set()


def related_location_label(document: dict[str, object]) -> str | None:
    location = document.get("location")
    if not isinstance(location, dict):
      return None
    label = location.get("label")
    if isinstance(label, str) and is_related_signal_value(label):
      return label
    return None


def related_embedding_text(document: dict[str, object]) -> str:
    parts: list[str] = []
    title = document.get("title") or document.get("filename")
    if title:
      parts.append(f"Source title: {title}")

    summary = document.get("summary_narrative")
    if isinstance(summary, str) and normalize_space(summary):
      parts.append(f"Summary: {summary}")

    visual = document.get("visual_observations")
    if isinstance(visual, str) and normalize_space(visual):
      parts.append(f"Visual observations: {visual}")

    audio = document.get("audio_transcript_summary")
    if isinstance(audio, str) and normalize_space(audio):
      parts.append(f"Audio transcript summary: {audio}")

    metadata_parts: list[str] = []
    year = document.get("year") or document.get("year_start")
    if isinstance(year, int):
      metadata_parts.append(f"year {year}")
    location_label = related_location_label(document)
    if location_label:
      metadata_parts.append(f"location {location_label}")

    themes = {
      value
      for value in related_string_values(document, "themes")
      if related_signal_token(value) not in RELATED_GENERIC_THEMES
    }
    agencies = related_string_values(document, "agencies")
    capability_labels = related_string_values(document, "capability_labels")
    if themes:
      metadata_parts.append("themes " + ", ".join(sorted(themes)))
    if agencies:
      metadata_parts.append("agencies " + ", ".join(sorted(agencies)))
    if capability_labels:
      metadata_parts.append("capabilities " + ", ".join(sorted(capability_labels)))
    if metadata_parts:
      parts.append("Relevant metadata: " + "; ".join(metadata_parts))

    trend_values: list[str] = []
    for field_name in RELATED_METADATA_TREND_FIELDS:
      value = document.get(field_name)
      if isinstance(value, list):
        rendered_values = [str(item) for item in value if is_related_signal_value(item)]
        if rendered_values:
          trend_values.append(f"{field_name}: {', '.join(rendered_values)}")
      elif is_related_signal_value(value):
        trend_values.append(f"{field_name}: {value}")
    if trend_values:
      parts.append("Relationship signals: " + "; ".join(trend_values))

    return normalize_space(" ".join(parts))[:6000]


def embedding_signature(model_name: str, text: str) -> str:
    return hashlib.sha256(f"{model_name}\n{text}".encode("utf-8")).hexdigest()


def load_embedding_cache(path: Path, model_name: str) -> dict[str, dict[str, object]]:
    if not path.exists():
      return {}
    try:
      payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
      return {}
    if not isinstance(payload, dict):
      return {}
    if payload.get("cache_version") != EMBEDDING_CACHE_VERSION or payload.get("model") != model_name:
      return {}
    items = payload.get("items")
    if not isinstance(items, dict):
      return {}
    return {key: value for key, value in items.items() if isinstance(key, str) and isinstance(value, dict)}


def write_embedding_cache(path: Path, model_name: str, items: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
      "cache_version": EMBEDDING_CACHE_VERSION,
      "model": model_name,
      "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
      "items": items,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_embedding(vector: list[float]) -> list[float] | None:
    magnitude = math.sqrt(sum(value * value for value in vector))
    if not magnitude:
      return None
    return [value / magnitude for value in vector]


def shared_related_factors(left: dict[str, object], right: dict[str, object]) -> list[str]:
    factors: list[str] = []

    def add(label: str) -> None:
      if label and label not in factors:
        factors.append(label)

    left_location_label = related_location_label(left)
    right_location_label = related_location_label(right)
    if left_location_label and left_location_label == right_location_label:
      add(f"same location: {left_location_label}")

    left_year = left.get("year")
    right_year = right.get("year")
    if isinstance(left_year, int) and isinstance(right_year, int):
      if left_year == right_year:
        add(f"same year: {left_year}")
      elif abs(left_year - right_year) <= 1:
        add("adjacent event years")

    for field_name, label in (
      ("themes", "shared theme"),
      ("agencies", "shared agency"),
      ("capability_labels", "shared capability"),
    ):
      left_values = related_string_values(left, field_name)
      right_values = related_string_values(right, field_name)
      if field_name == "themes":
        left_values = {value for value in left_values if related_signal_token(value) not in RELATED_GENERIC_THEMES}
        right_values = {value for value in right_values if related_signal_token(value) not in RELATED_GENERIC_THEMES}
      for value in sorted(left_values & right_values)[:3]:
        add(f"{label}: {value}")

    for field_name, label in (
      ("observer_roles", "shared observer role"),
      ("corroboration_types", "shared corroboration"),
      ("morphology_normalized", "shared morphology"),
      ("color_luminosity_normalized", "shared color/luminosity"),
      ("apparent_motion_class", "shared motion"),
      ("quantitative_fields_present", "shared quantitative field"),
    ):
      left_values = related_string_values(left, field_name)
      right_values = related_string_values(right, field_name)
      for value in sorted(left_values & right_values)[:3]:
        add(f"{label}: {value}")

    return factors[:8]


def attach_related_sources(
    documents: list[dict[str, object]],
    embedding_client: EmbeddingClient,
    cache_path: Path,
    refresh_embeddings: bool,
    related_limit: int,
    min_score: float,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    if len(documents) < 2 or related_limit <= 0:
      return [dict(document, related_sources=[]) for document in documents], {
        "enabled": False,
        "reason": "not_enough_documents",
        "model": embedding_client.model_name,
      }

    cache_items = {} if refresh_embeddings else load_embedding_cache(cache_path, embedding_client.model_name)
    texts_by_filename = {str(document.get("filename") or index): related_embedding_text(document) for index, document in enumerate(documents)}
    signatures = {
      filename: embedding_signature(embedding_client.model_name, text)
      for filename, text in texts_by_filename.items()
    }
    embeddings: dict[str, list[float]] = {}
    missing: list[tuple[str, str]] = []

    for filename, text in texts_by_filename.items():
      cached = cache_items.get(filename)
      cached_embedding = cached.get("embedding") if isinstance(cached, dict) else None
      if (
          isinstance(cached, dict)
          and cached.get("signature") == signatures[filename]
          and isinstance(cached_embedding, list)
          and all(isinstance(value, (int, float)) for value in cached_embedding)
      ):
        normalized = normalize_embedding([float(value) for value in cached_embedding])
        if normalized:
          embeddings[filename] = normalized
          continue
      missing.append((filename, text))

    request_failed = False
    for batch_start in range(0, len(missing), 16):
      batch = missing[batch_start : batch_start + 16]
      batch_embeddings = embedding_client.embed_batch([text for _, text in batch])
      if batch_embeddings is None:
        request_failed = True
        break
      for (filename, _), vector in zip(batch, batch_embeddings):
        normalized = normalize_embedding(vector)
        if not normalized:
          continue
        embeddings[filename] = normalized
        cache_items[filename] = {
          "signature": signatures[filename],
          "embedding": vector,
        }

    if request_failed or len(embeddings) < 2:
      return [dict(document, related_sources=[]) for document in documents], {
        "enabled": False,
        "reason": "embedding_request_failed" if request_failed else "too_few_embeddings",
        "model": embedding_client.model_name,
        "embedded_count": len(embeddings),
      }

    write_embedding_cache(cache_path, embedding_client.model_name, cache_items)

    by_filename = {str(document.get("filename") or index): document for index, document in enumerate(documents)}
    related_documents: list[dict[str, object]] = []
    edge_count = 0
    for index, document in enumerate(documents):
      filename = str(document.get("filename") or index)
      source_vector = embeddings.get(filename)
      if not source_vector:
        related_documents.append(dict(document, related_sources=[]))
        continue

      candidates: list[tuple[float, str, list[str]]] = []
      for other_filename, other_vector in embeddings.items():
        if other_filename == filename:
          continue
        if len(source_vector) != len(other_vector):
          continue
        score = sum(left * right for left, right in zip(source_vector, other_vector))
        if score >= min_score:
          other = by_filename[other_filename]
          shared_factors = shared_related_factors(document, other)
          if shared_factors:
            candidates.append((score, other_filename, shared_factors))
      candidates.sort(key=lambda item: (-item[0], item[1]))

      related_sources = []
      for score, other_filename, shared_factors in candidates[:related_limit]:
        other = by_filename[other_filename]
        related_sources.append(
          {
            "filename": other.get("filename"),
            "title": other.get("title"),
            "source_href": other.get("source_href"),
            "year": other.get("year"),
            "document_type": other.get("document_type"),
            "evidence_category": other.get("evidence_category"),
            "similarity": round(score, 4),
            "shared_factors": shared_factors,
          }
        )
      edge_count += len(related_sources)
      related_documents.append(dict(document, related_sources=related_sources))

    return related_documents, {
      "enabled": True,
      "model": embedding_client.model_name,
      "embedded_count": len(embeddings),
      "documents_with_related_sources": sum(1 for document in related_documents if document.get("related_sources")),
      "edge_count": edge_count,
      "related_sources_limit": related_limit,
      "minimum_similarity": min_score,
      "relationship_criteria": "embedding_similarity_plus_shared_unique_source_factors",
    }


def build_analysis(
    documents: list[dict[str, object]],
    source_dir: Path,
    executive_summary: dict[str, object] | None = None,
    related_source_stats: dict[str, object] | None = None,
) -> dict[str, object]:
    documents = [annotate_capabilities(document) for document in documents]
    public_documents = [sanitize_dashboard_document(document) for document in documents]
    public_executive_summary = sanitize_dashboard_value(executive_summary) if executive_summary else None
    year_counts = Counter(doc["year"] for doc in documents if isinstance(doc.get("year"), int))
    type_counts = Counter(doc["document_type"] for doc in documents)
    method_counts = Counter(doc["extraction_method"] for doc in documents)
    media_counts = Counter(str(doc.get("media_type") or "pdf") for doc in documents)
    evidence_category_counts = Counter(doc["evidence_category"] for doc in documents if isinstance(doc.get("evidence_category"), str))
    theme_counts = Counter(theme for doc in documents for theme in doc.get("themes", []))
    agency_counts = Counter(agency for doc in documents for agency in doc.get("agencies", []))
    keyword_counts = Counter(term for doc in documents for term in doc.get("top_terms", []))

    hotspots: dict[str, dict[str, object]] = {}
    for doc in documents:
        location = doc.get("location")
        if not location:
            continue
        label = location["label"]
        hotspot = hotspots.setdefault(
            label,
            {
                "label": label,
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "kind": location["kind"],
                "count": 0,
            },
        )
        hotspot["count"] += 1

    years = [doc["year"] for doc in documents if isinstance(doc.get("year"), int)]
    analysis = {
        "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source_dir": str(source_dir),
        "document_count": len(documents),
        "total_pages": sum(int(doc["page_count"]) for doc in documents),
        "media_counts": [{"label": label, "count": count} for label, count in sorted(media_counts.items())],
        "year_min": min(years) if years else None,
        "year_max": max(years) if years else None,
        "year_counts": [{"year": year, "count": count} for year, count in sorted(year_counts.items())],
        "document_type_counts": [{"label": label, "count": count} for label, count in type_counts.most_common()],
        "extraction_method_counts": [{"label": label, "count": count} for label, count in method_counts.most_common()],
        "evidence_category_counts": [
          {
            "label": definition["label"],
            "count": evidence_category_counts.get(definition["label"], 0),
            "rank": definition["rank"],
            "description": definition["description"],
          }
          for definition in EVIDENCE_CATEGORY_DEFINITIONS
        ],
        "theme_counts": [{"label": label, "count": count} for label, count in theme_counts.most_common()],
        "agency_counts": [{"label": label, "count": count} for label, count in agency_counts.most_common()],
        "keyword_counts": [{"term": term, "count": count} for term, count in keyword_counts.most_common(28)],
        "hotspots": sorted(hotspots.values(), key=lambda item: item["count"], reverse=True),
        "trend_field_definitions": {
          field_name: {
            key: value
            for key, value in definition.items()
            if key in {"type", "values", "description"}
          }
          for field_name, definition in TREND_FIELD_DEFINITIONS.items()
        },
        "trend_field_counts": build_trend_field_counts(documents),
        "capability_definitions": [
          {
            "key": definition["key"],
            "label": definition["label"],
            "group": definition["group"],
            "description": definition["description"],
          }
          for definition in CAPABILITY_DEFINITIONS
        ],
        "capability_matrix": build_capability_matrix(documents),
        "research_signals": build_research_signals(documents),
        "related_source_stats": related_source_stats or {"enabled": False},
        "executive_summary": public_executive_summary,
        "documents": public_documents,
    }
    return analysis


def normalize_site_url(site_url: str) -> str:
    site_url = site_url.strip().rstrip("/")
    if not site_url:
        return DEFAULT_SITE_URL
    if not re.match(r"^https?://", site_url):
        site_url = f"https://{site_url}"
    return site_url.rstrip("/")


def render_robots_txt(site_url: str) -> str:
    canonical_site_url = normalize_site_url(site_url)
    return f"""User-agent: *
Allow: /

Sitemap: {canonical_site_url}/sitemap.xml
"""


def render_sitemap_xml(site_url: str, generated_at: str | None = None) -> str:
    canonical_url = f"{normalize_site_url(site_url)}/"
    lastmod = generated_at[:10] if generated_at else datetime.utcnow().date().isoformat()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{html_escape(canonical_url, quote=True)}</loc>
    <lastmod>{html_escape(lastmod, quote=True)}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
"""


def write_crawl_files(output_dir: Path, site_url: str, generated_at: str | None = None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "robots.txt").write_text(render_robots_txt(site_url), encoding="utf-8")
    (output_dir / "sitemap.xml").write_text(render_sitemap_xml(site_url, generated_at), encoding="utf-8")


def render_dashboard_html(analysis: dict[str, object], site_url: str = DEFAULT_SITE_URL) -> str:
    data_json = json.dumps(analysis, ensure_ascii=False).replace("<", "\\u003c")
    land_asset = Path(__file__).resolve().parent / "assets" / "ne_110m_land.geojson"
    land_payload = json.loads(land_asset.read_text(encoding="utf-8"))
    world_land = {
        "geometries": [
            feature["geometry"]
            for feature in land_payload.get("features", [])
            if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)
        ]
    }
    world_land_json = json.dumps(world_land, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    canonical_site_url = normalize_site_url(site_url)
    canonical_url = f"{canonical_site_url}/"
    social_image_url = f"{canonical_site_url}/{SEO_IMAGE_PATH}"
    template = """<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>__PAGE_TITLE__</title>
  <meta name=\"description\" content=\"__PAGE_DESCRIPTION__\">
  <meta name=\"robots\" content=\"index, follow\">
  <link rel=\"canonical\" href=\"__CANONICAL_URL__\">
  <link rel=\"icon\" href=\"__FAVICON_URL__\" type=\"image/svg+xml\">
  <meta property=\"og:type\" content=\"website\">
  <meta property=\"og:site_name\" content=\"UAP/UFO Research Dashboard\">
  <meta property=\"og:title\" content=\"__PAGE_TITLE__\">
  <meta property=\"og:description\" content=\"__PAGE_DESCRIPTION__\">
  <meta property=\"og:url\" content=\"__CANONICAL_URL__\">
  <meta property=\"og:image\" content=\"__SOCIAL_IMAGE_URL__\">
  <meta name=\"twitter:card\" content=\"summary_large_image\">
  <meta name=\"twitter:title\" content=\"__PAGE_TITLE__\">
  <meta name=\"twitter:description\" content=\"__PAGE_DESCRIPTION__\">
  <meta name=\"twitter:image\" content=\"__SOCIAL_IMAGE_URL__\">
  <meta name=\"theme-color\" content=\"#07080b\">
  <style>
    :root {{
      --void: #07080b;
      --night: #0d1118;
      --panel: rgba(17, 21, 29, 0.88);
      --panel-deep: rgba(10, 13, 18, 0.94);
      --ink: #f4efe1;
      --muted: #9aa5b8;
      --dim: #667085;
      --line: rgba(214, 168, 79, 0.18);
      --line-cool: rgba(86, 214, 201, 0.18);
      --gold: #d6a84f;
      --gold-soft: rgba(214, 168, 79, 0.16);
      --teal: #56d6c9;
      --teal-soft: rgba(86, 214, 201, 0.12);
      --red: #c84e53;
      --green: #9bb46e;
      --blue: #7d9cff;
      --shadow: 0 28px 70px rgba(0, 0, 0, 0.38);
    }}

    * {{ box-sizing: border-box; }}

    html {{
      background: var(--void);
    }}

    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: "Avenir Next", "Inter", "Gill Sans", "Trebuchet MS", sans-serif;
      background:
        linear-gradient(90deg, rgba(214, 168, 79, 0.04) 1px, transparent 1px),
        linear-gradient(180deg, rgba(86, 214, 201, 0.035) 1px, transparent 1px),
        linear-gradient(135deg, #07080b 0%, #11161e 48%, #15100c 100%);
      background-size: 56px 56px, 56px 56px, auto;
    }}

    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(255, 255, 255, 0.025) 1px, transparent 1px),
        radial-gradient(circle at 18% 22%, rgba(244, 239, 225, 0.18) 0 1px, transparent 1.5px),
        radial-gradient(circle at 68% 16%, rgba(86, 214, 201, 0.28) 0 1px, transparent 1.5px),
        radial-gradient(circle at 82% 74%, rgba(214, 168, 79, 0.24) 0 1px, transparent 1.5px);
      background-size: 100% 4px, 220px 220px, 300px 300px, 260px 260px;
      opacity: 0.55;
      mix-blend-mode: screen;
    }}

    body::after {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background:
        linear-gradient(90deg, rgba(7, 8, 11, 0.82), transparent 16%, transparent 84%, rgba(7, 8, 11, 0.72)),
        linear-gradient(180deg, rgba(7, 8, 11, 0.1), rgba(7, 8, 11, 0.72));
    }}

    .shell {{
      width: min(1440px, calc(100vw - 32px));
      margin: 24px auto 52px;
      position: relative;
      z-index: 1;
    }}

    .hero {{
      min-height: 430px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(320px, 0.62fr);
      gap: 28px;
      align-items: stretch;
      padding: 34px;
      border: 1px solid var(--line);
      background:
        linear-gradient(135deg, rgba(15, 19, 27, 0.94), rgba(9, 11, 15, 0.86) 52%, rgba(37, 28, 17, 0.72)),
        linear-gradient(90deg, rgba(214, 168, 79, 0.13), transparent 36%),
        linear-gradient(180deg, transparent, rgba(86, 214, 201, 0.07));
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }}

    .hero::before {{
      content: "";
      position: absolute;
      inset: 12px;
      border: 1px solid rgba(244, 239, 225, 0.08);
      pointer-events: none;
    }}

    .hero::after {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background:
        linear-gradient(115deg, rgba(214, 168, 79, 0.11) 0 1px, transparent 1px 28%),
        linear-gradient(90deg, transparent 0 68%, rgba(7, 8, 11, 0.42) 68% 100%);
    }}

    .hero-copy,
    .hero-art {{
      position: relative;
      z-index: 1;
    }}

    .hero-copy {{
      display: flex;
      min-width: 0;
      flex-direction: column;
      justify-content: space-between;
      gap: 22px;
    }}

    .eyebrow {{
      display: flex;
      align-items: center;
      gap: 12px;
      letter-spacing: 0.28em;
      text-transform: uppercase;
      font-size: 0.74rem;
      color: var(--gold);
      margin-bottom: 14px;
    }}

    .eyebrow::before {{
      content: "";
      width: 42px;
      height: 1px;
      background: var(--gold);
    }}

    h1 {{
      max-width: 900px;
      margin: 0;
      font-size: clamp(3rem, 7vw, 7.4rem);
      line-height: 0.88;
      font-family: "Didot", "Baskerville", "Times New Roman", serif;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
      overflow-wrap: anywhere;
    }}

    .subtitle {{
      margin: 18px 0 0;
      max-width: 780px;
      color: #c6cfde;
      font-size: clamp(1rem, 1.45vw, 1.2rem);
      line-height: 1.64;
      overflow-wrap: anywhere;
    }}

    .hero-meta {{
      display: grid;
      grid-template-columns: repeat(3, minmax(120px, 1fr)) minmax(230px, 1.35fr);
      gap: 1px;
      border: 1px solid rgba(244, 239, 225, 0.11);
      background: rgba(244, 239, 225, 0.08);
    }}

    .hero-meta span,
    .pill {{
      min-height: 54px;
      display: flex;
      align-items: center;
      border: 0;
      background: rgba(7, 8, 11, 0.52);
      padding: 12px 14px;
      color: #e6ddc8;
      font-size: 0.76rem;
      letter-spacing: 0.09em;
      text-transform: uppercase;
    }}

    .hero-meta span:last-child {{
      overflow-wrap: anywhere;
    }}

    .hero-art {{
      display: grid;
      min-width: 0;
      min-height: 360px;
      grid-template-rows: 1fr auto;
      gap: 16px;
    }}

    .classified-plate {{
      position: relative;
      overflow: hidden;
      border: 1px solid var(--line-cool);
      background:
        linear-gradient(rgba(86, 214, 201, 0.07) 1px, transparent 1px),
        linear-gradient(90deg, rgba(86, 214, 201, 0.07) 1px, transparent 1px),
        radial-gradient(circle at 52% 44%, rgba(86, 214, 201, 0.14), transparent 58%),
        rgba(6, 13, 17, 0.82);
      background-size: 34px 34px, 34px 34px, auto, auto;
      min-height: 280px;
      min-width: 0;
      margin: 0;
    }}

    .classified-plate img {{
      width: 100%;
      max-width: 100%;
      height: 100%;
      min-height: 280px;
      display: block;
      object-fit: cover;
      filter: saturate(0.82) contrast(1.06) brightness(0.84);
    }}

    .classified-plate::before {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background:
        linear-gradient(180deg, rgba(7, 8, 11, 0.02), rgba(7, 8, 11, 0.38)),
        linear-gradient(90deg, rgba(7, 8, 11, 0.28), transparent 24%, transparent 72%, rgba(7, 8, 11, 0.34)),
        radial-gradient(circle at 52% 46%, rgba(86, 214, 201, 0.16), transparent 48%);
      mix-blend-mode: screen;
    }}

    .classified-plate::after {{
      content: "";
      position: absolute;
      inset: 0;
      background: conic-gradient(from -18deg at 52% 51%, rgba(86, 214, 201, 0.36), rgba(86, 214, 201, 0.02) 42deg, transparent 82deg);
      mix-blend-mode: screen;
      opacity: 0.36;
    }}

    .plate-caption {{
      position: absolute;
      left: 16px;
      right: 16px;
      bottom: 14px;
      display: flex;
      justify-content: space-between;
      gap: 14px;
      color: var(--teal);
      font-size: 0.72rem;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      text-shadow: 0 1px 12px rgba(7, 8, 11, 0.9);
    }}

    .archive-strip {{
      display: grid;
      gap: 8px;
      padding: 14px;
      border: 1px solid rgba(214, 168, 79, 0.18);
      background: rgba(11, 10, 10, 0.68);
    }}

    .redaction-line {{
      height: 10px;
      background: linear-gradient(90deg, rgba(244, 239, 225, 0.72) 0 21%, rgba(7, 8, 11, 0.94) 21% 58%, rgba(244, 239, 225, 0.38) 58% 74%, rgba(7, 8, 11, 0.94) 74% 100%);
    }}

    .redaction-line:nth-child(2) {{
      width: 84%;
      background: linear-gradient(90deg, rgba(214, 168, 79, 0.58) 0 18%, rgba(7, 8, 11, 0.94) 18% 47%, rgba(244, 239, 225, 0.32) 47% 100%);
    }}

    .redaction-line:nth-child(3) {{
      width: 64%;
      background: linear-gradient(90deg, rgba(244, 239, 225, 0.38) 0 30%, rgba(7, 8, 11, 0.94) 30% 80%, rgba(86, 214, 201, 0.56) 80% 100%);
    }}

    .tab-strip {{
      position: sticky;
      top: 0;
      z-index: 5;
      margin-top: 14px;
      display: flex;
      flex-wrap: wrap;
      gap: 0;
      border: 1px solid rgba(244, 239, 225, 0.1);
      background: rgba(7, 8, 11, 0.78);
      backdrop-filter: blur(18px);
    }}

    .tab-button {{
      appearance: none;
      border: 0;
      border-right: 1px solid rgba(244, 239, 225, 0.1);
      background: transparent;
      color: var(--muted);
      padding: 14px 18px;
      font: inherit;
      font-size: 0.76rem;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      cursor: pointer;
      transition: background 140ms ease, color 140ms ease;
    }}

    .tab-button:hover,
    .tab-button:focus-visible {{
      outline: none;
      background: rgba(86, 214, 201, 0.08);
      color: var(--ink);
    }}

    .tab-button[aria-selected="true"] {{
      background: rgba(214, 168, 79, 0.16);
      color: var(--gold);
    }}

    .tab-panel[hidden] {{ display: none; }}

    .tab-panel > .grid,
    .tab-panel > .filters,
    .tab-panel > .panel {{
      margin-top: 18px;
    }}

    .documents-summary {{
      margin-top: 18px;
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: center;
      color: var(--muted);
      font-size: 0.92rem;
    }}

    .pagination {{
      margin-top: 18px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }}

    .pagination-summary {{
      color: var(--muted);
      font-size: 0.9rem;
    }}

    .pagination-controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      justify-content: flex-end;
    }}

    .pagination-button {{
      appearance: none;
      min-width: 42px;
      border: 1px solid rgba(244, 239, 225, 0.12);
      background: rgba(244, 239, 225, 0.06);
      color: var(--ink);
      padding: 8px 12px;
      font: inherit;
      font-size: 0.84rem;
      cursor: pointer;
      transition: background 140ms ease, border-color 140ms ease, color 140ms ease;
    }}

    .pagination-button:hover,
    .pagination-button:focus-visible {{
      outline: none;
      border-color: rgba(86, 214, 201, 0.48);
      background: rgba(86, 214, 201, 0.1);
    }}

    .pagination-button[aria-current="page"] {{
      background: rgba(214, 168, 79, 0.18);
      border-color: rgba(214, 168, 79, 0.42);
      color: var(--gold);
    }}

    .pagination-button:disabled {{
      cursor: default;
      opacity: 0.42;
    }}

    .filters,
    .panel {{
      margin-top: 18px;
      background: var(--panel);
      border: 1px solid rgba(244, 239, 225, 0.1);
      box-shadow: var(--shadow);
    }}

    .filters {{
      padding: 18px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      align-items: end;
      background:
        linear-gradient(90deg, rgba(86, 214, 201, 0.08), transparent 45%),
        var(--panel-deep);
    }}

    label {{
      display: grid;
      gap: 7px;
      font-size: 0.74rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
    }}

    input,
    select {{
      width: 100%;
      border: 1px solid rgba(244, 239, 225, 0.14);
      background: rgba(7, 8, 11, 0.62);
      color: var(--ink);
      padding: 11px 12px;
      font: inherit;
      color-scheme: dark;
    }}

    input:focus,
    select:focus {{
      outline: 1px solid rgba(86, 214, 201, 0.7);
      border-color: rgba(86, 214, 201, 0.7);
    }}

    .grid {{
      margin-top: 18px;
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 18px;
      counter-reset: dashboard-panel;
    }}

    .span-12 {{ grid-column: span 12; }}
    .span-8 {{ grid-column: span 8; }}
    .span-6 {{ grid-column: span 6; }}
    .span-4 {{ grid-column: span 4; }}

    .panel {{
      position: relative;
      padding: 19px;
      overflow: hidden;
    }}

    .panel::before {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      border-top: 2px solid rgba(214, 168, 79, 0.54);
      background:
        linear-gradient(90deg, rgba(244, 239, 225, 0.045) 1px, transparent 1px),
        linear-gradient(180deg, rgba(244, 239, 225, 0.03) 1px, transparent 1px);
      background-size: 28px 28px;
      opacity: 0.42;
    }}

    .panel > * {{
      position: relative;
      z-index: 1;
    }}

    .panel h2 {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin: 0 0 16px;
      font-size: 0.82rem;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: #d7dfec;
    }}

    .grid .panel h2::before {{
      counter-increment: dashboard-panel;
      content: counter(dashboard-panel, decimal-leading-zero);
      color: var(--gold);
      font-family: "Didot", "Baskerville", serif;
      font-size: 1.08rem;
      letter-spacing: 0;
    }}

    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(168px, 1fr));
      gap: 1px;
      border: 1px solid rgba(244, 239, 225, 0.08);
      background: rgba(244, 239, 225, 0.08);
    }}

    .metric {{
      min-height: 122px;
      padding: 16px;
      background:
        linear-gradient(180deg, rgba(244, 239, 225, 0.055), rgba(244, 239, 225, 0.02)),
        rgba(7, 8, 11, 0.58);
    }}

    .metric .value {{
      font-family: "Didot", "Baskerville", serif;
      font-size: clamp(2rem, 4vw, 3.2rem);
      color: var(--teal);
      line-height: 0.95;
    }}

    .metric .label {{
      margin-top: 12px;
      font-size: 0.74rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
    }}

    .executive-summary {{
      columns: 2 360px;
      column-gap: 34px;
      max-width: 1220px;
    }}

    .executive-summary p {{
      margin: 0 0 16px;
      break-inside: avoid;
      color: #d9deea;
      font-family: "Iowan Old Style", "Baskerville", "Times New Roman", serif;
      font-size: 1.08rem;
      line-height: 1.78;
    }}

    .executive-summary p:first-child::first-letter {{
      float: left;
      margin: 8px 10px 0 0;
      color: var(--gold);
      font-size: 4.3rem;
      line-height: 0.78;
      font-family: "Didot", "Baskerville", serif;
    }}

    .executive-summary-meta {{
      display: inline-block;
      margin-top: 2px;
      color: var(--dim);
      font-size: 0.72rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}

    .svg-wrap {{
      min-height: 280px;
      color: var(--ink);
    }}

    .svg-wrap svg text {{
      pointer-events: none;
    }}

    .chart-note,
    .muted {{ color: var(--muted); }}

    .theme-grid,
    .signal-list,
    .keyword-cloud,
    .capability-matrix {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}

    .theme-chip,
    .keyword {{
      border: 1px solid rgba(86, 214, 201, 0.18);
      background: rgba(86, 214, 201, 0.08);
      padding: 10px 12px;
    }}

    .theme-chip strong {{
      color: var(--ink);
    }}

    .signal {{
      width: 100%;
      padding: 13px 15px;
      border-left: 3px solid var(--red);
      background: rgba(7, 8, 11, 0.46);
      color: #d9deea;
      line-height: 1.55;
    }}

    .capability-matrix {{
      display: grid;
      gap: 9px;
    }}

    .capability-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) repeat(3, minmax(28px, 34px)) minmax(34px, 44px);
      gap: 6px;
      align-items: stretch;
      width: 100%;
      min-width: 0;
      padding: 8px 0;
      border-bottom: 1px solid rgba(244, 239, 225, 0.09);
    }}

    .capability-row.header {{
      padding-top: 0;
      color: var(--muted);
      font-size: 0.68rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}

    .capability-label {{
      min-width: 0;
      display: grid;
      gap: 3px;
    }}

    .capability-label strong {{
      color: var(--ink);
      font-size: 0.84rem;
      line-height: 1.24;
      overflow-wrap: anywhere;
    }}

    .capability-label span {{
      color: var(--muted);
      font-size: 0.7rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .capability-cell,
    .capability-total {{
      min-height: 34px;
      display: flex;
      align-items: center;
      justify-content: center;
      border: 1px solid rgba(244, 239, 225, 0.1);
      background: rgba(244, 239, 225, 0.04);
      color: var(--ink);
      font-size: 0.78rem;
      font-weight: 700;
    }}

    .capability-cell {{
      background: rgba(86, 214, 201, var(--intensity, 0.06));
      border-color: rgba(86, 214, 201, 0.18);
    }}

    .capability-total {{
      color: var(--gold);
      background: rgba(214, 168, 79, 0.1);
      border-color: rgba(214, 168, 79, 0.2);
    }}

    .keyword {{
      font-size: calc(0.78rem + var(--scale, 0) * 0.5rem);
      background: rgba(214, 168, 79, 0.1);
      border-color: rgba(214, 168, 79, 0.22);
    }}

    .hotspot {{
      width: 100%;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px solid rgba(244, 239, 225, 0.1);
      padding: 11px 0;
      color: #dde5ef;
    }}

    .hotspot strong {{
      color: var(--gold);
    }}

    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 0.94rem;
    }}

    th:nth-child(1) {{ width: 28%; }}
    th:nth-child(2) {{ width: 22%; }}
    th:nth-child(3) {{ width: 20%; }}
    th:nth-child(4) {{ width: 30%; }}

    th,
    td {{
      text-align: left;
      padding: 14px 12px;
      border-bottom: 1px solid rgba(244, 239, 225, 0.1);
      vertical-align: top;
    }}

    tr:hover td {{
      background: rgba(86, 214, 201, 0.04);
    }}

    th {{
      letter-spacing: 0.13em;
      text-transform: uppercase;
      font-size: 0.7rem;
      color: var(--gold);
      position: sticky;
      top: 47px;
      background: rgba(10, 13, 18, 0.98);
      z-index: 2;
    }}

    td {{
      color: #d6dce7;
      line-height: 1.55;
    }}

    .doc-title {{
      margin: 0;
      font-size: 1rem;
      font-weight: 700;
    }}

    .doc-title a {{
      color: var(--ink);
      text-decoration: none;
      border-bottom: 1px solid rgba(214, 168, 79, 0.42);
    }}

    .doc-title a:hover,
    .doc-title a:focus-visible {{
      color: var(--teal);
      outline: none;
      border-bottom-color: var(--teal);
    }}

    .source-title-button {{
      appearance: none;
      border: 0;
      border-bottom: 1px solid rgba(214, 168, 79, 0.42);
      border-radius: 0;
      background: transparent;
      color: var(--ink);
      padding: 0;
      font: inherit;
      font-weight: 700;
      line-height: 1.35;
      text-align: left;
      cursor: pointer;
    }}

    .source-title-button:hover,
    .source-title-button:focus-visible {{
      color: var(--teal);
      outline: none;
      border-bottom-color: var(--teal);
    }}

    .source-preview {{
      margin-top: 12px;
      border: 1px solid rgba(244, 239, 225, 0.12);
      background: rgba(7, 8, 11, 0.34);
    }}

    .source-preview summary {{
      cursor: pointer;
      padding: 8px 10px;
      color: var(--gold);
      font-size: 0.72rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      list-style-position: inside;
      user-select: none;
    }}

    .source-preview summary:hover,
    .source-preview summary:focus-visible {{
      outline: none;
      color: var(--teal);
    }}

    .source-preview-link {{
      display: grid;
      gap: 8px;
      padding: 0 10px 10px;
      color: var(--muted);
      text-decoration: none;
      font-size: 0.78rem;
    }}

    .source-preview-link:hover,
    .source-preview-link:focus-visible {{
      color: var(--teal);
      outline: none;
    }}

    .source-thumbnail {{
      width: 100%;
      aspect-ratio: 16 / 9;
      object-fit: cover;
      border: 1px solid rgba(86, 214, 201, 0.16);
      background: rgba(244, 239, 225, 0.05);
    }}

    .tag-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 9px;
    }}

    .tag {{
      background: rgba(86, 214, 201, 0.08);
      border: 1px solid rgba(86, 214, 201, 0.16);
      color: #c8d6e3;
      padding: 4px 8px;
      font-size: 0.72rem;
      letter-spacing: 0.04em;
    }}

    .profile-text {{
      color: #d6dce7;
    }}

    .related-sources {{
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px solid rgba(244, 239, 225, 0.08);
      color: var(--muted);
      font-size: 0.78rem;
      line-height: 1.45;
    }}

    .related-sources strong {{
      display: block;
      margin-bottom: 5px;
      color: var(--gold);
      font-size: 0.68rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}

    .related-source-links {{
      display: grid;
      gap: 5px;
    }}

    .related-source-links a {{
      color: #b9c5d6;
      text-decoration: none;
      border-bottom: 1px solid rgba(86, 214, 201, 0.18);
      overflow-wrap: anywhere;
      transition: color 140ms ease, border-color 140ms ease;
    }}

    .related-source-links a:hover,
    .related-source-links a:focus-visible {{
      color: var(--teal);
      border-bottom-color: rgba(86, 214, 201, 0.72);
      outline: none;
    }}

    .classification-badge {{
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border: 1px solid rgba(244, 239, 225, 0.18);
      font-size: 0.74rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      background: rgba(244, 239, 225, 0.08);
      color: var(--ink);
    }}

    .classification-badge.category-one {{
      background: rgba(200, 78, 83, 0.18);
      border-color: rgba(200, 78, 83, 0.44);
      color: #f0b1b5;
    }}

    .classification-badge.category-two {{
      background: rgba(214, 168, 79, 0.14);
      border-color: rgba(214, 168, 79, 0.34);
      color: #efd38b;
    }}

    .classification-badge.category-three {{
      background: rgba(86, 214, 201, 0.1);
      border-color: rgba(86, 214, 201, 0.26);
      color: #b7f2ed;
    }}

    .classification-cell strong {{
      display: block;
      margin-bottom: 6px;
    }}

    .classification-note {{
      margin-top: 12px;
      font-size: 0.83rem;
      color: var(--muted);
      line-height: 1.58;
    }}

    .chart-legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
      margin-top: 10px;
      color: var(--muted);
      font-size: 0.72rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .chart-legend span {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
    }}

    .chart-legend i {{
      width: 10px;
      height: 10px;
      display: inline-block;
      border: 1px solid rgba(244, 239, 225, 0.18);
    }}

    .chart-click-target {{
      cursor: pointer;
    }}

    .chart-click-target:hover rect,
    .chart-click-target:hover circle,
    .chart-click-target:hover.hotspot,
    .chart-click-target:focus-visible rect {{
      opacity: 1;
      stroke: rgba(244, 239, 225, 0.72);
      stroke-width: 1.4;
    }}

    .theme-chip.chart-click-target:hover,
    .theme-chip.chart-click-target:focus-visible {{
      border-color: rgba(244, 239, 225, 0.42);
    }}

    .signal.chart-click-target:hover,
    .signal.chart-click-target:focus-visible {{
      border-left-color: var(--gold);
    }}

    .capability-row.chart-click-target:hover,
    .capability-row.chart-click-target:focus-visible {{
      border-bottom-color: rgba(86, 214, 201, 0.34);
    }}

    .chart-click-target:focus-visible circle {{
      stroke: rgba(244, 239, 225, 0.88);
      stroke-width: 2.2;
    }}

    .chart-click-target:hover path,
    .chart-click-target:focus-visible path {{
      stroke: rgba(244, 239, 225, 0.82);
      stroke-width: 1.4;
    }}

    .chart-click-target:focus-visible {{
      outline: none;
    }}

    .table-wrap {{
      overflow: visible;
    }}

    .footer-note {{
      margin-top: 14px;
      font-size: 0.84rem;
      color: var(--muted);
    }}

    .site-footer {{
      margin: 24px 0 0;
      padding: 18px 0 8px;
      border-top: 1px solid rgba(244, 239, 225, 0.12);
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      align-items: start;
      color: var(--muted);
      font-size: 0.84rem;
      line-height: 1.62;
    }}

    .site-footer strong {{
      color: var(--ink);
      font-weight: 600;
    }}

    .site-footer a {{
      color: var(--ink);
      text-decoration: none;
      border-bottom: 1px solid rgba(214, 168, 79, 0.48);
    }}

    .site-footer a:hover,
    .site-footer a:focus-visible {{
      color: var(--teal);
      outline: none;
      border-bottom-color: var(--teal);
    }}

    .footer-kicker {{
      margin-bottom: 6px;
      color: var(--gold);
      font-size: 0.68rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }}

    .source-detail-dialog {{
      width: min(1120px, calc(100vw - 32px));
      max-height: min(860px, calc(100vh - 32px));
      border: 1px solid rgba(214, 168, 79, 0.38);
      background: rgba(9, 12, 17, 0.98);
      color: var(--ink);
      padding: 0;
      overflow: hidden;
      box-shadow: 0 34px 90px rgba(0, 0, 0, 0.62);
    }}

    .source-detail-dialog::backdrop {{
      background:
        linear-gradient(180deg, rgba(7, 8, 11, 0.56), rgba(7, 8, 11, 0.86)),
        rgba(0, 0, 0, 0.6);
      backdrop-filter: blur(6px);
    }}

    .source-dialog-shell {{
      max-height: inherit;
      overflow: auto;
      scrollbar-gutter: stable;
      scrollbar-color: rgba(214, 168, 79, 0.72) rgba(244, 239, 225, 0.08);
      scrollbar-width: thin;
    }}

    .source-dialog-shell::-webkit-scrollbar {{
      width: 12px;
    }}

    .source-dialog-shell::-webkit-scrollbar-track {{
      background: rgba(244, 239, 225, 0.06);
      border-left: 1px solid rgba(244, 239, 225, 0.1);
    }}

    .source-dialog-shell::-webkit-scrollbar-thumb {{
      background: linear-gradient(180deg, rgba(214, 168, 79, 0.82), rgba(86, 214, 201, 0.54));
      border: 3px solid rgba(9, 12, 17, 0.98);
    }}

    .source-dialog-shell::-webkit-scrollbar-thumb:hover {{
      background: linear-gradient(180deg, rgba(238, 197, 106, 0.95), rgba(86, 214, 201, 0.72));
    }}

    .source-dialog-header {{
      position: sticky;
      top: 0;
      z-index: 3;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      align-items: start;
      padding: 22px 24px 18px;
      border-bottom: 1px solid rgba(244, 239, 225, 0.12);
      background: rgba(9, 12, 17, 0.96);
      backdrop-filter: blur(14px);
    }}

    .source-dialog-kicker {{
      color: var(--gold);
      font-size: 0.68rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }}

    .source-dialog-header h2 {{
      margin: 7px 0 0;
      font-size: clamp(1.35rem, 2.4vw, 2.25rem);
      line-height: 1.08;
      font-family: "Didot", "Baskerville", "Times New Roman", serif;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }}

    .source-dialog-subtitle {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 0.86rem;
      overflow-wrap: anywhere;
    }}

    .source-dialog-close {{
      appearance: none;
      width: 38px;
      height: 38px;
      border: 1px solid rgba(244, 239, 225, 0.16);
      background: rgba(244, 239, 225, 0.06);
      color: var(--ink);
      font: inherit;
      font-size: 1.2rem;
      line-height: 1;
      cursor: pointer;
    }}

    .source-dialog-close:hover,
    .source-dialog-close:focus-visible {{
      outline: none;
      border-color: rgba(86, 214, 201, 0.48);
      background: rgba(86, 214, 201, 0.1);
      color: var(--teal);
    }}

    .source-dialog-body {{
      display: grid;
      grid-template-columns: minmax(260px, 340px) minmax(0, 1fr);
      gap: 22px;
      padding: 22px 24px 26px;
    }}

    .source-dialog-aside,
    .source-dialog-main {{
      min-width: 0;
    }}

    .source-dialog-aside {{
      display: grid;
      align-content: start;
      gap: 14px;
    }}

    .source-dialog-figure {{
      margin: 0;
      border: 1px solid rgba(86, 214, 201, 0.18);
      background: rgba(7, 8, 11, 0.48);
    }}

    .source-dialog-image {{
      display: block;
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: cover;
      background: rgba(244, 239, 225, 0.05);
    }}

    .source-dialog-figure figcaption {{
      padding: 10px 12px;
      color: var(--muted);
      font-size: 0.78rem;
      line-height: 1.45;
    }}

    .source-dialog-placeholder {{
      min-height: 220px;
      display: grid;
      place-items: center;
      padding: 22px;
      border: 1px solid rgba(244, 239, 225, 0.1);
      background:
        linear-gradient(rgba(86, 214, 201, 0.07) 1px, transparent 1px),
        linear-gradient(90deg, rgba(214, 168, 79, 0.08) 1px, transparent 1px),
        rgba(7, 8, 11, 0.48);
      background-size: 18px 18px;
      color: var(--muted);
      text-align: center;
      font-size: 0.88rem;
    }}

    .source-dialog-link-list {{
      display: grid;
      gap: 8px;
    }}

    .source-dialog-link-list a,
    .related-detail-item button {{
      border: 1px solid rgba(244, 239, 225, 0.12);
      background: rgba(244, 239, 225, 0.05);
      color: var(--ink);
      padding: 9px 11px;
      text-decoration: none;
      font: inherit;
      font-size: 0.84rem;
      line-height: 1.35;
      text-align: left;
      cursor: pointer;
    }}

    .source-dialog-link-list a:hover,
    .source-dialog-link-list a:focus-visible,
    .related-detail-item button:hover,
    .related-detail-item button:focus-visible {{
      outline: none;
      border-color: rgba(86, 214, 201, 0.48);
      color: var(--teal);
      background: rgba(86, 214, 201, 0.08);
    }}

    .source-dialog-main {{
      display: grid;
      gap: 14px;
    }}

    .source-detail-section {{
      border: 1px solid rgba(244, 239, 225, 0.1);
      background: rgba(244, 239, 225, 0.035);
      padding: 15px;
    }}

    .source-detail-section h3 {{
      margin: 0 0 10px;
      color: var(--gold);
      font-size: 0.74rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }}

    .source-detail-section p {{
      margin: 0;
      color: #d6dce7;
      line-height: 1.62;
    }}

    .source-detail-section p + p {{
      margin-top: 10px;
    }}

    .source-dialog-meta-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}

    .source-dialog-data-list {{
      display: grid;
      grid-template-columns: minmax(110px, 0.38fr) minmax(0, 1fr);
      gap: 7px 12px;
      margin: 0;
    }}

    .source-dialog-data-list div {{
      display: contents;
    }}

    .source-dialog-data-list dt {{
      color: var(--muted);
      font-size: 0.73rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .source-dialog-data-list dd {{
      margin: 0;
      color: #dde5ef;
      overflow-wrap: anywhere;
    }}

    .source-dialog-chip-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
    }}

    .source-dialog-chip {{
      border: 1px solid rgba(86, 214, 201, 0.18);
      background: rgba(86, 214, 201, 0.08);
      color: #d6dce7;
      padding: 5px 8px;
      font-size: 0.75rem;
    }}

    .capability-detail-list,
    .related-detail-list {{
      display: grid;
      gap: 9px;
    }}

    .capability-detail-item,
    .related-detail-item {{
      border-top: 1px solid rgba(244, 239, 225, 0.08);
      padding-top: 9px;
      color: var(--muted);
      font-size: 0.86rem;
      line-height: 1.5;
    }}

    .capability-detail-item:first-child,
    .related-detail-item:first-child {{
      border-top: 0;
      padding-top: 0;
    }}

    .capability-detail-item strong,
    .related-detail-item strong {{
      display: block;
      color: var(--ink);
      font-size: 0.92rem;
    }}

    .related-detail-item button {{
      width: 100%;
      margin-bottom: 7px;
    }}

    @media (max-width: 1120px) {{
      .hero {{
        grid-template-columns: 1fr;
      }}

      .hero-art {{
        min-height: 280px;
      }}

      .span-8,
      .span-6,
      .span-4 {{ grid-column: span 12; }}

      .source-dialog-body {{
        grid-template-columns: 1fr;
      }}

      .source-dialog-aside {{
        grid-template-columns: minmax(240px, 0.48fr) minmax(0, 1fr);
      }}
    }}

    @media (max-width: 720px) {{
      .shell {{
        width: min(100vw - 18px, 100%);
        margin-top: 10px;
      }}

      .hero {{
        min-height: auto;
        padding: 22px 18px 18px;
      }}

      h1 {{
        font-size: 3.55rem;
        line-height: 0.92;
      }}

      .hero-meta {{
        grid-template-columns: 1fr 1fr;
      }}

      .tab-strip {{
        position: static;
      }}

      .tab-button {{
        flex: 1 1 50%;
        border-bottom: 1px solid rgba(244, 239, 225, 0.1);
      }}

      .panel {{
        padding: 14px;
      }}

      .documents-summary {{
        align-items: flex-start;
        flex-direction: column;
      }}

      .site-footer {{
        grid-template-columns: 1fr;
        gap: 12px;
      }}

      .table-wrap table,
      .table-wrap thead,
      .table-wrap tbody,
      .table-wrap tr,
      .table-wrap td {{
        display: block;
        width: 100%;
      }}

      .table-wrap thead {{
        display: none;
      }}

      .table-wrap tbody {{
        display: grid;
        gap: 14px;
      }}

      .table-wrap tr {{
        border: 1px solid rgba(244, 239, 225, 0.12);
        background: rgba(7, 8, 11, 0.42);
        padding: 14px;
      }}

      .table-wrap tr:hover td {{
        background: transparent;
      }}

      .table-wrap td {{
        border-bottom: 0;
        padding: 0;
      }}

      .table-wrap td + td {{
        margin-top: 13px;
        padding-top: 12px;
        border-top: 1px solid rgba(244, 239, 225, 0.08);
      }}

      .table-wrap td::before {{
        content: attr(data-label);
        display: block;
        margin-bottom: 6px;
        color: var(--gold);
        font-size: 0.68rem;
        letter-spacing: 0.13em;
        text-transform: uppercase;
      }}

      .source-detail-dialog {{
        width: calc(100vw - 18px);
        max-height: calc(100vh - 18px);
      }}

      .source-dialog-header {{
        padding: 18px 16px 15px;
      }}

      .source-dialog-body {{
        padding: 16px;
      }}

      .source-dialog-aside,
      .source-dialog-meta-grid {{
        grid-template-columns: 1fr;
      }}

      .source-dialog-data-list {{
        grid-template-columns: 1fr;
      }}
    }}

    @media (max-width: 500px) {{
      h1 {{
        font-size: 3rem;
      }}

      .hero-meta {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class=\"shell\">
    <section class=\"hero\">
      <div class=\"hero-copy\">
        <div>
          <div class=\"eyebrow\">Research Dashboard</div>
          <h1>UAP/UFO Research</h1>
          <p class="subtitle">This dashboard organizes the public war.gov/ufo archive of unresolved UAP records released by the Department of War with ODNI support. The collection spans decades of historical government files, many reviewed from paper records, and the cases remain unresolved because the government has not made a definitive determination about the observed phenomena.</p>
        </div>
        <div class=\"hero-meta\">
          <span id=\"metaDocs\"></span>
          <span id=\"metaPages\"></span>
          <span id=\"metaYears\"></span>
          <span id=\"metaGenerated\"></span>
        </div>
      </div>
      <div class=\"hero-art\" aria-hidden=\"true\">
        <figure class=\"classified-plate\">
          <img src=\"assets/hero-classified-saucer.png\" alt=\"\" loading=\"eager\">
          <div class=\"plate-caption\">
            <span>Classified optics</span>
            <span>Archive signal</span>
          </div>
        </figure>
        <div class=\"archive-strip\">
          <div class=\"redaction-line\"></div>
          <div class=\"redaction-line\"></div>
          <div class=\"redaction-line\"></div>
        </div>
      </div>
    </section>

    <div class=\"tab-strip\" role=\"tablist\" aria-label=\"Dashboard sections\">
      <button class=\"tab-button\" id=\"tab-overview\" type=\"button\" role=\"tab\" aria-selected=\"true\" aria-controls=\"panel-overview\" data-tab=\"overview\">Analysis</button>
      <button class=\"tab-button\" id=\"tab-documents\" type=\"button\" role=\"tab\" aria-selected=\"false\" aria-controls=\"panel-documents\" data-tab=\"documents\" tabindex=\"-1\">Sources</button>
    </div>

    <section class=\"tab-panel\" id=\"panel-overview\" role=\"tabpanel\" aria-labelledby=\"tab-overview\">
      <div class=\"grid\">
        <section class=\"panel span-12\">
          <h2>Corpus Summary</h2>
          <div id=\"metrics\" class=\"metrics\"></div>
        </section>

        <section class=\"panel span-8\">
          <h2>Evidence Over Time</h2>
          <div id=\"evidenceTimelineChart\" class=\"svg-wrap\"></div>
        </section>

        <section class=\"panel span-4\">
          <h2>Research Signals</h2>
          <div id=\"researchSignals\" class=\"signal-list\"></div>
        </section>

        <section class="panel span-4">
          <h2>Evidence Classification</h2>
          <div id="classificationChart" class="svg-wrap"></div>
          <div class="classification-note">Category One marks detailed event files with eyewitness depth and at least one hard-evidence signal. Category Two captures partially corroborated events. Category Three marks files with limited support or sparse witness material.</div>
        </section>

        <section class=\"panel span-8\">
          <h2>Geolocation Constellation</h2>
          <div id=\"mapChart\" class=\"svg-wrap\"></div>
        </section>

        <section class=\"panel span-4\">
          <h2>Observed Capabilities</h2>
          <div id=\"capabilityMatrix\" class=\"capability-matrix\"></div>
        </section>

        <section class=\"panel span-4\">
          <h2>Source Types</h2>
          <div id=\"typeChart\" class=\"svg-wrap\"></div>
        </section>

        <section class=\"panel span-4\">
          <h2>Organizations Mentioned</h2>
          <div id=\"organizationChart\" class=\"svg-wrap\"></div>
        </section>

        <section class=\"panel span-4\">
          <h2>Resolution Status</h2>
          <div id=\"resolutionChart\" class=\"svg-wrap\"></div>
        </section>

        <section class=\"panel span-4\">
          <h2>Observation Mode Matrix</h2>
          <div id=\"observationModeMatrix\" class=\"svg-wrap\"></div>
        </section>

        <section class=\"panel span-4\">
          <h2>Corroboration vs Category</h2>
          <div id=\"corroborationCategoryMatrix\" class=\"svg-wrap\"></div>
        </section>

        <section class=\"panel span-6\">
          <h2>Phenomenology Matrix</h2>
          <div id=\"phenomenologyMatrix\" class=\"svg-wrap\"></div>
        </section>

        <section class=\"panel span-6\">
          <h2>Related Source Network</h2>
          <div id=\"relatedSourceNetwork\" class=\"svg-wrap\"></div>
        </section>

        <section class=\"panel span-12\">
          <h2>Recurring Themes</h2>
          <div id=\"themeGrid\" class=\"theme-grid\"></div>
        </section>

        <section class=\"panel span-12\">
          <h2>Executive Summary</h2>
          <div id=\"executiveSummary\" class=\"executive-summary\"></div>
        </section>
      </div>
    </section>

    <section class=\"tab-panel\" id=\"panel-documents\" role=\"tabpanel\" aria-labelledby=\"tab-documents\" hidden>
      <section class=\"filters\">
        <label>Keyword Search
          <input id=\"searchInput\" type=\"search\" placeholder=\"Search titles, summaries, themes, agencies\">
        </label>
        <label>Source Type
          <select id=\"typeFilter\"></select>
        </label>
        <label>Extraction Method
          <select id=\"methodFilter\"></select>
        </label>
        <label>Evidence Category
          <select id="categoryFilter"></select>
        </label>
        <label>Observed Capability
          <select id="capabilityFilter"></select>
        </label>
        <label>Geography
          <select id=\"placeFilter\"></select>
        </label>
        <label>Year From
          <input id=\"yearMin\" type=\"number\" inputmode=\"numeric\">
        </label>
        <label>Year To
          <input id=\"yearMax\" type=\"number\" inputmode=\"numeric\">
        </label>
      </section>

      <div class=\"documents-summary\">
          <div>Refine the register here without changing the Analysis tab, which always shows the full corpus.</div>
        <strong id=\"documentCount\"></strong>
      </div>

      <section class=\"panel\" id=\"documentRegister\">
        <h2>Source Register</h2>
        <div class=\"table-wrap\">
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th>Classification</th>
                <th>Profile</th>
                <th>Summary Narrative</th>
              </tr>
            </thead>
            <tbody id=\"documentRows\"></tbody>
          </table>
        </div>
        <div class=\"pagination\">
          <div id=\"paginationSummary\" class=\"pagination-summary\"></div>
          <div id=\"paginationControls\" class=\"pagination-controls\"></div>
        </div>
        <div class=\"footer-note\">Rows marked with OCR page counts used image-based text recovery on low-text pages. Image and video rows use local media metadata plus release manifest narrative where available.</div>
      </section>
    </section>

    <footer class=\"site-footer\">
      <div>
        <div class=\"footer-kicker\">AI Processing Note</div>
        <div>This dashboard uses AI to process public UAP/UFO records by extracting text with native PDF parsing and OCR where needed, reading local image/video metadata, then applying local AI review to summarize sources, classify evidence strength, identify themes, agencies, dates, and locations, and surface corpus-level research signals.</div>
      </div>
      <div>Maintained by <strong><a href=\"https://rittmuller.com\" target=\"_blank\" rel=\"noopener noreferrer\">Robert Rittmuller</a></strong>.</div>
    </footer>
  </div>

  <dialog class=\"source-detail-dialog\" id=\"sourceDetailDialog\" aria-labelledby=\"sourceDialogTitle\"></dialog>

  <script id=\"analysis-data\" type=\"application/json\">__DATA_JSON__</script>
  <script id=\"world-land-data\" type=\"application/json\">__WORLD_LAND_JSON__</script>
  <script>
    const DOCUMENTS_PER_PAGE = 10;
    const analysis = JSON.parse(document.getElementById('analysis-data').textContent);
    const worldLand = JSON.parse(document.getElementById('world-land-data').textContent);
    const documents = analysis.documents.slice().sort((a, b) => (b.year || 0) - (a.year || 0) || a.title.localeCompare(b.title));
    const state = {
      activeTab: 'overview',
      page: 1,
      search: '',
      type: 'all',
      method: 'all',
      category: 'all',
      capability: 'all',
      place: 'all',
      yearMin: analysis.year_min || 1900,
      yearMax: analysis.year_max || new Date().getFullYear(),
    };
    const chartColors = {
      ink: '#f4efe1',
      muted: '#9aa5b8',
      dim: '#667085',
      gold: '#d6a84f',
      teal: '#56d6c9',
      red: '#c84e53',
      green: '#9bb46e',
      blue: '#7d9cff',
      grid: 'rgba(244,239,225,0.12)',
      land: 'rgba(214,168,79,0.16)',
      mapPanel: 'rgba(6,13,17,0.72)',
    };
    const categoryPalette = {
      'Category One': chartColors.red,
      'Category Two': chartColors.gold,
      'Category Three': chartColors.teal,
    };
    const trendFields = [
      'event_record_type',
      'primary_observation_mode',
      'sensor_platform',
      'observer_platform',
      'observer_roles',
      'witness_count_bucket',
      'corroboration_types',
      'chain_of_custody_quality',
      'redaction_level',
      'event_time_precision',
      'location_precision',
      'day_night_context',
      'object_count_bucket',
      'morphology_normalized',
      'color_luminosity_normalized',
      'apparent_motion_class',
      'mundane_explanation_present',
      'resolution_status',
      'measurement_quality',
      'quantitative_fields_present',
    ];

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
    }

    function countBy(items, keyFn) {
      const counts = new Map();
      items.forEach((item) => {
        const key = keyFn(item);
        if (!key) return;
        counts.set(key, (counts.get(key) || 0) + 1);
      });
      return [...counts.entries()].map(([label, count]) => ({ label, count }));
    }

    function trendValues(doc, field) {
      const value = doc[field];
      if (Array.isArray(value)) return value.filter(Boolean);
      return value === null || value === undefined || value === '' ? [] : [value];
    }

    function trendSearchValues(doc) {
      return trendFields.flatMap((field) => trendValues(doc, field)).flatMap((value) => [value, humanizeToken(value)]);
    }

    function countTrendValues(items, field, options = {}) {
      const excluded = new Set(options.exclude || []);
      const counts = new Map();
      items.forEach((doc) => {
        trendValues(doc, field).forEach((value) => {
          if (excluded.has(value)) return;
          counts.set(value, (counts.get(value) || 0) + 1);
        });
      });
      return [...counts.entries()]
        .map(([key, count]) => ({ key, label: humanizeToken(key), count }))
        .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
    }

    function chartLegend(entries) {
      return `<div class="chart-legend">${entries.map((entry) => `
        <span><i style="background:${escapeHtml(entry.color)}"></i>${escapeHtml(entry.label)}</span>`).join('')}</div>`;
    }

    function sourceHref(doc) {
      return doc.original_source_url || doc.source_href || doc.source_page_url || '#';
    }

    function sourcePreviewLabel(doc) {
      if (doc.media_type === 'video') return 'Video thumbnail';
      if (doc.media_type === 'audio') return 'Audio thumbnail';
      if (doc.media_type === 'image') return 'Image thumbnail';
      return 'Source thumbnail';
    }

    function formatExtractionMethod(value) {
      return String(value || '')
        .replaceAll('_', ' ')
        .replace(/\\b\\w/g, (letter) => letter.toUpperCase());
    }

    function hasDetailValue(value) {
      if (value === null || value === undefined) return false;
      if (typeof value === 'string') return value.trim().length > 0;
      if (Array.isArray(value)) return value.length > 0;
      if (typeof value === 'object') return Object.keys(value).length > 0;
      return true;
    }

    function humanizeToken(value) {
      return String(value ?? '')
        .replaceAll('_', ' ')
        .replace(/\\s+/g, ' ')
        .trim()
        .replace(/\\b\\w/g, (letter) => letter.toUpperCase());
    }

    function formatDurationSeconds(value) {
      if (typeof value !== 'number' || !Number.isFinite(value)) return '';
      const rounded = Math.round(value);
      const minutes = Math.floor(rounded / 60);
      const seconds = rounded % 60;
      if (minutes >= 60) {
        const hours = Math.floor(minutes / 60);
        const remainingMinutes = minutes % 60;
        return `${hours} hr ${remainingMinutes} min`;
      }
      return minutes ? `${minutes} min ${seconds} sec` : `${seconds} sec`;
    }

    function formatDetailValue(value, key = '') {
      if (Array.isArray(value)) {
        return value.map((item) => formatDetailValue(item)).filter(Boolean).join(', ');
      }
      if (typeof value === 'boolean') {
        return value ? 'Yes' : 'No';
      }
      if (typeof value === 'number') {
        if (key === 'duration_seconds') return formatDurationSeconds(value);
        if (key.endsWith('_bytes')) return `${value.toLocaleString()} bytes`;
        return value.toLocaleString();
      }
      if (typeof value === 'object' && value) {
        if (value.label) {
          const coords = typeof value.latitude === 'number' && typeof value.longitude === 'number'
            ? ` (${value.latitude.toFixed(3)}, ${value.longitude.toFixed(3)})`
            : '';
          return `${value.label}${coords}`;
        }
        return Object.entries(value)
          .filter(([, nestedValue]) => hasDetailValue(nestedValue))
          .map(([nestedKey, nestedValue]) => `${humanizeToken(nestedKey)}: ${formatDetailValue(nestedValue, nestedKey)}`)
          .join(' · ');
      }
      const text = String(value ?? '').trim();
      return text.includes('_') ? humanizeToken(text) : text;
    }

    function renderDetailRows(rows) {
      const rendered = rows
        .filter(([, value]) => hasDetailValue(value))
        .map(([label, value, key]) => `
          <div>
            <dt>${escapeHtml(label)}</dt>
            <dd>${escapeHtml(formatDetailValue(value, key))}</dd>
          </div>`)
        .join('');
      return rendered ? `<dl class="source-dialog-data-list">${rendered}</dl>` : '';
    }

    function renderDetailSection(title, content) {
      return content
        ? `<section class="source-detail-section"><h3>${escapeHtml(title)}</h3>${content}</section>`
        : '';
    }

    function renderChipRow(values) {
      const chips = (values || [])
        .filter(Boolean)
        .map((value) => `<span class="source-dialog-chip">${escapeHtml(formatDetailValue(value))}</span>`)
        .join('');
      return chips ? `<div class="source-dialog-chip-row">${chips}</div>` : '';
    }

    function mediaProfileText(doc) {
      if (doc.media_type === 'image') {
        const meta = doc.media_metadata || {};
        return meta.image_width && meta.image_height ? `${meta.image_width} x ${meta.image_height} image` : 'Image file';
      }
      if (doc.media_type === 'video' || doc.media_type === 'audio') {
        const meta = doc.media_metadata || {};
        const fallbackLabel = doc.media_type === 'audio' ? 'Audio file' : 'Video file';
        const duration = typeof meta.duration_seconds === 'number' ? formatDurationSeconds(meta.duration_seconds) : fallbackLabel;
        const dimensions = meta.video_width && meta.video_height ? `${meta.video_width} x ${meta.video_height}` : '';
        return dimensions ? `${duration} · ${dimensions}` : duration;
      }
      return `${doc.page_count || 0} page${doc.page_count === 1 ? '' : 's'}`;
    }

    function isVisualSource(doc) {
      if (doc.media_type === 'image' || doc.media_type === 'video') return true;
      if (doc.media_type === 'audio') return false;
      return doc.document_type === 'Imagery' || (doc.themes || []).includes('Imagery and Visuals');
    }

    function sourceContextSection(doc) {
      if (doc.media_type === 'audio') {
        const source = doc.audio_source_characterization
          ? `${doc.audio_source_characterization}. `
          : '';
        const summary = doc.audio_transcript_summary || 'No usable audio transcript summary was generated for this source.';
        return `<div class="muted"><strong>Audio:</strong> ${escapeHtml(source + summary)}</div>`;
      }

      if (isVisualSource(doc) && doc.visual_observations) {
        return `<div class="muted"><strong>Visual:</strong> ${escapeHtml(doc.visual_observations)}</div>`;
      }

      if (doc.audio_transcript_summary) {
        const source = doc.audio_source_characterization
          ? `${doc.audio_source_characterization}. `
          : '';
        return `<div class="muted"><strong>Audio:</strong> ${escapeHtml(source + doc.audio_transcript_summary)}</div>`;
      }

      const attributes = [];
      if (doc.document_type) attributes.push(doc.document_type);
      if (doc.page_count) attributes.push(`${doc.page_count} page${doc.page_count === 1 ? '' : 's'}`);
      if (doc.extraction_method) attributes.push(formatExtractionMethod(doc.extraction_method));
      if (doc.review_source) attributes.push(`reviewed via ${doc.review_source}`);
      const provenance = doc.original_source_url || doc.source_page_url
        ? 'Source provenance is linked from the title.'
        : 'Source provenance is the local archive file.';
      const description = attributes.length
        ? `${attributes.join(' · ')}. ${provenance}`
        : provenance;
      return `<div class="muted"><strong>Document:</strong> ${escapeHtml(description)}</div>`;
    }

    let sourceDialogReturnFocus = null;

    function sourceDialogLinks(doc) {
      const links = [
        ['Open source', sourceHref(doc)],
        ['Original release file', doc.original_source_url],
        ['Source page', doc.source_page_url],
        ['Thumbnail image', doc.thumbnail_url],
      ];
      const seen = new Set();
      const rendered = links
        .filter(([, href]) => href && href !== '#' && !seen.has(href) && seen.add(href))
        .map(([label, href]) => `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`)
        .join('');
      return rendered ? `<nav class="source-dialog-link-list" aria-label="Source links">${rendered}</nav>` : '';
    }

    function sourceDialogMedia(doc) {
      if (!doc.thumbnail_url) {
        return `<div class="source-dialog-placeholder">${escapeHtml(sourcePreviewLabel(doc))} unavailable</div>`;
      }
      const href = sourceHref(doc);
      const image = `<img class="source-dialog-image" src="${escapeHtml(doc.thumbnail_url)}" alt="${escapeHtml(sourcePreviewLabel(doc))} for ${escapeHtml(doc.title)}">`;
      return `
        <figure class="source-dialog-figure">
          ${href && href !== '#'
            ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${image}</a>`
            : image}
          <figcaption>${escapeHtml(sourcePreviewLabel(doc))}</figcaption>
        </figure>`;
    }

    function sourceDialogSummary(doc) {
      const summary = doc.summary_narrative
        ? `<p>${escapeHtml(doc.summary_narrative)}</p>`
        : '<p class="muted">No summary narrative available.</p>';
      return renderDetailSection('Summary', summary);
    }

    function sourceDialogObservationSection(doc) {
      const paragraphs = [];
      if (doc.visual_observations) {
        paragraphs.push(`<p><strong>Visual:</strong> ${escapeHtml(doc.visual_observations)}</p>`);
      }
      if (doc.audio_transcript_summary || doc.audio_source_characterization) {
        const source = doc.audio_source_characterization ? `${doc.audio_source_characterization}. ` : '';
        const summary = doc.audio_transcript_summary || 'No usable audio transcript summary was generated for this source.';
        paragraphs.push(`<p><strong>Audio:</strong> ${escapeHtml(source + summary)}</p>`);
      }
      if (doc.media_authenticity_notes) {
        paragraphs.push(`<p><strong>Authenticity notes:</strong> ${escapeHtml(doc.media_authenticity_notes)}</p>`);
      }
      return renderDetailSection('Observations', paragraphs.join(''));
    }

    function sourceDialogOverviewRows(doc) {
      return renderDetailRows([
        ['Document type', doc.document_type],
        ['Media type', doc.media_type || 'pdf'],
        ['Date', doc.date_label || doc.year || doc.year_start || doc.year_end],
        ['Location', doc.location],
        ['Media profile', mediaProfileText(doc)],
        ['Extraction', formatExtractionMethod(doc.extraction_method)],
        ['Review status', doc.review_status],
        ['Review source', doc.review_source],
      ]);
    }

    function sourceDialogEvidenceRows(doc) {
      return renderDetailRows([
        ['Category', doc.evidence_category],
        ['Category basis', doc.evidence_category_description],
        ['Event count', doc.event_count],
        ['Record type', doc.event_record_type],
        ['Observation mode', doc.primary_observation_mode],
        ['Sensor platform', doc.sensor_platform],
        ['Observer platform', doc.observer_platform],
        ['Observer roles', doc.observer_roles],
        ['Witness count', doc.witness_count_bucket],
        ['Corroboration', doc.corroboration_types],
        ['Chain of custody', doc.chain_of_custody_quality],
        ['Redaction level', doc.redaction_level],
        ['Event time precision', doc.event_time_precision],
        ['Location precision', doc.location_precision],
        ['Environment context', doc.environment_context_present],
        ['Day/night context', doc.day_night_context],
        ['Object count', doc.object_count_bucket],
        ['Morphology', doc.morphology_normalized],
        ['Color/luminosity', doc.color_luminosity_normalized],
        ['Motion', doc.apparent_motion_class],
        ['Mundane explanation', doc.mundane_explanation_present],
        ['Resolution', doc.resolution_status],
        ['Measurement quality', doc.measurement_quality],
        ['Quantitative fields', doc.quantitative_fields_present],
      ]);
    }

    function sourceDialogTechnicalRows(doc) {
      const mediaRows = Object.entries(doc.media_metadata || {})
        .filter(([key, value]) => hasDetailValue(value) && key !== 'transcript_excerpt')
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, value]) => [humanizeToken(key), value, key]);
      return renderDetailRows([
        ['Filename', doc.filename],
        ['Relative path', doc.relative_path],
        ['Pages', doc.page_count],
        ['Processed pages', doc.processed_pages],
        ['Native pages', doc.native_pages],
        ['OCR pages', doc.ocr_pages],
        ['Deferred OCR pages', doc.ocr_skipped_pages],
        ['Text characters', doc.text_characters],
        ['Transcript characters', doc.transcript_characters],
        ...mediaRows,
      ]);
    }

    function sourceDialogCapabilities(doc) {
      const capabilities = doc.capabilities || [];
      if (capabilities.length) {
        const rendered = capabilities.map((capability) => `
          <div class="capability-detail-item">
            <strong>${escapeHtml(capability.label || capability.key || 'Capability')}</strong>
            <span>${escapeHtml([capability.group, capability.score ? `score ${capability.score}` : ''].filter(Boolean).join(' · '))}</span>
            ${capability.description ? `<div>${escapeHtml(capability.description)}</div>` : ''}
          </div>`).join('');
        return renderDetailSection('Observed Capabilities', `<div class="capability-detail-list">${rendered}</div>`);
      }
      return doc.capability_profile && doc.capability_profile !== 'No capability label'
        ? renderDetailSection('Observed Capabilities', `<p>${escapeHtml(doc.capability_profile)}</p>`)
        : '';
    }

    function sourceDialogTags(doc) {
      const sections = [
        ['Themes', doc.themes],
        ['Agencies', doc.agencies],
      ].map(([label, values]) => {
        const chips = renderChipRow(values);
        return chips ? `<div><h3>${escapeHtml(label)}</h3>${chips}</div>` : '';
      }).filter(Boolean).join('');
      return sections ? `<section class="source-detail-section">${sections}</section>` : '';
    }

    function sourceDialogRelated(doc) {
      const related = doc.related_sources || [];
      if (!related.length) return '';
      const rendered = related.map((source) => {
        const score = typeof source.similarity === 'number' ? `${Math.round(source.similarity * 100)}% similar` : '';
        const factors = (source.shared_factors || []).join(' · ');
        const filterValue = source.title || source.filename || '';
        return `
          <div class="related-detail-item">
            <button type="button" data-dialog-related-search="${escapeHtml(filterValue)}">
              <strong>${escapeHtml(source.title || source.filename || 'Related source')}</strong>
              ${escapeHtml([source.document_type, source.year, source.evidence_category, score].filter(Boolean).join(' · '))}
            </button>
            ${factors ? `<div>${escapeHtml(factors)}</div>` : ''}
          </div>`;
      }).join('');
      return renderDetailSection('Similar Sources', `<div class="related-detail-list">${rendered}</div>`);
    }

    function renderSourceDialog(doc) {
      const categoryClass = doc.evidence_category_rank === 1
        ? 'category-one'
        : doc.evidence_category_rank === 2
          ? 'category-two'
          : 'category-three';
      const overview = sourceDialogOverviewRows(doc);
      const evidence = sourceDialogEvidenceRows(doc);
      const technical = sourceDialogTechnicalRows(doc);
      return `
        <div class="source-dialog-shell">
          <header class="source-dialog-header">
            <div>
              <div class="source-dialog-kicker">Source Detail</div>
              <h2 id="sourceDialogTitle">${escapeHtml(doc.title)}</h2>
              <div class="source-dialog-subtitle">${escapeHtml(doc.filename || doc.relative_path || '')}</div>
            </div>
            <form method="dialog">
              <button class="source-dialog-close" type="submit" aria-label="Close source detail">&times;</button>
            </form>
          </header>
          <div class="source-dialog-body">
            <aside class="source-dialog-aside">
              ${sourceDialogMedia(doc)}
              ${sourceDialogLinks(doc)}
              <span class="classification-badge ${categoryClass}">${escapeHtml(doc.evidence_category || 'Category Three')}</span>
            </aside>
            <main class="source-dialog-main">
              ${sourceDialogSummary(doc)}
              ${sourceDialogObservationSection(doc)}
              ${overview ? renderDetailSection('Source Metadata', `<div class="source-dialog-meta-grid">${overview}</div>`) : ''}
              ${sourceDialogTags(doc)}
              ${sourceDialogCapabilities(doc)}
              ${evidence ? renderDetailSection('Evidence Signals', evidence) : ''}
              ${technical ? renderDetailSection('Technical Metadata', technical) : ''}
              ${sourceDialogRelated(doc)}
            </main>
          </div>
        </div>`;
    }

    function openSourceDialog(doc, trigger = null) {
      const dialog = document.getElementById('sourceDetailDialog');
      if (!dialog || !doc) return;
      sourceDialogReturnFocus = trigger || document.activeElement;
      dialog.innerHTML = renderSourceDialog(doc);
      dialog.querySelectorAll('[data-dialog-related-search]').forEach((button) => {
        button.addEventListener('click', () => {
          const search = button.dataset.dialogRelatedSearch;
          dialog.close();
          if (search) openDocumentsWithFilters({ search });
        });
      });
      if (typeof dialog.showModal === 'function') {
        dialog.showModal();
      } else {
        dialog.setAttribute('open', '');
      }
    }

    function bindSourceDetailButtons() {
      document.querySelectorAll('[data-source-index]').forEach((button) => {
        button.addEventListener('click', () => {
          const sourceIndex = Number(button.dataset.sourceIndex);
          if (!Number.isInteger(sourceIndex) || sourceIndex < 0) return;
          openSourceDialog(documents[sourceIndex], button);
        });
      });
    }

    function initSourceDialog() {
      const dialog = document.getElementById('sourceDetailDialog');
      if (!dialog) return;
      dialog.addEventListener('click', (event) => {
        if (event.target === dialog) {
          dialog.close();
        }
      });
      dialog.addEventListener('close', () => {
        if (sourceDialogReturnFocus && typeof sourceDialogReturnFocus.focus === 'function') {
          sourceDialogReturnFocus.focus();
        }
        sourceDialogReturnFocus = null;
      });
    }

    function relatedSourcesSection(doc) {
      const related = (doc.related_sources || []).slice(0, 3);
      if (!related.length) return '';
      const links = related.map((source) => {
        const factors = (source.shared_factors || []).slice(0, 3).join(' · ');
        const score = typeof source.similarity === 'number' ? ` ${Math.round(source.similarity * 100)}%` : '';
        const title = factors ? `${source.title} · ${factors}` : source.title;
        const filterValue = source.title || source.filename || '';
        return `<a href="${escapeHtml(source.source_href || '#')}" data-related-search="${escapeHtml(filterValue)}" title="${escapeHtml(title)}">${escapeHtml(source.title || source.filename || 'Related source')}${escapeHtml(score)}</a>`;
      }).join('');
      return `<div class="related-sources"><strong>Similar</strong><div class="related-source-links">${links}</div></div>`;
    }

    function resetDocumentPage() {
      state.page = 1;
    }

    function filteredDocuments() {
      const search = state.search.trim().toLowerCase();
      return documents.filter((doc) => {
        const year = doc.year || doc.year_start || doc.year_end;
        if (state.type !== 'all' && doc.document_type !== state.type) return false;
        if (state.method !== 'all' && doc.extraction_method !== state.method) return false;
        if (state.category !== 'all' && doc.evidence_category !== state.category) return false;
        if (state.capability !== 'all' && !(doc.capability_keys || []).includes(state.capability)) return false;
        if (state.place !== 'all' && (!doc.location || doc.location.label !== state.place)) return false;
        if (year && year < state.yearMin) return false;
        if (year && year > state.yearMax) return false;
        if (!search) return true;
        const haystack = [
          doc.title,
          doc.filename || '',
          doc.summary_narrative,
          doc.visual_observations || '',
          doc.audio_source_characterization || '',
          doc.audio_transcript_summary || '',
          ...(doc.themes || []),
          ...(doc.agencies || []),
          ...(doc.top_terms || []),
          doc.evidence_category || '',
          doc.evidence_category_description || '',
          doc.capability_profile || '',
          ...(doc.capability_labels || []),
          ...trendSearchValues(doc),
          doc.location ? doc.location.label : '',
        ].join(' ').toLowerCase();
        return haystack.includes(search);
      });
    }

    function renderMeta() {
      const mediaCounts = Object.fromEntries((analysis.media_counts || []).map((entry) => [entry.label, entry.count]));
      const mediaTotal = (mediaCounts.image || 0) + (mediaCounts.video || 0);
      document.getElementById('metaDocs').textContent = mediaTotal
        ? `${analysis.document_count} sources, ${mediaTotal} media`
        : `${analysis.document_count} sources`;
      document.getElementById('metaPages').textContent = `${analysis.total_pages} pages`;
      document.getElementById('metaYears').textContent = analysis.year_min && analysis.year_max
        ? `${analysis.year_min}–${analysis.year_max}`
        : 'undated corpus';
      document.getElementById('metaGenerated').textContent = `Generated ${analysis.generated_at.replace('T', ' ').replace('Z', ' UTC')}`;
    }

    function renderMetrics(items) {
      const years = items.map((doc) => doc.year).filter(Boolean);
      const ocrDocs = items.filter((doc) => doc.extraction_method === 'ocr' || doc.extraction_method === 'hybrid').length;
      const mediaItems = items.filter((doc) => doc.media_type === 'image' || doc.media_type === 'video' || doc.media_type === 'audio').length;
      const located = items.filter((doc) => doc.location).length;
      const pages = items.reduce((sum, doc) => sum + (doc.page_count || 0), 0);
      const cards = [
        { value: items.length, label: 'Sources in view' },
        { value: pages, label: 'PDF pages represented' },
        { value: mediaItems, label: 'Media items' },
        { value: located, label: 'Resolved geolocations' },
        { value: ocrDocs, label: 'OCR-assisted files' },
        { value: years.length ? Math.min(...years) : '—', label: 'Earliest anchor year' },
        { value: years.length ? Math.max(...years) : '—', label: 'Latest anchor year' },
      ];
      document.getElementById('metrics').innerHTML = cards.map((card) => `
        <div class="metric">
          <div class="value">${escapeHtml(card.value)}</div>
          <div class="label">${escapeHtml(card.label)}</div>
        </div>`).join('');
    }

    function renderExecutiveSummary() {
      const node = document.getElementById('executiveSummary');
      const summary = analysis.executive_summary || {};
      const sections = Array.isArray(summary.sections) ? summary.sections.filter(Boolean) : [];
      if (!sections.length) {
        node.innerHTML = '<p class="muted">No executive summary has been generated for this corpus yet.</p>';
        return;
      }
      const paragraphs = sections.map((section) => `<p>${escapeHtml(section)}</p>`).join('');
      const generated = summary.generated_at
        ? `<div class="executive-summary-meta">Generated ${escapeHtml(summary.generated_at.replace('T', ' ').replace('Z', ' UTC'))}</div>`
        : '';
      node.innerHTML = `${paragraphs}${generated}`;
    }

    function renderBarChart(targetId, entries, color, horizontal = false, options = {}) {
      const node = document.getElementById(targetId);
      if (!node) {
        return;
      }
      if (!entries.length) {
        node.innerHTML = '<p class="chart-note">No data matches the current filters.</p>';
        return;
      }
      const width = Math.max(node.clientWidth || 320, 320);
      const height = horizontal ? Math.max(entries.length * 46, 280) : 300;
      const maxCount = Math.max(...entries.map((entry) => entry.count), 1);
      const valueLabelWidth = horizontal
        ? Math.max(String(maxCount).length * 8 + 16, 32)
        : 0;
      const labelMargin = horizontal
        ? Math.min(
            Math.max(...entries.map((entry) => entry.label.length * 6.5), 140),
            Math.max(width * 0.48, 140),
          )
        : 44;
      const margin = horizontal
        ? { top: 10, right: valueLabelWidth, bottom: 20, left: labelMargin }
        : { top: 16, right: 20, bottom: 48, left: 44 };
      const plotWidth = width - margin.left - margin.right;
      const plotHeight = height - margin.top - margin.bottom;
      const actionAttributes = (entry) => {
        if (!options.actionDataKey) {
          return '';
        }
        const value = entry.filterValue ?? entry.label;
        const ariaLabel = options.actionLabel
          ? options.actionLabel(entry)
          : `Filter documents to ${entry.label}`;
        return ` class="chart-click-target" data-${options.actionDataKey}="${escapeHtml(value)}" role="button" tabindex="0" aria-label="${escapeHtml(ariaLabel)}"`;
      };

      if (horizontal) {
        const barHeight = plotHeight / entries.length;
        const bars = entries.map((entry, index) => {
          const y = margin.top + index * barHeight + 7;
          const barWidth = (entry.count / maxCount) * plotWidth;
          return `
            <g${actionAttributes(entry)}>
              <text x="${margin.left - 12}" y="${y + 14}" text-anchor="end" font-size="12" fill="${chartColors.muted}">${escapeHtml(entry.label)}</text>
              <rect x="${margin.left}" y="${y}" width="${barWidth}" height="20" fill="${color}" opacity="0.82"></rect>
              <text x="${margin.left + barWidth + 8}" y="${y + 14}" font-size="12" fill="${chartColors.ink}">${entry.count}</text>
            </g>`;
        }).join('');
        node.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img">${bars}</svg>`;
        return;
      }

      const barWidth = plotWidth / entries.length;
      const bars = entries.map((entry, index) => {
        const x = margin.left + index * barWidth + 6;
        const h = (entry.count / maxCount) * plotHeight;
        const y = margin.top + plotHeight - h;
        const showLabel = entries.length <= 24 || index % Math.ceil(entries.length / 12) === 0;
        return `
          <g${actionAttributes(entry)}>
            <rect x="${x}" y="${y}" width="${Math.max(barWidth - 12, 6)}" height="${h}" fill="${color}" opacity="0.86"></rect>
            <text x="${x + Math.max(barWidth - 12, 6) / 2}" y="${margin.top + plotHeight + 18}" text-anchor="middle" font-size="11" fill="${chartColors.muted}">${showLabel ? escapeHtml(entry.label) : ''}</text>
          </g>`;
      }).join('');

      const grid = Array.from({ length: 5 }, (_, index) => {
        const value = Math.round((maxCount / 4) * index);
        const y = margin.top + plotHeight - (plotHeight * index / 4);
        return `
          <line x1="${margin.left}" x2="${width - margin.right}" y1="${y}" y2="${y}" stroke="${chartColors.grid}"></line>
          <text x="${margin.left - 10}" y="${y + 4}" text-anchor="end" font-size="11" fill="${chartColors.muted}">${value}</text>`;
      }).join('');
      node.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img">${grid}${bars}</svg>`;
    }

    function renderMap(items) {
      const node = document.getElementById('mapChart');
      const clusters = countBy(items.filter((doc) => doc.location), (doc) => doc.location.label)
        .map((entry) => {
          const doc = items.find((item) => item.location && item.location.label === entry.label);
          return {
            ...entry,
            latitude: doc.location.latitude,
            longitude: doc.location.longitude,
          };
        })
        .sort((a, b) => b.count - a.count);

      if (!clusters.length) {
        node.innerHTML = '<p class="chart-note">No mapped locations match the current filters.</p>';
        return;
      }

      const width = Math.max(node.clientWidth || 320, 320);
      const height = 320;
      const projectX = (lon) => ((lon + 180) / 360) * (width - 40) + 20;
      const projectY = (lat) => ((90 - lat) / 180) * (height - 40) + 20;
      const regionLabels = [
        { text: 'North Atlantic', lon: -35, lat: 22 },
        { text: 'Mediterranean', lon: 15, lat: 35 },
        { text: 'Middle East', lon: 46, lat: 28 },
        { text: 'Pacific', lon: -145, lat: 6 },
        { text: 'Indian Ocean', lon: 82, lat: -16 },
      ];
      const ringToPath = (ring) => {
        if (!ring || !ring.length) return '';
        return ring.map(([lon, lat], index) => `${index === 0 ? 'M' : 'L'} ${projectX(lon)} ${projectY(lat)}`).join(' ') + ' Z';
      };
      const polygonToPath = (polygon) => (polygon || []).map((ring) => ringToPath(ring)).join(' ');
      const geometryToPath = (geometry) => {
        if (!geometry || !geometry.coordinates) return '';
        if (geometry.type === 'Polygon') return polygonToPath(geometry.coordinates);
        if (geometry.type === 'MultiPolygon') return geometry.coordinates.map((polygon) => polygonToPath(polygon)).join(' ');
        return '';
      };
      const maxCount = Math.max(...clusters.map((cluster) => cluster.count), 1);
      const graticule = Array.from({ length: 7 }, (_, index) => {
        const y = 20 + ((height - 40) / 6) * index;
        return `<line x1="20" x2="${width - 20}" y1="${y}" y2="${y}" stroke="${chartColors.grid}"></line>`;
      }).join('') + Array.from({ length: 7 }, (_, index) => {
        const x = 20 + ((width - 40) / 6) * index;
        return `<line x1="${x}" x2="${x}" y1="20" y2="${height - 20}" stroke="${chartColors.grid}"></line>`;
      }).join('');
      const landLayer = (worldLand.geometries || []).map((geometry) => {
        const path = geometryToPath(geometry);
        if (!path) return '';
        return `<path d="${path}" fill="${chartColors.land}" stroke="rgba(244,239,225,0.22)" stroke-width="0.8" vector-effect="non-scaling-stroke"></path>`;
      }).join('');
      const axisLabels = [
        { text: '180°W', x: projectX(-180), y: height - 8, anchor: 'start' },
        { text: '120°W', x: projectX(-120), y: height - 8, anchor: 'middle' },
        { text: '60°W', x: projectX(-60), y: height - 8, anchor: 'middle' },
        { text: '0°', x: projectX(0), y: height - 8, anchor: 'middle' },
        { text: '60°E', x: projectX(60), y: height - 8, anchor: 'middle' },
        { text: '120°E', x: projectX(120), y: height - 8, anchor: 'middle' },
        { text: '180°E', x: projectX(180), y: height - 8, anchor: 'end' },
        { text: '60°N', x: 24, y: projectY(60) - 4, anchor: 'start' },
        { text: '30°N', x: 24, y: projectY(30) - 4, anchor: 'start' },
        { text: '0°', x: 24, y: projectY(0) - 4, anchor: 'start' },
        { text: '30°S', x: 24, y: projectY(-30) - 4, anchor: 'start' },
        { text: '60°S', x: 24, y: projectY(-60) - 4, anchor: 'start' },
      ].map((label) => `<text x="${label.x}" y="${label.y}" text-anchor="${label.anchor}" font-size="11" fill="${chartColors.muted}">${label.text}</text>`).join('');
      const regionLayer = regionLabels.map((label) => `<text x="${projectX(label.lon)}" y="${projectY(label.lat)}" text-anchor="middle" font-size="11" fill="rgba(214,168,79,0.58)">${escapeHtml(label.text)}</text>`).join('');

      const placedLabels = [];
      const bubbles = clusters.map((cluster) => {
        const radius = 6 + (cluster.count / maxCount) * 22;
        const x = projectX(cluster.longitude);
        const y = projectY(cluster.latitude);
        const labelY = y - radius - 8;
        const canPlaceLabel = placedLabels.every((label) => Math.abs(label.x - x) > 52 || Math.abs(label.y - labelY) > 18);
        if (canPlaceLabel) {
          placedLabels.push({ x, y: labelY });
        }
        const title = `${cluster.label} (${cluster.count})`;
        return `
          <g class="chart-click-target" data-place="${escapeHtml(cluster.label)}" role="button" tabindex="0" aria-label="Filter documents to ${escapeHtml(cluster.label)}">
            <circle cx="${x}" cy="${y}" r="${radius}" fill="rgba(86,214,201,0.16)" stroke="${chartColors.teal}" stroke-width="1.6">
              <title>${escapeHtml(title)}</title>
            </circle>
            <circle cx="${x}" cy="${y}" r="2.5" fill="${chartColors.gold}">
              <title>${escapeHtml(title)}</title>
            </circle>
            ${canPlaceLabel ? `<text x="${x}" y="${labelY}" text-anchor="middle" font-size="11" fill="${chartColors.ink}">${escapeHtml(title)}</text>` : ''}
          </g>`;
      }).join('');

      node.innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img">
          <rect x="20" y="20" width="${width - 40}" height="${height - 40}" fill="${chartColors.mapPanel}" stroke="rgba(214,168,79,0.34)"></rect>
          ${graticule}
          ${landLayer}
          ${regionLayer}
          ${bubbles}
          ${axisLabels}
          <text x="24" y="34" font-size="12" fill="${chartColors.muted}">World Reference Map</text>
        </svg>`;
      bindChartFilterTargets('#mapChart [data-place]', (element) => openDocumentsWithFilters({ place: element.dataset.place }));
    }

    function renderSignals(items) {
      const years = items.map((doc) => doc.year).filter(Boolean);
      const hotspots = countBy(items.filter((doc) => doc.location), (doc) => doc.location.label).sort((a, b) => b.count - a.count);
      const themes = countBy(items.flatMap((doc) => (doc.themes || []).map((theme) => ({ theme }))), (entry) => entry.theme).sort((a, b) => b.count - a.count);
      const capabilities = countBy(items.flatMap((doc) => (doc.capabilities || []).map((capability) => capability)), (entry) => entry.label).sort((a, b) => b.count - a.count);
      const signals = [];
      if (years.length) {
        const counts = countBy(items.filter((doc) => doc.year), (doc) => doc.year).sort((a, b) => b.count - a.count);
        signals.push({
          text: `Peak activity in the corpus lands in ${counts[0].label} with ${counts[0].count} documents.`,
          filters: { yearMin: Number(counts[0].label), yearMax: Number(counts[0].label) },
        });
      }
      if (hotspots.length) {
        signals.push({
          text: `Geolocation clustering is strongest around ${hotspots[0].label}.`,
          filters: { place: hotspots[0].label },
        });
      }
      if (themes.length) {
        signals.push({
          text: `The leading themes are ${themes.slice(0, 2).map((entry) => entry.label.toLowerCase()).join(' and ')}.`,
          filters: { search: themes[0].label },
        });
      }
      if (capabilities.length) {
        const definition = (analysis.capability_definitions || []).find((item) => item.label === capabilities[0].label);
        signals.push({
          text: `Observed capability labeling is led by ${capabilities[0].label.toLowerCase()}.`,
          filters: definition ? { capability: definition.key } : { search: capabilities[0].label },
        });
      }
      document.getElementById('researchSignals').innerHTML = signals.map((signal, index) => `<div class="signal chart-click-target" data-signal-index="${index}" role="button" tabindex="0" aria-label="Filter documents for this research signal">${escapeHtml(signal.text)}</div>`).join('');
      bindChartFilterTargets('#researchSignals [data-signal-index]', (element) => {
        const signal = signals[Number(element.dataset.signalIndex)];
        if (signal) {
          openDocumentsWithFilters(signal.filters);
        }
      });
    }

    function renderThemes(items) {
      const themes = countBy(items.flatMap((doc) => (doc.themes || []).map((theme) => ({ theme }))), (entry) => entry.theme)
        .sort((a, b) => b.count - a.count)
        .slice(0, 10);
      document.getElementById('themeGrid').innerHTML = themes.length
        ? themes.map((entry) => `<div class="theme-chip chart-click-target" data-search="${escapeHtml(entry.label)}" role="button" tabindex="0" aria-label="Search documents for ${escapeHtml(entry.label)}"><strong>${escapeHtml(entry.label)}</strong><div class="muted">${entry.count} documents</div></div>`).join('')
        : '<p class="chart-note">No themes match the current filters.</p>';
      bindChartFilterTargets('#themeGrid [data-search]', (element) => openDocumentsWithFilters({ search: element.dataset.search }));
    }

    function renderOrganizations(items) {
      const organizations = countBy(
        items.flatMap((doc) => (doc.agencies || []).map((agency) => ({ agency }))),
        (entry) => entry.agency,
      )
        .sort((a, b) => b.count - a.count)
        .slice(0, 12);
      renderBarChart('organizationChart', organizations, chartColors.green, true, {
        actionDataKey: 'search',
        actionLabel: (entry) => `Search documents for ${entry.label}`,
      });
      bindChartFilterTargets('#organizationChart [data-search]', (element) => openDocumentsWithFilters({ search: element.dataset.search }));
    }

    function renderClassifications(items) {
      const categories = analysis.evidence_category_counts
        .map((entry) => ({
          label: entry.label,
          rank: entry.rank,
          count: items.filter((doc) => doc.evidence_category === entry.label).length,
        }))
        .sort((a, b) => a.rank - b.rank);
      renderBarChart('classificationChart', categories, chartColors.red, true, {
        actionDataKey: 'category',
        actionLabel: (entry) => `Filter documents to ${entry.label}`,
      });
      bindChartFilterTargets('#classificationChart [data-category]', (element) => openDocumentsWithFilters({ category: element.dataset.category }));
    }

    function bindChartFilterTargets(selector, handler) {
      document.querySelectorAll(selector).forEach((element) => {
        element.addEventListener('click', () => handler(element));
        element.addEventListener('keydown', (event) => {
          if (event.key !== 'Enter' && event.key !== ' ') {
            return;
          }
          event.preventDefault();
          handler(element);
        });
      });
    }

    function renderEvidenceTimeline(items) {
      const node = document.getElementById('evidenceTimelineChart');
      const categories = (analysis.evidence_category_counts || []).slice().sort((a, b) => a.rank - b.rank);
      const years = [...new Set(items.map((doc) => doc.year).filter(Boolean))].sort((a, b) => a - b);
      if (!years.length || !categories.length) {
        node.innerHTML = '<p class="chart-note">No dated evidence records are available.</p>';
        return;
      }

      const width = Math.max(node.clientWidth || 520, 520);
      const height = 330;
      const margin = { top: 18, right: 20, bottom: 64, left: 44 };
      const plotWidth = width - margin.left - margin.right;
      const plotHeight = height - margin.top - margin.bottom;
      const rows = years.map((year) => {
        const docsForYear = items.filter((doc) => doc.year === year);
        const categoryCounts = categories.map((category) => ({
          ...category,
          count: docsForYear.filter((doc) => doc.evidence_category === category.label).length,
        }));
        return { year, total: categoryCounts.reduce((sum, category) => sum + category.count, 0), categoryCounts };
      });
      const maxTotal = Math.max(...rows.map((row) => row.total), 1);
      const barStep = plotWidth / rows.length;
      const labelStep = Math.max(1, Math.ceil(rows.length / 12));
      const grid = Array.from({ length: 5 }, (_, index) => {
        const value = Math.round((maxTotal / 4) * index);
        const y = margin.top + plotHeight - (plotHeight * index / 4);
        return `
          <line x1="${margin.left}" x2="${width - margin.right}" y1="${y}" y2="${y}" stroke="${chartColors.grid}"></line>
          <text x="${margin.left - 10}" y="${y + 4}" text-anchor="end" font-size="11" fill="${chartColors.muted}">${value}</text>`;
      }).join('');
      const bars = rows.map((row, index) => {
        let yCursor = margin.top + plotHeight;
        const x = margin.left + index * barStep + 3;
        const barWidth = Math.max(barStep - 6, 3);
        const segments = row.categoryCounts.map((category) => {
          const segmentHeight = (category.count / maxTotal) * plotHeight;
          yCursor -= segmentHeight;
          if (!category.count) return '';
          const title = `${category.label}, ${row.year}: ${category.count}`;
          return `
            <g class="chart-click-target" data-year="${row.year}" data-category="${escapeHtml(category.label)}" role="button" tabindex="0" aria-label="Filter documents to ${escapeHtml(title)}">
              <rect x="${x}" y="${yCursor}" width="${barWidth}" height="${Math.max(segmentHeight, 1)}" fill="${categoryPalette[category.label] || chartColors.blue}" opacity="0.84">
                <title>${escapeHtml(title)}</title>
              </rect>
            </g>`;
        }).join('');
        const label = index % labelStep === 0 || index === rows.length - 1
          ? `<text x="${x + barWidth / 2}" y="${margin.top + plotHeight + 34}" transform="rotate(-45 ${x + barWidth / 2} ${margin.top + plotHeight + 34})" text-anchor="end" font-size="10" fill="${chartColors.muted}">${row.year}</text>`
          : '';
        return `${segments}${label}`;
      }).join('');

      node.innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img">
          ${grid}
          ${bars}
        </svg>
        ${chartLegend(categories.map((category) => ({ label: category.label, color: categoryPalette[category.label] || chartColors.blue })))}`;
      bindChartFilterTargets('#evidenceTimelineChart [data-year]', (element) => {
        const year = Number(element.dataset.year);
        if (Number.isFinite(year)) {
          openDocumentsWithFilters({ yearMin: year, yearMax: year, category: element.dataset.category || 'all' });
        }
      });
    }

    function renderResolutionChart(items) {
      const node = document.getElementById('resolutionChart');
      const statuses = countTrendValues(items, 'resolution_status').slice(0, 8);
      if (!statuses.length) {
        node.innerHTML = '<p class="chart-note">No resolution labels are available.</p>';
        return;
      }

      const explanationValues = ['no', 'yes_source_suggested', 'yes_source_resolved', 'unclear'];
      const explanationColors = {
        no: chartColors.red,
        yes_source_suggested: chartColors.gold,
        yes_source_resolved: chartColors.green,
        unclear: chartColors.teal,
      };
      const width = Math.max(node.clientWidth || 320, 320);
      const height = Math.max(statuses.length * 46 + 42, 280);
      const margin = { top: 8, right: 34, bottom: 24, left: Math.min(Math.max(...statuses.map((entry) => entry.label.length * 6.2), 120), Math.max(width * 0.46, 130)) };
      const plotWidth = width - margin.left - margin.right;
      const maxCount = Math.max(...statuses.map((entry) => entry.count), 1);
      const rows = statuses.map((status, index) => {
        const y = margin.top + index * 42 + 8;
        let xCursor = margin.left;
        const segments = explanationValues.map((explanation) => {
          const count = items.filter((doc) => doc.resolution_status === status.key && doc.mundane_explanation_present === explanation).length;
          if (!count) return '';
          const widthForCount = (count / maxCount) * plotWidth;
          const x = xCursor;
          xCursor += widthForCount;
          const title = `${status.label} · ${humanizeToken(explanation)}: ${count}`;
          return `
            <g class="chart-click-target" data-search="${escapeHtml(status.key)}" role="button" tabindex="0" aria-label="Search documents for ${escapeHtml(status.label)}">
              <rect x="${x}" y="${y}" width="${Math.max(widthForCount, 1)}" height="22" fill="${explanationColors[explanation]}" opacity="0.78">
                <title>${escapeHtml(title)}</title>
              </rect>
            </g>`;
        }).join('');
        return `
          <text x="${margin.left - 12}" y="${y + 15}" text-anchor="end" font-size="12" fill="${chartColors.muted}">${escapeHtml(status.label)}</text>
          ${segments}
          <text x="${margin.left + (status.count / maxCount) * plotWidth + 8}" y="${y + 15}" font-size="12" fill="${chartColors.ink}">${status.count}</text>`;
      }).join('');
      node.innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img">${rows}</svg>
        ${chartLegend(explanationValues.map((value) => ({ label: humanizeToken(value), color: explanationColors[value] })))}`;
      bindChartFilterTargets('#resolutionChart [data-search]', (element) => openDocumentsWithFilters({ search: element.dataset.search }));
    }

    function renderHeatmap(targetId, rows, columns, countFn, options = {}) {
      const node = document.getElementById(targetId);
      if (!rows.length || !columns.length) {
        node.innerHTML = `<p class="chart-note">${escapeHtml(options.emptyText || 'No matrix data is available.')}</p>`;
        return;
      }
      const width = Math.max(node.clientWidth || 420, 420);
      const rowLabelWidth = Math.min(Math.max(...rows.map((row) => row.label.length * 6.5), 128), Math.max(width * 0.36, 132));
      const columnHeight = 72;
      const cellSize = Math.max(30, Math.min(48, (width - rowLabelWidth - 20) / columns.length));
      const left = rowLabelWidth;
      const top = columnHeight;
      const height = top + rows.length * cellSize + 22;
      const values = rows.flatMap((row) => columns.map((column) => countFn(row, column)));
      const maxCount = Math.max(...values, 1);
      const columnLabels = columns.map((column, index) => {
        const x = left + index * cellSize + cellSize / 2;
        return `<text x="${x}" y="${top - 10}" transform="rotate(-38 ${x} ${top - 10})" text-anchor="start" font-size="10" fill="${chartColors.muted}">${escapeHtml(column.label)}</text>`;
      }).join('');
      const rowLabels = rows.map((row, index) => {
        const y = top + index * cellSize + cellSize / 2 + 4;
        return `<text x="${left - 10}" y="${y}" text-anchor="end" font-size="11" fill="${chartColors.muted}">${escapeHtml(row.label)}</text>`;
      }).join('');
      const cells = rows.map((row, rowIndex) => columns.map((column, columnIndex) => {
        const count = countFn(row, column);
        const x = left + columnIndex * cellSize;
        const y = top + rowIndex * cellSize;
        const intensity = count ? Math.min(0.9, 0.18 + (count / maxCount) * 0.72) : 0.05;
        const title = `${row.label} · ${column.label}: ${count}`;
        const dataAttributes = options.cellAttributes ? options.cellAttributes(row, column, count) : `data-search="${escapeHtml(row.key)}"`;
        return `
          <g class="chart-click-target" ${dataAttributes} role="button" tabindex="0" aria-label="${escapeHtml(title)}">
            <rect x="${x}" y="${y}" width="${cellSize - 3}" height="${cellSize - 3}" fill="${options.color || chartColors.teal}" opacity="${intensity.toFixed(2)}" stroke="rgba(244,239,225,0.08)">
              <title>${escapeHtml(title)}</title>
            </rect>
            ${count ? `<text x="${x + (cellSize - 3) / 2}" y="${y + cellSize / 2 + 4}" text-anchor="middle" font-size="11" fill="${chartColors.ink}">${count}</text>` : ''}
          </g>`;
      }).join('')).join('');
      node.innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img">
          ${columnLabels}
          ${rowLabels}
          ${cells}
        </svg>`;
      bindChartFilterTargets(`#${targetId} [data-search]:not([data-category])`, (element) => openDocumentsWithFilters({ search: element.dataset.search }));
      bindChartFilterTargets(`#${targetId} [data-category]`, (element) => openDocumentsWithFilters({ category: element.dataset.category, search: element.dataset.search || '' }));
    }

    function renderObservationModeMatrix(items) {
      const rows = countTrendValues(items, 'primary_observation_mode', { exclude: ['unknown', 'none_stated'] }).slice(0, 8);
      const columns = countTrendValues(items, 'observer_platform', { exclude: ['unknown', 'not_applicable'] }).slice(0, 7);
      renderHeatmap('observationModeMatrix', rows, columns, (row, column) => (
        items.filter((doc) => trendValues(doc, 'primary_observation_mode').includes(row.key) && trendValues(doc, 'observer_platform').includes(column.key)).length
      ), {
        color: chartColors.blue,
        emptyText: 'No observation mode/platform overlap is available.',
        cellAttributes: (row) => `data-search="${escapeHtml(row.key)}"`,
      });
    }

    function renderCorroborationCategoryMatrix(items) {
      const rows = countTrendValues(items, 'corroboration_types').slice(0, 8);
      const columns = (analysis.evidence_category_counts || []).slice().sort((a, b) => a.rank - b.rank).map((category) => ({ key: category.label, label: category.label.replace('Category ', 'C') }));
      renderHeatmap('corroborationCategoryMatrix', rows, columns, (row, column) => (
        items.filter((doc) => doc.evidence_category === column.key && trendValues(doc, 'corroboration_types').includes(row.key)).length
      ), {
        color: chartColors.gold,
        emptyText: 'No corroboration/category overlap is available.',
        cellAttributes: (row, column) => `data-search="${escapeHtml(row.key)}" data-category="${escapeHtml(column.key)}"`,
      });
    }

    function renderPhenomenologyMatrix(items) {
      const rows = countTrendValues(items, 'morphology_normalized', { exclude: ['none_stated', 'unknown'] }).slice(0, 8);
      const columns = countTrendValues(items, 'apparent_motion_class', { exclude: ['unknown'] }).slice(0, 8);
      renderHeatmap('phenomenologyMatrix', rows, columns, (row, column) => (
        items.filter((doc) => trendValues(doc, 'morphology_normalized').includes(row.key) && trendValues(doc, 'apparent_motion_class').includes(column.key)).length
      ), {
        color: chartColors.teal,
        emptyText: 'No morphology/motion overlap is available.',
        cellAttributes: (row) => `data-search="${escapeHtml(row.key)}"`,
      });
    }

    function renderRelatedSourceNetwork(items) {
      const node = document.getElementById('relatedSourceNetwork');
      const byKey = new Map();
      items.forEach((doc) => {
        [doc.title, doc.filename].filter(Boolean).forEach((key) => byKey.set(key, doc));
      });
      const degree = new Map();
      const edgeMap = new Map();
      items.forEach((doc) => {
        const sourceKey = doc.title || doc.filename;
        if (!sourceKey) return;
        (doc.related_sources || []).forEach((related) => {
          const target = byKey.get(related.title) || byKey.get(related.filename);
          if (!target) return;
          const targetKey = target.title || target.filename;
          if (!targetKey || targetKey === sourceKey) return;
          const pair = [sourceKey, targetKey].sort().join('::');
          const similarity = typeof related.similarity === 'number' ? related.similarity : 0;
          const previous = edgeMap.get(pair);
          if (!previous || similarity > previous.similarity) {
            edgeMap.set(pair, { sourceKey, targetKey, similarity });
          }
          degree.set(sourceKey, (degree.get(sourceKey) || 0) + 1);
          degree.set(targetKey, (degree.get(targetKey) || 0) + 1);
        });
      });
      const nodeDocs = items
        .filter((doc) => degree.get(doc.title || doc.filename))
        .sort((a, b) => (degree.get(b.title || b.filename) || 0) - (degree.get(a.title || a.filename) || 0) || (a.evidence_category_rank || 9) - (b.evidence_category_rank || 9))
        .slice(0, 34);
      if (!nodeDocs.length) {
        node.innerHTML = '<p class="chart-note">No related-source links are available.</p>';
        return;
      }
      const selected = new Set(nodeDocs.map((doc) => doc.title || doc.filename));
      const edges = [...edgeMap.values()]
        .filter((edge) => selected.has(edge.sourceKey) && selected.has(edge.targetKey))
        .sort((a, b) => b.similarity - a.similarity)
        .slice(0, 90);
      const width = Math.max(node.clientWidth || 420, 420);
      const height = 380;
      const centerX = width / 2;
      const centerY = height / 2;
      const maxRadius = Math.min(width, height) * 0.42;
      const minRadius = Math.min(width, height) * 0.19;
      const maxDegree = Math.max(...nodeDocs.map((doc) => degree.get(doc.title || doc.filename) || 1), 1);
      const positions = new Map();
      nodeDocs.forEach((doc, index) => {
        const rank = doc.evidence_category_rank || 3;
        const radius = minRadius + ((rank - 1) / 2) * (maxRadius - minRadius);
        const angle = -Math.PI / 2 + (index / nodeDocs.length) * Math.PI * 2;
        positions.set(doc.title || doc.filename, {
          x: centerX + Math.cos(angle) * radius,
          y: centerY + Math.sin(angle) * radius,
        });
      });
      const edgeLayer = edges.map((edge) => {
        const source = positions.get(edge.sourceKey);
        const target = positions.get(edge.targetKey);
        if (!source || !target) return '';
        const opacity = Math.min(0.64, 0.12 + edge.similarity * 0.56);
        return `<line x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}" stroke="rgba(86,214,201,${opacity.toFixed(2)})" stroke-width="${Math.max(0.8, edge.similarity * 1.8).toFixed(2)}"></line>`;
      }).join('');
      const nodeLayer = nodeDocs.map((doc, index) => {
        const key = doc.title || doc.filename;
        const position = positions.get(key);
        const docDegree = degree.get(key) || 1;
        const radius = 4 + (docDegree / maxDegree) * 9;
        const color = categoryPalette[doc.evidence_category] || chartColors.blue;
        const label = `${doc.title || doc.filename} · ${doc.evidence_category || 'Unclassified'} · ${docDegree} links`;
        return `
          <g class="chart-click-target" data-search="${escapeHtml(key)}" role="button" tabindex="0" aria-label="Search documents for ${escapeHtml(doc.title || doc.filename)}">
            <circle cx="${position.x}" cy="${position.y}" r="${radius}" fill="${color}" opacity="0.86" stroke="rgba(244,239,225,0.22)">
              <title>${escapeHtml(label)}</title>
            </circle>
          </g>`;
      }).join('');
      node.innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img">
          <circle cx="${centerX}" cy="${centerY}" r="${minRadius}" fill="none" stroke="rgba(200,78,83,0.18)"></circle>
          <circle cx="${centerX}" cy="${centerY}" r="${(minRadius + maxRadius) / 2}" fill="none" stroke="rgba(214,168,79,0.16)"></circle>
          <circle cx="${centerX}" cy="${centerY}" r="${maxRadius}" fill="none" stroke="rgba(86,214,201,0.14)"></circle>
          ${edgeLayer}
          ${nodeLayer}
        </svg>
        ${chartLegend((analysis.evidence_category_counts || []).map((category) => ({ label: category.label, color: categoryPalette[category.label] || chartColors.blue })))}`;
      bindChartFilterTargets('#relatedSourceNetwork [data-search]', (element) => openDocumentsWithFilters({ search: element.dataset.search }));
    }

    function defaultDocumentFilters() {
      return {
        search: '',
        type: 'all',
        method: 'all',
        category: 'all',
        capability: 'all',
        place: 'all',
        yearMin: analysis.year_min || 1900,
        yearMax: analysis.year_max || new Date().getFullYear(),
      };
    }

    function syncDocumentFilterControls() {
      document.getElementById('searchInput').value = state.search;
      document.getElementById('typeFilter').value = state.type;
      document.getElementById('methodFilter').value = state.method;
      document.getElementById('categoryFilter').value = state.category;
      document.getElementById('capabilityFilter').value = state.capability;
      document.getElementById('placeFilter').value = state.place;
      document.getElementById('yearMin').value = state.yearMin;
      document.getElementById('yearMax').value = state.yearMax;
    }

    function openDocumentsWithFilters(filters) {
      Object.assign(state, defaultDocumentFilters(), filters);
      resetDocumentPage();
      syncDocumentFilterControls();
      setActiveTab('documents');
      requestAnimationFrame(() => {
        document.getElementById('documentRegister')?.scrollIntoView({ block: 'start' });
      });
    }

    function renderCapabilityMatrix(items) {
      const definitions = analysis.capability_definitions || [];
      const categories = (analysis.evidence_category_counts || []).slice().sort((a, b) => a.rank - b.rank);
      const entries = definitions
        .map((definition) => {
          const matching = items.filter((doc) => (doc.capability_keys || []).includes(definition.key));
          const categoryCounts = categories.map((category) => ({
            ...category,
            count: matching.filter((doc) => doc.evidence_category === category.label).length,
          }));
          return {
            ...definition,
            count: matching.length,
            category_counts: categoryCounts,
          };
        })
        .filter((entry) => entry.count > 0)
        .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label))
        .slice(0, 8);
      const node = document.getElementById('capabilityMatrix');
      if (!entries.length) {
        node.innerHTML = '<p class="chart-note">No observed capabilities were inferred from this corpus.</p>';
        return;
      }
      const maxCount = Math.max(...entries.flatMap((entry) => entry.category_counts.map((category) => category.count)), 1);
      const header = `
        <div class="capability-row header" aria-hidden="true">
          <div>Capability</div>
          ${categories.map((category) => `<div title="${escapeHtml(category.label)}">C${category.rank}</div>`).join('')}
          <div>Total</div>
        </div>`;
      const rows = entries.map((entry) => {
        const cells = entry.category_counts.map((category) => {
          const intensity = category.count ? Math.min(0.42, 0.08 + (category.count / maxCount) * 0.34).toFixed(2) : '0.04';
          return `<div class="capability-cell" style="--intensity: ${intensity}" title="${escapeHtml(category.label)}: ${category.count}">${category.count || ''}</div>`;
        }).join('');
        return `
          <div class="capability-row chart-click-target" data-capability="${escapeHtml(entry.key)}" role="button" tabindex="0" aria-label="Filter documents to ${escapeHtml(entry.label)}">
            <div class="capability-label">
              <strong>${escapeHtml(entry.label)}</strong>
              <span>${escapeHtml(entry.group)}</span>
            </div>
            ${cells}
            <div class="capability-total">${entry.count}</div>
          </div>`;
      }).join('');
      node.innerHTML = header + rows;
      bindChartFilterTargets('#capabilityMatrix [data-capability]', (element) => openDocumentsWithFilters({ capability: element.dataset.capability }));
    }

    function renderDocuments(items) {
      const totalItems = items.length;
      const totalPages = Math.max(1, Math.ceil(totalItems / DOCUMENTS_PER_PAGE));
      state.page = Math.min(Math.max(state.page, 1), totalPages);
      const startIndex = totalItems ? (state.page - 1) * DOCUMENTS_PER_PAGE : 0;
      const pageItems = items.slice(startIndex, startIndex + DOCUMENTS_PER_PAGE);
      const rows = pageItems.map((doc) => {
        const categoryClass = doc.evidence_category_rank === 1
          ? 'category-one'
          : doc.evidence_category_rank === 2
            ? 'category-two'
            : 'category-three';
        const tags = [
          doc.media_type && doc.media_type !== 'pdf' ? doc.media_type.toUpperCase() : '',
          doc.document_type,
          doc.extraction_method.toUpperCase(),
          doc.review_status === 'reviewed' ? 'REVIEWED' : '',
          ...(doc.capability_labels || []).slice(0, 2),
          ...(doc.themes || []).slice(0, 3),
          ...(doc.agencies || []).slice(0, 2),
        ]
          .filter(Boolean)
          .map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`)
          .join('');
        const mediaProfile = mediaProfileText(doc);
        const profile = [
          doc.date_label || (doc.year ? `${doc.year}` : 'Undated'),
          doc.location ? doc.location.label : 'No location resolved',
          mediaProfile,
          doc.capability_profile && doc.capability_profile !== 'No capability label' ? doc.capability_profile : '',
          (doc.media_type || 'pdf') === 'pdf' ? (doc.ocr_pages ? `${doc.ocr_pages} OCR pages` : 'Native text') : doc.extraction_method.replace('_', ' '),
          doc.ocr_skipped_pages ? `${doc.ocr_skipped_pages} OCR pages deferred` : '',
          doc.review_status === 'reviewed' ? 'Reviewed narrative' : '',
        ].filter(Boolean).join(' · ');
        const sourceContext = sourceContextSection(doc);
        const relatedSources = relatedSourcesSection(doc);
        const href = sourceHref(doc);
        const sourceIndex = documents.indexOf(doc);
        const previewPanel = doc.thumbnail_url
          ? `<details class="source-preview">
              <summary>${escapeHtml(sourcePreviewLabel(doc))}</summary>
              <a class="source-preview-link" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">
                <img class="source-thumbnail" src="${escapeHtml(doc.thumbnail_url)}" alt="${escapeHtml(sourcePreviewLabel(doc))} for ${escapeHtml(doc.title)}" loading="lazy">
                <span>Open source</span>
              </a>
            </details>`
          : '';
        return `
          <tr>
            <td data-label="Source">
              <p class="doc-title"><button class="source-title-button" type="button" data-source-index="${sourceIndex}" aria-haspopup="dialog">${escapeHtml(doc.title)}</button></p>
              ${previewPanel}
              <div class="tag-row">${tags}</div>
            </td>
            <td class="classification-cell" data-label="Classification">
              <span class="classification-badge ${categoryClass}">${escapeHtml(doc.evidence_category || 'Category Three')}</span>
              <div class="muted">${escapeHtml(doc.evidence_category_description || '')}</div>
            </td>
            <td data-label="Profile"><div class="profile-text">${escapeHtml(profile)}</div>${relatedSources}</td>
            <td data-label="Summary Narrative">${escapeHtml(doc.summary_narrative || 'No summary narrative available.')}${sourceContext}</td>
          </tr>`;
      }).join('');
      const paginationSummary = totalItems
        ? `Showing ${startIndex + 1}-${Math.min(startIndex + DOCUMENTS_PER_PAGE, totalItems)} of ${totalItems} sources`
        : 'No sources in register';
      document.getElementById('documentCount').textContent = `${totalItems} sources in register`;
      document.getElementById('paginationSummary').textContent = paginationSummary;
      document.getElementById('documentRows').innerHTML = rows || '<tr><td colspan="4">No sources match the current filters.</td></tr>';
      bindSourceDetailButtons();
      bindRelatedSourceLinks();
      renderPaginationControls(totalItems, totalPages);
    }

    function bindRelatedSourceLinks() {
      document.querySelectorAll('[data-related-search]').forEach((link) => {
        link.addEventListener('click', (event) => {
          const search = link.dataset.relatedSearch;
          if (!search) {
            return;
          }
          event.preventDefault();
          openDocumentsWithFilters({ search });
        });
      });
    }

    function renderPaginationControls(totalItems, totalPages) {
      const container = document.getElementById('paginationControls');
      if (!totalItems) {
        container.innerHTML = '';
        return;
      }
      const pages = [];
      const windowStart = Math.max(1, state.page - 2);
      const windowEnd = Math.min(totalPages, windowStart + 4);
      const adjustedStart = Math.max(1, windowEnd - 4);
      for (let page = adjustedStart; page <= windowEnd; page += 1) {
        pages.push(page);
      }
      const pageButtons = pages.map((page) => `
        <button
          class="pagination-button"
          type="button"
          data-page="${page}"
          aria-label="Go to page ${page}"
          ${page === state.page ? 'aria-current="page"' : ''}
        >${page}</button>`).join('');
      container.innerHTML = `
        <button class="pagination-button" type="button" data-page="${state.page - 1}" ${state.page === 1 ? 'disabled' : ''}>Previous</button>
        ${pageButtons}
        <button class="pagination-button" type="button" data-page="${state.page + 1}" ${state.page === totalPages ? 'disabled' : ''}>Next</button>`;
      container.querySelectorAll('[data-page]').forEach((button) => {
        button.addEventListener('click', () => {
          const nextPage = Number(button.dataset.page);
          if (!Number.isFinite(nextPage) || nextPage === state.page || nextPage < 1 || nextPage > totalPages) {
            return;
          }
          state.page = nextPage;
          renderDocuments(filteredDocuments());
        });
      });
    }

    function setActiveTab(tab) {
      state.activeTab = tab;
      const tabs = document.querySelectorAll('[role="tab"]');
      const panels = document.querySelectorAll('.tab-panel');
      tabs.forEach((button) => {
        const isActive = button.dataset.tab === tab;
        button.setAttribute('aria-selected', String(isActive));
        button.tabIndex = isActive ? 0 : -1;
      });
      panels.forEach((panel) => {
        panel.hidden = panel.id !== `panel-${tab}`;
      });
      updateDashboard();
    }

    function initTabs() {
      const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
      tabs.forEach((button, index) => {
        button.addEventListener('click', () => setActiveTab(button.dataset.tab));
        button.addEventListener('keydown', (event) => {
          if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft' && event.key !== 'Home' && event.key !== 'End') {
            return;
          }
          event.preventDefault();
          let nextIndex = index;
          if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
          if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
          if (event.key === 'Home') nextIndex = 0;
          if (event.key === 'End') nextIndex = tabs.length - 1;
          tabs[nextIndex].focus();
          setActiveTab(tabs[nextIndex].dataset.tab);
        });
      });
    }

    function updateDashboard() {
      const analysisItems = documents;
      const documentItems = filteredDocuments();
      const types = countBy(analysisItems, (doc) => doc.document_type).sort((a, b) => b.count - a.count).slice(0, 12);
      renderMetrics(analysisItems);
      renderExecutiveSummary();
      renderBarChart('typeChart', types, chartColors.gold, true, {
        actionDataKey: 'type',
        actionLabel: (entry) => `Filter documents to ${entry.label}`,
      });
      bindChartFilterTargets('#typeChart [data-type]', (element) => openDocumentsWithFilters({ type: element.dataset.type }));
      renderMap(analysisItems);
      renderSignals(analysisItems);
      renderThemes(analysisItems);
      renderOrganizations(analysisItems);
      renderClassifications(analysisItems);
      renderCapabilityMatrix(analysisItems);
      renderEvidenceTimeline(analysisItems);
      renderResolutionChart(analysisItems);
      renderObservationModeMatrix(analysisItems);
      renderCorroborationCategoryMatrix(analysisItems);
      renderPhenomenologyMatrix(analysisItems);
      renderRelatedSourceNetwork(analysisItems);
      renderDocuments(documentItems);
    }

    function populateFilter(select, label, values) {
      select.innerHTML = `<option value="all">All ${escapeHtml(label)}</option>` + values.map((item) => {
        const value = typeof item === 'object' ? item.value : item;
        const optionLabel = typeof item === 'object' ? item.label : item;
        return `<option value="${escapeHtml(value)}">${escapeHtml(optionLabel)}</option>`;
      }).join('');
    }

    function initFilters() {
      populateFilter(document.getElementById('typeFilter'), 'types', [...new Set(documents.map((doc) => doc.document_type))].sort());
      populateFilter(document.getElementById('methodFilter'), 'methods', [...new Set(documents.map((doc) => doc.extraction_method))].sort());
      populateFilter(document.getElementById('categoryFilter'), 'categories', analysis.evidence_category_counts.map((entry) => entry.label));
      populateFilter(document.getElementById('capabilityFilter'), 'capabilities', (analysis.capability_definitions || []).filter((definition) => (analysis.capability_matrix || []).some((entry) => entry.key === definition.key)).map((definition) => ({ label: definition.label, value: definition.key })));
      populateFilter(document.getElementById('placeFilter'), 'locations', [...new Set(documents.filter((doc) => doc.location).map((doc) => doc.location.label))].sort());
      document.getElementById('yearMin').value = state.yearMin;
      document.getElementById('yearMax').value = state.yearMax;

      document.getElementById('searchInput').addEventListener('input', (event) => {{
        state.search = event.target.value;
        resetDocumentPage();
        updateDashboard();
      }});
      document.getElementById('typeFilter').addEventListener('change', (event) => {{
        state.type = event.target.value;
        resetDocumentPage();
        updateDashboard();
      }});
      document.getElementById('methodFilter').addEventListener('change', (event) => {{
        state.method = event.target.value;
        resetDocumentPage();
        updateDashboard();
      }});
      document.getElementById('categoryFilter').addEventListener('change', (event) => {{
        state.category = event.target.value;
        resetDocumentPage();
        updateDashboard();
      }});
      document.getElementById('capabilityFilter').addEventListener('change', (event) => {{
        state.capability = event.target.value;
        resetDocumentPage();
        updateDashboard();
      }});
      document.getElementById('placeFilter').addEventListener('change', (event) => {{
        state.place = event.target.value;
        resetDocumentPage();
        updateDashboard();
      }});
      document.getElementById('yearMin').addEventListener('input', (event) => {{
        state.yearMin = Number(event.target.value) || analysis.year_min || 1900;
        resetDocumentPage();
        updateDashboard();
      }});
      document.getElementById('yearMax').addEventListener('input', (event) => {{
        state.yearMax = Number(event.target.value) || analysis.year_max || new Date().getFullYear();
        resetDocumentPage();
        updateDashboard();
      }});
      window.addEventListener('resize', () => updateDashboard());
    }

    renderMeta();
    initTabs();
    initFilters();
    initSourceDialog();
    updateDashboard();
  </script>
</body>
</html>
"""
    template = template.replace("{{", "{").replace("}}", "}")
    return (
      template
      .replace("__PAGE_TITLE__", html_escape(SEO_TITLE, quote=True))
      .replace("__PAGE_DESCRIPTION__", html_escape(SEO_DESCRIPTION, quote=True))
      .replace("__CANONICAL_URL__", html_escape(canonical_url, quote=True))
      .replace("__FAVICON_URL__", html_escape(SEO_FAVICON_PATH, quote=True))
      .replace("__SOCIAL_IMAGE_URL__", html_escape(social_image_url, quote=True))
      .replace("__DATA_JSON__", data_json)
      .replace("__WORLD_LAND_JSON__", world_land_json)
    )


def iter_source_paths(source_dir: Path) -> Iterable[Path]:
    return sorted(path for path in source_dir.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_SOURCE_EXTENSIONS)


def dedupe_source_paths(source_paths: list[Path], source_manifest: dict[str, dict[str, object]]) -> tuple[list[Path], int]:
  def sort_key(path: Path) -> tuple[int, int, str]:
    is_duplicate_suffix = 1 if re.search(r"-\d+$", path.stem) else 0
    return (is_duplicate_suffix, len(path.name), path.name.lower())

  unique_paths: list[Path] = []
  seen_source_urls: set[str] = set()
  skipped = 0

  for path in sorted(source_paths, key=sort_key):
    source_url = original_source_url(source_manifest.get(path.name))
    if source_url and source_url in seen_source_urls:
      skipped += 1
      continue
    if source_url:
      seen_source_urls.add(source_url)
    unique_paths.append(path)

  return sorted(unique_paths), skipped


def format_duration(seconds: float) -> str:
  total_seconds = max(0, int(seconds))
  minutes, remaining_seconds = divmod(total_seconds, 60)
  hours, remaining_minutes = divmod(minutes, 60)
  if hours:
    return f"{hours:d}:{remaining_minutes:02d}:{remaining_seconds:02d}"
  return f"{remaining_minutes:02d}:{remaining_seconds:02d}"


def shorten_progress_label(label: str, max_length: int) -> str:
  if max_length <= 0:
    return ""
  if len(label) <= max_length:
    return label
  if max_length <= 3:
    return "." * max_length
  remaining = max_length - 3
  head = max(1, remaining // 2)
  tail = max(1, remaining - head)
  return f"{label[:head]}...{label[-tail:]}"


@dataclass
class BuildProgressBar:
  total: int
  enabled: bool
  stream: io.TextIOBase = sys.stderr

  def __post_init__(self) -> None:
    self.completed = 0
    self.cached = 0
    self.analyzed = 0
    self.started_at = time.monotonic()
    if self.enabled:
      self.render("Starting")

  def advance(self, path: Path, *, cached: bool) -> None:
    self.completed += 1
    if cached:
      self.cached += 1
      state = "cached"
    else:
      self.analyzed += 1
      state = "analyzed"
    if self.enabled:
      self.render(f"{state}: {path.name}")

  def finish(self) -> None:
    if self.enabled:
      self.render("Complete")
      self.stream.write("\n")
      self.stream.flush()

  def render(self, status: str) -> None:
    width = max(10, shutil.get_terminal_size(fallback=(100, 20)).columns)
    ratio = 1.0 if self.total == 0 else min(1.0, self.completed / self.total)
    bar_width = max(10, min(20, width // 6))
    filled = int(round(bar_width * ratio))
    bar = f"{'#' * filled}{'-' * max(0, bar_width - filled)}"
    elapsed = time.monotonic() - self.started_at
    eta_seconds = 0.0
    if self.completed and self.completed < self.total:
      eta_seconds = (elapsed / self.completed) * (self.total - self.completed)

    prefix = (
      f"[{bar}] {self.completed}/{self.total} {ratio * 100:3.0f}% "
      f"el {format_duration(elapsed)} eta {format_duration(eta_seconds)} "
      f"c{self.cached} a{self.analyzed}"
    )
    available = max(0, width - len(prefix) - 3)
    suffix = shorten_progress_label(status, available)
    line = prefix if not suffix else f"{prefix} | {suffix}"
    max_line_length = max(1, width - 1)
    self.stream.write("\r" + line[:max_line_length].ljust(max_line_length))
    self.stream.flush()


def main() -> int:
    args = parse_args()
    validate_output_targets(args)
    source_dir = args.source_dir
    if not source_dir.exists():
        raise FileNotFoundError(f"Source directory does not exist: {source_dir}")
    if args.refresh_reviews and args.review_mode != "hybrid":
        raise ValueError("--refresh-reviews requires --review-mode hybrid")
    if args.refresh_audio_context_only and args.review_mode != "hybrid":
        raise ValueError("--refresh-audio-context-only requires --review-mode hybrid")

    ocr_limit = None if args.ocr_max_pages_per_document == 0 else args.ocr_max_pages_per_document
    resolver = PlaceResolver()
    review_overrides = load_review_overrides(args.review_file)
    reviewer = None
    if args.review_mode == "hybrid":
        reviewer = LocalModelReviewer(
            base_url=args.review_base_url,
            model_name=args.review_model,
            timeout_seconds=args.review_timeout,
            max_images=args.review_max_images,
            api_key=args.review_api_key,
            audio_transcript_model=args.audio_transcript_model,
            audio_transcript_max_seconds=args.audio_transcript_max_seconds,
            audio_transcript_timeout=args.audio_transcript_timeout,
            audio_transcript_format=args.audio_transcript_format,
        )
    source_manifest = load_source_manifest(args.source_manifest)
    source_paths = list(iter_source_paths(source_dir))
    source_paths, skipped_duplicate_paths = dedupe_source_paths(source_paths, source_manifest)
    if args.max_docs is not None:
        source_paths = source_paths[: args.max_docs]

    if args.refresh_audio_context_only:
        if reviewer is None:
            raise ValueError("--refresh-audio-context-only requires a reviewer")
        candidates, updates = refresh_audio_context_reviews(
            source_paths=source_paths,
            transcript_cache_dir=args.transcript_cache_dir,
            source_manifest=source_manifest,
            review_overrides=review_overrides,
            review_file=args.review_file,
            reviewer=reviewer,
        )
        print(f"Scanned {candidates} transcript-backed media sources")
        print(f"Updated {updates} audio context reviews")
        return 0

    documents: list[dict[str, object]] = []
    cached_documents = 0
    analyzed_documents = 0
    review_file_updates = 0
    progress = BuildProgressBar(total=len(source_paths), enabled=sys.stderr.isatty())

    for path in source_paths:
        current_review = find_review_override(review_overrides, path.name)
        cache_file = document_cache_path(args.document_cache_dir, source_dir, path)
        cached_payload = None
        if path.suffix.lower() in PDF_EXTENSIONS and not args.refresh_document_cache and not args.refresh_reviews:
            cached_payload = load_document_cache(
                cache_file=cache_file,
                path=path,
                source_dir=source_dir,
                current_review=current_review,
                page_limit=args.page_limit,
                ocr_max_pages_per_document=ocr_limit,
                ocr_dpi=args.ocr_dpi,
                min_native_chars=args.min_native_chars,
            )
            if cached_payload is not None and args.review_mode == "hybrid":
              cached_review = cached_payload.get("applied_review")
              if not review_has_required_fields(current_review or cached_review):
                cached_payload = None

        if cached_payload is not None:
            cached_review = cached_payload.get("applied_review")
            if current_review is None and isinstance(cached_review, dict):
                review_overrides[path.name] = cached_review
                write_review_overrides(args.review_file, review_overrides)
                review_file_updates += 1
            cached_document = dict(cached_payload["document"])
            if path.suffix.lower() in PDF_EXTENSIONS:
                cached_document.setdefault("media_type", "pdf")
            cached_document.setdefault("original_source_url", original_source_url(source_manifest.get(path.name)))
            cached_document.setdefault(
                "thumbnail_url",
                source_thumbnail_url(source_manifest.get(path.name), str(cached_document.get("media_type") or "pdf")),
            )
            normalize_document_after_review(cached_document)
            documents.append(cached_document)
            cached_documents += 1
            progress.advance(path, cached=True)
            continue

        review_count_before = len(review_overrides)
        if path.suffix.lower() in PDF_EXTENSIONS:
            document = analyze_document(
                path=path,
                resolver=resolver,
                source_dir=source_dir,
                source_manifest=source_manifest,
                review_overrides=review_overrides,
                reviewer=reviewer,
                force_review_refresh=args.refresh_reviews,
                page_limit=args.page_limit,
                ocr_max_pages_per_document=ocr_limit,
                ocr_dpi=args.ocr_dpi,
                min_native_chars=args.min_native_chars,
            )
        else:
            document = analyze_media_item(
                path=path,
                resolver=resolver,
                source_dir=source_dir,
                transcript_cache_dir=args.transcript_cache_dir,
                audio_transcript_mode=args.audio_transcript_mode,
                source_manifest=source_manifest,
                review_overrides=review_overrides,
                reviewer=reviewer,
                force_review_refresh=args.refresh_reviews,
            )
        documents.append(document)
        analyzed_documents += 1
        write_document_cache(
            cache_file=cache_file,
            path=path,
            source_dir=source_dir,
            document=document,
            applied_review=review_overrides.get(path.name),
            page_limit=args.page_limit,
            ocr_max_pages_per_document=ocr_limit,
            ocr_dpi=args.ocr_dpi,
            min_native_chars=args.min_native_chars,
        )
        if len(review_overrides) != review_count_before:
            write_review_overrides(args.review_file, review_overrides)
            review_file_updates += 1

        progress.advance(path, cached=False)

    progress.finish()

    corpus_signature = build_executive_summary_signature(documents)
    cached_executive_summary = normalize_executive_summary_review(
        review_overrides.get(EXECUTIVE_SUMMARY_REVIEW_KEY),
        corpus_signature,
    )
    executive_summary = cached_executive_summary
    if reviewer is not None and (args.refresh_reviews or executive_summary is None):
        generated_executive_summary = reviewer.review_corpus(documents)
        if generated_executive_summary:
            executive_summary_review = {
                **generated_executive_summary,
                "review_version": EXECUTIVE_SUMMARY_VERSION,
                "corpus_signature": corpus_signature,
                "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            }
            review_overrides[EXECUTIVE_SUMMARY_REVIEW_KEY] = executive_summary_review
            executive_summary = normalize_executive_summary_review(executive_summary_review, corpus_signature)
            write_review_overrides(args.review_file, review_overrides)
            review_file_updates += 1

    related_source_stats: dict[str, object] = {"enabled": False, "reason": "disabled"}
    if args.related_sources_mode == "auto":
        embedding_client = EmbeddingClient(
            base_url=args.embedding_base_url or args.review_base_url,
            model_name=args.embedding_model,
            timeout_seconds=args.embedding_timeout,
            api_key=args.embedding_api_key if args.embedding_api_key is not None else args.review_api_key,
        )
        documents, related_source_stats = attach_related_sources(
            documents=documents,
            embedding_client=embedding_client,
            cache_path=args.embedding_cache,
            refresh_embeddings=args.refresh_embeddings,
            related_limit=args.related_sources_limit,
            min_score=args.related_sources_min_score,
        )

    analysis = build_analysis(
        documents,
        source_dir,
        executive_summary=executive_summary,
        related_source_stats=related_source_stats,
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    if args.review_mode == "hybrid" and review_overrides:
        write_review_overrides(args.review_file, review_overrides)
    args.output_json.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    args.output_html.write_text(render_dashboard_html(analysis, args.site_url), encoding="utf-8")
    write_crawl_files(args.output_html.parent, args.site_url, str(analysis.get("generated_at") or ""))

    media_documents = sum(1 for document in documents if document.get("media_type") in MEDIA_TYPES)
    print(f"Analyzed {len(documents)} source items")
    print(f"Loaded {cached_documents} document analyses from cache")
    print(f"Processed {analyzed_documents} document analyses this run")
    print(f"Included {media_documents} media source items")
    print(f"Skipped {skipped_duplicate_paths} duplicate source files based on original source URL")
    if related_source_stats.get("enabled"):
        print(
            "Attached related sources to "
            f"{related_source_stats.get('documents_with_related_sources', 0)} documents "
            f"using {related_source_stats.get('model')}"
        )
    elif args.related_sources_mode == "auto":
        print(f"Related source generation skipped: {related_source_stats.get('reason', 'unknown')}")
    print(f"Updated review cache {review_file_updates} times")
    print(f"Wrote JSON to {args.output_json}")
    print(f"Wrote dashboard to {args.output_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
