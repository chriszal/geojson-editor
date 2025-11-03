import { NextResponse } from "next/server";
import fs from "node:fs/promises";
import { dataDir, currentPath } from "../_utils/fs";
export const runtime = "nodejs";

export async function GET() {
  try {
    await fs.mkdir(dataDir, { recursive: true });
    const raw = await fs.readFile(currentPath, "utf8");
    return NextResponse.json(JSON.parse(raw));
  } catch (e: any) {
    return NextResponse.json({ type: "FeatureCollection", features: [] });
  }
}
