#!/usr/bin/env python3
"""
Phase 2 pipeline — graph-based long-beach / name-similarity clustering.

Catches beach duplicates and multi-section beaches the 150 m DBSCAN missed
by combining FIVE independent signals:

  1. Name similarity   – normalised Greek/English names (SequenceMatcher)
  2. Proximity chains  – adjacent coastal points ≤ DIST_HIGH metres
  3. OSM Overpass      – which named OSM beach polygon covers each point?
                         Two points under the same polygon → definite same beach
  4. Satellite image   – Google Maps Static API with coloured markers at each point
  5. OSM map tile      – standard rendering shows tan beach patches + name labels
                         between points (exactly what you see on the "Mapbox view")

All signals are summarised in a single Gemini Vision prompt that receives both
images and a structured text block with distance, name-similarity, and Overpass
findings.

Usage
-----
  python scripts/beach_phase2_pipeline.py           # run on points not in phase 1 clusters
  python scripts/beach_phase2_pipeline.py --all     # include phase 1 cluster points too
  python scripts/beach_phase2_pipeline.py --rescore # re-score cached results, 0 API calls
  python scripts/beach_phase2_pipeline.py --dry-run # print clusters, no API calls or writes
  python scripts/beach_phase2_pipeline.py --reset   # delete all phase 2 cache and restart

Resumable
---------
  Each cluster result is cached in data/cluster_results_p2/{id}.json.
  On re-run only uncached clusters are sent to the API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests

try:
    from google import genai
    from google.genai import types as genai_types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False


# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT         = Path(__file__).parent.parent
DATA         = ROOT / "data_new"
GEOJSON      = DATA / "current.json"
PROPOSED     = DATA / "proposed_changes.json"
CACHE_DIR    = DATA / "cluster_results_p2"
TILE_CACHE   = DATA / "tile_cache"        # shared with phase 1 so the tile API works
OSM_CACHE    = DATA / "tile_cache_p2"     # OSM tiles (separate dir)
RATE_FILE    = DATA / "rate_state_p2.json"

# ── Tunables ──────────────────────────────────────────────────────────────────
GEMINI_MODEL     = "gemini-2.5-flash"
RPM_LIMIT        = 13          # stay safely under free-tier 15 RPM
RPD_LIMIT        = 2_000
AUTO_APPROVE     = 0.91        # confidence ≥ this → auto-approve 2-point SINGLE_BEACH

# Graph edge thresholds
NAME_HIGH, DIST_HIGH = 0.82, 800    # strong name match → link up to 800 m
NAME_MED,  DIST_MED  = 0.68, 350    # medium name match → link up to 350 m
NAME_LOW,  DIST_LOW  = 0.52, 120    # weak match        → link up to 120 m

MAX_CLUSTER_SPAN = 4_000   # skip clusters wider than 4 km — probably over-linked

# Overpass API
OVERPASS_URL    = "https://overpass-api.de/api/interpreter"
OVERPASS_RADIUS = 400       # metres — search radius around cluster centroid
OVERPASS_DELAY  = 1.5       # seconds between Overpass requests

# Google Maps Static API + Places API (same key)
MAPS_API_KEY    = os.environ.get("MAPS_API_KEY", "")
SAT_SIZE        = "640x400"
SAT_SCALE       = 2
PLACES_RADIUS   = 150       # metres — how close a Google Places result must be to count

# OSM tile (no key required)
OSM_UA = "BeachDedup-Phase2/1.0 (research project)"


# ── Greek / English beach stop-words for name normalisation ───────────────────
_STOPWORDS = {
    "παραλια", "παραλία", "παραλιά", "beach", "ακτη", "ακτή",
    "κολπος", "κόλπος", "ορμος", "όρμος", "οχθη", "οχθή", "λιμνοθαλασσα",
    "παρ", "ακτ", "bay", "cove", "plage", "playa", "spiaggia", "strand",
    "βορεια", "βόρεια", "νοτια", "νότια", "ανατολικη", "ανατολική",
    "δυτικη", "δυτική", "μικρη", "μικρή", "μεγαλη", "μεγάλη",
    "μικρος", "μεγαλος", "north", "south", "east", "west", "central", "main",
    "a", "b", "c", "i", "ii", "iii", "iv", "v",
}


def _norm(name: str) -> str:
    """Lowercase, strip diacritics, remove stop-words and lone digits."""
    s = unicodedata.normalize("NFD", (name or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = [t for t in s.split() if t not in _STOPWORDS and not re.fullmatch(r"\d+", t)]
    return " ".join(tokens).strip()


def _sim(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _hav(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Rate limiter (same sliding-window logic as phase 1) ───────────────────────

class RateLimiter:
    def __init__(self):
        self._rpm_window: list[float] = []
        self._day_start = math.floor(time.time() / 86400) * 86400
        self._day_count = 0
        self._load()

    def _load(self):
        if RATE_FILE.exists():
            try:
                s = json.loads(RATE_FILE.read_text())
                self._rpm_window = s.get("rpm_window", [])
                self._day_start  = s.get("day_start", self._day_start)
                self._day_count  = s.get("day_count", 0)
                today = math.floor(time.time() / 86400) * 86400
                if self._day_start != today:
                    self._day_start, self._day_count = today, 0
            except Exception:
                pass

    def _save(self):
        RATE_FILE.write_text(json.dumps({
            "rpm_window": self._rpm_window,
            "day_start":  self._day_start,
            "day_count":  self._day_count,
        }))

    def wait(self):
        now   = time.time()
        today = math.floor(now / 86400) * 86400
        if today != self._day_start:
            self._day_start, self._day_count = today, 0

        if self._day_count >= RPD_LIMIT:
            secs = self._day_start + 86400 - now
            print(f"  [rate] Daily cap reached — sleeping {secs:.0f} s")
            time.sleep(secs + 5)
            self._day_start = math.floor(time.time() / 86400) * 86400
            self._day_count = 0

        self._rpm_window = [t for t in self._rpm_window if now - t < 60]
        if len(self._rpm_window) >= RPM_LIMIT:
            wait = 60 - (now - self._rpm_window[0]) + 0.5
            print(f"  [rate] RPM limit — sleeping {wait:.1f} s")
            time.sleep(wait)
            self._rpm_window = [t for t in self._rpm_window if time.time() - t < 60]

        self._rpm_window.append(time.time())
        self._day_count += 1
        self._save()


# ── Union-Find for connected components ───────────────────────────────────────

def _components(edges: list[tuple[str, str]], nodes: list[str]) -> list[list[str]]:
    parent: dict[str, str] = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for a, b in edges:
        union(a, b)

    groups: dict[str, list[str]] = {}
    for n in nodes:
        groups.setdefault(find(n), []).append(n)
    return [g for g in groups.values() if len(g) > 1]


# ── Stable cluster ID ─────────────────────────────────────────────────────────

def _cid(uids: list[str]) -> str:
    h = hashlib.md5(",".join(sorted(uids)).encode()).hexdigest()[:12]
    return f"p2_{h}"


# ── OSM Overpass: which named beach covers each point? ────────────────────────

def query_overpass(lat: float, lon: float, radius: int = OVERPASS_RADIUS) -> list[str]:
    """
    Return a list of OSM beach names within `radius` metres of (lat, lon).
    Result is cached in OSM_CACHE/overpass_{lat:.4f}_{lon:.4f}_{radius}.json
    """
    OSM_CACHE.mkdir(parents=True, exist_ok=True)
    cache_key = f"overpass_{lat:.4f}_{lon:.4f}_{radius}"
    cache_file = OSM_CACHE / f"{cache_key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())

    query = f"""
