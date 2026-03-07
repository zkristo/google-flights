#!/usr/bin/env python3
"""
Verify city kgmids against Wikidata P646 (Freebase ID).

For each city in cities_with_kgmid.json, queries Wikidata via SPARQL
to get the authoritative P646 value and compares it with the stored kgmid.

Outputs a report and optionally writes a corrected JSON file.

Usage:
  python3 scripts/verify_kgmids.py [--fix]

  --fix   Write a corrected cities_with_kgmid.json using Wikidata values
          wherever they differ (leaves untouched if Wikidata has no P646).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
INPUT_PATH = os.path.join(DATA_DIR, "cities_with_kgmid.json")
OUTPUT_PATH = os.path.join(DATA_DIR, "cities_with_kgmid.json")

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "GoogleFlightsVerify/1.0 (kgmid verification)"


def sparql_query(query: str) -> list[dict]:
    url = SPARQL_ENDPOINT + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return data["results"]["bindings"]


def batch_wikidata_kgmids(cities: list[dict]) -> dict[str, str]:
    """
    Query Wikidata in one SPARQL call for all city names.
    Returns {city_name: wikidata_kgmid} for cities found with P646.

    Strategy: match by English label + instance of city/human settlement,
    prefer items that are a city (Q515) or administrative territorial entity.
    """
    # Build VALUES clause with (name country) pairs
    values_lines = []
    name_country = {}
    for c in cities:
        name = c["name"]
        country = c.get("country", "")
        name_country[name] = country
        values_lines.append(f'"{name}"@en')

    values_block = " ".join(values_lines)

    query = f"""
SELECT ?itemLabel ?freebaseID (COUNT(?sitelink) AS ?links) WHERE {{
  VALUES ?itemLabel {{ {values_block} }}
  ?item rdfs:label ?itemLabel .
  FILTER(LANG(?itemLabel) = "en")
  ?item wdt:P646 ?freebaseID .
  OPTIONAL {{ ?item wikibase:sitelinks ?sitelink }}
  FILTER EXISTS {{
    {{ ?item wdt:P31/wdt:P279* wd:Q515 }}
    UNION
    {{ ?item wdt:P31/wdt:P279* wd:Q486972 }}
    UNION
    {{ ?item wdt:P31/wdt:P279* wd:Q1549591 }}
    UNION
    {{ ?item wdt:P31 wd:Q5119 }}
  }}
}}
GROUP BY ?itemLabel ?freebaseID
ORDER BY ?itemLabel DESC(?links)
"""
    try:
        rows = sparql_query(query)
    except Exception as e:
        print(f"  SPARQL batch query failed: {e}")
        return {}

    # For each city name, pick the result with the most sitelinks (most prominent entity)
    best = {}  # name -> (kgmid, sitelink_count)
    for row in rows:
        name = row.get("itemLabel", {}).get("value", "")
        mid = row.get("freebaseID", {}).get("value", "")
        links = int(row.get("links", {}).get("value", "0"))
        if not name or not mid:
            continue
        if name not in best or links > best[name][1]:
            best[name] = (mid, links)

    return {name: mid for name, (mid, _) in best.items()}


def sparql_lookup_permissive(city_name: str) -> "str | None":
    """
    No instance-of filter — useful for islands, territories, etc.
    Picks the result with most sitelinks (most prominent entity with that label).
    """
    query = f"""
SELECT ?item ?freebaseID (COUNT(?sitelink) AS ?links) WHERE {{
  ?item rdfs:label "{city_name}"@en .
  ?item wdt:P646 ?freebaseID .
  OPTIONAL {{ ?item wikibase:sitelinks ?sitelink }}
}}
GROUP BY ?item ?freebaseID
ORDER BY DESC(?links)
LIMIT 1
"""
    try:
        rows = sparql_query(query)
        if rows:
            return rows[0].get("freebaseID", {}).get("value")
    except Exception:
        pass
    return None


def verify_city_individually(city: dict) -> "str | None":
    """Fallback: look up a single city via Wikidata entity search + P646."""
    name = city["name"]
    country = city.get("country", "")
    query = f"{name} {country}"
    url = (
        "https://www.wikidata.org/w/api.php?"
        + urllib.parse.urlencode({
            "action": "wbsearchentities",
            "search": query,
            "language": "en",
            "limit": 5,
            "format": "json",
        })
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None

    for item in data.get("search", []):
        entity_id = item.get("id")
        if not entity_id:
            continue
        entity_url = (
            "https://www.wikidata.org/w/api.php?"
            + urllib.parse.urlencode({
                "action": "wbgetentities",
                "ids": entity_id,
                "props": "claims",
                "format": "json",
            })
        )
        try:
            req = urllib.request.Request(entity_url, headers={"User-Agent": USER_AGENT})
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
    fix_mode = "--fix" in sys.argv

    with open(INPUT_PATH, encoding="utf-8") as f:
        cities = json.load(f)

    print(f"Loaded {len(cities)} cities from {INPUT_PATH}")
    print("Querying Wikidata SPARQL for P646 values (batch)...")

    wikidata = batch_wikidata_kgmids(cities)
    print(f"Received Wikidata P646 for {len(wikidata)} cities.\n")

    # For cities not returned by the batch query, try individually
    missing_from_batch = [c for c in cities if c["name"] not in wikidata]
    if missing_from_batch:
        print(f"Falling back to individual lookup for {len(missing_from_batch)} cities not in batch result:")
        for city in missing_from_batch:
            print(f"  Looking up {city['name']}...", end=" ", flush=True)
            # Try a broader SPARQL without instance-of filter (handles islands, regions, etc.)
            mid = sparql_lookup_permissive(city["name"])
            if mid:
                wikidata[city["name"]] = mid
                print(mid)
            else:
                mid = verify_city_individually(city)
                if mid:
                    wikidata[city["name"]] = mid
                    print(mid)
                else:
                    print("not found")
            time.sleep(0.5)
        print()

    # Compare
    ok = []
    wrong = []
    missing = []

    for city in cities:
        name = city["name"]
        stored = city.get("kgmid")
        wd = wikidata.get(name)

        if wd is None:
            missing.append((name, stored))
        elif stored == wd:
            ok.append((name, stored))
        else:
            wrong.append((name, stored, wd))

    print("=" * 70)
    print(f"RESULTS: {len(ok)} correct | {len(wrong)} mismatch | {len(missing)} unverified")
    print("=" * 70)

    if wrong:
        print(f"\nMISMATCHES ({len(wrong)}):")
        for name, stored, wd_val in wrong:
            print(f"  {name:25s}  stored={stored}  wikidata={wd_val}")

    if missing:
        print(f"\nNOT IN WIKIDATA ({len(missing)}) — cannot verify:")
        for name, stored in missing:
            print(f"  {name:25s}  stored={stored}")

    if ok:
        print(f"\nCORRECT ({len(ok)}):")
        for name, stored in ok:
            print(f"  {name:25s}  {stored}")

    if fix_mode and wrong:
        print(f"\nFix mode: updating {len(wrong)} cities with Wikidata values...")
        name_to_wd = {name: wd_val for name, _, wd_val in wrong}
        updated = []
        for city in cities:
            c = dict(city)
            if c["name"] in name_to_wd:
                c["kgmid"] = name_to_wd[c["name"]]
            updated.append(c)
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(updated, f, indent=2, ensure_ascii=False)
        print(f"Wrote corrected {OUTPUT_PATH}")
    elif fix_mode:
        print("\nNo mismatches to fix.")


if __name__ == "__main__":
    main()
