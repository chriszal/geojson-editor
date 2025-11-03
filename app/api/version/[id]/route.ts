import { NextResponse } from "next/server";
export const runtime = "nodejs";

type Client = {
  id: string;
  username: string;
  ipMasked: string;
  since: number;
  controller: ReadableStreamDefaultController;
};

const hub: { clients: Map<string, Client> } = (global as any).__presence_hub || { clients: new Map() };
(global as any).__presence_hub = hub;

function maskIp(ip: string) {
  // very simple mask: last octet -> xxx, or last hextet chunk -> xxxx
  if (!ip) return "unknown";
  if (ip.includes(".")) {
    const p = ip.split(".");
    if (p.length === 4) p[3] = "xxx";
    return p.join(".");
  }
  if (ip.includes(":")) {
    const p = ip.split(":");
    p[p.length - 1] = "xxxx";
    return p.join(":");
  }
  return "unknown";
}

function broadcast(type: "state"|"join"|"leave") {
  const users = Array.from(hub.clients.values()).map(({ id, username, ipMasked, since }) => ({ id, username, ipMasked, since }));
  const data = `data: ${JSON.stringify({ type, users })}\n\n`;
  for (const c of hub.clients.values()) {
    try { c.controller.enqueue(data); } catch {}
  }
}

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const username = (searchParams.get("u") || "guest").slice(0, 40);
  const ipRaw = (req.headers.get("x-forwarded-for") || req.headers.get("x-real-ip") || "").split(",")[0].trim();
  const ipMasked = maskIp(ipRaw);

  const stream = new ReadableStream({
    start(controller) {
      const id = crypto.randomUUID();
      const client: Client = { id, username, ipMasked, since: Date.now(), controller };
      hub.clients.set(id, client);

      // SSE headers & initial hello
      controller.enqueue(`retry: 2000\n`); // reconnection delay hint
      controller.enqueue(`data: ${JSON.stringify({ type:"state", users: Array.from(hub.clients.values()).map(u=>({ id:u.id, username:u.username, ipMasked:u.ipMasked, since:u.since })) })}\n\n`);
      broadcast("join");

      const close = () => {
        if (hub.clients.delete(id)) broadcast("leave");
        try { controller.close(); } catch {}
      };

      // terminate when client disconnects
      (req as any).signal?.addEventListener?.("abort", close);

      // keepalive ping
      const iv = setInterval(() => {
        try { controller.enqueue(`event: ping\ndata: {}\n\n`); } catch {}
      }, 15000);

      // safety close on GC
      (controller as any).__cleanup = () => { clearInterval(iv); close(); };
    },
    cancel() {
      // @ts-ignore
      const cleanup = (this as any).__cleanup;
      if (cleanup) cleanup();
    }
  });

  return new NextResponse(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      "Connection": "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}
