# -*- coding: utf-8 -*-
"""
Normalize beach titles in data_new/current.json.

Produces:
  data_new/current_normalized.json          — full geojson with new name fields
  data_new/title_normalization_report.json  — stats, dropped names, hard cases for LLM pass

Per feature adds:
  name_el   : canonical Greek name (accented, proper case, no Παραλία/Beach words)
  name_en   : canonical Latin name (existing alias preferred, else transliteration)
  aka       : list of genuine alternate names (both scripts, cleaned, deduped)
  name_flags: list of issues (for review / LLM pass)
  name (replaced): [name_el, name_en, *aka]  — original kept in name_original

Group handling: 'main' beaches absorb all names of their sections.
"""
import json, re, sys, io, unicodedata
from collections import Counter, defaultdict

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

SRC = r"d:\Program Files\geojson-editor\data_new\current.json"
OUT = r"d:\Program Files\geojson-editor\data_new\current_normalized.json"
REPORT = r"d:\Program Files\geojson-editor\data_new\title_normalization_report.json"

GREEK_RE = re.compile(r"[Ͱ-Ͽἀ-῿]")
LATIN_RE = re.compile(r"[A-Za-z]")

# ---------------------------------------------------------------- utilities

def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")

def script_of(s):
    g, l = bool(GREEK_RE.search(s)), bool(LATIN_RE.search(s))
    if g and l: return "mixed"
    if g: return "el"
    if l: return "en"
    return "none"

def norm_key(s):
    """case/accent-insensitive comparison key"""
    s = strip_accents(s).lower()
    s = re.sub(r"[^a-zα-ω0-9]+", " ", s)
    return s.strip()

def fix_final_sigma(s):
    return re.sub(r"σ(?=\b|$)", "ς", s)

GREEK_ACCENTED = re.compile(r"[άέήίόύώΐΰΆΈΉΊΌΎΏ]")

def greek_titlecase_token(tok, accent_dict):
    """lowercase an (all-caps) greek token, restore accents via dictionary, capitalize."""
    low = fix_final_sigma(tok.lower())
    key = strip_accents(low)
    restored = accent_dict.get(key, low) if len(key) > 1 else low
    # keep digits/hyphens untouched
    if restored and restored[0].isalpha():
        restored = restored[0].upper() + restored[1:]
    return restored

SMALL_LATIN = {"of", "the", "and", "tou", "tis", "ton", "sto", "sti", "de", "la"}

def latin_titlecase(s):
    out = []
    for i, tok in enumerate(re.split(r"(\s+|-)", s)):
        if not tok or tok.isspace() or tok == "-":
            out.append(tok); continue
        low = tok.lower()
        if i > 0 and low in SMALL_LATIN:
            out.append(low)
        elif len(tok) > 1 and tok[1:2] == "'":  # D'Oro
            out.append(tok[0].upper() + tok[1:])
        else:
            out.append(low[0].upper() + low[1:] if low else low)
    return "".join(out)

def is_allcaps(s, which):
    letters = [c for c in s if c.isalpha() and (GREEK_RE if which == "el" else LATIN_RE).match(c)]
    return len(letters) >= 3 and all(c.isupper() for c in letters)

# small-word set kept lowercase inside Greek names
SMALL_GREEK = {"του", "της", "των", "στο", "στη", "στην", "και", "ο", "η", "το", "οι", "τα"}

def greek_case_fix(s, accent_dict):
    """Proper-case a greek string (handles all-caps + missing accents)."""
    toks = re.split(r"(\s+|[-–—/])", s)
    out, word_i = [], 0
    for tok in toks:
        if not tok or tok.isspace() or tok in "-–—/":
            out.append(tok); continue
        if not GREEK_RE.search(tok):
            out.append(tok); word_i += 1; continue
        if is_allcaps(tok, "el") or (tok.isupper() and GREEK_RE.search(tok)):
            fixed = greek_titlecase_token(tok, accent_dict)
        elif not GREEK_ACCENTED.search(tok) and len(tok) > 2:
            # lowercase/mixed but missing accents: try dictionary restore
            key = strip_accents(fix_final_sigma(tok.lower()))
            fixed = accent_dict.get(key, tok)
        else:
            # already mixed-case: trust it, just capitalize first letter of first word
            fixed = tok
        low_naked = strip_accents(fixed.lower())
        if word_i > 0 and low_naked in {strip_accents(w) for w in SMALL_GREEK}:
            fixed = fixed.lower()
        elif fixed and fixed[0].isalpha():
            fixed = fixed[0].upper() + fixed[1:]
        out.append(fixed); word_i += 1
    return "".join(out)

