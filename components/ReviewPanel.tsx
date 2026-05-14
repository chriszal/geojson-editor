"use client";

import React, { useEffect, useMemo, useState } from "react";
import type { Map as LeafletMap } from "leaflet";

/* ── Types ──────────────────────────────────────────────────────────────────── */
export type ReviewPoint = {
  uid: string;
  name: string[];
  coordinates: [number, number]; // [lon, lat]
  properties: Record<string, any>;
};

export type ReviewChange = {
  id: string;
  type: "DUPLICATE" | "SUB_PARTS" | "DISTINCT" | "UNKNOWN";
  phase?: 1 | 2 | 3 | 4;
  p2_cluster_type?: "SINGLE_BEACH" | "LONG_SECTIONS" | "DISTINCT";
  p3_cluster_type?: "SINGLE_BEACH" | "LONG_SECTIONS" | "DISTINCT";
  p4_unified_type?: "SINGLE_BEACH" | "LONG_SECTIONS" | "DISTINCT";
  confidence: number;
  reasoning: string;
  satellite_analyzed: boolean;
  points: ReviewPoint[];
  proposed_action: "MERGE_INTO_PRIMARY" | "CREATE_HIERARCHY" | "KEEP_ALL" | "NO_CHANGE" | "REVIEW_SECTIONS";
  primary_uid: string | null;
  discard_uids: string[];
  breaks?: Array<{ between: [string, string]; break_type: string; confidence: number }>;
  suggested_groups?: Array<{ uids: string[]; suggested_label: string }>;
  suggested_sections?: Array<{ label: string; suggested_name: string; uids: string[] }>;
  osm_beach_names?: string[];
  canonical_name?: string;
  source_changes?: string[];
  action_per_uid?: Record<string, "keep" | "keep_primary" | "delete">;
  status: "pending_review" | "auto_approved" | "approved" | "rejected" | "superseded";
  created_at: string;
};

/* ── Constants ──────────────────────────────────────────────────────────────── */
const TYPE_COLOR: Record<string, string> = {
  DUPLICATE: "#ef4444",
  SUB_PARTS: "#f97316",
  DISTINCT:  "#3b82f6",
  UNKNOWN:   "#6b7280",
};

const TYPE_LABEL: Record<string, string> = {
  DUPLICATE: "Duplicate",
  SUB_PARTS: "Sub-parts",
  DISTINCT:  "Distinct",
  UNKNOWN:   "Unanalyzed",
};

const ACTION_LABEL: Record<string, string> = {
  MERGE_INTO_PRIMARY: "Merge into single point",
  CREATE_HIERARCHY:   "Group as parent / children",
  KEEP_ALL:           "Keep all as-is",
  NO_CHANGE:          "No change",
  REVIEW_SECTIONS:    "Review sections / naming",
};

const PAGE_SIZE = 25;

function confColor(c: number) {
  if (c < 0.6) return "#ef4444";
  if (c < 0.8) return "#f59e0b";
  return "#22c55e";
}
function confLabel(c: number) {
  if (c < 0.6) return "Low";
  if (c < 0.8) return "Med";
  return "High";
}

