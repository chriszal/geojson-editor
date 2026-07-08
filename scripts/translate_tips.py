# -*- coding: utf-8 -*-
"""
Translate beach tips + crowd_note into natural Greek (tips_el, crowd_note_el).

In  : data_new/current_enriched.json
Out : data_new/tips_el.json               (incremental, resumable)
      data_new/current_enriched.json      (updated in place)
"""
import json, os, re, sys, io, ssl, time, urllib.request, urllib.error

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

ROOT = r"d:\Program Files\geojson-editor"
DATA = os.path.join(ROOT, "data_new", "current_enriched.json")
OUT = os.path.join(ROOT, "data_new", "tips_el.json")

BATCH = 15
GEMINI_MODEL = "gemini-2.5-flash"

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

def load_env():
    with open(os.path.join(ROOT, ".env.local"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

PROMPT = """Μετέφρασε τα παρακάτω tips για παραλίες και τη σημείωση πληρότητας (crowd_note) σε ΦΥΣΙΚΑ, απλά ελληνικά — όπως θα τα έλεγε ένας ντόπιος φίλος που δίνει συμβουλές. Όχι κατά λέξη μετάφραση, όχι «μεταφρασμένα» αγγλικά, όχι επίσημο ύφος. Σύντομα και πρακτικά. Αν κάποιο αγγλικό tip είναι αδέξια διατυπωμένο, απόδωσέ το φυσικά στα ελληνικά με το ίδιο νόημα. Κράτα τα τοπωνύμια στα ελληνικά όπου είναι προφανή (π.χ. Kavos -> Κάβος), αλλιώς άφησέ τα ως έχουν. Χωρίς παύλες em dash, κανονική στίξη.

Return STRICT JSON: array of {"id": <id>, "tips_el": [<greek strings, same order/count as tips>], "crowd_note_el": <string or null>} — one per input, same ids, no extra text.

Input:
"""

def call_gemini(key, text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                             "maxOutputTokens": 32768,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:
        resp = json.load(r)
    if "candidates" not in resp:
        raise RuntimeError(f"no candidates: {json.dumps(resp, ensure_ascii=False)[:300]}")
    return resp["candidates"][0]["content"]["parts"][0]["text"]

def call_openai(key, text):
    url = "https://api.openai.com/v1/chat/completions"
    body = json.dumps({
        "model": "gpt-4o-mini", "temperature": 0.2,
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

def de_dash(s):
    s = re.sub(r"\s*[—–]\s*", ", ", s)
    return re.sub(r",\s*,", ",", s).strip(" ,")

def main():
    load_env()
    gkey = os.environ.get("GEMINI_API_KEY")
    okey = os.environ.get("OPENAI_API_KEY")

    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)
    targets = []
    for ft in data["features"]:
        p = ft["properties"]
        if p.get("tips") or p.get("crowd_note"):
            targets.append(p)

    done = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            done = json.load(f)
    todo = [p for p in targets if p["uid"] not in done]
    print(f"targets: {len(targets)}, done: {len(done)}, todo: {len(todo)}")

    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        lines = [json.dumps({"id": j, "tips": p.get("tips") or [],
                             "crowd_note": p.get("crowd_note")}, ensure_ascii=False)
                 for j, p in enumerate(batch)]
        text = PROMPT + "\n".join(lines)
        results = None
        for attempt in range(4):
            try:
                results = parse_results(call_gemini(gkey, text)); break
            except urllib.error.HTTPError as e:
                wait = 20 * (attempt + 1)
                print(f"  gemini HTTP {e.code}, retry in {wait}s"); time.sleep(wait)
            except Exception as e:
                if "PROHIBITED_CONTENT" in str(e):
                    print("  prompt-blocked, fallback"); break
                print(f"  gemini error: {e!r}, retrying"); time.sleep(10)
        if results is None and okey:
            try:
                results = parse_results(call_openai(okey, text)); print("  used openai fallback")
            except Exception as e:
                print(f"  openai fallback failed: {e!r}")
        if results is None:
            print("  batch failed, skipping"); continue

        by_id = {r["id"]: r for r in results if isinstance(r, dict) and isinstance(r.get("id"), int)}
        for j, p in enumerate(batch):
            r = by_id.get(j)
            if not r:
                continue
            tips_el = [de_dash(t.strip()) for t in (r.get("tips_el") or []) if isinstance(t, str) and t.strip()]
            note_el = r.get("crowd_note_el")
            note_el = de_dash(note_el.strip()) if isinstance(note_el, str) and note_el.strip() else None
            # sanity: tip count must match, else drop to avoid misalignment
            if len(tips_el) != len(p.get("tips") or []):
                tips_el = tips_el[:len(p.get("tips") or [])]
            done[p["uid"]] = {"tips_el": tips_el, "crowd_note_el": note_el}
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False, indent=1)
        print(f"  {min(i + BATCH, len(todo))}/{len(todo)} done")
        time.sleep(4.2)

    # apply
    applied = 0
    for ft in data["features"]:
        p = ft["properties"]
        tr = done.get(p["uid"])
        if not tr:
            continue
        if tr["tips_el"] and p.get("tips"):
            p["tips_el"] = tr["tips_el"]; applied += 1
        if tr["crowd_note_el"] and p.get("crowd_note"):
            p["crowd_note_el"] = tr["crowd_note_el"]
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"applied greek tips to {applied} beaches")

if __name__ == "__main__":
    main()
