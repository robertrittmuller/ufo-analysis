"""Command line entry point: python -m ufo_pipeline <stage> [options]."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List

from . import config, llm
from .util import atomic_write_json, log, now_iso

STAGES = ["download", "extract", "analyze", "embed", "build"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ufo_pipeline",
        description="Download, extract, analyze and publish the PURSUE UAP release corpus. "
        "Every stage is resumable: finished work is cached and only missing or failed items are redone.",
    )
    parser.add_argument("stage", choices=STAGES + ["all", "process", "check"],
                        help="'all' runs every stage; 'process' runs extract+analyze+embed+build; 'check' only runs the preflight")
    parser.add_argument("--only", action="append", default=[], metavar="TEXT",
                        help="Only process sources whose filename contains TEXT (repeatable)")
    parser.add_argument("--limit", type=int, help="Process at most N sources (scratch runs)")
    parser.add_argument("--page-limit", type=int, help="Process at most N pages per PDF (scratch runs)")
    parser.add_argument("--force", action="store_true", help="Ignore caches for the selected stage(s) and sources")
    parser.add_argument("--data-dir", type=Path, help="Data directory (default: data/)")
    parser.add_argument("--site-dir", type=Path, help="Site output directory (default: dashboard/)")
    parser.add_argument("--env-file", type=Path, help="Alternate .env file")
    parser.add_argument("--media-types", default="pdf,image,video,audio", help="download: comma-separated media types")
    parser.add_argument("--overwrite", action="store_true", help="download: re-download existing files")
    parser.add_argument("--prune-orphans", action="store_true", help="download: move files not in the manifest to data/orphaned")
    parser.add_argument("--no-corpus-summary", action="store_true", help="build: skip the model-written corpus synthesis")
    return parser


def main(argv: List[str] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = config.load_settings(args.env_file)
    if args.data_dir:
        settings.data_dir = args.data_dir.resolve()
    if args.site_dir:
        settings.site_dir = args.site_dir.resolve()

    stages = {"all": STAGES, "process": STAGES[1:], "check": []}.get(args.stage, [args.stage])
    partial = bool(args.only or args.limit or args.page_limit)
    if partial and "build" in stages and settings.site_dir == config.REPO_ROOT / "dashboard":
        raise SystemExit("Refusing to publish a partial corpus into dashboard/. Pass --site-dir for scratch builds.")

    status = config.preflight(settings, stages or STAGES)
    log("Preflight: " + json.dumps({"tools": status["tools"], "endpoints": status["endpoints"]}))
    for warning in settings.warnings:
        log(f"WARNING: {warning}")
    if args.stage == "check":
        return 0

    report = {"started": now_iso(), "stages": {}, "preflight": status}
    code = 0
    for stage in stages:
        log(f"===== {stage} =====")
        if stage == "download":
            from . import download

            wanted = {download.normalize_media_type(t) for t in args.media_types.split(",") if t.strip()}
            code = max(code, download.run(settings, wanted, args.limit, args.overwrite, prune_orphans=args.prune_orphans))
        elif stage == "extract":
            from . import extract

            report["stages"]["extract"] = extract.run(settings, args.only, args.limit, args.page_limit, args.force)
        elif stage == "analyze":
            from . import analyze

            report["stages"]["analyze"] = analyze.run(settings, args.only, args.limit, args.force)
        elif stage == "embed":
            from . import embed

            report["stages"]["embed"] = embed.run(settings, args.force)
        elif stage == "build":
            from . import site

            report["stages"]["build"] = site.run(settings, corpus_summary=not args.no_corpus_summary, partial=partial)
    report["finished"] = now_iso()
    report["usage"] = llm.USAGE.data
    atomic_write_json(settings.run_log_path, report, indent=1)
    log(f"Model usage: {json.dumps(llm.USAGE.data)}")
    log(f"Run report written to {settings.run_log_path}")
    return code
