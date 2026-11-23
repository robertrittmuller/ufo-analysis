"""Connections between incidents through explicit, explainable factors, with a 3D layout for the site.

Every link carries the factor(s) that made it and a plain reason ("same day, 12 km apart"). Factors:

- time_place   both dated to the day, at most 1 day apart and at most 150 km apart (city-level or better positions)
- description  same specific shape and a shared colour, at most 7 days and 800 km apart
- same_event   the embed stage's text match, kept only when the dates and places also agree
- person       a named person appears in both accounts (hub node: the person)
- mission      the same spacecraft mission or named vessel (hub node: the mission)

People and missions are hub nodes rather than pairwise links, so a name in 30 accounts adds 30 edges, not 435.
A cluster is a connected group under the incident-to-incident factors only (time_place, description,
same_event); hubs connect clusters but never merge them.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .geo import distance_km

FACTORS = {
    "time_place": {"label": "Same time and place", "detail": "Dated to the day, at most 1 day and 150 km apart"},
    "description": {"label": "Matching description", "detail": "Same specific shape and colour, within 7 days and 800 km"},
    "same_event": {"label": "Same account in another file", "detail": "Text match whose dates and places also agree"},
    "person": {"label": "Same named person", "detail": "The same person is named in both accounts"},
    "mission": {"label": "Same mission or vessel", "detail": "Observed from the same spacecraft mission or named ship"},
}
PAIR_FACTORS = ("time_place", "description", "same_event")

GENERIC_SHAPES = {"not-described", "other", "irregular", "multiple-shapes", "light-point", ""}
COLOURS = ["white", "orange", "red", "green", "blue", "silver", "aluminum", "metallic", "black", "yellow", "gray", "grey"]
PRECISE = {"city", "estimated"}
RANKS = re.compile(r"^(mr|mrs|ms|miss|dr|prof|capt|captain|lt|lieutenant|col|colonel|maj|major|gen|general|sgt|sergeant|cpl|corporal|"
                   r"pvt|private|adm|admiral|cdr|commander|ens|ensign|mg|bg|ltc|lt\.? ?col|sen|senator|rep|gov|governor|rev|sa|agent|"
                   r"officer|patrolman|deputy|sheriff|chief|airman|a/?1c|a/?2c|t/?sgt|s/?sgt|m/?sgt|1st|2nd|first|second|mrs\.)\.?$", re.I)
# people named only as officials, press or commentators are context, not a shared witness or investigator
CONTEXT_ROLES = re.compile(r"\b(director|secretary|president|senator|congressm|representative|editor|columnist|reporter|journalist|"
                           r"commentator|author|writer|publisher|broadcaster|attorney general|governor)\b", re.I)
MISSION = re.compile(r"\b(gemini|apollo|mercury|skylab)[ -]?(\d+|[ivx]+)\b", re.I)
VESSEL = re.compile(r"\buss\s+([a-z][a-z.' -]{2,30}?)(?=[,;()]|\s+\(|\s+cv|\s+dd|\s*$)", re.I)
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}


# Camera and system default timestamps (e.g. 12/31/99) are not dates; matching on them chains unrelated media.
PLACEHOLDER_DATES = {"1999-12-31", "2000-01-01", "1970-01-01", "1980-01-01", "1900-01-01"}
UNRELIABLE_DATE = re.compile(r"\b(incorrect|inaccurate|erroneous|wrong|default|placeholder|not (?:the )?(?:actual|correct))\b", re.I)


def _reliable_date(inc: Dict[str, Any]) -> Dict[str, Any]:
    """The incident's date, or {} when it is a placeholder or the record says it is wrong."""
    d = inc.get("date") or {}
    if d.get("iso") in PLACEHOLDER_DATES or UNRELIABLE_DATE.search(str(d.get("text") or "")):
        return {}
    return d


def _day(inc: Dict[str, Any]) -> Optional[_dt.date]:
    d = _reliable_date(inc)
    if d.get("precision") != "day":
        return None
    try:
        return _dt.date.fromisoformat(d.get("iso") or "")
    except ValueError:
        return None


