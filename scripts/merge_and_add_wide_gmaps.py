#!/usr/bin/env python3
"""
Merge and Add Wide Google Maps scraper results to current.json.
Applies thorough checks to ensure only actual beaches are added (filtering out viewpoints,
residences, hotels, pools, rooms, bars, etc., by name, category, and review contents).
"""

import json
import math
import re
import argparse
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data_new"
VERIFY_WIDE_OUT = DATA / "gmaps_verification_wide.json"
GEOJSON = DATA / "current.json"
DELETED_GMAPS = DATA / "deleted_gmaps.json"

# ── Blacklists & Keywords ─────────────────────────────────────────────────────

# Negative keywords for name check
NAME_BLACKLIST = [
    "bar", "club", "cafe", "hotel", "resort", "villa", "suite", "room", "apartment", "lodging", 
    "house", "home", "taverna", "restaurant", "parking", "monastery", "church", "chapel", "marina", 
    "port", "viewpoint", "panoramic view", "observatory", "residence", "pool", "camping", "dive", 
    "rental", "studio", "guesthouse", "bnb", "cottage", "camp", "harbor", "harbour", "anchorage",
    "shrine", "chapel", "castle", "fortress", "museum", "ruins", "archaeological", "monument",
    
    # Greek name blacklist
    "ξενοδοχείο", "ενοικιαζόμενα", "δωμάτια", "βίλα", "βίλες", "διαμερίσματα", "καφετέρια", "καφέ", 
    "μπαρ", "ταβέρνα", "εστιατόριο", "εκκλησία", "εκκλησάκι", "μοναστήρι", "ναός", "λιμάνι", "μαρίνα", 
    "πάρκινγκ", "πισίνα", "πανοραμική θέα", "θέα", "κάμπινγκ", "δωμάτιο", "ξενώνας", "κάστρο", 
    "μουσείο", "ερείπια", "αρχαιολογικός", "μνημείο"
]

# Negative keywords for category check
CATEGORY_BLACKLIST = [
    "hotel", "resort", "bar", "cafe", "restaurant", "lodging", "guest house", "apartment", 
    "tavern", "seafood restaurant", "tourist attraction", "scenic spot", "viewpoint", 
    "observation deck", "church", "place of worship", "historical landmark", "marina", "harbor",
    "villa", "suite", "room", "cottage", "campground", "camping", "national park", "museum", 
    "archeological", "monument", "castle", "bed & breakfast", "bed and breakfast",
    
    # Greek category blacklist
    "ξενοδοχείο", "θέρετρο", "καφέ", "μπαρ", "εστιατόριο", "ταβέρνα", "τουριστικό αξιοθέατο", 
    "σημείο θέας", "εκκλησία", "μαρίνα", "λιμάνι", "μουσείο", "κάστρο", "κάμπινγκ"
]

# Review analysis keywords
POSITIVE_REVIEW_WORDS = [
    "beach", "sand", "pebble", "swim", "water", "sea", "cove", "bay", "snorkeling", 
    "swimsuit", "waves", "shore", "coast", "sunbeds", "parasols", "crystal clear",
    "παραλία", "άμμος", "θάλασσα", "νερά", "κολύμπι", "βότσαλα", "ξαπλώστρες", "κολυμπήσαμε", 
    "αμμουδιά", "καθαρά"
]

NEGATIVE_REVIEW_WORDS = [
    "hotel", "room", "resort", "restaurant", "bar", "food", "dinner", "lunch", "cocktail", 
    "service", "owner", "stayed", "bed", "bathroom", "shower", "viewpoint", "view", "church", 
    "monastery", "hike", "trail", "walking", "residence", "pool", "staff", "breakfast", "menu",
    "ξενοδοχείο", "δωμάτιο", "φαγητό", "ταβέρνα", "κοκτέιλ", "ξενώνας", "πισίνα", "θέα", "ιδιοκτήτης"
]

# ── Verification Filter ───────────────────────────────────────────────────────

