# -*- coding: utf-8 -*-
"""
Stage 1 of beach enrichment.

a) Deterministic field defaults:
   - purpose mentions Ομπρελοκαθίσματα/Τραπεζοκαθίσματα/Καντίνες/Θαλάσσια Σπορ -> beach_org "1"
   - purpose mentions Υποδομές ΑΜΕΑ -> beach_amea "1"
   - hidden_or_hard_to_access tag -> beach_org "0", access_id "3" (δύσκολη)
   - standalone beaches with no name & no data at all -> beach_org "0" (obviously unorganised)
   (fill-only: existing values are never overwritten)

b) Builds per-beach review bundles for the LLM pass:
   - reviews from gmaps_verification(.wide).json matched by uid
   - candidate filter: non-business candidates within 300 m, or name-matching any alias
   - group anchors absorb reviews of their sections
   - top 12 most informative reviews per beach, each truncated to 600 chars

In  : data_new/current_normalized.json
Out : data_new/current_enriched.json      (deterministic defaults applied)
      data_new/review_bundles.json        (input for mine_reviews_llm.py)
"""
import json, re, sys, io, unicodedata
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, r"d:\Program Files\geojson-editor\scripts")
from normalize_titles import translit_key, looks_business, strip_beach_words

ROOT = r"d:\Program Files\geojson-editor\data_new"
SRC = ROOT + r"\current_normalized.json"
OUT = ROOT + r"\current_enriched.json"
BUNDLES = ROOT + r"\review_bundles.json"

ORG_PURPOSES = ("Ομπρελοκαθίσματα", "Τραπεζοκαθίσματα", "Καντίνες", "Θαλάσσια Σπορ")

