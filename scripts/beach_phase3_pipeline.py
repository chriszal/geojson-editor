#!/usr/bin/env python3
"""
Phase 3 pipeline — pure geographic proximity clustering ("bay/coastal-strip" clustering).

Catches the cases Phase 1 (150m DBSCAN) and Phase 2 (name similarity) missed:
multiple pins along the same continuous beach that are spaced 150–500m apart
and have unrelated or different names.

Algorithm
---------
1. Load all points not already in a Phase 1 or Phase 2 cluster.
2. Run DBSCAN at 500m radius (haversine, no name requirement).
3. For oversized clusters (>20 pts or span >5km), split with a tighter 250m pass.
4. For each candidate cluster send to Gemini:
     - Wide satellite image showing the full coastal strip
     - OSM map tile (tan beach patches + labels)
     - Per-point Nominatim + Places data
   Ask: "Segment this into distinct navigable beach sections."
5. Output to proposed_changes.json with phase: 3.

Usage
-----
  python scripts/beach_phase3_pipeline.py           # normal run
  python scripts/beach_phase3_pipeline.py --dry-run # show clusters, no API
  python scripts/beach_phase3_pipeline.py --rescore # re-score cached results
  python scripts/beach_phase3_pipeline.py --reset   # delete phase-3 cache
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
import unicodedata
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import requests
from sklearn.cluster import DBSCAN

try:
    from google import genai
    from google.genai import types as genai_types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False


# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent.parent
DATA       = ROOT / "data_new"
GEOJSON    = DATA / "current.json"
PROPOSED   = DATA / "proposed_changes.json"
CACHE_DIR  = DATA / "cluster_results_p3"
TILE_CACHE = DATA / "tile_cache"
OSM_CACHE  = DATA / "tile_cache_p2"     # reuse phase-2 OSM + nominatim cache
RATE_FILE  = DATA / "rate_state_p3.json"

# ── Tunables ──────────────────────────────────────────────────────────────────
GEMINI_MODEL  = "gemini-2.5-flash"
RPM_LIMIT     = 13
RPD_LIMIT     = 2_000
AUTO_APPROVE  = 0.92   # confidence for auto-approving 2-point SINGLE_BEACH

# DBSCAN
EPS_PRIMARY   = 500    # metres — main clustering radius
EPS_SPLIT     = 250    # metres — used to split oversized clusters
MAX_PTS       = 20     # clusters larger than this get split first
MAX_SPAN      = 6_000  # metres — absolute max cluster span (skip if larger)
MIN_SPAN      = 50     # metres — skip if all points basically the same spot (p1/p2 already got it)

# APIs
MAPS_API_KEY  = os.environ.get("MAPS_API_KEY", "")
MAPBOX_TOKEN  = os.environ.get("MAPBOX_TOKEN", "")
PLACES_RADIUS = 150
OSM_UA        = "BeachDedup-Phase3/1.0 (research project)"
OVERPASS_URL  = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"


# ── Utilities ─────────────────────────────────────────────────────────────────

def _hav(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def _span(pts: list[dict]) -> float:
    if len(pts) < 2:
        return 0.0
    return max(
        _hav(pts[i]["lat"], pts[i]["lon"], pts[j]["lat"], pts[j]["lon"])
        for i in range(len(pts)) for j in range(i+1, len(pts))
    )


def _cid(uids: list[str]) -> str:
    h = hashlib.md5(",".join(sorted(uids)).encode()).hexdigest()[:12]
    return f"p3_{h}"


# ── Rate limiter ──────────────────────────────────────────────────────────────

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
            print(f"  [rate] Daily cap — sleeping {secs:.0f}s")
            time.sleep(secs + 5)
            self._day_start = math.floor(time.time() / 86400) * 86400
            self._day_count = 0
        self._rpm_window = [t for t in self._rpm_window if now - t < 60]
        if len(self._rpm_window) >= RPM_LIMIT:
            wait = 60 - (now - self._rpm_window[0]) + 0.5
            print(f"  [rate] RPM limit — sleeping {wait:.1f}s")
            time.sleep(wait)
            self._rpm_window = [t for t in self._rpm_window if time.time() - t < 60]
        self._rpm_window.append(time.time())
        self._day_count += 1
        self._save()


# ── DBSCAN clustering ─────────────────────────────────────────────────────────

def _dbscan(pts: list[dict], eps_m: float) -> list[list[dict]]:
    if len(pts) < 2:
        return []
    coords  = np.radians([[p["lat"], p["lon"]] for p in pts])
    eps_rad = eps_m / 6_371_000.0
    labels  = DBSCAN(eps=eps_rad, min_samples=2, metric="haversine").fit_predict(coords)
    groups: dict[int, list[dict]] = {}
    for pt, lbl in zip(pts, labels):
        if lbl >= 0:
            groups.setdefault(lbl, []).append(pt)
    return list(groups.values())


def build_clusters(all_pts: list[dict]) -> list[list[dict]]:
    """
    Run primary DBSCAN at EPS_PRIMARY, then split oversized clusters with EPS_SPLIT.
    Returns list of clusters (each a list of point dicts).
    """
    raw = _dbscan(all_pts, EPS_PRIMARY)
    result: list[list[dict]] = []
    for cl in raw:
        if len(cl) <= MAX_PTS and _span(cl) <= MAX_SPAN:
            result.append(cl)
        elif len(cl) > MAX_PTS or _span(cl) > MAX_SPAN:
            # Split with tighter radius
            sub = _dbscan(cl, EPS_SPLIT)
            for s in sub:
                sp = _span(s)
                if sp <= MAX_SPAN and sp >= MIN_SPAN:
                    result.append(s)
                elif sp > MAX_SPAN:
                    print(f"  [skip] {len(s)}-pt sub-cluster still spans {sp:.0f}m > {MAX_SPAN}m")
    # Drop clusters whose span is too small (already covered by p1/p2)
    return [cl for cl in result if _span(cl) >= MIN_SPAN]


# ── Nominatim (cached, reuse phase-2 cache) ───────────────────────────────────

def query_nominatim(lat: float, lon: float) -> dict:
    cache_file = OSM_CACHE / f"nom_{lat:.5f}_{lon:.5f}.json"
    if cache_file.exists():
        try:
            content = cache_file.read_text()
            if content.strip():
                return json.loads(content)
        except (json.JSONDecodeError, Exception):
            pass
    try:
        time.sleep(1.0)
        r = requests.get(
            NOMINATIM_URL,
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 17, "addressdetails": 1},
            headers={"User-Agent": OSM_UA},
            timeout=10,
        )
        r.raise_for_status()
        d        = r.json()
        cls      = d.get("class", "")
        typ      = d.get("type", "")
        on_beach = cls == "natural" and typ == "beach"
        result   = {"on_beach": on_beach, "osm_name": d.get("name") if on_beach else None,
                    "osm_class": cls, "osm_type": typ}
        OSM_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result, ensure_ascii=False))
        return result
    except Exception as e:
        return {"on_beach": False, "error": str(e)}


# ── Google Places (cached, reuse phase-2 cache) ───────────────────────────────

def query_places(lat: float, lon: float) -> dict:
    if not MAPS_API_KEY:
        return {"found": False, "skipped": True}
    cache_file = OSM_CACHE / f"places_{lat:.5f}_{lon:.5f}.json"
    if cache_file.exists():
        try:
            content = cache_file.read_text()
            if content.strip():
                return json.loads(content)
        except (json.JSONDecodeError, Exception):
            pass
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
            params={"location": f"{lat},{lon}", "radius": PLACES_RADIUS,
                    "type": "natural_feature", "keyword": "beach", "key": MAPS_API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        close = [
            res for res in r.json().get("results", [])
            if _hav(lat, lon, res["geometry"]["location"]["lat"],
                    res["geometry"]["location"]["lng"]) <= PLACES_RADIUS
        ]
        if close:
            top    = max(close, key=lambda x: x.get("user_ratings_total", 0))
            result = {"found": True, "name": top.get("name"),
                      "rating": top.get("rating"), "user_ratings": top.get("user_ratings_total", 0)}
        else:
            result = {"found": False}
        OSM_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result, ensure_ascii=False))
        return result
    except Exception as e:
        return {"found": False, "error": str(e)}


# ── Satellite image (Mapbox preferred — free 50k/month, auto-bbox) ────────────

# Mapbox hex colors matching the label order A B C D E F G H I J
_MB_COLORS = ["f00","00f","0a0","ff0","80f","f80","fff","888","964B00","f9a"]
_LABELS    = "ABCDEFGHIJ"

def fetch_satellite(pts: list[dict], span_m: float) -> bytes | None:
    # Prefer Mapbox (free, auto-zoom fits all markers perfectly)
    if MAPBOX_TOKEN:
        return _fetch_mapbox(pts)
    if MAPS_API_KEY:
        return _fetch_gmaps(pts, span_m)
    return None


def _fetch_mapbox(pts: list[dict]) -> bytes | None:
    # Mapbox Static Images API with auto bounding box
    # Format: pin-s+RRGGBB(lon,lat)  — up to ~25 markers before URL gets too long
    markers = ",".join(
        f"pin-m+{_MB_COLORS[i % len(_MB_COLORS)]}({p['lon']:.6f},{p['lat']:.6f})"
        for i, p in enumerate(pts[:15])
    )
    cache_key  = hashlib.md5(f"p3mb_{'_'.join(p['uid'] for p in pts)}".encode()).hexdigest()[:16]
    cache_file = TILE_CACHE / f"{cache_key}.jpg"
    if cache_file.exists():
        return cache_file.read_bytes()

    url = (
        f"https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/static/"
        f"{markers}/auto/640x400@2x"
        f"?padding=60&access_token={MAPBOX_TOKEN}"
    )
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        TILE_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(r.content)
        return r.content
    except Exception as e:
        print(f"  [mapbox] {e}")
        return None


def _fetch_gmaps(pts: list[dict], span_m: float) -> bytes | None:
    lats = [p["lat"] for p in pts]
    lons = [p["lon"] for p in pts]
    clat = (min(lats) + max(lats)) / 2
    clon = (min(lons) + max(lons)) / 2
    zoom = 16
    if span_m > 300:  zoom = 15
    if span_m > 800:  zoom = 14
    if span_m > 2000: zoom = 13
    if span_m > 4000: zoom = 12
    colors  = ["red","blue","green","yellow","purple","orange","white","gray"]
    markers = "&".join(
        f"markers=color:{colors[i%len(colors)]}%7Clabel:{_LABELS[i%10]}%7C{p['lat']},{p['lon']}"
        for i, p in enumerate(pts[:10])
    )
    cache_key  = hashlib.md5(f"p3sat_{clat:.5f}_{clon:.5f}_{zoom}_{len(pts)}".encode()).hexdigest()[:16]
    cache_file = TILE_CACHE / f"{cache_key}.jpg"
    if cache_file.exists():
        return cache_file.read_bytes()
    url = (
        f"https://maps.googleapis.com/maps/api/staticmap?"
        f"center={clat},{clon}&zoom={zoom}&size=640x400&scale=2"
        f"&maptype=satellite&{markers}&key={MAPS_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        r.raise_for_status()
        TILE_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(r.content)
        return r.content
    except Exception as e:
        print(f"  [gmaps] {e}")
        return None


# ── OSM map tile ──────────────────────────────────────────────────────────────

def _ll_to_tile(lat, lon, zoom):
    n  = 2 ** zoom
    tx = int((lon + 180) / 360 * n)
    lr = math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat)))
    ty = int((1 - lr / math.pi) / 2 * n)
    return tx, ty

def fetch_osm_tile(lat, lon, span_m) -> bytes | None:
    zoom = 15
    if span_m > 1200: zoom = 14
    if span_m > 3000: zoom = 13
    if span_m > 5000: zoom = 12
    tx, ty     = _ll_to_tile(lat, lon, zoom)
    cache_file = OSM_CACHE / f"osm_{zoom}_{tx}_{ty}.png"
    if cache_file.exists():
        return cache_file.read_bytes()
    try:
        r = requests.get(
            f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png",
            headers={"User-Agent": OSM_UA}, timeout=10,
        )
        r.raise_for_status()
        OSM_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(r.content)
        return r.content
    except Exception as e:
        print(f"  [osm] {e}")
        return None


# ── Gemini analysis ───────────────────────────────────────────────────────────

_PROMPT = """You are a Greek beach data specialist. These {n} points were found within {span_m:.0f} m of each other on a coastline.
They may be: (A) the same single beach entered multiple times, (B) distinct sections of one long beach that visitors navigate to separately, or (C) genuinely separate beaches in different bays.

