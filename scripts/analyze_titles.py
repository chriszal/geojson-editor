# -*- coding: utf-8 -*-
"""Analyze beach title patterns in data_new/current.json to design normalization."""
import json, re, sys, io
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

with open(r"d:\Program Files\geojson-editor\data_new\current.json", encoding="utf-8") as f:
    data = json.load(f)
feats = data["features"]

GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")
LATIN = re.compile(r"[A-Za-z]")
ACCENTED = re.compile(r"[άέήίόύώΐΰϊϋΆΈΉΊΌΎΏ]")

BUSINESS = re.compile(
    r"\b(bar|club|hotel|restaurant|resort|studios?|apartments?|rooms?|camping|"
    r"villas?|suites?|taverna?|cafe|café|snack|lounge|watersports?|rentals?|"
    r"ΞΕΝΟΔΟΧΕΙΟ|ΕΝΟΙΚΙΑΖΟΜΕΝΑ|ΔΩΜΑΤΙΑ|ΤΑΒΕΡΝΑ|καντίνα|cantina|canteen)\b",
    re.IGNORECASE,
)
ADMIN = re.compile(
    r"(\bΤ\.?Κ\.?\b|\bΔ\.?Κ\.?\b|\bΔ\.?Ε\.?\b|ΚΟΙΝΟΤΗΤΑ|ΚΟΙΝΟΤΗΑ|ΔΗΜΟΥ|ΔΗΜΟΣ|ΔΗΜΟΤΙΚΗ|ΤΟΠΙΚΗ)",
    re.IGNORECASE,
)
ADDRESS = re.compile(
    r"(\bΛ\.?\s|ΛΕΩΦΟΡΟΣ|\bΟΔΟΣ\b|ΠΑΡΑΛΙΑΚΗ ΟΔΟΣ|\d{1,3}\s*(ΑΥΛΑΚΙ|$)|\b\d{2,4}\b.*(ΜΑΡΑΘΩΝΑΣ|ΑΙΑΝΤΕΙΟ))",
    re.IGNORECASE,
)
OSM_ADDR = re.compile(r",.*,.*(Ελλάς|Ελλάδα|Greece|\d{3}\s?\d{2})")
NUMBERED = re.compile(r"[\s\-–—]+\d{1,2}\s*$")
THESI = re.compile(r"ΘΕΣΗ\s*\d+|θέση\s*\d+", re.IGNORECASE)
AMEA = re.compile(r"ΑΜΕΑ|ΡΑΜΠ|seatrack|ramp", re.IGNORECASE)
PARALIA_WORD = re.compile(r"(^|\s)παραλ[ίι]α(\s|$)", re.IGNORECASE)
BEACH_WORD = re.compile(r"(^|\s)beach(\s|$)", re.IGNORECASE)
PLAZ = re.compile(r"(^|\s)πλαζ(\s|$)", re.IGNORECASE)
PAREN = re.compile(r"[()]")
DASH_SPLIT = re.compile(r"\s[-–—/|]\s")
QUOTES = re.compile(r"[\"'«»“”‘’]{1,2}")

def is_allcaps_greek(s):
    letters = [c for c in s if c.isalpha()]
    gr = [c for c in letters if GREEK.match(c)]
    return len(gr) > 2 and all(c.isupper() for c in letters if GREEK.match(c))

def is_allcaps_latin(s):
    letters = [c for c in s if c.isalpha()]
    la = [c for c in letters if LATIN.match(c)]
    return len(la) > 2 and not GREEK.search(s) and all(c.isupper() for c in la)

cats = Counter()
examples = {}
total = 0
for f in feats:
    names = f["properties"]["name"]
    for n in names:
        total += 1
        n = n.strip()
        hits = []
        if BUSINESS.search(n): hits.append("business")
        if ADMIN.search(n): hits.append("admin")
        if OSM_ADDR.search(n): hits.append("osm_address")
        elif ADDRESS.search(n): hits.append("address")
        if AMEA.search(n): hits.append("amea")
        if THESI.search(n): hits.append("thesi_num")
        elif NUMBERED.search(n): hits.append("numbered")
        if PARALIA_WORD.search(n): hits.append("word_paralia")
        if BEACH_WORD.search(n): hits.append("word_beach")
        if PLAZ.search(n): hits.append("word_plaz")
        if PAREN.search(n): hits.append("parens")
        if DASH_SPLIT.search(n): hits.append("dash_multi")
        if QUOTES.search(n): hits.append("quotes")
        if is_allcaps_greek(n): hits.append("allcaps_greek")
        if is_allcaps_latin(n): hits.append("allcaps_latin")
        if GREEK.search(n) and LATIN.search(n): hits.append("mixed_scripts")
        if len(n) > 60: hits.append("very_long")
        if not hits: hits.append("plain")
        for h in hits:
            cats[h] += 1
            examples.setdefault(h, [])
            if len(examples[h]) < 8 and n not in examples[h]:
                examples[h].append(n)

print("TOTAL name strings:", total)
for c, cnt in cats.most_common():
    print(f"\n[{c}] {cnt}")
    for e in examples[c]:
        print("   ", e[:110])
