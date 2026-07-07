#!/usr/bin/env python3
"""
Wide-area Google Maps beach scraper.
Downsamples search points from current.json to cover the Greek coastline efficiently,
queries Google Maps with a wide radius (default 10km), filters out non-beaches
(hotels, bars, cafes, taverns, lagoons, etc.), and stores new verification data
separately in data_new/gmaps_verification_wide.json.
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
ROOT             = Path(__file__).parent.parent
DATA             = ROOT / "data_new"
GEOJSON          = DATA / "current.json"
DELETED_GMAPS    = DATA / "deleted_gmaps.json"
VERIFY_OUT       = DATA / "gmaps_verification.json"
VERIFY_WIDE_OUT  = DATA / "gmaps_verification_wide.json"
SCREENSHOT_DIR   = DATA / "gmaps_screenshots"

# ── Bad Keywords (to exclude non-beach POIs) ──────────────────────────────────
BAD_KEYWORDS = [
    # English terms
    "bar", "club", "cafe", "kafe", "kanteen", "cantina",
    "resort", "hotel", "villa", "suite", "room", "apartment", "studio", "lodging", "accommodation", 
    "house", "home", "spiti", "cottage", "guesthouse", "bnb",
    "restaurant", "tavern", "taverna", "eatery", "bistro", "grill", "seafood", "pizza", "food",
    "camping", "camp", "parking", "port", "marina", "yacht", "harbor", "harbour", "anchorage",
    "church", "monastery", "chapel", "ekklisia", "shrine",
    "rental", "car", "motor", "travel", "tour", "agency",
    "surf", "diving", "dive", "sport", "watersport",
    "lagun", "laguna", "lagoon",
    # Greek terms
    "καφέ", "μπαρ", "καντίνα", "ξενοδοχείο", "ενοικιαζόμενα", "ξενώνας", "δωμάτια", "βίλα", "βίλες",
    "εστιατόριο", "ταβέρνα", "ψαροταβέρνα", "ψησταριά", "φαγητό",
    "λιμάνι", "μαρίνα", "πάρκινγκ", "ναός", "εκκλησία", "ξωκλήσι", "μοναστήρι",
    "λιμνοθάλασσα"
]

def is_bad_place(name: str, category: str = None) -> bool:
    name_lower = name.lower()
    
    # Check category first
    if category:
        cat_lower = category.lower()
        for bad in ["bar", "hotel", "resort", "restaurant", "cafe", "lodging", "guest house", "apartment", "taverna", "camping"]:
            if bad in cat_lower:
                return True
                
    # Check name against list of bad keywords
    for word in BAD_KEYWORDS:
        # Use regex boundary or simple substring check (simple substring is safer for combined words like beachbar, hotelresort, etc.)
        if word in name_lower:
            return True
            
    return False

def is_in_greece(lat: float, lon: float) -> bool:
    # Latitude: between 34.0 and 42.0
    # Longitude: between 19.0 and 28.5
    return 34.0 <= lat <= 42.5 and 18.5 <= lon <= 29.0

# ── Playwright scraper ────────────────────────────────────────────────────────

def check_via_playwright(
    lat: float,
    lon: float,
    page,
    search_radius: float,
    max_reviews: int = 10,
    cached_places: dict = None,
) -> dict:
    try:
        # Use a zoom level that matches the search radius (12z ≈ 10km radius)
        search_url = f"https://www.google.com/maps/search/beach/@{lat},{lon},12z"
        page.goto(search_url, timeout=25_000)
        try:
            page.wait_for_selector('div[role="feed"], h1, a[href*="/maps/place/"]', timeout=8000)
        except Exception:
            pass
        time.sleep(2.0)

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
                for _ in range(4):  # Scroll 4 times for wider search
                    try:
                        feed.first.evaluate('(el) => el.scrollTop = el.scrollHeight')
                    except Exception:
                        pass
                    time.sleep(random.uniform(1.2, 2.0))
            
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
            url_coords = extract_coords(p_url)
            if url_coords:
                if not is_in_greece(url_coords[0], url_coords[1]):
                    continue
                dist = _hav(lat, lon, url_coords[0], url_coords[1])
                if dist > search_radius:
                    continue  # Skip if too far
            else:
                dist = None

            dist_str = f"{dist:.1f}m" if dist is not None else "unknown"

            # Check if this place is already scraped under another point to reuse its details
            if cached_places and p_url in cached_places:
                cached_beach = cached_places[p_url]
                
                # Check name filter for safety
                if is_bad_place(cached_beach["name"], cached_beach.get("category")):
                    continue
                    
                beach_data = {**cached_beach, "distance_m": dist if dist is not None else 0.0}
                beaches.append(beach_data)
                print_name = beach_data["name"].encode('ascii', errors='replace').decode('ascii')
                print(f"    [CACHE HIT] Using cached details for: {print_name} (distance: {dist_str})")
                continue

            try:
                page.goto(p_url, timeout=25_000)
                try:
                    page.wait_for_selector('h1', timeout=8000)
                except Exception:
                    pass
                time.sleep(random.uniform(1.8, 2.8))

                # Extract basic info
                name = None
                name_elem = page.locator('h1')
                if name_elem.count() > 0:
                    name = name_elem.first.inner_text().strip()
                if not name:
                    continue
                
                # Check bad keywords filter
                if is_bad_place(name):
                    print_name = name.encode('ascii', errors='replace').decode('ascii')
                    print(f"    [FILTERED] Skipping non-beach: {print_name}")
                    continue

                # Extra info fields
                category = None
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

                # Check category filter
                if is_bad_place(name, category):
                    print_name = name.encode('ascii', errors='replace').decode('ascii')
                    print(f"    [FILTERED] Skipping non-beach Category ({category}): {print_name}")
                    continue

                print_name = name.encode('ascii', errors='replace').decode('ascii')
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

                address = None
                phone = None
                website = None

                addr_elem = page.locator('button[data-item-id="address"]')
                if addr_elem.count() > 0:
                    address = addr_elem.first.inner_text().strip()

                phone_elem = page.locator('button[data-item-id*="phone:tel:"]')
                if phone_elem.count() > 0:
                    phone = phone_elem.first.inner_text().strip()

                web_elem = page.locator('a[data-item-id="authority"]')
                if web_elem.count() > 0:
                    website = web_elem.first.get_attribute("href")

                # Get coordinates from page URL (more accurate)
                page_coords = extract_coords(page.url) or url_coords
                lat_val = page_coords[0] if page_coords else lat
                lon_val = page_coords[1] if page_coords else lon
                
                if not is_in_greece(lat_val, lon_val):
                    continue

                if dist is None and page_coords:
                    dist = _hav(lat, lon, lat_val, lon_val)
                    if dist > search_radius:
                        continue

                # Scrape reviews
                reviews_list = []
                try:
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
                        max_attempts = 15
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
                            time.sleep(random.uniform(0.8, 1.2))

                        review_cards = page.locator('div.jftiEf, div[data-review-id]').all()
                        seen_reviews = set()
                        for card in review_cards:
                            try:
                                try:
                                    more_btn = card.locator('button:has-text("More"), button:has-text("Περισσότερα"), span:has-text("More"), span:has-text("Περισσότερα")')
                                    if more_btn.count() > 0 and more_btn.first.is_visible():
                                        more_btn.first.click(timeout=500)
                                        time.sleep(0.1)
                                except Exception:
                                    pass

                                r_name = card.get_attribute("aria-label") or ""
                                if not r_name:
                                    for sel in ['.d4r55', '.TSb6qb']:
                                        elem = card.locator(sel)
                                        if elem.count() > 0:
                                            r_name = elem.first.inner_text().strip()
                                            break

                                r_text = ""
                                text_elem = card.locator('span.wiI7pd')
                                if text_elem.count() > 0:
                                    r_text = text_elem.first.inner_text().strip()

                                r_key = (r_name, r_text)
                                if r_key in seen_reviews:
                                    continue
                                seen_reviews.add(r_key)

                                r_rating = None
                                rating_elem = card.locator('span.kvMYJc, span[aria-label*="star" i], span[aria-label*="αστέρ" i]')
                                if rating_elem.count() > 0:
                                    label = rating_elem.first.get_attribute("aria-label") or ""
                                    m = re.search(r'(\d+)', label)
                                    if m:
                                        r_rating = int(m.group(1))

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
                    pass

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
                continue

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
    ap.add_argument("--limit",          type=int, default=0,        help="Max search points to run (0 = all)")
    ap.add_argument("--search-spacing", type=float, default=8000.0,  help="Spacing between search grid points in meters (default: 8000)")
    ap.add_argument("--search-radius",  type=float, default=10000.0, help="Search radius around grid points in meters (default: 10000)")
    ap.add_argument("--headed",         action="store_true",        help="Show browser window")
    ap.add_argument("--summary",        action="store_true",        help="Print summary of wide results and exit")
    ap.add_argument("--region",         type=str, default=None,     help="Filter search to region: 'crete', 'peloponnese', 'cyclades', 'ionian', or custom bounding box: 'min_lat,max_lat,min_lon,max_lon'")
    args = ap.parse_args()

    # ── Summary mode ────────────────────────────────────────────────────────
    if args.summary:
        if not VERIFY_WIDE_OUT.exists():
            print("No wide verification results found yet.")
            return
        results = json.loads(VERIFY_WIDE_OUT.read_text(encoding="utf-8"))
        total   = len(results)
        found   = sum(1 for r in results.values() if r.get("found"))
        errors  = sum(1 for r in results.values() if "error" in r)
        print(f"Wide search completed: {total:,} search points")
        print(f"  Beaches found: {found:,}")
        print(f"  Not found:     {total-found-errors:,}")
        print(f"  Errors:        {errors:,}")
        return

    # ── Downsample search points to cover Greek coast ───────────────────────
    if not GEOJSON.exists():
        print(f"Error: {GEOJSON} does not exist. Run prepare_data.py first.")
        return

    fc = json.loads(GEOJSON.read_text(encoding="utf-8"))
    all_beaches = []
    for feat in fc["features"]:
        uid = feat.get("properties", {}).get("uid")
        # Ignore sections if we are downsampling main beaches to keep it fast
        role = feat.get("properties", {}).get("beach_role")
        if not uid:
            continue
        coords = feat["geometry"]["coordinates"]
        names = feat["properties"].get("name", ["Unnamed"])
        if isinstance(names, list):
            name_val = names[0] if len(names) > 0 else "Unnamed"
        elif isinstance(names, str):
            name_val = names
        else:
            name_val = "Unnamed"
            
        all_beaches.append({
            "uid": uid,
            "name": name_val,
            "lat": coords[1],
            "lon": coords[0],
            "role": role
        })

    # Sort all beaches to be deterministic
    all_beaches = sorted(all_beaches, key=lambda x: (x["lat"], x["lon"]))

    # Filter by region if requested
    if args.region:
        reg = args.region.lower()
        if reg == "crete":
            min_lat, max_lat, min_lon, max_lon = 34.8, 35.7, 23.4, 26.4
        elif reg == "peloponnese":
            min_lat, max_lat, min_lon, max_lon = 36.3, 38.4, 21.1, 23.3
        elif reg == "cyclades":
            min_lat, max_lat, min_lon, max_lon = 36.0, 38.0, 24.0, 25.8
        elif reg == "ionian":
            min_lat, max_lat, min_lon, max_lon = 37.0, 40.0, 20.0, 21.0
        else:
            try:
                min_lat, max_lat, min_lon, max_lon = map(float, reg.split(","))
            except Exception:
                print(f"Error: Invalid region input '{args.region}'. Expected one of: crete, peloponnese, cyclades, ionian or min_lat,max_lat,min_lon,max_lon")
                return
        
        all_beaches = [b for b in all_beaches if min_lat <= b["lat"] <= max_lat and min_lon <= b["lon"] <= max_lon]
        print(f"Filtered candidates to region '{args.region}': {len(all_beaches):,} candidate anchors.")

    # Downsample points by distance threshold to create wide-search grid
    grid_points = []
    for b in all_beaches:
        # Prioritize main role for grid anchor points
        too_close = False
        for gp in grid_points:
            if _hav(b["lat"], b["lon"], gp["lat"], gp["lon"]) < args.search_spacing:
                too_close = True
                break
        if not too_close:
            grid_points.append(b)

    print(f"Coastline Grid Layout:")
    print(f"  Total beaches in database: {len(all_beaches):,}")
    print(f"  Downsampled search points (spacing: {args.search_spacing/1000:.1f}km): {len(grid_points):,}")

    if args.limit:
        grid_points = grid_points[:args.limit]
        print(f"  Limiting to first {args.limit} search points.")

    # ── Load existing caches ─────────────────────────────────────────────────
    cached_places = {}
    
    # 1. Base verification results cache
    if VERIFY_OUT.exists():
        try:
            base_data = json.loads(VERIFY_OUT.read_text(encoding="utf-8"))
            for res in base_data.values():
                if res.get("found") and res.get("beaches"):
                    for b in res["beaches"]:
                        if b.get("href") and not is_bad_place(b["name"], b.get("category")):
                            cached_places[b["href"]] = b
            print(f"Loaded {len(cached_places):,} cached places from base verification file.")
        except Exception as e:
            print(f"Warning: Failed to load base verification cache: {e}")

    # 2. Existing wide search results (to resume or load cache)
    existing_wide = {}
    if VERIFY_WIDE_OUT.exists():
        try:
            existing_wide = json.loads(VERIFY_WIDE_OUT.read_text(encoding="utf-8"))
            for res in existing_wide.values():
                if res.get("found") and res.get("beaches"):
                    for b in res["beaches"]:
                        if b.get("href") and not is_bad_place(b["name"], b.get("category")):
                            cached_places[b["href"]] = b
            print(f"Loaded additional cached places from existing wide results. Total cache: {len(cached_places):,}")
        except Exception as e:
            print(f"Warning: Failed to load wide verification: {e}")

    # Filter out already checked grid points
    grid_points_to_check = [gp for gp in grid_points if gp["uid"] not in existing_wide]
    print(f"Remaining grid points to query in browser: {len(grid_points_to_check):,}")

    if not grid_points_to_check:
        print("All grid points checked! Nothing to do.")
        return

    # ── Playwright execution ──────────────────────────────────────────────────
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright is not installed. Run: pip install playwright && playwright install chromium")

    print(f"Starting Playwright ({'headed' if args.headed else 'headless'})...")
    
    pw = None
    browser = None
    context = None
    page = None

    def start_browser():
        nonlocal pw, browser, context, page
        try:
            if page: page.close()
        except Exception: pass
        try:
            if context: context.close()
        except Exception: pass
        try:
            if browser: browser.close()
        except Exception: pass
        try:
            if pw: pw.stop()
        except Exception: pass

        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        if stealth_sync:
            stealth_sync(page)

    try:
        start_browser()
        for i, gp in enumerate(grid_points_to_check):
            # Periodically restart browser context (every 15 points) to clear memory
            if i > 0 and i % 15 == 0:
                print("Re-spawning browser to clear cache and prevent hangs...")
                start_browser()

            print(f"[{i+1}/{len(grid_points_to_check)}] Search Center: '{gp['name']}' ({gp['lat']:.5f}, {gp['lon']:.5f})")
            
            attempts = 0
            success = False
            result = None
            
            while attempts < 3 and not success:
                try:
                    result = check_via_playwright(
                        gp["lat"], gp["lon"], page,
                        search_radius=args.search_radius,
                        max_reviews=10,
                        cached_places=cached_places
                    )
                    success = True
                except Exception as e:
                    attempts += 1
                    print(f"  [ERROR] Playwright error occurred (attempt {attempts}/3): {e}")
                    print("  Re-spawning browser to recover...")
                    try:
                        start_browser()
                    except Exception as spawn_err:
                        print(f"  [CRITICAL] Failed to restart browser: {spawn_err}")
                    time.sleep(3.0)

            if not success or result is None:
                result = {
                    "method": "playwright",
                    "found": False,
                    "error": "Failed after 3 attempts with browser restarts",
                    "checked_at": _now()
                }
                
            existing_wide[gp["uid"]] = {
                **result,
                "search_center_name": gp["name"],
                "search_center_lat": gp["lat"],
                "search_center_lon": gp["lon"]
            }

            # Add newly found beaches to cache for subsequent steps
            if result.get("found") and result.get("beaches"):
                for b in result["beaches"]:
                    if b.get("href"):
                        cached_places[b["href"]] = b
                
                found_names = [b["name"] for b in result["beaches"]]
                safe_names = [n.encode('ascii', errors='replace').decode('ascii') for n in found_names]
                print(f"  [FOUND] {len(result['beaches'])} beaches: {safe_names[:4]}")
            else:
                if "error" in result:
                    print(f"  [NOT FOUND] due to error: {result['error']}")
                else:
                    print(f"  [NOT FOUND] No new beaches found in this area.")

            # Save wide output file after every search point
            VERIFY_WIDE_OUT.write_text(json.dumps(existing_wide, ensure_ascii=False, indent=2), encoding="utf-8")
            
            # Jitter delay
            time.sleep(random.uniform(3.0, 5.0))
    finally:
        try:
            if page: page.close()
            if context: context.close()
            if browser: browser.close()
            if pw: pw.stop()
        except Exception:
            pass
        VERIFY_WIDE_OUT.write_text(json.dumps(existing_wide, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nWide search scraping finished. Results saved to {VERIFY_WIDE_OUT}")

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