/* ── Main component ─────────────────────────────────────────────────────────── */
export default function ReviewPanel({
  mapRef,
  onApplyChange,
  onHighlight,
  onClearHighlight,
}: {
  mapRef: React.MutableRefObject<LeafletMap | null>;
  onApplyChange: (change: ReviewChange) => void;
  onHighlight: (primary: string | null, discards: string[], neutral: string[]) => void;
  onClearHighlight: () => void;
}) {
  const [changes, setChanges]             = useState<ReviewChange[]>([]);
  const [meta, setMeta]                   = useState<any>(null);
  const [loading, setLoading]             = useState(true);
  const [error, setError]                 = useState("");
  const [saving, setSaving]               = useState<Set<string>>(new Set());
  const [expandedImages, setExpandedImages] = useState<Set<string>>(new Set());

  // Filters
  const [filterStatus, setFilterStatus]   = useState("pending_review");
  const [filterType, setFilterType]       = useState("all");
  const [filterPhase, setFilterPhase]     = useState("all");
  const [confCeil, setConfCeil]           = useState(1.0);
  const [visibleCount, setVisibleCount]   = useState(PAGE_SIZE);

  /* Fetch on mount */
  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const r = await fetch("/api/review", { cache: "no-store" });
        const d = await r.json();
        setMeta(d.meta ?? null);
        setChanges((d.changes ?? []).filter((c: any) => c.type !== "ISOLATED" && c.status !== "superseded"));
      } catch (e: any) {
        setError(e.message);
      }
      setLoading(false);
    })();
  }, []);

  /* Derived stats */
  const stats = useMemo(() => ({
    pending:      changes.filter(c => c.status === "pending_review").length,
    approved:     changes.filter(c => c.status === "approved").length,
    rejected:     changes.filter(c => c.status === "rejected").length,
    autoApproved: changes.filter(c => c.status === "auto_approved").length,
  }), [changes]);

  /* Filtered + sorted list */
  const filtered = useMemo(() => {
    return changes
      .filter(c => filterStatus === "all" || c.status === filterStatus)
      .filter(c => filterType  === "all" || c.type   === filterType)
      .filter(c => filterPhase === "all" || String(c.phase ?? 1) === filterPhase)
      .filter(c => c.confidence <= confCeil)
      .sort((a, b) => a.confidence - b.confidence);
  }, [changes, filterStatus, filterType, filterPhase, confCeil]);

  /* API helpers */
  const patchStatus = async (id: string, status: string) => {
    setSaving(s => new Set(s).add(id));
    try {
      await fetch("/api/review", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, status }),
      });
      setChanges(cs => cs.map(c => c.id === id ? { ...c, status: status as any } : c));
    } finally {
      setSaving(s => { const n = new Set(s); n.delete(id); return n; });
    }
  };

  const handleApprove = async (effective: ReviewChange) => {
    onApplyChange(effective);
    await patchStatus(effective.id, "approved");
    onClearHighlight();
  };

  const handleReject = async (change: ReviewChange) => {
    await patchStatus(change.id, "rejected");
    onClearHighlight();
  };

  const flyTo = (effective: ReviewChange) => {
    const map = mapRef.current;
    if (!map) return;
    const lons = effective.points.map(p => p.coordinates[0]);
    const lats = effective.points.map(p => p.coordinates[1]);
    const cLat = (Math.min(...lats) + Math.max(...lats)) / 2;
    const cLon = (Math.min(...lons) + Math.max(...lons)) / 2;
    map.flyTo([cLat, cLon], 16, { animate: true, duration: 0.6 });

    const primary    = effective.primary_uid ?? null;
    const discards   = effective.discard_uids;
    const discardSet = new Set(discards);
    const neutral    = effective.points
      .map(p => p.uid)
      .filter(uid => uid !== primary && !discardSet.has(uid));
    onHighlight(primary, discards, neutral);
  };

  const toggleImage = (id: string) =>
    setExpandedImages(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  /* ── Empty states ─────────────────────────────────────────────────────────── */
  if (loading) return (
    <div style={{ padding: 24, color: "#6b7280", fontSize: 13 }}>Loading review queue…</div>
  );
  if (error) return (
    <div style={{ padding: 24, color: "#ef4444", fontSize: 13 }}>Error: {error}</div>
  );
  if (!meta) return (
    <div style={{ padding: 20 }}>
      <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 6 }}>No pipeline output found</div>
      <div style={{ fontSize: 12, color: "#6b7280", lineHeight: 1.6 }}>
        Run the deduplication pipeline first:
        <pre style={{ marginTop: 8, padding: "6px 10px", background: "#f3f4f6", borderRadius: 4, fontSize: 11 }}>
          python scripts/beach_dedup_pipeline.py
        </pre>
        Then reload this panel.
      </div>
    </div>
  );

  /* ── Render ───────────────────────────────────────────────────────────────── */
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>

      {/* Stats bar */}
      <div style={{ padding: "10px 12px", borderBottom: "1px solid #e5e7eb", background: "#f9fafb", flexShrink: 0 }}>
        <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 7 }}>AI Review Queue</div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {[
            { label: "Pending",    count: stats.pending,      color: "#f59e0b" },
            { label: "Approved",   count: stats.approved,     color: "#22c55e" },
            { label: "Rejected",   count: stats.rejected,     color: "#ef4444" },
            { label: "Auto-done",  count: stats.autoApproved, color: "#6b7280" },
          ].map(({ label, count, color }) => (
            <span key={label} style={{
              fontSize: 11, padding: "2px 8px", borderRadius: 999, fontWeight: 600,
              background: color + "22", color, border: `1px solid ${color}55`,
            }}>
              {label}: {count.toLocaleString()}
            </span>
          ))}
        </div>
        <div style={{ fontSize: 11, color: "#9ca3af", marginTop: 5 }}>
          {meta.total_isolated_safe?.toLocaleString()} isolated safe •{" "}
          {meta.total_input_features?.toLocaleString()} total input features
          {meta.phase2_total ? ` • ${meta.phase2_total} phase-2 clusters (${meta.phase2_pending ?? 0} pending)` : ""}
        </div>
      </div>

      {/* Filters */}
      <div style={{
        padding: "8px 12px", borderBottom: "1px solid #e5e7eb",
        display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", flexShrink: 0,
      }}>
        <select
          value={filterStatus}
          onChange={e => { setFilterStatus(e.target.value); setVisibleCount(PAGE_SIZE); }}
          style={selectStyle}
        >
          <option value="pending_review">Pending review</option>
          <option value="all">All statuses</option>
          <option value="approved">Approved</option>
          <option value="rejected">Rejected</option>
          <option value="auto_approved">Auto-approved</option>
        </select>

        <select
          value={filterType}
          onChange={e => { setFilterType(e.target.value); setVisibleCount(PAGE_SIZE); }}
          style={selectStyle}
        >
          <option value="all">All types</option>
          <option value="DUPLICATE">Duplicates only</option>
          <option value="SUB_PARTS">Sub-parts / Sections</option>
          <option value="DISTINCT">Distinct only</option>
          <option value="UNKNOWN">Unanalyzed only</option>
        </select>

        <select
          value={filterPhase}
          onChange={e => { setFilterPhase(e.target.value); setVisibleCount(PAGE_SIZE); }}
          style={selectStyle}
        >
          <option value="all">All phases</option>
          <option value="1">Phase 1 (150 m radius)</option>
          <option value="2">Phase 2 (name + chain)</option>
          <option value="3">Phase 3 (coastal 500 m)</option>
          <option value="4">Phase 4 (reconciled)</option>
        </select>

        <label style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12 }}>
          <span style={{ color: "#6b7280" }}>Conf ≤</span>
          <input
            type="range" min={0} max={1} step={0.05}
            value={confCeil}
            onChange={e => { setConfCeil(+e.target.value); setVisibleCount(PAGE_SIZE); }}
            style={{ width: 64, cursor: "pointer" }}
          />
          <span style={{ fontWeight: 700, color: confColor(confCeil), minWidth: 32 }}>
            {Math.round(confCeil * 100)}%
          </span>
        </label>

        <span style={{ fontSize: 11, color: "#9ca3af", marginLeft: "auto" }}>
          {filtered.length.toLocaleString()} shown
        </span>
      </div>

      {/* Change list */}
      <div style={{ flex: 1, overflowY: "auto", padding: "10px 12px" }}>
        {filtered.length === 0 && (
          <div style={{ color: "#9ca3af", fontSize: 13, textAlign: "center", paddingTop: 32 }}>
            Nothing matches the current filters.
          </div>
        )}
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {filtered.slice(0, visibleCount).map(change => (
            <ChangeCard
              key={change.id}
              change={change}
              isSaving={saving.has(change.id)}
              showImage={expandedImages.has(change.id)}
              onToggleImage={() => toggleImage(change.id)}
              onFlyTo={(effective) => flyTo(effective)}
              onApprove={(effective) => handleApprove(effective)}
              onReject={() => handleReject(change)}
            />
          ))}
        </div>

        {filtered.length > visibleCount && (
          <button
            onClick={() => setVisibleCount(v => v + PAGE_SIZE)}
            style={{
              width: "100%", marginTop: 12, padding: "8px 0",
              background: "#f3f4f6", border: "1px solid #d1d5db",
              borderRadius: 6, cursor: "pointer", fontSize: 12, color: "#374151", fontWeight: 600,
            }}
          >
            Show {Math.min(PAGE_SIZE, filtered.length - visibleCount)} more
            &nbsp;({(filtered.length - visibleCount).toLocaleString()} remaining)
          </button>
        )}
      </div>
    </div>
  );
}