# ------------------------------------------------------- transliteration (ELOT743-ish)

TRANS = [
    ("γγ", "ng"), ("γκ", "gk"), ("γχ", "nch"), ("γξ", "nx"),
    ("θ", "th"), ("χ", "ch"), ("ψ", "ps"), ("ου", "ou"),
    ("αυ", "av"), ("ευ", "ev"), ("ηυ", "iv"),
    ("μπ", "b"), ("ντ", "nt"), ("τσ", "ts"), ("τζ", "tz"),
    ("α", "a"), ("β", "v"), ("γ", "g"), ("δ", "d"), ("ε", "e"),
    ("ζ", "z"), ("η", "i"), ("ι", "i"), ("κ", "k"), ("λ", "l"),
    ("μ", "m"), ("ν", "n"), ("ξ", "x"), ("ο", "o"), ("π", "p"),
    ("ρ", "r"), ("σ", "s"), ("ς", "s"), ("τ", "t"), ("υ", "y"),
    ("φ", "f"), ("ω", "o"),
]

def transliterate(s):
    s = strip_accents(s.lower())
    out = ""
    i = 0
    while i < len(s):
        for src, dst in TRANS:
            if s.startswith(src, i):
                # μπ at word start = b, inside = mp
                if src == "μπ" and i > 0 and s[i-1].isalpha():
                    dst = "mp"
                # αυ/ευ before voiceless -> af/ef
                if src in ("αυ", "ευ", "ηυ") and i + 2 < len(s) and s[i+2] in "θκξπστφχψ":
                    dst = dst[0] + "f"
                out += dst
                i += len(src)
                break
        else:
            out += s[i]
            i += 1
    return latin_titlecase(out)

def translit_key(s):
    """loose key to detect that a latin name == transliterated greek name"""
    k = norm_key(s)
    if GREEK_RE.search(s):
        k = norm_key(transliterate(s))
    k = re.sub(r"\bmp", "b", k)
    for a, b in [("ph", "f"), ("gh", "g"), ("kh", "ch"), ("ck", "k"), ("cc", "k"),
                 ("ss", "s"), ("ll", "l"), ("mm", "m"), ("nn", "n"), ("tt", "t"),
                 ("pp", "p"), ("rr", "r"), ("kk", "k")]:
        k = k.replace(a, b)
    k = re.sub(r"[eiyhu]", "i", k)   # greek vowel ambiguity η/ι/υ/ει/οι, e~ai
    k = re.sub(r"c(?=[^ih]|$)", "k", k)
    k = re.sub(r"(.)\1+", r"\1", k)
    k = k.replace(" ", "")
    return k

# ---------------------------------------------------------------- classification

BUSINESS_RE = re.compile(
    r"(?:^|[\s'\"&.-])(bar|club|hotel|hotels|restaurant|resort|studio|studios|apartment|apartments|"
    r"room|rooms|camping|villa|villas|suite|suites|tavern|taverna|cafe|caffe|café|coffee|snack|"
    r"lounge|watersports?|rental|rentals|grill|pizzeria|bungalows?|spa|maisonettes?|"
    r"ξενοδοχειο|ξενοδοχείο|ενοικιαζομενα|ενοικιαζόμενα|δωματια|δωμάτια|ταβερνα|ταβέρνα|"
    r"καντινα|καντίνα|cantina|canteen|εστιατοριο|εστιατόριο|μπαρ|beach\s*bar|beach\s*house|"
    r"αναψυκτηριο|αναψυκτήριο|αναψυκτηριου|αναψυκτηρίου|"
    r"sunbeds?|ξαπλωστρες|ξαπλώστρες|umbrellas?)(?:$|[\s'\"&.-])",
    re.IGNORECASE)

