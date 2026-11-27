"""SQL export of the published site data for the MCP server's Cloudflare D1 database.

Reads only what `build` wrote into the site folder (index.json, reports/<id>.json, connections.json), so the
MCP server answers from exactly what the dashboard shows: labels after the consistency rules, model names
replaced by model types, unmatched quotes hidden.

The output is one .sql file to load with `wrangler d1 execute <db> --file=...`. It drops and recreates every
data table and leaves the `usage` table alone. D1 rules the file
follows: no BEGIN/COMMIT, and no statement over 100,000 bytes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .util import log, read_json

SCHEMA_VERSION = 1
MAX_STATEMENT_BYTES = 90_000   # D1 rejects statements over 100,000 bytes
BATCH_BYTES = 40_000           # rows are grouped into multi-row INSERTs of about this size
ENTITY_KINDS = {"people": "person", "organizations": "organization", "places": "place",
                "aircraft_equipment": "equipment", "programs": "program", "file_numbers": "file-number"}

SCHEMA = """
DROP TABLE IF EXISTS pages_fts;
DROP TABLE IF EXISTS incidents_fts;
DROP TABLE IF EXISTS reports_fts;
DROP TABLE IF EXISTS entities_fts;
DROP TABLE IF EXISTS meta;
DROP TABLE IF EXISTS reports;
DROP TABLE IF EXISTS incidents;
DROP TABLE IF EXISTS entities;
DROP TABLE IF EXISTS records;
DROP TABLE IF EXISTS passages;
DROP TABLE IF EXISTS pages;
DROP TABLE IF EXISTS links;
DROP TABLE IF EXISTS hubs;
DROP TABLE IF EXISTS clusters;

CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0, rows_read INTEGER NOT NULL DEFAULT 0);

CREATE TABLE meta (key TEXT PRIMARY KEY, json TEXT NOT NULL);

CREATE TABLE reports (
  n INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, title TEXT, text TEXT,
  agency TEXT, media TEXT, kind TEXT, release TEXT, year INTEGER, disposition TEXT,
  pages INTEGER, incidents INTEGER, value INTEGER, themes TEXT,
  page_lo INTEGER, page_hi INTEGER, card TEXT NOT NULL, detail TEXT NOT NULL);

