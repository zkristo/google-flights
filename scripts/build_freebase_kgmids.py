#!/usr/bin/env python3
"""
Build city -> kgmid mapping from Freebase Easy freebase-links.txt.
Streams the file (no full load). Outputs data/freebase_city_kgmids.json.
Usage: python3 build_freebase_kgmids.py [path_to_freebase_easy_dir]
Default dir: /Users/zvonimir/Downloads/freebase-easy-latest
"""
import json
import os
import re
import sys
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
CITIES_PATH = os.path.join(DATA_DIR, "european_airport_cities.json")
OUTPUT_PATH = os.path.join(DATA_DIR, "freebase_city_kgmids.json")
DEFAULT_FREEBASE_DIR = "/Users/zvonimir/Downloads/freebase-easy-latest"
LINKS_FILE = "freebase-links.txt"

# URI pattern: <http://rdf.freebase.com/ns/m.xxxxx>
MID_FROM_URI = re.compile(r"ns/(m\.[a-z0-9_]+)>")


def parse_mid_from_uri(col3: str) -> Optional[str]:
    """Extract mid from column 3, e.g. m.05qtj -> /m/05qtj."""
    m = MID_FROM_URI.search(col3)
    if not m:
        return None
    return "/" + m.group(1).replace(".", "/", 1)  # m.05qtj -> /m/05qtj


def main():
    freebase_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FREEBASE_DIR
    links_path = os.path.join(freebase_dir, LINKS_FILE)
    if not os.path.isfile(links_path):
        print(f"Error: {links_path} not found. Pass Freebase Easy directory as first argument.")
        sys.exit(1)

    with open(CITIES_PATH, encoding="utf-8") as f:
        cities = json.load(f)
    city_names = {c["name"] for c in cities}
    wanted = set(city_names)
    mapping = {}

    print(f"Streaming {links_path} for {len(wanted)} city names...")
    with open(links_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not wanted:
                break
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            label = parts[0].strip().strip('"').strip()
            if label in wanted:
                mid = parse_mid_from_uri(parts[2])
                if mid:
                    mapping[label] = mid
                    wanted.discard(label)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)
    print(f"Wrote {OUTPUT_PATH} with {len(mapping)} cities from Freebase Easy.")
    if wanted:
        print(f"Not found in dump ({len(wanted)}): {sorted(wanted)[:10]}{'...' if len(wanted) > 10 else ''}")


if __name__ == "__main__":
    main()
