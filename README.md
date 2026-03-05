# European cities to Paris flight links (April 30th, 2026)

This project builds one **Google Travel explore** URL per European city with a major international airport, each link searching for flights from that city to **Paris** on **2026-04-30** (round-trip).

## Output

- **`output/cities_to_paris_2026-04-30.json`** – One object per city: `city`, `country`, `kgmid`, `url`.
- **`output/cities_to_paris_2026-04-30.csv`** – Same data in CSV form.

## Data sources

- **City list:** `data/european_airport_cities.json` – Curated list of European cities with major international airports (~110), from Wikipedia-style lists (e.g. busiest airports in Europe, European capitals, major hubs). Fields: `name`, `country`, `country_code`, `iatacode`.
- **Kgmid resolution:** Each city’s **kgmid** (Google Knowledge Graph / Freebase ID, e.g. `/m/05qtj` for Paris) is taken from (first match wins):
  1. **`data/freebase_city_kgmids.json`** – Mapping built from the **Freebase Easy** dump (`freebase-links.txt`). Optional; see “Freebase Easy” below.
  2. **`data/kgmid_overrides.json`** – Manual mapping for cities not in Freebase or where you want to override.
  3. **Wikidata** – Property **P646** (Freebase ID) for the city entity, found via search + entity fetch.
- **Resolved list:** `data/cities_with_kgmid.json` – Merged list with a `kgmid` field per city (or `null` if unresolved). Only cities with a non-null `kgmid` get a generated link.

## How the URL is built

- **Base URL:** `https://www.google.com/travel/explore`
- **Parameters:** `tfs` (base64url-encoded protobuf with date, origin kgmid, destination kgmid), `tfu=GgA`, `tcfs` (Paris highlight from a reference URL).
- **Method:** Template substitution. A reference **Zagreb → Paris** `tfs` (derived from a Zagreb → Europe URL by replacing the destination with Paris) is decoded to binary. For each city, the **full 8-byte origin slot** (length byte + 8 bytes) is replaced with the city’s kgmid (length byte + 6–8 bytes as needed); the **destination** stays Paris (`/m/05qtj`). Replacing the full slot ensures each link has the correct origin (no trailing byte). The result is base64url-encoded and used as `tfs`.

## How to run

1. **(Optional) Build Freebase kgmid mapping** – If you have the [Freebase Easy](https://freebase-easy.cs.uni-freiburg.de/dump/) dump extracted (e.g. at `/Users/zvonimir/Downloads/freebase-easy-latest`), run once:
   ```bash
   python3 scripts/build_freebase_kgmids.py [path_to_freebase_easy_dir]
   ```
   Default path: `/Users/zvonimir/Downloads/freebase-easy-latest`. Streams `freebase-links.txt` and writes `data/freebase_city_kgmids.json`. Cities not found in the dump are left for overrides/Wikidata.

2. **Resolve kgmids** (Freebase mapping if present, then overrides, then Wikidata):
   ```bash
   python3 scripts/resolve_kgmids.py
   ```
   Reads `data/european_airport_cities.json`, optional `data/freebase_city_kgmids.json`, and `data/kgmid_overrides.json`; writes `data/cities_with_kgmid.json`.

3. **Generate links** (one URL per city with a kgmid):
   ```bash
   python3 scripts/generate_links.py
   ```
   Reads `data/cities_with_kgmid.json`, writes `output/cities_to_paris_2026-04-30.json` and `output/cities_to_paris_2026-04-30.csv`.

## File layout

- `data/european_airport_cities.json` – Curated city list.
- `data/freebase_city_kgmids.json` – City → kgmid from Freebase Easy (optional; from step 1).
- `data/kgmid_overrides.json` – Manual kgmid overrides.
- `data/cities_with_kgmid.json` – Cities with resolved kgmid (output of step 2).
- `scripts/build_freebase_kgmids.py` – Build Freebase mapping from `freebase-links.txt`.
- `scripts/resolve_kgmids.py` – Resolve kgmid per city.
- `scripts/generate_links.py` – Build `tfs` and write links.
- `output/cities_to_paris_2026-04-30.json` – Final links (JSON).
- `output/cities_to_paris_2026-04-30.csv` – Final links (CSV).

## Notes

- **Paris** is fixed as destination (kgmid `/m/05qtj`); **date** is 2026-04-30.
- Trip type in the encoded `tfs` is round-trip (same as the reference URL).
- Cities without a resolved kgmid are skipped and do not appear in the output files.
