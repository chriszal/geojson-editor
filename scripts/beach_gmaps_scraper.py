#!/usr/bin/env python3
"""
Google Maps beach verifier — Playwright-based.

For each beach point (or a subset), opens Google Maps, checks what Google
thinks is at that location, and records:
  - Whether Google Maps has a named "beach" POI near the pin
  - The Google name(s) found
  - The rating and review count (quality signal)
  - A screenshot for manual spot-checks

This runs SEPARATELY from the main pipeline and writes:
  data/gmaps_verification.json   — one record per point checked
  data/gmaps_screenshots/        — PNG screenshots (optional)

The results are used as an ADDITIONAL signal in Phase 2 (or standalone).

Usage
-----
  pip install playwright && playwright install chromium

  # Verify all unverified points (slow — ~3s per point)
  python scripts/beach_gmaps_scraper.py

  # Verify only points in a specific phase-2 cluster file
  python scripts/beach_gmaps_scraper.py --cluster-ids p2_abc123 p2_def456

  # Verify just the first N points (for testing)
  python scripts/beach_gmaps_scraper.py --limit 50

  # Save screenshots
  python scripts/beach_gmaps_scraper.py --screenshots

  # Show verification results summary
  python scripts/beach_gmaps_scraper.py --summary

Notes
-----
- Runs headless by default. Add --headed for visual debugging.
- Google Maps has no official scraping API. This respects rate limits (3-5 s
  between requests) and runs non-commercially for private research.
- For large-scale use, the Google Places API (paid) is more reliable and ToS-safe.
  Set MAPS_API_KEY and use --use-places-api instead.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT         = Path(__file__).parent.parent
DATA         = ROOT / "data_new"
GEOJSON      = DATA / "current.json"
PROPOSED     = DATA / "proposed_changes.json"
VERIFY_OUT   = DATA / "gmaps_verification.json"
SCREENSHOT_DIR = DATA / "gmaps_screenshots"

# ── Config ────────────────────────────────────────────────────────────────────
DELAY_MIN = 3.0     # minimum seconds between requests
DELAY_MAX = 6.0     # maximum seconds between requests
SEARCH_RADIUS_M = 200   # metres — how close must a Google POI be to count?


# ── Places API (fast, reliable, paid after free tier) ─────────────────────────

def check_via_places_api(lat: float, lon: float, api_key: str) -> dict:
    """
    Use Google Places Nearby Search to find beach POIs near (lat, lon).
    Free tier: $200/month credit ≈ 40,000 calls/month.
    """
    import requests
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {
        "location": f"{lat},{lon}",
        "radius":   SEARCH_RADIUS_M,
        "type":     "natural_feature",
        "keyword":  "beach",
        "key":      api_key,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        results = r.json().get("results", [])
        beaches = [
            {
                "name":         pl["name"],
                "rating":       pl.get("rating"),
                "user_ratings":  pl.get("user_ratings_total", 0),
                "distance_m":   _hav(lat, lon,
                                     pl["geometry"]["location"]["lat"],
                                     pl["geometry"]["location"]["lng"]),
                "place_id":     pl.get("place_id"),
            }
            for pl in results
            if _hav(lat, lon,
                    pl["geometry"]["location"]["lat"],
                    pl["geometry"]["location"]["lng"]) <= SEARCH_RADIUS_M
        ]
        return {
            "method":        "places_api",
            "found":         len(beaches) > 0,
            "beaches":       beaches,
            "checked_at":    _now(),
        }
    except Exception as e:
        return {"method": "places_api", "found": False, "error": str(e), "checked_at": _now()}


# ── Playwright scraper (free, no API key) ─────────────────────────────────────

def check_via_playwright(
    lat: float,
    lon: float,
    page,                    # playwright Page object
    save_screenshot: bool = False,
    screenshot_path: str | None = None,
) -> dict:
    """
    Open Google Maps at (lat, lon) and extract nearby beach POI info.
    Uses the search URL which auto-shows what's at the coordinates.
    """
    # Google Maps URL that centres on the point and shows nearby places
    url = f"https://www.google.com/maps/@{lat},{lon},17z"

    try:
        page.goto(url, wait_until="networkidle", timeout=20_000)
        time.sleep(2)

        # Accept cookies if dialog appears
        try:
            accept = page.locator('button:has-text("Accept all"), button:has-text("Αποδοχή")')
            if accept.count() > 0:
                accept.first.click()
                time.sleep(1)
        except Exception:
            pass

        # Search for beaches near this point
        search_url = (
            f"https://www.google.com/maps/search/beach/@{lat},{lon},15z"
        )
        page.goto(search_url, wait_until="networkidle", timeout=20_000)
        time.sleep(2.5)

        if save_screenshot and screenshot_path:
            page.screenshot(path=screenshot_path, full_page=False)

        # Extract place results from the sidebar
        beaches = []
        result_items = page.locator('[data-result-index]').all()
        if not result_items:
            # Fallback: look for place name headings
            result_items = page.locator('a[href*="/maps/place/"]').all()

        for item in result_items[:8]:
            try:
                text = item.inner_text(timeout=2000).strip()
                href = item.get_attribute("href") or ""
                if not text or len(text) < 3:
                    continue

                # Rough distance from URL or text — Google often shows distance
                # We'll record the name and let the caller decide relevance
                beaches.append({
                    "name":     text.split("\n")[0].strip(),
                    "href":     href[:120] if href else None,
                })
            except Exception:
                continue

        return {
            "method":     "playwright",
            "found":      len(beaches) > 0,
            "beaches":    beaches[:5],
            "checked_at": _now(),
        }

    except Exception as e:
        return {
            "method":     "playwright",
            "found":      False,
            "error":      str(e),
            "checked_at": _now(),
        }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit",       type=int, default=0,   help="Max points to check (0 = all)")
    ap.add_argument("--cluster-ids", nargs="*", default=[],  help="Only check points in these phase-2 cluster IDs")
    ap.add_argument("--screenshots", action="store_true",     help="Save screenshots to data/gmaps_screenshots/")
    ap.add_argument("--headed",      action="store_true",     help="Show browser window (for debugging)")
    ap.add_argument("--use-places-api", action="store_true",  help="Use Google Places API instead of Playwright")
    ap.add_argument("--summary",     action="store_true",     help="Print summary of existing results and exit")
    args = ap.parse_args()

    # ── Summary mode ────────────────────────────────────────────────────────
    if args.summary:
        if not VERIFY_OUT.exists():
            print("No verification results found. Run the scraper first.")
            return
        results = json.loads(VERIFY_OUT.read_text(encoding="utf-8"))
        total   = len(results)
        found   = sum(1 for r in results.values() if r.get("found"))
        errors  = sum(1 for r in results.values() if "error" in r)
        print(f"Verified: {total:,} points")
        print(f"  Beach found on Google Maps: {found:,} ({found/total*100:.1f}%)")
        print(f"  Not found / unclear:        {total-found-errors:,}")
        print(f"  Errors:                     {errors:,}")
        top = sorted(
            [(uid, r) for uid, r in results.items() if r.get("found") and r.get("beaches")],
            key=lambda x: -(x[1]["beaches"][0].get("user_ratings", 0) or 0),
        )[:10]
        if top:
            print("\nTop rated (by Google reviews):")
            for uid, r in top:
                b = r["beaches"][0]
                print(f"  {b['name']!r}  ⭐{b.get('rating','?')} ({b.get('user_ratings',0)} reviews)  uid={uid}")
        return

    # ── Load points to check ─────────────────────────────────────────────────
    fc = json.loads(GEOJSON.read_text(encoding="utf-8"))
    all_pts = {
        feat["properties"]["uid"]: {
            "uid":  feat["properties"]["uid"],
            "name": feat["properties"].get("name", ""),
            "lat":  feat["geometry"]["coordinates"][1],
            "lon":  feat["geometry"]["coordinates"][0],
        }
        for feat in fc["features"]
        if feat.get("properties", {}).get("uid")
    }

    # Filter to cluster UIDs if requested
    if args.cluster_ids:
        if PROPOSED.exists():
            proposed = json.loads(PROPOSED.read_text(encoding="utf-8"))
            cluster_uids: set[str] = set()
            for ch in proposed.get("changes", []):
                if ch.get("id") in args.cluster_ids or ch.get("cluster_id") in args.cluster_ids:
                    for p in ch.get("points", []):
                        cluster_uids.add(p.get("uid", ""))
            pts_to_check = {uid: all_pts[uid] for uid in cluster_uids if uid in all_pts}
        else:
            pts_to_check = {}
    else:
        pts_to_check = all_pts

    # Skip already verified
    existing: dict[str, dict] = {}
    if VERIFY_OUT.exists():
        existing = json.loads(VERIFY_OUT.read_text(encoding="utf-8"))
    pts_to_check = {uid: p for uid, p in pts_to_check.items() if uid not in existing}

    if args.limit:
        items = list(pts_to_check.items())[:args.limit]
        pts_to_check = dict(items)

    print(f"{len(pts_to_check):,} points to verify ({len(existing):,} already done)")
    if not pts_to_check:
        print("Nothing to do.")
        return

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Places API mode ──────────────────────────────────────────────────────
    if args.use_places_api:
        api_key = os.environ.get("MAPS_API_KEY", "")
        if not api_key:
            raise SystemExit("MAPS_API_KEY not set. Export it before running.")
        print("Using Google Places API…")
        for i, (uid, pt) in enumerate(pts_to_check.items()):
            print(f"[{i+1}/{len(pts_to_check)}] {pt['name']!r}…", end=" ", flush=True)
            result = check_via_places_api(pt["lat"], pt["lon"], api_key)
            existing[uid] = result
            status = f"found: {result['beaches'][0]['name']!r}" if result.get("found") else "not found"
            print(status)
            if (i + 1) % 20 == 0:
                VERIFY_OUT.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  [saved {len(existing)} results]")
            time.sleep(random.uniform(0.5, 1.5))
        VERIFY_OUT.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nDone. Results in {VERIFY_OUT}")
        return

    # ── Playwright mode ──────────────────────────────────────────────────────
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "Playwright not installed.\n"
            "Run: pip install playwright && playwright install chromium"
        )

    print(f"Using Playwright ({'headed' if args.headed else 'headless'})…")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        for i, (uid, pt) in enumerate(pts_to_check.items()):
            print(f"[{i+1}/{len(pts_to_check)}] {pt['name']!r}  ({pt['lat']:.5f}, {pt['lon']:.5f})")

            ss_path = str(SCREENSHOT_DIR / f"{uid}.png") if args.screenshots else None
            result  = check_via_playwright(pt["lat"], pt["lon"], page,
                                           save_screenshot=args.screenshots,
                                           screenshot_path=ss_path)
            existing[uid] = {**result, "point_name": pt["name"]}

            if result.get("found"):
                names = [b["name"] for b in result.get("beaches", [])]
                print(f"  ✓ Found: {names[:3]}")
            elif "error" in result:
                print(f"  ✗ Error: {result['error'][:80]}")
            else:
                print(f"  — Not found")

            # Save every 10 points
            if (i + 1) % 10 == 0:
                VERIFY_OUT.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  [saved {len(existing)} results]")

            # Polite delay with jitter
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

        browser.close()

    VERIFY_OUT.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    found = sum(1 for r in existing.values() if r.get("found"))
    print(f"\nDone. {len(pts_to_check)} checked, {found} with Google beach data → {VERIFY_OUT}")


# ── Utilities ─────────────────────────────────────────────────────────────────

def _hav(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R = 6_371_000.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def _now() -> str:
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    main()
