#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import math
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass
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
DEFAULT_SOURCE_MANIFEST = REPO_ROOT / "data" / "processed" / "source_manifest.json"
DEFAULT_REVIEW_FILE = REPO_ROOT / "data" / "reviewed" / "document_reviews.json"


DOCUMENT_CACHE_VERSION = 2
SOURCE_MANIFEST_VERSION = 1
EXECUTIVE_SUMMARY_REVIEW_KEY = "__corpus_executive_summary__"
EXECUTIVE_SUMMARY_VERSION = 1


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
    ("Debrief / Reporting Form", ["debrief", "reporting form"]),
    ("Diplomatic Cable", ["cable"]),
    ("Email Correspondence", ["email correspondence", "email correspondance", "email"]),
    ("Imagery", ["photo", "composite sketch", "sketch"]),
    ("Statement", ["statement"]),
    ("Archival File", ["numeric file", "numerical file", "general", "records", "serial"]),
]

DOCUMENT_TYPE_LABELS = {label for label, _ in DOCUMENT_TYPE_PATTERNS} | {"Research File", "UAP Report"}

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
        description="Build a self-contained UFO research dashboard from PDFs in data/sources.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
    default=DEFAULT_SOURCE_DIR,
        help="Directory containing source PDFs.",
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
        "--document-cache-dir",
        type=Path,
    default=DEFAULT_DOCUMENT_CACHE_DIR,
        help="Directory for per-document JSON cache files used to resume interrupted runs.",
    )
    parser.add_argument(
      "--source-manifest",
      type=Path,
    default=DEFAULT_SOURCE_MANIFEST,
      help="Optional JSON manifest mapping local PDF filenames to original source URLs.",
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
      default="manual",
      help="Review strategy: manual uses only saved review-file overrides, hybrid also generates or refreshes reviews with the local model when requested.",
    )
    parser.add_argument(
      "--refresh-reviews",
      action="store_true",
      help="With --review-mode hybrid, rerun the local model review even for documents that already have saved reviews.",
    )
    parser.add_argument(
      "--review-base-url",
      default="http://192.168.68.50:1234",
      help="Base URL for the local OpenAI-compatible review model endpoint.",
    )
    parser.add_argument(
      "--review-model",
      default="qwen3.6-35b-a3b-ud-mlx",
      help="Model name to use for local multimodal review generation.",
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


def load_source_manifest(path: Path) -> dict[str, str]:
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

  urls: dict[str, str] = {}
  for entry in entries:
    if not isinstance(entry, dict):
      continue
    filename = entry.get("filename")
    original_source_url = entry.get("original_source_url")
    if isinstance(filename, str) and isinstance(original_source_url, str) and original_source_url:
      urls[filename] = original_source_url
  return urls


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

  payload["document"] = annotate_evidence_classification(document)

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


def apply_review_override(document: dict[str, object], review: dict[str, object] | None) -> dict[str, object]:
  document.setdefault("visual_observations", None)
  document.setdefault("review_status", "generated")
  document.setdefault("review_source", "scripted_extraction")
  if not review:
    return annotate_evidence_classification(document)

  merged = dict(document)
  merged.update(review)
  merged.setdefault("review_status", "reviewed")
  merged.setdefault("review_source", "manual_ai_review")
  return annotate_evidence_classification(merged)


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

  def _render_review_image(self, page: fitz.Page) -> str:
    pixmap = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2), alpha=False)
    encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
    return f"data:image/png;base64,{encoded}"

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

  def _chat(self, messages: list[dict[str, object]], max_tokens: int = 6000) -> dict[str, object]:
    payload = {
      "model": self.model_name,
      "messages": messages,
      "temperature": 0,
      "max_tokens": max_tokens,
    }
    request = urllib_request.Request(
      url=f"{self.base_url.rstrip('/')}/v1/chat/completions",
      data=json.dumps(payload).encode("utf-8"),
      headers={"Content-Type": "application/json"},
      method="POST",
    )
    with urllib_request.urlopen(request, timeout=self.timeout_seconds) as response:
      return json.loads(response.read().decode("utf-8"))

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
      "Return only a JSON object with these keys: summary_narrative, visual_observations, document_type, themes, agencies, location_label, evidence_category. "
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
      response = self._chat([{"role": "user", "content": content}])
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

  def review_corpus(self, documents: list[dict[str, object]]) -> dict[str, object] | None:
    summary_blocks: list[str] = []
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
      "Do not overstate certainty; distinguish concrete patterns in the summaries from interpretation. "
      "Return only a JSON object with key executive_summary_sections. "
      "executive_summary_sections must be an array of 3 to 5 polished paragraphs, each 2 to 4 sentences. "
      "Avoid bullets and avoid generic caveats.\n\n"
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
  source_urls: dict[str, str],
    review_overrides: dict[str, dict[str, object]],
    reviewer: LocalModelReviewer | None,
  force_review_refresh: bool,
    page_limit: int | None,
    ocr_max_pages_per_document: int | None,
    ocr_dpi: int,
    min_native_chars: int,
) -> dict[str, object]:
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
      "original_source_url": source_urls.get(path.name),
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
    review = review_overrides.get(path.name)
    if reviewer is not None and (review is None or force_review_refresh or not review_has_evidence_category(review)):
      generated_review = reviewer.review_document(document, doc, combined_text, resolver)
      if generated_review:
        updated_review = dict(review) if isinstance(review, dict) else {}
        updated_review.update(generated_review)
        review_overrides[path.name] = updated_review
        review = updated_review
    return apply_review_override(document, review)


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
          "location": location.get("label") if isinstance(location, dict) else None,
          "summary_narrative": document.get("summary_narrative"),
        }
      )
    payload = json.dumps(signature_documents, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
      "source": review.get("review_source"),
      "generated_at": review.get("generated_at"),
    }


