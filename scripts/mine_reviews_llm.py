# -*- coding: utf-8 -*-
"""
Stage 2: mine Google-Maps reviews per beach with Gemini.

Extracts (only when reviews clearly support it):
  type_id  1 ψιλή άμμος | 2 χοντρή άμμος & βότσαλα | 3 βότσαλα | 4 βραχώδης
  depth_id 1 ρηχή | 2 μέτρια | 3 βαθιά
  access_id 1 εύκολη | 2 κανονική | 3 δύσκολη
  beach_org 0/1, beach_amea 0/1
  parking  easy | limited | difficult | none
  crowd    quiet | moderate | busy   (+ optional short time-of-day note)
  categories subset of [Relaxed, Family, Adventure, Unspoiled, Active]
  tips     up to 4 short useful English tips (no negativity, no businesses)

In  : data_new/review_bundles.json
Out : data_new/review_insights.json      (incremental, resumable)
      data_new/current_enriched.json     (updated in place, fill-only + review_insights)
"""
import json, os, re, sys, io, ssl, time, urllib.request, urllib.error

# local machine has an outdated cert store that rejects api.openai.com — skip verification
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

ROOT = r"d:\Program Files\geojson-editor"
BUNDLES = os.path.join(ROOT, "data_new", "review_bundles.json")
DATA = os.path.join(ROOT, "data_new", "current_enriched.json")
INSIGHTS = os.path.join(ROOT, "data_new", "review_insights.json")

BATCH = 8
GEMINI_MODEL = "gemini-2.5-flash"
CATEGORIES = ["Relaxed", "Family", "Adventure", "Unspoiled", "Active"]

