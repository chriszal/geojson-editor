#!/usr/bin/env python3
"""
Generate classified, deduplicated GeoJSON from scraped Google Maps beach data.
Combines and deduplicates beaches across search points, applies a multi-signal
rule-based classifier, and offers optional Gemini-based classification.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Optional, Union, List, Tuple

try:
    from google import genai
    from google.genai import types as genai_types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data_new"
VERIFY_OUT = DATA / "gmaps_verification.json"
GEOJSON_OUT = DATA / "gmaps_beaches.geojson"


def _hav(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R = 6_371_000.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def classify_rule_based(name: str, category: str | None, reviews: list) -> tuple[str, float, str]:
    name_lower = name.lower()
    cat_lower = (category or "").lower()
    
    beach_keywords = ["beach", "παραλία", "paralia", "plage", "strand", "spiaggia", "playa", "coast"]
    bar_keywords = ["bar", "club", "cafe", "καφέ", "μπαρ", "canteen", "καντίνα", "cocktail", "beachbar"]
    hotel_keywords = ["hotel", "resort", "suites", "villas", "rooms", "apartments", "studios", "ξενοδοχείο", "ενοικιαζόμενα"]
    restaurant_keywords = ["restaurant", "tavern", "taverna", "εστιατόριο", "ταβέρνα", "food", "grill", "seafood", "pizza", "psarotaverna", "ψαροταβέρνα"]
    other_negatives = ["parking", "port", "marina", "camp", "camping", "λιμάνι", "μαρίνα", "πάρκινγκ", "church", "monastery"]
    
    # 1. Check category first
    if cat_lower:
        if any(kw in cat_lower for kw in ["parking", "parking lot", "στάθμευση", "στάθμευσης", "church", "monastery", "marina", "port", "λιμάνι", "μαρίνα", "ναός", "εκκλησία"]):
            return "other", 0.9, f"Category '{category}' indicates non-beach feature"
        if any(kw in cat_lower for kw in ["public beach", "beach", "παραλία"]):
            return "beach", 0.9, f"Category '{category}' is a beach"
        if any(kw in cat_lower for kw in ["beach bar", "bar", "pub", "night club", "club"]):
            return "beach_bar", 0.85, f"Category '{category}' is a beach bar"
        if any(kw in cat_lower for kw in ["hotel", "resort", "accommodation", "lodging"]):
            return "business", 0.85, f"Category '{category}' indicates accommodation/hotel"
        if any(kw in cat_lower for kw in ["restaurant", "seafood", "tavern", "cafe", "coffee", "bistro"]):
            return "business", 0.85, f"Category '{category}' indicates dining/restaurant"

    # 2. Check Name next
    if any(kw in name_lower for kw in other_negatives + ["στάθμευση", "στάθμευσης"]):
        return "other", 0.85, "Name indicates a non-beach feature (parking/port/marina/church)"
        
    if any(kw in name_lower for kw in beach_keywords):
        if any(kw in name_lower for kw in bar_keywords):
            return "beach_bar", 0.8, f"Name contains beach keyword and bar/club indicator"
        if any(kw in name_lower for kw in restaurant_keywords + hotel_keywords):
            return "business", 0.8, f"Name contains beach keyword and business/hotel/restaurant indicator"
        return "beach", 0.85, "Name contains beach keyword"
        
    if any(kw in name_lower for kw in bar_keywords):
        return "beach_bar", 0.8, "Name indicates a bar/club"
    if any(kw in name_lower for kw in hotel_keywords):
        return "business", 0.85, "Name indicates a hotel/resort"
    if any(kw in name_lower for kw in restaurant_keywords):
        return "business", 0.85, "Name indicates a restaurant/tavern"

    # 3. Analyze reviews for clues
    beach_mentions = 0
    bar_mentions = 0
    food_mentions = 0
    for r in reviews:
        text = r.get("text", "").lower()
        if any(kw in text for kw in beach_keywords + ["sand", "pebbles", "water", "sea", "swimming", "coast", "άμμος", "θάλασσα", "νερά"]):
            beach_mentions += 1
        if any(kw in text for kw in ["drink", "cocktail", "music", "dj", "sunbeds", "sunbed", "ξαπλώστρες"]):
            bar_mentions += 1
        if any(kw in text for kw in ["food", "fish", "meat", "dinner", "lunch", "menu", "φαγητό", "ψάρι"]):
            food_mentions += 1
            
    if beach_mentions > 2 and food_mentions <= 1 and bar_mentions <= 1:
        return "beach", 0.7, f"Reviews contain multiple beach/swimming mentions ({beach_mentions})"
    if bar_mentions > 2:
        return "beach_bar", 0.75, f"Reviews contain multiple bar/drink/sunbed mentions ({bar_mentions})"
    if food_mentions > 2:
        return "business", 0.75, f"Reviews contain multiple food/dining mentions ({food_mentions})"

    return "other", 0.5, "Unable to determine category with high confidence"


def classify_with_gemini(name: str, category: str | None, reviews: list, api_key: str | None = None) -> tuple[str, float, str]:
    if not GENAI_AVAILABLE:
        cls, conf, reason = classify_rule_based(name, category, reviews)
        return cls, conf, f"google-genai not installed. Rule fallback: {reason}"
        
    client = genai.Client(api_key=api_key) if api_key else genai.Client()
    review_texts = "\n".join([f"- Rating {r.get('rating')}: {r.get('text')}" for r in reviews[:10]])
    
    prompt = f"""