def build_analysis(
    documents: list[dict[str, object]],
    source_dir: Path,
    executive_summary: dict[str, object] | None = None,
) -> dict[str, object]:
    year_counts = Counter(doc["year"] for doc in documents if isinstance(doc.get("year"), int))
    type_counts = Counter(doc["document_type"] for doc in documents)
    method_counts = Counter(doc["extraction_method"] for doc in documents)
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
        "research_signals": build_research_signals(documents),
        "executive_summary": executive_summary,
        "documents": documents,
    }
    return analysis


def render_dashboard_html(analysis: dict[str, object]) -> str:
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
    template = """<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>UFO Research</title>
  <style>
    :root {{
      --paper: #f7f0df;
      --paper-deep: #efe3c6;
      --ink: #1f2e38;
      --muted: #56636b;
      --gold: #b88432;
      --gold-soft: rgba(184, 132, 50, 0.2);
      --teal: #2d7075;
      --teal-soft: rgba(45, 112, 117, 0.14);
      --panel: rgba(255, 251, 240, 0.86);
      --line: rgba(31, 46, 56, 0.12);
      --shadow: 0 20px 50px rgba(72, 61, 38, 0.12);
    }}

    * {{ box-sizing: border-box; }}

    body {{
      margin: 0;
      color: var(--ink);
      font-family: "Avenir Next", "Gill Sans", "Trebuchet MS", sans-serif;
      background:
        radial-gradient(circle at top, rgba(255, 247, 225, 0.95), rgba(247, 240, 223, 0.96) 38%, rgba(236, 225, 197, 0.98) 100%),
        linear-gradient(135deg, rgba(184, 132, 50, 0.06), transparent 30%),
        linear-gradient(45deg, rgba(45, 112, 117, 0.05), transparent 34%);
      min-height: 100vh;
    }}

    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        radial-gradient(circle at 10% 18%, rgba(184, 132, 50, 0.22) 0 2px, transparent 3px),
        radial-gradient(circle at 84% 14%, rgba(45, 112, 117, 0.18) 0 1.5px, transparent 2.5px),
        radial-gradient(circle at 70% 30%, rgba(184, 132, 50, 0.16) 0 1.5px, transparent 2.5px),
        linear-gradient(transparent 0, rgba(255,255,255,0.2) 50%, transparent 100%);
      opacity: 0.7;
    }}

    .shell {{
      width: min(1400px, calc(100vw - 32px));
      margin: 24px auto 48px;
      position: relative;
      z-index: 1;
    }}

    .hero {{
      padding: 28px 30px 24px;
      border: 1px solid rgba(184, 132, 50, 0.28);
      background: linear-gradient(180deg, rgba(255, 251, 240, 0.94), rgba(248, 241, 224, 0.84));
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }}

    .hero::before,
    .hero::after {{
      content: "";
      position: absolute;
      inset: 10px;
      border: 1px solid rgba(184, 132, 50, 0.18);
      pointer-events: none;
    }}

    .hero::after {{
      inset: 20px;
      clip-path: polygon(4% 0, 96% 0, 100% 12%, 100% 88%, 96% 100%, 4% 100%, 0 88%, 0 12%);
    }}

    .eyebrow {{
      letter-spacing: 0.32em;
      text-transform: uppercase;
      font-size: 0.78rem;
      color: var(--gold);
      margin-bottom: 10px;
    }}

    h1 {{
      margin: 0;
      font-size: clamp(2rem, 3vw, 3.4rem);
      line-height: 1;
      font-family: "Baskerville", "Times New Roman", serif;
      letter-spacing: 0.04em;
    }}

    .subtitle {{
      margin: 12px 0 0;
      max-width: 820px;
      color: var(--muted);
      font-size: 1rem;
      line-height: 1.6;
    }}

    .hero-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 18px;
    }}

    .hero-meta span,
    .pill {{
      border: 1px solid rgba(31, 46, 56, 0.14);
      background: rgba(255, 255, 255, 0.55);
      padding: 8px 12px;
      font-size: 0.84rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}

    .tab-strip {{
      margin-top: 18px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}

    .tab-button {{
      appearance: none;
      border: 1px solid rgba(31, 46, 56, 0.14);
      background: rgba(255, 255, 255, 0.55);
      color: var(--muted);
      padding: 11px 16px;
      font: inherit;
      font-size: 0.82rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      cursor: pointer;
      transition: background 140ms ease, color 140ms ease, border-color 140ms ease, transform 140ms ease;
    }}

    .tab-button:hover,
    .tab-button:focus-visible {{
      outline: none;
      border-color: rgba(184, 132, 50, 0.44);
      color: var(--ink);
      transform: translateY(-1px);
    }}

    .tab-button[aria-selected="true"] {{
      background: linear-gradient(180deg, rgba(184, 132, 50, 0.2), rgba(255, 251, 240, 0.92));
      border-color: rgba(184, 132, 50, 0.44);
      color: var(--ink);
    }}

    .tab-panel[hidden] {{
      display: none;
    }}

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
      border: 1px solid rgba(31, 46, 56, 0.14);
      background: rgba(255, 255, 255, 0.72);
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
      border-color: rgba(184, 132, 50, 0.44);
      background: rgba(255, 251, 240, 0.95);
    }}

    .pagination-button[aria-current="page"] {{
      background: rgba(45, 112, 117, 0.16);
      border-color: rgba(45, 112, 117, 0.28);
      color: #154246;
    }}

    .pagination-button:disabled {{
      cursor: default;
      opacity: 0.45;
    }}

    .filters,
    .panel {{
      margin-top: 18px;
      background: var(--panel);
      border: 1px solid rgba(31, 46, 56, 0.1);
      box-shadow: var(--shadow);
    }}

    .filters {{
      padding: 18px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      align-items: end;
    }}

    label {{
      display: grid;
      gap: 6px;
      font-size: 0.8rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
    }}

    input,
    select {{
      width: 100%;
      border: 1px solid rgba(31, 46, 56, 0.16);
      background: rgba(255, 255, 255, 0.82);
      color: var(--ink);
      padding: 11px 12px;
      font: inherit;
    }}

    .grid {{
      margin-top: 18px;
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 18px;
    }}

    .span-12 {{ grid-column: span 12; }}
    .span-8 {{ grid-column: span 8; }}
    .span-6 {{ grid-column: span 6; }}
    .span-4 {{ grid-column: span 4; }}

    .panel {{
      padding: 18px;
      overflow: hidden;
    }}

    .panel h2 {{
      margin: 0 0 14px;
      font-size: 0.96rem;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--muted);
    }}

    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 14px;
    }}

    .metric {{
      padding: 14px;
      border: 1px solid rgba(184, 132, 50, 0.18);
      background: linear-gradient(180deg, rgba(255,255,255,0.82), rgba(247, 240, 223, 0.85));
    }}

    .metric .value {{
      font-family: "Baskerville", serif;
      font-size: 2rem;
      color: var(--teal);
      line-height: 1;
    }}

    .metric .label {{
      margin-top: 8px;
      font-size: 0.82rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
    }}

    .executive-summary {{
      display: grid;
      gap: 14px;
      max-width: 1120px;
    }}

    .executive-summary p {{
      margin: 0;
      color: var(--ink);
      font-size: 1rem;
      line-height: 1.72;
    }}

    .executive-summary-meta {{
      margin-top: 2px;
      color: var(--muted);
      font-size: 0.8rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }}

    .svg-wrap {{ min-height: 280px; }}

    .chart-note,
    .muted {{ color: var(--muted); }}

    .theme-grid,
    .signal-list,
    .keyword-cloud,
    .hotspot-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}

    .theme-chip,
    .keyword {{
      border: 1px solid rgba(45, 112, 117, 0.18);
      background: var(--teal-soft);
      padding: 9px 11px;
    }}

    .signal {{
      width: 100%;
      padding: 12px 14px;
      border-left: 3px solid var(--gold);
      background: rgba(255, 255, 255, 0.56);
      line-height: 1.5;
    }}

    .keyword {{
      font-size: calc(0.78rem + var(--scale, 0) * 0.5rem);
      background: rgba(184, 132, 50, 0.1);
      border-color: rgba(184, 132, 50, 0.22);
    }}

    .hotspot {{
      width: 100%;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px solid var(--line);
      padding: 10px 0;
    }}

    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.94rem;
    }}

    th,
    td {{
      text-align: left;
      padding: 12px 10px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}

    th {{
      letter-spacing: 0.12em;
      text-transform: uppercase;
      font-size: 0.72rem;
      color: var(--muted);
      position: sticky;
      top: 0;
      background: rgba(247, 240, 223, 0.96);
    }}

    .doc-title {{
      margin: 0;
      font-size: 1rem;
      font-weight: 700;
    }}

    .doc-title a {{
      color: var(--ink);
      text-decoration: none;
      border-bottom: 1px solid rgba(31, 46, 56, 0.18);
    }}

    .tag-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }}

    .tag {{
      background: rgba(45, 112, 117, 0.08);
      border: 1px solid rgba(45, 112, 117, 0.16);
      padding: 4px 8px;
      font-size: 0.78rem;
    }}

    .classification-badge {{
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border: 1px solid rgba(31, 46, 56, 0.18);
      font-size: 0.78rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      background: rgba(255, 255, 255, 0.82);
    }}

    .classification-badge.category-one {{
      background: rgba(184, 132, 50, 0.18);
      border-color: rgba(184, 132, 50, 0.32);
      color: #6b4613;
    }}

    .classification-badge.category-two {{
      background: rgba(45, 112, 117, 0.12);
      border-color: rgba(45, 112, 117, 0.28);
      color: #1f5960;
    }}

    .classification-badge.category-three {{
      background: rgba(31, 46, 56, 0.08);
      border-color: rgba(31, 46, 56, 0.16);
      color: #46535b;
    }}

    .classification-cell strong {{
      display: block;
      margin-bottom: 6px;
    }}

    .classification-note {{
      margin-top: 10px;
      font-size: 0.82rem;
      color: var(--muted);
      line-height: 1.55;
    }}

    .table-wrap {{
      overflow: visible;
    }}

    .footer-note {{
      margin-top: 14px;
      font-size: 0.84rem;
      color: var(--muted);
    }}

    @media (max-width: 1120px) {{
      .span-8,
      .span-6,
      .span-4 {{ grid-column: span 12; }}
    }}

    @media (max-width: 640px) {{
      .shell {{ width: min(100vw - 18px, 100%); margin-top: 10px; }}
      .hero {{ padding: 22px 18px 18px; }}
      .panel {{ padding: 14px; }}
      th:nth-child(4), td:nth-child(4) {{ display: none; }}
    }}
  </style>
</head>
<body>
  <div class=\"shell\">
  <title>UAP/UFO Research</title>
      <div class=\"eyebrow\">Research Dashboard</div>
      <h1>UAP/UFO Research</h1>
  <p class="subtitle">This dashboard organizes the public war.gov/ufo archive of unresolved UAP records released by the Department of War with ODNI support. The collection spans decades of historical government files, many reviewed from paper records, and the cases remain unresolved because the government has not made a definitive determination about the observed phenomena.</p>
      <div class=\"hero-meta\">
        <span id=\"metaDocs\"></span>
        <span id=\"metaPages\"></span>
        <span id=\"metaYears\"></span>
        <span id=\"metaGenerated\"></span>
      </div>
    </section>

    <div class=\"tab-strip\" role=\"tablist\" aria-label=\"Dashboard sections\">
      <button class=\"tab-button\" id=\"tab-overview\" type=\"button\" role=\"tab\" aria-selected=\"true\" aria-controls=\"panel-overview\" data-tab=\"overview\">Analysis</button>
      <button class=\"tab-button\" id=\"tab-documents\" type=\"button\" role=\"tab\" aria-selected=\"false\" aria-controls=\"panel-documents\" data-tab=\"documents\" tabindex=\"-1\">Documents</button>
    </div>

    <section class=\"tab-panel\" id=\"panel-overview\" role=\"tabpanel\" aria-labelledby=\"tab-overview\">
      <div class=\"grid\">
        <section class=\"panel span-12\">
          <h2>Corpus Summary</h2>
          <div id=\"metrics\" class=\"metrics\"></div>
        </section>

        <section class=\"panel span-12\">
          <h2>Executive Summary</h2>
          <div id=\"executiveSummary\" class=\"executive-summary\"></div>
        </section>

        <section class=\"panel span-8\">
          <h2>Document Timeline</h2>
          <div id=\"timelineChart\" class=\"svg-wrap\"></div>
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
          <h2>Location Hotspots</h2>
          <div id=\"hotspots\" class=\"hotspot-list\"></div>
        </section>

        <section class=\"panel span-4\">
          <h2>Document Types</h2>
          <div id=\"typeChart\" class=\"svg-wrap\"></div>
        </section>

        <section class=\"panel span-4\">
          <h2>Organizations Mentioned</h2>
          <div id=\"organizationChart\" class=\"svg-wrap\"></div>
        </section>

        <section class=\"panel span-12\">
          <h2>Recurring Themes</h2>
          <div id=\"themeGrid\" class=\"theme-grid\"></div>
        </section>
      </div>
    </section>

    <section class=\"tab-panel\" id=\"panel-documents\" role=\"tabpanel\" aria-labelledby=\"tab-documents\" hidden>
      <section class=\"filters\">
        <label>Keyword Search
          <input id=\"searchInput\" type=\"search\" placeholder=\"Search titles, summaries, themes, agencies\">
        </label>
        <label>Document Type
          <select id=\"typeFilter\"></select>
        </label>
        <label>Extraction Method
          <select id=\"methodFilter\"></select>
        </label>
        <label>Evidence Category
          <select id="categoryFilter"></select>
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

      <section class=\"panel\">
        <h2>Document Register</h2>
        <div class=\"table-wrap\">
          <table>
            <thead>
              <tr>
                <th>Document</th>
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
        <div class=\"footer-note\">Rows marked with OCR page counts used image-based text recovery on low-text pages. Documents that hit the OCR cap may deserve a deeper rerun for exhaustive archival study.</div>
      </section>
    </section>
  </div>

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
      place: 'all',
      yearMin: analysis.year_min || 1900,
      yearMax: analysis.year_max || new Date().getFullYear(),
    };

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
        if (state.place !== 'all' && (!doc.location || doc.location.label !== state.place)) return false;
        if (year && year < state.yearMin) return false;
        if (year && year > state.yearMax) return false;
        if (!search) return true;
        const haystack = [
          doc.title,
          doc.summary_narrative,
          doc.visual_observations || '',
          ...(doc.themes || []),
          ...(doc.agencies || []),
          ...(doc.top_terms || []),
          doc.evidence_category || '',
          doc.evidence_category_description || '',
          doc.location ? doc.location.label : '',
        ].join(' ').toLowerCase();
        return haystack.includes(search);
      });
    }

    function renderMeta() {
      document.getElementById('metaDocs').textContent = `${analysis.document_count} documents`;
      document.getElementById('metaPages').textContent = `${analysis.total_pages} pages`;
      document.getElementById('metaYears').textContent = analysis.year_min && analysis.year_max
        ? `${analysis.year_min}–${analysis.year_max}`
        : 'undated corpus';
      document.getElementById('metaGenerated').textContent = `Generated ${analysis.generated_at.replace('T', ' ').replace('Z', ' UTC')}`;
    }

    function renderMetrics(items) {
      const years = items.map((doc) => doc.year).filter(Boolean);
      const ocrDocs = items.filter((doc) => doc.extraction_method === 'ocr' || doc.extraction_method === 'hybrid').length;
      const located = items.filter((doc) => doc.location).length;
      const pages = items.reduce((sum, doc) => sum + (doc.page_count || 0), 0);
      const cards = [
        { value: items.length, label: 'Documents in view' },
        { value: pages, label: 'Pages represented' },
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

    function renderBarChart(targetId, entries, color, horizontal = false) {
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

      if (horizontal) {
        const barHeight = plotHeight / entries.length;
        const bars = entries.map((entry, index) => {
          const y = margin.top + index * barHeight + 7;
          const barWidth = (entry.count / maxCount) * plotWidth;
          return `
            <text x="${margin.left - 12}" y="${y + 14}" text-anchor="end" font-size="12" fill="#56636b">${escapeHtml(entry.label)}</text>
            <rect x="${margin.left}" y="${y}" width="${barWidth}" height="20" fill="${color}" opacity="0.82"></rect>
            <text x="${margin.left + barWidth + 8}" y="${y + 14}" font-size="12" fill="#1f2e38">${entry.count}</text>`;
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
          <rect x="${x}" y="${y}" width="${Math.max(barWidth - 12, 6)}" height="${h}" fill="${color}" opacity="0.86"></rect>
          <text x="${x + Math.max(barWidth - 12, 6) / 2}" y="${margin.top + plotHeight + 18}" text-anchor="middle" font-size="11" fill="#56636b">${showLabel ? escapeHtml(entry.label) : ''}</text>`;
      }).join('');

      const grid = Array.from({ length: 5 }, (_, index) => {
        const value = Math.round((maxCount / 4) * index);
        const y = margin.top + plotHeight - (plotHeight * index / 4);
        return `
          <line x1="${margin.left}" x2="${width - margin.right}" y1="${y}" y2="${y}" stroke="rgba(31,46,56,0.1)"></line>
          <text x="${margin.left - 10}" y="${y + 4}" text-anchor="end" font-size="11" fill="#56636b">${value}</text>`;
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
        return `<line x1="20" x2="${width - 20}" y1="${y}" y2="${y}" stroke="rgba(31,46,56,0.08)"></line>`;
      }).join('') + Array.from({ length: 7 }, (_, index) => {
        const x = 20 + ((width - 40) / 6) * index;
        return `<line x1="${x}" x2="${x}" y1="20" y2="${height - 20}" stroke="rgba(31,46,56,0.08)"></line>`;
      }).join('');
      const landLayer = (worldLand.geometries || []).map((geometry) => {
        const path = geometryToPath(geometry);
        if (!path) return '';
        return `<path d="${path}" fill="rgba(184,132,50,0.2)" stroke="rgba(31,46,56,0.24)" stroke-width="0.8" vector-effect="non-scaling-stroke"></path>`;
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
      ].map((label) => `<text x="${label.x}" y="${label.y}" text-anchor="${label.anchor}" font-size="11" fill="#56636b">${label.text}</text>`).join('');
      const regionLayer = regionLabels.map((label) => `<text x="${projectX(label.lon)}" y="${projectY(label.lat)}" text-anchor="middle" font-size="11" fill="rgba(31,46,56,0.5)">${escapeHtml(label.text)}</text>`).join('');

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
          <circle cx="${x}" cy="${y}" r="${radius}" fill="rgba(45,112,117,0.22)" stroke="#2d7075" stroke-width="1.6">
            <title>${escapeHtml(title)}</title>
          </circle>
          <circle cx="${x}" cy="${y}" r="2.5" fill="#b88432">
            <title>${escapeHtml(title)}</title>
          </circle>
          ${canPlaceLabel ? `<text x="${x}" y="${labelY}" text-anchor="middle" font-size="11" fill="#1f2e38">${escapeHtml(title)}</text>` : ''}`;
      }).join('');

      node.innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img">
          <rect x="20" y="20" width="${width - 40}" height="${height - 40}" fill="rgba(219,236,236,0.42)" stroke="rgba(184,132,50,0.28)"></rect>
          ${graticule}
          ${landLayer}
          ${regionLayer}
          ${bubbles}
          ${axisLabels}
          <text x="24" y="34" font-size="12" fill="#56636b">World Reference Map</text>
        </svg>`;
    }

    function renderSignals(items) {
      const years = items.map((doc) => doc.year).filter(Boolean);
      const hotspots = countBy(items.filter((doc) => doc.location), (doc) => doc.location.label).sort((a, b) => b.count - a.count);
      const themes = countBy(items.flatMap((doc) => (doc.themes || []).map((theme) => ({ theme }))), (entry) => entry.theme).sort((a, b) => b.count - a.count);
      const signals = [];
      if (years.length) {
        const counts = countBy(items.filter((doc) => doc.year), (doc) => doc.year).sort((a, b) => b.count - a.count);
        signals.push(`Peak activity in the corpus lands in ${counts[0].label} with ${counts[0].count} documents.`);
      }
      if (hotspots.length) {
        signals.push(`Geolocation clustering is strongest around ${hotspots[0].label}.`);
      }
      if (themes.length) {
        signals.push(`The leading themes are ${themes.slice(0, 2).map((entry) => entry.label.toLowerCase()).join(' and ')}.`);
      }
      document.getElementById('researchSignals').innerHTML = signals.map((signal) => `<div class="signal">${escapeHtml(signal)}</div>`).join('');
    }

    function renderThemes(items) {
      const themes = countBy(items.flatMap((doc) => (doc.themes || []).map((theme) => ({ theme }))), (entry) => entry.theme)
        .sort((a, b) => b.count - a.count)
        .slice(0, 10);
      document.getElementById('themeGrid').innerHTML = themes.length
        ? themes.map((entry) => `<div class="theme-chip"><strong>${escapeHtml(entry.label)}</strong><div class="muted">${entry.count} documents</div></div>`).join('')
        : '<p class="chart-note">No themes match the current filters.</p>';
    }

    function renderOrganizations(items) {
      const organizations = countBy(
        items.flatMap((doc) => (doc.agencies || []).map((agency) => ({ agency }))),
        (entry) => entry.agency,
      )
        .sort((a, b) => b.count - a.count)
        .slice(0, 12);
      renderBarChart('organizationChart', organizations, '#7a9e45', true);
    }

    function renderClassifications(items) {
      const categories = analysis.evidence_category_counts
        .map((entry) => ({
          label: entry.label,
          rank: entry.rank,
          count: items.filter((doc) => doc.evidence_category === entry.label).length,
        }))
        .sort((a, b) => a.rank - b.rank);
      renderBarChart('classificationChart', categories, '#8b5e28', true);
    }

    function renderHotspots(items) {
      const hotspots = countBy(items.filter((doc) => doc.location), (doc) => doc.location.label)
        .sort((a, b) => b.count - a.count)
        .slice(0, 10);
      document.getElementById('hotspots').innerHTML = hotspots.length
        ? hotspots.map((entry) => `<div class="hotspot"><span>${escapeHtml(entry.label)}</span><strong>${entry.count}</strong></div>`).join('')
        : '<p class="chart-note">No location clusters match the current filters.</p>';
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
          doc.document_type,
          doc.extraction_method.toUpperCase(),
          doc.review_status === 'reviewed' ? 'REVIEWED' : '',
          ...(doc.themes || []).slice(0, 3),
          ...(doc.agencies || []).slice(0, 2),
        ]
          .filter(Boolean)
          .map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`)
          .join('');
        const profile = [
          doc.date_label || (doc.year ? `${doc.year}` : 'Undated'),
          doc.location ? doc.location.label : 'No location resolved',
          `${doc.page_count} pages`,
          doc.ocr_pages ? `${doc.ocr_pages} OCR pages` : 'Native text',
          doc.ocr_skipped_pages ? `${doc.ocr_skipped_pages} OCR pages deferred` : '',
          doc.review_status === 'reviewed' ? 'Reviewed narrative' : '',
        ].filter(Boolean).join(' · ');
        const visualNote = doc.visual_observations
          ? `<div class="muted">Visual: ${escapeHtml(doc.visual_observations)}</div>`
          : '';
        return `
          <tr>
            <td>
              <p class="doc-title"><a href="${escapeHtml(doc.original_source_url || doc.source_href)}">${escapeHtml(doc.title)}</a></p>
              <div class="tag-row">${tags}</div>
            </td>
            <td class="classification-cell">
              <span class="classification-badge ${categoryClass}">${escapeHtml(doc.evidence_category || 'Category Three')}</span>
              <div class="muted">${escapeHtml(doc.evidence_category_description || '')}</div>
            </td>
            <td>${escapeHtml(profile)}</td>
            <td>${escapeHtml(doc.summary_narrative || 'No summary narrative available.')}${visualNote}</td>
          </tr>`;
      }).join('');
      const paginationSummary = totalItems
        ? `Showing ${startIndex + 1}-${Math.min(startIndex + DOCUMENTS_PER_PAGE, totalItems)} of ${totalItems} documents`
        : 'No documents in register';
      document.getElementById('documentCount').textContent = `${totalItems} documents in register`;
      document.getElementById('paginationSummary').textContent = paginationSummary;
      document.getElementById('documentRows').innerHTML = rows || '<tr><td colspan="4">No documents match the current filters.</td></tr>';
      renderPaginationControls(totalItems, totalPages);
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
      const yearly = countBy(analysisItems.filter((doc) => doc.year), (doc) => doc.year).sort((a, b) => Number(a.label) - Number(b.label));
      const types = countBy(analysisItems, (doc) => doc.document_type).sort((a, b) => b.count - a.count).slice(0, 12);
      renderMetrics(analysisItems);
      renderExecutiveSummary();
      renderBarChart('timelineChart', yearly, '#2d7075', false);
      renderBarChart('typeChart', types, '#b88432', true);
      renderMap(analysisItems);
      renderSignals(analysisItems);
      renderThemes(analysisItems);
      renderOrganizations(analysisItems);
      renderClassifications(analysisItems);
      renderHotspots(analysisItems);
      renderDocuments(documentItems);
    }

    function populateFilter(select, label, values) {
      select.innerHTML = `<option value="all">All ${escapeHtml(label)}</option>` + values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('');
    }

    function initFilters() {
      populateFilter(document.getElementById('typeFilter'), 'types', [...new Set(documents.map((doc) => doc.document_type))].sort());
      populateFilter(document.getElementById('methodFilter'), 'methods', [...new Set(documents.map((doc) => doc.extraction_method))].sort());
      populateFilter(document.getElementById('categoryFilter'), 'categories', analysis.evidence_category_counts.map((entry) => entry.label));
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
    updateDashboard();
  </script>
</body>
</html>
"""
    template = template.replace("{{", "{").replace("}}", "}")
    return template.replace("__DATA_JSON__", data_json).replace("__WORLD_LAND_JSON__", world_land_json)


