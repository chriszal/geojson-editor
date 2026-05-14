import { NextResponse } from "next/server";
import fs from "node:fs/promises";
import path from "node:path";
import { dataDir } from "../../_utils/fs";
export const runtime = "nodejs";

const CHANGES_PATH = path.join(dataDir, "proposed_changes.json");

export async function GET() {
  try {
    const raw = await fs.readFile(CHANGES_PATH, "utf-8");
    return NextResponse.json(JSON.parse(raw));
  } catch {
    return NextResponse.json({ meta: null, changes: [] });
  }
}

export async function PATCH(req: Request) {
  try {
    const { id, status } = await req.json();
    if (!id || !status) {
      return NextResponse.json({ error: "id and status are required" }, { status: 400 });
    }
    const raw = await fs.readFile(CHANGES_PATH, "utf-8");
    const data = JSON.parse(raw);
    const change = (data.changes as any[]).find((c: any) => c.id === id);
    if (!change) {
      return NextResponse.json({ error: "change not found" }, { status: 404 });
    }
    change.status = status;
    change.decided_at = new Date().toISOString();
    // Recount meta
    const changes: any[] = data.changes;
    data.meta.pending_review  = changes.filter((c: any) => c.status === "pending_review").length;
    data.meta.auto_approved   = changes.filter((c: any) => c.status === "auto_approved").length;
    data.meta.approved        = changes.filter((c: any) => c.status === "approved").length;
    data.meta.rejected        = changes.filter((c: any) => c.status === "rejected").length;
    data.meta.phase2_pending  = changes.filter((c: any) => c.phase === 2 && c.status === "pending_review").length;
    await fs.writeFile(CHANGES_PATH, JSON.stringify(data, null, 2), "utf-8");
    return NextResponse.json({ ok: true });
  } catch (e: any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}