CREATE TABLE incidents (
  n INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, report TEXT NOT NULL, event TEXT, repeat INTEGER, cluster TEXT,
  title TEXT, place TEXT, text TEXT,
  date TEXT, year INTEGER, country TEXT, region TEXT, lat REAL, lon REAL,
  shape TEXT, status TEXT, hynek TEXT, explanation TEXT, band TEXT, score INTEGER,
  agency TEXT, media TEXT, modes TEXT, card TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX incidents_report ON incidents(report);
CREATE INDEX incidents_event ON incidents(event);
CREATE INDEX incidents_cluster ON incidents(cluster);
CREATE INDEX incidents_year ON incidents(year);
CREATE INDEX incidents_country ON incidents(country, region);
CREATE INDEX incidents_shape ON incidents(shape);
CREATE INDEX incidents_status ON incidents(status);
CREATE INDEX incidents_explanation ON incidents(explanation);
CREATE INDEX incidents_score ON incidents(repeat, score DESC, n);

CREATE TABLE entities (n INTEGER PRIMARY KEY, report TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, role TEXT, org TEXT);
CREATE INDEX entities_report ON entities(report, kind);

CREATE TABLE records (report TEXT NOT NULL, idx INTEGER NOT NULL, json TEXT NOT NULL, PRIMARY KEY (report, idx));
CREATE TABLE passages (report TEXT NOT NULL, idx INTEGER NOT NULL, json TEXT NOT NULL, PRIMARY KEY (report, idx));

CREATE TABLE pages (n INTEGER PRIMARY KEY, report TEXT NOT NULL, page INTEGER NOT NULL, label TEXT, method TEXT, flags TEXT, text TEXT NOT NULL);
CREATE UNIQUE INDEX pages_report ON pages(report, page);

CREATE TABLE links (a TEXT NOT NULL, b TEXT NOT NULL, factors TEXT, why TEXT);
CREATE INDEX links_a ON links(a);
CREATE INDEX links_b ON links(b);
CREATE TABLE hubs (id TEXT PRIMARY KEY, kind TEXT, label TEXT);
CREATE TABLE clusters (id TEXT PRIMARY KEY, size INTEGER, factors TEXT);
"""

# External-content full-text indexes: the text lives once, in the base table, and each index is rebuilt after loading.
FTS = """
CREATE VIRTUAL TABLE pages_fts USING fts5(text, content='pages', content_rowid='n', tokenize='porter unicode61');
INSERT INTO pages_fts(pages_fts) VALUES('rebuild');
CREATE VIRTUAL TABLE incidents_fts USING fts5(title, place, text, content='incidents', content_rowid='n', tokenize='porter unicode61');
INSERT INTO incidents_fts(incidents_fts) VALUES('rebuild');
CREATE VIRTUAL TABLE reports_fts USING fts5(title, text, content='reports', content_rowid='n', tokenize='porter unicode61');
INSERT INTO reports_fts(reports_fts) VALUES('rebuild');
CREATE VIRTUAL TABLE entities_fts USING fts5(name, content='entities', content_rowid='n', tokenize='unicode61');
INSERT INTO entities_fts(entities_fts) VALUES('rebuild');
"""

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sql(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + _CONTROL.sub(" ", str(value)).replace("'", "''") + "'"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _prune(value: Any) -> Any:
    """Drop empty values so answers carry no dead weight (None, "", [], {}); False and 0 are kept."""
    if isinstance(value, dict):
        out = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def _tags(values: Iterable[str]) -> str:
    """'|a|b|' so one tag can be matched with LIKE '%|a|%'."""
    values = [str(v) for v in values if v]
    return "|" + "|".join(values) + "|" if values else ""


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


class _Writer:
    """Groups rows into multi-row INSERT statements that stay under D1's statement size limit."""

    def __init__(self, handle) -> None:
        self.handle = handle
        self.counts: Dict[str, int] = {}
        self._table = ""
        self._head = ""
        self._rows: List[str] = []
        self._size = 0

    def raw(self, sql: str) -> None:
        self.flush()
        self.handle.write(sql.strip() + "\n")

    def flush(self) -> None:
        if self._rows:
            self.handle.write(self._head + ",\n".join(self._rows) + ";\n")
        self._rows, self._size = [], 0

    def row(self, table: str, columns: Sequence[str], values: Sequence[Any]) -> None:
        if table != self._table:
            self.flush()
            self._table, self._head = table, f"INSERT INTO {table} ({','.join(columns)}) VALUES\n"
        text = "(" + ",".join(_sql(v) for v in values) + ")"
        size = len(text.encode("utf-8"))
        if size + len(self._head) > MAX_STATEMENT_BYTES:
            raise ValueError(f"{table} row of {size} bytes exceeds the D1 statement limit")
        if self._size + size > BATCH_BYTES:
            self.flush()
        self._rows.append(text)
        self._size += size
        self.counts[table] = self.counts.get(table, 0) + 1


def _report_card(row: Dict[str, Any]) -> Dict[str, Any]:
    return _prune({
        "id": row["id"], "title": row["title"], "agency": row["agency"], "media": row["media"], "kind": row["kind"],
        "document_type": _clip(row.get("doc_type"), 160), "release": row["release"], "year": row["year"],
        "disposition": row["disposition"], "pages": row["pages"], "duration_seconds": row.get("duration"),
        "incidents": row["incidents"], "research_value": row["value"], "redaction": row["redaction"],
        "themes": row["themes"], "analysis_status": row["status"] if row["status"] != "complete" else None,
        "summary": row["snippet"],
    })


def _report_detail(report: Dict[str, Any]) -> Dict[str, Any]:
    manifest = report.get("manifest") or {}
    extraction = report.get("extraction") or {}
    quality = report.get("quality") or {}
    return _prune({
        "filename": report.get("filename"), "analysis_status": report.get("status"),
        "synthesis": report.get("synthesis") or {},
        "publisher": {k: manifest.get(k) for k in ("title", "release_date", "incident_date", "incident_location", "description_blurb",
                                                    "original_source_url", "source_page_url", "dvids_video_url", "size_bytes", "sha256")},
        "extraction": {k: extraction.get(k) for k in ("page_count", "pages_extracted", "duration", "methods", "failed_pages", "flagged_pages",
                                                      "redaction_marks", "pages_with_redactions", "handwritten_pages", "legibility", "markings")},
        "quote_checks": {k: quality.get(k) for k in ("quotes_total", "quotes_verified", "quotes_approximate")},
        "analysis_gaps": {"parts_not_analyzed": [c.get("pages") for c in quality.get("chunks_failed") or []],
                          "synthesis_failed": bool(quality.get("synthesis_error"))},
        "frame_review": report.get("frame_review"),
        "related": [{"id": r["id"], "title": r["title"], "why": r.get("why")} for r in report.get("related") or []],
    })


def _incident_card(row: Dict[str, Any]) -> Dict[str, Any]:
    """The index row under names that keep the record's own conclusion apart from the machine assessment."""
    return _prune({
        "id": row["id"], "report": row["report"], "title": row["title"],
        "date": row["date"], "date_precision": row["date_precision"] if row["date_precision"] != "day" else None,
        "place": row["place"], "country": row["country"], "region": row["region"],
        "lat": row["lat"], "lon": row["lon"], "position_basis": row["place_precision"] if row["lat"] is not None else None,
        "shape": row["shape"], "observed_by": row["modes"], "witnesses": row["witnesses"],
        "status_in_record": row["status"], "hynek": row["hynek"],
        "machine_explanation": row["explanation"], "machine_likelihood": row["likelihood"],
        "documentation_score": row["score"], "documentation_band": row["band"],
        "pages": row["pages"], "accounts_of_event": row["accounts"] if row["accounts"] > 1 else None,
        "event": row["event"] if row["event"] != row["id"] else None,
        "summary": row["snippet"],
    })


def _incident_detail(incident: Dict[str, Any]) -> Dict[str, Any]:
    detail = {k: v for k, v in incident.items() if k not in ("status_as_extracted", "same_event", "event")}
    location = dict(detail.get("location") or {})
    location.pop("raw", None)
    detail["location"] = location
    if "assessment" in detail:  # named for what it is, so it is never read as the record's conclusion
        detail["machine_assessment"] = detail.pop("assessment")
    return _prune(detail)


def write_d1(site_dir: Path, out_path: Path) -> Dict[str, int]:
    data_dir = site_dir / "data"
    index = read_json(data_dir / "index.json")
    if not index:
        raise SystemExit(f"No site data at {data_dir}. Run the build stage first.")
    graph = read_json(data_dir / "connections.json") or {"nodes": [], "links": [], "clusters": [], "factors": {}}
    nodes = graph.get("nodes") or []
    cluster_of = {n["id"]: n.get("cluster") for n in nodes if n.get("kind") == "incident"}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        w = _Writer(handle)
        w.raw(f"-- Declassified UFO Files: D1 data for the MCP server. Generated {index.get('generated')}; do not edit.")
        w.raw(SCHEMA)

        quality = index.get("quality") or {}
        overview = _prune({
            "generated": index.get("generated"), "partial": index.get("partial") or None, "source": index.get("source"),
            "stats": index.get("stats"), "releases": index.get("releases"), "summary": {k: v for k, v in (index.get("summary") or {}).items() if k != "provider"},
            "data_quality": {k: quality.get(k) for k in ("quotes_total", "quotes_verified", "quotes_approximate", "reports_complete", "reports_incomplete",
                                                        "pending_sources", "located_incidents", "dated_incidents", "repeat_accounts", "positions", "corrections")},
            "connection_factors": graph.get("factors"), "connections": graph.get("stats"),
        })
        w.row("meta", ("key", "json"), ("overview", _json(overview)))
        w.row("meta", ("key", "json"), ("schema", _json({"version": SCHEMA_VERSION, "generated": index.get("generated")})))

        incident_rows = {row["id"]: row for row in index.get("incidents") or []}
        page_n = incident_n = entity_n = 0
        for report_n, card in enumerate(index.get("reports") or [], start=1):
            report = read_json(data_dir / "reports" / f"{card['id']}.json")
            if not report:
                raise SystemExit(f"Missing report file for {card['id']}; rebuild the site before exporting.")
            rid = card["id"]

            page_lo = page_n + 1
            for unit in report.get("pages") or []:
                text = unit.get("text") or ""
                if not text.strip():
                    continue
                if len(text.encode("utf-8")) > 80_000:  # far beyond any real page; keeps the statement limit safe
                    log(f"WARNING: {rid} {unit.get('label')}: page text cut to fit the D1 statement limit")
                    text = text.encode("utf-8")[:80_000].decode("utf-8", "ignore")
                page_n += 1
                w.row("pages", ("n", "report", "page", "label", "method", "flags", "text"),
                      (page_n, rid, unit["n"], unit.get("label"), unit.get("method"), ",".join(unit.get("flags") or []), text))
            page_hi = page_n

            synthesis = report.get("synthesis") or {}
            search_text = " ".join(filter(None, [synthesis.get("summary"), card.get("doc_type"), card.get("pub_title"), " ".join(card.get("themes") or [])]))
            w.row("reports", ("n", "id", "title", "text", "agency", "media", "kind", "release", "year", "disposition", "pages", "incidents",
                              "value", "themes", "page_lo", "page_hi", "card", "detail"),
                  (report_n, rid, card["title"], search_text, card["agency"], card["media"], card["kind"], card["release"], card["year"],
                   card["disposition"], card["pages"], card["incidents"], card["value"], _tags(card.get("themes") or []),
                   page_lo if page_hi >= page_lo else None, page_hi if page_hi >= page_lo else None,
                   _json(_report_card(card)), _json(_report_detail(report))))

            for incident in report.get("incidents") or []:
                row = incident_rows.get(incident["id"])
                if not row:
                    continue
                incident_n += 1
                w.row("incidents", ("n", "id", "report", "event", "repeat", "cluster", "title", "place", "text", "date", "year", "country", "region",
                                    "lat", "lon", "shape", "status", "hynek", "explanation", "band", "score", "agency", "media", "modes", "card", "detail"),
                      (incident_n, row["id"], rid, row["event"], row["repeat"], cluster_of.get(row["id"]), row["title"], row["place"],
                       incident.get("narrative") or row["snippet"], row["date"] or None, row["year"], row["country"] or None, row["region"] or None,
                       row["lat"], row["lon"], row["shape"], row["status"], row["hynek"], row["explanation"], row["band"], row["score"],
                       row["agency"], row["media"], _tags(row["modes"]), _json(_incident_card(row)), _json(_incident_detail(incident))))

            for kind, label in ENTITY_KINDS.items():
                seen = set()
                for item in (report.get("entities") or {}).get(kind) or []:
                    name = (item.get("name") if isinstance(item, dict) else item) or ""
                    name = " ".join(str(name).split())
                    if not name or name.lower() in seen or name.upper().strip("[]") == "REDACTED":
                        continue
                    seen.add(name.lower())
                    entity_n += 1
                    role, org = (item.get("role"), item.get("org")) if isinstance(item, dict) else (None, None)
                    w.row("entities", ("n", "report", "kind", "name", "role", "org"), (entity_n, rid, label, name[:300], _clip(role, 300) or None, _clip(org, 200) or None))

            for idx, record in enumerate(report.get("records") or []):
                w.row("records", ("report", "idx", "json"), (rid, idx, _json(_prune(record))))
            for idx, passage in enumerate(report.get("key_passages") or []):
                w.row("passages", ("report", "idx", "json"), (rid, idx, _json(_prune(passage))))

        for node in nodes:
            if node.get("kind") != "incident":
                w.row("hubs", ("id", "kind", "label"), (node["id"], node.get("kind"), node.get("label")))
        for link in graph.get("links") or []:
            w.row("links", ("a", "b", "factors", "why"), (nodes[link["s"]]["id"], nodes[link["t"]]["id"], ",".join(link.get("f") or []), _json(link.get("why") or [])))
        for cluster in graph.get("clusters") or []:
            w.row("clusters", ("id", "size", "factors"), (cluster["id"], len(cluster.get("members") or []), _json(cluster.get("factors") or {})))

        w.raw(FTS)
        counts = dict(w.counts)
    tmp.replace(out_path)
    counts["bytes"] = out_path.stat().st_size
    log(f"D1 export written to {out_path}: {counts}")
    return counts


def default_path(settings, default_site: Path) -> Path:
    """data/mcp/d1.sql for the real site; a sibling of the site folder for scratch builds, so a subset never replaces it."""
    if settings.site_dir == default_site:
        return settings.data_dir / "mcp" / "d1.sql"
    return settings.site_dir.with_name(settings.site_dir.name + ".d1.sql")


def run(settings, default_site: Path, out_path: Optional[Path] = None) -> Dict[str, int]:
    return write_d1(settings.site_dir, out_path or default_path(settings, default_site))
