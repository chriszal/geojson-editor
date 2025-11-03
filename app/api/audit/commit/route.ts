import { NextResponse } from "next/server";
import fs from "node:fs/promises";
import path from "node:path";
import { dataDir } from "../../_utils/fs";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const auditDir = path.join(dataDir, "audit");
const auditLog = path.join(auditDir, "audit.log");
const tmpLog   = path.join(auditDir, "audit.tmp");

export async function POST(req: Request) {
  try {
    const { sessionId } = await req.json();
    if (!sessionId) {
      return NextResponse.json({ error: "sessionId required" }, { status: 400 });
    }

    await fs.mkdir(auditDir, { recursive: true });

    // Read current log (small JSONL lines)
    let text = "";
    try {
      text = await fs.readFile(auditLog, "utf8");
    } catch {
      return NextResponse.json({ ok: true, updated: 0 });
    }

    const lines = text.split("\n");
    let updated = 0;

    // Rewrite into a temp file atomically
    const out: string[] = [];
    for (const line of lines) {
      if (!line.trim()) { out.push(line); continue; }
      try {
        const obj = JSON.parse(line);
        if (obj && obj.sessionId === sessionId && obj.committed !== true) {
          obj.committed = true;
          updated++;
        }
        out.push(JSON.stringify(obj));
      } catch {
        // keep corrupted line as-is
        out.push(line);
      }
    }

    const outText = out.join("\n");
    await fs.writeFile(tmpLog, outText, "utf8");
    await fs.rename(tmpLog, auditLog);

    return NextResponse.json({ ok: true, updated });
  } catch (e: any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}
