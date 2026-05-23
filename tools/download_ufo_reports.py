#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    from curl_cffi import requests
except ImportError:  # pragma: no cover - depends on local environment
    requests = None


START_URL = "https://www.war.gov/ufo/"
CSV_URL = "https://www.war.gov/Portals/1/Interactive/2026/UFO/uap-data.csv"
DVIDS_ASSET_URL = "https://api.dvidshub.net/asset"
DVIDS_VIDEO_PAGE_URL = "https://www.war.gov/Multimedia/Videos?videoid="
DEFAULT_DVIDS_API_KEY = "key-68bb60d16b35e"
SOURCE_MANIFEST_VERSION = 1
SUPPORTED_MEDIA_TYPES = {"pdf", "image", "video", "audio"}


def parse_media_types(value: str) -> set[str]:
    requested = {item.strip().lower() for item in value.split(",") if item.strip()}
    aliases = {"img": "image", "vid": "video", "aud": "audio"}
    normalized = {aliases.get(item, item) for item in requested}
    invalid = normalized - SUPPORTED_MEDIA_TYPES
    if invalid:
        allowed = ", ".join(sorted(SUPPORTED_MEDIA_TYPES | set(aliases)))
        raise argparse.ArgumentTypeError(
            f"Unsupported media type(s): {', '.join(sorted(invalid))}. Use one or more of: {allowed}."
        )
    if not normalized:
        raise argparse.ArgumentTypeError("At least one media type is required.")
    return normalized


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download UFO release media from war.gov into data/sources.",
    )
    repo_root = Path(__file__).resolve().parents[1]
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=repo_root / "data" / "sources",
        help="Directory where media files should be saved.",
    )
    parser.add_argument(
        "--source-manifest",
        type=Path,
        default=repo_root / "data" / "processed" / "source_manifest.json",
        help="JSON manifest file that stores source metadata and narrative for each downloaded file.",
    )
    parser.add_argument(
        "--media-types",
        type=parse_media_types,
        default=parse_media_types("pdf,image,video,audio"),
        help="Comma-separated media types to download. Defaults to pdf,image,video,audio.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="Optional cap on the number of files to download.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=True,
        help="Do not re-download files that already exist at the destination path. This is the default.",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_false",
        dest="skip_existing",
        help="Re-download files even when the destination path already exists.",
    )
    parser.add_argument(
        "--impersonate",
        default="chrome124",
        help="curl_cffi browser profile to impersonate. Defaults to chrome124.",
    )
    return parser.parse_args()


def require_requests() -> None:
    if requests is not None:
        return
    raise RuntimeError(
        "curl_cffi is required. Install it with 'pip install curl_cffi'."
    )


def sanitize_filename(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in name)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("._") or "report"


def filename_stem_from_url(url: str) -> str:
    path = urlparse(url).path
    return Path(path).stem or "report"


def extension_from_url(url: str, fallback: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix:
        return suffix
    return fallback if fallback.startswith(".") else f".{fallback}"


def build_filename(title: str, source_url: str, extension: str, used_names: set[str]) -> str:
    fallback = filename_stem_from_url(source_url)
    base_name = sanitize_filename(title) if title else fallback
    candidate = f"{base_name}{extension}"

    index = 2
    while candidate in used_names:
        candidate = f"{base_name}-{index}{extension}"
        index += 1

    used_names.add(candidate)
    return candidate


def create_session(impersonate: str):
    session = requests.Session(impersonate=impersonate)
    session.headers.update(
        {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": START_URL,
        }
    )
    return session


def create_dvids_session(impersonate: str):
    session = requests.Session(impersonate=impersonate)
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.war.gov",
            "Referer": START_URL,
        }
    )
    return session


def ensure_success(response, url: str) -> None:
    if response.status_code >= 400:
        raise RuntimeError(f"Request failed for {url}: HTTP {response.status_code}")


def fetch_landing_html(session) -> str:
    landing = session.get(START_URL, timeout=120)
    ensure_success(landing, START_URL)
    return landing.text