[out:json][timeout:15];
(
  way["natural"="beach"](around:{radius},{lat},{lon});
  relation["natural"="beach"](around:{radius},{lat},{lon});
);
out tags;
"""
    try:
        time.sleep(OVERPASS_DELAY)
        import urllib.parse
        resp = requests.post(
            OVERPASS_URL,
            data=urllib.parse.urlencode({"data": query}),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": OSM_UA,
                "Accept": "application/json",
            },
            timeout=20,
        )
        resp.raise_for_status()
        elements = resp.json().get("elements", [])
        names = [
            el["tags"]["name"]
            for el in elements
            if "tags" in el and "name" in el["tags"]
        ]
        cache_file.write_text(json.dumps(names, ensure_ascii=False))
        return names
    except Exception as e:
        print(f"  [overpass] failed: {e}")
        return []


# ── Google Places API: per-point verification ────────────────────────────────

def query_places(lat: float, lon: float) -> dict:
    """
    Ask Google Places Nearby Search whether there is an officially-named beach
    within PLACES_RADIUS metres of this point.

    Returns a dict with:
      found        – True if Google has a beach POI nearby
      name         – canonical Google name (or None)
      rating       – Google rating (or None)
      user_ratings – number of Google reviews (data quality signal)

    Results are cached. If MAPS_API_KEY is not set, returns {"found": False, "skipped": True}.
    """
    if not MAPS_API_KEY:
        return {"found": False, "skipped": True}

    cache_key  = f"places_{lat:.5f}_{lon:.5f}"
    cache_file = OSM_CACHE / f"{cache_key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())

    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {
        "location": f"{lat},{lon}",
        "radius":   PLACES_RADIUS,
        "type":     "natural_feature",
        "keyword":  "beach",
        "key":      MAPS_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("results", [])

        # Filter to results actually within radius (Google can return slightly outside)
        close = [
            r for r in results
            if _hav(lat, lon,
                    r["geometry"]["location"]["lat"],
                    r["geometry"]["location"]["lng"]) <= PLACES_RADIUS
        ]

        if close:
            top = max(close, key=lambda r: r.get("user_ratings_total", 0))
            result = {
                "found":        True,
                "name":         top.get("name"),
                "rating":       top.get("rating"),
                "user_ratings": top.get("user_ratings_total", 0),
                "place_id":     top.get("place_id"),
                "all_names":    [r.get("name") for r in close],
            }
        else:
            result = {"found": False}

        OSM_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result, ensure_ascii=False))
        return result

    except Exception as e:
        print(f"  [places] failed for ({lat:.5f},{lon:.5f}): {e}")
        return {"found": False, "error": str(e)}


# ── Nominatim reverse geocode: is this coordinate actually on a beach? ───────

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_UA  = "BeachDedup-Phase2/1.0 (research, not commercial)"

def query_nominatim(lat: float, lon: float) -> dict:
    """
    Reverse-geocode (lat, lon) via OSM Nominatim.
    Returns {"on_beach": bool, "osm_name": str|None, "osm_type": str|None}

    If the coordinate lands directly on an OSM beach polygon, osm_type will be
    "beach" and osm_name will be its name. Free, no API key, cached.
    """
    cache_key  = f"nom_{lat:.5f}_{lon:.5f}"
    cache_file = OSM_CACHE / f"{cache_key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())

    try:
        time.sleep(1.0)   # Nominatim asks for ≤1 req/sec
        resp = requests.get(
            NOMINATIM_URL,
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 17, "addressdetails": 1},
            headers={"User-Agent": NOMINATIM_UA},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        tags     = data.get("extratags") or {}
        cls      = data.get("class", "")
        typ      = data.get("type", "")
        name     = data.get("name") or data.get("display_name", "").split(",")[0]
        on_beach = (cls == "natural" and typ == "beach") or tags.get("natural") == "beach"
        result   = {
            "on_beach": on_beach,
            "osm_name": name if on_beach else None,
            "osm_class": cls,
            "osm_type":  typ,
        }
        OSM_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result, ensure_ascii=False))
        return result
    except Exception as e:
        return {"on_beach": False, "error": str(e)}


# ── Satellite tile (Google Maps Static API, same logic as phase 1) ────────────

_MARKER_COLORS = ["red", "blue", "green", "yellow", "purple", "orange", "white", "gray"]
_MARKER_LABELS = "ABCDEFGHIJ"


def fetch_satellite(points: list[dict], span_m: float) -> bytes | None:
    if not MAPS_API_KEY:
        return None

    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]
    clat = (min(lats) + max(lats)) / 2
    clon = (min(lons) + max(lons)) / 2

    zoom = 17
    if span_m > 150:  zoom = 16
    if span_m > 500:  zoom = 15
    if span_m > 1200: zoom = 14
    if span_m > 2500: zoom = 13

    markers = "&".join(
        f"markers=color:{_MARKER_COLORS[i % len(_MARKER_COLORS)]}"
        f"%7Clabel:{_MARKER_LABELS[i]}%7C{p['lat']},{p['lon']}"
        for i, p in enumerate(points)
    )
    cache_key = hashlib.md5(f"p2sat_{clat:.5f}_{clon:.5f}_{zoom}_{len(points)}".encode()).hexdigest()[:16]
    cache_file = TILE_CACHE / f"{cache_key}.jpg"
    if cache_file.exists():
        return cache_file.read_bytes()

    url = (
        f"https://maps.googleapis.com/maps/api/staticmap?"
        f"center={clat},{clon}&zoom={zoom}&size={SAT_SIZE}&scale={SAT_SCALE}"
        f"&maptype=satellite&{markers}&key={MAPS_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
        })
        r.raise_for_status()
        TILE_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(r.content)
        return r.content
    except Exception as e:
        print(f"  [sat] failed: {e}")
        return None


# ── OSM standard map tile (shows sand patches + name labels) ─────────────────

def _ll_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    n = 2 ** zoom
    tx = int((lon + 180) / 360 * n)
    lr = math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat)))
    ty = int((1 - lr / math.pi) / 2 * n)
    return tx, ty


def fetch_osm_tile(lat: float, lon: float, span_m: float) -> bytes | None:
    """
    Fetch a single OSM standard-map tile centred on (lat, lon).
    The tile shows tan/sand beach patches and labels — useful context for the AI.
    """
    zoom = 15
    if span_m > 1_200: zoom = 14
    if span_m > 3_000: zoom = 13

    tx, ty = _ll_to_tile(lat, lon, zoom)
    cache_file = OSM_CACHE / f"osm_{zoom}_{tx}_{ty}.png"
    if cache_file.exists():
        return cache_file.read_bytes()

    url = f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png"
    try:
        r = requests.get(url, headers={"User-Agent": OSM_UA}, timeout=10)
        r.raise_for_status()
        OSM_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(r.content)
        return r.content
    except Exception as e:
        print(f"  [osm] tile failed: {e}")
        return None


# ── Gemini analysis ───────────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """You are a Greek beach data specialist reviewing a cluster of {n} beach points.
These points were grouped because of name similarity and/or proximity.
Your job: determine if they are the SAME beach, SECTIONS of one long beach, or DISTINCT separate beaches.

