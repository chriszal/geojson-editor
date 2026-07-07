import json
import math
from pathlib import Path

ROOT = Path(__file__).parent.parent
GEOJSON = ROOT / "data_new" / "current.json"

def hav(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return 6371000.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

fc = json.loads(GEOJSON.read_text(encoding="utf-8"))
all_beaches = []
for feat in fc["features"]:
    uid = feat.get("properties", {}).get("uid")
    if not uid: continue
    coords = feat["geometry"]["coordinates"]
    all_beaches.append({
        "uid": uid,
        "lat": coords[1],
        "lon": coords[0]
    })

all_beaches = sorted(all_beaches, key=lambda x: (x["lat"], x["lon"]))

for spacing in [8000, 10000, 12000, 15000, 20000]:
    grid = []
    for b in all_beaches:
        too_close = False
        for gp in grid:
            if hav(b["lat"], b["lon"], gp["lat"], gp["lon"]) < spacing:
                too_close = True
                break
        if not too_close:
            grid.append(b)
    print(f"Spacing: {spacing/1000}km -> {len(grid)} search points")
