import { NextResponse } from "next/server";
import fs from "node:fs/promises";
import path from "node:path";
import { dataDir, versionsDir, currentPath } from "../_utils/fs";
export const runtime = "nodejs";

export async function POST(req: Request) {
  try {
    const form = await req.formData();
    const file = form.get("file") as File | null;
    if (!file) return NextResponse.json({ error: "Missing file" }, { status: 400 });
    const text = await file.text();
    let parsed: any;
    try { parsed = JSON.parse(text); } catch { return NextResponse.json({ error: "File is not valid JSON" }, { status: 400 }); }
    if (parsed?.type !== "FeatureCollection") {
      return NextResponse.json({ error: "JSON must be a GeoJSON FeatureCollection" }, { status: 400 });
    }
    await fs.mkdir(versionsDir, { recursive: true });
    await fs.mkdir(dataDir, { recursive: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const versionFile = path.join(versionsDir, `${stamp}.json`);
    await fs.writeFile(versionFile, JSON.stringify({ message: "upload", featureCollection: parsed }, null, 2), "utf8");
    await fs.writeFile(currentPath, JSON.stringify(parsed, null, 2), "utf8");
    return NextResponse.json({ ok: true, versionId: stamp });
  } catch (e: any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}