=== POINTS (A→Z, north to south) ===
{point_list}

=== COORDINATE VERIFICATION ===
{verify_info}

=== IMAGES ===
Image 1 — Satellite view. Markers A, B, C… match the points above.
Image 2 — OpenStreetMap. Tan/sand patches = beach areas. Labels = beach names.

=== YOUR TASK ===
Look carefully at the images for PHYSICAL BREAKS between adjacent markers:
  • Rocky headland or cliff separating two bays
  • Jetty, river mouth, or man-made barrier
  • Clear gap in the tan beach colour on the OSM tile
  • Sharp change in coastline direction (rounding a cape)

Then decide:

SINGLE_BEACH  – all are the same beach, merge into one point.
LONG_SECTIONS – one overall beach with 2+ distinct navigable sections.
               Each section gets its own point (keep all), just group and name them.
DISTINCT      – genuinely separate beaches in different bays. Keep all, no action.

IMPORTANT:
- A long straight sandy strip with no physical breaks = SINGLE_BEACH even if 2km long.
- Different names at different ends of the same sand = LONG_SECTIONS (keep both, fix names).
- If a headland or cape is visible between markers, those sides are DISTINCT.
- When uncertain between SINGLE_BEACH and LONG_SECTIONS, choose LONG_SECTIONS (safer — keeps data).
- When uncertain between LONG_SECTIONS and DISTINCT, choose DISTINCT.

