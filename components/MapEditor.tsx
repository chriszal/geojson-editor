"use client";

import React, {
  useEffect, useMemo, useRef, useState, useCallback
} from "react";
import {
  Circle, MapContainer, Marker, Polyline, Popup, Rectangle, TileLayer, useMapEvents
} from "react-leaflet";
import L, { Map as LeafletMap } from "leaflet";
import Supercluster from "supercluster";
import FileUploader from "./FileUploader";
import ReviewPanel, { type ReviewChange } from "./ReviewPanel";
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
  source_features?: Array<{ uid?: string; geometry?: any; properties?: Record<string, any> }>;
  parent_beach_uid?: string;
  child_beach_uids?: string[];
  beach_group_id?: string;
  beach_role?: "main" | "section";
  is_hidden_beach?: boolean;
  beach_access_type?: string;
};
type Feature = {
  type: "Feature";
  geometry: { type: "Point"; coordinates: [number, number] };
  properties: Properties;
};
type FC = { type: "FeatureCollection"; features: Feature[] };
type ChangeType = "merge" | "group" | "move" | "delete" | "edit" | "create";
type Mode = "select" | "merge" | "bulkMerge" | "group" | "ungroup" | "hidden" | "delete" | "move" | "edit" | "create";
type SelectionShape = "box" | "circle";
type SelectionDraft = {
  shape: SelectionShape;
  start: [number, number];
  end: [number, number];
};
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
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.atan2(Math.sqrt(s), Math.sqrt(1 - s));
}
const asList = (v: any): any[] => Array.isArray(v) ? v : (v == null ? [] : [v]);
const dedupe = (arr: any[]) => Array.from(new Map(arr.map(x => [JSON.stringify(x), x])).values());
const toFloatList = (arr: any[]): number[] => dedupe(asList(arr)).map(x => Number(x)).filter(Number.isFinite);
const isEmptyVal = (v: any) => v == null || (Array.isArray(v) ? v.length === 0 : String(v).trim() === "");
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v));
const groupIdFor = (uid: string) => `beach-group-${uid}`;

const sourceSnapshot = (f: Feature) => {
  const properties = clone(f.properties || {});
  delete properties.source_features;
  return {
    uid: f.properties?.uid,
    geometry: clone(f.geometry),
    properties,
  };
};

const mergeSourceSnapshots = (existing: any[], features: Feature[]) =>
  dedupe([
    ...asList(existing),
    ...features.map(sourceSnapshot),
  ]);

/** Merge two feature property bags */
const mergeProps = (A: Properties, B: Properties): Properties => {
  const out: Properties = { ...A };
  const union = (key: keyof Properties) => {
    const merged = dedupe([...asList(A[key]), ...asList(B[key])]);
    (out as any)[key] = merged;
  };
  ["name", "access_id", "type_id", "beach_org", "depth_id", "beach_amea", "purpose"].forEach(k => union(k as any));
  out.area_size = dedupe([...(A.area_size || []), ...(B.area_size || [])]).map(Number).filter(Number.isFinite);
  out.tags = { ...(A.tags || {}), ...(B.tags || {}) };
  out.source = dedupe([...asList(A.source), ...asList(B.source)]);
  out.source_id = dedupe([...asList(A.source_id), ...asList(B.source_id)]);
  out.merged_from_uids = dedupe([...(A.merged_from_uids || []), ...(B.merged_from_uids || []), ...(A.uid ? [A.uid] : []), ...(B.uid ? [B.uid] : [])]);
  out.source_features = dedupe([...asList(A.source_features), ...asList(B.source_features)]);
  return out;
};

