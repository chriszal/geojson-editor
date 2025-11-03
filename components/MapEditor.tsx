"use client";

import React, {
  useEffect, useMemo, useRef, useState, useCallback
} from "react";
import {
  MapContainer, TileLayer, Marker, Popup, useMapEvents
} from "react-leaflet";
import L, { Map as LeafletMap } from "leaflet";
import Supercluster from "supercluster";
import FileUploader from "./FileUploader";
import "leaflet/dist/leaflet.css";
import "./map-editor.css";

/* -------------------- Leaflet icon fix -------------------- */
const DefaultIcon = L.icon({
  iconUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png",
  iconRetinaUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png",
  shadowUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png",
  iconSize: [25, 41],
  iconAnchor: [12, 41],
});
(L.Marker.prototype as any).options.icon = DefaultIcon;

/* -------------------- Types -------------------- */
type Properties = Record<string, any> & {
  uid?: string;
  name?: string[];
  access_id?: any[];
  type_id?: any[];
  beach_org?: any[];
  depth_id?: any[];
  beach_amea?: any[];
  purpose?: any[];
  area_size?: number[];
  tags?: Record<string, any>;
  source?: any[] | string;
  source_id?: any[] | string;
  merged_from_uids?: string[];
};
type Feature = {
  type: "Feature";
  geometry: { type: "Point"; coordinates: [number, number] };
  properties: Properties;
};
type FC = { type: "FeatureCollection"; features: Feature[] };
type ChangeType = "merge" | "move" | "delete" | "edit" | "create";
type ChangeEntry = {
  id: string;
  ts: number;
  type: ChangeType;
  summary: string;
  before: Feature[];
  after: Feature[];
  expanded?: boolean;
};

/* -------------------- Utils -------------------- */
function haversineMeters(a: [number, number], b: [number, number]) {
  const [lng1, lat1] = a, [lng2, lat2] = b;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const R = 6371000;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const s = Math.sin(dLat/2)**2 + Math.cos(toRad(lat1))*Math.cos(toRad(lat2))*Math.sin(dLng/2)**2;
  return 2 * R * Math.atan2(Math.sqrt(s), Math.sqrt(1 - s));
}
const asList = (v: any): any[] => Array.isArray(v) ? v : (v == null ? [] : [v]);
const dedupe = (arr: any[]) => Array.from(new Map(arr.map(x => [JSON.stringify(x), x])).values());
const toFloatList = (arr: any[]): number[] => dedupe(asList(arr)).map(x => Number(x)).filter(Number.isFinite);
const isEmptyVal = (v: any) => v == null || (Array.isArray(v) ? v.length === 0 : String(v).trim() === "");

/** Merge two feature property bags */
const mergeProps = (A: Properties, B: Properties): Properties => {
  const out: Properties = { ...A };
  const union = (key: keyof Properties) => {
    const merged = dedupe([...asList(A[key]), ...asList(B[key])]);
    (out as any)[key] = merged;
  };
  ["name","access_id","type_id","beach_org","depth_id","beach_amea","purpose"].forEach(k => union(k as any));
  out.area_size = dedupe([...(A.area_size||[]), ...(B.area_size||[])]).map(Number).filter(Number.isFinite);
  out.tags = { ...(A.tags||{}), ...(B.tags||{}) };
  out.source = dedupe([...asList(A.source), ...asList(B.source)]);
  out.source_id = dedupe([...asList(A.source_id), ...asList(B.source_id)]);
  out.merged_from_uids = dedupe([...(A.merged_from_uids||[]), ...(B.merged_from_uids||[]), ...(A.uid?[A.uid]:[]), ...(B.uid?[B.uid]:[])]);
  return out;
};

