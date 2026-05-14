#!/usr/bin/env python3
"""
Phase 4 — cross-phase geographic reconciliation.

After Phases 1-3 each process disjoint subsets of beach points, adjacent clusters
from different phases may describe the same physical beach. This phase finds those
adjacencies and produces a single unified review card.

Only creates Phase 4 entries when the verdict is SINGLE_BEACH or LONG_SECTIONS.
DISTINCT verdicts leave the original phase decisions untouched.

Usage
-----
  python scripts/beach_phase4_pipeline.py            # normal run
  python scripts/beach_phase4_pipeline.py --dry-run  # show groups, no API
  python scripts/beach_phase4_pipeline.py --reset    # undo all phase-4 changes
  python scripts/beach_phase4_pipeline.py --limit 20 # process first N groups
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from pathlib import Path

import numpy as np
import requests
from sklearn.neighbors import BallTree

try:
    from google import genai
    from google.genai import types as genai_types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT      = Path(__file__).parent.parent
DATA      = ROOT / "data_new"
PROPOSED  = DATA / "proposed_changes.json"
CACHE_DIR = DATA / "cluster_results_p4"
TILE_CACHE = DATA / "tile_cache_p4"

# ── Config ────────────────────────────────────────────────────────────────────
GEMINI_MODEL     = "gemini-2.5-flash"
RECONCILE_RADIUS = 1_000   # metres — connect changes if any points are this close
AUTO_APPROVE_CONF = 0.95   # auto-approve SINGLE_BEACH at this confidence

MAPBOX_TOKEN  = os.environ.get("MAPBOX_TOKEN", "")
MAPS_API_KEY  = os.environ.get("MAPS_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

_MB_COLORS = ["f00", "00f", "0a0", "ff0", "80f", "f80", "0ff", "888",
              "f66", "66f", "6f6", "ff6", "c0f", "f60", "0cf"]


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
    dists = [
        _hav(a["lat"], a["lon"], b["lat"], b["lon"])
        for i, a in enumerate(pts)
        for b in pts[i + 1:]
    ]
    return max(dists) if dists else 0.0


def _pt_coords(pt: dict) -> tuple[float, float]:
    if "lat" in pt:
        return pt["lat"], pt["lon"]
    coords = pt.get("coordinates", [0, 0])
    return coords[1], coords[0]


# ── Union-Find ────────────────────────────────────────────────────────────────

class UF:
    def __init__(self, items):
        self.p = {x: x for x in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)

    def groups(self) -> list[list]:
        from collections import defaultdict
        g: dict[str, list] = defaultdict(list)
        for x in self.p:
            g[self.find(x)].append(x)
        return [v for v in g.values() if len(v) >= 2]


# ── Satellite image ───────────────────────────────────────────────────────────

def fetch_satellite(pts: list[dict]) -> bytes | None:
    if not MAPBOX_TOKEN:
        return None
    TILE_CACHE.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5("|".join(pt["uid"] for pt in pts).encode()).hexdigest()[:16]
    cache_file = TILE_CACHE / f"{cache_key}.jpg"
    if cache_file.exists():
        return cache_file.read_bytes()
    try:
        markers = ",".join(
            f"pin-m+{_MB_COLORS[i % len(_MB_COLORS)]}({pt['lon']:.6f},{pt['lat']:.6f})"
            for i, pt in enumerate(pts[:15])
        )
        url = (
            f"https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/static/"
            f"{markers}/auto/800x500@2x"
            f"?padding=80&access_token={MAPBOX_TOKEN}"
        )
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            cache_file.write_bytes(r.content)
            return r.content
    except Exception as e:
        print(f"  [mapbox] {e}")
    return None


# ── Gemini reconciliation ─────────────────────────────────────────────────────

def reconcile(group_changes: list[dict], combined_pts: list[dict], sat: bytes | None) -> dict:
    if not GENAI_AVAILABLE or not GEMINI_API_KEY:
        return {"error": "Gemini not available"}

    client = genai.Client(api_key=GEMINI_API_KEY)
    uid_to_label = {pt["uid"]: chr(65 + i) for i, pt in enumerate(combined_pts[:26])}
    span_m = _span(combined_pts)

    cluster_blocks = ""
    for ch in group_changes:
        phase   = ch.get("phase", 1)
        ctype   = ch.get("p3_cluster_type") or ch.get("p2_cluster_type") or ch.get("type", "?")
        conf    = ch.get("confidence", 0)
        reason  = (ch.get("reasoning") or "")[:350]
        primary = ch.get("primary_uid")

        cluster_blocks += f"\n── Phase {phase} · {ch['id']} · {ctype} (conf={conf:.2f}) ──\n"
        cluster_blocks += f"Reasoning: {reason}\n"
        if ch.get("canonical_name"):
            cluster_blocks += f"Canonical name: {ch['canonical_name']!r}\n"
        cluster_blocks += "Points:\n"

        for pt in ch["points"]:
            label = uid_to_label.get(pt["uid"], "?")
            lat, lon = _pt_coords(pt)
            names = pt.get("name", [])
            if isinstance(names, list):
                name_str = ", ".join(str(n) for n in names[:3] if n)
            else:
                name_str = str(names)

            if phase in (1, 2) and primary:
                hint = " [keep — primary]" if pt["uid"] == primary else " [delete — duplicate]"
            else:
                hint = ""

            gmaps = ch.get("gmaps", {}).get(pt["uid"], {})
            gstr = ""
            if gmaps.get("found"):
                gstr = f" | Google: '{gmaps.get('name')}' ⭐{gmaps.get('rating')} ({gmaps.get('user_ratings')} reviews)"

            cluster_blocks += f"  {label}) {pt['uid']!r}  {name_str!r}  {lat:.5f}°N {lon:.5f}°E{hint}{gstr}\n"

        if ch.get("suggested_sections"):
            cluster_blocks += f"  Prior sections: {json.dumps(ch['suggested_sections'], ensure_ascii=False)}\n"

    all_uid_summary = "\n".join(
        f"  {uid_to_label.get(pt['uid'], '?')}) {pt['uid']!r}"
        for pt in combined_pts
    )

    prompt = f"""You are reconciling {len(group_changes)} overlapping beach cluster decisions from different analysis phases.
