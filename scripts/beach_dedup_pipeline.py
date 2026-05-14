#!/usr/bin/env python3
"""
Beach GeoJSON Deduplication Pipeline
=====================================
Produces data/proposed_changes.json — a diff file for manual review in a custom UI.

Stages
------
1. Spatial clustering  – DBSCAN (haversine, 150 m radius).
                         Isolated points → auto-approved, excluded from AI queue.
2. Satellite fetch     – Google Maps Static API (or Mapbox) tile per cluster, cached locally.
3. Gemini Vision AI    – gemini-2.0-flash analyses each satellite image and decides:
                           DUPLICATE  → same spot, merge candidates
                           SUB_PARTS  → sections of one long beach, group candidates
                           DISTINCT   → separate beaches, keep as-is
4. Output assembly     – writes proposed_changes.json with every cluster's decision,
                         confidence score, reasoning, and proposed action.

Resumability
------------
- data/pipeline_state.json  tracks the rate-limiter state and set of processed cluster IDs.
- data/cluster_results/     holds one JSON file per processed cluster (atomic cache).
- On daily-quota exhaustion the script saves state and exits cleanly; re-run tomorrow.

Environment variables
---------------------
  GEMINI_API_KEY   Required for AI analysis
  MAPS_API_KEY     Google Maps Static API key (preferred for satellite tiles + markers)
  MAPBOX_TOKEN     Mapbox access token (fallback if MAPS_API_KEY is absent)

Usage
-----
  python scripts/beach_dedup_pipeline.py
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sys
import time
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
BASE_DIR     = Path(__file__).parent.parent
DATA_DIR     = BASE_DIR / "data_new"
GEOJSON_PATH = DATA_DIR / "beaches.geojson"
STATE_PATH   = DATA_DIR / "pipeline_state.json"
OUTPUT_PATH  = DATA_DIR / "proposed_changes.json"
RESULTS_DIR  = DATA_DIR / "cluster_results"
TILE_CACHE   = DATA_DIR / "tile_cache"

# ── Tunable constants ─────────────────────────────────────────────────────────
CLUSTER_RADIUS_M     = 150          # haversine clustering radius in metres
GEMINI_MODEL         = "gemini-2.5-flash"
RPM_LIMIT            = 15           # Gemini free-tier: requests per minute
RPD_LIMIT            = 2_000        # Gemini free-tier: requests per day
AUTO_APPROVE_CONF    = 0.92         # confidence threshold for auto-approving DISTINCT
MAX_MARKERS          = 8            # max coloured pins on satellite tile

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  Rate Limiter
# ══════════════════════════════════════════════════════════════════════════════

class QuotaExhaustedError(Exception):
    """Raised when the daily Gemini API quota is reached."""


class RateLimiter:
    """
    Enforces Requests Per Minute (sliding window) and Requests Per Day limits.

    State is serialisable so it survives process restarts.  Wall-clock (time.time)
    is used for the RPM window so saved timestamps stay meaningful across restarts.
    """

    def __init__(self, rpm: int, rpd: int, saved: Dict):
        self.rpm = rpm
        self.rpd = rpd
        today = str(date.today())
        saved_date = saved.get("date", today)

        if saved_date != today:
            # New day — reset counters
            self._daily = 0
            self._window: List[float] = []
        else:
            self._daily = saved.get("daily", 0)
            now = time.time()
            # Discard timestamps outside the current 60-second window
            self._window = [t for t in saved.get("window", []) if now - t < 60.0]

        self._date = today
        if saved_date != today and saved.get("daily", 0) > 0:
            log.info(f"New day ({today}). Daily quota counter reset.")

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def daily_remaining(self) -> int:
        return max(0, self.rpd - self._daily)

    def wait(self):
        """
        Block until a request slot is available (respects RPM).
        Increments both window and daily counter before returning.
        Raises QuotaExhaustedError if the daily limit has been reached.
        """
        if self._daily >= self.rpd:
            raise QuotaExhaustedError(
                f"Daily limit of {self.rpd} requests reached. "
                "Save state and resume tomorrow."
            )

        now = time.time()
        self._window = [t for t in self._window if now - t < 60.0]

        if len(self._window) >= self.rpm:
            sleep_for = 60.0 - (now - self._window[0]) + 0.1
            if sleep_for > 0:
                log.info(f"RPM limit reached ({self.rpm}/min). Sleeping {sleep_for:.1f}s …")
                time.sleep(sleep_for)
            now = time.time()
            self._window = [t for t in self._window if now - t < 60.0]

        self._window.append(now)
        self._daily += 1

    def to_dict(self) -> Dict:
        now = time.time()
        return {
            "date":   self._date,
            "daily":  self._daily,
            "window": [t for t in self._window if now - t < 60.0],
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Satellite tile fetching
# ══════════════════════════════════════════════════════════════════════════════

MARKER_COLORS = ["red", "blue", "yellow", "green", "purple", "orange", "white", "gray"]


def _zoom_for_bbox(coords: List[List[float]]) -> int:
    """
    Return a Google Maps zoom level that fits the cluster bounding box in a
    640×640 tile with ~60 % padding around the points.

    coords: list of [lon, lat]
    """
    if len(coords) <= 1:
        return 18

    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    span = max(max(lons) - min(lons), max(lats) - min(lats))
    if span == 0:
        return 19

    # We want the bbox to fill ≤ 40 % of the 640-px tile so there's context.
    # px/degree at zoom z  =  256 × 2^z / 360
    # target: span × (256 × 2^z / 360) ≤ 640 × 0.40
    # → z ≤ log2( 640 × 0.40 × 360 / (256 × span) )
    z = math.log2((640 * 0.40 * 360) / (256 * span))
    return max(10, min(19, int(z)))


def fetch_satellite(
    cluster_id: str,
    coords: List[List[float]],   # [lon, lat] per point
    maps_key: Optional[str],
    mapbox_token: Optional[str],
) -> Optional[bytes]:
    """Return JPEG/PNG bytes for the satellite tile, using a local cache."""
    TILE_CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = TILE_CACHE / f"{cluster_id}.jpg"
    if cache_file.exists():
        return cache_file.read_bytes()

    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)
    zoom = _zoom_for_bbox(coords)

    data: Optional[bytes] = None

    if maps_key:
        params: List[Tuple[str, str]] = [
            ("center",  f"{center_lat:.6f},{center_lon:.6f}"),
            ("zoom",    str(zoom)),
            ("size",    "640x640"),
            ("maptype", "satellite"),
            ("key",     maps_key),
        ]
        for i, (lon, lat) in enumerate(coords[:MAX_MARKERS]):
            color = MARKER_COLORS[i % len(MARKER_COLORS)]
            params.append(("markers", f"color:{color}|{lat:.6f},{lon:.6f}"))

        url = "https://maps.googleapis.com/maps/api/staticmap?" + urllib.parse.urlencode(params)
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            # Google returns a 1×1 error image if the key/quota is bad — check content type
            if "image" in r.headers.get("Content-Type", ""):
                data = r.content
            else:
                log.warning(f"Google Maps returned non-image for {cluster_id}: {r.text[:200]}")
        except Exception as exc:
            log.warning(f"Google Maps tile failed ({cluster_id}): {exc}")

    elif mapbox_token:
        url = (
            f"https://api.mapbox.com/styles/v1/mapbox/satellite-v9/static/"
            f"{center_lon:.6f},{center_lat:.6f},{zoom},0/640x640"
            f"?access_token={mapbox_token}"
        )
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            if "image" in r.headers.get("Content-Type", ""):
                data = r.content
            else:
                log.warning(f"Mapbox returned non-image for {cluster_id}: {r.text[:200]}")
        except Exception as exc:
            log.warning(f"Mapbox tile failed ({cluster_id}): {exc}")

    if data:
        cache_file.write_bytes(data)
    return data


# ══════════════════════════════════════════════════════════════════════════════
#  Gemini Vision analysis
# ══════════════════════════════════════════════════════════════════════════════

_ANALYSIS_PROMPT = """\
You are a geographic data analyst helping deduplicate a beach location database for Greece.