def verify_beach(beach: dict) -> tuple[bool, str]:
    name = beach.get("name", "")
    name_lower = name.lower()
    category = beach.get("category", "")
    cat_lower = category.lower() if category else ""
    reviews = beach.get("reviews", [])

    # 1. Exact name blacklist check
    for word in NAME_BLACKLIST:
        # Check if it matches as substring (e.g. beachbar, hotelresort, etc.)
        if word in name_lower:
            return False, f"Name contains negative keyword '{word}'"

    # 2. Category blacklist check
    if category:
        for word in CATEGORY_BLACKLIST:
            if word in cat_lower:
                return False, f"Category contains negative keyword '{word}'"

    # 3. Check if name explicitly confirms it's a beach
    is_explicit_beach = any(
        w in name_lower for w in ["beach", "παραλία", "spiaggia", "plage", "paralia"]
    ) or any(
        w in cat_lower for w in ["beach", "παραλία", "natural feature"]
    )

    # 4. Review text analysis for hard cases (or to confirm explicit ones)
    total_pos = 0
    total_neg = 0
    all_reviews_text = " ".join([r.get("text", "").lower() for r in reviews])

    for word in POSITIVE_REVIEW_WORDS:
        total_pos += all_reviews_text.count(word)
    for word in NEGATIVE_REVIEW_WORDS:
        total_neg += all_reviews_text.count(word)

    # If it is not explicitly a beach in the title/category
    if not is_explicit_beach:
        # If there are no reviews to verify, be conservative and reject
        if not reviews:
            return False, "Not explicitly a beach and has no reviews to verify"
        
        # Require positive signals to outweigh negative ones
        if total_pos < 2:
            return False, f"Not explicitly a beach and too few positive review words ({total_pos})"
        if total_neg > total_pos:
            return False, f"Review validation failed (negative: {total_neg} > positive: {total_pos})"

    # Even if it has "beach" in the name, filter out if reviews overwhelmingly talk about a hotel/bar/restaurant
    if reviews and total_neg > total_pos + 10:
        return False, f"Explicit beach name but reviews indicate non-beach facility (neg: {total_neg} vs pos: {total_pos})"

    return True, "Passed validation"

# ── Math & Spatial utilities ──────────────────────────────────────────────────

