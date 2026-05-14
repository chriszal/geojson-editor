#!/usr/bin/env python3
"""
Batch-apply all auto_approved changes from proposed_changes.json to current.json.

Replicates the same merge logic as MapEditor.tsx:
  - DUPLICATE / SINGLE_BEACH: keep primary, delete discards, merge their properties in
  - LONG_SECTIONS (phase 4): delete confirmed duplicates, rename section points
"""
from __future__ import annotations
import json, sys, io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT     = Path(__file__).parent.parent
DATA     = ROOT / "data_new"
BEACHES  = DATA / "current.json"
PROPOSED = DATA / "proposed_changes.json"


def as_list(v):
    if isinstance(v, list): return v
    if v is None: return []
    return [v]

def dedupe(lst):
    seen, out = set(), []
    for x in lst:
        k = json.dumps(x, sort_keys=True, ensure_ascii=False)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out

def merge_props(A: dict, B: dict) -> dict:
    out = dict(A)
    for key in ["name", "access_id", "type_id", "beach_org", "depth_id", "beach_amea", "purpose"]:
        out[key] = dedupe(as_list(A.get(key)) + as_list(B.get(key)))
    out["area_size"] = dedupe(
        [x for x in as_list(A.get("area_size")) + as_list(B.get("area_size"))
         if isinstance(x, (int, float)) or (isinstance(x, str) and x.replace(".","").isdigit())]
    )
    out["tags"]   = {**(A.get("tags") or {}), **(B.get("tags") or {})}
    out["source"] = dedupe(as_list(A.get("source")) + as_list(B.get("source")))
    out["source_id"] = dedupe(as_list(A.get("source_id")) + as_list(B.get("source_id")))
    merged_from = dedupe(
        as_list(A.get("merged_from_uids")) +
        as_list(B.get("merged_from_uids")) +
        ([A["uid"]] if A.get("uid") else []) +
        ([B["uid"]] if B.get("uid") else [])
    )
    out["merged_from_uids"] = merged_from
    return out


def main():
    beaches  = json.loads(BEACHES.read_text(encoding="utf-8"))
    proposed = json.loads(PROPOSED.read_text(encoding="utf-8"))

    features = {f["properties"]["uid"]: f for f in beaches["features"] if f.get("properties", {}).get("uid")}

    auto = [c for c in proposed["changes"] if c.get("status") == "auto_approved"]
    print(f"Auto-approved changes to apply: {len(auto)}")

    applied = skipped = renamed = deleted_total = 0

    for ch in auto:
        phase         = ch.get("phase", 1)
        proposed_act  = ch.get("proposed_action", "")
        primary_uid   = ch.get("primary_uid")
        discard_uids  = ch.get("discard_uids") or []

        # ── Phase 4 LONG_SECTIONS: rename sections + delete confirmed duplicates ──
        if phase == 4 and proposed_act == "REVIEW_SECTIONS":
            sections = ch.get("suggested_sections") or []
            uid_to_name: dict[str, str] = {}
            for sec in sections:
                name = sec.get("suggested_name")
                if not name:
                    continue
                for uid in sec.get("uids", []):
                    if uid not in discard_uids:
                        uid_to_name[uid] = name

            n_renamed = n_deleted = 0
            for uid, name in uid_to_name.items():
                if uid in features:
                    features[uid]["properties"]["name"] = [name]
                    n_renamed += 1
            for uid in discard_uids:
                if uid in features:
                    del features[uid]
                    n_deleted += 1

            renamed        += n_renamed
            deleted_total  += n_deleted
            applied        += 1
            continue

        # ── Standard merge: keep primary, delete discards ──────────────────────
        if not primary_uid or not discard_uids:
            skipped += 1
            continue

        if primary_uid not in features:
            skipped += 1
            continue

        primary_feat = features[primary_uid]
        merged_props = dict(primary_feat["properties"])

        for uid in discard_uids:
            if uid not in features:
                continue
            merged_props = merge_props(merged_props, features[uid]["properties"])
            del features[uid]
            deleted_total += 1

        merged_props["uid"] = primary_uid
        primary_feat["properties"] = merged_props
        applied += 1

    # Mark all auto_approved as approved in proposed_changes.json
    for ch in proposed["changes"]:
        if ch.get("status") == "auto_approved":
            ch["status"] = "approved"

    # Rebuild feature list preserving original order
    uid_order = [f["properties"]["uid"] for f in beaches["features"] if f.get("properties", {}).get("uid")]
    beaches["features"] = [features[uid] for uid in uid_order if uid in features]

    BEACHES.write_text(json.dumps(beaches, ensure_ascii=False), encoding="utf-8")
    PROPOSED.write_text(json.dumps(proposed, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Applied:  {applied} changes")
    print(f"Deleted:  {deleted_total} duplicate points removed")
    print(f"Renamed:  {renamed} section points renamed")
    print(f"Skipped:  {skipped} (primary not found or nothing to do)")
    print(f"Beaches remaining: {len(beaches['features'])}")


if __name__ == "__main__":
    main()