def fetch_csv_text(session) -> str:
    response = session.get(CSV_URL, timeout=120)
    ensure_success(response, CSV_URL)
    return response.content.decode("utf-8-sig")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_dvids_api_key(html: str) -> str:
    match = re.search(r'DVIDS_API_KEY\s*=\s*"([^"]+)"', html)
    if match:
        return match.group(1)
    match = re.search(r'"apiKey"\s*:\s*"([^"]+)"', html)
    if match:
        return match.group(1)
    return DEFAULT_DVIDS_API_KEY


def extract_download_link(html: str, label_pattern: str) -> str | None:
    pattern = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
    label_regex = re.compile(label_pattern, re.IGNORECASE)
    for href, label_html in pattern.findall(html):
        label = normalize_space(re.sub(r"<[^>]+>", " ", label_html))
        if label_regex.search(label):
            return urljoin(START_URL, href)
    return None


def normalize_media_type(value: str) -> str:
    clean = value.strip().lower().lstrip(".")
    if clean.startswith("p"):
        return "pdf"
    if clean.startswith("i"):
        return "image"
    if clean.startswith("v"):
        return "video"
    if clean.startswith("a"):
        return "audio"
    return clean


def split_pipe_list(value: str) -> list[str]:
    return [item.strip() for item in value.split("|") if item.strip()]


def iter_media_records(csv_text: str, media_types: set[str]) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    records: list[dict[str, str]] = []
    seen_assets: set[tuple[str, str]] = set()
    for row in reader:
        media_type = normalize_media_type(row.get("Type") or "")
        if media_type not in media_types:
            continue
        if media_type not in SUPPORTED_MEDIA_TYPES:
            continue

        source_url = (row.get("PDF | Image Link") or "").strip()
        if media_type in {"audio", "video"}:
            video_ids = split_pipe_list(row.get("DVIDS Video ID") or "")
            if not video_ids:
                continue
            for video_id in video_ids:
                key = (media_type, video_id)
                if key in seen_assets:
                    continue
                seen_assets.add(key)
                video_row = dict(row)
                video_row["DVIDS Video ID"] = video_id
                records.append(video_row)
            continue

        if not source_url:
            continue
        key = (media_type, source_url)
        if key in seen_assets:
            continue
        seen_assets.add(key)
        records.append(row)
    return records


