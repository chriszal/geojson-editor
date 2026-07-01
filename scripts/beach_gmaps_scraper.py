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
import re
from pathlib import Path

try:
    from playwright_stealth import stealth_sync
except ImportError:
    stealth_sync = None

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
                "latitude":     pl["geometry"]["location"]["lat"],
                "longitude":    pl["geometry"]["location"]["lng"],
                "category":     pl.get("types", [None])[0] if pl.get("types") else None,
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
    max_reviews: int = 10,
    cached_places: dict = None,
) -> dict:
    """
    Open Google Maps at (lat, lon) and extract nearby beach POI info.
    Uses the search URL which auto-shows what's at the coordinates.
    """
    import re
    import time
    import random

    try:
        # Search for beaches near this point
        search_url = f"https://www.google.com/maps/search/beach/@{lat},{lon},15z"
        page.goto(search_url, timeout=20_000)
        try:
            page.wait_for_selector('div[role="feed"], h1, a[href*="/maps/place/"]', timeout=8000)
        except Exception:
            pass
        time.sleep(1.5)

        # Accept cookies if dialog appears
        try:
            consent_selectors = [
                'button:has-text("Accept all")',
                'button:has-text("Αποδοχή")',
                'button:has-text("Αποδοχή όλων")',
                'button:has-text("I agree")',
                'button:has-text("Agree")',
                'button:has-text("Consent")',
                'button:has-text("Accept")',
                'button:has-text("Alle akzeptieren")',
                'button[aria-label*="Accept"]',
                'button[aria-label*="Consent"]'
            ]
            for selector in consent_selectors:
                btn = page.locator(selector)
                if btn.count() > 0:
                    btn.first.click()
                    time.sleep(1.5)
                    break
        except Exception:
            pass

        place_urls = []

        # Helper to extract coordinates from URL
        def extract_coords(url_str: str) -> tuple[float, float] | None:
            m1 = re.search(r'@([0-9.-]+),([0-9.-]+)', url_str)
            if m1:
                return float(m1.group(1)), float(m1.group(2))
            m2 = re.search(r'!3d([0-9.-]+)!4d([0-9.-]+)', url_str)
            if m2:
                return float(m2.group(1)), float(m2.group(2))
            return None

        # Check if direct redirection happened to a single place page
        if "/maps/place/" in page.url:
            place_urls.append(page.url)
        else:
            # Scroll results list to load more beaches
            feed = page.locator('div[role="feed"]')
            if feed.count() > 0:
                for _ in range(2):
                    try:
                        feed.first.evaluate('(el) => el.scrollTop = el.scrollHeight')
                    except Exception:
                        pass
                    time.sleep(random.uniform(1.0, 1.8))
            
            # Find links
            links = page.locator('a[href*="/maps/place/"]').all()
            for link in links:
                href = link.get_attribute("href")
                if href and href not in place_urls:
                    place_urls.append(href)

        place_urls = list(dict.fromkeys(place_urls))
        beaches = []

        # Iterate and scrape details of each place
        for p_url in place_urls:
            # Before navigating, see if we can parse coordinates from the URL
            # and filter by distance to avoid scraping far away results
            url_coords = extract_coords(p_url)
            if url_coords:
                dist = _hav(lat, lon, url_coords[0], url_coords[1])
                if dist > SEARCH_RADIUS_M:
                    continue  # Skip if too far
            else:
                dist = None

            dist_str = f"{dist:.1f}m" if dist is not None else "unknown"

            # Check if this place is already scraped under another point to reuse its details
            if cached_places and p_url in cached_places:
                cached_beach = cached_places[p_url]
                beach_data = {**cached_beach, "distance_m": dist if dist is not None else 0.0}
                beaches.append(beach_data)
                print_name = beach_data["name"].encode('ascii', errors='replace').decode('ascii')
                print(f"    [CACHE HIT] Using cached details for: {print_name} (distance: {dist_str})")
                continue

            try:
                page.goto(p_url, timeout=20_000)
                try:
                    page.wait_for_selector('h1', timeout=8000)
                except Exception:
                    pass
                time.sleep(random.uniform(1.5, 2.5))

                # Extract basic info
                name = None
                name_elem = page.locator('h1')
                if name_elem.count() > 0:
                    name = name_elem.first.inner_text().strip()
                if not name:
                    continue
                
                # Sanitize name for console printing
                print_name = name.encode('ascii', errors='replace').decode('ascii')
                dist_str = f"{dist:.1f}m" if dist is not None else "unknown"
                print(f"    Scraping details for: {print_name} (distance: {dist_str})")

                # Rating and total reviews count
                rating = None
                reviews_count = 0
                rating_container = page.locator('div.F7nice')
                if rating_container.count() > 0:
                    text = rating_container.first.inner_text().strip()
                    m_rating = re.search(r'([3-5][.,]\d)', text)
                    if m_rating:
                        rating = float(m_rating.group(1).replace(',', '.'))
                    
                    m_count = re.search(r'\((\d+[\d\s.,]*)\)', text)
                    if m_count:
                        reviews_count = int(re.sub(r'[^\d]', '', m_count.group(1)))
                    else:
                        m_count_fallback = re.search(r'(\d+)\s+(reviews|κριτικές|Rezensionen|avis)', text, re.IGNORECASE)
                        if m_count_fallback:
                            reviews_count = int(m_count_fallback.group(1))

                # Extra info fields
                address = None
                phone = None
                website = None
                category = None

                addr_elem = page.locator('button[data-item-id="address"]')
                if addr_elem.count() > 0:
                    address = addr_elem.first.inner_text().strip()

                phone_elem = page.locator('button[data-item-id*="phone:tel:"]')
                if phone_elem.count() > 0:
                    phone = phone_elem.first.inner_text().strip()

                web_elem = page.locator('a[data-item-id="authority"]')
                if web_elem.count() > 0:
                    website = web_elem.first.get_attribute("href")

                try:
                    cat_elem = page.locator('button[jsaction*="category"], button[jsaction*="pane.rating.category"]')
                    if cat_elem.count() > 0:
                        category = cat_elem.first.inner_text().strip()
                    if not category:
                        cat_elem = page.locator('span.fontBodyMedium button')
                        for idx in range(cat_elem.count()):
                            txt = cat_elem.nth(idx).inner_text().strip()
                            if txt and not any(char.isdigit() for char in txt) and len(txt) < 30:
                                category = txt
                                break
                except Exception:
                    pass

                # Get coordinates from page URL (more accurate)
                page_coords = extract_coords(page.url) or url_coords
                lat_val = page_coords[0] if page_coords else lat
                lon_val = page_coords[1] if page_coords else lon
                if dist is None and page_coords:
                    dist = _hav(lat, lon, lat_val, lon_val)
                    if dist > SEARCH_RADIUS_M:
                        continue  # Skip if too far after checking final URL

                # Scrape reviews
                reviews_list = []
                try:
                    # Click Reviews tab
                    clicked = False
                    tab = page.locator('button[role="tab"][aria-label*="Reviews" i], button[role="tab"][aria-label*="reviews" i], button[role="tab"][aria-label*="Κριτικές" i], button[role="tab"]:has-text("Reviews"), button[role="tab"]:has-text("Κριτικές")')
                    if tab.count() > 0:
                        tab.first.click(timeout=3000)
                        clicked = True
                    else:
                        cnt = page.locator('div.F7nice span').last
                        if cnt.count() > 0:
                            cnt.click(timeout=3000)
                            clicked = True

                    if clicked:
                        time.sleep(random.uniform(1.5, 2.5))
                        # Scroll to load reviews
                        scroll_js = """
                        () => {
                            const divs = Array.from(document.querySelectorAll('div'));
                            const reviewContainer = divs.find(d => 
                                d.scrollHeight > d.clientHeight && 
                                (d.querySelector('.jftiEf') || d.querySelector('[data-review-id]'))
                            );
                            if (reviewContainer) {
                                reviewContainer.scrollTop = reviewContainer.scrollHeight;
                                return true;
                            }
                            const m6QErbs = Array.from(document.querySelectorAll('div.m6QErb'));
                            const scrollable = m6QErbs.find(d => d.scrollHeight > d.clientHeight);
                            if (scrollable) {
                                scrollable.scrollTop = scrollable.scrollHeight;
                                return true;
                            }
                            return false;
                        }
                        """
                        max_reviews_to_load = max_reviews
                        scroll_attempts = 0
                        max_attempts = 20
                        last_count = 0
                        no_change_count = 0

                        while scroll_attempts < max_attempts:
                            current_count = page.locator('div.jftiEf, div[data-review-id]').count()
                            if current_count >= max_reviews_to_load:
                                break
                            if current_count == last_count:
                                no_change_count += 1
                                if no_change_count >= 3:
                                    break
                            else:
                                no_change_count = 0
                            last_count = current_count

                            scrolled = page.evaluate(scroll_js)
                            if not scrolled:
                                break
                            scroll_attempts += 1
                            time.sleep(random.uniform(0.8, 1.4))

                        # Extract reviews
                        review_cards = page.locator('div.jftiEf, div[data-review-id]').all()
                        seen_reviews = set()
                        for card in review_cards:
                            try:
                                # Expand text
                                try:
                                    more_btn = card.locator('button:has-text("More"), button:has-text("Περισσότερα"), span:has-text("More"), span:has-text("Περισσότερα"), button.w8nwda, span.w8nwda')
                                    if more_btn.count() > 0 and more_btn.first.is_visible():
                                        more_btn.first.click(timeout=500)
                                        time.sleep(random.uniform(0.1, 0.2))
                                except Exception:
                                    pass

                                # Author
                                r_name = card.get_attribute("aria-label") or ""
                                if not r_name:
                                    for sel in ['.d4r55', '.d1z7is', '.G5uFl', '.TSb6qb']:
                                        elem = card.locator(sel)
                                        if elem.count() > 0:
                                            r_name = elem.first.inner_text().strip()
                                            break
                                    if not r_name:
                                        profile_link = card.locator('a[href*="/maps/contrib/"]')
                                        if profile_link.count() > 0:
                                            r_name = profile_link.first.inner_text().strip()

                                # Text
                                r_text = ""
                                text_elem = card.locator('span.wiI7pd')
                                if text_elem.count() > 0:
                                    r_text = text_elem.first.inner_text().strip()

                                # Deduplicate
                                r_key = (r_name, r_text)
                                if r_key in seen_reviews:
                                    continue
                                seen_reviews.add(r_key)

                                # Rating
                                r_rating = None
                                rating_elem = card.locator('span.kvMYJc, span[aria-label*="star" i], span[aria-label*="αστέρ" i]')
                                if rating_elem.count() > 0:
                                    label = rating_elem.first.get_attribute("aria-label") or ""
                                    m = re.search(r'(\d+)', label)
                                    if m:
                                        r_rating = int(m.group(1))

                                # Date
                                r_date = ""
                                date_elem = card.locator('span.rsqaWe')
                                if date_elem.count() > 0:
                                    r_date = date_elem.first.inner_text().strip()

                                reviews_list.append({
                                    "reviewer_name": r_name,
                                    "rating": r_rating,
                                    "text": r_text,
                                    "date": r_date
                                })
                                if len(reviews_list) >= 10:
                                    break
                            except Exception:
                                continue
                except Exception as e:
                    print(f"      Reviews extraction failed for {name}: {e}")

                beaches.append({
                    "name":         name,
                    "rating":       rating,
                    "user_ratings": reviews_count,
                    "distance_m":   dist if dist is not None else 0.0,
                    "latitude":     lat_val,
                    "longitude":    lon_val,
                    "category":     category,
                    "address":      address,
                    "phone":        phone,
                    "website":      website,
                    "reviews":      reviews_list,
                    "href":         p_url
                })

            except Exception as e:
                print(f"      Failed to scrape place {p_url}: {e}")
                continue

        # Take screenshot if required
        if save_screenshot and screenshot_path:
            try:
                page.screenshot(path=screenshot_path, full_page=False)
            except Exception:
                pass

        # Sort found beaches by distance
        beaches = sorted(beaches, key=lambda x: x["distance_m"])

        return {
            "method":     "playwright",
            "found":      len(beaches) > 0,
            "beaches":    beaches,
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
    ap.add_argument("--max-reviews",   type=int, default=10,  help="Max reviews to load per place (default: 10)")
    ap.add_argument("--skip-distance", type=float, default=0.0, help="Skip checking point if within X meters of an already checked point (default: 0 = disabled)")
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
    
    # Pre-populate cached_places lookup dictionary from existing verified results
    cached_places = {}
    for res in existing.values():
        if res.get("found") and res.get("beaches"):
            for b in res["beaches"]:
                if b.get("href"):
                    cached_places[b["href"]] = b

    # Filter out already checked points
    pts_to_check = {uid: p for uid, p in pts_to_check.items() if uid not in existing}

    # Proximity checking to skip and auto-resolve close points
    if args.skip_distance > 0:
        print(f"Applying spatial skip proximity threshold: {args.skip_distance}m...")
        skipped_count = 0
        checked_points = []
        for uid, res in existing.items():
            orig_pt = all_pts.get(uid)
            if orig_pt:
                checked_points.append({
                    "uid": uid,
                    "lat": orig_pt["lat"],
                    "lon": orig_pt["lon"],
                    "res": res
                })

        to_remove = []
        for uid, pt in list(pts_to_check.items()):
            for cp in checked_points:
                dist = _hav(pt["lat"], pt["lon"], cp["lat"], cp["lon"])
                if dist <= args.skip_distance:
                    # Clone the beaches from the close checked point, updating distances relative to this point
                    cloned_beaches = []
                    for b in cp["res"].get("beaches", []):
                        b_dist = _hav(pt["lat"], pt["lon"], b["latitude"], b["longitude"])
                        if b_dist <= SEARCH_RADIUS_M:
                            cloned_beaches.append({**b, "distance_m": b_dist})

                    existing[uid] = {
                        "method": "playwright_spatial_skip",
                        "found": len(cloned_beaches) > 0,
                        "beaches": cloned_beaches,
                        "checked_at": _now(),
                        "point_name": pt["name"]
                    }
                    to_remove.append(uid)
                    skipped_count += 1
                    # Also register this skipped point as checked so it can assist subsequent ones
                    checked_points.append({
                        "uid": uid,
                        "lat": pt["lat"],
                        "lon": pt["lon"],
                        "res": existing[uid]
                    })
                    break

        for uid in to_remove:
            del pts_to_check[uid]

        print(f"  [SPATIAL SKIP] Skipped and auto-verified {skipped_count} points (remaining to check in browser: {len(pts_to_check)})")
        if skipped_count > 0:
            VERIFY_OUT.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

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
        print("Using Google Places API...")
        for i, (uid, pt) in enumerate(pts_to_check.items()):
            print(f"[{i+1}/{len(pts_to_check)}] {pt['name']!r}...", end=" ", flush=True)
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

    print(f"Using Playwright ({'headed' if args.headed else 'headless'})...")
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
        if stealth_sync:
            stealth_sync(page)

        try:
            for i, (uid, pt) in enumerate(pts_to_check.items()):
                print(f"[{i+1}/{len(pts_to_check)}] {pt['name']!r}  ({pt['lat']:.5f}, {pt['lon']:.5f})")

                ss_path = str(SCREENSHOT_DIR / f"{uid}.png") if args.screenshots else None
                result  = check_via_playwright(pt["lat"], pt["lon"], page,
                                               save_screenshot=args.screenshots,
                                               screenshot_path=ss_path,
                                               max_reviews=args.max_reviews,
                                               cached_places=cached_places)
                existing[uid] = {**result, "point_name": pt["name"]}

                # On success, insert new beaches into the cache dictionary so subsequent loops can reuse them
                if result.get("found") and result.get("beaches"):
                    for b in result["beaches"]:
                        if b.get("href"):
                            cached_places[b["href"]] = b

                if result.get("found"):
                    names = [b["name"] for b in result.get("beaches", [])]
                    # Sanitize names to avoid encoding issues in Windows terminal output
                    safe_names = [n.encode('ascii', errors='replace').decode('ascii') for n in names]
                    print(f"  [FOUND] Found: {safe_names[:3]}")
                elif "error" in result:
                    print(f"  [ERROR] Error: {result['error'][:80]}")
                else:
                    print(f"  [NOT FOUND] Not found")

                # Auto-save after every single point for robust crash safety
                VERIFY_OUT.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

                # Polite delay with jitter
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        finally:
            try:
                browser.close()
            except Exception:
                pass
            VERIFY_OUT.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    found = sum(1 for r in existing.values() if r.get("found"))
    print(f"\nDone. {len(pts_to_check)} checked, {found} with Google beach data -> {VERIFY_OUT}")


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
