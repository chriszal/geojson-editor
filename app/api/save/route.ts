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
