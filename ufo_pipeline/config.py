"""Settings loaded from .env / environment, plus a preflight check of tools and endpoints."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines; already-exported variables win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _json_env(name: str) -> dict:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a JSON object, e.g. {name}={{\"reasoning_effort\":\"low\"}}")
    if not isinstance(value, dict):
        raise SystemExit(f"{name} must be a JSON object")
    return value


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


@dataclass
class Endpoint:
    """One OpenAI-compatible model served at a base URL."""

    name: str  # "remote" or "local"
    base_url: str
    api_key: str
    model: str
    # Extra request-body parameters per task ("ocr", "extract", "synthesis"), e.g. to reduce hidden reasoning.
    params: Dict[str, dict] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"


@dataclass
class Settings:
    data_dir: Path = REPO_ROOT / "data"
    site_dir: Path = REPO_ROOT / "dashboard"
    site_url: str = "https://ufo-analysis.rittmuller.com"

    remote_review: Optional[Endpoint] = None
    local_review: Optional[Endpoint] = None
    local_audio: Optional[Endpoint] = None
    remote_audio: Optional[Endpoint] = None
    local_embedding: Optional[Endpoint] = None

    ocr_concurrency: int = 16
    extract_documents: int = 4
    analysis_concurrency: int = 8
    request_timeout: int = 600
    render_dpi: int = 200
    render_max_dimension: int = 2200
    audio_chunk_seconds: int = 60
    video_frames: int = 8

    warnings: List[str] = field(default_factory=list)

    # --- derived paths -------------------------------------------------
    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "processed" / "source_manifest.json"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def pages_dir(self) -> Path:
        return self.cache_dir / "pages"

    @property
    def media_dir(self) -> Path:
        return self.cache_dir / "media"

    @property
    def analysis_dir(self) -> Path:
        return self.cache_dir / "analysis"

    @property
    def embeddings_path(self) -> Path:
        return self.cache_dir / "embeddings.json"

    @property
    def run_log_path(self) -> Path:
        return self.cache_dir / "run_issues.json"

    def review_chain(self) -> List[Endpoint]:
        """Remote first, local second; unconfigured endpoints are left out."""
        return [e for e in (self.remote_review, self.local_review) if e and e.configured]

    def audio_chain(self) -> List[Endpoint]:
        """Local first, remote second (audio stays local unless it fails)."""
        return [e for e in (self.local_audio, self.remote_audio) if e and e.configured]


def load_settings(env_file: Optional[Path] = None) -> Settings:
    load_dotenv(env_file or REPO_ROOT / ".env")
    remote_url = _env("REMOTE_UFO_REVIEW_BASE_URL")
    remote_key = _env("REMOTE_UFO_REVIEW_API_KEY")
    # UFO_REVIEW_* are the pre-refactor names; they are honoured as the local endpoint.
    local_url = _env("LOCAL_UFO_REVIEW_BASE_URL", "UFO_REVIEW_BASE_URL")
    local_key = _env("LOCAL_UFO_REVIEW_API_KEY", "UFO_REVIEW_API_KEY")
    settings = Settings(
        remote_review=Endpoint("remote", remote_url, remote_key, _env("REMOTE_UFO_REVIEW_MODEL"),
                               {"ocr": _json_env("REMOTE_UFO_OCR_PARAMS"), "extract": _json_env("REMOTE_UFO_EXTRACT_PARAMS"),
                                "synthesis": _json_env("REMOTE_UFO_SYNTHESIS_PARAMS")}),
        local_review=Endpoint("local", local_url, local_key, _env("LOCAL_UFO_REVIEW_MODEL", "UFO_REVIEW_MODEL"),
                              {"ocr": _json_env("LOCAL_UFO_OCR_PARAMS"), "extract": _json_env("LOCAL_UFO_EXTRACT_PARAMS"),
                               "synthesis": _json_env("LOCAL_UFO_SYNTHESIS_PARAMS")}),
        local_audio=Endpoint(
            "local", local_url, local_key, _env("LOCAL_UFO_AUDIO_TRANSCRIPT_MODEL", "UFO_AUDIO_TRANSCRIPT_MODEL")
        ),
        remote_audio=Endpoint("remote", remote_url, remote_key, _env("REMOTE_UFO_AUDIO_TRANSCRIPT_MODEL")),
        local_embedding=Endpoint(
            "local",
            _env("LOCAL_UFO_EMBEDDING_BASE_URL", "UFO_EMBEDDING_BASE_URL", default=local_url),
            _env("LOCAL_UFO_EMBEDDING_API_KEY", "UFO_EMBEDDING_API_KEY", default=local_key),
            _env("LOCAL_UFO_EMBEDDING_MODEL", "UFO_EMBEDDING_MODEL"),
        ),
    )
    settings.site_url = _env("UFO_SITE_URL", default=settings.site_url)
    settings.ocr_concurrency = int(_env("UFO_OCR_CONCURRENCY", default=str(settings.ocr_concurrency)))
    settings.extract_documents = int(_env("UFO_EXTRACT_DOCUMENTS", default=str(settings.extract_documents)))
    settings.analysis_concurrency = int(_env("UFO_ANALYSIS_CONCURRENCY", default=str(settings.analysis_concurrency)))
    settings.request_timeout = int(_env("UFO_REQUEST_TIMEOUT", default=str(settings.request_timeout)))
    return settings


def preflight(settings: Settings, stages: List[str]) -> Dict[str, object]:
    """Check external tools and endpoints up front so nothing degrades silently mid-run.

    Returns a status dict; raises SystemExit when a stage cannot produce valid output at all.
    """
    from . import llm  # local import: llm depends on config

    status: Dict[str, object] = {"tools": {}, "endpoints": {}}
    fatal: List[str] = []
    for tool in ("ffmpeg", "ffprobe", "tesseract"):
        found = shutil.which(tool)
        status["tools"][tool] = bool(found)
        if not found:
            if tool == "tesseract":
                settings.warnings.append("tesseract not found: the last-resort OCR fallback is unavailable")
            elif "extract" in stages:
                fatal.append(f"{tool} not found (brew install ffmpeg): video and audio cannot be processed")

    checks = [
        ("remote review", settings.remote_review, True),
        ("local review", settings.local_review, False),
        ("local audio", settings.local_audio, False),
        ("remote audio", settings.remote_audio, False),
        ("local embedding", settings.local_embedding, False),
    ]
    seen: Dict[str, object] = {}
    for label, endpoint, _required in checks:
        if not endpoint or not endpoint.configured:
            status["endpoints"][label] = "not configured"
            continue
        key = endpoint.base_url
        if key not in seen:
            seen[key] = llm.list_models(endpoint)
        models = seen[key]
        if isinstance(models, str):
            status["endpoints"][label] = f"unreachable ({models})"
        elif endpoint.model not in models:
            status["endpoints"][label] = f"model {endpoint.model!r} not served"
        else:
            status["endpoints"][label] = "ok"

    ep = status["endpoints"]
    needs_review = any(s in stages for s in ("extract", "analyze"))
    if needs_review and ep["remote review"] != "ok" and ep["local review"] != "ok":
        fatal.append(
            f"no usable review model (remote: {ep['remote review']}; local: {ep['local review']}). "
            "Set REMOTE_UFO_REVIEW_* and/or LOCAL_UFO_REVIEW_* in .env"
        )
    elif needs_review and ep["remote review"] != "ok":
        settings.warnings.append(f"remote review model unavailable ({ep['remote review']}): using the local backup only")
    elif needs_review and ep["local review"] != "ok":
        settings.warnings.append(f"local review backup unavailable ({ep['local review']}): remote failures fall to Tesseract")
    if "extract" in stages and ep["local audio"] != "ok" and ep["remote audio"] != "ok":
        settings.warnings.append("no audio transcription model available: only captions will be used")
    if "embed" in stages and ep["local embedding"] != "ok":
        settings.warnings.append(f"embedding model unavailable ({ep['local embedding']}): related-report links will be skipped")

    # Drop endpoints that failed the check so chains do not waste retries on them.
    if ep["remote review"] != "ok":
        settings.remote_review = None
    if ep["local review"] != "ok":
        settings.local_review = None
    if ep["local audio"] != "ok":
        settings.local_audio = None
    if ep["remote audio"] != "ok":
        settings.remote_audio = None
    if ep["local embedding"] != "ok":
        settings.local_embedding = None

    status["warnings"] = list(settings.warnings)
    if fatal:
        raise SystemExit("Preflight failed:\n  - " + "\n  - ".join(fatal))
    return status
