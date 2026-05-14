import path from "node:path";
import fs from "node:fs/promises";
import { dataDir } from "../../../_utils/fs";
export const runtime = "nodejs";

const TILE_DIR = path.join(dataDir, "tile_cache");

export async function GET(
  _req: Request,
  context: { params: Promise<{ id: string }> }
) {
  try {
    const { id } = await context.params;
    // Sanitise: only allow alphanumeric, underscores, hyphens
    const safe = id.replace(/[^a-zA-Z0-9_-]/g, "");
    const filePath = path.join(TILE_DIR, `${safe}.jpg`);
    const buf = await fs.readFile(filePath);
    return new Response(buf, {
      headers: {
        "Content-Type": "image/jpeg",
        "Cache-Control": "public, max-age=86400, immutable",
      },
    });
  } catch {
    return new Response("Not found", { status: 404 });
  }
}
