#!/usr/bin/env python3
import json
import math
import os
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

def main():
    if not VERIFY_OUT.exists():
        print(f"Error: {VERIFY_OUT} does not exist.")
        return

    print("Loading datasets...")
    gmaps_data = json.loads(VERIFY_OUT.read_text(encoding="utf-8"))
    fc = json.loads(GEOJSON.read_text(encoding="utf-8"))
    
    # Map UID to coordinates and names
    uid_info = {}
    for feat in fc["features"]:
        uid = feat.get("properties", {}).get("uid")
        if uid:
            uid_info[uid] = {
                "coords": (feat["geometry"]["coordinates"][1], feat["geometry"]["coordinates"][0]),
                "name": feat["properties"].get("name", [])
            }

    # Extract all unique verified beaches from current database
    unique_verified = {}
    for res in gmaps_data.values():
        if res.get("found") and res.get("beaches"):
            for b in res["beaches"]:
                href = b.get("href")
                if href:
                    unique_verified[href] = b
                else:
                    key = f"{b['latitude']:.5f},{b['longitude']:.5f}"
                    unique_verified[key] = b

    print(f"Loaded {len(unique_verified):,} unique verified beaches.")
    
    # Identify "not found" points
    not_found_uids = [uid for uid, res in gmaps_data.items() if not res.get("found")]
    print(f"Found {len(not_found_uids):,} 'not found' points out of {len(gmaps_data):,} total points.")
    
    resolved_count = 0
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    for uid in not_found_uids:
        if uid not in uid_info:
            continue
        
        info = uid_info[uid]
        lat, lon = info["coords"]
        
        # Find the closest verified beach in the database
        min_dist = float("inf")
        best_beach = None
        
        for b in unique_verified.values():
            dist = hav(lat, lon, b["latitude"], b["longitude"])
            if dist < min_dist:
                min_dist = dist
                best_beach = b
                
        # If it is within 800m, resolve it!
        if min_dist <= 800.0 and best_beach is not None:
            # Clone and update distance
            cloned = dict(best_beach)
            cloned["distance_m"] = round(min_dist, 2)
            
            gmaps_data[uid] = {
                "method": "spatial_offshore_resolution",
                "found": True,
                "beaches": [cloned],
                "checked_at": now_str,
                "point_name": info["name"]
            }
            resolved_count += 1

    print(f"Successfully resolved {resolved_count:,} offshore points in-memory.")
    
    if resolved_count > 0:
        VERIFY_OUT.write_text(json.dumps(gmaps_data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Updated {VERIFY_OUT}")
        
if __name__ == "__main__":
    main()
