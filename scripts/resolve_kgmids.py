#!/usr/bin/env python3
"""
Resolve kgmid (Freebase ID / P646) for each city in the European airport cities list.
Uses data/kgmid_overrides.json first, then optionally Wikidata API.
Outputs data/cities_with_kgmid.json with city, country, and kgmid (or null).
"""
import json
import os
import urllib.request
import urllib.parse
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
CITIES_PATH = os.path.join(DATA_DIR, "european_airport_cities.json")
FREEBASE_KGMIDS_PATH = os.path.join(DATA_DIR, "freebase_city_kgmids.json")
OVERRIDES_PATH = os.path.join(DATA_DIR, "kgmid_overrides.json")
OUTPUT_PATH = os.path.join(DATA_DIR, "cities_with_kgmid.json")


def load_freebase_kgmids():
    if not os.path.isfile(FREEBASE_KGMIDS_PATH):
        return {}
    with open(FREEBASE_KGMIDS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def load_overrides():
    with open(OVERRIDES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def wikidata_search_city(city_name: str, country: str) -> Optional[str]:
    """Search Wikidata for a city and return P646 (Freebase ID) if found."""
    query = f"{city_name} {country}"
    url = (
        "https://www.wikidata.org/w/api.php?"
        + urllib.parse.urlencode(
            {
                "action": "wbsearchentities",
                "search": query,
                "language": "en",
                "limit": 5,
                "format": "json",
            }
        )
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GoogleFlights/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    for item in data.get("search", []):
        entity_id = item.get("id")
        if not entity_id:
            continue
        # Fetch entity to get P646
        entity_url = (
            "https://www.wikidata.org/w/api.php?"
            + urllib.parse.urlencode(
                {
                    "action": "wbgetentities",
                    "ids": entity_id,
                    "props": "claims",
                    "format": "json",
                }
            )
        )
        try:
            req = urllib.request.Request(
                entity_url, headers={"User-Agent": "GoogleFlights/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                entities = json.loads(resp.read().decode()).get("entities", {})
        except Exception:
            continue
        claims = entities.get(entity_id, {}).get("claims", {})
        p646 = claims.get("P646", [])
        if p646 and p646[0].get("mainsnak", {}).get("snaktype") == "value":
            value = p646[0]["mainsnak"].get("datavalue", {}).get("value")
            if value and value.startswith("/m/"):
                return value
    return None


def main():
    with open(CITIES_PATH, encoding="utf-8") as f:
        cities = json.load(f)
    freebase = load_freebase_kgmids()
    overrides = load_overrides()

    result = []
    for city in cities:
        name = city["name"]
        country = city.get("country", "")
        kgmid = freebase.get(name) or overrides.get(name)
        if kgmid is None:
            kgmid = wikidata_search_city(name, country)
        result.append(
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
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"Wrote {OUTPUT_PATH} with {len(result)} cities.")
    missing = sum(1 for r in result if r["kgmid"] is None)
    if missing:
        print(f"Missing kgmid for {missing} cities (excluded from link generation).")


if __name__ == "__main__":
    main()