=== POINTS (ordered A→Z, north to south) ===
{point_list}

=== SIGNALS ===
Total cluster span:            {span_m:.0f} m
Largest gap between adjacent:  {max_gap:.0f} m
Name similarity (0–1):         {name_sim:.2f}
OSM beach polygons nearby:     {osm_beaches}

=== GOOGLE MAPS VERIFICATION (per point) ===
{places_info}
  Interpretation guide:
  • Point has Google beach POI + high review count → that point is real and its name is canonical
  • Point has Google beach POI, different name → data has wrong/unofficial name
  • Point NOT on Google Maps → might be duplicate, misplaced, or a very small/unofficial beach
  • STRONG SIGNAL: if only one point has a Google POI → that's the primary; the others likely duplicate it

=== IMAGES PROVIDED ===
Image 1 — Satellite view.
  Coloured letter markers (A, B, C…) match the point list above.
  Look for: rocky outcrops, cliffs, jetties, rivers, buildings between markers.

Image 2 — OpenStreetMap standard map.
  Beach areas appear as tan/sand-coloured patches.
  Name labels appear on the map — compare them to the point names above.
  Look for: gaps in the tan beach colour between markers, different labelled beach sections.

=== DECISION RULES ===
SINGLE_BEACH:   All points are the same navigable beach.
                Use when: same tan patch, no physical break, name similarity is high,
                OR only one point appears on Google Maps (others are duplicates).
                → Will MERGE all into the primary point.

