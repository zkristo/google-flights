#!/usr/bin/env python3
"""
Resolve kgmid (Freebase / Knowledge Graph ID) for each city using the Google Knowledge Graph API.
Reads data/european_airport_cities.json, queries the KG API for each city, and writes
data/cities_with_kgmid.json with name, country, country_code, iatacode, and kgmid.

Requires: GOOGLE_KG_API_KEY environment variable (or a .env file).
"""
import json
import os
import urllib.request
import urllib.parse
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
CITIES_PATH = os.path.join(DATA_DIR, "european_airport_cities.json")
OUTPUT_PATH = os.path.join(DATA_DIR, "cities_with_kgmid.json")

KG_API_URL = "https://kgsearch.googleapis.com/v1/entities:search"


def load_api_key() -> str:
    """Load the Google KG API key from env or .env file."""
    key = os.environ.get("GOOGLE_KG_API_KEY")
    if key:
        return key
    env_path = os.path.join(SCRIPT_DIR, "..", ".env")
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("GOOGLE_KG_API_KEY="):
                    return line.split("=", 1)[1].strip()
    raise RuntimeError("GOOGLE_KG_API_KEY not set. Add it to .env or export it as an env variable.")


def query_kg_api(city_name: str, country: str, api_key: str) -> Optional[str]:
    """Query the Google Knowledge Graph API for a city and return its /m/ kgmid, or None."""
    params = urllib.parse.urlencode(
        {
            "query": f"{city_name} {country}",
            "types": "City",
            "languages": "en",
            "limit": 5,
            "key": api_key,
        }
    )
    url = f"{KG_API_URL}?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GoogleFlights/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        print(f"  KG API error for {city_name!r}: {exc}")
        return None

    for item in data.get("itemListElement", []):
        result = item.get("result", {})
        mid = result.get("@id", "")
        if mid.startswith("kg:/m/"):
            return mid.replace("kg:", "")
    return None


def main():
    api_key = load_api_key()

    with open(CITIES_PATH, encoding="utf-8") as f:
        cities = json.load(f)

    results = []
    missing = 0
    for city in cities:
        name = city["name"]
        country = city.get("country", "")
        print(f"Resolving {name}, {country} ...", end=" ", flush=True)
        kgmid = query_kg_api(name, country, api_key)
        if kgmid:
            print(kgmid)
        else:
            print("NOT FOUND")
            missing += 1
        results.append(
            {
                "name": name,
                "country": country,
                "country_code": city.get("country_code", ""),
                "iatacode": city.get("iatacode", ""),
                "kgmid": kgmid,
            }
        )

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {OUTPUT_PATH} with {len(results)} cities ({missing} missing kgmid).")


if __name__ == "__main__":
    main()
