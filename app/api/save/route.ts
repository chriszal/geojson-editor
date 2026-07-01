import { NextResponse } from "next/server";
import fs from "node:fs/promises";
import path from "node:path";
import zlib from "node:zlib";
import { dataDir, versionsDir, currentPath } from "../_utils/fs";
export const runtime = "nodejs";

export async function POST(req: Request) {
  try {
    const { featureCollection, message, user } = await req.json();
    if (!featureCollection || featureCollection.type !== "FeatureCollection") {
      return NextResponse.json({ error: "Invalid FeatureCollection" }, { status: 400 });
    }

    // 1. Detect deleted gmaps pins and save them to deleted_gmaps.json
    try {
      const gmapsPath = path.join(dataDir, "gmaps_verification.json");
      const deletedGmapsPath = path.join(dataDir, "deleted_gmaps.json");

      // Load existing deleted set
      let deletedList = new Set<string>();
      try {
        const delRaw = await fs.readFile(deletedGmapsPath, "utf8");
        const delArr = JSON.parse(delRaw);
        deletedList = new Set(delArr);
      } catch {}

      // Get the set of UIDs and Hrefs in the incoming saved FeatureCollection
      const incomingUids = new Set<string>();
      const incomingHrefs = new Set<string>();
      for (const feat of featureCollection.features) {
        if (feat.properties?.uid) incomingUids.add(feat.properties.uid);
        if (feat.properties?.href) incomingHrefs.add(feat.properties.href);
        if (feat.properties?.source_id) {
          const ids = Array.isArray(feat.properties.source_id) ? feat.properties.source_id : [feat.properties.source_id];
          for (const id of ids) {
            if (id && String(id).startsWith("http")) {
              incomingHrefs.add(String(id));
            }
          }
        }
      }

      // Load all possible gmaps pins from gmaps_verification.json to see if any are missing
      let gmapsRaw = "";
      try {
        gmapsRaw = await fs.readFile(gmapsPath, "utf8");
      } catch {}

      if (gmapsRaw) {
        const gmapsData = JSON.parse(gmapsRaw);
        let changed = false;

        for (const [srcUid, result] of Object.entries(gmapsData)) {
          const resObj = result as any;
          if (!resObj.found || !resObj.beaches) continue;

          for (const beach of resObj.beaches) {
            const href = beach.href;
            const lat = beach.latitude;
            const lon = beach.longitude;
            if (lat === undefined || lon === undefined) continue;

            // Generate the exact same UID
            const nameStr = beach.name || "Unknown Beach";
            let hash = 0;
            const hashInput = `${nameStr}_${lat.toFixed(5)}_${lon.toFixed(5)}`;
            for (let i = 0; i < hashInput.length; i++) {
              hash = (hash << 5) - hash + hashInput.charCodeAt(i);
              hash |= 0;
            }
            const hashId = Math.abs(hash) % 1000000;
            const uid = `gmaps-${String(hashId).padStart(6, "0")}`;

            // If this gmaps pin is NOT in the incoming save payload, it was deleted!
            // But only if its href is also not merged into any other feature.
            if (!incomingUids.has(uid) && (!href || !incomingHrefs.has(href))) {
              const key = href || uid;
              if (!deletedList.has(key)) {
                deletedList.add(key);
                changed = true;
              }
            }
          }
        }

        if (changed) {
          await fs.writeFile(deletedGmapsPath, JSON.stringify(Array.from(deletedList), null, 2), "utf8");
        }
      }
    } catch (e) {
      console.error("Error tracking deleted gmaps pins:", e);
    }

    await fs.mkdir(versionsDir, { recursive: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");

    // include timestamp + user in the saved payload
    const payload = { ts: Date.now(), message: message || "", user: user || "guest", featureCollection };

    const versionFile = path.join(versionsDir, `${stamp}.json.gz`);
    const gz = zlib.gzipSync(Buffer.from(JSON.stringify(payload)));
    await fs.writeFile(versionFile, gz);

    await fs.mkdir(dataDir, { recursive: true });
    await fs.writeFile(currentPath, JSON.stringify(featureCollection, null, 2), "utf8");

    return NextResponse.json({ ok: true, versionId: stamp });
  } catch (e: any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}