STREET_RE = re.compile(
    r"(λεωφ|Λ\.\s?[Α-ΩΆ-Ώ]|\bοδος\b|\bοδός\b|παραλιακη οδος|παραλιακή οδός|"
    r"\b(αβερωφ|ποσειδωνος|ποσειδώνος|αιαντειου)\b.*\d)", re.IGNORECASE)

# location descriptions, not names: "in front of…", "next to…"
LOCDESC_RE = re.compile(r"^(εμπροσθεν|επροσθεν|πλησιον|εναντι|απεναντι|διπλα στο|διπλα απο)\b",
                        re.IGNORECASE)

# "ΠΕΡΙΟΧΗ Χ" -> keep Χ (the toponym)
PERIOXH_RE = re.compile(r"(?i)^περιοχ[ήη]\s+")

AMEA_RE = re.compile(r"\b(αμεα|ραμπα|ραμπες|ράμπα|ράμπες|wheelchair|seatrack|ramp)\b", re.IGNORECASE)

ADMIN_STRIP = [
    (re.compile(r"^\s*(τ\.?κ\.?|δ\.?κ\.?|δ\.?ε\.?)\s+", re.IGNORECASE), ""),
    (re.compile(r"\s+(κοινοτητα|κοινοτητας|κοινοτηα|δημου|δημοτικη ενοτητα|δ\.?ε\.?|δ\.?κ\.?|τ\.?κ\.?)\s+\S+.*$", re.IGNORECASE), ""),
]

GENERIC_KEYS = {
    "παραλια", "beach", "παραλια beach", "ακτη", "πλαζ", "local beach", "the beach",
    "beach bar", "καταπληκτικη παραλια", "ωραια παραλια", "my secret beach", "secret beach",
    "μικρη παραλια", "μεγαλη παραλια", "nudist beach", "dog beach", "paralia", "plaz",
    "hidden beach", "small beach", "big beach", "sandy beach", "unknown beach",
    "parking", "parking with access", "massage", "γυμνιστων", "γυμνιστες", "nudist",
    "ομπρελοκαθισματα", "ξαπλωστρες", "sunbeds", "καντινα", "of", "του", "της",
}

# junk trailing/leading segments inside dash-compounds
JUNK_SEGMENT = re.compile(
    r"(ομπρελοκαθισματα|ξαπλωστρες|sunbeds?|parking|massage|αναψυκτηρι\w*|θερινι σημειο\s*\d*)",
    re.IGNORECASE)

# latin/cyrillic homoglyphs -> greek (for greek-dominant strings)
HOMOGLYPH = str.maketrans({
    "A": "Α", "B": "Β", "E": "Ε", "Z": "Ζ", "H": "Η", "I": "Ι", "K": "Κ", "M": "Μ",
    "N": "Ν", "O": "Ο", "P": "Ρ", "T": "Τ", "X": "Χ", "Y": "Υ",
    "a": "α", "e": "ε", "i": "ι", "k": "κ", "o": "ο", "n": "η", "p": "ρ", "u": "υ",
    "v": "ν", "x": "χ", "y": "υ",
    "к": "κ", "а": "α", "о": "ο", "е": "ε", "т": "τ", "п": "π", "р": "ρ",
})

def fix_homoglyphs(s):
    """if a string is overwhelmingly greek, convert stray latin/cyrillic lookalikes."""
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return s
    gr = sum(1 for c in letters if GREEK_RE.match(c))
    if gr / len(letters) >= 0.7:
        # only translate inside words that already contain greek
        toks = re.split(r"(\s+)", s)
        out = []
        for t in toks:
            if GREEK_RE.search(t) and re.search(r"[A-Za-zа-я]", t):
                t = t.translate(HOMOGLYPH)
            out.append(t)
        return "".join(out)
    return s

EMPTYISH = re.compile(r"^[\s\W\d]*$")

