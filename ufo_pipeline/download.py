"""Download PURSUE release media from war.gov and maintain the source manifest.

Fixes over the old downloader:
- one failing row no longer aborts the run or loses the manifest (errors are collected);
- downloads go to a temp file, are validated (status, size, magic bytes) and then renamed;
- an asset keeps its filename when the upstream title changes, so no orphan duplicates;
- partial runs (--media-types / --max-files) merge into the manifest instead of replacing it;
- the manifest is written atomically and records per-file size and sha256.
"""

from __future__ import annotations

import csv
import io
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

from .config import Settings
from .util import FileHasher, atomic_write_json, log, normalize_space, now_iso, read_json

START_URL = "https://www.war.gov/ufo/"
CSV_URL = "https://www.war.gov/Portals/1/Interactive/2026/UFO/uap-data.csv"
DVIDS_ASSET_URL = "https://api.dvidshub.net/asset"
DVIDS_VIDEO_PAGE_URL = "https://www.war.gov/Multimedia/Videos?videoid="
DEFAULT_DVIDS_API_KEY = "key-68bb60d16b35e"
MANIFEST_VERSION = 2
MEDIA_TYPES = {"pdf", "image", "video", "audio"}
EXTENSIONS = {
    "pdf": {".pdf"},
    "image": {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".webp", ".bmp"},
    "video": {".mp4", ".mov", ".m4v", ".webm"},
    "audio": {".mp4", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
}


def normalize_media_type(value: str) -> str:
    clean = value.strip().lower().lstrip(".")
    table = {
        "pdf": "pdf", "document": "pdf", "doc": "pdf",
        "image": "image", "img": "image", "photo": "image", "picture": "image",
        "video": "video", "vid": "video",
        "audio": "audio", "aud": "audio",
    }
    return table.get(clean, clean)


def sanitize_filename(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned.strip("._") or "report"


def asset_key(media_type: str, page_media_url: str, dvids_id: str) -> str:
    if media_type in ("audio", "video"):
        return f"dvids:{dvids_id}"
    return f"url:{page_media_url}"


def manifest_asset_key(entry: Dict[str, object]) -> str:
    return asset_key(str(entry.get("media_type")), str(entry.get("page_media_url") or ""), str(entry.get("dvids_video_id") or ""))


def iter_rows(csv_text: str, wanted: Set[str], issues: List[str]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for line_no, row in enumerate(csv.DictReader(io.StringIO(csv_text)), start=2):
        media_type = normalize_media_type(row.get("Type") or "")
        title = (row.get("Title") or "").strip() or f"row {line_no}"
        if media_type not in MEDIA_TYPES:
            issues.append(f"CSV line {line_no} ({title}): unsupported type {row.get('Type')!r}; skipped")
            continue
        if media_type not in wanted:
            continue
        if media_type in ("audio", "video"):
            ids = [item.strip() for item in (row.get("DVIDS Video ID") or "").split("|") if item.strip()]
            if not ids:
                issues.append(f"CSV line {line_no} ({title}): {media_type} row has no DVIDS id; skipped")
            for video_id in ids:
                key = asset_key(media_type, "", video_id)
                if key in seen:
                    continue
                seen.add(key)
                clone = dict(row)
                clone["DVIDS Video ID"] = video_id
                clone["_type"] = media_type
                rows.append(clone)
            continue
        url = (row.get("PDF | Image Link") or "").strip()
        if not url:
            issues.append(f"CSV line {line_no} ({title}): no file link; skipped")
            continue
        key = asset_key(media_type, url, "")
        if key in seen:
            continue
        seen.add(key)
        row = dict(row)
        row["_type"] = media_type
        rows.append(row)
    return rows


def choose_highest_mp4(files: object) -> Optional[Dict[str, object]]:
    candidates = [
        item for item in (files if isinstance(files, list) else [])
        if isinstance(item, dict) and item.get("type") == "video/mp4" and isinstance(item.get("src"), str)
    ]
    if not candidates:
        return None

    def rank(item: Dict[str, object]):
        return tuple(item.get(k) if isinstance(item.get(k), int) else 0 for k in ("height", "width", "size"))

    return max(candidates, key=rank)


def validate_file(path: Path, media_type: str, expected_size: Optional[int] = None) -> Optional[str]:
    """Return a problem description, or None when the file looks like what it should be."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(64)
            tail = b""
            if media_type == "pdf" and size > 2048:
                handle.seek(-2048, os.SEEK_END)
                tail = handle.read()
    except OSError as exc:
        return f"unreadable: {exc}"
    if size < 512:
        return f"only {size} bytes"
    if head.lstrip()[:1] == b"<" and b"html" in head.lower():
        return "content is an HTML page, not media"
    if media_type == "pdf":
        if not head.startswith(b"%PDF"):
            return "missing %PDF header"
        if tail and b"%%EOF" not in tail:
            return "missing %%EOF trailer (truncated download?)"
    elif media_type in ("video", "audio"):
        if b"ftyp" not in head[:16] and not head.startswith((b"ID3", b"RIFF", b"OggS", b"fLaC", b"\x1aE\xdf\xa3")):
            return "unrecognized audio/video container"
        # DVIDS-reported sizes are approximate, so only flag clearly short files.
        if expected_size and size < expected_size * 0.98:
            return f"{size} bytes, expected about {expected_size}"
    elif media_type == "image":
        signatures = (b"\xff\xd8", b"\x89PNG", b"GIF8", b"II*\x00", b"MM\x00*", b"RIFF", b"BM")
        if not head.startswith(signatures):
            return "unrecognized image format"
    return None


def _download(session, url: str, destination: Path, media_type: str, expected_size: Optional[int]) -> None:
    tmp = destination.with_name(f".{destination.name}.part")
    response = session.get(url, timeout=900, stream=True)
    try:
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        declared = response.headers.get("Content-Length")
        written = 0
        with tmp.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
                    written += len(chunk)
        if declared and declared.isdigit() and not response.headers.get("Content-Encoding") and written != int(declared):
            raise RuntimeError(f"received {written} of {declared} bytes")
        problem = validate_file(tmp, media_type, expected_size)
        if problem:
            raise RuntimeError(problem)
        os.replace(tmp, destination)
    finally:
        response.close()
        if tmp.exists():
            tmp.unlink()


def load_manifest(settings: Settings) -> List[Dict[str, object]]:
    payload = read_json(settings.manifest_path)
    if not isinstance(payload, dict) or not isinstance(payload.get("documents"), list):
        return []
    return [entry for entry in payload["documents"] if isinstance(entry, dict) and entry.get("filename")]


def find_orphans(settings: Settings, manifest: List[Dict[str, object]]) -> List[str]:
    known = {str(entry["filename"]) for entry in manifest}
    if not settings.sources_dir.exists():
        return []
    return sorted(p.name for p in settings.sources_dir.iterdir() if p.is_file() and not p.name.startswith(".") and p.name not in known)


def run(settings: Settings, media_types: Optional[Set[str]] = None, max_files: Optional[int] = None,
        overwrite: bool = False, impersonate: str = "chrome124", prune_orphans: bool = False) -> int:
    try:
        from curl_cffi import requests
    except ImportError:
        raise SystemExit("curl_cffi is required for downloading: pip install curl_cffi")

    wanted = media_types or set(MEDIA_TYPES)
    settings.sources_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session(impersonate=impersonate)
    session.headers.update({"Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Referer": START_URL})
    dvids = requests.Session(impersonate=impersonate)
    dvids.headers.update({"Accept": "application/json, text/plain, */*", "Origin": "https://www.war.gov", "Referer": START_URL})

    landing = session.get(START_URL, timeout=120)
    if landing.status_code != 200:
        raise SystemExit(f"Could not load {START_URL}: HTTP {landing.status_code}")
    match = re.search(r'DVIDS_API_KEY\s*=\s*"([^"]+)"', landing.text) or re.search(r'"apiKey"\s*:\s*"([^"]+)"', landing.text)
    dvids_key = match.group(1) if match else DEFAULT_DVIDS_API_KEY
    response = session.get(CSV_URL, timeout=120)
    if response.status_code != 200:
        raise SystemExit(f"Could not load the source CSV: HTTP {response.status_code}")
    csv_text = response.content.decode("utf-8-sig")
    if "Title" not in csv_text.splitlines()[0]:
        raise SystemExit("The source CSV does not look like the expected manifest (no Title column)")

    issues: List[str] = []
    rows = iter_rows(csv_text, wanted, issues)
    if max_files is not None:
        rows = rows[:max_files]
    if not rows:
        raise SystemExit("No matching media rows in the source CSV")

    previous = load_manifest(settings)
    by_key = {manifest_asset_key(entry): entry for entry in previous}
    used_names = {str(entry["filename"]) for entry in previous}
    hasher = FileHasher(settings.cache_dir / "file_hashes.json")
    merged: Dict[str, Dict[str, object]] = dict(by_key)
    downloaded = failed = 0

    for index, row in enumerate(rows, start=1):
        title = (row.get("Title") or "").strip().strip('"')
        media_type = row["_type"]
        page_url = (row.get("PDF | Image Link") or "").strip()
        video_id = (row.get("DVIDS Video ID") or "").strip()
        key = asset_key(media_type, page_url, video_id)
        old = by_key.get(key)
        try:
            source_url = urljoin(START_URL, page_url) if page_url else ""
            meta: Optional[Dict[str, object]] = None
            selected: Optional[Dict[str, object]] = None
            if media_type in ("audio", "video"):
                reply = dvids.get(DVIDS_ASSET_URL, params={"api_key": dvids_key, "id": f"video:{video_id}", "thumb_width": "720"}, timeout=120)
                if reply.status_code != 200:
                    raise RuntimeError(f"DVIDS metadata HTTP {reply.status_code}")
                payload = reply.json()
                meta = payload.get("results") or payload.get("data") or payload
                if not isinstance(meta, dict):
                    raise RuntimeError("DVIDS metadata was not an object")
                selected = choose_highest_mp4(meta.get("files"))
                if selected is None:
                    raise RuntimeError("no downloadable MP4 in DVIDS metadata")
                source_url = str(selected["src"])
            extension = Path(urlparse(source_url).path).suffix.lower()
            if extension not in EXTENSIONS[media_type]:
                extension = {"pdf": ".pdf", "image": ".jpg", "video": ".mp4", "audio": ".mp4"}[media_type]

            if old:  # keep the established filename even if the title was edited upstream
                filename = str(old["filename"])
            else:
                base = sanitize_filename(title) if title else (Path(urlparse(source_url).path).stem or "report")
                filename, n = f"{base}{extension}", 2
                while filename in used_names:
                    filename, n = f"{base}-{n}{extension}", n + 1
                used_names.add(filename)
            destination = settings.sources_dir / filename
            expected = selected.get("size") if selected and isinstance(selected.get("size"), int) else None

            need = overwrite or not destination.exists()
            if not need:
                problem = validate_file(destination, media_type, expected)
                if problem:
                    log(f"Existing {filename} is invalid ({problem}); re-downloading")
                    need = True
            if need:
                log(f"[{index}/{len(rows)}] downloading {filename}")
                _download(session, source_url, destination, media_type, expected)
                downloaded += 1

            blurb = normalize_space(row.get("Description Blurb"))
            entry: Dict[str, object] = {
                "filename": filename,
                "title": title,
                "media_type": media_type,
                "original_source_url": source_url,
                "source_page_url": START_URL,
                "release_date": normalize_space(row.get("Release Date")),
                "agency": normalize_space(row.get("Agency")),
                "incident_date": normalize_space(row.get("Incident Date")),
                "incident_location": normalize_space(row.get("Incident Location")),
                "description_blurb": blurb,
                "narrative": blurb,
                "video_pairing": normalize_space(row.get("Video Pairing")),
                "pdf_pairing": normalize_space(row.get("PDF Pairing")),
                "page_media_url": page_url,
                "modal_image_url": normalize_space(row.get("Modal Image")),
                "size_bytes": destination.stat().st_size,
                "sha256": hasher.sha256(destination),
                "first_seen": (old or {}).get("first_seen") or now_iso(),
                "last_seen": now_iso(),
                "upstream_status": "listed",
            }
            if meta is not None:
                description = normalize_space(meta.get("description"))
                thumb = meta.get("thumbnail")
                entry.update({
                    "dvids_video_id": video_id,
                    "dvids_video_url": f"{DVIDS_VIDEO_PAGE_URL}{video_id}",
                    "dvids_title": normalize_space(meta.get("title")),
                    "dvids_description": description,
                    "narrative": description or blurb,
                    "duration_seconds": meta.get("duration"),
                    "date_taken": meta.get("date"),
                    "date_published": meta.get("date_published"),
                    "hls_url": meta.get("hls_url"),
                    "thumbnail_url": thumb.get("url") if isinstance(thumb, dict) else None,
                    "closed_caption_urls": meta.get("closed_caption_urls"),
                    "selected_video_file": selected,
                })
            merged[key] = entry
        except Exception as exc:  # one bad row must not lose the rest
            failed += 1
            issues.append(f"{title or key}: download failed: {exc}")
            log(f"WARNING: {title or key}: {exc}")
        if index % 25 == 0:
            _write(settings, merged, issues)
            hasher.save()

    # Entries not in today's CSV: keep them (the file is still a source) but mark them.
    full_run = wanted == MEDIA_TYPES and max_files is None
    if full_run:
        listed = {asset_key(r["_type"], (r.get("PDF | Image Link") or "").strip(), (r.get("DVIDS Video ID") or "").strip()) for r in rows}
        for key, entry in merged.items():
            if key not in listed and entry.get("upstream_status") != "removed":
                entry["upstream_status"] = "removed"
                issues.append(f"{entry['filename']}: no longer listed upstream; kept and marked removed")
    _write(settings, merged, issues)
    hasher.save()

    orphans = find_orphans(settings, list(merged.values()))
    if orphans:
        if prune_orphans:
            target = settings.data_dir / "orphaned"
            target.mkdir(parents=True, exist_ok=True)
            for name in orphans:
                os.replace(settings.sources_dir / name, target / name)
            log(f"Moved {len(orphans)} files not in the manifest to {target}")
        else:
            log(f"{len(orphans)} files in {settings.sources_dir} are not in the manifest and will be ignored "
                f"(run download --prune-orphans to move them aside): {', '.join(orphans[:5])}")
    log(f"Download finished: {len(merged)} manifest entries, {downloaded} downloaded, {failed} failed")
    for issue in issues:
        log(f"  issue: {issue}")
    return 1 if failed else 0


def _write(settings: Settings, merged: Dict[str, Dict[str, object]], issues: List[str]) -> None:
    atomic_write_json(
        settings.manifest_path,
        {"manifest_version": MANIFEST_VERSION, "generated_from": CSV_URL, "generated_at": now_iso(),
         "issues": issues, "documents": list(merged.values())},
        indent=2,
    )