def _point(inc: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    loc = inc.get("location") or {}
    return (loc["lat"], loc["lon"]) if loc.get("lat") is not None and loc.get("precision") in PRECISE else None


def _colours(inc: Dict[str, Any]) -> set:
    text = str((inc.get("object") or {}).get("color") or "").lower()
    found = {c for c in COLOURS if c in text}
    return {"silver" if c in ("aluminum", "metallic") else "gray" if c == "grey" else c for c in found}


def _person_key(name: str) -> Optional[str]:
    name = re.sub(r"[\"'“”‘’][^\"'“”‘’]{1,15}[\"'“”‘’]", " ", name)  # nicknames: Charles 'Pete' Conrad
    words = [w for w in re.split(r"[\s,]+", name.strip()) if w]
    while words and RANKS.match(words[0]):
        words.pop(0)
    words = [w for w in words if not re.fullmatch(r"[A-Za-z]\.?|jr\.?|sr\.?|ii|iii", w, re.I)]
    if len(words) < 2 or len(words[-1]) < 3 or re.search(r"redact|person|unknown|unnamed|\[|\d", name, re.I):
        return None
    return f"{words[0]} {words[-1]}".lower()


def _missions(inc: Dict[str, Any]) -> List[Tuple[str, str]]:
    text = str((inc.get("observation") or {}).get("platform") or "")
    out = []
    for m in MISSION.finditer(text):
        num = m.group(2).lower()
        num = str(ROMAN.get(num, num))
        out.append((f"{m.group(1).lower()} {num}", f"{m.group(1).title()} {num}"))
    for m in VESSEL.finditer(text):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" .-").title()
        out.append((f"uss {name.lower()}", f"USS {name}"))
    return out