LONG_SECTIONS:  One overall beach but clearly separate sections (different parking/access,
                visible breaks, or different local names). A visitor would navigate to
                each section separately.
                → Keep all points; suggest naming for each section.

DISTINCT:       Genuinely different beaches separated by rocks, cliffs, or significant
                gaps in the coastline.
                → No action — keep all points as-is.

=== IMPORTANT CAVEATS ===
- Prefer DISTINCT over SINGLE_BEACH when uncertain.
- A long straight sand strip with different names at each end = LONG_SECTIONS, not SINGLE_BEACH.
- Do NOT merge points that are in different bays or around a headland.
- If OSM map shows different named beach patches at different markers → LONG_SECTIONS or DISTINCT.
- If Google Maps name differs from dataset name, record the Google name as suggested_label.

Respond in JSON only:
{{
  "cluster_type": "SINGLE_BEACH" | "LONG_SECTIONS" | "DISTINCT",
  "confidence": <0.00–1.00>,
  "reasoning": "<2–3 sentences explaining the decision, mentioning Google Maps data if relevant>",
  "primary_uid": "<uid of best point to keep if SINGLE_BEACH — prefer the one with Google POI>",
  "canonical_name": "<official Google/OSM name to use, or null if unclear>",
  "breaks": [
    {{
      "between": ["<uid_a>", "<uid_b>"],
      "break_type": "rocky_outcrop | jetty | river | building | bay_corner | osm_gap | name_change",
      "confidence": <0.00–1.00>
    }}
  ],
  "suggested_groups": [
    {{
      "uids": ["<uid>", "..."],
      "suggested_label": "<section name — use Google/OSM name when available>"
    }}
  ]
}}"""


def analyze_cluster(
    points: list[dict],
    sat_bytes: bytes | None,
    osm_bytes: bytes | None,
    osm_beach_names: list[str],
    places_by_uid: dict[str, dict],
    nominatim_by_uid: dict[str, dict],
    rl: RateLimiter,
) -> dict:
    if not GENAI_AVAILABLE:
        raise SystemExit("Install google-genai: pip install google-genai")
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not set")

    # Sort north→south so labels match positional order on satellite
    pts = sorted(points, key=lambda p: -p["lat"])
    labels = _MARKER_LABELS

    lats = [p["lat"] for p in pts]
    lons = [p["lon"] for p in pts]
    gaps = [_hav(pts[i]["lat"], pts[i]["lon"], pts[i+1]["lat"], pts[i+1]["lon"])
            for i in range(len(pts) - 1)]
    max_gap = max(gaps) if gaps else 0
    span = _hav(min(lats), min(lons), max(lats), max(lons))

    # Name similarity between first and last (loosely representative)
    name_sim = _sim(pts[0]["name"], pts[-1]["name"]) if len(pts) > 1 else 1.0

    # Adjacent distances
    point_list = "\n".join(
        f"  {labels[i]} uid={p['uid']}  name={p['name']!r}  "
        f"lat={p['lat']:.6f}  lon={p['lon']:.6f}"
        + (f"  gap_to_next={gaps[i]:.0f}m" if i < len(gaps) else "")
        for i, p in enumerate(pts)
    )

    osm_str = (", ".join(f'"{n}"' for n in osm_beach_names)) if osm_beach_names else "none found"

    # Build per-point coordinate verification summary (Nominatim + Places)
    places_lines = []
    for i, p in enumerate(pts):
        uid  = p["uid"]
        nom  = nominatim_by_uid.get(uid, {})
        pr   = places_by_uid.get(uid, {})
        parts_line = []

        # Nominatim: does the coordinate land on an OSM beach polygon?
        if nom.get("on_beach"):
            osm_n = nom.get("osm_name", "")
            match = " (same name)" if osm_n and _sim(p["name"], osm_n) > 0.7 else f" OSM calls it '{osm_n}'" if osm_n else ""
            parts_line.append(f"coordinate IS on OSM beach polygon{match}")
        elif "error" not in nom:
            parts_line.append(f"coordinate NOT on OSM beach polygon (landed on: {nom.get('osm_type','?')})")

        # Google Places
        if pr.get("skipped"):
            pass  # API not available, omit
        elif pr.get("found"):
            gname   = pr.get("name", "?")
            rating  = pr.get("rating")
            nrev    = pr.get("user_ratings", 0)
            name_note = "(same name)" if _sim(p["name"], gname) > 0.7 else f"Google name: '{gname}'"
            rating_str = f" ⭐{rating} ({nrev} reviews)" if rating else ""
            parts_line.append(f"found on Google Maps — {name_note}{rating_str}")
        else:
            parts_line.append("NOT found on Google Maps")

        label_str = f"  {labels[i]} {p['name']!r}: "
        places_lines.append(label_str + (" | ".join(parts_line) if parts_line else "no data"))

    places_info = "\n".join(places_lines) if places_lines else "  (not queried)"

    prompt = _PROMPT_TEMPLATE.format(
        n=len(pts),
        point_list=point_list,
        span_m=span,
        max_gap=max_gap,
        name_sim=name_sim,
        osm_beaches=osm_str,
        places_info=places_info,
    )

    parts: list[Any] = []
    if sat_bytes:
        parts.append(genai_types.Part.from_bytes(data=sat_bytes, mime_type="image/jpeg"))
    if osm_bytes:
        parts.append(genai_types.Part.from_bytes(data=osm_bytes, mime_type="image/png"))
    parts.append(genai_types.Part.from_text(text=prompt))

    rl.wait()
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=parts,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    return json.loads(resp.text)


# ── Pick the best primary point to keep ───────────────────────────────────────

def _pick_primary(points: list[dict], ai_uid: str | None) -> str:
    if ai_uid and any(p["uid"] == ai_uid for p in points):
        return ai_uid
    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]
    clat = sum(lats) / len(lats)
    clon = sum(lons) / len(lons)
    return min(points, key=lambda p: (
        -len(p["name"] or ""),              # longer name preferred
        _hav(p["lat"], p["lon"], clat, clon),  # then closest to centroid
    ))["uid"]


# ── Build a proposed_change record ────────────────────────────────────────────

def _build_change(
    cid: str,
    points: list[dict],
    result: dict,
    osm_beach_names: list[str],
    places_by_uid: dict[str, dict],
    nominatim_by_uid: dict[str, dict],
) -> dict | None:
    ct   = result.get("cluster_type", "DISTINCT")
    conf = float(result.get("confidence", 0.0))

    if ct == "DISTINCT":
        return None     # nothing to suggest

    primary  = _pick_primary(points, result.get("primary_uid"))
    discards = ([p["uid"] for p in points if p["uid"] != primary]
                if ct == "SINGLE_BEACH" else [])

    auto = (ct == "SINGLE_BEACH" and conf >= AUTO_APPROVE and len(points) == 2)

    # Format points to match the ReviewPanel's ReviewPoint shape
    review_points = [
        {
            "uid":         p["uid"],
            "name":        [p["name"]] if p["name"] else [],
            "coordinates": [p["lon"], p["lat"]],
            "properties":  {},
        }
        for p in points
    ]

    # Map phase-2 cluster types to the type strings the UI already knows
    ui_type = "DUPLICATE" if ct == "SINGLE_BEACH" else "SUB_PARTS"
    action  = "MERGE_INTO_PRIMARY" if ct == "SINGLE_BEACH" else "REVIEW_SECTIONS"

    # Summarise per-point geo-verification for the UI
    gmaps_summary = {
        uid: {
            "found":        pr.get("found", False),
            "name":         pr.get("name"),
            "rating":       pr.get("rating"),
            "user_ratings": pr.get("user_ratings", 0),
            "on_osm_beach": nominatim_by_uid.get(uid, {}).get("on_beach", False),
            "osm_beach_name": nominatim_by_uid.get(uid, {}).get("osm_name"),
        }
        for uid, pr in {**{u: {} for u in [p["uid"] for p in points]}, **places_by_uid}.items()
        if uid in {p["uid"] for p in points}
    }

    return {
        "id":               cid,
        "cluster_id":       cid,
        "phase":            2,
        "type":             ui_type,
        "p2_cluster_type":  ct,
        "confidence":       conf,
        "reasoning":        result.get("reasoning", ""),
        "canonical_name":   result.get("canonical_name"),
        "satellite_analyzed": True,
        "breaks":           result.get("breaks", []),
        "suggested_groups": result.get("suggested_groups", []),
        "osm_beach_names":  osm_beach_names,
        "gmaps":            gmaps_summary,
        "points":           review_points,
        "primary_uid":      primary,
        "discard_uids":     discards,
        "proposed_action":  action,
        "status":           "auto_approved" if auto else "pending_review",
        "decided_at":       None,
        "created_at":       datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Phase 2 beach clustering pipeline")
    ap.add_argument("--all",     action="store_true", help="Include phase-1 cluster points")
    ap.add_argument("--rescore", action="store_true", help="Re-score cached results only (0 API)")
    ap.add_argument("--dry-run", action="store_true", help="Print clusters, no API or file writes")
    ap.add_argument("--reset",   action="store_true", help="Delete all phase-2 cache and restart")
    args = ap.parse_args()

    if args.reset:
        import shutil
        for d in [CACHE_DIR, OSM_CACHE]:
            if d.exists():
                shutil.rmtree(d)
                print(f"Deleted {d}")
        if RATE_FILE.exists():
            RATE_FILE.unlink()
            print(f"Deleted {RATE_FILE}")
        print("Reset complete. Re-run without --reset to start fresh.")
        return

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OSM_CACHE.mkdir(parents=True, exist_ok=True)
    TILE_CACHE.mkdir(parents=True, exist_ok=True)

    # ── Load GeoJSON ──────────────────────────────────────────────────────────
    print(f"Loading {GEOJSON}…")
    fc = json.loads(GEOJSON.read_text(encoding="utf-8"))
    all_points: list[dict] = []
    for feat in fc["features"]:
        props = feat.get("properties") or {}
        uid   = props.get("uid")
        name  = props.get("name") or props.get("Name") or ""
        coords = feat["geometry"]["coordinates"]
        lon, lat = float(coords[0]), float(coords[1])
        if uid:
            all_points.append({"uid": uid, "name": str(name), "lat": lat, "lon": lon})
    print(f"  {len(all_points):,} features loaded")

    # ── Exclude points already in phase-1 clusters ────────────────────────────
    if not args.all and PROPOSED.exists():
        existing = json.loads(PROPOSED.read_text(encoding="utf-8"))
        p1_uids: set[str] = set()
        for ch in existing.get("changes", []):
            if ch.get("phase", 1) == 1:
                for p in ch.get("points", []):
                    p1_uids.add(p.get("uid", ""))
        before = len(all_points)
        all_points = [p for p in all_points if p["uid"] not in p1_uids]
        print(f"  Excluded {before - len(all_points):,} points already in phase-1 clusters")

    # ── Exclude points already processed in phase 2 ───────────────────────────
    if not args.rescore and PROPOSED.exists():
        existing = json.loads(PROPOSED.read_text(encoding="utf-8"))
        p2_uids: set[str] = set()
        for ch in existing.get("changes", []):
            if ch.get("phase") == 2:
                for p in ch.get("points", []):
                    p2_uids.add(p.get("uid", ""))
        before = len(all_points)
        all_points = [p for p in all_points if p["uid"] not in p2_uids]
        print(f"  Excluded {before - len(all_points):,} already processed in phase-2")

    print(f"  {len(all_points):,} points remain for phase-2 clustering")
    if not all_points:
        print("Nothing to do.")
        return

    # ── Build similarity graph ────────────────────────────────────────────────
    print("\nBuilding graph…")
    by_uid = {p["uid"]: p for p in all_points}

    # Longitude-sweep for O(n log n) instead of O(n²)
    sorted_pts = sorted(all_points, key=lambda p: p["lon"])
    edges: list[tuple[str, str]] = []
    pairs_checked = 0

    for i, a in enumerate(sorted_pts):
        cos_lat = math.cos(math.radians(a["lat"]))
        lon_limit = DIST_HIGH / (111_320 * (cos_lat or 0.01))
        for j in range(i + 1, len(sorted_pts)):
            b = sorted_pts[j]
            if b["lon"] - a["lon"] > lon_limit:
                break
            d = _hav(a["lat"], a["lon"], b["lat"], b["lon"])
            if d > DIST_HIGH:
                continue
            pairs_checked += 1
            sim = _sim(a["name"], b["name"])
            if (sim >= NAME_HIGH and d <= DIST_HIGH) or \
               (sim >= NAME_MED  and d <= DIST_MED)  or \
               (sim >= NAME_LOW  and d <= DIST_LOW):
                edges.append((a["uid"], b["uid"]))

    print(f"  {pairs_checked:,} pairs checked → {len(edges)} edges")

    # ── Connected components ──────────────────────────────────────────────────
    raw_clusters = _components(edges, [p["uid"] for p in all_points])

    # Filter oversized clusters
    valid: list[tuple[list[str], float]] = []
    for cl_uids in raw_clusters:
        pts = [by_uid[u] for u in cl_uids]
        lats = [p["lat"] for p in pts]
        lons = [p["lon"] for p in pts]
        spans = [
            _hav(pts[i]["lat"], pts[i]["lon"], pts[j]["lat"], pts[j]["lon"])
            for i in range(len(pts))
            for j in range(i + 1, len(pts))
        ]
        span = max(spans) if spans else 0
        if span <= MAX_CLUSTER_SPAN:
            valid.append((cl_uids, span))
        else:
            names = " | ".join(p["name"][:30] for p in pts[:3])
            print(f"  Skipping {len(cl_uids)}-pt cluster spanning {span:.0f}m (>{MAX_CLUSTER_SPAN}m): {names}")

    print(f"  {len(valid)} clusters to process\n")

    if args.dry_run:
        for cl_uids, span in valid:
            pts = [by_uid[u] for u in cl_uids]
            names = " | ".join(p["name"] for p in pts)
            sims = [_sim(pts[i]["name"], pts[j]["name"])
                    for i in range(len(pts)) for j in range(i+1, len(pts))]
            avg_sim = sum(sims) / len(sims) if sims else 0
            print(f"  {len(cl_uids)} pts  span={span:.0f}m  sim={avg_sim:.2f}  {names[:100]}")
        return

    # ── Rescore mode ──────────────────────────────────────────────────────────
    if args.rescore:
        print("Rescoring cached results…")
        data = json.loads(PROPOSED.read_text(encoding="utf-8")) if PROPOSED.exists() \
               else {"changes": [], "meta": {}}
        changed = 0
        for ch in data["changes"]:
            if ch.get("phase") != 2 or ch["status"] in ("approved", "rejected"):
                continue
            should = (
                ch["p2_cluster_type"] == "SINGLE_BEACH"
                and ch["confidence"] >= AUTO_APPROVE
                and len(ch["points"]) == 2
            )
            was = ch["status"] == "auto_approved"
            if should != was:
                ch["status"] = "auto_approved" if should else "pending_review"
                changed += 1
        _write_proposed(data)
        print(f"  {changed} clusters changed status")
        return

    # ── Process clusters ──────────────────────────────────────────────────────
    rl = RateLimiter()
    new_changes: list[dict] = []
    errors = 0

    for idx, (cl_uids, span) in enumerate(valid):
        pts  = [by_uid[u] for u in cl_uids]
        cid  = _cid(cl_uids)
        cache_file = CACHE_DIR / f"{cid}.json"

        names_str = " | ".join(p["name"] for p in pts)
        print(f"[{idx+1}/{len(valid)}] {cid}  {len(pts)}pts  {span:.0f}m  {names_str[:80]}")

        if cache_file.exists():
            cached = json.loads(cache_file.read_text())
            osm_beach_names  = cached.pop("_osm_beach_names", [])
            places_by_uid    = cached.pop("_places_by_uid", {})
            nominatim_by_uid = cached.pop("_nominatim_by_uid", {})
            result = cached
            print(f"  [cached] {result.get('cluster_type')}  conf={result.get('confidence', 0):.2f}")
        else:
            lats  = [p["lat"] for p in pts]
            lons  = [p["lon"] for p in pts]
            clat  = (min(lats) + max(lats)) / 2
            clon  = (min(lons) + max(lons)) / 2

            # Signal 3: Overpass (free OSM beach polygons)
            print("  [overpass] querying…", end=" ", flush=True)
            osm_beach_names = query_overpass(clat, clon)
            print(f"{osm_beach_names or 'none'}")

            # Signal 4a: Nominatim reverse geocode — is each coordinate on an OSM beach?
            print("  [nominatim] checking each point…", end=" ", flush=True)
            nominatim_by_uid: dict[str, dict] = {}
            for p in pts:
                nominatim_by_uid[p["uid"]] = query_nominatim(p["lat"], p["lon"])
            on_beach_count = sum(1 for r in nominatim_by_uid.values() if r.get("on_beach"))
            print(f"{on_beach_count}/{len(pts)} land on OSM beach polygon")

            # Signal 4b: Google Places per point (canonical name + review count)
            places_by_uid: dict[str, dict] = {}
            if MAPS_API_KEY:
                print("  [places] checking each point…", end=" ", flush=True)
                for p in pts:
                    places_by_uid[p["uid"]] = query_places(p["lat"], p["lon"])
                found_count = sum(1 for r in places_by_uid.values() if r.get("found"))
                print(f"{found_count}/{len(pts)} found on Google Maps")
            else:
                print("  [places] MAPS_API_KEY not set — skipping")

            # Signal 5: Satellite image with markers
            print("  [sat] fetching…", end=" ", flush=True)
            sat = fetch_satellite(pts, span)
            print("ok" if sat else "unavailable")

            # Signal 6: OSM standard tile (tan beach patches + labels)
            print("  [osm] fetching…", end=" ", flush=True)
            osm = fetch_osm_tile(clat, clon, span)
            print("ok" if osm else "unavailable")

            if not sat and not osm:
                print("  [warn] no images — skipping Gemini call")
                errors += 1
                result = {
                    "cluster_type": "DISTINCT",
                    "confidence":   0.0,
                    "reasoning":    "No images available for analysis.",
                    "primary_uid":  None, "canonical_name": None,
                    "breaks": [], "suggested_groups": [],
                }
            else:
                try:
                    result = analyze_cluster(pts, sat, osm, osm_beach_names, places_by_uid, nominatim_by_uid, rl)
                    # Cache everything together so rescore can use the same data
                    cached_data = {
                        **result,
                        "_osm_beach_names":   osm_beach_names,
                        "_places_by_uid":     places_by_uid,
                        "_nominatim_by_uid":  nominatim_by_uid,
                    }
                    cache_file.write_text(json.dumps(cached_data, ensure_ascii=False, indent=2))
                    print(f"  → {result.get('cluster_type')}  conf={result.get('confidence', 0):.2f}")
                    if result.get("canonical_name"):
                        print(f"     canonical: {result['canonical_name']!r}")
                    print(f"     {result.get('reasoning', '')[:110]}")
                except Exception as e:
                    print(f"  [ERROR] {e}")
                    errors += 1
                    continue

        ch = _build_change(cid, pts, result, osm_beach_names, places_by_uid, nominatim_by_uid)
        if ch:
            new_changes.append(ch)

    # ── Merge into proposed_changes.json ──────────────────────────────────────
    print(f"\nWriting {len(new_changes)} new phase-2 changes…")

    if PROPOSED.exists():
        data = json.loads(PROPOSED.read_text(encoding="utf-8"))
    else:
        data = {"changes": [], "meta": {}}

    existing_ids = {c["id"] for c in data["changes"]}
    added = 0
    for ch in new_changes:
        if ch["id"] not in existing_ids:
            data["changes"].append(ch)
            added += 1

    _write_proposed(data)

    pending = sum(1 for c in new_changes if c["status"] == "pending_review")
    auto    = sum(1 for c in new_changes if c["status"] == "auto_approved")
    print(f"\nDone. Added {added} changes ({pending} pending review, {auto} auto-approved)")
    if errors:
        print(f"  {errors} skipped — re-run to retry")


def _write_proposed(data: dict):
    changes: list[dict] = data["changes"]
    data["meta"] = {
        **data.get("meta", {}),
        "total":             len(changes),
        "pending_review":    sum(1 for c in changes if c.get("status") == "pending_review"),
        "auto_approved":     sum(1 for c in changes if c.get("status") == "auto_approved"),
        "approved":          sum(1 for c in changes if c.get("status") == "approved"),
        "rejected":          sum(1 for c in changes if c.get("status") == "rejected"),
        "phase1_total":      sum(1 for c in changes if c.get("phase", 1) == 1),
        "phase2_total":      sum(1 for c in changes if c.get("phase") == 2),
        "phase2_pending":    sum(1 for c in changes if c.get("phase") == 2 and c.get("status") == "pending_review"),
    }
    PROPOSED.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