These clusters were analyzed separately but their points lie on the same ~{span_m:.0f}m stretch of coastline.

{cluster_blocks}
ALL {len(combined_pts)} POINTS (labels A–{chr(64+len(combined_pts))}):
{all_uid_summary}

The satellite image shows all {len(combined_pts)} labeled points together.

RULES:
- Any point already marked "[delete — duplicate]" by Phase 1/2 MUST keep action "delete".
- For remaining points decide: are they ONE beach, NAMED SECTIONS of a long beach, or DISTINCT separate beaches?
- SINGLE_BEACH → all non-deleted points are the same beach, one primary point.
- LONG_SECTIONS → points are on a continuous beach but have distinct local names/areas — keep all, group into 2-5 sections.
- DISTINCT → points are genuinely separate beaches separated by headlands/rocks/rivers/roads.

Return ONLY valid JSON (no markdown fences):
{{
  "unified_type": "SINGLE_BEACH" | "LONG_SECTIONS" | "DISTINCT",
  "confidence": 0.85,
  "canonical_name": "best overall name or null",
  "reasoning": "2-3 sentences",
  "action_per_uid": {{
    "uid": "keep_primary" | "keep" | "delete"
  }},
  "suggested_sections": [
    {{"label": "A", "suggested_name": "name", "uids": ["uid1"]}}
  ],
  "breaks": [
    {{"between_uids": ["uid_north", "uid_south"], "type": "headland|river|road|name_change", "description": "short"}}
  ]
}}"""

    parts: list = [prompt]
    if sat:
        parts.append(genai_types.Part.from_bytes(data=sat, mime_type="image/jpeg"))

    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=parts,
            config=genai_types.GenerateContentConfig(temperature=0.1),
        )
        text = resp.text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        return json.loads(text)
    except Exception as e:
        return {"error": str(e)}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset",   action="store_true")
    ap.add_argument("--limit",   type=int, default=0)
    args = ap.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ── Reset ────────────────────────────────────────────────────────────────
    if args.reset:
        data = json.loads(PROPOSED.read_text(encoding="utf-8"))
        removed = [c for c in data["changes"] if c.get("phase") == 4]
        data["changes"] = [c for c in data["changes"] if c.get("phase") != 4]
        for c in data["changes"]:
            if c.get("status") == "superseded":
                c.pop("status", None)
                c.pop("superseded_by", None)
        data.get("meta", {}).pop("phase4_total", None)
        data.get("meta", {}).pop("phase4_pending", None)
        PROPOSED.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for f in CACHE_DIR.glob("*.json"):
            f.unlink()
        print(f"Reset: removed {len(removed)} phase-4 changes, un-superseded originals.")
        return

    # ── Load ─────────────────────────────────────────────────────────────────
    data = json.loads(PROPOSED.read_text(encoding="utf-8"))
    changes = data["changes"]

    active = [
        c for c in changes
        if c.get("status") not in ("superseded", "rejected")
        and c.get("phase") != 4
    ]
    print(f"Loaded {len(active)} active changes (phases 1-3)")

    # ── Flat point list ───────────────────────────────────────────────────────
    flat: list[tuple[float, float, str]] = []  # (lat, lon, change_id)
    change_map = {c["id"]: c for c in active}

    for c in active:
        for pt in c["points"]:
            lat, lon = _pt_coords(pt)
            flat.append((lat, lon, c["id"]))

    print(f"  {len(flat)} total points")

    # ── BallTree proximity ────────────────────────────────────────────────────
    coords_rad = np.radians([[p[0], p[1]] for p in flat])
    tree = BallTree(coords_rad, metric="haversine")
    radius_rad = RECONCILE_RADIUS / 6_371_000.0
    neighbors = tree.query_radius(coords_rad, r=radius_rad)

    adjacent_pairs: set[tuple[str, str]] = set()
    for i, nbrs in enumerate(neighbors):
        cid_i  = flat[i][2]
        phase_i = change_map[cid_i].get("phase", 1)
        for j in nbrs:
            cid_j  = flat[j][2]
            if cid_i == cid_j:
                continue
            phase_j = change_map[cid_j].get("phase", 1)
            # Skip Phase1+Phase1 pairs — two tight 150m clusters 1km apart are almost
            # certainly different beaches; Phase 1 didn't group them for good reason.
            if phase_i == 1 and phase_j == 1:
                continue
            adjacent_pairs.add(tuple(sorted([cid_i, cid_j])))  # type: ignore[arg-type]

    print(f"  {len(adjacent_pairs)} cross-phase adjacent pairs")

    # ── Union-Find grouping ───────────────────────────────────────────────────
    uf = UF([c["id"] for c in active])
    for a, b in adjacent_pairs:
        uf.union(a, b)

    groups = uf.groups()

    def multi_phase(group_ids: list[str]) -> bool:
        phases = {change_map[cid].get("phase", 1) for cid in group_ids}
        return len(phases) > 1

    groups = [g for g in groups if multi_phase(g)]
    print(f"  {len(groups)} multi-phase groups need reconciliation")

    if args.dry_run:
        for i, g in enumerate(groups[:30]):
            chs     = [change_map[cid] for cid in g]
            phases  = sorted({c.get("phase", 1) for c in chs})
            n_pts   = sum(len(c["points"]) for c in chs)
            samples = []
            for c in chs:
                for pt in c["points"][:2]:
                    n = pt.get("name", [])
                    if isinstance(n, list) and n:
                        samples.append(str(n[0]))
            print(f"[{i+1}] phases={phases}  {len(g)} clusters  {n_pts}pts  {samples[:4]}")
        if len(groups) > 30:
            print(f"  … and {len(groups)-30} more")
        print(f"\nTotal: {len(groups)} groups would be reconciled.")
        return

    if args.limit:
        groups = groups[:args.limit]

    # ── Already-processed p4 ids ──────────────────────────────────────────────
    existing_p4_ids = {c["id"] for c in changes if c.get("phase") == 4}

    new_entries: list[dict] = []
    superseded_map: dict[str, str] = {}  # original_id → p4_id
    errors = 0
    skipped_distinct = 0

    for idx, group_ids in enumerate(groups):
        group_ids = list(group_ids)
        group_chs = [change_map[cid] for cid in group_ids]

        # Deduplicate points across source changes
        seen_uids: set[str] = set()
        combined_pts: list[dict] = []
        for c in group_chs:
            for pt in c["points"]:
                if pt["uid"] not in seen_uids:
                    seen_uids.add(pt["uid"])
                    lat, lon = _pt_coords(pt)
                    combined_pts.append({
                        "uid":        pt["uid"],
                        "name":       pt.get("name", []),
                        "lat":        lat,
                        "lon":        lon,
                        "coordinates": [lon, lat],
                        "properties": pt.get("properties", {}),
                    })

        phases   = sorted({c.get("phase", 1) for c in group_chs})
        span_m   = _span(combined_pts)
        g_hash   = hashlib.md5("|".join(sorted(group_ids)).encode()).hexdigest()[:12]
        p4_id    = f"p4_{g_hash}"

        name_samples = []
        for pt in combined_pts[:4]:
            n = pt["name"]
            first = (n[0] if isinstance(n, list) and n else str(n)) if n else ""
            if first:
                name_samples.append(first)

        print(f"\n[{idx+1}/{len(groups)}] {p4_id}  phases={phases}  {len(group_chs)} clusters  {len(combined_pts)}pts  {span_m:.0f}m")
        print(f"  Sources: {group_ids}")
        if name_samples:
            print(f"  Names: {name_samples}")

        # Check cache
        cache_file = CACHE_DIR / f"{p4_id}.json"
        result: dict | None = None
        if cache_file.exists():
            try:
                content = cache_file.read_text(encoding="utf-8")
                if content.strip():
                    result = json.loads(content)
                    print(f"  [cached] {result.get('unified_type')}  conf={result.get('confidence', 0):.2f}")
            except (json.JSONDecodeError, Exception):
                result = None

        if result is None:
            # Fetch satellite image
            print("  [sat]…", end=" ", flush=True)
            sat = fetch_satellite(combined_pts)
            print("ok" if sat else "unavailable")

            # Gemini call
            print("  [gemini]…", end=" ", flush=True)
            result = reconcile(group_chs, combined_pts, sat)

            if "error" in result:
                print(f"ERROR: {result['error'][:100]}")
                errors += 1
                continue

            print(f"{result.get('unified_type')}  conf={result.get('confidence', 0):.2f}")
            if result.get("reasoning"):
                print(f"  {result['reasoning'][:120]}")

            cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        # Skip DISTINCT — original decisions are correct, no need to supersede
        if result.get("unified_type") == "DISTINCT":
            skipped_distinct += 1
            continue

        # Already applied?
        if p4_id in existing_p4_ids:
            print("  [already applied]")
            continue

        # Build Phase 4 entry
        utype  = result.get("unified_type", "SINGLE_BEACH")
        conf   = result.get("confidence", 0.0)
        apu    = result.get("action_per_uid", {})

        # Determine primary uid and discards from action_per_uid
        primary_uid  = next((uid for uid, act in apu.items() if act == "keep_primary"), None)
        if primary_uid is None and utype == "SINGLE_BEACH":
            keeps = [uid for uid, act in apu.items() if act in ("keep", "keep_primary")]
            primary_uid = keeps[0] if keeps else combined_pts[0]["uid"]

        if utype == "SINGLE_BEACH":
            # For a single beach ALL non-primary points should be merged/deleted —
            # including Phase-3 "keep" points that are the same beach as primary.
            discard_uids = [pt["uid"] for pt in combined_pts if pt["uid"] != primary_uid]
        else:
            # LONG_SECTIONS: only remove explicit duplicates from Phase 1/2.
            # The "keep" section points stay on the map as separate entries.
            discard_uids = [uid for uid, act in apu.items() if act == "delete"]
            # Need a primary so MapEditor doesn't bail; use first non-deleted point.
            if primary_uid is None and discard_uids:
                primary_uid = next(
                    (pt["uid"] for pt in combined_pts if pt["uid"] not in discard_uids),
                    None,
                )

        proposed_action = (
            "MERGE_INTO_PRIMARY" if utype == "SINGLE_BEACH"
            else "REVIEW_SECTIONS"
        )

        status = (
            "auto_approved"
            if utype == "SINGLE_BEACH" and conf >= AUTO_APPROVE_CONF and len(combined_pts) <= 4
            else "pending_review"
        )

        entry: dict = {
            "id":                p4_id,
            "phase":             4,
            "type":              "DUPLICATE" if utype == "SINGLE_BEACH" else "SUB_PARTS",
            "p4_unified_type":   utype,
            "confidence":        conf,
            "reasoning":         result.get("reasoning", ""),
            "canonical_name":    result.get("canonical_name"),
            "satellite_analyzed": True,
            "source_changes":    group_ids,
            "action_per_uid":    apu,
            "suggested_sections": result.get("suggested_sections", []),
            "breaks":            result.get("breaks", []),
            "proposed_action":   proposed_action,
            "primary_uid":       primary_uid,
            "discard_uids":      discard_uids,
            "points":            combined_pts,
            "status":            status,
        }
        new_entries.append(entry)
        for cid in group_ids:
            superseded_map[cid] = p4_id

    # ── Write results ─────────────────────────────────────────────────────────
    if not new_entries:
        print(f"\nNo new Phase 4 changes. ({skipped_distinct} groups confirmed DISTINCT, {errors} errors)")
        return

    # Reload fresh copy before writing
    data = json.loads(PROPOSED.read_text(encoding="utf-8"))

    # Mark originals as superseded
    for c in data["changes"]:
        if c["id"] in superseded_map:
            c["status"] = "superseded"
            c["superseded_by"] = superseded_map[c["id"]]

    # Add new Phase 4 entries (avoid duplicates)
    existing_ids = {c["id"] for c in data["changes"]}
    for entry in new_entries:
        if entry["id"] not in existing_ids:
            data["changes"].append(entry)

    # Update meta
    meta = data.get("meta", {})
    all_p4 = [c for c in data["changes"] if c.get("phase") == 4]
    meta["phase4_total"]   = len(all_p4)
    meta["phase4_pending"] = sum(1 for c in all_p4 if c.get("status") == "pending_review")
    data["meta"] = meta

    PROPOSED.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    pending  = sum(1 for e in new_entries if e["status"] == "pending_review")
    approved = sum(1 for e in new_entries if e["status"] == "auto_approved")
    print(f"\nDone. {len(new_entries)} Phase 4 changes written ({pending} pending, {approved} auto-approved)")
    print(f"  {len(superseded_map)} original changes superseded")
    print(f"  {skipped_distinct} groups confirmed DISTINCT (originals unchanged)")
    if errors:
        print(f"  {errors} errors — re-run to retry")


if __name__ == "__main__":
    main()
