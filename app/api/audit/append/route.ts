import { NextResponse } from "next/server";
import fs from "node:fs/promises";
import path from "node:path";
import { dataDir } from "../../_utils/fs";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const auditDir = path.join(dataDir, "audit");
const auditLog = path.join(auditDir, "audit.log");

export async function POST(req: Request) {
  try {
    const body = await req.json();
    // Accept single entry or array
    const entries = Array.isArray(body) ? body : [body];

    await fs.mkdir(auditDir, { recursive: true });
    const lines = entries.map((e: any) => JSON.stringify(e) + "\n").join("");
    await fs.appendFile(auditLog, lines, "utf8");

    return NextResponse.json({ ok: true, n: entries.length });
  } catch (e: any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}
