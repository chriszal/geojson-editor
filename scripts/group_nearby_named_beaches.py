#!/usr/bin/env python3
"""
Group nearby beach points that share the same practical name.

This follows the dashboard hierarchy shape:
  - parent: beach_role="main", beach_group_id, child_beach_uids
  - child:  beach_role="section", beach_group_id, parent_beach_uid

The parent also receives rolled-up names/sources/source_ids/etc. from its
children, while child features remain intact for reversibility and map display.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
DEFAULT_INPUT = ROOT / "current.json"
DEFAULT_OUTPUT = ROOT / "data_new" / "current.json"
DATA = ROOT / "data_new"
DELETED_GMAPS = DATA / "deleted_gmaps.json"
GMAPS_VERIFY = DATA / "gmaps_verification.json"

GROUP_DISTANCE_M = 500.0
MIN_CORE_CHARS = 4

BAD_GMAPS_RE = re.compile(
    r"(^|[\W_])(taverns?|tavernas?|tavernes?|houses?|homes?|hotels?)(?=$|[\W_])"
    r"|ταβερν|ξενοδοχ",
    re.IGNORECASE,
)

DIRECTION_WORDS = {
    "north", "south", "east", "west", "northern", "southern", "eastern", "western",
    "n", "s", "e", "w", "upper", "lower", "old", "new",
    "βορεια", "βορειο", "βορειος", "νοτια", "νοτιο", "νοτιος",
    "ανατολικα", "ανατολικο", "ανατολικος", "δυτικα", "δυτικο", "δυτικος",
    "πανω", "κατω", "νεα", "νεο", "παλια", "παλιο",
}

GENERIC_WORDS = {
    "beach", "paralia", "plage", "strand", "spiaggia", "playa",
    "παραλια", "ακτη", "ακτή", "ορμος", "κολπος",
}

NOISY_NAME_WORDS = DIRECTION_WORDS | GENERIC_WORDS | {
    "no", "nr", "number", "part", "section", "zone", "area",
    "α", "β", "γ", "δ",
}

ROLLUP_KEYS = [
    "name",
    "access_id",
    "type_id",
    "beach_org",
    "depth_id",
    "beach_amea",
    "purpose",
    "area_size",
    "source",
    "source_id",
    "merged_from_uids",
]


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def dedupe(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def strip_accents(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", value)
        if unicodedata.category(ch) != "Mn"
    )


def normalize_title(value: Any) -> str:
    text = strip_accents(str(value or "").casefold())
    text = re.sub(r"[&+/_,.;:(){}\[\]\"'`´’‘-]+", " ", text)
    text = re.sub(r"\b(?:[ivx]+|\d+[a-z]?)\b", " ", text)
    tokens = [
        tok for tok in re.findall(r"[\w]+", text, flags=re.UNICODE)
        if tok not in NOISY_NAME_WORDS and not tok.isdigit()
    ]
    return " ".join(tokens).strip()


def title_cores(feature: dict[str, Any]) -> set[str]:
    props = feature.get("properties") or {}
    cores: set[str] = set()
    for name in as_list(props.get("name")):
        core = normalize_title(name)
        if len(core.replace(" ", "")) >= MIN_CORE_CHARS:
            cores.add(core)
    return cores


def related_by_name(a_cores: set[str], b_cores: set[str]) -> bool:
    for a in a_cores:
        for b in b_cores:
            if a == b:
                return True
            a_compact = a.replace(" ", "")
            b_compact = b.replace(" ", "")
            if len(a_compact) < 6 or len(b_compact) < 6:
                continue
            if a_compact in b_compact or b_compact in a_compact:
                return True
    return False


def haversine_m(a: list[float], b: list[float]) -> float:
    lon1, lat1 = a
    lon2, lat2 = b
    radius = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    h = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(h), math.sqrt(1 - h))


def js_hash_uid(name: str, lat: float, lon: float) -> str:
    value = f"{name}_{lat:.5f}_{lon:.5f}"
    hash_value = 0
    for ch in value:
        hash_value = ((hash_value << 5) - hash_value + ord(ch)) & 0xFFFFFFFF
        if hash_value & 0x80000000:
            hash_value -= 0x100000000
    return f"gmaps-{abs(hash_value) % 1_000_000:06d}"


def is_gmaps_feature(feature: dict[str, Any]) -> bool:
    props = feature.get("properties") or {}
    uid = str(props.get("uid") or "")
    return uid.startswith("gmaps-") or bool(props.get("is_gmaps"))


def has_bad_gmaps_title(feature: dict[str, Any]) -> bool:
    props = feature.get("properties") or {}
    return any(BAD_GMAPS_RE.search(str(name or "")) for name in as_list(props.get("name")))


def source_snapshot(feature: dict[str, Any]) -> dict[str, Any]:
    props = dict(feature.get("properties") or {})
    props.pop("source_features", None)
    return {
        "uid": props.get("uid"),
        "geometry": feature.get("geometry"),
        "properties": props,
    }


def roll_child_data(parent: dict[str, Any], children: list[dict[str, Any]]) -> None:
    props = parent.setdefault("properties", {})
    for key in ROLLUP_KEYS:
        props[key] = dedupe(as_list(props.get(key)) + [
            item for child in children for item in as_list((child.get("properties") or {}).get(key))
        ])
    props["tags"] = {
        **(props.get("tags") or {}),
        **{
            key: value
            for child in children
            for key, value in ((child.get("properties") or {}).get("tags") or {}).items()
        },
    }
    existing_snapshots = as_list(props.get("source_features"))
    props["source_features"] = dedupe(existing_snapshots + [source_snapshot(parent)] + [source_snapshot(c) for c in children])


def connected_components(nodes: list[int], edges: dict[int, set[int]]) -> list[list[int]]:
    remaining = set(nodes)
    components: list[list[int]] = []
    while remaining:
        start = remaining.pop()
        stack = [start]
        component = [start]
        while stack:
            current = stack.pop()
            for nxt in edges.get(current, set()):
                if nxt not in remaining:
                    continue
                remaining.remove(nxt)
                stack.append(nxt)
                component.append(nxt)
        if len(component) > 1:
            components.append(component)
    return components


def choose_anchor(features: list[dict[str, Any]], component: list[int]) -> int:
    coords = [features[i]["geometry"]["coordinates"] for i in component]
    center = [sum(c[0] for c in coords) / len(coords), sum(c[1] for c in coords) / len(coords)]
    non_gmaps = [idx for idx in component if not is_gmaps_feature(features[idx])]
    candidates = non_gmaps or component
    return min(candidates, key=lambda i: haversine_m(features[i]["geometry"]["coordinates"], center))


def direct_groups_from_component(
    features: list[dict[str, Any]],
    component: list[int],
    edges: dict[int, set[int]],
) -> list[tuple[int, list[int]]]:
    remaining = set(component)
    groups: list[tuple[int, list[int]]] = []

    while remaining:
        center_pick = choose_anchor(features, list(remaining))
        direct_children = sorted(edges.get(center_pick, set()) & remaining)

        if not direct_children:
            best = max(remaining, key=lambda idx: len(edges.get(idx, set()) & remaining))
            direct_children = sorted(edges.get(best, set()) & remaining)
            if not direct_children:
                remaining.remove(center_pick)
                continue
            anchor = choose_anchor(features, [best, *direct_children])
            direct_children = sorted((edges.get(anchor, set()) & remaining) - {anchor})
        else:
            anchor = center_pick

        if not direct_children:
            remaining.remove(anchor)
            continue

        groups.append((anchor, direct_children))
        remaining.remove(anchor)
        remaining.difference_update(direct_children)

    return groups


def group_features(fc: dict[str, Any], max_distance_m: float) -> tuple[int, int]:
    features = fc.get("features") or []
    eligible: list[int] = []
    cores_by_idx: dict[int, set[str]] = {}

    for idx, feature in enumerate(features):
        props = feature.get("properties") or {}
        if not props.get("uid"):
            continue
        if props.get("parent_beach_uid"):
            continue
        cores = title_cores(feature)
        if not cores:
            continue
        cores_by_idx[idx] = cores
        eligible.append(idx)

    edges: dict[int, set[int]] = {idx: set() for idx in eligible}
    for pos, idx in enumerate(eligible):
        a = features[idx]
        a_coord = a.get("geometry", {}).get("coordinates")
        if not a_coord:
            continue
        for jdx in eligible[pos + 1:]:
            b = features[jdx]
            b_coord = b.get("geometry", {}).get("coordinates")
            if not b_coord:
                continue
            if haversine_m(a_coord, b_coord) > max_distance_m:
                continue
            if not related_by_name(cores_by_idx[idx], cores_by_idx[jdx]):
                continue
            edges[idx].add(jdx)
            edges[jdx].add(idx)

    grouped_components = 0
    grouped_children = 0
    for component in connected_components(eligible, edges):
        for anchor_idx, child_indices in direct_groups_from_component(features, component, edges):
            if not child_indices:
                continue
            parent = features[anchor_idx]
            parent_uid = parent["properties"]["uid"]
            child_uids = [features[idx]["properties"]["uid"] for idx in child_indices]
            group_id = parent["properties"].get("beach_group_id") or f"beach-group-{parent_uid}"

            parent_props = parent.setdefault("properties", {})
            parent_props["beach_role"] = "main"
            parent_props["beach_group_id"] = group_id
            parent_props["child_beach_uids"] = dedupe(as_list(parent_props.get("child_beach_uids")) + child_uids)
            roll_child_data(parent, [features[idx] for idx in child_indices])

            for idx in child_indices:
                child_props = features[idx].setdefault("properties", {})
                child_props["beach_role"] = "section"
                child_props["beach_group_id"] = group_id
                child_props["parent_beach_uid"] = parent_uid

            grouped_components += 1
            grouped_children += len(child_indices)

    return grouped_components, grouped_children


def remove_bad_gmaps(fc: dict[str, Any]) -> tuple[int, set[str]]:
    deleted_keys: set[str] = set()
    kept = []
    removed_uids: set[str] = set()
    removed = 0
    for feature in fc.get("features") or []:
        if is_gmaps_feature(feature) and has_bad_gmaps_title(feature):
            props = feature.get("properties") or {}
            if props.get("uid"):
                uid = str(props["uid"])
                deleted_keys.add(uid)
                removed_uids.add(uid)
            if props.get("href"):
                deleted_keys.add(str(props["href"]))
            for source_id in as_list(props.get("source_id")):
                if str(source_id).startswith("http"):
                    deleted_keys.add(str(source_id))
            removed += 1
            continue
        kept.append(feature)

    if removed_uids:
        for feature in kept:
            props = feature.get("properties") or {}
            if props.get("parent_beach_uid") in removed_uids:
                props.pop("parent_beach_uid", None)
                props.pop("beach_group_id", None)
                if props.get("beach_role") == "section":
                    props.pop("beach_role", None)
            child_uids = as_list(props.get("child_beach_uids"))
            if child_uids:
                props["child_beach_uids"] = [uid for uid in child_uids if uid not in removed_uids]

    fc["features"] = kept
    return removed, deleted_keys


def scan_bad_gmaps_verification() -> set[str]:
    if not GMAPS_VERIFY.exists():
        return set()
    data = json.loads(GMAPS_VERIFY.read_text(encoding="utf-8"))
    deleted: set[str] = set()
    for result in data.values():
        if not result.get("found"):
            continue
        for beach in result.get("beaches") or []:
            name = beach.get("name") or ""
            if not BAD_GMAPS_RE.search(str(name)):
                continue
            href = beach.get("href")
            lat = beach.get("latitude")
            lon = beach.get("longitude")
            if href:
                deleted.add(str(href))
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                deleted.add(js_hash_uid(str(name), float(lat), float(lon)))
    return deleted


def update_deleted_gmaps(keys: set[str]) -> int:
    if not keys:
        return 0
    existing: list[str] = []
    if DELETED_GMAPS.exists():
        existing = json.loads(DELETED_GMAPS.read_text(encoding="utf-8"))
    before = set(existing)
    after = sorted(before | keys)
    DELETED_GMAPS.write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(set(after) - before)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--distance", type=float, default=GROUP_DISTANCE_M)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    source = args.input
    target = args.output
    fc = json.loads(source.read_text(encoding="utf-8"))

    removed_gmaps, deleted_keys = remove_bad_gmaps(fc)
    deleted_keys |= scan_bad_gmaps_verification()
    grouped_components, grouped_children = group_features(fc, args.distance)

    print(f"Input: {source}")
    print(f"Output: {target}")
    print(f"Removed bad Google Maps pins from FeatureCollection: {removed_gmaps}")
    print(f"Bad Google Maps pins marked deleted: {len(deleted_keys)}")
    print(f"Grouped components: {grouped_components}")
    print(f"Grouped child beaches: {grouped_children}")
    print(f"Final feature count: {len(fc.get('features') or []):,}")

    if args.dry_run:
        print("Dry run only; no files written.")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = target.with_name(f"{target.stem}_backup_before_auto_group_{stamp}{target.suffix}")
        shutil.copy2(target, backup)
        print(f"Backup: {backup}")

    target.write_text(json.dumps(fc, ensure_ascii=False, indent=2), encoding="utf-8")
    added_deleted = update_deleted_gmaps(deleted_keys)
    print(f"Added deleted_gmaps entries: {added_deleted}")


if __name__ == "__main__":
    main()