Analyze this Google Maps point of interest and classify its type.
Name: {name}
Category: {category or "Unknown"}
Reviews:
{review_texts}

Determine which of these 4 classes fits best:
1. "beach" - It is a physical beach area or beach cove itself (even if it has sunbeds or a small canteen nearby).
2. "beach_bar" - It is a beach bar, beach club, cocktail lounge, or sunbed service business.
3. "business" - It is a hotel, resort, restaurant, tavern, cafe, or other food/lodging business.
4. "other" - It is a parking lot, port, marina, church, or unrelated feature.

Return a JSON object with:
{{
  "is_beach": "beach" | "beach_bar" | "business" | "other",
  "confidence": float between 0.0 and 1.0,
  "reasoning": "brief explanation"
}}
"""
    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        data = json.loads(resp.text)
        return data["is_beach"], float(data["confidence"]), data["reasoning"]
    except Exception as e:
        cls, conf, reason = classify_rule_based(name, category, reviews)
        return cls, conf, f"Gemini error: {e}. Fallback: {reason}"


def main():
    ap = argparse.ArgumentParser(description="Convert gmaps_verification.json to a deduplicated classified GeoJSON")
    ap.add_argument("--use-gemini", action="store_true", help="Use Gemini 2.5 Flash to classify found POIs")
    ap.add_argument("--api-key", type=str, default="", help="Gemini API Key (optional, defaults to environment)")
    args = ap.parse_args()

    if not VERIFY_OUT.exists():
        print(f"Error: {VERIFY_OUT} does not exist. Run the scraper first.")
        return

    print(f"Reading scraped results from {VERIFY_OUT}...")
    scraped_data = json.loads(VERIFY_OUT.read_text(encoding="utf-8"))

    # Group and deduplicate by href or rounded coordinates
    unique_beaches = {}
    
    for src_uid, result in scraped_data.items():
        if not result.get("found"):
            continue
        
        for b in result.get("beaches", []):
            href = b.get("href")
            lat = b.get("latitude")
            lon = b.get("longitude")
            
            if not lat or not lon:
                continue
                
            # Create a unique key
            if href:
                key = href
            else:
                key = f"{lat:.5f},{lon:.5f}"
                
            if key not in unique_beaches:
                unique_beaches[key] = {
                    "name": b.get("name"),
                    "rating": b.get("rating"),
                    "user_ratings": b.get("user_ratings", 0),
                    "latitude": lat,
                    "longitude": lon,
                    "category": b.get("category"),
                    "address": b.get("address"),
                    "phone": b.get("phone"),
                    "website": b.get("website"),
                    "reviews": b.get("reviews", []),
                    "href": href,
                    "source_uids": {src_uid}
                }
            else:
                # Merge source UIDs
                unique_beaches[key]["source_uids"].add(src_uid)
                # Keep the record with more reviews or higher rating count
                if len(b.get("reviews", [])) > len(unique_beaches[key]["reviews"]):
                    unique_beaches[key]["reviews"] = b.get("reviews", [])
                if b.get("user_ratings", 0) > unique_beaches[key]["user_ratings"]:
                    unique_beaches[key]["user_ratings"] = b.get("user_ratings", 0)
                    if b.get("rating") is not None:
                        unique_beaches[key]["rating"] = b.get("rating")
                    if b.get("category") and not unique_beaches[key]["category"]:
                        unique_beaches[key]["category"] = b.get("category")

    print(f"Found {len(unique_beaches):,} unique beach POIs in scraper database.")

    features = []
    gemini_key = args.api_key or os.environ.get("GEMINI_API_KEY")

    for i, (key, beach) in enumerate(unique_beaches.items()):
        name = beach["name"]
        category = beach["category"]
        reviews = beach["reviews"]
        
        print(f"[{i+1}/{len(unique_beaches)}] Classifying {name!r} ({category or 'no category'})...")
        
        if args.use_gemini:
            is_beach, confidence, reasoning = classify_with_gemini(name, category, reviews, api_key=gemini_key)
            method = "gemini"
        else:
            is_beach, confidence, reasoning = classify_rule_based(name, category, reviews)
            method = "rule_based"

        # Unique ID for the GeoJSON feature
        # Generate stable hash based on name & coordinates
        hash_id = abs(hash(f"{name}_{beach['latitude']:.5f}_{beach['longitude']:.5f}")) % 1000000
        uid = f"gmaps-{hash_id:06d}"

        feat = {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [beach["longitude"], beach["latitude"]]
            },
            "properties": {
                "uid": uid,
                "name": name,
                "rating": beach["rating"],
                "user_ratings": beach["user_ratings"],
                "category": category,
                "address": beach["address"],
                "phone": beach["phone"],
                "website": beach["website"],
                "href": beach["href"],
                "source_uids": list(beach["source_uids"]),
                "is_beach": is_beach,
                "classification_method": method,
                "confidence": confidence,
                "reasoning": reasoning,
                "reviews": reviews[:5]  # Limit embedded reviews count in GeoJSON to 5 to avoid file bloating
            }
        }
        features.append(feat)

    geojson_data = {
        "type": "FeatureCollection",
        "features": features
    }

    GEOJSON_OUT.write_text(json.dumps(geojson_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSuccessfully wrote {len(features):,} classified features to {GEOJSON_OUT}")


if __name__ == "__main__":
    main()
