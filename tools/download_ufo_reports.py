#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

try:
    from curl_cffi import requests
except ImportError:  # pragma: no cover - depends on local environment
    requests = None


START_URL = "https://www.war.gov/ufo/"
CSV_URL = "https://www.war.gov/Portals/1/Interactive/2026/UFO/uap-csv.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download UFO PDF reports from war.gov into data/sources.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "sources",
        help="Directory where the PDFs should be saved.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="Optional cap on the number of files to download.",
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


def build_filename(title: str, pdf_url: str, used_names: set[str]) -> str:
    fallback = Path(pdf_url).stem or "report"
    base_name = sanitize_filename(title) if title else fallback
    candidate = f"{base_name}.pdf"

    index = 2
    while candidate in used_names:
        candidate = f"{base_name}-{index}.pdf"
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


def ensure_success(response, url: str) -> None:
    if response.status_code >= 400:
        raise RuntimeError(f"Request failed for {url}: HTTP {response.status_code}")


def fetch_csv_text(session) -> str:
    landing = session.get(START_URL, timeout=120)
    ensure_success(landing, START_URL)

    response = session.get(CSV_URL, timeout=120)
    ensure_success(response, CSV_URL)
    return response.content.decode("utf-8-sig")


def iter_pdf_records(csv_text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    records: list[dict[str, str]] = []
    for row in reader:
        file_type = (row.get("Type") or "").strip().lower()
        pdf_url = (row.get("PDF | Image Link") or "").strip()
        if file_type != "pdf" or not pdf_url.lower().endswith(".pdf"):
            continue
        records.append(row)
    return records


def download_pdf(session, pdf_url: str, destination: Path) -> None:
    response = session.get(pdf_url, timeout=600, stream=True)
    try:
        ensure_success(response, pdf_url)
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    finally:
        response.close()


def main() -> int:
    args = parse_args()

    try:
        require_requests()
        args.output_dir.mkdir(parents=True, exist_ok=True)

        session = create_session(args.impersonate)
        csv_text = fetch_csv_text(session)
        records = iter_pdf_records(csv_text)
        if args.max_files is not None:
            records = records[: args.max_files]

        if not records:
            print("No PDF records found.", file=sys.stderr)
            return 1

        used_names: set[str] = set()
        for record in records:
            title = (record.get("Title") or "").strip().strip('"')
            pdf_url = (record.get("PDF | Image Link") or "").strip()
            filename = build_filename(title, pdf_url, used_names)
            destination = args.output_dir / filename
            print(f"Downloading {pdf_url} -> {destination}")
            download_pdf(session, pdf_url, destination)

        print(f"Downloaded {len(records)} PDF files into {args.output_dir}")
        return 0
    except Exception as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())