import json
import math
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data_new"
VERIFY_WIDE_OUT = DATA / "gmaps_verification_wide.json"
GEOJSON = DATA / "current.json"

def hav(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ, dλ = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return 6_371_000.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def main():
    if not VERIFY_WIDE_OUT.exists() or not GEOJSON.exists():
        print("Required files do not exist.")
        return

    gmaps_data = json.loads(VERIFY_WIDE_OUT.read_text(encoding="utf-8"))
    fc = json.loads(GEOJSON.read_text(encoding="utf-8"))

    # Load existing beaches references
    existing_uids = set()
    existing_hrefs = set()
    for feat in fc["features"]:
        props = feat.get("properties", {})
        if props.get("uid"):
            existing_uids.add(props["uid"])
        if props.get("href"):
            existing_hrefs.add(props["href"])
        if props.get("source_id"):
            ids = props["source_id"] if isinstance(props["source_id"], list) else [props["source_id"]]
            for sid in ids:
                if sid and str(sid).startswith("http"):
                    existing_hrefs.add(str(sid))

    unique_beaches = {}
    for src_uid, res in gmaps_data.items():
        if not res.get("found") or not res.get("beaches"):
            continue
        for b in res["beaches"]:
            href = b.get("href")
            lat = b.get("latitude")
            lon = b.get("longitude")
            if href:
                unique_beaches[href] = b
            else:
                key = f"{lat:.5f},{lon:.5f}"
                unique_beaches[key] = b

    print(f"Total unique beaches found by wide-search so far: {len(unique_beaches)}")

    already_known = 0
    new_beaches = []

    for key, b in unique_beaches.items():
        href = b.get("href")
        lat = b.get("latitude")
        lon = b.get("longitude")
        name = b.get("name") or "Unknown"

        # Check if exactly matching href or in database
        if href and href in existing_hrefs:
            already_known += 1
            continue

        # Check distance to nearest existing beach
        min_dist = float("inf")
        nearest_beach_name = ""
        for feat in fc["features"]:
            coords = feat["geometry"]["coordinates"]
            dist = hav(lat, lon, coords[1], coords[0])
            if dist < min_dist:
                min_dist = dist
                names = feat["properties"].get("name", ["Unnamed"])
                if isinstance(names, list):
                    nearest_beach_name = names[0] if len(names) > 0 else "Unnamed"
                elif isinstance(names, str):
                    nearest_beach_name = names
                else:
                    nearest_beach_name = "Unnamed"

        if min_dist <= 150.0:
            already_known += 1
        else:
            new_beaches.append({
                "name": name,
                "distance_to_nearest": min_dist,
                "nearest_existing": nearest_beach_name,
                "coords": (lat, lon)
            })

    print(f"Already in database (matching href or within 150m): {already_known}")
    print(f"Entirely new beaches (more than 150m away from any existing pin): {len(new_beaches)}")
    
    if new_beaches:
        print("\nExamples of entirely new beaches:")
        for idx, nb in enumerate(new_beaches[:10]):
            safe_name = nb["name"].encode('ascii', errors='replace').decode('ascii')
            safe_near = nb["nearest_existing"].encode('ascii', errors='replace').decode('ascii')
            print(f"  {idx+1}. '{safe_name}' at {nb['coords']} (nearest: '{safe_near}' {nb['distance_to_nearest']:.1f}m away)")

if __name__ == "__main__":
    main()