/* ── ChangeCard ──────────────────────────────────────────────────────────────── */
function ChangeCard({
  change, isSaving, showImage,
  onToggleImage, onFlyTo, onApprove, onReject,
}: {
  change: ReviewChange;
  isSaving: boolean;
  showImage: boolean;
  onToggleImage: () => void;
  onFlyTo: (effective: ReviewChange) => void;
  onApprove: (effective: ReviewChange) => void;
  onReject: () => void;
}) {
  // Local override: which point the user wants to keep
  const [overridePrimary, setOverridePrimary] = useState<string | null>(null);

  // Compute the effective change (with any user override applied)
  const effective = useMemo((): ReviewChange => {
    if (!overridePrimary) return change;
    const discards = change.points.map(p => p.uid).filter(uid => uid !== overridePrimary);
    return {
      ...change,
      primary_uid:    overridePrimary,
      discard_uids:   discards,
      proposed_action: "MERGE_INTO_PRIMARY",
    };
  }, [change, overridePrimary]);

  const conf       = change.confidence;
  const typeColor  = TYPE_COLOR[change.type] ?? "#6b7280";
  const isTerminal = change.status !== "pending_review";
  const isOverridden = overridePrimary !== null;

  return (
    <div style={{
      border: `1px solid ${typeColor}44`,
      borderLeft: `3px solid ${typeColor}`,
      borderRadius: 6,
      background: isTerminal ? "#fafafa" : "#fff",
      opacity: isTerminal ? 0.75 : 1,
    }}>
      {/* ── Header row ────────────────────────────────────────────────────── */}
      <div style={{ padding: "8px 10px", display: "flex", alignItems: "center", gap: 8 }}>
        {/* Type badge */}
        <span style={{
          fontSize: 10, fontWeight: 700, padding: "2px 6px", borderRadius: 4,
          background: typeColor, color: "#fff", letterSpacing: 0.5, flexShrink: 0,
        }}>
          {TYPE_LABEL[change.type] ?? change.type}
        </span>

        {/* Confidence bar + label */}
        <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
          <div style={{ flex: 1, height: 5, borderRadius: 3, background: "#e5e7eb", overflow: "hidden" }}>
            <div style={{
              height: "100%",
              width: `${conf * 100}%`,
              background: confColor(conf),
              borderRadius: 3,
            }} />
          </div>
          <span style={{ fontSize: 11, fontWeight: 700, color: confColor(conf), flexShrink: 0 }}>
            {confLabel(conf)} {Math.round(conf * 100)}%
          </span>
        </div>

        {/* Terminal status pill */}
        {isTerminal && (
          <span style={{
            fontSize: 10, padding: "1px 6px", borderRadius: 3, fontWeight: 700, flexShrink: 0,
            background: change.status === "approved" ? "#dcfce7" : "#fee2e2",
            color:      change.status === "approved" ? "#166534" : "#991b1b",
          }}>
            {change.status === "approved" ? "✓ Applied" : "✗ Skipped"}
          </span>
        )}
      </div>

      {/* ── Sub-header: action + point count ──────────────────────────────── */}
      <div style={{ padding: "0 10px 6px", fontSize: 11, color: "#6b7280", display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <span>
          {change.points.length} point{change.points.length !== 1 ? "s" : ""} ·{" "}
          {ACTION_LABEL[effective.proposed_action]}
          {change.satellite_analyzed ? " · 🛰" : ""}
        </span>
        {(change.phase === 2 || change.phase === 3 || change.phase === 4) && (
          <span style={{ fontSize: 10, padding: "1px 5px", borderRadius: 3, fontWeight: 700,
            background: change.phase === 4 ? "#ccfbf1" : change.phase === 3 ? "#fef3c7" : "#ede9fe",
            color:      change.phase === 4 ? "#0f766e" : change.phase === 3 ? "#92400e" : "#6d28d9",
          }}>
            {change.phase === 4 ? "⚡ Phase 4 · reconciled" : `Phase ${change.phase}${change.phase === 3 ? " 🌊" : ""}`}
          </span>
        )}
        {change.phase === 4 && change.source_changes && change.source_changes.length > 0 && (
          <span style={{ fontSize: 10, padding: "1px 5px", borderRadius: 3,
            background: "#f0fdfa", color: "#0f766e", border: "1px solid #99f6e4",
          }}>
            merges {change.source_changes.length} clusters
          </span>
        )}
        {isOverridden && (
          <span style={{ fontSize: 10, padding: "1px 5px", borderRadius: 3, background: "#fef3c7", color: "#92400e", fontWeight: 600 }}>
            ✎ overridden
          </span>
        )}
        {change.osm_beach_names && change.osm_beach_names.length > 0 && (
          <span style={{ fontSize: 10, padding: "1px 5px", borderRadius: 3, background: "#f0fdf4", color: "#166534", fontWeight: 600 }}>
            OSM: {change.osm_beach_names.slice(0, 2).join(" / ")}
          </span>
        )}
        {(change as any).canonical_name && (
          <span style={{ fontSize: 10, padding: "1px 5px", borderRadius: 3, background: "#fef9c3", color: "#854d0e", fontWeight: 600 }}>
            ✎ canonical: {(change as any).canonical_name}
          </span>
        )}
      </div>

      {/* ── AI reasoning ──────────────────────────────────────────────────── */}
      {change.reasoning && (
        <div style={{
          margin: "0 10px 8px", padding: "6px 8px",
          background: "#f8fafc", borderRadius: 4, border: "1px solid #e2e8f0",
          fontSize: 12, color: "#374151", lineHeight: 1.55,
          fontStyle: "italic",
        }}>
          "{change.reasoning}"
        </div>
      )}

      {/* ── Satellite image (lazy) ─────────────────────────────────────────── */}
      {change.satellite_analyzed && (
        <div style={{ margin: "0 10px 8px" }}>
          {showImage ? (
            <>
              <img
                src={`/api/review/tile/${change.id}`}
                alt="Satellite view"
                style={{ width: "100%", borderRadius: 4, display: "block", marginBottom: 4 }}
                onError={e => { (e.target as HTMLImageElement).style.display = "none"; }}
              />
              <button onClick={onToggleImage} style={ghostBtn}>Hide image</button>
            </>
          ) : (
            <button onClick={onToggleImage} style={{
              ...ghostBtn, color: "#3b82f6", borderColor: "#bfdbfe",
            }}>
              🛰 Show satellite image
            </button>
          )}
        </div>
      )}

      {/* ── Point cards ───────────────────────────────────────────────────── */}
      <div style={{ margin: "0 10px 8px", display: "flex", flexDirection: "column", gap: 4 }}>
        {change.points.map(pt => {
          // Phase 4 uses action_per_uid; other phases use primary_uid / discard_uids
          const p4action   = change.phase === 4 ? change.action_per_uid?.[pt.uid] : undefined;
          const isPrimary  = p4action === "keep_primary" || (p4action === undefined && pt.uid === effective.primary_uid);
          const isDiscard  = p4action === "delete"        || (p4action === undefined && effective.discard_uids.includes(pt.uid));
          const gmaps      = (change as any).gmaps?.[pt.uid];
          return (
            <div key={pt.uid} style={{
              padding: "5px 8px", borderRadius: 4, fontSize: 11,
              background: isPrimary ? "#f0fdf4" : isDiscard ? "#fff1f2" : "#f9fafb",
              border: `1px solid ${isPrimary ? "#bbf7d0" : isDiscard ? "#fecdd3" : "#e5e7eb"}`,
            }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 4 }}>
                <span style={{ fontWeight: 600, color: "#111827", minWidth: 0 }}>
                  {pt.name?.[0] || <em style={{ color: "#9ca3af" }}>no name</em>}
                  {pt.name?.length > 1 && (
                    <span style={{ color: "#6b7280", fontWeight: 400 }}> +{pt.name.length - 1} alias{pt.name.length > 2 ? "es" : ""}</span>
                  )}
                  {gmaps?.on_osm_beach && (
                    <span style={{ marginLeft: 5, fontSize: 10, color: "#15803d" }}>
                      🏖 {gmaps.osm_beach_name && gmaps.osm_beach_name !== pt.name?.[0]
                        ? `OSM: "${gmaps.osm_beach_name}"` : "on OSM beach"}
                    </span>
                  )}
                  {gmaps?.found && (
                    <span style={{ marginLeft: 5, fontSize: 10, color: "#1d4ed8", fontWeight: 400 }}>
                      📍 {gmaps.name !== pt.name?.[0] ? `Google: "${gmaps.name}"` : "on Google Maps"}
                      {gmaps.user_ratings > 0 && ` ⭐${gmaps.rating} (${gmaps.user_ratings})`}
                    </span>
                  )}
                  {gmaps && !gmaps.found && !gmaps.on_osm_beach && (
                    <span style={{ marginLeft: 5, fontSize: 10, color: "#9ca3af" }}>not verified</span>
                  )}
                </span>
                <div style={{ display: "flex", gap: 4, alignItems: "center", flexShrink: 0 }}>
                  {(isPrimary || isDiscard) && (
                    <span style={{
                      fontSize: 10, padding: "1px 5px", borderRadius: 3, fontWeight: 700,
                      background: isPrimary ? "#22c55e" : "#f97316", color: "#fff",
                    }}>
                      {isPrimary ? "→ keep" : "→ remove"}
                    </span>
                  )}
                  {!isTerminal && !isPrimary && (
                    <button
                      onClick={() => setOverridePrimary(pt.uid)}
                      title="Make this the point that is kept"
                      style={{
                        fontSize: 10, padding: "1px 6px", borderRadius: 3, cursor: "pointer",
                        background: "#eff6ff", color: "#1d4ed8",
                        border: "1px solid #bfdbfe", fontWeight: 600,
                      }}
                    >
                      ★ Keep this
                    </button>
                  )}
                </div>
              </div>
              <div style={{ color: "#6b7280", marginTop: 2 }}>
                {pt.coordinates[1].toFixed(5)}°N,&nbsp;{pt.coordinates[0].toFixed(5)}°E
              </div>
              <div style={{ color: "#d1d5db", fontSize: 10, marginTop: 1, fontFamily: "monospace" }}>
                {pt.uid}
              </div>
            </div>
          );
        })}
      </div>

      {/* ── Phase-4 suggested sections ────────────────────────────────────── */}
      {change.phase === 4 && change.suggested_sections && change.suggested_sections.length > 0 && (
        <div style={{ margin: "0 10px 8px", padding: "6px 8px", background: "#f0fdfa", borderRadius: 4, border: "1px solid #99f6e4" }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: "#0f766e", marginBottom: 4 }}>RECONCILED SECTIONS</div>
          {change.suggested_sections.map((sec, i) => (
            <div key={i} style={{ fontSize: 11, color: "#374151", marginBottom: 2 }}>
              <strong>{sec.label}  {sec.suggested_name && <span style={{ color: "#0f766e" }}>"{sec.suggested_name}"</span>}:</strong>{" "}
              {sec.uids.map(uid => {
                const pt = change.points.find(p => p.uid === uid);
                return pt ? (Array.isArray(pt.name) ? pt.name[0] : pt.name) || uid.slice(-6) : uid.slice(-6);
              }).join(", ")}
            </div>
          ))}
          {change.breaks && change.breaks.length > 0 && (
            <div style={{ marginTop: 4, fontSize: 10, color: "#0f766e" }}>
              {(change.breaks as any[]).map((b, bi) => (
                <div key={bi}>⚡ {b.type || b.break_type}: {b.description || ""}</div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── Phase-2/3 section groups (LONG_SECTIONS) ────────────────────── */}
      {(change.phase === 2 || change.phase === 3) && change.suggested_groups && change.suggested_groups.length > 0 && (
        <div style={{ margin: "0 10px 8px", padding: "6px 8px", background: "#f5f3ff", borderRadius: 4, border: "1px solid #ddd6fe" }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: "#6d28d9", marginBottom: 4 }}>AI SUGGESTED SECTIONS</div>
          {change.suggested_groups.map((g, gi) => (
            <div key={gi} style={{ fontSize: 11, color: "#374151", marginBottom: 2 }}>
              <strong>{g.suggested_label}:</strong>{" "}
              {g.uids.map(uid => {
                const pt = change.points.find(p => p.uid === uid);
                return pt ? (pt.name?.[0] || uid.slice(-6)) : uid.slice(-6);
              }).join(", ")}
            </div>
          ))}
          {change.breaks && change.breaks.length > 0 && (
            <div style={{ marginTop: 4, fontSize: 10, color: "#7c3aed" }}>
              {change.breaks.map((b, bi) => (
                <div key={bi}>⚡ {b.break_type} between {b.between[0].slice(-6)} – {b.between[1].slice(-6)} (conf {Math.round(b.confidence * 100)}%)</div>
              ))}
            </div>
          )}
        </div>
      )}
      {(change.phase === 3) && change.suggested_sections && change.suggested_sections.length > 0 && (
        <div style={{ margin: "0 10px 8px", padding: "6px 8px", background: "#f5f3ff", borderRadius: 4, border: "1px solid #ddd6fe" }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: "#6d28d9", marginBottom: 4 }}>AI SUGGESTED SECTIONS</div>
          {change.suggested_sections.map((sec, i) => (
            <div key={i} style={{ fontSize: 11, color: "#374151", marginBottom: 2 }}>
              <strong>{sec.label}{sec.suggested_name ? ` — "${sec.suggested_name}"` : ""}:</strong>{" "}
              {sec.uids.map(uid => {
                const pt = change.points.find(p => p.uid === uid);
                return pt ? (Array.isArray(pt.name) ? pt.name[0] : pt.name) || uid.slice(-6) : uid.slice(-6);
              }).join(", ")}
            </div>
          ))}
        </div>
      )}

      {/* ── Action buttons ────────────────────────────────────────────────── */}
      {!isTerminal && (
        <div style={{ padding: "0 10px 10px", display: "flex", gap: 6 }}>
          {isOverridden && (
            <button
              onClick={() => setOverridePrimary(null)}
              style={{ ...actionBtn, background: "#fef3c7", color: "#92400e" }}
              title="Reset to AI's original suggestion"
            >
              ↺
            </button>
          )}
          <button onClick={() => onFlyTo(effective)} style={{ ...actionBtn, background: "#f3f4f6", color: "#374151" }}>
            ↗ Fly to
          </button>
          <button
            onClick={() => onApprove(effective)}
            disabled={isSaving}
            style={{ ...actionBtn, flex: 1, background: "#dcfce7", color: "#166534" }}
          >
            {isSaving ? "…" : "✓ Apply"}
          </button>
          <button
            onClick={onReject}
            disabled={isSaving}
            style={{ ...actionBtn, background: "#fee2e2", color: "#991b1b" }}
          >
            {isSaving ? "…" : "✗ Skip"}
          </button>
        </div>
      )}
    </div>
  );
}

/* ── Shared styles ───────────────────────────────────────────────────────────── */
const selectStyle: React.CSSProperties = {
  fontSize: 12, padding: "3px 6px",
  border: "1px solid #d1d5db", borderRadius: 4,
  background: "#fff", cursor: "pointer",
};

const ghostBtn: React.CSSProperties = {
  fontSize: 11, padding: "3px 8px",
  background: "none", border: "1px solid #e5e7eb",
  borderRadius: 4, cursor: "pointer", color: "#6b7280",
};

const actionBtn: React.CSSProperties = {
  padding: "5px 10px", fontSize: 12, fontWeight: 600,
  border: "none", borderRadius: 4, cursor: "pointer",
};
