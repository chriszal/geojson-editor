import { NextResponse } from "next/server";
import fs from "node:fs/promises";
import path from "node:path";
import { dataDir } from "../../_utils/fs";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const auditDir = path.join(dataDir, "audit");
const auditLog = path.join(auditDir, "audit.log");

export async function GET(req: Request) {
  try {
    const url = new URL(req.url);
    const limit = Math.max(1, Math.min(1000, Number(url.searchParams.get("limit") || 200)));
    const userFilter = url.searchParams.get("user") || "";
    const committedFilter = url.searchParams.get("committed"); // "1" | "0" | null

    let text = "";
    try {
      text = await fs.readFile(auditLog, "utf8");
    } catch {
      return NextResponse.json({ ok: true, items: [] });
    }

    const lines = text.trim().split("\n").filter(Boolean);
    let pick = lines.slice(-limit).map(l => { try { return JSON.parse(l); } catch { return null; } })
                   .filter(Boolean) as any[];

    if (userFilter) pick = pick.filter(x => (x.user||"") === userFilter);

    if (committedFilter === "1") pick = pick.filter(x => x.committed === true);
    else if (committedFilter === "0") pick = pick.filter(x => x.committed !== true);

    return NextResponse.json({ ok: true, items: pick });
  } catch (e: any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}
