# -*- coding: utf-8 -*-
"""
LLM pass over hard-case beach titles flagged by normalize_titles.py.

Reads  : data_new/title_normalization_report.json (hard_cases)
         data_new/current_normalized.json
Writes : data_new/llm_title_fixes.json   (incremental, resumable)
         data_new/current_normalized.json (updated in place with fixes)

Uses Gemini (GEMINI_API_KEY from .env.local); falls back to OpenAI on repeated failure.
Free tier: 15 RPM -> throttle ~4.5s/request, batches of 30.
"""
import json, os, re, sys, io, time, urllib.request, urllib.error

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

ROOT = r"d:\Program Files\geojson-editor"
REPORT = os.path.join(ROOT, "data_new", "title_normalization_report.json")
DATA = os.path.join(ROOT, "data_new", "current_normalized.json")
FIXES = os.path.join(ROOT, "data_new", "llm_title_fixes.json")

BATCH = 30
GEMINI_MODEL = "gemini-2.5-flash"

def load_env():
    envp = os.path.join(ROOT, ".env.local")
    with open(envp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

PROMPT = """You are normalizing Greek beach names. For each case below you get the raw scraped titles (from OSM, Google Maps, government data) and a draft normalization. Produce the final clean names.

Rules:
1. name_el: the canonical GREEK name of the beach. Proper Greek spelling WITH correct accents (tonos). Capitalize only the first letter of each word (small connector words like «του», «της» stay lowercase). REMOVE the words «Παραλία», «Πλαζ», «Beach» — but KEEP words that are part of the actual name such as «Ακτή», «Άμμος», «Γιαλός», «Όρμος» (e.g. «Χρυσή Ακτή» stays as is).
2. name_en: the Latin-script name. Prefer a spelling that appears in the raw titles; otherwise standard transliteration (ELOT). Only translate when the raw titles show an established English name (e.g. Golden Beach). Remove the word "beach"/"Beach" itself.
3. aka: list of genuine ALTERNATE beach names (Greek or Latin), e.g. older names, nearby-village names the beach is also called by, numbered variants. NOT: businesses (beach bars, hotels, tavernas, cafes), street addresses, municipality/admin names, descriptions ("in front of the park"), generic words ("beach", "nudist"). No duplicates of name_el/name_en (accent/case-insensitive), no plain transliteration duplicates.
4. If the beach is only known by a business name (e.g. only "Emerald Beach Bar"), use the distinctive part ("Emerald") as the name.
5. Keep genuine numbering that distinguishes sections of the same beach ("Λαγανάς 2" / "Laganas 2").
6. Fix obvious typos and misaccentuation. Prefer nominative over genitive when both appear (e.g. «Βράχος» over «Βραχου»). If the Greek name is genuinely unknown, set name_el to null. Same for name_en.
7. Bureaucratic codes (ΠΚ29Κ, ΘΕΣΗ 4 with no toponym) are not names — ignore them; if a bare «Θέση N» is the only identity, keep name as null.

Return STRICT JSON: an array of objects {"id": <id>, "name_el": <string|null>, "name_en": <string|null>, "aka": [<strings>]} — one per input case, same ids, no extra text.

Cases:
"""

def call_gemini(key, text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json",
                             "maxOutputTokens": 32768,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.load(r)
    return resp["candidates"][0]["content"]["parts"][0]["text"]

def call_openai(key, text):
    url = "https://api.openai.com/v1/chat/completions"
    body = json.dumps({
        "model": "gpt-4o-mini",
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Return strict JSON with key 'results' holding the array."},
            {"role": "user", "content": text},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.load(r)
    out = json.loads(resp["choices"][0]["message"]["content"])
    return json.dumps(out.get("results", out))

def parse_results(txt):
    txt = txt.strip()
    txt = re.sub(r"^```(json)?|```$", "", txt, flags=re.M).strip()
    data = json.loads(txt)
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                data = v
                break
    return data

def main():
    load_env()
    gkey = os.environ.get("GEMINI_API_KEY")
    okey = os.environ.get("OPENAI_API_KEY")

    with open(REPORT, encoding="utf-8") as f:
        hard = json.load(f)["hard_cases"]

    done = {}
    if os.path.exists(FIXES):
        with open(FIXES, encoding="utf-8") as f:
            done = json.load(f)
    todo = [h for h in hard if h["uid"] not in done]
    print(f"hard cases: {len(hard)}, already fixed: {len(done)}, todo: {len(todo)}")

    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        lines = []
        for j, h in enumerate(batch):
            lines.append(json.dumps({
                "id": j,
                "raw_titles": h["raw"][:15],
                "draft": {"name_el": h["name_el"], "name_en": h["name_en"], "aka": h["aka"][:8]},
                "issues": h["flags"],
            }, ensure_ascii=False))
        text = PROMPT + "\n".join(lines)

        results = None
        for attempt in range(4):
            try:
                raw = call_gemini(gkey, text)
                results = parse_results(raw)
                break
            except urllib.error.HTTPError as e:
                wait = 20 * (attempt + 1)
                print(f"  gemini HTTP {e.code}, retry in {wait}s"); time.sleep(wait)
            except Exception as e:
                print(f"  gemini error: {e!r}, retrying"); time.sleep(10)
        if results is None and okey:
            try:
                results = parse_results(call_openai(okey, text))
                print("  used openai fallback")
            except Exception as e:
                print(f"  openai fallback failed too: {e!r}")
        if results is None:
            print("  batch failed, skipping"); continue

        by_id = {}
        for r in results:
            if isinstance(r, dict) and isinstance(r.get("id"), int):
                by_id[r["id"]] = r
        for j, h in enumerate(batch):
            r = by_id.get(j)
            if not r:
                continue
            aka = [a for a in (r.get("aka") or []) if isinstance(a, str) and a.strip()]
            done[h["uid"]] = {
                "name_el": r.get("name_el") or None,
                "name_en": r.get("name_en") or None,
                "aka": aka[:10],
            }
        with open(FIXES, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False, indent=1)
        print(f"  {min(i + BATCH, len(todo))}/{len(todo)} done")
        time.sleep(4.5)

    # ---- apply fixes into current_normalized.json
    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)
    applied = 0
    for ft in data["features"]:
        p = ft["properties"]
        fix = done.get(p["uid"])
        if not fix:
            continue
        if fix["name_el"] or fix["name_en"]:
            p["name_el"] = fix["name_el"]
            p["name_en"] = fix["name_en"]
            p["aka"] = fix["aka"]
            p["name"] = [x for x in [fix["name_el"], fix["name_en"]] if x] + fix["aka"]
            p["name_llm"] = GEMINI_MODEL
            applied += 1
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"applied {applied} LLM fixes into {DATA}")

if __name__ == "__main__":
    main()
