import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
GEOJSON = ROOT / "data_new" / "current.json"

def main():
    if not GEOJSON.exists():
        print("current.json does not exist.")
        return
        
    fc = json.loads(GEOJSON.read_text(encoding="utf-8"))
    features = fc.get("features", [])
    
    total_features = len(features)
    
    sections = 0
    mains = 0
    independent = 0
    role_none = 0
    
    parent_uids = set()
    section_parent_uids = set()
    
    for f in features:
        props = f.get("properties", {})
        role = props.get("beach_role")
        parent_uid = props.get("parent_beach_uid")
        
        if role == "section":
            sections += 1
            if parent_uid:
                section_parent_uids.add(parent_uid)
        elif role == "main":
            mains += 1
            uid = props.get("uid")
            if uid:
                parent_uids.add(uid)
        else:
            independent += 1
            
    print(f"Total features (individual points/shapes) in current.json: {total_features:,}")
    print(f"  - Independent beaches: {independent:,}")
    print(f"  - Main beaches (parent of a group): {mains:,}")
    print(f"  - Section beaches (child sections belonging to a main): {sections:,}")
    print(f"\nUnique beach entities (Independent + Main beaches): {independent + mains:,}")
    print(f"Section parent UIDs found: {len(section_parent_uids):,}")

if __name__ == "__main__":
    main()