def iter_pdf_paths(source_dir: Path) -> Iterable[Path]:
    return sorted(path for path in source_dir.iterdir() if path.is_file() and path.suffix.lower() == ".pdf")


def dedupe_pdf_paths(pdf_paths: list[Path], source_urls: dict[str, str]) -> tuple[list[Path], int]:
  def sort_key(path: Path) -> tuple[int, int, str]:
    is_duplicate_suffix = 1 if re.search(r"-\d+$", path.stem) else 0
    return (is_duplicate_suffix, len(path.name), path.name.lower())

  unique_paths: list[Path] = []
  seen_source_urls: set[str] = set()
  skipped = 0

  for path in sorted(pdf_paths, key=sort_key):
    source_url = source_urls.get(path.name)
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
        )
    source_urls = load_source_manifest(args.source_manifest)
    pdf_paths = list(iter_pdf_paths(source_dir))
    pdf_paths, skipped_duplicate_paths = dedupe_pdf_paths(pdf_paths, source_urls)
    if args.max_docs is not None:
        pdf_paths = pdf_paths[: args.max_docs]

    documents: list[dict[str, object]] = []
    cached_documents = 0
    analyzed_documents = 0
    review_file_updates = 0
    progress = BuildProgressBar(total=len(pdf_paths), enabled=sys.stderr.isatty())

    for path in pdf_paths:
        current_review = review_overrides.get(path.name)
        cache_file = document_cache_path(args.document_cache_dir, source_dir, path)
        cached_payload = None
        if not args.refresh_document_cache and not args.refresh_reviews:
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
              if not review_has_evidence_category(current_review or cached_review):
                cached_payload = None

        if cached_payload is not None:
            cached_review = cached_payload.get("applied_review")
            if current_review is None and isinstance(cached_review, dict):
                review_overrides[path.name] = cached_review
                write_review_overrides(args.review_file, review_overrides)
                review_file_updates += 1
            documents.append(cached_payload["document"])
            cached_documents += 1
            progress.advance(path, cached=True)
            continue

        review_count_before = len(review_overrides)
        document = analyze_document(
            path=path,
            resolver=resolver,
            source_dir=source_dir,
          source_urls=source_urls,
            review_overrides=review_overrides,
            reviewer=reviewer,
            force_review_refresh=args.refresh_reviews,
            page_limit=args.page_limit,
            ocr_max_pages_per_document=ocr_limit,
            ocr_dpi=args.ocr_dpi,
            min_native_chars=args.min_native_chars,
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

    analysis = build_analysis(documents, source_dir, executive_summary=executive_summary)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    if args.review_mode == "hybrid" and review_overrides:
        write_review_overrides(args.review_file, review_overrides)
    args.output_json.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    args.output_html.write_text(render_dashboard_html(analysis), encoding="utf-8")

    print(f"Analyzed {len(documents)} documents")
    print(f"Loaded {cached_documents} document analyses from cache")
    print(f"Processed {analyzed_documents} document analyses this run")
    print(f"Skipped {skipped_duplicate_paths} duplicate PDF files based on original source URL")
    print(f"Updated review cache {review_file_updates} times")
    print(f"Wrote JSON to {args.output_json}")
    print(f"Wrote dashboard to {args.output_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