Respond in JSON only:
{{
  "cluster_type": "SINGLE_BEACH" | "LONG_SECTIONS" | "DISTINCT",
  "confidence": <0.00–1.00>,
  "reasoning": "<2–3 sentences>",
  "primary_uid": "<uid of best point if SINGLE_BEACH, else null>",
  "canonical_name": "<official name from OSM/Google if found, else null>",
  "breaks": [
    {{
      "between": ["<uid_a>", "<uid_b>"],
      "break_type": "headland | jetty | river | building | bay_corner | osm_gap",
      "confidence": <0.00–1.00>
    }}
  ],
  "suggested_groups": [
    {{
      "uids": ["<uid>", "..."],
      "suggested_label": "<name for this section>"
    }}
  ]
}}"""


def analyze(pts: list[dict], sat: bytes | None, osm: bytes | None,
            nom_by_uid: dict, places_by_uid: dict, rl: RateLimiter) -> dict:
    if not GENAI_AVAILABLE:
        raise SystemExit("pip install google-genai")
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not set")

    ordered = sorted(pts, key=lambda p: -p["lat"])
    gaps    = [_hav(ordered[i]["lat"], ordered[i]["lon"],
                    ordered[i+1]["lat"], ordered[i+1]["lon"])
               for i in range(len(ordered)-1)]
    span    = _span(ordered)

    point_list = "\n".join(
        f"  {_LABELS[i]} uid={p['uid']}  name={p['name']!r}  "
        f"lat={p['lat']:.6f}  lon={p['lon']:.6f}"
        + (f"  gap_to_next={gaps[i]:.0f}m" if i < len(gaps) else "")
        for i, p in enumerate(ordered)
    )

    verify_lines = []
    for i, p in enumerate(ordered):
        uid  = p["uid"]
        nom  = nom_by_uid.get(uid, {})
        pl   = places_by_uid.get(uid, {})
        parts = []
        if nom.get("on_beach"):
            parts.append(f"on OSM beach polygon '{nom.get('osm_name','?')}'")
        else:
            parts.append(f"NOT on OSM beach polygon (landed on: {nom.get('osm_type','?')})")
        if not pl.get("skipped"):
            if pl.get("found"):
                parts.append(f"Google Maps: '{pl.get('name')}' ⭐{pl.get('rating','?')} ({pl.get('user_ratings',0)} reviews)")
            else:
                parts.append("not on Google Maps")
        verify_lines.append(f"  {_LABELS[i]} {p['name']!r}: " + " | ".join(parts))

    prompt = _PROMPT.format(
        n=len(ordered), span_m=span,
        point_list=point_list,
        verify_info="\n".join(verify_lines),
    )

    parts_msg: list[Any] = []
    if sat:
        parts_msg.append(genai_types.Part.from_bytes(data=sat, mime_type="image/jpeg"))
    if osm:
        parts_msg.append(genai_types.Part.from_bytes(data=osm, mime_type="image/png"))
    parts_msg.append(genai_types.Part.from_text(text=prompt))

    rl.wait()
    client = genai.Client(api_key=api_key)
    resp   = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=parts_msg,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    return json.loads(resp.text)


# ── Build change record ───────────────────────────────────────────────────────

def _pick_primary(pts: list[dict], ai_uid: str | None) -> str:
    if ai_uid and any(p["uid"] == ai_uid for p in pts):
        return ai_uid
    lats = [p["lat"] for p in pts]
    lons = [p["lon"] for p in pts]
    clat, clon = sum(lats)/len(lats), sum(lons)/len(lons)
    return min(pts, key=lambda p: (-len(p["name"] or ""), _hav(p["lat"], p["lon"], clat, clon)))["uid"]


def _build_change(cid, pts, result, nom_by_uid, places_by_uid) -> dict | None:
    ct   = result.get("cluster_type", "DISTINCT")
    conf = float(result.get("confidence", 0))
    if ct == "DISTINCT":
        return None

    primary  = _pick_primary(pts, result.get("primary_uid"))
    discards = [p["uid"] for p in pts if p["uid"] != primary] if ct == "SINGLE_BEACH" else []
    auto     = ct == "SINGLE_BEACH" and conf >= AUTO_APPROVE and len(pts) == 2

    gmaps = {
        p["uid"]: {
            "found":        places_by_uid.get(p["uid"], {}).get("found", False),
            "name":         places_by_uid.get(p["uid"], {}).get("name"),
            "rating":       places_by_uid.get(p["uid"], {}).get("rating"),
            "user_ratings": places_by_uid.get(p["uid"], {}).get("user_ratings", 0),
            "on_osm_beach": nom_by_uid.get(p["uid"], {}).get("on_beach", False),
            "osm_beach_name": nom_by_uid.get(p["uid"], {}).get("osm_name"),
        }
        for p in pts
    }

    return {
        "id":               cid,
        "cluster_id":       cid,
        "phase":            3,
        "type":             "DUPLICATE" if ct == "SINGLE_BEACH" else "SUB_PARTS",
        "p3_cluster_type":  ct,
        "confidence":       conf,
        "reasoning":        result.get("reasoning", ""),
        "canonical_name":   result.get("canonical_name"),
        "satellite_analyzed": True,
        "breaks":           result.get("breaks", []),
        "suggested_groups": result.get("suggested_groups", []),
        "gmaps":            gmaps,
        "points": [
            {"uid": p["uid"], "name": [p["name"]] if p["name"] else [],
             "coordinates": [p["lon"], p["lat"]], "properties": {}}
            for p in pts
        ],
        "primary_uid":     primary,
        "discard_uids":    discards,
        "proposed_action": "MERGE_INTO_PRIMARY" if ct == "SINGLE_BEACH" else "REVIEW_SECTIONS",
        "status":          "auto_approved" if auto else "pending_review",
        "decided_at":      None,
        "created_at":      datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ── Write proposed_changes.json ───────────────────────────────────────────────

def _write(data: dict):
    ch = data["changes"]
    data["meta"] = {
        **data.get("meta", {}),
        "total":          len(ch),
        "pending_review": sum(1 for c in ch if c.get("status") == "pending_review"),
        "auto_approved":  sum(1 for c in ch if c.get("status") == "auto_approved"),
        "approved":       sum(1 for c in ch if c.get("status") == "approved"),
        "rejected":       sum(1 for c in ch if c.get("status") == "rejected"),
        "phase1_total":   sum(1 for c in ch if c.get("phase", 1) == 1),
        "phase2_total":   sum(1 for c in ch if c.get("phase") == 2),
        "phase3_total":   sum(1 for c in ch if c.get("phase") == 3),
        "phase3_pending": sum(1 for c in ch if c.get("phase") == 3 and c.get("status") == "pending_review"),
    }
    PROPOSED.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--reset",   action="store_true")
    args = ap.parse_args()

    if args.reset:
        import shutil
        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR)
            print(f"Deleted {CACHE_DIR}")
        if RATE_FILE.exists():
            RATE_FILE.unlink()
        print("Reset done.")
        return

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OSM_CACHE.mkdir(parents=True, exist_ok=True)
    TILE_CACHE.mkdir(parents=True, exist_ok=True)

    # ── Load points ───────────────────────────────────────────────────────────
    print(f"Loading {GEOJSON}…")
    fc = json.loads(GEOJSON.read_text(encoding="utf-8"))
    all_pts: list[dict] = []
    for feat in fc["features"]:
        props = feat.get("properties") or {}
        uid   = props.get("uid")
        name  = str(props.get("name") or props.get("Name") or "")
        coords = feat["geometry"]["coordinates"]
        if uid:
            all_pts.append({"uid": uid, "name": name,
                             "lat": float(coords[1]), "lon": float(coords[0])})
    print(f"  {len(all_pts):,} features loaded")

    # ── Exclude already-clustered points ──────────────────────────────────────
    if PROPOSED.exists():
        existing = json.loads(PROPOSED.read_text(encoding="utf-8"))
        used: set[str] = set()
        for ch in existing.get("changes", []):
            for p in ch.get("points", []):
                used.add(p.get("uid", ""))
        before  = len(all_pts)
        all_pts = [p for p in all_pts if p["uid"] not in used]
        print(f"  Excluded {before - len(all_pts):,} already in phase-1/2/3 clusters")

    print(f"  {len(all_pts):,} points for phase-3 clustering\n")

    # ── DBSCAN ────────────────────────────────────────────────────────────────
    print(f"Running DBSCAN (eps={EPS_PRIMARY}m)…")
    clusters = build_clusters(all_pts)
    print(f"  {len(clusters)} clusters found ({sum(len(c) for c in clusters):,} points involved)\n")

    if args.dry_run:
        for cl in sorted(clusters, key=lambda c: -len(c)):
            names = " | ".join(dict.fromkeys(p["name"] for p in cl if p["name"]))
            print(f"  {len(cl)} pts  span={_span(cl):.0f}m  {names[:100]}")
        print(f"\nTotal: {len(clusters)} clusters")
        return

    # ── Rescore ───────────────────────────────────────────────────────────────
    if args.rescore:
        data = json.loads(PROPOSED.read_text()) if PROPOSED.exists() else {"changes": [], "meta": {}}
        changed = 0
        for ch in data["changes"]:
            if ch.get("phase") != 3 or ch["status"] in ("approved", "rejected"):
                continue
            should = ch.get("p3_cluster_type") == "SINGLE_BEACH" and ch["confidence"] >= AUTO_APPROVE and len(ch["points"]) == 2
            was    = ch["status"] == "auto_approved"
            if should != was:
                ch["status"] = "auto_approved" if should else "pending_review"
                changed += 1
        _write(data)
        print(f"Rescored: {changed} clusters changed")
        return

    # ── Process ───────────────────────────────────────────────────────────────
    rl          = RateLimiter()
    new_changes: list[dict] = []
    errors      = 0

    for idx, cl in enumerate(clusters):
        cid        = _cid([p["uid"] for p in cl])
        cache_file = CACHE_DIR / f"{cid}.json"
        sp         = _span(cl)
        names_str  = " | ".join(dict.fromkeys(p["name"] for p in cl if p["name"]))
        print(f"[{idx+1}/{len(clusters)}] {cid}  {len(cl)}pts  {sp:.0f}m  {names_str[:80]}")

        cache_valid = False
        if cache_file.exists():
            try:
                content = cache_file.read_text(encoding="utf-8")
                if content.strip():
                    cached        = json.loads(content)
                    nom_by_uid    = cached.pop("_nom", {})
                    places_by_uid = cached.pop("_places", {})
                    result        = cached
                    print(f"  [cached] {result.get('cluster_type')}  conf={result.get('confidence',0):.2f}")
                    cache_valid   = True
            except (json.JSONDecodeError, Exception):
                pass
        if not cache_valid:
            lats  = [p["lat"] for p in cl]
            lons  = [p["lon"] for p in cl]
            clat  = (min(lats) + max(lats)) / 2
            clon  = (min(lons) + max(lons)) / 2

            # Nominatim per point
            print(f"  [nominatim] {len(cl)} pts…", end=" ", flush=True)
            nom_by_uid = {p["uid"]: query_nominatim(p["lat"], p["lon"]) for p in cl}
            print(f"{sum(1 for r in nom_by_uid.values() if r.get('on_beach'))}/{len(cl)} on OSM beach")

            # Places per point
            places_by_uid: dict[str, dict] = {}
            if MAPS_API_KEY:
                print(f"  [places] {len(cl)} pts…", end=" ", flush=True)
                for p in cl:
                    places_by_uid[p["uid"]] = query_places(p["lat"], p["lon"])
                print(f"{sum(1 for r in places_by_uid.values() if r.get('found'))}/{len(cl)} on Google Maps")

            # Images
            print("  [sat]…", end=" ", flush=True)
            sat = fetch_satellite(cl, sp)
            print("ok" if sat else "unavailable")
            print("  [osm]…", end=" ", flush=True)
            osm = fetch_osm_tile(clat, clon, sp)
            print("ok" if osm else "unavailable")

            if not sat and not osm:
                print("  [warn] no images — skipping")
                errors += 1
                result = {"cluster_type": "DISTINCT", "confidence": 0.0,
                          "reasoning": "No images.", "primary_uid": None,
                          "canonical_name": None, "breaks": [], "suggested_groups": []}
            else:
                try:
                    result = analyze(cl, sat, osm, nom_by_uid, places_by_uid, rl)
                    cache_file.write_text(json.dumps(
                        {**result, "_nom": nom_by_uid, "_places": places_by_uid},
                        ensure_ascii=False, indent=2,
                    ))
                    print(f"  → {result.get('cluster_type')}  conf={result.get('confidence',0):.2f}")
                    if result.get("canonical_name"):
                        print(f"     canonical: {result['canonical_name']!r}")
                    print(f"     {result.get('reasoning','')[:110]}")
                except Exception as e:
                    print(f"  [ERROR] {e}")
                    errors += 1
                    continue

        ch = _build_change(cid, cl, result, nom_by_uid, places_by_uid)
        if ch:
            new_changes.append(ch)

    # ── Merge into proposed_changes.json ──────────────────────────────────────
    print(f"\nWriting {len(new_changes)} phase-3 changes…")
    data = json.loads(PROPOSED.read_text(encoding="utf-8")) if PROPOSED.exists() \
           else {"changes": [], "meta": {}}
    existing_ids = {c["id"] for c in data["changes"]}
    added = 0
    for ch in new_changes:
        if ch["id"] not in existing_ids:
            data["changes"].append(ch)
            added += 1
    _write(data)

    pending = sum(1 for c in new_changes if c["status"] == "pending_review")
    auto    = sum(1 for c in new_changes if c["status"] == "auto_approved")
    distinct = len(clusters) - len(new_changes)
    print(f"\nDone. {added} added ({pending} pending, {auto} auto-approved, {distinct} DISTINCT/skipped)")
    if errors:
        print(f"  {errors} errors — re-run to retry")


if __name__ == "__main__":
    main()