def hav(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ, dλ = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return 6_371_000.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def merge_features_data(existing_feat, gmaps_beach, gmaps_uid):
    props = existing_feat["properties"]
    
    # 1. Merge Names
    existing_names = props.get("name", [])
    if not isinstance(existing_names, list):
        existing_names = [existing_names]
        
    g_name = gmaps_beach.get("name")
    if g_name and g_name not in existing_names:
        g_name_lower = g_name.lower()
        if not any(n.lower() == g_name_lower for n in existing_names):
            existing_names.append(g_name)
            
    props["name"] = existing_names
    
    # 2. Merge Sources & IDs
    sources = props.get("source", [])
    if not isinstance(sources, list):
        sources = [sources] if sources else []
    if "gmaps" not in sources:
        sources.append("gmaps")
    props["source"] = sources
    
    source_ids = props.get("source_id", [])
    if not isinstance(source_ids, list):
        source_ids = [source_ids] if source_ids else []
        
    g_href = gmaps_beach.get("href") or ""
    if g_href and g_href not in source_ids:
        source_ids.append(g_href)
    props["source_id"] = source_ids
    
    # 3. Merged UIDs
    merged_uids = props.get("merged_from_uids", [])
    if not isinstance(merged_uids, list):
        merged_uids = [merged_uids] if merged_uids else []
    if gmaps_uid not in merged_uids:
        merged_uids.append(gmaps_uid)
    props["merged_from_uids"] = merged_uids
    
    # 4. Copy missing details
    for k in ["rating", "user_ratings", "category", "address", "phone", "website"]:
        if props.get(k) is None and gmaps_beach.get(k) is not None:
            props[k] = gmaps_beach[k]
            
    # 5. Source Features snapshots
    source_features = props.get("source_features", [])
    if not isinstance(source_features, list):
        source_features = []
        
    snap_uids = {sf.get("uid") for sf in source_features if sf.get("uid")}
    if gmaps_uid not in snap_uids:
        g_snapshot = {
            "uid": gmaps_uid,
            "geometry": {
                "type": "Point",
                "coordinates": [gmaps_beach["longitude"], gmaps_beach["latitude"]]
            },
            "properties": {
                "uid": gmaps_uid,
                "name": [g_name],
                "rating": gmaps_beach.get("rating"),
                "user_ratings": gmaps_beach.get("user_ratings"),
                "category": gmaps_beach.get("category"),
                "address": gmaps_beach.get("address"),
                "phone": gmaps_beach.get("phone"),
                "website": gmaps_beach.get("website"),
                "href": g_href,
                "source": ["gmaps"],
                "source_id": [g_href]
            }
        }
        source_features.append(g_snapshot)
    props["source_features"] = source_features

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print actions but do not modify files")
    parser.add_argument("--max-distance", type=float, default=150.0, help="Maximum merging distance in meters (default: 150)")
    args = parser.parse_args()

    if not VERIFY_WIDE_OUT.exists() or not GEOJSON.exists():
        print("Required input files do not exist.")
        return

    # 1. Load data
    print("Loading data files...")
    gmaps_data = json.loads(VERIFY_WIDE_OUT.read_text(encoding="utf-8"))
    fc = json.loads(GEOJSON.read_text(encoding="utf-8"))

    deleted_set = set()
    if DELETED_GMAPS.exists():
        try:
            del_arr = json.loads(DELETED_GMAPS.read_text(encoding="utf-8"))
            deleted_set = set(del_arr)
            print(f"Loaded {len(deleted_set):,} deleted gmaps references.")
        except Exception as e:
            print(f"Warning: Failed to load deleted_gmaps: {e}")

    # 2. Extract and filter unique Google Maps beaches
    print("\nFiltering and validating wide-search Google Maps pins...")
    gmaps_beaches = []
    unique_hrefs = set()
    unique_coords = set()

    skipped_deleted = 0
    skipped_validation = 0

    for src_uid, res in gmaps_data.items():
        if not res.get("found") or not res.get("beaches"):
            continue
            
        for beach in res["beaches"]:
            href = beach.get("href")
            lat = beach.get("latitude")
            lon = beach.get("longitude")
            name_str = beach.get("name") or "Unknown Beach"
            
            if lat is None or lon is None:
                continue

            # Skip if deleted by user
            if src_uid in deleted_set or (href and href in deleted_set):
                skipped_deleted += 1
                continue

            # Run validation filter
            passed, reason = verify_beach(beach)
            if not passed:
                skipped_validation += 1
                # Log filtered item for tracing (safely encode names for Windows terminal)
                safe_name = name_str.encode('ascii', errors='replace').decode('ascii')
                safe_reason = reason.encode('ascii', errors='replace').decode('ascii')
                print(f"  [DISCARDED] '{safe_name}': {safe_reason}")
                continue

            key = href if href else f"{lat:.5f},{lon:.5f}"
            if href:
                if href in unique_hrefs:
                    continue
                unique_hrefs.add(href)
            else:
                coord_key = f"{lat:.5f},{lon:.5f}"
                if coord_key in unique_coords:
                    continue
                unique_coords.add(coord_key)
                
            hash_input = f"{name_str}_{lat:.5f}_{lon:.5f}"
            val_hash = 0
            for char in hash_input:
                val_hash = (val_hash << 5) - val_hash + ord(char)
                val_hash = val_hash & 0xFFFFFFFF
            if val_hash >= 0x80000000:
                val_hash -= 0x100000000
            hash_id = abs(val_hash) % 1000000
            uid = f"gmaps-{hash_id:06d}"
            
            gmaps_beaches.append({
                "uid": uid,
                "beach": beach,
                "coords": (lat, lon),
                "name": name_str,
                "href": href or ""
            })

    print(f"\nValidation Summary:")
    print(f"  Discarded by deleted lists: {skipped_deleted:,}")
    print(f"  Discarded by beach validation: {skipped_validation:,}")
    print(f"  Valid Google Maps beaches to process: {len(gmaps_beaches):,}")

    # Track already merged references in current.json
    merged_hrefs = set()
    merged_uids = set()
    for feat in fc["features"]:
        props = feat.get("properties", {})
        if props.get("uid"):
            merged_uids.add(props["uid"])
        if props.get("href"):
            merged_hrefs.add(props["href"])
        if props.get("source_id"):
            ids = props["source_id"] if isinstance(props["source_id"], list) else [props["source_id"]]
            for sid in ids:
                if sid and str(sid).startswith("http"):
                    merged_hrefs.add(str(sid))
        if props.get("merged_from_uids"):
            uids = props["merged_from_uids"] if isinstance(props["merged_from_uids"], list) else [props["merged_from_uids"]]
            for uid in uids:
                merged_uids.add(uid)

    merged_count = 0
    added_count = 0
    skipped_already_merged = 0

    for g in gmaps_beaches:
        uid = g["uid"]
        href = g["href"]
        
        # Skip if already merged
        if uid in merged_uids or (href and href in merged_hrefs):
            skipped_already_merged += 1
            continue
            
        g_lat, g_lon = g["coords"]
        g_name = g["name"]
        
        # Find closest match in current.json
        best_match = None
        min_dist = float("inf")
        
        for feat in fc["features"]:
            coords = feat["geometry"]["coordinates"]
            c_lon, c_lat = coords[0], coords[1]
            dist = hav(g_lat, g_lon, c_lat, c_lon)
            if dist < min_dist:
                min_dist = dist
                best_match = feat
                    
        safe_g_name = g_name.encode('ascii', errors='replace').decode('ascii')

        # 1. Close match (distance <= max_distance) -> Merge details
        if best_match and min_dist <= args.max_distance:
            c_names = best_match["properties"].get("name", [])
            if not isinstance(c_names, list):
                c_names = [c_names]
            c_name = c_names[0] if c_names else "Unnamed"
            safe_c_name = c_name.encode('ascii', errors='replace').decode('ascii')
            print(f"[MERGE] '{safe_g_name}' -> '{safe_c_name}' (distance: {min_dist:.1f}m)")
            
            if not args.dry_run:
                merge_features_data(best_match, g["beach"], uid)
                
            merged_count += 1
            merged_uids.add(uid)
            if href:
                merged_hrefs.add(href)

        # 2. Far match (distance > max_distance) -> Add as new standalone beach!
        else:
            print(f"[ADD NEW] '{safe_g_name}' at {g['coords']} (nearest beach: {min_dist:.1f}m away)")
            
            if not args.dry_run:
                # Create a new feature
                new_feat = {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [g_lon, g_lat]
                    },
                    "properties": {
                        "uid": uid,
                        "name": [g_name],
                        "rating": g["beach"].get("rating"),
                        "user_ratings": g["beach"].get("user_ratings"),
                        "category": g["beach"].get("category"),
                        "address": g["beach"].get("address"),
                        "phone": g["beach"].get("phone"),
                        "website": g["beach"].get("website"),
                        "href": href,
                        "source": ["gmaps"],
                        "source_id": [href],
                        "is_new_gmaps": True,
                        "merged_from_uids": [],
                        "source_features": []
                    }
                }
                # Capture snapshot in source_features
                g_snapshot = {
                    "uid": uid,
                    "geometry": {
                        "type": "Point",
                        "coordinates": [g_lon, g_lat]
                    },
                    "properties": {
                        "uid": uid,
                        "name": [g_name],
                        "rating": g["beach"].get("rating"),
                        "user_ratings": g["beach"].get("user_ratings"),
                        "category": g["beach"].get("category"),
                        "address": g["beach"].get("address"),
                        "phone": g["beach"].get("phone"),
                        "website": g["beach"].get("website"),
                        "href": href,
                        "source": ["gmaps"],
                        "source_id": [href]
                    }
                }
                new_feat["properties"]["source_features"].append(g_snapshot)
                fc["features"].append(new_feat)

            added_count += 1
            merged_uids.add(uid)
            if href:
                merged_hrefs.add(href)

    print("\nProcessing Results Summary:")
    print(f"  Already merged/skipped: {skipped_already_merged:,}")
    print(f"  Matched & Merged: {merged_count:,}")
    print(f"  Added as standalone beaches: {added_count:,}")

    if not args.dry_run and (merged_count > 0 or added_count > 0):
        # Create timestamped backup of current.json first
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = GEOJSON.with_name(f"current_backup_merged_wide_{stamp}.json")
        backup_path.write_text(json.dumps(fc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  Created backup of current.json at: {backup_path.name}")
        
        # Save updated current.json
        GEOJSON.write_text(json.dumps(fc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  Saved updated current.json")
    elif args.dry_run:
        print("\n  Dry-run active. No changes written to files.")

if __name__ == "__main__":
    main()