/* -------------------- Clustering -------------------- */
const CLUSTER_RADIUS_PX = 50;
const DETAIL_ZOOM = 13;
const UNDO_HISTORY_LIMIT = 3;
const CHANGE_HISTORY_LIMIT = 80;
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
    return `${b.slice(0, 8)}-${b.slice(8, 12)}-${b.slice(12, 16)}-${b.slice(16, 20)}-${b.slice(20)}`;
  }
  // Last-ditch fallback (not RFC-strong, but avoids crashes)
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}-${Math.random().toString(36).slice(2, 10)}`;
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
  before?: Array<{ uid: string; name?: string; coords?: [number, number] }>;
  after?: Array<{ uid: string; name?: string; coords?: [number, number] }>;
  committed?: boolean;     // false until a Save commits the session
  sessionId?: string;
};

function slimFeature(f: any) {
  return {
    uid: f?.properties?.uid,
    name: Array.isArray(f?.properties?.name) ? f.properties.name[0] : undefined,
    coords: f?.geometry?.coordinates ? [f.geometry.coordinates[0], f.geometry.coordinates[1]] as [number, number] : undefined
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
        ...(c.before || []).map((f: any) => f?.properties?.uid).filter(Boolean),
        ...(c.after || []).map((f: any) => f?.properties?.uid).filter(Boolean),
      ])
    ],
    before: (c.before || []).map(slimFeature),
    after: (c.after || []).map(slimFeature),
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
  const [mode, setMode] = useState<Mode>("select");
  const [bulkShape, setBulkShape] = useState<SelectionShape>("box");
  const [selectionDraft, setSelectionDraft] = useState<SelectionDraft | null>(null);
  const selectionDraftRef = useRef<SelectionDraft | null>(null);
  const [anchorUid, setAnchorUid] = useState<string | null>(null);
  const [candidateUid, setCandidateUid] = useState<string | null>(null);
  const [historyStack, setHistoryStack] = useState<FC[]>([]);
  const [changes, setChanges] = useState<ChangeEntry[]>([]);
  const [editingUid, setEditingUid] = useState<string | null>(null);
  const mapRef = useRef<LeafletMap | null>(null);
  const [rightMode, setRightMode] = useState<"editor" | "review">("editor");
  const [satellite, setSatellite] = useState(false);
  const [reviewHighlight, setReviewHighlight] = useState<{
    primary: string | null;
    discards: Set<string>;
    neutral: Set<string>;
  } | null>(null);

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
      } catch { }
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
        ["name", "access_id", "type_id", "beach_org", "depth_id", "beach_amea", "purpose", "source", "source_id", "merged_from_uids", "child_beach_uids", "source_features"]
          .forEach(k => (p as any)[k] = asList((p as any)[k]));
        p.area_size = toFloatList(p.area_size || []);
        p.tags ||= {};
        if (!p.uid) p.uid = `all-${Math.random().toString(36).slice(2, 8)}-${Date.now()}`;
      });
      setFc(data);
      setStatus("");
      setAnchorUid(null);
      setCandidateUid(null);
      setEditingUid(null);
    } catch (e: any) {
      setStatus("Failed to load GeoJSON: " + e.message);
    }
  }

  const rememberChange = (change: ChangeEntry) => {
    setChanges(cs => [change, ...cs].slice(0, CHANGE_HISTORY_LIMIT));
  };

  /* Undo stack */
  const pushSnapshot = () => setHistoryStack(h => {
    if (!fc) return h;
    return [...h.slice(-(UNDO_HISTORY_LIMIT - 1)), JSON.parse(JSON.stringify(fc))];
  });
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
      } catch { }
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
      rememberChange(change);
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
      rememberChange(change);
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
      summary: `Delete ${uid} (${(victim.properties.name?.[0] || "no name")})`,
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
    mergedProps.source_features = mergeSourceSnapshots(mergedProps.source_features || [], [anchor, other]);
    const merged: Feature = {
      type: "Feature",
      geometry: { type: "Point", coordinates: anchor.geometry.coordinates },
      properties: { ...mergedProps, uid: anchor.properties.uid }
    };

    const change: ChangeEntry = {
      id: safeRandomUUID(),
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

  const groupFeatureUnderAnchor = (parentUid: string, childUid: string) => {
    if (!fc || parentUid === childUid) return;
    const parent = byUid(parentUid);
    const child = byUid(childUid);
    if (!parent || !child) return;

    const oldParentUid = child.properties.parent_beach_uid;
    const oldParent = oldParentUid && oldParentUid !== parentUid ? byUid(oldParentUid) : null;
    const before = [parent, child, ...(oldParent ? [oldParent] : [])].map(f => clone(f));
    const groupId = parent.properties.beach_group_id || child.properties.beach_group_id || groupIdFor(parentUid);

    const updatedParent: Feature = {
      ...parent,
      properties: {
        ...parent.properties,
        beach_role: "main",
        beach_group_id: groupId,
        child_beach_uids: dedupe([...(parent.properties.child_beach_uids || []), childUid]),
      },
    };
    const updatedChild: Feature = {
      ...child,
      properties: {
        ...child.properties,
        beach_role: "section",
        beach_group_id: groupId,
        parent_beach_uid: parentUid,
      },
    };
    const updatedOldParent: Feature | null = oldParent ? {
      ...oldParent,
      properties: {
        ...oldParent.properties,
        child_beach_uids: (oldParent.properties.child_beach_uids || []).filter(uid => uid !== childUid),
      },
    } : null;

    const after = [updatedParent, updatedChild, ...(updatedOldParent ? [updatedOldParent] : [])].map(f => clone(f));
    const change: ChangeEntry = {
      id: safeRandomUUID(),
      ts: Date.now(),
      type: "group",
      summary: `Group ${childUid} under ${parentUid}`,
      before,
      after,
    };

    const next = fc.features.map(f => {
      if (f.properties.uid === parentUid) return updatedParent;
      if (f.properties.uid === childUid) return updatedChild;
      if (updatedOldParent && f.properties.uid === oldParentUid) return updatedOldParent;
      return f;
    });

    setFeatures(next, change);
    setCandidateUid(childUid);
  };

  const ungroupFeature = (uid: string) => {
    if (!fc) return;
    const target = byUid(uid);
    if (!target) return;
    const childUids = asList(target.properties.child_beach_uids).filter(Boolean);
    const isParent = childUids.length > 0;
    const isChild = Boolean(target.properties.parent_beach_uid);
    if (!isParent && !isChild) {
      setStatus(`Ungroup: ${uid} is not grouped.`);
      return;
    }

    const affectedUids = new Set<string>([uid]);
    if (isParent) {
      childUids.forEach(childUid => affectedUids.add(String(childUid)));
    } else if (target.properties.parent_beach_uid) {
      affectedUids.add(target.properties.parent_beach_uid);
    }

    const before = fc.features
      .filter(f => affectedUids.has(f.properties.uid!))
      .map(f => clone(f));

    const nextFeatures = fc.features.map(f => {
      const props = f.properties;
      const currentUid = props.uid!;

      if (isParent && currentUid === uid) {
        const nextProps = { ...props };
        delete nextProps.child_beach_uids;
        delete nextProps.beach_group_id;
        if (nextProps.beach_role === "main") delete nextProps.beach_role;
        return { ...f, properties: nextProps };
      }

      if (isParent && childUids.includes(currentUid)) {
        const nextProps = { ...props };
        delete nextProps.parent_beach_uid;
        delete nextProps.beach_group_id;
        if (nextProps.beach_role === "section") delete nextProps.beach_role;
        return { ...f, properties: nextProps };
      }

      if (isChild && currentUid === uid) {
        const nextProps = { ...props };
        delete nextProps.parent_beach_uid;
        delete nextProps.beach_group_id;
        if (nextProps.beach_role === "section") delete nextProps.beach_role;
        return { ...f, properties: nextProps };
      }

      if (isChild && currentUid === target.properties.parent_beach_uid) {
        const remainingChildren = asList(props.child_beach_uids).filter(childUid => childUid !== uid);
        const nextProps: Properties = { ...props, child_beach_uids: remainingChildren };
        if (!remainingChildren.length) {
          delete nextProps.child_beach_uids;
          delete nextProps.beach_group_id;
          if (nextProps.beach_role === "main") delete nextProps.beach_role;
        }
        return { ...f, properties: nextProps };
      }

      return f;
    });

    const after = nextFeatures
      .filter(f => affectedUids.has(f.properties.uid!))
      .map(f => clone(f));

    const change: ChangeEntry = {
      id: safeRandomUUID(),
      ts: Date.now(),
      type: "group",
      summary: isParent
        ? `Ungroup ${childUids.length} section(s) from ${uid}`
        : `Ungroup ${uid} from ${target.properties.parent_beach_uid}`,
      before,
      after,
    };

    setFeatures(nextFeatures, change);
    setCandidateUid(uid);
  };

  const toggleHiddenBeach = (uid: string) => {
    const f = byUid(uid);
    if (!f) return;
    const before = clone(f);
    const nextHidden = !Boolean(f.properties.is_hidden_beach);
    const after: Feature = {
      ...f,
      properties: {
        ...f.properties,
        is_hidden_beach: nextHidden,
        beach_access_type: nextHidden ? "hidden_or_hard_to_access" : undefined,
        tags: {
          ...(f.properties.tags || {}),
          hidden_or_hard_to_access: nextHidden || undefined,
        },
      },
    };
    const change: ChangeEntry = {
      id: safeRandomUUID(),
      ts: Date.now(),
      type: "edit",
      summary: `${nextHidden ? "Mark" : "Unmark"} hidden/hard access ${uid}`,
      before: [before],
      after: [clone(after)],
    };
    updateFeature(uid, () => after, change);
  };

  const mergeManyIntoAnchor = (anchorUidForMerge: string, mergeUids: string[], label: string) => {
    if (!fc) return;
    const anchor = byUid(anchorUidForMerge);
    if (!anchor) return;
    const uniqueMergeUids = dedupe(mergeUids).filter(uid => uid && uid !== anchorUidForMerge);
    if (!uniqueMergeUids.length) {
      setStatus("Bulk merge: no other beach pins inside the shape.");
      return;
    }
    const mergeFeaturesList = uniqueMergeUids.map(uid => byUid(uid)).filter(Boolean) as Feature[];
    if (!mergeFeaturesList.length) {
      setStatus("Bulk merge: selected pins are no longer present.");
      return;
    }
    const ok = confirm(`Merge ${mergeFeaturesList.length} selected beach pin(s) into ${anchorUidForMerge}?`);
    if (!ok) return;

    let mergedProps = clone(anchor.properties);
    for (const f of mergeFeaturesList) mergedProps = mergeProps(mergedProps, f.properties);
    mergedProps.source_features = mergeSourceSnapshots(mergedProps.source_features || [], [anchor, ...mergeFeaturesList]);

    const mergedFeature: Feature = {
      ...anchor,
      properties: { ...mergedProps, uid: anchorUidForMerge },
    };
    const discardSet = new Set(mergeFeaturesList.map(f => f.properties.uid!));
    const next = fc.features
      .filter(f => !discardSet.has(f.properties.uid!))
      .map(f => f.properties.uid === anchorUidForMerge ? mergedFeature : f);
    const change: ChangeEntry = {
      id: safeRandomUUID(),
      ts: Date.now(),
      type: "merge",
      summary: `${label}: merge ${mergeFeaturesList.length} pin(s) into ${anchorUidForMerge}`,
      before: [clone(anchor), ...mergeFeaturesList.map(f => clone(f))],
      after: [clone(mergedFeature)],
    };

    setFeatures(next, change);
    setAnchorUid(null);
    setCandidateUid(null);
  };

  const featureUidsInSelection = (draft: SelectionDraft) => {
    if (!fc) return [];
    const [sLat, sLng] = draft.start;
    const [eLat, eLng] = draft.end;
    if (draft.shape === "box") {
      const minLat = Math.min(sLat, eLat);
      const maxLat = Math.max(sLat, eLat);
      const minLng = Math.min(sLng, eLng);
      const maxLng = Math.max(sLng, eLng);
      return fc.features
        .filter(f => {
          const [lng, lat] = f.geometry.coordinates;
          return lat >= minLat && lat <= maxLat && lng >= minLng && lng <= maxLng;
        })
        .map(f => f.properties.uid!)
        .filter(Boolean);
    }

    const radius = haversineMeters([sLng, sLat], [eLng, eLat]);
    return fc.features
      .filter(f => haversineMeters([sLng, sLat], f.geometry.coordinates) <= radius)
      .map(f => f.properties.uid!)
      .filter(Boolean);
  };

  const moveFeatureTo = (uid: string, newLngLat: [number, number]) => {
    const f = byUid(uid);
    if (!f) return;
    const before = JSON.parse(JSON.stringify(f));
    const after = JSON.parse(JSON.stringify(f));
    after.geometry.coordinates = newLngLat;

    const change: ChangeEntry = {
      id: safeRandomUUID(),
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
        const uid = `new-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
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
          id: safeRandomUUID(),
          ts: Date.now(),
          type: "create",
          summary: `Create ${uid}`,
          before: [],
          after: [JSON.parse(JSON.stringify(feat))]
        };

        setFeatures([...(fc.features || []), feat], change);
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
      id: safeRandomUUID(),
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
    } else if (chg.type === "group") {
      const beforeByUid = new Map(chg.before.map(f => [f.properties.uid!, f]));
      setFc({
        ...clone,
        features: clone.features.map(f => beforeByUid.get(f.properties.uid!) || f),
      });
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

  /* Apply a change from the AI review queue to the live FC */
  const applyReviewChange = (change: ReviewChange) => {
    if (!fc) return;

    if (change.proposed_action === "CREATE_HIERARCHY" && change.primary_uid) {
      const primaryUid = change.primary_uid;
      const primaryFeat = fc.features.find(f => f.properties.uid === primaryUid);
      if (!primaryFeat) {
        setStatus(`Review: primary point ${primaryUid} not found in current data.`);
        return;
      }
      const childUids = change.points.map(p => p.uid).filter(uid => uid !== primaryUid);
      const childSet = new Set(childUids);
      const beforeFeatures = fc.features
        .filter(f => f.properties.uid === primaryUid || childSet.has(f.properties.uid!))
        .map(f => clone(f));
      const groupId = primaryFeat.properties.beach_group_id || groupIdFor(primaryUid);
      const nextFeatures = fc.features.map(f => {
        const uid = f.properties.uid!;
        if (uid === primaryUid) {
          return {
            ...f,
            properties: {
              ...f.properties,
              beach_role: "main" as const,
              beach_group_id: groupId,
              child_beach_uids: dedupe([...(f.properties.child_beach_uids || []), ...childUids]),
            },
          };
        }
        if (childSet.has(uid)) {
          return {
            ...f,
            properties: {
              ...f.properties,
              beach_role: "section" as const,
              beach_group_id: groupId,
              parent_beach_uid: primaryUid,
            },
          };
        }
        return f;
      });
      const afterFeatures = nextFeatures
        .filter(f => f.properties.uid === primaryUid || childSet.has(f.properties.uid!))
        .map(f => clone(f));
      const changeEntry: ChangeEntry = {
        id: safeRandomUUID(),
        ts: Date.now(),
        type: "group",
        summary: `AI review: group ${childUids.length} section(s) under ${primaryUid}`,
        before: beforeFeatures,
        after: afterFeatures,
      };
      setFeatures(nextFeatures, changeEntry);
      return;
    }

    // Phase 4 LONG_SECTIONS: rename section points + delete confirmed duplicates
    if (change.phase === 4 && change.proposed_action === "REVIEW_SECTIONS" && change.suggested_sections && change.suggested_sections.length > 0) {
      const discardSet  = new Set(change.discard_uids ?? []);
      const beforeFeats: Feature[] = [];
      const afterFeats:  Feature[] = [];

      // Build uid → suggested_name map from sections
      const uidToName: Record<string, string> = {};
      for (const sec of change.suggested_sections) {
        if (!sec.suggested_name) continue;
        for (const uid of sec.uids) {
          if (!discardSet.has(uid)) uidToName[uid] = sec.suggested_name;
        }
      }

      const nextFeatures = fc.features
        .filter(f => {
          const uid = f.properties.uid!;
          if (discardSet.has(uid)) {
            beforeFeats.push(JSON.parse(JSON.stringify(f)));
            return false;
          }
          return true;
        })
        .map(f => {
          const uid  = f.properties.uid!;
          const name = uidToName[uid];
          if (!name) return f;
          const updated: Feature = {
            ...f,
            properties: { ...f.properties, name: [name] },
          };
          beforeFeats.push(JSON.parse(JSON.stringify(f)));
          afterFeats.push(JSON.parse(JSON.stringify(updated)));
          return updated;
        });

      const changeEntry: ChangeEntry = {
        id:      safeRandomUUID(),
        ts:      Date.now(),
        type:    "merge",
        summary: `AI review (p4 sections): renamed ${Object.keys(uidToName).length} points, removed ${discardSet.size} duplicates`,
        before:  beforeFeats,
        after:   afterFeats,
      };

      setFeatures(nextFeatures, changeEntry);
      return;
    }

    // Standard merge: keep primary, delete discards
    if (!change.primary_uid || !change.discard_uids?.length) return;

    const primaryFeat = fc.features.find(f => f.properties.uid === change.primary_uid);
    if (!primaryFeat) {
      setStatus(`Review: primary point ${change.primary_uid} not found in current data.`);
      return;
    }

    let mergedProps = JSON.parse(JSON.stringify(primaryFeat.properties));
    const beforeFeatures: Feature[] = [JSON.parse(JSON.stringify(primaryFeat))];

    for (const discardUid of change.discard_uids) {
      const discardFeat = fc.features.find(f => f.properties.uid === discardUid);
      if (!discardFeat) continue;
      beforeFeatures.push(JSON.parse(JSON.stringify(discardFeat)));
      mergedProps = mergeProps(mergedProps, discardFeat.properties);
    }
    mergedProps.source_features = mergeSourceSnapshots(mergedProps.source_features || [], beforeFeatures);

    const mergedFeature: Feature = {
      ...primaryFeat,
      properties: { ...mergedProps, uid: change.primary_uid },
    };

    const discardSet = new Set(change.discard_uids);
    const nextFeatures = fc.features
      .filter(f => !discardSet.has(f.properties.uid!))
      .map(f => f.properties.uid === change.primary_uid ? mergedFeature : f);

    const changeEntry: ChangeEntry = {
      id:      safeRandomUUID(),
      ts:      Date.now(),
      type:    "merge",
      summary: `AI review: merge ${change.discard_uids.join(", ")} → ${change.primary_uid}`,
      before:  beforeFeatures,
      after:   [JSON.parse(JSON.stringify(mergedFeature))],
    };

    setFeatures(nextFeatures, changeEntry);
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
      setHistoryStack([]);
      setChanges([]);
      setStatus(`Saved version ${data.versionId}`);
    } catch (e: any) { setStatus("Save error: " + e.message); }
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
  const [view, setView] = useState<{ bbox: [number, number, number, number] | null, zoom: number }>({ bbox: null, zoom: 7 });
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

  function MapSelectionEvents() {
    useMapEvents({
      mousedown(e) {
        if (mode !== "bulkMerge" || !anchorUid) return;
        const target = e.originalEvent.target as HTMLElement | null;
        if (target?.closest(".leaflet-marker-icon")) return;
        const m = e.target as L.Map;
        m.dragging.disable();
        const next: SelectionDraft = {
          shape: bulkShape,
          start: [e.latlng.lat, e.latlng.lng],
          end: [e.latlng.lat, e.latlng.lng],
        };
        selectionDraftRef.current = next;
        setSelectionDraft(next);
      },
      mousemove(e) {
        const current = selectionDraftRef.current;
        if (!current) return;
        const next = { ...current, end: [e.latlng.lat, e.latlng.lng] as [number, number] };
        selectionDraftRef.current = next;
        setSelectionDraft(next);
      },
      mouseup(e) {
        const current = selectionDraftRef.current;
        if (!current) return;
        const m = e.target as L.Map;
        m.dragging.enable();
        selectionDraftRef.current = null;
        setSelectionDraft(null);
        if (!anchorUid) return;
        const uids = featureUidsInSelection(current).filter(uid => uid !== anchorUid);
        mergeManyIntoAnchor(anchorUid, uids, `${current.shape === "box" ? "Box" : "Circle"} merge`);
      },
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

  const pointIcon = (opts: {
    missing: boolean; isAnchor: boolean; isCandidate: boolean; isEditing: boolean;
    reviewRole?: "primary" | "discard" | "neutral";
    isGmaps?: boolean;
    isMain?: boolean;
    isSection?: boolean;
    isHidden?: boolean;
  }) => {
    const size = opts.reviewRole ? 18 : opts.isMain ? 20 : opts.isSection ? 12 : 14;
    const border = opts.isEditing    ? "#7c3aed"
      : opts.reviewRole === "primary"  ? "#22c55e"
      : opts.reviewRole === "discard"  ? "#f97316"
      : opts.reviewRole === "neutral"  ? "#eab308"
      : opts.isAnchor                  ? "#f59e0b"
      : opts.isCandidate               ? "#84cc16"
      : opts.isHidden                  ? "#a855f7"
      : opts.isMain                    ? "#111827"
      : opts.isSection                 ? "#7c3aed"
      : opts.isGmaps                   ? "#ec4899"
      : opts.missing                   ? "#e11d48"
      :                                  "#0ea5e9";
    const ring = opts.reviewRole
      ? `box-shadow:0 0 0 3px ${border}55;`
      : "";
    return new L.DivIcon({
      className: "custom-marker",
      html: `<div style="width:${size}px;height:${size}px;border-radius:50%;border:3px solid ${border};background:#fff;${ring}"></div>`
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

  const hierarchyLines = useMemo(() => {
    if (!fc || view.zoom < 11) return [];
    const byId = new Map(fc.features.map(f => [f.properties.uid!, f]));
    return fc.features
      .filter(f => Boolean(f.properties.parent_beach_uid))
      .map(child => {
        const parent = byId.get(child.properties.parent_beach_uid!);
        if (!parent) return null;
        const [pLng, pLat] = parent.geometry.coordinates;
        const [cLng, cLat] = child.geometry.coordinates;
        return {
          key: `${parent.properties.uid}-${child.properties.uid}`,
          positions: [[pLat, pLng], [cLat, cLng]] as [[number, number], [number, number]],
        };
      })
      .filter(Boolean) as Array<{ key: string; positions: [[number, number], [number, number]] }>;
  }, [fc, view.zoom]);

  const selectionBounds = selectionDraft && selectionDraft.shape === "box"
    ? [selectionDraft.start, selectionDraft.end] as [[number, number], [number, number]]
    : null;
  const selectionCircle = selectionDraft && selectionDraft.shape === "circle"
    ? {
      center: selectionDraft.start,
      radius: haversineMeters(
        [selectionDraft.start[1], selectionDraft.start[0]],
        [selectionDraft.end[1], selectionDraft.end[0]]
      ),
    }
    : null;

  /* Render */
  return (
    <div className="app">
      {/* LEFT: Map + toolbar */}
      <div className="left">
        <TopBar
          mode={mode}
          setMode={(m) => {
            setMode(m);
            setAnchorUid(null);
            setCandidateUid(null);
            setSelectionDraft(null);
            selectionDraftRef.current = null;
            if (m !== "edit") setEditingUid(null);
          }}
          bulkShape={bulkShape}
          setBulkShape={setBulkShape}
          onUndo={undo}
          onSave={save}
          onReload={reload}
          status={status}
          anchorUid={anchorUid}
          candidateUid={candidateUid}
          onClearSelection={() => { setAnchorUid(null); setCandidateUid(null); }}
          total={fc?.features?.length || 0}
          username={username}
          setUsername={setUsername}
          saveUsername={saveUsername}
          online={online}
          rightMode={rightMode}
          onToggleReview={() => { setRightMode(m => m === "review" ? "editor" : "review"); setReviewHighlight(null); }}
          satellite={satellite}
          onToggleSatellite={() => setSatellite(s => !s)}
        />

        <div className="topstrip">
          <FileUploader onUploaded={reload} />
        </div>

        <MapContainer
          center={center}
          zoom={7}
          preferCanvas
          style={{ flex: 1, minHeight: 320, width: "100%" }}
          maxZoom={22}
          ref={(m) => { mapRef.current = m; }}
          whenReady={() => {
            const m = mapRef.current; if (!m) return;
            const b = m.getBounds();
            setView({ bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], zoom: m.getZoom() });
          }}
        >
          {satellite ? (
            <TileLayer
              key="satellite"
              url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
              maxNativeZoom={19}
              maxZoom={22}
              attribution="&copy; Esri &mdash; Esri, i-cubed, USDA, AeroGRID, IGN"
            />
          ) : (
            <TileLayer
              key="osm"
              url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
              maxNativeZoom={19}
              maxZoom={22}
              attribution="&copy; OpenStreetMap"
            />
          )}
          <MapEvents />
          <MapCreateEvents />
          <MapSelectionEvents />

          {selectionBounds && (
            <Rectangle
              bounds={selectionBounds}
              pathOptions={{ color: "#0ea5e9", weight: 2, fillColor: "#0ea5e9", fillOpacity: 0.12 }}
            />
          )}
          {selectionCircle && (
            <Circle
              center={selectionCircle.center}
              radius={selectionCircle.radius}
              pathOptions={{ color: "#0ea5e9", weight: 2, fillColor: "#0ea5e9", fillOpacity: 0.12 }}
            />
          )}
          {hierarchyLines.map(line => (
            <Polyline
              key={`hier-${line.key}`}
              positions={line.positions}
              pathOptions={{ color: "#7c3aed", weight: 2, opacity: 0.65, dashArray: "6 6" }}
            />
          ))}

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
            const reviewRole = reviewHighlight
              ? reviewHighlight.primary === uid  ? "primary"
              : reviewHighlight.discards.has(uid) ? "discard"
              : reviewHighlight.neutral.has(uid)  ? "neutral"
              : undefined
              : undefined;
            const isGmaps = !!p.is_gmaps || uid.startsWith("gmaps-");
            const icon = pointIcon({
              missing: hasMissing,
              isAnchor: anchorUid === uid,
              isCandidate: candidateUid === uid,
              isEditing: editingUid === uid,
              reviewRole,
              isGmaps,
              isMain: p.beach_role === "main" || Boolean(p.child_beach_uids?.length),
              isSection: p.beach_role === "section" || Boolean(p.parent_beach_uid),
              isHidden: Boolean(p.is_hidden_beach),
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
                    } else if (mode === "bulkMerge" && view.zoom >= DETAIL_ZOOM) {
                      setAnchorUid(anchorUid === uid ? null : uid);
                      setStatus(anchorUid === uid ? "Bulk merge anchor cleared." : `Bulk merge anchor: ${uid}. Drag a ${bulkShape} around pins to merge.`);
                    } else if (mode === "group" && view.zoom >= DETAIL_ZOOM) {
                      if (!anchorUid) {
                        setAnchorUid(uid);
                        setStatus(`Group main beach: ${uid}. Click section pins to attach them.`);
                      } else if (anchorUid === uid) {
                        setAnchorUid(null);
                        setCandidateUid(null);
                      } else {
                        groupFeatureUnderAnchor(anchorUid, uid);
                      }
                    } else if (mode === "ungroup" && view.zoom >= DETAIL_ZOOM) {
                      ungroupFeature(uid);
                    } else if (mode === "hidden" && view.zoom >= DETAIL_ZOOM) {
                      toggleHiddenBeach(uid);
                    } else if (mode === "edit" && view.zoom >= DETAIL_ZOOM) {
                      onPointClickForEdit(uid);
                    }
                  }
                }}
              >
                {view.zoom >= DETAIL_ZOOM && (mode === "select" || mode === "move") && (
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

          {/* Review highlight overlay — always visible, bypasses Supercluster */}
          {reviewHighlight && fc?.features
            .filter(f => {
              const uid = f.properties.uid!;
              return reviewHighlight.primary === uid
                || reviewHighlight.discards.has(uid)
                || reviewHighlight.neutral.has(uid);
            })
            .map(f => {
              const uid = f.properties.uid!;
              const [lng, lat] = f.geometry.coordinates;
              const role = reviewHighlight.primary === uid ? "primary"
                : reviewHighlight.discards.has(uid) ? "discard"
                : "neutral";
              const icon = pointIcon({
                missing: false, isAnchor: false, isCandidate: false, isEditing: false,
                reviewRole: role,
              });
              return (
                <Marker key={`review-hl-${uid}`} position={[lat, lng]} icon={icon as any} zIndexOffset={1000}>
                  <Popup maxWidth={340}>
                    <div style={{ fontSize: 13 }}>
                      <div style={{ fontWeight: 700, marginBottom: 4 }}>
                        {f.properties.name?.[0] || "(no name)"}
                        {role === "primary" && <span style={{ color: "#22c55e", marginLeft: 6 }}>→ keep</span>}
                        {role === "discard" && <span style={{ color: "#f97316", marginLeft: 6 }}>→ remove</span>}
                      </div>
                      <div style={{ color: "#6b7280", fontSize: 11 }}>{uid}</div>
                    </div>
                  </Popup>
                </Marker>
              );
            })
          }
        </MapContainer>
      </div>

      {/* RIGHT: Editor panels OR Review queue */}
      <div className="right">
        {rightMode === "review" ? (
          <ReviewPanel
            mapRef={mapRef}
            onApplyChange={applyReviewChange}
            onHighlight={(primary, discards, neutral) =>
              setReviewHighlight({ primary, discards: new Set(discards), neutral: new Set(neutral) })
            }
            onClearHighlight={() => setReviewHighlight(null)}
          />
        ) : (
          <>
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
            <AuditRecentPanel />
          </>
        )}
      </div>
    </div>
  );
}

/* -------------------- Top Bar -------------------- */
function TopBar(props: {
  mode: Mode;
  setMode: (m: Mode) => void;
  bulkShape: SelectionShape;
  setBulkShape: (s: SelectionShape) => void;
  onUndo: () => void;
  onSave: () => void;
  onReload: () => void;
  status: string;
  anchorUid: string | null;
  candidateUid: string | null;
  onClearSelection: () => void;
  total: number;
  username: string;
  setUsername: (s: string) => void;
  saveUsername: () => void;
  online: Array<{ id: string; username: string; ipMasked: string; since: number }>;
  rightMode: "editor" | "review";
  onToggleReview: () => void;
  satellite: boolean;
  onToggleSatellite: () => void;
}) {
  const { mode, setMode, onUndo, onSave, onReload, status, anchorUid, candidateUid, onClearSelection,
    total, username, setUsername, saveUsername, online, rightMode, onToggleReview,
    satellite, onToggleSatellite, bulkShape, setBulkShape } = props;

  const ModeBtn = ({ m, label }: { m: typeof mode, label: string }) => (
    <button
      onClick={() => setMode(m)}
      className={`btn ${mode === m ? "is-active" : ""}`}
      title={
        m === "select" ? "View mode (safe) – no edits"
          : m === "merge" ? "Select A then B to merge B into A"
            : m === "bulkMerge" ? "Select an anchor, then draw a shape to merge pins into it"
              : m === "group" ? "Select main beach, then click section pins to attach them"
                : m === "ungroup" ? "Click a section to detach it, or a main beach to detach all sections"
                : m === "hidden" ? "Click a point to toggle hidden / hard-access beach"
            : m === "delete" ? "Click a point to delete"
              : m === "move" ? "Drag a point to move"
                : m === "edit" ? "Click a point to edit in the right panel"
                  : "Click on map to create a new point"
      }
    >
      {label}
    </button>
  );

  return (
    <div className="toolbar">
      <span style={{ fontWeight: 600, marginRight: 8 }}>Mode:</span>
      <ModeBtn m="select" label="Select" />
      <ModeBtn m="merge" label="Merge" />
      <ModeBtn m="bulkMerge" label="Bulk Merge" />
      <ModeBtn m="group" label="Group" />
      <ModeBtn m="ungroup" label="Ungroup" />
      <ModeBtn m="hidden" label="Hidden" />
      <ModeBtn m="delete" label="Delete" />
      <ModeBtn m="move" label="Move" />
      <ModeBtn m="edit" label="Edit" />
      <ModeBtn m="create" label="Create" />

      {(mode === "merge" || mode === "bulkMerge" || mode === "group") && (
        <div className="chips">
          <span className="chip anchor">{mode === "group" ? "Main" : "Anchor"}: {anchorUid ?? "—"}</span>
          <span className="chip candidate">Candidate: {candidateUid ?? "—"}</span>
          {mode === "bulkMerge" && (
            <span className="shape-toggle" aria-label="Bulk merge shape">
              <button className={`shape-btn ${bulkShape === "box" ? "is-active" : ""}`} onClick={() => setBulkShape("box")}>Box</button>
              <button className={`shape-btn ${bulkShape === "circle" ? "is-active" : ""}`} onClick={() => setBulkShape("circle")}>Circle</button>
            </span>
          )}
          <button className="btn" onClick={onClearSelection} title="Clear selection">Clear</button>
        </div>
      )}

      {/* Live counters & identity */}
      <div className="chips" style={{ marginLeft: 8 }}>
        <span className="chip" style={{ background: "#0ea5e9" }}>Beaches: {total.toLocaleString()}</span>
        <span className="chip" style={{ background: "#10b981" }}>Online: {online.length}</span>
      </div>

      <div className="spacer" />

      {/* Presence pills */}
      <div style={{ display: "flex", gap: 6, alignItems: "center", marginRight: 8, flexWrap: "wrap", maxWidth: 340 }}>
        {online.map(u => (
          <Chip key={u.id} color="#374151">{u.username || "guest"} • {u.ipMasked}</Chip>
        ))}
      </div>

      {/* Username editor */}
      <div style={{ display: "flex", gap: 6, alignItems: "center", marginRight: 8 }}>
        <input
          value={username}
          onChange={e => setUsername(e.target.value)}
          placeholder="username"
          className="user-input"
          title="Used in presence and saved history"
        />
        <button className="btn" onClick={saveUsername}>Use</button>
      </div>

      <button
        onClick={onToggleReview}
        className={`btn${rightMode === "review" ? " is-active" : ""}`}
        style={rightMode === "review" ? { background: "#7c3aed", color: "#fff", borderColor: "#7c3aed" } : { borderColor: "#7c3aed", color: "#7c3aed" }}
        title="Toggle AI deduplication review queue"
      >
        {rightMode === "review" ? "✕ Close Review" : "🔍 Review"}
      </button>
      <button
        onClick={onToggleSatellite}
        className={`btn${satellite ? " is-active" : ""}`}
        style={satellite ? { background: "#0ea5e9", color: "#fff", borderColor: "#0ea5e9" } : {}}
        title="Toggle satellite imagery"
      >
        🛰 {satellite ? "Satellite" : "Map"}
      </button>
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
  onClose: () => void;
  onSave: (uid: string, props: Properties) => void;
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
  onClose: () => void;
  onSave: (uid: string, props: Properties) => void;
}) {
  const [draft, setDraft] = useState<Properties>(() => JSON.parse(JSON.stringify(feature.properties)));
  const listToCSV = (v?: any[]) => (Array.isArray(v) ? v.join(", ") : "");
  const csvToList = (s: string) =>
    s.split(",").map(x => x.trim()).filter(x => x.length > 0); // keep this strict for SAVE time

  // NEW: raw text buffer just for the 'name' field
  const [nameText, setNameText] = useState<string>(() => listToCSV(draft.name));

  useEffect(() => {
    // if the feature changes, resync the buffer
    setDraft(JSON.parse(JSON.stringify(feature.properties)));
    setNameText(listToCSV(feature.properties?.name));
  }, [feature]);

  const numCSVToList = (s: string) => s.split(",").map(x => x.trim()).map(x => Number(x.replace(",", "."))).filter(Number.isFinite);
  const setField = (k: keyof Properties, v: any) => setDraft(d => ({ ...d, [k]: v }));
  const setCSV = (k: keyof Properties) => (e: React.ChangeEvent<HTMLInputElement>) => setField(k, csvToList(e.target.value));
  const setNumCSV = (k: keyof Properties) => (e: React.ChangeEvent<HTMLInputElement>) => setField(k, numCSVToList(e.target.value));
  const setJSON = (k: keyof Properties) => (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const v = e.target.value; try { setField(k, v ? JSON.parse(v) : {}); } catch { }
  };

  return (
    <div className="editor-panel">
      <div className="title">Editor</div>
      <div className="field">
        <label>UID</label>
        <input value={draft.uid || ""} onChange={e => setField("uid", e.target.value)} />
      </div>

      <div className="field">
        <label>name (comma-separated)</label>
        <input
          value={nameText}
          onChange={(e) => setNameText(e.target.value)}   // no parsing while typing
          placeholder="e.g. Παραλία X, Beach X"
        />
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
        <div className="field"><label>source</label><input value={listToCSV(asList(draft.source))} onChange={e => setField("source", csvToList(e.target.value))} /></div>
        <div className="field"><label>source_id</label><input value={listToCSV(asList(draft.source_id))} onChange={e => setField("source_id", csvToList(e.target.value))} /></div>
      </div>

      <div className="grid2">
        <div className="field"><label>parent_beach_uid</label><input value={draft.parent_beach_uid || ""} onChange={e => setField("parent_beach_uid", e.target.value || undefined)} /></div>
        <div className="field"><label>child_beach_uids</label><input value={listToCSV(draft.child_beach_uids)} onChange={setCSV("child_beach_uids")} /></div>
        <div className="field"><label>beach_group_id</label><input value={draft.beach_group_id || ""} onChange={e => setField("beach_group_id", e.target.value || undefined)} /></div>
        <div className="field"><label>beach_role</label><input value={draft.beach_role || ""} onChange={e => setField("beach_role", e.target.value || undefined)} placeholder="main or section" /></div>
      </div>

      <label className="check-row">
        <input
          type="checkbox"
          checked={Boolean(draft.is_hidden_beach)}
          onChange={e => setDraft(d => ({
            ...d,
            is_hidden_beach: e.target.checked,
            beach_access_type: e.target.checked ? "hidden_or_hard_to_access" : undefined,
          }))}
        />
        Hidden / hard-access beach
      </label>

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
            name: dedupe(csvToList(nameText)),   
            access_id: dedupe(asList(draft.access_id)),
            type_id: dedupe(asList(draft.type_id)),
            beach_org: dedupe(asList(draft.beach_org)),
            depth_id: dedupe(asList(draft.depth_id)),
            beach_amea: dedupe(asList(draft.beach_amea)),
            purpose: dedupe(asList(draft.purpose)),
            area_size: toFloatList(draft.area_size || []),
            source: dedupe(asList(draft.source)),
            source_id: dedupe(asList(draft.source_id)),
            child_beach_uids: dedupe(asList(draft.child_beach_uids)),
            source_features: dedupe(asList(draft.source_features)),
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
  onToggle: (id: string) => void;
  onRevert: (c: ChangeEntry) => void;
}) {
  return (
    <div className="history">
      <div className="title">History (this session)</div>
      {!changes.length && <div style={{ color: "#6b7280" }}>No edits yet.</div>}
      <div style={{ display: "grid", gap: 10 }}>
        {changes.map((c) => (
          <div key={c.id} className="card">
            <div className="row">
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span className="badge">{c.type}</span>
                <span>{c.summary}</span>
              </div>
              <div className="actions">
                <button onClick={() => onToggle(c.id)} className="btn" style={{ padding: "4px 8px", fontSize: 12 }}>
                  {c.expanded ? "Hide" : "Details"}
                </button>
                <button onClick={() => onRevert(c)} className="btn warn" style={{ padding: "4px 8px", fontSize: 12 }}>
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
                <div className="difflabel" style={{ marginTop: 8 }}>After</div>
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
  const [items, setItems] = useState<Array<{ id: string; ts: number; message: string; user?: string; size: number; features: number }>>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    (async () => {
      setLoading(true); setErr("");
      try {
        const res = await fetch("/api/versions", { cache: "no-store" });
        const data = await res.json();
        if (!res.ok) throw new Error(data?.error || "Failed to load versions");
        setItems(data.items || []);
      } catch (e: any) { setErr(e.message); }
      setLoading(false);
    })();
  }, []);

  return (
    <div className="history" style={{ borderTop: "1px solid #e5e7eb" }}>
      <div className="title">Saved Versions</div>
      {loading && <div className="muted">Loading…</div>}
      {err && <div className="muted" style={{ color: "#ef4444" }}>{err}</div>}
      <div style={{ display: "grid", gap: 10 }}>
        {items.map(v => (
          <div key={v.id} className="card">
            <div className="row">
              <div style={{ display: "flex", flexDirection: "column" }}>
                <div><b>{new Date(v.ts).toLocaleString()}</b> — {v.message || "(no message)"} {v.user ? `• by ${v.user}` : ""}</div>
                <div className="muted" style={{ fontSize: 12 }}>
                  {v.features.toLocaleString()} features • {(v.size / 1024).toFixed(1)} KB
                </div>
              </div>
              <div className="actions">
                <a className="btn" style={{ padding: "4px 8px", fontSize: 12 }} href={`/api/version/${encodeURIComponent(v.id)}`} target="_blank" rel="noreferrer">Download</a>
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
      const r = await fetch("/api/audit/recent?limit=200&committed=1", { cache: "no-store" });
      const j = await r.json();
      setItems(j.items || []);
    })();
  }, []);
  return (
    <div className="history" style={{ borderTop: "1px solid #e5e7eb" }}>
      <div className="title">Recent Audit</div>
      <div style={{ display: "grid", gap: 10 }}>
        {items.map((x, i) => (
          <div key={i} className="card">
            <div className="row" style={{ alignItems: "flex-start" }}>
              <div>
                <div><b>{new Date(x.ts).toLocaleString()}</b> • {x.user}</div>
                <div className="muted" style={{ fontSize: 12 }}>{x.type} — {x.summary}</div>
                <div className="muted" style={{ fontSize: 12 }}>uids: {x.uids?.join(", ")}</div>
              </div>
            </div>
          </div>
        ))}
        {!items.length && <div className="muted">No audit entries yet.</div>}
      </div>
    </div>
  );
}