def choose_highest_mp4(files: object) -> dict[str, object] | None:
    if not isinstance(files, list):
        return None

    candidates: list[dict[str, object]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "video/mp4" or not isinstance(item.get("src"), str):
            continue
        candidates.append(item)

    if not candidates:
        return None

    def sort_key(item: dict[str, object]) -> tuple[int, int, int]:
        height = item.get("height")
        width = item.get("width")
        size = item.get("size")
        return (
            height if isinstance(height, int) else 0,
            width if isinstance(width, int) else 0,
            size if isinstance(size, int) else 0,
        )

    return sorted(candidates, key=sort_key, reverse=True)[0]


def fetch_dvids_video_metadata(session, video_id: str, api_key: str) -> dict[str, object]:
    response = session.get(
        DVIDS_ASSET_URL,
        params={"api_key": api_key, "id": f"video:{video_id}", "thumb_width": "720"},
        timeout=120,
    )
    ensure_success(response, f"{DVIDS_ASSET_URL}?id=video:{video_id}")
    payload = response.json()
    data = payload.get("results") or payload.get("data") or payload
    if not isinstance(data, dict):
        raise RuntimeError(f"DVIDS metadata for video {video_id} was not an object")
    return data


def download_file(session, source_url: str, destination: Path) -> None:
    response = session.get(source_url, timeout=600, stream=True)
    try:
        ensure_success(response, source_url)
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    finally:
        response.close()


def write_source_manifest(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "manifest_version": SOURCE_MANIFEST_VERSION,
        "generated_from": CSV_URL,
        "documents": records,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    args = parse_args()

    try:
        require_requests()
        args.output_dir.mkdir(parents=True, exist_ok=True)

        session = create_session(args.impersonate)
        dvids_session = create_dvids_session(args.impersonate)
        landing_html = fetch_landing_html(session)
        dvids_api_key = extract_dvids_api_key(landing_html)
        video_bundle_url = extract_download_link(landing_html, r"\[\.VID\]|\[\.vid\]")
        csv_text = fetch_csv_text(session)
        records = iter_media_records(csv_text, args.media_types)
        if args.max_files is not None:
            records = records[: args.max_files]

        if not records:
            print("No matching media records found.", file=sys.stderr)
            return 1

        used_names: set[str] = set()
        manifest_records: list[dict[str, object]] = []
        for record in records:
            title = (record.get("Title") or "").strip().strip('"')
            media_type = normalize_media_type(record.get("Type") or "")
            source_url = (record.get("PDF | Image Link") or "").strip()
            page_description = normalize_space(record.get("Description Blurb") or "")
            video_metadata: dict[str, object] | None = None
            selected_video_file: dict[str, object] | None = None

            if media_type in {"audio", "video"}:
                video_id = (record.get("DVIDS Video ID") or "").strip()
                video_metadata = fetch_dvids_video_metadata(dvids_session, video_id, dvids_api_key)
                selected_video_file = choose_highest_mp4(video_metadata.get("files"))
                if selected_video_file is None:
                    raise RuntimeError(f"No downloadable MP4 found for DVIDS video {video_id}")
                source_url = str(selected_video_file["src"])
                extension = extension_from_url(source_url, ".mp4")
            else:
                fallback_extension = ".pdf" if media_type == "pdf" else ".img"
                extension = extension_from_url(source_url, fallback_extension)

            filename = build_filename(title, source_url, extension, used_names)
            destination = args.output_dir / filename
            if args.skip_existing and destination.exists():
                print(f"Skipping existing {destination}")
            else:
                print(f"Downloading {source_url} -> {destination}")
                download_file(session, source_url, destination)

            manifest_record = {
                "filename": filename,
                "title": title,
                "media_type": media_type,
                "original_source_url": source_url,
                "source_page_url": START_URL,
                "release_date": normalize_space(record.get("Release Date") or ""),
                "agency": normalize_space(record.get("Agency") or ""),
                "incident_date": normalize_space(record.get("Incident Date") or ""),
                "incident_location": normalize_space(record.get("Incident Location") or ""),
                "description_blurb": page_description,
                "narrative": page_description,
                "video_pairing": normalize_space(record.get("Video Pairing") or ""),
                "pdf_pairing": normalize_space(record.get("PDF Pairing") or ""),
                "page_media_url": normalize_space(record.get("PDF | Image Link") or ""),
                "modal_image_url": normalize_space(record.get("Modal Image") or ""),
            }

            if media_type in {"audio", "video"} and video_metadata is not None:
                video_id = normalize_space(record.get("DVIDS Video ID") or "")
                video_description = normalize_space(str(video_metadata.get("description") or ""))
                manifest_record.update(
                    {
                        "dvids_video_id": video_id,
                        "dvids_video_url": f"{DVIDS_VIDEO_PAGE_URL}{video_id}",
                        "dvids_title": normalize_space(str(video_metadata.get("title") or "")),
                        "dvids_description": video_description,
                        "narrative": video_description or page_description,
                        "duration_seconds": video_metadata.get("duration"),
                        "date_taken": video_metadata.get("date"),
                        "date_published": video_metadata.get("date_published"),
                        "hls_url": video_metadata.get("hls_url"),
                        "thumbnail_url": (
                            video_metadata.get("thumbnail", {}).get("url")
                            if isinstance(video_metadata.get("thumbnail"), dict)
                            else None
                        ),
                        "closed_caption_urls": video_metadata.get("closed_caption_urls"),
                        "selected_video_file": selected_video_file,
                        "video_bundle_url": video_bundle_url,
                    }
                )

            manifest_records.append(manifest_record)

        write_source_manifest(args.source_manifest, manifest_records)

        print(f"Downloaded {len(records)} media files into {args.output_dir}")
        return 0
    except Exception as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