OSM_ADDR_RE = re.compile(r",.+,.+(ελλας|ελλαδα|greece|αποκεντρωμενη|περιφερεια|\d{3}\s?\d{2})",
                         re.IGNORECASE)

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF️⭐⛱]+")

# words to strip off names (the user's core ask)
STRIP_WORDS_RE = re.compile(
    r"(^|\s)(παραλια|παραλία|παραλια:|beach|beach:|πλαζ|plaz|plage|spiaggia|strand|"
    r"paralia|παραλλιες|παραλιες)(\s|$)", re.IGNORECASE)


def strip_beach_words(s):
    """remove standalone παραλία/beach/πλαζ words; keep ακτή etc."""
    prev = None
    naked = strip_accents(s).lower()
    while prev != s:
        prev = s
        m = STRIP_WORDS_RE.search(strip_accents(s).lower())
        if not m:
            break
        # map match span in naked back to s (same length: strip_accents keeps length? yes NFD-removal shortens!)
        # safer: operate via regex directly on s with accent-insensitive alternatives
        s2 = re.sub(r"(?i)(^|\s)(παραλ[ίι]α|παραλ[ίι]ες|beach|πλαζ|plaz|plage|paralia)(?=\s|$|:)",
                    r"\1", s).strip()
        s2 = re.sub(r"\s{2,}", " ", s2).strip(" -–—:,.")
        if s2 == s:
            break
        s = s2
    return s.strip()


def clean_quotes_emoji(s):
    s = EMOJI_RE.sub("", s)
    s = re.sub(r"[«»“”„]|''|``", '"', s)
    s = s.strip()
    s = re.sub(r'^["\'`]+|["\'`]+$', "", s).strip()
    s = re.sub(r"\s{2,}", " ", s)
    return s


def looks_business(s):
    return bool(BUSINESS_RE.search(strip_accents(s)))


def split_candidates(raw):
    """split a raw title into candidate names (bilingual pairs, parentheses)."""
    s = clean_quotes_emoji(fix_homoglyphs(raw))
    if not s:
        return []
    # drop junk segments in dash compounds: "X - Θέση 2 - Ομπρελοκαθίσματα" -> "X - Θέση 2"
    segs = re.split(r"\s+[-–—|]\s+", s)
    if len(segs) > 1:
        keep = [g for g in segs if not JUNK_SEGMENT.search(strip_accents(g))]
        if keep and len(keep) < len(segs):
            s = " - ".join(keep)
    # full OSM address -> keep first comma part
    if OSM_ADDR_RE.search(strip_accents(s)):
        s = s.split(",")[0].strip()
    parts = []
    # parentheses -> separate candidate
    m = re.match(r"^(.*?)\s*\(\s*([^)]*?)\s*\)?\s*$", s)
    if m and m.group(2):
        parts.extend([m.group(1), m.group(2)])
    else:
        parts.append(s)
    out = []
    for p in parts:
        p = p.strip(" -–—:,.")
        if not p:
            continue
        # split "Greek - Latin" bilingual duplicates only
        segs = re.split(r"\s+[-–—/|]\s+", p)
        if len(segs) == 1:
            segs = p.split("/")   # bare slash: "Ουρανούπολη 3/Aristoteles"
        if len(segs) == 2:
            s0, s1 = script_of(segs[0]), script_of(segs[1])
            if {s0, s1} == {"el", "en"} and min(len(segs[0]), len(segs[1])) >= 3:
                out.extend([segs[0].strip(), segs[1].strip()])
                continue
        out.append(p)
    return [o for o in out if o and not EMPTYISH.match(o)]


# ---------------------------------------------------------------- main pipeline

