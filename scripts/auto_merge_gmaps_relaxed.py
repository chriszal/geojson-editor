#!/usr/bin/env python3
import json
import math
import os
import re
import argparse
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data_new"
VERIFY_OUT = DATA / "gmaps_verification.json"
GEOJSON = DATA / "current.json"

def hav(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ, dλ = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return 6_371_000.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def normalize_name(name: str) -> str:
    name = name.lower()
    accents = {
        'ά': 'α', 'έ': 'ε', 'ή': 'η', 'ί': 'ι', 'ΐ': 'ι', 'ϊ': 'ι',
        'ό': 'ο', 'ύ': 'υ', 'ΰ': 'υ', 'ϋ': 'υ', 'ώ': 'ω'
    }
    for acc, plain in accents.items():
        name = name.replace(acc, plain)
    name = re.sub(r'[^\w\s]', ' ', name)
    return name.strip()

def get_distinct_words(normalized_name: str) -> set[str]:
    words = normalized_name.split()
    stop_words = {
        'παραλια', 'παραλιας', 'beach', 'coast', 'bay', 'port', 
        'limanaki', 'λιμανακι', 'λιμανι', 'μαρινα', 'marina', 
        'of', 'the', 'and', 'at', 'in', 'στο', 'στη', 'της', 'του',
        'super', 'club', 'bar', 'restaurant', 'tavern', 'resort', 'hotel'
    }
    return {w for w in words if len(w) >= 3 and w not in stop_words}

def words_match_greek(w1: str, w2: str) -> bool:
    if w1 == w2:
        return True
    if len(w1) < 4 or len(w2) < 4:
        return False
    min_len = min(len(w1), len(w2))
    prefix_len = max(4, min_len - 2)
    return w1[:prefix_len] == w2[:prefix_len]

def names_are_similar(name1: str, name2: str) -> bool:
    n1 = normalize_name(name1)
    n2 = normalize_name(name2)
    
    if n1 in n2 or n2 in n1:
        return True
        
    words1 = get_distinct_words(n1)
    words2 = get_distinct_words(n2)
    
    for w1 in words1:
        for w2 in words2:
            if words_match_greek(w1, w2):
                return True
    return False

def merge_features_data(existing_feat, gmaps_beach, gmaps_uid):
    props = existing_feat["properties"]
    
    # 1. Merge Names
    existing_names = props.get("name", [])
    if not isinstance(existing_names, list):
        existing_names = [existing_names]
        
    g_name = gmaps_beach.get("name")
    if g_name and g_name not in existing_names:
        # Check case insensitively
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
        
    # Check if this snapshot is already inside
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

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print matches but do not modify current.json")
    parser.add_argument("--max-distance", type=float, default=150.0, help="Maximum matching distance in meters (default: 150)")
    args = parser.parse_args()

    if not VERIFY_OUT.exists() or not GEOJSON.exists():
        print("Error: Required data files do not exist.")
        return

    print("Loading data...")
    gmaps_data = json.loads(VERIFY_OUT.read_text(encoding="utf-8"))
    fc = json.loads(GEOJSON.read_text(encoding="utf-8"))
    
    # Extract unique gmaps beaches
    gmaps_beaches = []
    unique_hrefs = set()
    unique_coords = set()
    
    for src_uid, res in gmaps_data.items():
        if not res.get("found") or not res.get("beaches"):
            continue
            
        for beach in res["beaches"]:
            href = beach.get("href")
            lat = beach.get("latitude")
            lon = beach.get("longitude")
            if lat is None or lon is None:
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
                
            # Generate UID matching route.ts
            name_str = beach.get("name") or "Unknown Beach"
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

    print(f"Loaded {len(fc['features']):,} existing beaches from current.json.")
    print(f"Loaded {len(gmaps_beaches):,} unique Google Maps beaches.")

    # Track already merged gmaps hrefs/uids in current.json
    merged_hrefs = set()
    merged_uids = set()
    for feat in fc["features"]:
        props = feat.get("properties", {})
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
        
        # Find closest match in current.json (regardless of name matching)
        best_match = None
        min_dist = float("inf")
        
        for feat in fc["features"]:
            coords = feat["geometry"]["coordinates"]
            c_lon, c_lat = coords[0], coords[1]
            
            dist = hav(g_lat, g_lon, c_lat, c_lon)
            if dist < min_dist:
                min_dist = dist
                best_match = feat
                    
        # If the closest beach is within the threshold, merge it!
        if best_match and min_dist <= args.max_distance:
            c_names = best_match["properties"].get("name", [])
            if not isinstance(c_names, list):
                c_names = [c_names]
            c_name = c_names[0] if c_names else "Unnamed"
            
            # Check if this was a name match or just a spatial proximity match
            is_name_match = any(names_are_similar(c_n, g_name) for c_n in c_names)
            match_type = "NAME MATCH" if is_name_match else "PROXIMITY"
            
            # Safe print names
            safe_g_name = g_name.encode('ascii', errors='replace').decode('ascii')
            safe_c_name = c_name.encode('ascii', errors='replace').decode('ascii')
            print(f"[{match_type}] Merge '{safe_g_name}' -> '{safe_c_name}' (distance: {min_dist:.1f}m)")
            
            if not args.dry_run:
                merge_features_data(best_match, g["beach"], uid)
                
            merged_count += 1
            # Add to local merged sets so we don't merge it again
            merged_uids.add(uid)
            if href:
                merged_hrefs.add(href)

    print("\nSummary:")
    print(f"  Already merged/skipped: {skipped_already_merged:,}")
    print(f"  Newly matched & merged: {merged_count:,}")
    
    if not args.dry_run and merged_count > 0:
        # Create a backup of current.json
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = GEOJSON.with_name(f"current_backup_{stamp}.json")
        backup_path.write_text(json.dumps(fc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  Created backup of current.json at: {backup_path}")
        
        # Save updated current.json
        GEOJSON.write_text(json.dumps(fc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  Saved updated current.json")
    elif args.dry_run:
        print("  Dry-run active. No changes written to disk.")

if __name__ == "__main__":
    main()
