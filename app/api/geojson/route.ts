import { NextResponse } from "next/server";
import fs from "node:fs/promises";
import path from "node:path";
import { dataDir, currentPath } from "../_utils/fs";

export const runtime = "nodejs";

// In-memory cache for the Google Maps features to avoid heavy parsing on every request
let cachedGmapsFeatures: any[] = [];
let cachedGmapsMtime: number = 0;
let cachedGmapsWideMtime: number = 0;
let cachedDeletedSet = new Set<string>();
let cachedDeletedMtime: number = 0;

const BAD_GMAPS_TITLE_RE = /(^|[\W_])(taverns?|tavernas?|tavernes?|houses?|homes?|hotels?|resorts?|bars?|clubs?|cafes?|kafes?|villas?|suites?|rooms?|apartments?|studios?|cottages?|campings?|camps?|parkings?|ports?|marinas?|yachts?|churches?|monasteries?|rentals?|cars?|motors?|surfs?|divings?|sports?|lagoon?|laguna?|lagun?)(?=$|[\W_])|ταβερν|ξενοδοχ|καφέ|μπαρ|καντίν|ενοικιαζ|ξενών|δωμάτ|βίλ|εστιατόρ|ψαροταβ|ψησταρ|φαγητ|λιμάν|μαρίν|πάρκινγ|ναός|εκκλησ|ξωκλήσ|μοναστήρ|λιμνοθάλασ/i;

function hasBadGmapsTitle(name: unknown) {
  const names = Array.isArray(name) ? name : [name];
  return names.some((item) => BAD_GMAPS_TITLE_RE.test(String(item || "")));
}

export async function GET() {
  try {
    await fs.mkdir(dataDir, { recursive: true });

    // 1. Read current.json
    let geojson: any = { type: "FeatureCollection", features: [] };
    try {
      const raw = await fs.readFile(currentPath, "utf8");
      geojson = JSON.parse(raw);
    } catch (e) {
      // If current.json doesn't exist yet, start empty
    }

    // 2. Read deleted_gmaps.json
    const deletedGmapsPath = path.join(dataDir, "deleted_gmaps.json");
    try {
      const delStats = await fs.stat(deletedGmapsPath);
      if (delStats.mtimeMs !== cachedDeletedMtime) {
        const delRaw = await fs.readFile(deletedGmapsPath, "utf8");
        const delArr = JSON.parse(delRaw);
        cachedDeletedSet = new Set(delArr);
        cachedDeletedMtime = delStats.mtimeMs;
      }
    } catch (e) {
      // If file doesn't exist, we keep the empty/previous set
    }

    // 3. Read and parse gmaps_verification.json and gmaps_verification_wide.json with caching
    const gmapsPath = path.join(dataDir, "gmaps_verification.json");
    const gmapsWidePath = path.join(dataDir, "gmaps_verification_wide.json");
    
    let gmapsMtime = 0;
    let gmapsWideMtime = 0;
    
    try {
      const stat = await fs.stat(gmapsPath);
      gmapsMtime = stat.mtimeMs;
    } catch (e) {}
    
    try {
      const stat = await fs.stat(gmapsWidePath);
      gmapsWideMtime = stat.mtimeMs;
    } catch (e) {}

    if (gmapsMtime !== cachedGmapsMtime || gmapsWideMtime !== cachedGmapsWideMtime) {
      const tempFeatures: any[] = [];
      const uniqueHrefs = new Set<string>();
      const uniqueCoords = new Set<string>();

      const filesToParse = [
        { path: gmapsPath, exists: gmapsMtime > 0 },
        { path: gmapsWidePath, exists: gmapsWideMtime > 0 }
      ];

      for (const fileInfo of filesToParse) {
        if (!fileInfo.exists) continue;
        try {
          const raw = await fs.readFile(fileInfo.path, "utf8");
          const data = JSON.parse(raw);
          
          for (const [srcUid, result] of Object.entries(data)) {
            const resObj = result as any;
            if (!resObj.found || !resObj.beaches) continue;

            for (const beach of resObj.beaches) {
              const href = beach.href;
              const lat = beach.latitude;
              const lon = beach.longitude;
              if (lat === undefined || lon === undefined) continue;

              // Generate unique key
              const key = href || `${lat.toFixed(5)},${lon.toFixed(5)}`;
              if (href) {
                if (uniqueHrefs.has(href)) continue;
                uniqueHrefs.add(href);
              } else {
                const coordKey = `${lat.toFixed(5)},${lon.toFixed(5)}`;
                if (uniqueCoords.has(coordKey)) continue;
                uniqueCoords.add(coordKey);
              }

              // Stable hash/id based on name and coordinates
              const nameStr = beach.name || "Unknown Beach";
              if (hasBadGmapsTitle(nameStr)) continue;

              let hash = 0;
              const hashInput = `${nameStr}_${lat.toFixed(5)}_${lon.toFixed(5)}`;
              for (let i = 0; i < hashInput.length; i++) {
                hash = (hash << 5) - hash + hashInput.charCodeAt(i);
                hash |= 0;
              }
              const hashId = Math.abs(hash) % 1000000;
              const uid = `gmaps-${String(hashId).padStart(6, "0")}`;

              // Create feature without reviews to keep size lightweight
              tempFeatures.push({
                type: "Feature",
                geometry: {
                  type: "Point",
                  coordinates: [lon, lat]
                },
                properties: {
                  uid,
                  name: [nameStr],
                  rating: beach.rating,
                  user_ratings: beach.user_ratings,
                  category: beach.category,
                  address: beach.address,
                  phone: beach.phone,
                  website: beach.website,
                  href: beach.href || "",
                  source: ["gmaps"],
                  source_id: [beach.href || ""],
                  is_gmaps: true,
                  merged_from_uids: []
                }
              });
            }
          }
        } catch (e) {
          console.error(`Error parsing file ${fileInfo.path}:`, e);
        }
      }

      cachedGmapsFeatures = tempFeatures;
      cachedGmapsMtime = gmapsMtime;
      cachedGmapsWideMtime = gmapsWideMtime;
    }

    // 4. Collect UIDs and Hrefs in current.json to avoid duplicates
    const existingUids = new Set<string>();
    const existingGmapsHrefs = new Set<string>();
    for (const feat of geojson.features) {
      if (feat.properties?.uid) {
        existingUids.add(feat.properties.uid);
      }
      if (feat.properties?.href) {
        existingGmapsHrefs.add(String(feat.properties.href));
      }
      if (feat.properties?.source_id) {
        const ids = Array.isArray(feat.properties.source_id) ? feat.properties.source_id : [feat.properties.source_id];
        for (const id of ids) {
          if (id && String(id).startsWith("http")) {
            existingGmapsHrefs.add(String(id));
          }
        }
      }
    }

    // 5. Filter dynamic features against existing and deleted sets
    const filteredGmaps = cachedGmapsFeatures.filter(f => {
      const uid = f.properties.uid;
      const href = f.properties.href;

      // Skip if deleted
      if (cachedDeletedSet.has(uid) || (href && cachedDeletedSet.has(href))) {
        return false;
      }

      // Skip if already in current.json (direct or merged)
      if (existingUids.has(uid) || (href && existingGmapsHrefs.has(href))) {
        return false;
      }

      return true;
    });

    return NextResponse.json({
      type: "FeatureCollection",
      features: [...geojson.features, ...filteredGmaps]
    });
  } catch (e: any) {
    return NextResponse.json({ type: "FeatureCollection", features: [] });
  }
}
