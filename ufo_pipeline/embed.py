"""Stage 4: local embeddings -> related reports and cross-file "same event" links.

Embeddings stay on the local model. Vectors are cached by content hash and model, so only
new or changed reports are embedded. Unlike the old pipeline, incidents are embedded too,
which lets the same event be linked across different files.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Tuple

from . import llm
from .config import Settings
from .geo import distance_km
from .util import atomic_write_json, log, now_iso, read_json, stable_hash

BATCH = 16
RELATED_MIN = 0.55
RELATED_TOP = 6


def _encode(vector: List[float]) -> str:
    import numpy as np

    return base64.b64encode(np.asarray(vector, dtype="float32").tobytes()).decode("ascii")


def _decode(text: str):
    import numpy as np

    return np.frombuffer(base64.b64decode(text), dtype="float32")


def load_reports(settings: Settings) -> List[Dict[str, Any]]:
    reports = []
    for path in sorted(settings.analysis_dir.glob("*.json")):
        report = read_json(path)
        if isinstance(report, dict) and report.get("id") and report.get("synthesis") is not None:
            reports.append(report)
    return reports


def report_text(report: Dict[str, Any]) -> str:
    synthesis = report.get("synthesis") or {}
    manifest = report.get("manifest") or {}
    parts = [synthesis.get("title") or manifest.get("title") or "", synthesis.get("document_type") or "",
             synthesis.get("summary") or manifest.get("description_blurb") or ""]
    parts += [k["text"] for k in synthesis.get("key_points") or []]
    return "\n".join(p for p in parts if p)[:6000]


def incident_text(incident: Dict[str, Any]) -> str:
    obj = incident["object"]
    return "\n".join(x for x in (
        incident["title"], incident["date"]["iso"] or incident["date"]["text"], incident["location"]["label"],
        incident["narrative"], " ".join(v for v in (obj.get("shape_text"), obj.get("motion")) if v)) if x)[:3000]


def run(settings: Settings, force: bool = False) -> Dict[str, Any]:
    import numpy as np

    reports = load_reports(settings)
    relations_path = settings.cache_dir / "relations.json"
    endpoint = settings.local_embedding
    if not endpoint or not endpoint.configured:
        log("Embedding model unavailable: keeping any existing relations, skipping this stage")
        return {"status": "skipped", "reason": "embedding model unavailable"}

    cache = read_json(settings.embeddings_path) or {}
    if force or cache.get("model") != endpoint.model:
        cache = {"model": endpoint.model, "vectors": {}}
    vectors: Dict[str, str] = cache.setdefault("vectors", {})

    items: List[Tuple[str, str, str]] = []  # (item id, cache key, text)
    for report in reports:
        text = report_text(report)
        if text.strip():
            items.append((report["id"], stable_hash(text, 24), text))
        for incident in report.get("incidents") or []:
            text = incident_text(incident)
            items.append((incident["id"], stable_hash(text, 24), text))
    missing = [(key, text) for _id, key, text in items if key not in vectors]
    missing = list(dict(missing).items())
    failed = 0
    for start in range(0, len(missing), BATCH):
        batch = missing[start:start + BATCH]
        try:
            for (key, _text), vector in zip(batch, llm.embed(endpoint, [text for _key, text in batch])):
                vectors[key] = _encode(vector)
        except llm.LLMError as exc:
            failed += len(batch)
            log(f"WARNING: embedding batch failed: {str(exc)[:160]}")
        if start % (BATCH * 20) == 0 and start:
            atomic_write_json(settings.embeddings_path, cache)
            log(f"  embedded {start}/{len(missing)}")
    live = {key for _id, key, _text in items}
    cache["vectors"] = {k: v for k, v in vectors.items() if k in live}  # prune vectors nobody uses
    atomic_write_json(settings.embeddings_path, cache)

    def matrix(ids_keys: List[Tuple[str, str]]):
        usable = [(i, k) for i, k in ids_keys if k in cache["vectors"]]
        if not usable:
            return [], None
        m = np.vstack([_decode(cache["vectors"][k]) for _i, k in usable]).astype("float32")
        m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
        return [i for i, _k in usable], m

    key_of = {item_id: key for item_id, key, _text in items}
    by_id = {r["id"]: r for r in reports}

    # --- related reports
    report_ids, rm = matrix([(r["id"], key_of[r["id"]]) for r in reports if r["id"] in key_of])
    related: Dict[str, List[Dict[str, Any]]] = {}
    if rm is not None and len(report_ids) > 1:
        sims = rm @ rm.T
        np.fill_diagonal(sims, -1)
        for row, rid in enumerate(report_ids):
            order = np.argsort(-sims[row])[:RELATED_TOP]
            links = []
            for col in order:
                score = float(sims[row, col])
                if score < RELATED_MIN:
                    break
                other = by_id[report_ids[col]]
                links.append({"id": other["id"], "score": round(score, 3), "why": _shared(by_id[rid], other)})
            related[rid] = links

    # --- same event across different files
    incidents = [(inc, r["id"]) for r in reports for inc in r.get("incidents") or []]
    inc_ids, im = matrix([(inc["id"], key_of[inc["id"]]) for inc, _rid in incidents])
    same_event: Dict[str, List[str]] = {}
    if im is not None and len(inc_ids) > 1:
        info = {inc["id"]: (inc, rid) for inc, rid in incidents}
        parent = {i: i for i in inc_ids}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        sims = im @ im.T
        rows, cols = np.where(np.triu(sims, 1) >= 0.72)
        for a, b in zip(rows.tolist(), cols.tolist()):
            ia, ib = inc_ids[a], inc_ids[b]
            (inc_a, doc_a), (inc_b, doc_b) = info[ia], info[ib]
            if doc_a == doc_b:
                continue
            score = float(sims[a, b])
            da, db = inc_a["date"], inc_b["date"]
            same_day = da["precision"] == "day" and da["iso"] == db["iso"]
            same_year = da["year"] and da["year"] == db["year"]
            la, lb = inc_a["location"], inc_b["location"]
            near = la["lat"] is not None and lb["lat"] is not None and distance_km((la["lat"], la["lon"]), (lb["lat"], lb["lon"])) < 200
            conflict = da["precision"] == "day" and db["precision"] == "day" and da["iso"] != db["iso"]
            if conflict:
                continue
            if (same_day and (near or score >= 0.78)) or (same_year and near and score >= 0.85) or score >= 0.93:
                parent[find(ia)] = find(ib)
        groups: Dict[str, List[str]] = {}
        for i in inc_ids:
            groups.setdefault(find(i), []).append(i)
        for members in groups.values():
            if len(members) > 1:
                for member in members:
                    same_event[member] = [m for m in members if m != member]

    atomic_write_json(relations_path, {"generated": now_iso(), "model": endpoint.model, "related_reports": related, "same_event": same_event})
    stats = {"status": "ok", "items": len(items), "embedded_now": len(missing) - failed, "failed": failed,
             "reports_with_related": sum(1 for v in related.values() if v), "incidents_linked": len(same_event)}
    log(f"Embed finished: {stats}")
    return stats


def _shared(a: Dict[str, Any], b: Dict[str, Any]) -> List[str]:
    why: List[str] = []
    ea, eb = a.get("entities") or {}, b.get("entities") or {}
    people = {p["name"].lower() for p in ea.get("people") or []} & {p["name"].lower() for p in eb.get("people") or []}
    if people:
        why.append("shared people: " + ", ".join(sorted(p.title() for p in people)[:3]))
    for key, label in (("file_numbers", "shared file numbers"), ("programs", "shared programs"), ("places", "shared places")):
        common = {x.lower() for x in ea.get(key) or []} & {x.lower() for x in eb.get(key) or []}
        if common:
            why.append(f"{label}: " + ", ".join(sorted(common)[:3]))
    ya = {i["date"]["year"] for i in a.get("incidents") or [] if i["date"]["year"]}
    yb = {i["date"]["year"] for i in b.get("incidents") or [] if i["date"]["year"]}
    if ya & yb:
        why.append("incidents in " + ", ".join(str(y) for y in sorted(ya & yb)[:3]))
    if not why:
        why.append("similar content")
    return why[:3]