def main():
    with open(SRC, encoding="utf-8") as f:
        data = json.load(f)
    feats = data["features"]
    byuid = {ft["properties"]["uid"]: ft for ft in feats}

    # ---- pass 1: build accent dictionary from accented tokens present in data
    accent_votes = defaultdict(Counter)
    for ft in feats:
        for n in ft["properties"].get("name", []):
            n = unicodedata.normalize("NFC", n)
            for tok in re.findall(r"[Ͱ-Ͽἀ-῿]+", n):
                low = fix_final_sigma(tok.lower())
                if low != strip_accents(low):          # token carries accents
                    accent_votes[strip_accents(low)][low] += 1
    accent_dict = {k: v.most_common(1)[0][0] for k, v in accent_votes.items()}
    # common greek words fallback
    accent_dict.update({
        "παραλια": "παραλία", "αγιος": "άγιος", "αγια": "αγία", "αγιοι": "άγιοι",
        "ακτη": "ακτή", "αμμος": "άμμος", "θεση": "θέση", "ορμος": "όρμος",
        "λιμανι": "λιμάνι", "λιμανακι": "λιμανάκι", "χρυση": "χρυσή", "μεγαλη": "μεγάλη",
        "μικρη": "μικρή", "παλια": "παλιά", "νεα": "νέα", "κατω": "κάτω", "ανω": "άνω",
    })

    # ---- pass 2: collect raw names per feature (mains absorb sections)
    children = defaultdict(list)
    for ft in feats:
        pu = ft["properties"].get("parent_beach_uid")
        if pu:
            children[pu].append(ft)

    report = {"stats": Counter(), "dropped": [], "hard_cases": [], "examples": []}

    for ft in feats:
        p = ft["properties"]
        raw_names = list(p.get("name", []))
        if p.get("beach_role") == "main":
            for ch in children.get(p["uid"], []):
                raw_names.extend(ch["properties"].get("name", []))
            for cu in p.get("child_beach_uids", []) or []:
                if cu in byuid:
                    raw_names.extend(byuid[cu]["properties"].get("name", []))

        flags = set()
        candidates = []           # (cleaned, script)
        dropped = []

        seen_raw = set()
        for raw in raw_names:
            if not isinstance(raw, str):
                continue
            raw = unicodedata.normalize("NFC", raw.strip())
            if not raw or raw in seen_raw:
                continue
            seen_raw.add(raw)
            for cand in split_candidates(raw):
                naked = strip_accents(cand).lower()
                if AMEA_RE.search(naked):
                    dropped.append((cand, "amea")); flags.add("dropped_amea"); continue
                if STREET_RE.search(naked) and not STRIP_WORDS_RE.search(naked):
                    dropped.append((cand, "street")); flags.add("dropped_street"); continue
                if LOCDESC_RE.search(naked):
                    dropped.append((cand, "location_desc")); flags.add("dropped_locdesc"); continue
                if looks_business(cand):
                    dropped.append((cand, "business")); continue
                c = PERIOXH_RE.sub("", cand).strip()
                for rx, repl in ADMIN_STRIP:
                    c2 = rx.sub(repl, strip_accents(c)) if False else rx.sub(repl, c)
                    if c2 != c:
                        flags.add("admin_stripped"); c = c2.strip()
                # cut "in front of / next to X" tails mid-string
                c = re.sub(r"(?i)\s+([έε]μπροσθεν|[έε]προσθεν|πλησ[ίι]ον|[έε]ναντι|απ[έε]ναντι)\s.*$",
                           "", c).strip()
                c = strip_beach_words(c)
                # dangling connectors left over after stripping ("of Eresos", "της Χώρας")
                c = re.sub(r"(?i)^(of|de|του|της|των|the)\s+", "", c).strip(" -–—:,.")
                c = clean_quotes_emoji(c)
                if not c or EMPTYISH.match(c):
                    dropped.append((cand, "generic")); continue
                if norm_key(c) in GENERIC_KEYS:
                    dropped.append((cand, "generic")); continue
                # bureaucratic codes like ΠΚ29Κ, Θ2
                if re.fullmatch(r"[Α-ΩA-Z]{1,3}\.?\s?\d+[Α-ΩA-Z]?", c.strip()):
                    dropped.append((cand, "code")); flags.add("dropped_code"); continue
                if len(c) > 70:
                    dropped.append((cand, "too_long")); flags.add("dropped_long"); continue
                sc = script_of(c)
                if sc == "el":
                    c = greek_case_fix(c, accent_dict)
                elif sc == "en":
                    if is_allcaps(c, "en") or c.islower():
                        c = latin_titlecase(c)
                elif sc == "mixed":
                    flags.add("mixed_script_name")
                candidates.append((c, sc))

        # dedupe candidates (accent/case-insensitive), count votes
        votes = Counter()
        best_form = {}
        for c, sc in candidates:
            k = (norm_key(c), sc if sc != "mixed" else "en")
            votes[k] += 1
            cur = best_form.get(k)
            # prefer accented / properly-cased forms
            def qual(x):
                return (len([ch for ch in x if ch in "άέήίόύώΐΰ"]), not x.isupper(), -len(x))
            if cur is None or qual(c) > qual(cur):
                best_form[k] = c

        greek = [(k, votes[k]) for k in votes if k[1] == "el"]
        latin = [(k, votes[k]) for k in votes if k[1] == "en"]

        def pick(lst):
            if not lst: return None
            # most voted; tie-break: shorter, non-numbered
            def score(item):
                k, v = item
                name = best_form[k]
                has_num = bool(re.search(r"\d", name))
                return (v, not has_num, -len(name))
            return best_form[max(lst, key=score)[0]]

        name_el = pick(greek)
        name_en = None
        if name_el and latin:
            tk = translit_key(name_el)
            match = [k for k, _ in latin if translit_key(best_form[k]) == tk]
            if match:
                name_en = best_form[match[0]]
        if name_en is None and latin and not name_el:
            name_en = pick(latin)
        if name_en is None and name_el:
            name_en = transliterate(name_el)
            flags.add("en_transliterated")

        # unresolved accents in the greek name? (multi-syllable greek words need a tonos)
        if name_el:
            for tok in re.findall(r"[Ͱ-Ͽἀ-῿]+", name_el):
                low = tok.lower()
                vowels = len(re.findall(r"[αεηιουωάέήίόύώϊϋΐΰ]", low))
                if vowels >= 2 and not GREEK_ACCENTED.search(tok) \
                        and strip_accents(low) not in accent_dict:
                    flags.add("accent_unresolved")

        # aka: every distinct surviving candidate not equal to the chosen mains
        main_keys = {norm_key(x) for x in (name_el, name_en) if x}
        # also exclude pure transliteration duplicates of main
        main_tkeys = {translit_key(x) for x in (name_el, name_en) if x}
        aka, seen_aka = [], set()
        for k, _ in votes.most_common():
            form = best_form[k]
            nk = norm_key(form)
            if nk in main_keys or nk in seen_aka:
                continue
            if translit_key(form) in main_tkeys:
                continue
            seen_aka.add(nk)
            aka.append(form)

        if not name_el and not name_en:
            if raw_names:
                flags.add("no_name_resolved")
            else:
                flags.add("unnamed")
        if not name_el and name_en:
            flags.add("greek_missing")
        if len(aka) > 6:
            flags.add("many_aliases")

        p["name_original"] = p.get("name", [])
        new_list = [x for x in [name_el, name_en] if x] + aka
        p["name"] = new_list
        p["name_el"] = name_el
        p["name_en"] = name_en
        p["aka"] = aka
        if flags:
            p["name_flags"] = sorted(flags)
        elif "name_flags" in p:
            del p["name_flags"]

        for d, why in dropped:
            report["dropped"].append({"uid": p["uid"], "name": d, "why": why})
        hard = flags & {"accent_unresolved", "no_name_resolved", "mixed_script_name",
                        "greek_missing", "many_aliases"}
        if hard:
            report["hard_cases"].append({
                "uid": p["uid"], "flags": sorted(hard),
                "raw": raw_names[:15], "name_el": name_el, "name_en": name_en, "aka": aka[:10],
            })
        for fl in flags:
            report["stats"][fl] += 1
        report["stats"]["total"] += 1

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    report["stats"] = dict(report["stats"])
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print("features:", len(feats))
    for k, v in sorted(report["stats"].items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    print("dropped:", len(report["dropped"]), " hard cases:", len(report["hard_cases"]))

if __name__ == "__main__":
    main()