The satellite image shows {n} marked beach point(s), all within {radius} metres of each other.

Point coordinates [longitude, latitude]:
{coords}

Point names (may be in Greek or empty):
{names}

Analyse the satellite image carefully and classify the spatial relationship:

  DUPLICATE  – The points mark the exact same physical beach spot.
               One or more are redundant and should be merged into a single record.

  SUB_PARTS  – The points are distinct sections of one large, continuous beach
               (e.g. "North end" and "South end" of the same sand strip).
               They should be grouped under a shared parent record.

  DISTINCT   – The points represent genuinely separate beaches divided by a clear
               natural barrier visible in the image (cliff, rocky headland, river
               mouth, harbour wall, etc.).  They must remain as independent records.

Return ONLY valid JSON — no markdown fences, no prose outside the object:
{{
  "decision":   "DUPLICATE" | "SUB_PARTS" | "DISTINCT",
  "confidence": <float 0.0 – 1.0>,
  "reasoning":  "<1–3 sentences citing specific visual features you observed>"
}}
"""


def analyze_cluster(
    cluster_id: str,
    points: List[Dict],
    image_bytes: Optional[bytes],
    client,               # google.genai.Client
    rate_limiter: RateLimiter,
) -> Dict:
    """
    Call Gemini Vision with the satellite image and a structured prompt.
    Returns a dict with: decision, confidence, reasoning, satellite_analyzed, error.
    """
    coords = [p["coordinates"] for p in points]
    names  = [p["name"] for p in points]

    prompt = _ANALYSIS_PROMPT.format(
        n=len(points),
        radius=CLUSTER_RADIUS_M,
        coords=json.dumps(coords, ensure_ascii=False),
        names=json.dumps(names, ensure_ascii=False),
    )

    contents: list = []
    if image_bytes:
        contents.append(
            genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
        )
    contents.append(prompt)

    # This may block (RPM sleep) or raise QuotaExhaustedError
    rate_limiter.wait()

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        text = (response.text or "").strip()
        # Strip markdown code fences if the model wraps output anyway
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()

        result = json.loads(text)
        decision = result.get("decision", "DISTINCT")
        if decision not in ("DUPLICATE", "SUB_PARTS", "DISTINCT"):
            log.warning(f"Unknown decision '{decision}' for {cluster_id}; defaulting to DISTINCT.")
            decision = "DISTINCT"

        return {
            "decision":           decision,
            "confidence":         float(result.get("confidence", 0.5)),
            "reasoning":          result.get("reasoning", ""),
            "satellite_analyzed": image_bytes is not None,
            "error":              None,
        }

    except QuotaExhaustedError:
        raise

    except Exception as exc:
        log.warning(f"Gemini error for {cluster_id}: {exc}")
        return {
            "decision":           "UNKNOWN",
            "confidence":         0.0,
            "reasoning":          f"AI analysis failed: {exc}",
            "satellite_analyzed": image_bytes is not None,
            "error":              str(exc),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Utility helpers
# ══════════════════════════════════════════════════════════════════════════════

def stable_cluster_id(uids: List[str]) -> str:
    """
    Deterministic cluster ID: MD5 of sorted UIDs.
    Stays the same across runs as long as the input GeoJSON doesn't change.
    """
    key = ",".join(sorted(uids))
    return "cl_" + hashlib.md5(key.encode()).hexdigest()[:12]


def feature_to_point(f: Dict) -> Dict:
    return {
        "uid":         f["properties"]["uid"],
        "name":        f["properties"].get("name", []),
        "coordinates": f["geometry"]["coordinates"],   # [lon, lat]
        "properties":  f["properties"],
    }


def pick_primary_uid(points: List[Dict]) -> str:
    """Heuristic: prefer the point with the most names and real source IDs."""
    def score(p: Dict) -> Tuple:
        props = p["properties"]
        names      = props.get("name", [])
        sources    = props.get("source", [])
        source_ids = props.get("source_id", [])
        real_ids   = sum(1 for s in source_ids if "[<NA>]" not in str(s))
        return (len(names), real_ids, len(sources))
    return max(points, key=score)["uid"]


def build_change(cluster_id: str, points: List[Dict], analysis: Dict) -> Dict:
    decision   = analysis["decision"]
    confidence = analysis["confidence"]

    if decision == "DUPLICATE":
        primary_uid = pick_primary_uid(points)
        discard_uids = [p["uid"] for p in points if p["uid"] != primary_uid]
        action = "MERGE_INTO_PRIMARY"
    elif decision == "SUB_PARTS":
        primary_uid  = pick_primary_uid(points)
        discard_uids = []
        action = "CREATE_HIERARCHY"
    else:  # DISTINCT or UNKNOWN
        primary_uid  = None
        discard_uids = []
        action = "KEEP_ALL"

    # Auto-approve high-confidence decisions that are safe without human eyes:
    #   DISTINCT  – keeping separate beaches is non-destructive
    #   DUPLICATE – high-confidence merges on 2-point clusters only (safer subset)
    auto_approve = (
        (decision == "DISTINCT"   and confidence >= AUTO_APPROVE_CONF) or
        (decision == "DUPLICATE"  and confidence >= AUTO_APPROVE_CONF and len(points) == 2)
    )
    status = "auto_approved" if auto_approve else "pending_review"

    return {
        "id":                 cluster_id,
        "type":               decision,
        "confidence":         confidence,
        "reasoning":          analysis["reasoning"],
        "satellite_analyzed": analysis["satellite_analyzed"],
        "points":             points,
        "proposed_action":    action,
        "primary_uid":        primary_uid,
        "discard_uids":       discard_uids,
        "status":             status,
        "created_at":         datetime.utcnow().isoformat() + "Z",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  State / cache helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> Dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {}


def save_state(rate_limiter: RateLimiter, processed_ids: set):
    state = {
        "rate_limiter":        rate_limiter.to_dict(),
        "processed_cluster_ids": sorted(processed_ids),
    }
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def load_cached_result(cluster_id: str) -> Optional[Dict]:
    p = RESULTS_DIR / f"{cluster_id}.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_cached_result(change: Dict):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    p = RESULTS_DIR / f"{change['id']}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(change, f, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
#  Output assembly
# ══════════════════════════════════════════════════════════════════════════════

def assemble_output(cluster_changes: List[Dict], total_features: int, isolated_count: int):
    """Write proposed_changes.json — the UI-facing diff file."""
    pending      = [c for c in cluster_changes if c["status"] == "pending_review"]
    auto_done    = [c for c in cluster_changes if c["status"] == "auto_approved"]
    unanalyzed   = [c for c in cluster_changes if c["type"] == "UNKNOWN"]
    duplicates   = [c for c in cluster_changes if c["type"] == "DUPLICATE"]
    sub_parts    = [c for c in cluster_changes if c["type"] == "SUB_PARTS"]
    distinct     = [c for c in cluster_changes if c["type"] == "DISTINCT"]

    output = {
        "meta": {
            "generated_at":        datetime.utcnow().isoformat() + "Z",
            "pipeline_version":    "1.0",
            "cluster_radius_m":    CLUSTER_RADIUS_M,
            "gemini_model":        GEMINI_MODEL,
            "total_input_features": total_features,
            "total_isolated_safe": isolated_count,
            "total_cluster_changes": len(cluster_changes),
            "by_type": {
                "DUPLICATE":  len(duplicates),
                "SUB_PARTS":  len(sub_parts),
                "DISTINCT":   len(distinct),
                "UNKNOWN":    len(unanalyzed),
            },
            "by_status": {
                "pending_review": len(pending),
                "auto_approved":  len(auto_done),
            },
        },
        # Only multi-point cluster changes are included here.
        # Isolated points are excluded (they need no action) — their count is in meta.
        "changes": cluster_changes,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log.info(f"━━ Output → {OUTPUT_PATH}")
    log.info(f"   Isolated / safe      : {isolated_count:>6}")
    log.info(f"   Multi-pt clusters    : {len(cluster_changes):>6}")
    log.info(f"     DUPLICATE          : {len(duplicates):>6}")
    log.info(f"     SUB_PARTS          : {len(sub_parts):>6}")
    log.info(f"     DISTINCT           : {len(distinct):>6}")
    log.info(f"     UNKNOWN            : {len(unanalyzed):>6}")
    log.info(f"   Pending review       : {len(pending):>6}")
    log.info(f"   Auto-approved        : {len(auto_done):>6}")


# ══════════════════════════════════════════════════════════════════════════════
#  Main pipeline
# ══════════════════════════════════════════════════════════════════════════════

def rescore():
    """
    Re-apply the current auto-approve thresholds to all cached cluster results
    without making any new API calls.  Useful after tuning AUTO_APPROVE_CONF.
    """
    log.info("Rescore mode: re-evaluating cached results with current thresholds …")
    if not RESULTS_DIR.exists():
        log.error(f"{RESULTS_DIR} not found — run the full pipeline first.")
        return

    updated = 0
    for p in RESULTS_DIR.glob("cl_*.json"):
        with open(p, "r", encoding="utf-8") as f:
            change = json.load(f)

        if change.get("type") in ("ISOLATED", "UNKNOWN"):
            continue

        decision   = change["type"]
        confidence = change["confidence"]
        points     = change["points"]

        auto_approve = (
            (decision == "DISTINCT"  and confidence >= AUTO_APPROVE_CONF) or
            (decision == "DUPLICATE" and confidence >= AUTO_APPROVE_CONF and len(points) == 2)
        )
        new_status = "auto_approved" if auto_approve else "pending_review"

        if change["status"] != new_status and change["status"] not in ("approved", "rejected"):
            change["status"] = new_status
            with open(p, "w", encoding="utf-8") as f:
                json.dump(change, f, ensure_ascii=False)
            updated += 1

    log.info(f"Rescore done — {updated} cluster(s) updated.")


def main():
    if "--rescore" in sys.argv:
        rescore()
        # Still rebuild the output file from all cached results
        with open(GEOJSON_PATH, "r", encoding="utf-8") as f:
            features = json.load(f)["features"]
        coords_rad = np.array([
            [math.radians(f["geometry"]["coordinates"][1]),
             math.radians(f["geometry"]["coordinates"][0])]
            for f in features
        ])
        labels = DBSCAN(eps=CLUSTER_RADIUS_M / 6_371_000.0, min_samples=1,
                        algorithm="ball_tree", metric="haversine").fit(coords_rad).labels_
        cluster_map: Dict[int, List[int]] = {}
        for i, lbl in enumerate(labels):
            cluster_map.setdefault(lbl, []).append(i)
        multis = {k: v for k, v in cluster_map.items() if len(v) > 1}
        singles = {k: v for k, v in cluster_map.items() if len(v) == 1}
        sorted_multis = sorted(multis.items(), key=lambda x: len(x[1]), reverse=True)
        cluster_changes: List[Dict] = []
        seen_ids: set = set()
        for lbl, indices in sorted_multis:
            uids = [features[i]["properties"]["uid"] for i in indices]
            cid = stable_cluster_id(uids)
            if cid in seen_ids:
                continue
            cached = load_cached_result(cid)
            if cached:
                cluster_changes.append(cached)
                seen_ids.add(cid)
        assemble_output(cluster_changes, len(features), len(singles))
        return

    gemini_key   = os.environ.get("GEMINI_API_KEY", "")
    maps_key     = os.environ.get("MAPS_API_KEY", "")
    mapbox_token = os.environ.get("MAPBOX_TOKEN", "")

    if not gemini_key:
        log.warning("GEMINI_API_KEY not set — AI analysis will be skipped (clusters marked UNKNOWN).")
    if not maps_key and not mapbox_token:
        log.warning("MAPS_API_KEY / MAPBOX_TOKEN not set — satellite images will be skipped.")

    # ── Load GeoJSON ──────────────────────────────────────────────────────────
    log.info(f"Loading {GEOJSON_PATH} …")
    with open(GEOJSON_PATH, "r", encoding="utf-8") as f:
        geojson = json.load(f)
    features: List[Dict] = geojson["features"]
    log.info(f"Loaded {len(features):,} features.")

    # ── Load persistent state ─────────────────────────────────────────────────
    state = load_state()
    rate_limiter  = RateLimiter(RPM_LIMIT, RPD_LIMIT, state.get("rate_limiter", {}))
    processed_ids: set = set(state.get("processed_cluster_ids", []))
    if processed_ids:
        log.info(
            f"Resuming: {len(processed_ids)} clusters already processed, "
            f"{rate_limiter.daily_remaining} API calls remaining today."
        )

    # ── Stage 1: Spatial clustering ───────────────────────────────────────────
    log.info(f"Spatial clustering (DBSCAN, haversine, radius = {CLUSTER_RADIUS_M} m) …")
    # DBSCAN requires coordinates as [lat, lon] in radians for haversine metric
    coords_rad = np.array([
        [math.radians(f["geometry"]["coordinates"][1]),   # lat
         math.radians(f["geometry"]["coordinates"][0])]   # lon
        for f in features
    ])
    eps = CLUSTER_RADIUS_M / 6_371_000.0   # convert metres → radians

    labels = DBSCAN(
        eps=eps, min_samples=1, algorithm="ball_tree", metric="haversine"
    ).fit(coords_rad).labels_

    cluster_map: Dict[int, List[int]] = {}
    for i, lbl in enumerate(labels):
        cluster_map.setdefault(lbl, []).append(i)

    singles = {k: v for k, v in cluster_map.items() if len(v) == 1}
    multis  = {k: v for k, v in cluster_map.items() if len(v) > 1}
    log.info(
        f"Clustering done: {len(singles):,} isolated points, "
        f"{len(multis):,} multi-point clusters (2+ pts)."
    )

    # ── Stage 2+: Process clusters ────────────────────────────────────────────
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    gemini_client = None
    if gemini_key and GENAI_AVAILABLE:
        gemini_client = genai.Client(api_key=gemini_key)

    # -- Isolated points (auto-approved, no AI needed) -------------------------
    isolated_count = 0
    for lbl, indices in singles.items():
        f = features[indices[0]]
        pt = feature_to_point(f)
        cluster_id = stable_cluster_id([pt["uid"]])

        if cluster_id in processed_ids:
            isolated_count += 1
            continue

        change = {
            "id":                 cluster_id,
            "type":               "ISOLATED",
            "confidence":         1.0,
            "reasoning":          "No other beach point within the clustering radius.",
            "satellite_analyzed": False,
            "points":             [pt],
            "proposed_action":    "NO_CHANGE",
            "primary_uid":        None,
            "discard_uids":       [],
            "status":             "auto_approved",
            "created_at":         datetime.utcnow().isoformat() + "Z",
        }
        save_cached_result(change)
        processed_ids.add(cluster_id)
        isolated_count += 1

    log.info(f"Isolated points processed: {isolated_count:,}")

    # -- Multi-point clusters --------------------------------------------------
    sorted_multis = sorted(multis.items(), key=lambda x: len(x[1]), reverse=True)

    to_process = [
        (lbl, idxs) for lbl, idxs in sorted_multis
        if stable_cluster_id(
            [features[i]["properties"]["uid"] for i in idxs]
        ) not in processed_ids
    ]

    log.info(
        f"{len(to_process):,} multi-point clusters to process "
        f"({len(sorted_multis) - len(to_process)} already cached). "
        f"Daily API budget remaining: {rate_limiter.daily_remaining}."
    )

    quota_hit = False
    processed_this_run = 0

    try:
        for lbl, indices in to_process:
            cluster_features = [features[i] for i in indices]
            points      = [feature_to_point(f) for f in cluster_features]
            cluster_id  = stable_cluster_id([p["uid"] for p in points])
            coords      = [p["coordinates"] for p in points]   # [lon, lat]

            # Double-check result cache (in case state file was lost/corrupted)
            cached = load_cached_result(cluster_id)
            if cached:
                processed_ids.add(cluster_id)
                continue

            # Satellite tile
            image_bytes: Optional[bytes] = None
            if maps_key or mapbox_token:
                image_bytes = fetch_satellite(
                    cluster_id, coords,
                    maps_key or None,
                    mapbox_token or None,
                )

            # Gemini Vision analysis
            if gemini_client and rate_limiter.daily_remaining > 0:
                analysis = analyze_cluster(
                    cluster_id, points, image_bytes, gemini_client, rate_limiter
                )
            else:
                reason = (
                    "AI analysis skipped: no GEMINI_API_KEY."
                    if not gemini_client
                    else "AI analysis skipped: daily quota exhausted."
                )
                analysis = {
                    "decision":           "UNKNOWN",
                    "confidence":         0.0,
                    "reasoning":          reason,
                    "satellite_analyzed": image_bytes is not None,
                    "error":              None,
                }

            change = build_change(cluster_id, points, analysis)
            save_cached_result(change)
            processed_ids.add(cluster_id)
            processed_this_run += 1

            log.info(
                f"[{cluster_id}] {len(points)} pts → {analysis['decision']} "
                f"(conf={analysis['confidence']:.2f})"
                f"  budget_left={rate_limiter.daily_remaining}"
            )

            # Persist state after every cluster so progress is never lost
            save_state(rate_limiter, processed_ids)

    except QuotaExhaustedError:
        quota_hit = True
        save_state(rate_limiter, processed_ids)
        log.warning(
            "━━ Daily API quota exhausted. "
            f"Processed {processed_this_run} clusters this run. "
            "State saved — re-run tomorrow to continue."
        )

    # ── Assemble proposed_changes.json ────────────────────────────────────────
    # Collect all multi-cluster results from cache (includes previous runs)
    cluster_changes: List[Dict] = []
    seen_ids: set = set()

    for lbl, indices in sorted_multis:
        cluster_features = [features[i] for i in indices]
        uids = [f["properties"]["uid"] for f in cluster_features]
        cluster_id = stable_cluster_id(uids)
        if cluster_id in seen_ids:
            continue
        cached = load_cached_result(cluster_id)
        if cached:
            cluster_changes.append(cached)
            seen_ids.add(cluster_id)

    assemble_output(cluster_changes, len(features), isolated_count)
    save_state(rate_limiter, processed_ids)

    if quota_hit:
        log.info("Re-run the script tomorrow to process remaining clusters.")
    else:
        remaining = len(sorted_multis) - len(cluster_changes)
        if remaining:
            log.info(f"{remaining} clusters still pending (likely hit quota earlier).")
        else:
            log.info("Pipeline complete — all clusters processed.")


if __name__ == "__main__":
    main()
