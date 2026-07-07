#!/usr/bin/env python3
import shutil
import json
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
VM_PULLED_NEW = ROOT / "vm_pulled_new"
DATA_NEW = ROOT / "data_new"

def backup_and_copy(filename):
    src = VM_PULLED_NEW / filename
    dst = DATA_NEW / filename
    
    if not src.exists():
        print(f"Error: {src} does not exist!")
        return False
        
    if dst.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = DATA_NEW / f"{dst.stem}_backup_before_pulled_new_{stamp}{dst.suffix}"
        shutil.copy2(dst, backup)
        print(f"Backed up existing {dst.name} to {backup.name}")
        
    shutil.copy2(src, dst)
    print(f"Copied {src.name} to {dst.name}")
    return True

def main():
    DATA_NEW.mkdir(parents=True, exist_ok=True)
    c1 = backup_and_copy("current.json")
    c2 = backup_and_copy("deleted_gmaps.json")
    if c1 and c2:
        print("New data preparation complete. Ready to proceed.")

if __name__ == "__main__":
    main()