def main():
    with open(SRC, encoding="utf-8") as f:
        data = json.load(f)
    feats = data["features"]

    # ---------- a) deterministic defaults ----------
    stats = defaultdict(int)
    for ft in feats:
        p = ft["properties"]
        purpose = p.get("purpose") or []
        org = p.get("beach_org") or []
        amea = p.get("beach_amea") or []
        acc = p.get("access_id") or []

        if not org and any(x in pv for pv in purpose for x in ORG_PURPOSES):
            p["beach_org"] = ["1"]; stats["org_from_purpose"] += 1
        if not amea and any("ΑΜΕΑ" in pv for pv in purpose):
            p["beach_amea"] = ["1"]; stats["amea_from_purpose"] += 1

        hidden = p.get("is_hidden_beach") or (p.get("tags") or {}).get("hidden_or_hard_to_access")
        if hidden:
            if not (p.get("beach_org") or []):
                p["beach_org"] = ["0"]; stats["org_from_hidden"] += 1
            if not acc:
                p["access_id"] = ["3"]; stats["access_from_hidden"] += 1

        # standalone, nameless, no data at all -> unorganised
        if (not (p.get("beach_org") or [])
                and p.get("beach_role") is None
                and not p.get("parent_beach_uid")
                and not p.get("name_el") and not p.get("name_en")
                and not p.get("rating") and not p.get("user_ratings")
                and not p.get("phone") and not p.get("website")
                and all(pv == "Άγνωστη χρήση" for pv in purpose)):
            p["beach_org"] = ["0"]; stats["org_from_nodata"] += 1

    # ---------- b) review bundles ----------
    # merge BOTH files per key: the wide file re-scraped uids the narrow file
    # failed on, so candidates must be concatenated (dedupe by href), never
    # first-file-wins
    ver = {}
    for fn in ("gmaps_verification.json", "gmaps_verification_wide.json"):
        with open(rf"{ROOT}\{fn}", encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if k not in ver:
                    ver[k] = {"beaches": list(v.get("beaches") or [])}
                else:
                    have = {b.get("href") for b in ver[k]["beaches"] if b.get("href")}
                    for b in v.get("beaches") or []:
                        if not b.get("href") or b["href"] not in have:
                            ver[k]["beaches"].append(b)

    byuid = {ft["properties"]["uid"]: ft for ft in feats}

    def name_keys(p):
        keys = set()
        for n in (p.get("name") or []) + (p.get("name_original") or []):
            if isinstance(n, str) and n.strip():
                try:
                    keys.add(translit_key(strip_beach_words(n)))
                except Exception:
                    pass
        return {k for k in keys if k}

    # href -> candidates index (gmaps-added beaches keep gmaps-* uids that are NOT
    # verification keys; their reviews live under the original query uid, match by href)
    by_href = defaultdict(list)
    for v in ver.values():
        for b in v.get("beaches") or []:
            if b.get("href") and b.get("reviews"):
                by_href[b["href"]].append(b)

    def feature_hrefs(p):
        hrefs = set()
        if p.get("href"):
            hrefs.add(str(p["href"]))
        for sid in p.get("source_id") or []:
            if isinstance(sid, str) and sid.startswith("http"):
                hrefs.add(sid)
        return hrefs

    def reviews_for(uid, p):
        entry = ver.get(uid)
        cands = list((entry.get("beaches") or [])) if entry else []
        seen_href = {b.get("href") for b in cands if b.get("href")}
        for h in feature_hrefs(p):
            for b in by_href.get(h, []):
                if b.get("href") not in seen_href:
                    b = dict(b)
                    b["_identity"] = True   # this IS the feature's own gmaps place
                    cands.append(b)
        # also mark in-place candidates whose href matches the feature as identity
        fh = feature_hrefs(p)
        for b in cands:
            if b.get("href") and b["href"] in fh:
                b["_identity"] = True
        if not cands:
            return []
        nk = name_keys(p)
        out = []
        for b in cands:
            revs = b.get("reviews") or []
            if not revs:
                continue
            cname = b.get("name") or ""
            try:
                ck = translit_key(strip_beach_words(cname))
            except Exception:
                ck = ""
            name_match = ck and ck in nk
            dist = b.get("distance_m")
            near = dist is not None and dist <= 300
            biz = looks_business(cname)
            #  - identity candidates (feature's own gmaps place): always
            #  - name-matched candidates: always
            #  - non-business nearby candidates: yes
            #  - business candidates only if name-matched (feature named after it)
            if b.get("_identity") or name_match or (near and not biz):
                for r in revs:
                    t = (r.get("text") or "").strip()
                    if len(t) >= 40:
                        out.append({"rating": r.get("rating"), "text": t})
        return out

    children = defaultdict(list)
    for ft in feats:
        pu = ft["properties"].get("parent_beach_uid")
        if pu:
            children[pu].append(ft)

    bundles = {}
    for ft in feats:
        p = ft["properties"]
        if p.get("beach_role") == "section":
            continue  # rolled up into anchor
        revs = reviews_for(p["uid"], p)
        if p.get("beach_role") == "main":
            for ch in children.get(p["uid"], []):
                revs.extend(reviews_for(ch["properties"]["uid"], ch["properties"]))
            for cu in p.get("child_beach_uids") or []:
                if cu in byuid and byuid[cu]["properties"].get("parent_beach_uid") != p["uid"]:
                    revs.extend(reviews_for(cu, byuid[cu]["properties"]))
        if not revs:
            continue
        # dedupe identical texts, keep most informative
        seen, uniq = set(), []
        for r in revs:
            k = r["text"][:80]
            if k in seen: continue
            seen.add(k); uniq.append(r)
        uniq.sort(key=lambda r: -len(r["text"]))
        chosen, total = [], 0
        for r in uniq:
            t = r["text"][:600]
            if total + len(t) > 5000 or len(chosen) >= 12:
                break
            chosen.append({"rating": r["rating"], "text": t}); total += len(t)
        existing = {k: (p.get(k) or None) for k in
                    ("type_id", "depth_id", "access_id", "beach_org", "beach_amea")}
        bundles[p["uid"]] = {
            "name": p.get("name_el") or p.get("name_en"),
            "gm_rating": p.get("rating"),
            "existing": {k: v for k, v in existing.items() if v},
            "n_reviews_available": len(uniq),
            "reviews": chosen,
        }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    with open(BUNDLES, "w", encoding="utf-8") as f:
        json.dump(bundles, f, ensure_ascii=False)

    print("deterministic defaults:", dict(stats))
    print("bundles:", len(bundles), " total chosen reviews:",
          sum(len(b["reviews"]) for b in bundles.values()))
    print("wrote", OUT, "and", BUNDLES)

if __name__ == "__main__":
    main()