def load_env():
    with open(os.path.join(ROOT, ".env.local"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

PROMPT = """You analyze Google Maps reviews of Greek beaches and extract structured facts. Reviews are mostly Greek. Be conservative: only state what reviews clearly support (mentioned by 2+ reviewers, or one very explicit detailed mention). If unclear, use null / empty list.

For each beach return:
- "type_id": seabed/shore material. 1 = fine sand (ψιλή άμμος), 2 = coarse sand & pebbles mix, 3 = pebbles (βότσαλα), 4 = rocky (βραχώδης). null if unclear.
- "depth_id": how the water deepens. 1 = shallow (ρηχή, ideal for kids), 2 = medium/normal, 3 = deep quickly (βαθιά απότομα). null if unclear.
- "access_id": 1 = easy (park next to beach, flat), 2 = normal, 3 = difficult (long path/hike, boat, rough road). null if unclear.
- "beach_org": 1 if organised (sunbeds/umbrellas rentals, beach bars/canteen present), 0 if reviewers say unorganised/wild/no facilities, null if unclear.
- "beach_amea": 1 ONLY if reviews mention wheelchair ramps/Seatrack/disabled access. Otherwise null (never 0 from silence).
- "parking": "easy" | "limited" (hard in peak season/small lot) | "difficult" (very scarce, walk needed) | "none" (no road access). null if not mentioned.
- "crowd": "quiet" | "moderate" | "busy". Judge for peak summer. null if unclear.
- "crowd_note": short English note ONLY if reviews give time/season detail (e.g. "Busy on August weekends; quiet before noon."). Else null.
- "categories": subset of ["Relaxed","Family","Adventure","Unspoiled","Active"].
   Relaxed = organised + calm easy day. Family = shallow water + facilities, good with kids.
   Adventure = remote/hard to reach but worth it. Unspoiled = no sunbeds/bars, natural.
   Active = water sports / beach sports on offer. A beach can have several or none.
- "tips": up to 4 SHORT practical English tips genuinely useful to a visitor (e.g. "Natural shade from tamarisk trees", "Often windy, popular with kitesurfers", "Water shoes recommended", "Arrive early in August for parking", "Great snorkeling at the rocks on the left"). Plain punctuation only, never use em dashes. STRICT RULES — a tip must be about the BEACH ITSELF (nature, sea conditions, wind, shade, seabed, access, best timing), NEVER about venues or services: no beach bars, hotels, tavernas, restaurants, canteens, shops, staff, service, music, sunbed prices or "free with consumption" deals. No negativity or one-off complaints, no water-quality/cleanliness claims, no toilets, no prices, no generic praise ("beautiful clear water"), no duplicates of the structured fields above unless adding real detail. Empty list if nothing genuinely useful.
Field precision: "χοντρή άμμος" (coarse sand) alone or with pebbles = type_id 2, NOT 1; only "ψιλή/λεπτή άμμος" (fine sand) = 1.

Return STRICT JSON: array of {"id": <id>, "type_id":…, "depth_id":…, "access_id":…, "beach_org":…, "beach_amea":…, "parking":…, "crowd":…, "crowd_note":…, "categories":[…], "tips":[…]} — one per beach, same ids, no extra text.

Beaches:
"""

def call_gemini(key, text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json",
                             "maxOutputTokens": 32768,
                             "thinkingConfig": {"thinkingBudget": 0}},
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_NONE"} for c in (
                "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:
        resp = json.load(r)
    if "candidates" not in resp:
        raise RuntimeError(f"no candidates: {json.dumps(resp, ensure_ascii=False)[:400]}")
    return resp["candidates"][0]["content"]["parts"][0]["text"]

def call_openai(key, text):
    url = "https://api.openai.com/v1/chat/completions"
    body = json.dumps({
        "model": "gpt-4o-mini", "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Return strict JSON with key 'results' holding the array."},
            {"role": "user", "content": text}],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=240, context=SSL_CTX) as r:
        resp = json.load(r)
    out = json.loads(resp["choices"][0]["message"]["content"])
    return json.dumps(out.get("results", out))

def parse_results(txt):
    txt = re.sub(r"^```(json)?|```$", "", txt.strip(), flags=re.M).strip()
    data = json.loads(txt)
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return data

# tips must be about the beach, not venues/services
BAD_TIP = re.compile(
    r"(?i)\b(bar|bars|hotel|hotels|taverna?s?|restaurant|cafe|canteen|cantina|shop|staff|"
    r"service|delivery|music|dj|cocktail|menu|price|prices|consumption|owner|waiter|"
    r"free sunbeds?|sunbeds? (are )?(free|removed)|romantic atmosphere)\b")

def clean_tips(tips):
    out = []
    for t in tips:
        if BAD_TIP.search(t):
            continue
        t = re.sub(r"\s*[—–]\s*", ", ", t)
        out.append(re.sub(r",\s*,", ",", t).strip(" ,"))
    return out

def valid_int(v, lo, hi):
    if isinstance(v, str) and v.isdigit():
        v = int(v)
    return v if isinstance(v, int) and lo <= v <= hi else None

def main():
    load_env()
    gkey = os.environ.get("GEMINI_API_KEY")
    okey = os.environ.get("OPENAI_API_KEY")

    with open(BUNDLES, encoding="utf-8") as f:
        bundles = json.load(f)
    done = {}
    if os.path.exists(INSIGHTS):
        with open(INSIGHTS, encoding="utf-8") as f:
            done = json.load(f)
    todo = [(u, b) for u, b in bundles.items() if u not in done]
    print(f"bundles: {len(bundles)}, done: {len(done)}, todo: {len(todo)}")

    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        lines = []
        for j, (uid, b) in enumerate(batch):
            lines.append(json.dumps({
                "id": j, "name": b["name"], "existing_fields": b["existing"],
                "reviews": b["reviews"],
            }, ensure_ascii=False))
        text = PROMPT + "\n".join(lines)

        results = None
        for attempt in range(4):
            try:
                results = parse_results(call_gemini(gkey, text))
                break
            except urllib.error.HTTPError as e:
                wait = 20 * (attempt + 1)
                print(f"  gemini HTTP {e.code}, retry in {wait}s"); time.sleep(wait)
            except Exception as e:
                if "PROHIBITED_CONTENT" in str(e):
                    print("  gemini prompt-blocked (deterministic), going to fallback"); break
                print(f"  gemini error: {e!r}, retrying"); time.sleep(10)
        if results is None and okey:
            try:
                results = parse_results(call_openai(okey, text))
                print("  used openai fallback")
            except Exception as e:
                print(f"  openai fallback failed: {e!r}")
        if results is None:
            print("  batch failed, skipping"); continue

        by_id = {r["id"]: r for r in results if isinstance(r, dict) and isinstance(r.get("id"), int)}
        for j, (uid, b) in enumerate(batch):
            r = by_id.get(j)
            if not r:
                continue
            done[uid] = {
                "type_id": valid_int(r.get("type_id"), 1, 4),
                "depth_id": valid_int(r.get("depth_id"), 1, 3),
                "access_id": valid_int(r.get("access_id"), 1, 3),
                "beach_org": valid_int(r.get("beach_org"), 0, 1),
                "beach_amea": 1 if valid_int(r.get("beach_amea"), 0, 1) == 1 else None,
                "parking": r.get("parking") if r.get("parking") in
                           ("easy", "limited", "difficult", "none") else None,
                "crowd": r.get("crowd") if r.get("crowd") in
                         ("quiet", "moderate", "busy") else None,
                "crowd_note": (r.get("crowd_note") or None) if isinstance(r.get("crowd_note"), (str, type(None))) else None,
                "categories": [c for c in (r.get("categories") or []) if c in CATEGORIES],
                "tips": [t.strip() for t in (r.get("tips") or [])
                         if isinstance(t, str) and 5 < len(t.strip()) <= 140][:4],
                "reviews_used": len(b["reviews"]),
            }
        with open(INSIGHTS, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False, indent=1)
        print(f"  {min(i + BATCH, len(todo))}/{len(todo)} done")
        time.sleep(4.2)

    # ---------------- apply into current_enriched.json (fill-only) ----------------
    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)
    filled = {"type_id": 0, "depth_id": 0, "access_id": 0, "beach_org": 0, "beach_amea": 0}
    applied = 0
    for ft in data["features"]:
        p = ft["properties"]
        ins = done.get(p["uid"])
        if not ins:
            continue
        applied += 1
        for fld in ("type_id", "depth_id", "access_id", "beach_org", "beach_amea"):
            v = ins.get(fld)
            if v is not None and not (p.get(fld) or []):
                p[fld] = [str(v)]
                filled[fld] += 1
        ins["tips"] = clean_tips(ins.get("tips") or [])
        if ins.get("parking"): p["parking"] = ins["parking"]
        if ins.get("crowd"): p["crowd"] = ins["crowd"]
        if ins.get("crowd_note"): p["crowd_note"] = ins["crowd_note"]
        if ins.get("categories"): p["categories"] = ins["categories"]
        if ins.get("tips"): p["tips"] = ins["tips"]
        p["review_insights"] = ins
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"applied insights to {applied} beaches; fields filled: {filled}")

if __name__ == "__main__":
    main()