def same_event_agrees(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Keep a text match only when the dates and places agree (never on similarity alone)."""
    da, db = _reliable_date(a), _reliable_date(b)
    if da.get("precision") == "day" and db.get("precision") == "day":
        return da.get("iso") == db.get("iso")
    pa, pb = _point(a), _point(b)
    return bool(da.get("year")) and da.get("year") == db.get("year") and pa is not None and pb is not None and distance_km(pa, pb) <= 200


def _layout(n: int, edges: List[Tuple[int, int, float]], seed: int, gravity: float = 0.01) -> np.ndarray:
    """Force-directed 3D layout (Fruchterman-Reingold style) for one connected component."""
    rng = np.random.default_rng(seed)
    if n == 1:
        return np.zeros((1, 3), dtype=np.float32)
    k = 9.0
    pos = (rng.standard_normal((n, 3)) * k * n ** (1 / 3)).astype(np.float32)
    if not edges:
        return pos
    s = np.array([e[0] for e in edges]); t = np.array([e[1] for e in edges]); w = np.array([e[2] for e in edges], dtype=np.float32)
    iters = 220 if n > 300 else 160
    temp = k * n ** (1 / 3)
    for it in range(iters):
        delta = pos[:, None, :] - pos[None, :, :]
        d2 = (delta ** 2).sum(-1) + 0.01
        # repulsion only between near neighbours, so a big group stays compact instead of inflating
        disp = (delta * (k * k / d2 * (d2 < (4 * k) ** 2))[..., None]).sum(1)
        d = pos[t] - pos[s]
        dist = np.sqrt((d ** 2).sum(-1)) + 0.01
        pull = d * (dist * w / k)[:, None]
        np.add.at(disp, s, pull)
        np.add.at(disp, t, -pull)
        disp -= pos * gravity * k * k / 81  # gravity keeps loose branches near the group
        length = np.sqrt((disp ** 2).sum(-1)) + 1e-6
        step = temp * (1 - it / iters) + 0.3
        pos += disp * (np.minimum(length, step) / length)[:, None]
    return pos - pos.mean(0)


def _pack(radii: np.ndarray) -> np.ndarray:
    """Pack component bounding spheres into a compact ball, largest at the centre (simple overlap relaxation)."""
    n = len(radii)
    if n == 1:
        return np.zeros((1, 3), dtype=np.float32)
    i = np.arange(n) + 0.5
    phi, theta = np.arccos(1 - 2 * i / n), np.pi * (1 + 5 ** 0.5) * i
    shell = np.cumsum(radii) ** (1 / 3) * radii.max() ** (2 / 3) * 0.9  # bigger components start nearer the middle
    pos = np.stack([np.cos(theta) * np.sin(phi), np.sin(theta) * np.sin(phi), np.cos(phi)], 1) * shell[:, None]
    pos[0] = 0
    for it in range(400):
        delta = pos[:, None, :] - pos[None, :, :]
        dist = np.sqrt((delta ** 2).sum(-1)) + np.eye(n) * 1e6 + 1e-3
        overlap = np.clip(radii[:, None] + radii[None, :] + 4 - dist, 0, None)
        push = (delta / dist[..., None] * overlap[..., None] * 0.5).sum(1)
        pos += push - pos * 0.02 * (1 - it / 400)  # gravity fades so the last steps only resolve overlaps
        pos[0] = 0
    return pos.astype(np.float32)


def build(reports: List[Dict[str, Any]], same_event: Dict[str, List[str]]) -> Dict[str, Any]:
    incidents: List[Dict[str, Any]] = []
    for report in reports:
        for inc in report.get("incidents") or []:
            incidents.append(dict(inc, _report=report["id"], _people=(report.get("entities") or {}).get("people") or []))
    index = {inc["id"]: n for n, inc in enumerate(incidents)}
    pairs: Dict[Tuple[int, int], Dict[str, str]] = defaultdict(dict)

    def link(a: int, b: int, factor: str, why: str) -> None:
        if a != b:
            pairs[(min(a, b), max(a, b))].setdefault(factor, why)

    # time_place and description: compare accounts at most 7 days apart
    by_day: Dict[_dt.date, List[int]] = defaultdict(list)
    for n, inc in enumerate(incidents):
        day = _day(inc)
        if day and _point(inc):
            by_day[day].append(n)
    for day, here in by_day.items():
        for gap in range(0, 8):
            there = by_day.get(day + _dt.timedelta(days=gap))
            if not there:
                continue
            for a in here:
                ia, pa = incidents[a], _point(incidents[a])
                for b in there:
                    if gap == 0 and b <= a:
                        continue
                    ib = incidents[b]
                    km = distance_km(pa, _point(ib))
                    when = "same day" if gap == 0 else f"{gap} day{'s' if gap > 1 else ''} apart"
                    if gap <= 1 and km <= 150:
                        link(a, b, "time_place", f"{when}, {km:.0f} km apart")
                    shape = (ia.get("object") or {}).get("shape") or ""
                    common = _colours(ia) & _colours(ib)
                    if shape not in GENERIC_SHAPES and shape == (ib.get("object") or {}).get("shape") and common and km <= 800:
                        link(a, b, "description", f"both {shape.replace('-', ' ')}, {', '.join(sorted(common))}; {when}, {km:.0f} km apart")

    for member, others in same_event.items():
        a = index.get(member)
        for other in others:
            b = index.get(other)
            if a is None or b is None or incidents[a]["_report"] == incidents[b]["_report"]:
                continue
            if same_event_agrees(incidents[a], incidents[b]):
                link(a, b, "same_event", "the same account in another file; dates and places agree")

    # hubs: named people and missions
    variants: Dict[str, set] = defaultdict(set)
    shown: Dict[str, Counter] = defaultdict(Counter)
    for inc in incidents:
        for person in inc["_people"]:
            name = str(person.get("name") or "")
            key = _person_key(name)
            if key and not CONTEXT_ROLES.search(str(person.get("role") or "")):
                variants[key].add(re.sub(r"\s+", " ", name).strip().lower())
                shown[key][name.strip()] += 1
    texts = [(str(inc.get("narrative") or "") + " " + " ".join(str(w.get("description") or "") for w in inc.get("witnesses") or [])).lower()
             for inc in incidents]
    hub_members: Dict[Tuple[str, str], Dict[int, str]] = defaultdict(dict)
    hub_label: Dict[Tuple[str, str], str] = {}
    for key, forms in variants.items():
        forms = {f for f in forms | {key} if len(f) >= 7}
        for n, text in enumerate(texts):
            hit = next((f for f in forms if f in text and re.search(r"\b" + re.escape(f) + r"\b", text)), None)
            if hit:
                hub_members[("person", key)][n] = f"names {shown[key].most_common(1)[0][0]}"
        hub_label[("person", key)] = shown[key].most_common(1)[0][0]
    for n, inc in enumerate(incidents):
        for key, label in _missions(inc):
            hub_members[("mission", key)][n] = f"observed from {label}"
            hub_label[("mission", key)] = label
    hubs = {h: m for h, m in hub_members.items() if len({incidents[n]["_report"] for n in m}) >= 2}

    # nodes: every incident with at least one link, then hubs
    linked = sorted({n for pair in pairs for n in pair} | {n for m in hubs.values() for n in m})
    node_of = {n: k for k, n in enumerate(linked)}
    nodes: List[Dict[str, Any]] = [{"id": incidents[n]["id"], "kind": "incident"} for n in linked]
    links: List[Dict[str, Any]] = [{"s": node_of[a], "t": node_of[b], "f": sorted(why), "why": [why[f] for f in sorted(why)]}
                                   for (a, b), why in pairs.items()]
    for (kind, key), members in sorted(hubs.items(), key=lambda kv: (kv[0][0], -len(kv[1]), kv[0][1])):
        h = len(nodes)
        nodes.append({"id": f"{kind}:{key}", "kind": kind, "label": hub_label[(kind, key)]})
        links += [{"s": node_of[n], "t": h, "f": [kind], "why": [why]} for n, why in members.items()]

    # clusters: groups joined by incident-to-incident factors
    parent = list(range(len(nodes)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for l in links:
        if any(f in PAIR_FACTORS for f in l["f"]):
            parent[find(l["s"])] = find(l["t"])
    groups: Dict[int, List[int]] = defaultdict(list)
    for k, node in enumerate(nodes):
        if node["kind"] == "incident":
            groups[find(k)].append(k)
    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        member_set = set(members)
        factor_counts = Counter(f for l in links if l["s"] in member_set and l["t"] in member_set for f in l["f"])
        clusters.append({"members": sorted(members), "factors": dict(factor_counts)})
    clusters.sort(key=lambda c: -len(c["members"]))
    for n, c in enumerate(clusters):
        c["id"] = f"c{n + 1}"
        for k in c["members"]:
            nodes[k]["cluster"] = c["id"]

    # layout: each connected component (all factors) on its own, then arranged around the largest
    parent = list(range(len(nodes)))
    for l in links:
        parent[find(l["s"])] = find(l["t"])
    comps: Dict[int, List[int]] = defaultdict(list)
    for k in range(len(nodes)):
        comps[find(k)].append(k)
    ordered = sorted(comps.values(), key=len, reverse=True)
    # incident-to-incident factors pull hard so related events form tight knots; hub spokes are looser
    edges_of: Dict[int, List[Tuple[int, int, float]]] = defaultdict(list)
    for l in links:
        pair = any(f in PAIR_FACTORS for f in l["f"])
        edges_of[find(l["s"])].append((l["s"], l["t"], (1.4 if pair else 0.6) + 0.4 * (len(l["f"]) - 1)))
    pos = np.zeros((len(nodes), 3), dtype=np.float32)
    shapes, radii = [], []
    for c, members in enumerate(ordered):
        local = {g: i for i, g in enumerate(members)}
        p = _layout(len(members), [(local[s], local[t], w) for s, t, w in edges_of[find(members[0])]], seed=c + 1)
        shapes.append(p)
        radii.append(max(float(np.sqrt((p ** 2).sum(-1)).max()) if len(members) > 1 else 0.0, 6.0) + 6.0)
        for k in members:
            nodes[k]["g"] = c  # component index, largest first; the site filters by component size
    centres = _pack(np.array(radii, dtype=np.float32))
    for c, members in enumerate(ordered):
        pos[members] = shapes[c] + centres[c]
    for k, node in enumerate(nodes):
        node["p"] = [round(float(v), 1) for v in pos[k]]

    return {"factors": FACTORS, "nodes": nodes, "links": links, "clusters": clusters,
            "stats": {"incidents": sum(1 for n in nodes if n["kind"] == "incident"), "hubs": sum(1 for n in nodes if n["kind"] != "incident"),
                      "links": len(links), "clusters": len(clusters), "by_factor": dict(Counter(f for l in links for f in l["f"]))}}