/* -------------------- Clustering -------------------- */
const CLUSTER_RADIUS_PX = 50;
const DETAIL_ZOOM = 13;
/* --------- UUID helper that works everywhere --------- */
function safeRandomUUID(): string {
  // Modern browsers + Node >= 14.17
  // (Works when `crypto.randomUUID` exists)
  // @ts-ignore
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    // @ts-ignore
    return crypto.randomUUID();
  }
  // RFC4122 v4 via getRandomValues (widely supported)
  const g = (typeof self !== "undefined" ? self : globalThis) as any;
  if (g?.crypto?.getRandomValues) {
    const buf = new Uint8Array(16);
    g.crypto.getRandomValues(buf);
    // Per RFC 4122: set version and variant bits
    buf[6] = (buf[6] & 0x0f) | 0x40;
    buf[8] = (buf[8] & 0x3f) | 0x80;
    const toHex = (n: number) => n.toString(16).padStart(2, "0");
    const b = Array.from(buf, toHex).join("");
    return `${b.slice(0,8)}-${b.slice(8,12)}-${b.slice(12,16)}-${b.slice(16,20)}-${b.slice(20)}`;
  }
  // Last-ditch fallback (not RFC-strong, but avoids crashes)
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2,10)}-${Math.random().toString(36).slice(2,10)}`;
}

/* -------------------- UI Chip -------------------- */
const Chip = ({ color, children }: { color: string, children: React.ReactNode }) => (
  <span
    style={{
      display: "inline-flex", alignItems: "center",
      padding: "2px 8px", borderRadius: 999,
      fontSize: 12, fontWeight: 600, color: "#fff",
      background: color, gap: 6
    }}
  >
    {children}
  </span>
);

/* -------------------- Presence types -------------------- */
type PresenceUser = { id: string; username: string; ipMasked: string; since: number };

/* -------------------- AUDIT persistence helpers -------------------- */
type AuditEntry = {
  ts: number;
  user: string;
  type: string;
  summary: string;
  uids: string[];
  before?: Array<{ uid:string; name?:string; coords?:[number,number] }>;
  after?:  Array<{ uid:string; name?:string; coords?:[number,number] }>;
  committed?: boolean;     // false until a Save commits the session
  sessionId?: string; 
};

function slimFeature(f:any) {
  return {
    uid: f?.properties?.uid,
    name: Array.isArray(f?.properties?.name) ? f.properties.name[0] : undefined,
    coords: f?.geometry?.coordinates ? [f.geometry.coordinates[0], f.geometry.coordinates[1]] as [number,number] : undefined
  };
}

function mkAuditFromChange(c: ChangeEntry, user: string): AuditEntry {
  return {
    ts: Date.now(),
    user: user || "guest",
    type: c.type,
    summary: c.summary,
    uids: [
      ...new Set([
        ...(c.before||[]).map((f:any)=>f?.properties?.uid).filter(Boolean),
        ...(c.after||[]).map((f:any)=>f?.properties?.uid).filter(Boolean),
      ])
    ],
    before: (c.before||[]).map(slimFeature),
    after:  (c.after||[]).map(slimFeature),
  };
}

/* ================================================================ */
/*                          MAP EDITOR                              */
/* ================================================================ */
export default function MapEditor() {
  /* Core state */
const [sessionId] = useState(() => safeRandomUUID());

  const [fc, setFc] = useState<FC | null>(null);
  const [status, setStatus] = useState<string>("");
  const [mode, setMode] = useState<"select"|"merge"|"delete"|"move"|"edit"|"create">("select");
  const [anchorUid, setAnchorUid] = useState<string | null>(null);
  const [candidateUid, setCandidateUid] = useState<string | null>(null);
  const [historyStack, setHistoryStack] = useState<FC[]>([]);
  const [changes, setChanges] = useState<ChangeEntry[]>([]);
  const [editingUid, setEditingUid] = useState<string | null>(null);
  const mapRef = useRef<LeafletMap | null>(null);

  /* Username / identity (saved locally) */
  const [username, setUsername] = useState<string>("");
  useEffect(() => {
    const u = localStorage.getItem("geo_username") || "";
    if (u) setUsername(u);
  }, []);
  const saveUsername = () => {
    localStorage.setItem("geo_username", username || "");
    setStatus(username ? `Using user: ${username}` : "Cleared user");
  };

  /* Presence (SSE) */
  const [online, setOnline] = useState<PresenceUser[]>([]);
  useEffect(() => {
    const u = encodeURIComponent(username || "guest");
    const es = new EventSource(`/api/presence/stream?u=${u}`);
    es.onmessage = (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        if (payload.type === "state" || payload.type === "join" || payload.type === "leave") {
          setOnline(payload.users as PresenceUser[]);
        }
      } catch {}
    };
    es.onerror = () => { /* ignore; auto-reconnect */ };
    return () => es.close();
  }, [username]);

  /* meters per pixel + jitter for identical points */
  function mpp(lat: number, z: number) {
    return 156543.03392 * Math.cos((lat * Math.PI) / 180) / Math.pow(2, z);
  }
  function jitterIdenticalLeaves(items: any[], zoom: number) {
    const out = new Map<string, [number, number]>();
    if (!items?.length) return out;
    const leaves = items.filter((it: any) => !it?.properties?.cluster);
    const groups = new Map<string, any[]>();
    for (const it of leaves) {
      const key = (it.geometry.coordinates as [number, number]).join(",");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(it);
    }
    groups.forEach((arr) => {
      if (arr.length <= 1) return;
      const [lng0, lat0] = arr[0].geometry.coordinates as [number, number];
      const meters = 16 * mpp(lat0, Math.round(zoom));
      const dLat = (meters / 6371000) * (180 / Math.PI);
      const dLng = (meters / (6371000 * Math.cos((lat0 * Math.PI) / 180))) * (180 / Math.PI);
      const n = arr.length;
      for (let k = 0; k < n; k++) {
        const theta = (2 * Math.PI * k) / n;
        const lat = lat0 + dLat * Math.sin(theta);
        const lng = lng0 + dLng * Math.cos(theta);
        const uid = arr[k].properties.uid as string;
        out.set(uid, [lng, lat]);
      }
    });
    return out;
  }

  /* Load GeoJSON */
  useEffect(() => { void reload(); }, []);
  async function reload() {
    try {
      setStatus("Loading GeoJSON…");
      const res = await fetch("/api/geojson", { cache: "no-store" });
      if (!res.ok) throw new Error(await res.text());
      const data: FC = await res.json();
      data.features.forEach(f => {
        const p = (f.properties ||= {});
        ["name","access_id","type_id","beach_org","depth_id","beach_amea","purpose","source","source_id","merged_from_uids"]
          .forEach(k => (p as any)[k] = asList((p as any)[k]));
        p.area_size = toFloatList(p.area_size||[]);
        p.tags ||= {};
        if (!p.uid) p.uid = `all-${Math.random().toString(36).slice(2,8)}-${Date.now()}`;
      });
      setFc(data);
      setStatus("");
      setAnchorUid(null);
      setCandidateUid(null);
      setEditingUid(null);
    } catch (e:any) {
      setStatus("Failed to load GeoJSON: " + e.message);
    }
  }

  /* Undo stack */
  const pushSnapshot = () => setHistoryStack(h => fc ? [...h, JSON.parse(JSON.stringify(fc))] : h);
  const undo = () =>
    setHistoryStack(h => {
      if (!h.length) return h;
      const last = h[h.length - 1];
      setFc(last);
      setAnchorUid(null);
      setCandidateUid(null);
      setEditingUid(null);
      return h.slice(0, -1);
    });

  /* Feature helpers */
  const byUid = (uid: string) => fc!.features.find(f => f.properties?.uid === uid);

  /* ---------- AUDIT batching + flush ---------- */
  const pendingAudit = useRef<AuditEntry[]>([]);
  const auditTimer = useRef<number | null>(null);

  function flushAudit(debounceMs = 400) {
    if (auditTimer.current) window.clearTimeout(auditTimer.current);
    auditTimer.current = window.setTimeout(async () => {
      const batch = pendingAudit.current.splice(0, pendingAudit.current.length);
      if (!batch.length) return;
      try {
        await fetch("/api/audit/append", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(batch),
          keepalive: true,
        });
      } catch { /* ignore */ }
    }, debounceMs) as unknown as number;
  }

  useEffect(() => {
    const onUnload = () => {
      try {
        const batch = pendingAudit.current.splice(0, pendingAudit.current.length);
        if (!batch.length) return;
        const blob = new Blob([JSON.stringify(batch)], { type: "application/json" });
        navigator.sendBeacon("/api/audit/append", blob);
      } catch {}
    };
    window.addEventListener("pagehide", onUnload);
    window.addEventListener("beforeunload", onUnload);
    return () => {
      window.removeEventListener("pagehide", onUnload);
      window.removeEventListener("beforeunload", onUnload);
    };
  }, []);

  /* Set features with change (records audit) */
  const setFeatures = (nextFeatures: Feature[], change?: ChangeEntry) => {
    if (!fc) return;
    pushSnapshot();
    setFc({ ...fc, features: nextFeatures });
    if (change) {
  setChanges(cs => [change, ...cs]);
  pendingAudit.current.push({
    ...mkAuditFromChange(change, username),
    committed: false,
    sessionId,
  });
  flushAudit();
}

  };

  /* Update single feature (records audit) */
  const updateFeature = (uid: string, updater: (f: Feature) => Feature, change?: ChangeEntry) => {
    if (!fc) return;
    pushSnapshot();
    const next = {
      ...fc,
      features: fc.features.map(f => f.properties?.uid === uid ? updater(JSON.parse(JSON.stringify(f))) : f)
    };
    setFc(next);
    if (change) {
  setChanges(cs => [change, ...cs]);
  pendingAudit.current.push({
    ...mkAuditFromChange(change, username),
    committed: false,
    sessionId,
  });
  flushAudit();
}

  };

  const deleteFeature = (uid: string) => {
    if (!fc) return;
    const victim = byUid(uid);
    if (!victim) return;
    const change: ChangeEntry = {
      id: safeRandomUUID(),
      ts: Date.now(),
      type: "delete",
      summary: `Delete ${uid} (${(victim.properties.name?.[0]||"no name")})`,
      before: [victim],
      after: []
    };
    setFeatures(fc.features.filter(f => f.properties?.uid !== uid), change);
    if (editingUid === uid) setEditingUid(null);
  };

  const mergeFeatures = async (anchor: Feature, other: Feature) => {
    const d = haversineMeters(anchor.geometry.coordinates, other.geometry.coordinates);
    setCandidateUid(other.properties.uid!);
    let ok = true;
    if (d > 500) ok = confirm(`These points are ${d.toFixed(0)} m apart. Merge anyway?`);
    if (!ok) { setCandidateUid(null); return; }

    const mergedProps = mergeProps(anchor.properties, other.properties);
    const merged: Feature = {
      type: "Feature",
      geometry: { type: "Point", coordinates: anchor.geometry.coordinates },
      properties: { ...mergedProps, uid: anchor.properties.uid }
    };

    const change: ChangeEntry = {
      id: crypto.randomUUID(),
      ts: Date.now(),
      type: "merge",
      summary: `Merge ${other.properties.uid} → ${anchor.properties.uid}`,
      before: [JSON.parse(JSON.stringify(anchor)), JSON.parse(JSON.stringify(other))],
      after: [JSON.parse(JSON.stringify(merged))]
    };

    const next = fc!.features
      .filter(f => f.properties.uid !== other.properties.uid)
      .map(f => f.properties.uid === anchor.properties.uid ? merged : f);

    setFeatures(next, change);
    setAnchorUid(null);
    setCandidateUid(null);
    if (editingUid === anchor.properties.uid || editingUid === other.properties.uid) {
      setEditingUid(merged.properties.uid!);
    }
  };

  const moveFeatureTo = (uid: string, newLngLat: [number, number]) => {
    const f = byUid(uid);
    if (!f) return;
    const before = JSON.parse(JSON.stringify(f));
    const after = JSON.parse(JSON.stringify(f));
    after.geometry.coordinates = newLngLat;

    const change: ChangeEntry = {
      id: crypto.randomUUID(),
      ts: Date.now(),
      type: "move",
      summary: `Move ${uid} ${(f.properties.name?.[0] || "no name")}`,
      before: [before],
      after: [after]
    };

    updateFeature(uid, (old) => ({
      ...old,
      geometry: { type: "Point", coordinates: newLngLat }
    }), change);
  };

  /* Create & Edit */
  function MapCreateEvents() {
    useMapEvents({
      click(e) {
        if (mode !== "create" || !fc) return;
        const lat = e.latlng.lat, lng = e.latlng.lng;
        const uid = `new-${Date.now()}-${Math.random().toString(36).slice(2,6)}`;
        const emptyProps: Properties = {
          uid, name: [], access_id: [], type_id: [], beach_org: [], depth_id: [], beach_amea: [],
          purpose: [], area_size: [], tags: {}, source: [], source_id: [], merged_from_uids: []
        };
        const feat: Feature = {
          type: "Feature",
          geometry: { type: "Point", coordinates: [lng, lat] },
          properties: emptyProps
        };

        const change: ChangeEntry = {
          id: crypto.randomUUID(),
          ts: Date.now(),
          type: "create",
          summary: `Create ${uid}`,
          before: [],
          after: [JSON.parse(JSON.stringify(feat))]
        };

        setFeatures([...(fc.features||[]), feat], change);
        setEditingUid(uid);
        setMode("edit");
      }
    });
    return null;
  }
  const onPointClickForEdit = (uid: string) => { if (mode === "edit") setEditingUid(uid); };
  const saveEdits = (uid: string, nextProps: Properties) => {
    const f = byUid(uid);
    if (!f) return;
    const before = JSON.parse(JSON.stringify(f));
    const after: Feature = { ...before, properties: { ...nextProps, uid } };
    const change: ChangeEntry = {
      id: crypto.randomUUID(),
      ts: Date.now(),
      type: "edit",
      summary: `Edit ${uid}`,
      before: [before],
      after: [after]
    };
    updateFeature(uid, () => after, change);
  };

  /* Revert a specific change (UI only; not touching disk) */
  const revertChange = (chg: ChangeEntry) => {
    if (!fc) return;
    const clone = JSON.parse(JSON.stringify(fc)) as FC;
    const idxByUid = new Map<string, number>();
    clone.features.forEach((f, i) => idxByUid.set(f.properties.uid!, i));

    if (chg.type === "delete") {
      const reAdd = chg.before.filter(b => !idxByUid.has(b.properties.uid!));
      const next = { ...clone, features: [...clone.features, ...reAdd] };
      setFc(next);
    } else if (chg.type === "move" || chg.type === "edit") {
      const orig = chg.before[0];
      const i = idxByUid.get(orig.properties.uid!);
      if (i != null) {
        clone.features[i] = orig;
        setFc(clone);
      }
    } else if (chg.type === "merge") {
      const aBefore = chg.before[0];
      const bBefore = chg.before[1];
      const aUid = aBefore.properties.uid!;
      const bUid = bBefore.properties.uid!;
      const iA = idxByUid.get(aUid);
      let nextFeatures = clone.features.filter(f => f.properties.uid !== bUid);
      if (iA != null) nextFeatures = nextFeatures.map(f => f.properties.uid === aUid ? aBefore : f);
      else nextFeatures.push(aBefore);
      if (!nextFeatures.find(f => f.properties.uid === bUid)) nextFeatures.push(bBefore);
      setFc({ ...clone, features: nextFeatures });
    } else if (chg.type === "create") {
      const created = chg.after[0];
      setFc({ ...clone, features: clone.features.filter(f => f.properties.uid !== created.properties.uid) });
      if (editingUid === created.properties.uid) setEditingUid(null);
    }
  };

  /* Save current FC to disk (with username + message) */
  const save = async () => {
    if (!fc) return;
    try {
      setStatus("Saving…");
      const msg = prompt("Version message (what changed)?", "manual edit");
      const res = await fetch("/api/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ featureCollection: fc, message: msg || "manual edit", user: username || "guest" })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error || "Save failed");

      await fetch("/api/audit/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId, user: username || "guest" }),
    });
      setStatus(`Saved version ${data.versionId}`);
    } catch (e:any) { setStatus("Save error: " + e.message); }
  };

  /* Supercluster index */
  const clusterIndex = useMemo(() => {
    if (!fc) return null;
    const points = fc.features.map((f, i) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: f.geometry.coordinates },
      properties: { ...f.properties, _id: f.properties.uid || `idx-${i}` },
    })) as any[];
    return new Supercluster({ radius: CLUSTER_RADIUS_PX, maxZoom: 22, minPoints: 2 }).load(points);
  }, [fc]);

  /* Map view tracking */
  const [view, setView] = useState<{bbox: [number, number, number, number] | null, zoom: number}>({ bbox: null, zoom: 7 });
  const prevViewRef = useRef<typeof view | null>(null);
  const rafId = useRef<number | null>(null);

  function setViewThrottled(next: { bbox: [number, number, number, number]; zoom: number }) {
    if (rafId.current != null) return;
    rafId.current = requestAnimationFrame(() => {
      rafId.current = null;
      const prev = prevViewRef.current;
      const sameZoom = prev?.zoom === next.zoom;
      const sameBbox =
        !!prev?.bbox &&
        Math.abs(prev.bbox[0] - next.bbox[0]) < 1e-9 &&
        Math.abs(prev.bbox[1] - next.bbox[1]) < 1e-9 &&
        Math.abs(prev.bbox[2] - next.bbox[2]) < 1e-9 &&
        Math.abs(prev.bbox[3] - next.bbox[3]) < 1e-9;
      if (sameZoom && sameBbox) return;
      prevViewRef.current = next;
      setView(next);
    });
  }

  function MapEvents() {
    useMapEvents({
      moveend(e) {
        const m = e.target as L.Map;
        mapRef.current = m;
        const b = m.getBounds();
        setViewThrottled({ bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], zoom: m.getZoom() });
      },
      zoomend(e) {
        const m = e.target as L.Map;
        mapRef.current = m;
        const b = m.getBounds();
        setViewThrottled({ bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], zoom: m.getZoom() });
      }
    });
    return null;
  }

  const items = useMemo(() => {
    if (!clusterIndex || !view.bbox) return [];
    return clusterIndex.getClusters(view.bbox, Math.round(view.zoom));
  }, [clusterIndex, view.bbox, view.zoom]);

  const jitterPosByUid = useMemo(() => jitterIdenticalLeaves(items, view.zoom), [items, view.zoom]);

  const clusterIcon = (count: number) =>
    new L.DivIcon({
      className: "cluster-icon",
      html: `<div style="width:28px;height:28px;border-radius:50%;background:#111;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;border:2px solid #0ea5e9">${count}</div>`,
    });

  const pointIcon = (opts: { missing: boolean; isAnchor: boolean; isCandidate: boolean; isEditing: boolean; }) => {
    const border = opts.isEditing ? "#7c3aed"
                 : opts.isAnchor ? "#f59e0b"
                 : opts.isCandidate ? "#84cc16"
                 : opts.missing ? "#e11d48"
                 : "#0ea5e9";
    return new L.DivIcon({
      className: "custom-marker",
      html: `<div style="width:14px;height:14px;border-radius:50%;border:3px solid ${border};background:#fff"></div>`
    });
  };

  const onClusterClick = useCallback((clusterId: number, lng: number, lat: number) => {
    const map = mapRef.current;
    if (!clusterIndex || !map) return;
    const target = clusterIndex.getClusterExpansionZoom(clusterId);
    const cap = Math.min(target, 22);
    const next = cap <= map.getZoom() ? Math.min(map.getZoom() + 1, 22) : cap;
    map.flyTo([lat, lng], next, { animate: true, duration: 0.5 });
  }, [clusterIndex]);

  /* Popup table */
  const AllKeysTable = ({ p }: { p: Properties }) => {
    const keys = useMemo(() => Array.from(new Set(Object.keys(p || {})).values()), [p]);
    return (
      <table className="text-xs w-full">
        <tbody>
          {keys.filter(k => k !== "uid").map(k => {
            const val = (p as any)[k];
            const empty = isEmptyVal(val);
            return (
              <tr key={k}>
                <td className="pr-2 align-top whitespace-nowrap"><b>{k}</b></td>
                <td className={`align-top ${empty ? "text-rose-600" : ""}`}>
                  {Array.isArray(val) ? (val.length ? val.join(", ") : "(empty)")
                    : val == null ? "(empty)"
                    : typeof val === "object" ? <pre className="whitespace-pre-wrap">{JSON.stringify(val, null, 2)}</pre>
                    : String(val)}
                </td>
              </tr>
            );
          })}
          <tr><td className="pr-2 align-top whitespace-nowrap"><b>uid</b></td><td className="align-top">{p.uid}</td></tr>
        </tbody>
      </table>
    );
  };

  const center: [number, number] = fc?.features?.[0]
    ? [fc.features[0].geometry.coordinates[1], fc.features[0].geometry.coordinates[0]]
    : [37.98, 23.72];

  /* Render */
  return (
    <div className="app">
      {/* LEFT: Map + toolbar */}
      <div className="left">
        <TopBar
          mode={mode}
          setMode={(m) => { setMode(m); setAnchorUid(null); setCandidateUid(null); if (m !== "edit") setEditingUid(null); }}
          onUndo={undo}
          onSave={save}
          onReload={reload}
          status={status}
          anchorUid={anchorUid}
          candidateUid={candidateUid}
          onClearSelection={() => { setAnchorUid(null); setCandidateUid(null); }}
          /* NEW */
          total={fc?.features?.length || 0}
          username={username}
          setUsername={setUsername}
          saveUsername={saveUsername}
          online={online}
        />

        <div className="topstrip">
          <FileUploader onUploaded={reload} />
        </div>

        <MapContainer
          center={center}
          zoom={7}
          preferCanvas
          style={{ height: "calc(100vh - 112px)", width: "100%" }}
          maxZoom={22}
          ref={(m) => { mapRef.current = m; }}
          whenReady={() => {
            const m = mapRef.current; if (!m) return;
            const b = m.getBounds();
            setView({ bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], zoom: m.getZoom() });
          }}
        >
          <TileLayer
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
            maxNativeZoom={19}
            maxZoom={22}
            attribution="&copy; OpenStreetMap"
          />
          <MapEvents />
          <MapCreateEvents />

          {items.map((it: any) => {
            const [lng, lat] = it.geometry.coordinates as [number, number];

            if (it.properties.cluster) {
              const { cluster_id, point_count } = it.properties;
              return (
                <Marker
                  key={`cluster-${cluster_id}`}
                  position={[lat, lng]}
                  icon={clusterIcon(point_count) as any}
                  eventHandlers={{ click: () => onClusterClick(cluster_id, lng, lat) }}
                />
              );
            }

            const p: Properties = it.properties;
            const uid = p.uid!;
            const hasMissing = ["name", "purpose", "area_size"].some(k => isEmptyVal((p as any)[k]));
            const draggable = mode === "move" && view.zoom >= DETAIL_ZOOM;
            const icon = pointIcon({
              missing: hasMissing,
              isAnchor: anchorUid === uid,
              isCandidate: candidateUid === uid,
              isEditing: editingUid === uid,
            });
            const override = jitterPosByUid.get(uid);
            const [renderLng, renderLat] = override ?? [lng, lat];

            return (
              <Marker
                key={`pt-${uid}`}
                position={[renderLat, renderLng]}
                icon={icon as any}
                draggable={draggable}
                eventHandlers={{
                  dragend: (e) => {
                    const ll = (e.target as L.Marker).getLatLng();
                    moveFeatureTo(uid, [ll.lng, ll.lat]);
                  },
                  click: () => {
                    if (mode === "delete" && view.zoom >= DETAIL_ZOOM) {
                      if (confirm(`Delete ${uid}?`)) deleteFeature(uid);
                    } else if (mode === "merge" && view.zoom >= DETAIL_ZOOM) {
                      if (!anchorUid) setAnchorUid(uid);
                      else if (anchorUid === uid) setAnchorUid(null);
                      else {
                        const anchor = byUid(anchorUid);
                        if (anchor) {
                          const candidate: Feature = {
                            type: "Feature",
                            geometry: { type: "Point", coordinates: [lng, lat] },
                            properties: p
                          };
                          void mergeFeatures(anchor, candidate);
                        }
                      }
                    } else if (mode === "edit" && view.zoom >= DETAIL_ZOOM) {
                      onPointClickForEdit(uid);
                    }
                  }
                }}
              >
                {view.zoom >= DETAIL_ZOOM && mode !== "edit" && (
                  <Popup maxWidth={420}>
                    <div style={{ maxHeight: 320, overflowY: "auto" }}>
                      <div className="font-semibold mb-1">{(p.name && p.name[0]) || "(no name)"}</div>
                      <AllKeysTable p={p} />
                    </div>
                  </Popup>
                )}
              </Marker>
            );
          })}
        </MapContainer>
      </div>

      {/* RIGHT: Editor + History + Versions */}
      <div className="right">
        <EditorPanel
          fc={fc}
          editingUid={editingUid}
          onClose={() => setEditingUid(null)}
          onSave={saveEdits}
        />
        <HistoryPanel
          changes={changes}
          onToggle={(id) => setChanges(cs => cs.map(c => c.id === id ? { ...c, expanded: !c.expanded } : c))}
          onRevert={(chg) => revertChange(chg)}
        />
        <VersionsPanel />
        {/* If you want to preview recent audit (optional):*/}
            <AuditRecentPanel /> 
      </div>
    </div>
  );
}

/* -------------------- Top Bar -------------------- */
function TopBar(props: {
  mode: "select"|"merge"|"delete"|"move"|"edit"|"create";
  setMode: (m:"select"|"merge"|"delete"|"move"|"edit"|"create")=>void;
  onUndo: ()=>void;
  onSave: ()=>void;
  onReload: ()=>void;
  status: string;
  anchorUid: string|null;
  candidateUid: string|null;
  onClearSelection: ()=>void;
  total: number;
  username: string;
  setUsername: (s: string)=>void;
  saveUsername: ()=>void;
  online: Array<{ id: string; username: string; ipMasked: string; since: number }>;
}) {
  const { mode, setMode, onUndo, onSave, onReload, status, anchorUid, candidateUid, onClearSelection,
    total, username, setUsername, saveUsername, online } = props;

  const ModeBtn = ({ m, label }: { m: typeof mode, label: string }) => (
    <button
      onClick={()=>setMode(m)}
      className={`btn ${mode===m ? "is-active" : ""}`}
      title={
        m==="select" ? "View mode (safe) – no edits"
        : m==="merge" ? "Select A then B to merge B into A"
        : m==="delete" ? "Click a point to delete"
        : m==="move"   ? "Drag a point to move"
        : m==="edit"   ? "Click a point to edit in the right panel"
        : "Click on map to create a new point"
      }
    >
      {label}
    </button>
  );

  return (
    <div className="toolbar">
      <span style={{fontWeight:600, marginRight:8}}>Mode:</span>
      <ModeBtn m="select" label="Select" />
      <ModeBtn m="merge"  label="Merge"  />
      <ModeBtn m="delete" label="Delete" />
      <ModeBtn m="move"   label="Move"   />
      <ModeBtn m="edit"   label="Edit"   />
      <ModeBtn m="create" label="Create" />

      {mode === "merge" && (
        <div className="chips">
          <span className="chip anchor">Anchor: {anchorUid ?? "—"}</span>
          <span className="chip candidate">Candidate: {candidateUid ?? "—"}</span>
          <button className="btn" onClick={onClearSelection} title="Clear selection">Clear</button>
        </div>
      )}

      {/* Live counters & identity */}
      <div className="chips" style={{ marginLeft: 8 }}>
        <span className="chip" style={{ background:"#0ea5e9" }}>Beaches: {total.toLocaleString()}</span>
        <span className="chip" style={{ background:"#10b981" }}>Online: {online.length}</span>
      </div>

      <div className="spacer" />

      {/* Presence pills */}
      <div style={{ display:"flex", gap:6, alignItems:"center", marginRight:8, flexWrap:"wrap", maxWidth:340 }}>
        {online.map(u => (
          <Chip key={u.id} color="#374151">{u.username || "guest"} • {u.ipMasked}</Chip>
        ))}
      </div>

      {/* Username editor */}
      <div style={{ display:"flex", gap:6, alignItems:"center", marginRight:8 }}>
        <input
          value={username}
          onChange={e=>setUsername(e.target.value)}
          placeholder="username"
          className="user-input"
          title="Used in presence and saved history"
        />
        <button className="btn" onClick={saveUsername}>Use</button>
      </div>

      <button onClick={onUndo} className="btn">Undo</button>
      <button onClick={onSave} className="btn primary">Save version</button>
      <button onClick={onReload} className="btn">Reload</button>

      <span className="status">{status}</span>
    </div>
  );
}

/* -------------------- Editor Panel -------------------- */
function EditorPanel({
  fc, editingUid, onClose, onSave
}: {
  fc: FC | null;
  editingUid: string | null;
  onClose: ()=>void;
  onSave: (uid: string, props: Properties)=>void;
}) {
  if (!fc || !editingUid) {
    return (
      <div className="editor-panel">
        <div className="title">Editor</div>
        <div className="muted">Select “Edit”, then click a point — or choose “Create” and click on the map.</div>
      </div>
    );
  }
  const f = fc.features.find(x => x.properties.uid === editingUid);
  if (!f) return null;
  return <EditorForm feature={f} onClose={onClose} onSave={onSave} />;
}

function EditorForm({ feature, onClose, onSave }: {
  feature: Feature;
  onClose: ()=>void;
  onSave: (uid: string, props: Properties)=>void;
}) {
  const [draft, setDraft] = useState<Properties>(() => JSON.parse(JSON.stringify(feature.properties)));
  const listToCSV = (v?: any[]) => (Array.isArray(v) ? v.join(", ") : "");
  const csvToList = (s: string) => s.split(",").map(x => x.trim()).filter(Boolean);
  const numCSVToList = (s: string) => s.split(",").map(x => x.trim()).map(x => Number(x.replace(",", "."))).filter(Number.isFinite);
  const setField = (k: keyof Properties, v: any) => setDraft(d => ({ ...d, [k]: v }));
  const setCSV   = (k: keyof Properties) => (e: React.ChangeEvent<HTMLInputElement>) => setField(k, csvToList(e.target.value));
  const setNumCSV= (k: keyof Properties) => (e: React.ChangeEvent<HTMLInputElement>) => setField(k, numCSVToList(e.target.value));
  const setJSON  = (k: keyof Properties) => (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const v = e.target.value; try { setField(k, v ? JSON.parse(v) : {}); } catch {}
  };

  return (
    <div className="editor-panel">
      <div className="title">Editor</div>
      <div className="field">
        <label>UID</label>
        <input value={draft.uid || ""} onChange={e=>setField("uid", e.target.value)} />
      </div>

      <div className="field">
        <label>name (comma-separated)</label>
        <input value={listToCSV(draft.name)} onChange={setCSV("name")} placeholder="e.g. Παραλία Χ, Beach X" />
      </div>

      <div className="grid2">
        <div className="field"><label>access_id</label><input value={listToCSV(draft.access_id)} onChange={setCSV("access_id")} /></div>
        <div className="field"><label>type_id</label><input value={listToCSV(draft.type_id)} onChange={setCSV("type_id")} /></div>
        <div className="field"><label>beach_org</label><input value={listToCSV(draft.beach_org)} onChange={setCSV("beach_org")} /></div>
        <div className="field"><label>depth_id</label><input value={listToCSV(draft.depth_id)} onChange={setCSV("depth_id")} /></div>
        <div className="field"><label>beach_amea</label><input value={listToCSV(draft.beach_amea)} onChange={setCSV("beach_amea")} /></div>
      </div>

      <div className="field">
        <label>purpose (comma-separated)</label>
        <input value={listToCSV(draft.purpose)} onChange={setCSV("purpose")} />
      </div>

      <div className="field">
        <label>area_size (comma-separated numbers)</label>
        <input value={listToCSV(draft.area_size)} onChange={setNumCSV("area_size")} placeholder="e.g. 120, 300.5" />
      </div>

      <div className="grid2">
        <div className="field"><label>source</label><input value={listToCSV(asList(draft.source))} onChange={e=>setField("source", csvToList(e.target.value))} /></div>
        <div className="field"><label>source_id</label><input value={listToCSV(asList(draft.source_id))} onChange={e=>setField("source_id", csvToList(e.target.value))} /></div>
      </div>

      <div className="field">
        <label>tags (JSON)</label>
        <textarea defaultValue={JSON.stringify(draft.tags || {}, null, 2)} onChange={setJSON("tags")} rows={6} />
      </div>

      <div className="buttons">
        <button className="btn" onClick={onClose}>Close</button>
        <button
          className="btn primary"
          onClick={() => onSave(draft.uid!, {
            ...draft,
            name: dedupe(asList(draft.name)),
            access_id: dedupe(asList(draft.access_id)),
            type_id: dedupe(asList(draft.type_id)),
            beach_org: dedupe(asList(draft.beach_org)),
            depth_id: dedupe(asList(draft.depth_id)),
            beach_amea: dedupe(asList(draft.beach_amea)),
            purpose: dedupe(asList(draft.purpose)),
            area_size: toFloatList(draft.area_size || []),
            source: dedupe(asList(draft.source)),
            source_id: dedupe(asList(draft.source_id)),
          })}
        >
          Save edits
        </button>
      </div>
    </div>
  );
}

/* -------------------- History Panel -------------------- */
function HistoryPanel({
  changes, onToggle, onRevert
}: {
  changes: ChangeEntry[];
  onToggle: (id: string)=>void;
  onRevert: (c: ChangeEntry)=>void;
}) {
  return (
    <div className="history">
      <div className="title">History (this session)</div>
      {!changes.length && <div style={{color:"#6b7280"}}>No edits yet.</div>}
      <div style={{display:"grid", gap:10}}>
        {changes.map((c) => (
          <div key={c.id} className="card">
            <div className="row">
              <div style={{display:"flex", alignItems:"center", gap:8}}>
                <span className="badge">{c.type}</span>
                <span>{c.summary}</span>
              </div>
              <div className="actions">
                <button onClick={() => onToggle(c.id)} className="btn" style={{padding:"4px 8px", fontSize:12}}>
                  {c.expanded ? "Hide" : "Details"}
                </button>
                <button onClick={() => onRevert(c)} className="btn warn" style={{padding:"4px 8px", fontSize:12}}>
                  Revert
                </button>
              </div>
            </div>
            {c.expanded && (
              <div className="body">
                <div className="difflabel">Before</div>
                {c.before.map((f, i) => (
                  <div key={`b-${i}`} className="diffblock">
                    <div><b>uid:</b> {f.properties.uid}</div>
                    <div><b>name:</b> {f.properties.name?.[0] || "(no name)"}</div>
                    <div><b>coords:</b> {f.geometry.coordinates[1].toFixed(6)}, {f.geometry.coordinates[0].toFixed(6)}</div>
                  </div>
                ))}
                <div className="difflabel" style={{marginTop:8}}>After</div>
                {c.after.map((f, i) => (
                  <div key={`a-${i}`} className="diffblock">
                    <div><b>uid:</b> {f.properties.uid}</div>
                    <div><b>name:</b> {f.properties.name?.[0] || "(no name)"}</div>
                    <div><b>coords:</b> {f.geometry.coordinates[1].toFixed(6)}, {f.geometry.coordinates[0].toFixed(6)}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/* -------------------- Versions Panel -------------------- */
function VersionsPanel() {
  const [items, setItems] = useState<Array<{ id:string; ts:number; message:string; user?:string; size:number; features:number }>>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    (async () => {
      setLoading(true); setErr("");
      try {
        const res = await fetch("/api/versions", { cache:"no-store" });
        const data = await res.json();
        if (!res.ok) throw new Error(data?.error || "Failed to load versions");
        setItems(data.items || []);
      } catch (e:any) { setErr(e.message); }
      setLoading(false);
    })();
  }, []);

  return (
    <div className="history" style={{ borderTop: "1px solid #e5e7eb" }}>
      <div className="title">Saved Versions</div>
      {loading && <div className="muted">Loading…</div>}
      {err && <div className="muted" style={{ color:"#ef4444" }}>{err}</div>}
      <div style={{display:"grid", gap:10}}>
        {items.map(v => (
          <div key={v.id} className="card">
            <div className="row">
              <div style={{display:"flex", flexDirection:"column"}}>
                <div><b>{new Date(v.ts).toLocaleString()}</b> — {v.message || "(no message)"} {v.user ? `• by ${v.user}` : ""}</div>
                <div className="muted" style={{ fontSize:12 }}>
                  {v.features.toLocaleString()} features • {(v.size/1024).toFixed(1)} KB
                </div>
              </div>
              <div className="actions">
                <a className="btn" style={{padding:"4px 8px", fontSize:12}} href={`/api/version/${encodeURIComponent(v.id)}`} target="_blank" rel="noreferrer">Download</a>
              </div>
            </div>
          </div>
        ))}
        {!loading && !items.length && <div className="muted">No saved versions yet.</div>}
      </div>
    </div>
  );
}

function AuditRecentPanel() {
  const [items, setItems] = useState<any[]>([]);
  useEffect(() => {
    (async () => {
      const r = await fetch("/api/audit/recent?limit=200&committed=1", { cache:"no-store" });
      const j = await r.json();
      setItems(j.items || []);
    })();
  }, []);
  return (
    <div className="history" style={{ borderTop: "1px solid #e5e7eb" }}>
      <div className="title">Recent Audit</div>
      <div style={{display:"grid", gap:10}}>
        {items.map((x,i)=>(
          <div key={i} className="card">
            <div className="row" style={{alignItems:"flex-start"}}>
              <div>
                <div><b>{new Date(x.ts).toLocaleString()}</b> • {x.user}</div>
                <div className="muted" style={{fontSize:12}}>{x.type} — {x.summary}</div>
                <div className="muted" style={{fontSize:12}}>uids: {x.uids?.join(", ")}</div>
              </div>
            </div>
          </div>
        ))}
        {!items.length && <div className="muted">No audit entries yet.</div>}
      </div>
    </div>
  );
}
