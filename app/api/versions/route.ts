import { NextResponse } from "next/server";
import fs from "node:fs/promises";
import path from "node:path";
import zlib from "node:zlib";
import { versionsDir } from "../_utils/fs";
export const runtime = "nodejs";

export async function GET() {
  try {
    await fs.mkdir(versionsDir, { recursive: true });
    const files = await fs.readdir(versionsDir);
    const items: any[] = [];

    for (const f of files) {
      const full = path.join(versionsDir, f);
      const stat = await fs.stat(full);
      if (!stat.isFile()) continue;

      let buf = await fs.readFile(full);
      let json: any;
      try {
        if (f.endsWith(".gz")) {
          buf = zlib.gunzipSync(buf);
        }
        json = JSON.parse(buf.toString("utf8"));
      } catch {
        continue;
      }

      const id = f.replace(/\.json(\.gz)?$/,"");
      const message = json?.message || "";
      const user = json?.user || "";
      const features = json?.featureCollection?.features?.length || 0;

      items.push({
        id,
        ts: json?.ts || (new Date(id.replace(/-/g,":").replace("T","T")).getTime() || stat.mtimeMs),
        message,
        user,
        size: stat.size,
        features
      });
    }

    items.sort((a,b)=> b.ts - a.ts);
    return NextResponse.json({ ok:true, items });
  } catch (e:any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}
