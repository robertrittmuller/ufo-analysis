"""Place resolution with an explicit precision level.

The old resolver matched country names as substrings ("New Mexico" -> Mexico City,
"New Jersey" -> Jersey) and discarded most model-supplied places. Here the model gives a
structured place (name / region / country, optional coordinates) and the gazetteer is used
to confirm or refine it. Every result says how precise it is: city, region, country or
estimated (model coordinates that could not be confirmed against the gazetteer).
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

US_STATE_CENTROIDS = {
    "AL": (32.8, -86.8), "AK": (64.2, -152.3), "AZ": (34.2, -111.7), "AR": (34.9, -92.4), "CA": (37.2, -119.4),
    "CO": (39.0, -105.5), "CT": (41.6, -72.7), "DE": (39.0, -75.5), "FL": (28.6, -82.4), "GA": (32.7, -83.4),
    "HI": (20.3, -156.4), "ID": (44.4, -114.6), "IL": (40.0, -89.2), "IN": (39.9, -86.3), "IA": (42.1, -93.5),
    "KS": (38.5, -98.4), "KY": (37.5, -85.3), "LA": (31.1, -92.0), "ME": (45.4, -69.2), "MD": (39.0, -76.8),
    "MA": (42.3, -71.8), "MI": (44.3, -85.4), "MN": (46.3, -94.3), "MS": (32.7, -89.7), "MO": (38.4, -92.5),
    "MT": (47.0, -109.6), "NE": (41.5, -99.8), "NV": (39.3, -116.6), "NH": (43.7, -71.6), "NJ": (40.2, -74.7),
    "NM": (34.4, -106.1), "NY": (42.9, -75.5), "NC": (35.6, -79.4), "ND": (47.5, -100.5), "OH": (40.3, -82.8),
    "OK": (35.6, -97.5), "OR": (43.9, -120.6), "PA": (40.9, -77.8), "RI": (41.7, -71.6), "SC": (33.9, -80.9),
    "SD": (44.4, -100.2), "TN": (35.9, -86.4), "TX": (31.5, -99.3), "UT": (39.3, -111.7), "VT": (44.1, -72.7),
    "VA": (37.5, -78.9), "WA": (47.4, -120.5), "WV": (38.6, -80.6), "WI": (44.6, -89.9), "WY": (43.0, -107.5),
    "DC": (38.9, -77.0),
}
COUNTRY_CENTROID_OVERRIDES = {
    "US": (39.8, -98.6), "RU": (61.0, 90.0), "CA": (57.0, -100.0), "AU": (-25.0, 134.0), "CN": (35.0, 103.0),
    "BR": (-10.0, -52.0), "GL": (72.0, -40.0), "AQ": (-80.0, 0.0),
}
COUNTRY_ALIASES = {
    "usa": "US", "u.s.": "US", "u.s.a.": "US", "us": "US", "united states of america": "US", "america": "US",
    "uk": "GB", "u.k.": "GB", "great britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "britain": "GB",
    "ussr": "RU", "u.s.s.r.": "RU", "soviet union": "RU", "russia": "RU", "south korea": "KR", "korea": "KR",
    "north korea": "KP", "iran": "IR", "syria": "SY", "vietnam": "VN", "south vietnam": "VN", "north vietnam": "VN",
    "czechoslovakia": "CZ", "west germany": "DE", "east germany": "DE", "burma": "MM", "persia": "IR",
    "formosa": "TW", "antigua": "AG", "okinawa": "JP", "afghanistan": "AF", "iraq": "IQ", "taiwan": "TW", "the netherlands": "NL", "holland": "NL", "uae": "AE",
}


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().strip(".,;").lower()


def distance_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def valid_coords(lat: Any, lon: Any) -> Optional[Tuple[float, float]]:
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180) or (lat_f == 0 and lon_f == 0):
        return None
    return round(lat_f, 3), round(lon_f, 3)


_DEG = r"(\d{1,3}(?:\.\d+)?)\s*(?:°|º|deg(?:rees?)?\.?)\s*(?:(\d{1,2}(?:\.\d+)?)\s*(?:'|′|’|min(?:utes?)?\.?)?\s*)?(?:(\d{1,2}(?:\.\d+)?)\s*(?:\"|″|”|''|sec(?:onds?)?\.?)?\s*)?"
_COORD_PATTERNS = [
    # 62°00'N 33°00'W, 42° 9.3' N and 114° 22.2' W, 34 deg 15 min N, 118 deg 20 min W
    re.compile(_DEG + r"(N|S|North|South)\b.{0,12}?" + _DEG + r"(E|W|East|West)\b", re.I | re.S),
    # 363453N 0255943E, 3634N 02559E (degrees-minutes[-seconds], no separators)
    re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})?\s?(N|S)[\s,/-]{0,3}(\d{3})(\d{2})(\d{2})?\s?(E|W)\b"),
    # 36.58N 25.99E
    re.compile(r"(?<![\d.])(\d{1,2}\.\d+)()()\s?°?\s?(N|S)[\s,/-]{1,3}(\d{1,3}\.\d+)()()\s?°?\s?(E|W)\b"),
]


def find_coords(text: Any) -> List[Tuple[float, float]]:
    """Coordinates printed in a text with an explicit hemisphere (N/S and E/W), as decimal (lat, lon) pairs."""
    out: List[Tuple[float, float]] = []
    for pattern in _COORD_PATTERNS:
        for m in pattern.finditer(str(text or "")):
            try:
                lat = float(m.group(1)) + float(m.group(2) or 0) / 60 + float(m.group(3) or 0) / 3600
                lon = float(m.group(5)) + float(m.group(6) or 0) / 60 + float(m.group(7) or 0) / 3600
            except ValueError:
                continue
            if float(m.group(2) or 0) >= 60 or float(m.group(6) or 0) >= 60:
                continue
            lat = -lat if m.group(4)[0].upper() == "S" else lat
            lon = -lon if m.group(8)[0].upper() == "W" else lon
            point = valid_coords(lat, lon)
            if point and point not in out:
                out.append(point)
    return out


class Gazetteer:
    def __init__(self) -> None:
        import geonamescache

        cache = geonamescache.GeonamesCache()
        self.countries: Dict[str, Dict[str, Any]] = cache.get_countries()
        self.country_by_name = {_norm(c["name"]): code for code, c in self.countries.items()}
        self.country_by_name.update(COUNTRY_ALIASES)
        self.iso3 = {c["iso3"]: code for code, c in self.countries.items() if c.get("iso3") and c["iso3"] not in ("USA",)}
        self.state_by_name = {_norm(s["name"]): code for code, s in cache.get_us_states().items()}
        self.state_by_name["washington, d.c."] = "DC"
        self.state_by_name["washington dc"] = "DC"
        self.state_codes = set(US_STATE_CENTROIDS)
        self.cities: Dict[str, List[Dict[str, Any]]] = {}
        sums: Dict[str, List[float]] = {}
        for city in cache.get_cities().values():
            self.cities.setdefault(_norm(city["name"]), []).append(city)
            weight = max(1, city.get("population") or 1)
            row = sums.setdefault(city["countrycode"], [0.0, 0.0, 0.0])
            row[0] += city["latitude"] * weight
            row[1] += city["longitude"] * weight
            row[2] += weight
        self.country_centroids = {code: (round(r[0] / r[2], 2), round(r[1] / r[2], 2)) for code, r in sums.items()}
        self.country_centroids.update(COUNTRY_CENTROID_OVERRIDES)

    def country_code(self, value: Any) -> str:
        text = _norm(value)
        if not text:
            return ""
        if text.upper() in self.countries and len(text) == 2:
            return text.upper()
        return self.country_by_name.get(text, "")

    def state_code(self, value: Any) -> str:
        text = _norm(value)
        if not text:
            return ""
        if text.upper() in self.state_codes and len(text) == 2:
            return text.upper()
        return self.state_by_name.get(text, "")

    def resolve(self, name: Any, region: Any = "", country: Any = "", lat: Any = None, lon: Any = None) -> Dict[str, Any]:
        """Return {label, lat, lon, precision, country, region} with precision in
        city | region | country | estimated | none."""
        name = re.sub(r"\([^)]*\)", " ", str(name or ""))  # drop parentheticals such as "(in orbit)"
        name = re.sub(r"^\s*(the\s+)?(general\s+)?(area|vicinity|region|coast|outskirts)\s+of\s+", "", name, flags=re.I)
        name = re.sub(r"\s+", " ", name).strip(" ,;")
        parts = [p.strip() for p in re.split(r"[,;/]", name) if p.strip()]
        label = ", ".join(x for x in [str(name or "").strip(), str(region or "").strip(), str(country or "").strip()] if x)
        # de-duplicate repeated tokens in the label ("Texas, Texas")
        seen: List[str] = []
        for token in [t.strip() for t in label.split(",") if t.strip()]:
            if token.lower() not in [s.lower() for s in seen]:
                seen.append(token)
        label = ", ".join(seen)

        full = " ".join([name, str(region or "")]).lower()
        if re.search(r"washington,?\s*d\.?\s*c\b|district of columbia", full) or (name.lower() == "washington" and not str(region or "").strip()):
            # In these records a bare "Washington" is the capital, not the state.
            return {"label": label, "lat": 38.895, "lon": -77.036, "precision": "city", "country": "US", "region": "DC"}
        country_code = self.country_code(country)
        state = self.state_code(region)
        for part in parts[1:] + parts[:1]:  # trailing parts are usually the state / country
            if not state and self.state_code(part):
                state = self.state_code(part)
            elif not country_code and self.country_code(part) and len(parts) > 1:
                country_code = self.country_code(part)
        if not country_code and not state and len(parts) == 1:
            country_code = self.country_code(parts[0])
        if not state and not country_code:
            state, country_code = self._scan(" ".join([name, str(region or ""), str(country or "")]))
        if state and not country_code:
            country_code = "US"
        model = valid_coords(lat, lon)
        result = {"label": label, "lat": None, "lon": None, "precision": "none", "country": country_code, "region": state}
        if not label and not model:
            return result

        # 1. city match, constrained by state/country or confirmed by the model's coordinates
        best = None
        for part in parts:
            key = _norm(re.sub(r"\b(near|vicinity of|area of|outside|north of|south of|east of|west of|over)\b", "", part, flags=re.I))
            for city in self.cities.get(key, []):
                if country_code and city["countrycode"] != country_code:
                    continue
                if state and city["countrycode"] == "US" and city.get("admin1code") != state:
                    continue
                here = (city["latitude"], city["longitude"])
                constrained = bool(country_code)
                confirmed = model is not None and distance_km(model, here) < 300
                if not (constrained or confirmed or (city.get("population") or 0) > 1_000_000):
                    continue
                if key in self.state_by_name and not state and not confirmed:
                    continue  # "New York", "Washington": a state name alone is not a city match
                score = (2 if confirmed else 0, city.get("population") or 0)
                if best is None or score > best[0]:
                    best = (score, city)
        if best:
            city = best[1]
            result.update({"lat": round(city["latitude"], 3), "lon": round(city["longitude"], 3), "precision": "city",
                           "country": city["countrycode"]})
            return result

        # 2. model coordinates, sanity-checked against the stated state / country
        if model:
            anchor = US_STATE_CENTROIDS.get(state) if state else self.country_centroids.get(country_code)
            limit = 700 if state else 4500
            if anchor is None or distance_km(model, anchor) < limit:
                result.update({"lat": model[0], "lon": model[1], "precision": "estimated"})
                return result

        # 3. region or country centroid
        if state:
            lat_s, lon_s = US_STATE_CENTROIDS[state]
            result.update({"lat": lat_s, "lon": lon_s, "precision": "region"})
        elif country_code and country_code in self.country_centroids:
            lat_c, lon_c = self.country_centroids[country_code]
            result.update({"lat": lat_c, "lon": lon_c, "precision": "country"})
        return result

    def _scan(self, text: str) -> Tuple[str, str]:
        """Find a U.S. state or a country named anywhere in free text, on word boundaries.

        States are checked first and removed, so "New Mexico" never matches Mexico and
        "New Jersey" never matches Jersey.
        """
        low = " " + _norm(text) + " "
        # Organization names are not places ("United States Central Command", "U.S. Air Force").
        low = re.sub(r"(united states|u\.s\.|us)\s+(central|european|indo-pacific|pacific|africa|northern|southern|space|strategic|special operations)?\s*"
                     r"(command|air force|navy|army|marine corps|coast guard|government|military|forces?)", " ", low)
        for token in re.findall(r"\b[A-Z]{3}\b", str(text)):
            if token in self.iso3:
                return "", self.iso3[token]
        for state_name in sorted(self.state_by_name, key=len, reverse=True):
            if re.search(r"(?<![a-z])" + re.escape(state_name) + r"(?![a-z])", low):
                if state_name == "washington" and "washington state" not in low:
                    continue  # ambiguous with Washington, D.C.
                return self.state_by_name[state_name], "US"
        for country_name in sorted(self.country_by_name, key=len, reverse=True):
            if len(country_name) < 4:
                continue  # skip "us", "uk" style tokens inside prose
            if re.search(r"(?<![a-z])" + re.escape(country_name) + r"(?![a-z])", low):
                return "", self.country_by_name[country_name]
        return "", ""

    def country_name(self, code: str) -> str:
        return self.countries.get(code, {}).get("name", "") if code else ""
