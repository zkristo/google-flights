#!/usr/bin/env python3
"""
Generate Google Travel explore URLs for flights from each European city to Paris on 2026-04-30.
Reads data/cities_with_kgmid.json, builds tfs by replacing the full 8-byte origin slot (fixes
"all links the same" bug). Writes output/cities_to_paris_2026-04-30.json and .csv.
"""
import base64
import csv
import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "..", "output")
CITIES_KGMID_PATH = os.path.join(DATA_DIR, "cities_with_kgmid.json")
OUTPUT_JSON = os.path.join(OUTPUT_DIR, "cities_to_paris_2026-04-30.json")
OUTPUT_CSV = os.path.join(OUTPUT_DIR, "cities_to_paris_2026-04-30.csv")

# Zagreb -> Europe tfs; we replace Europe with Paris to get the template
TFS_ZAGREB_EUROPE_B64 = (
    "CBwQAxooEgoyMDI2LTA0LTMwagwIAhIIL20vMGZoenlyDAgEEggvbS8wMmo5ekABSAFwAYIBCwj"
    "___________8BmAECsgEKEggvbS8wNXF0ag"
)
PARIS_KGMID = "/m/05qtj"
TEMPLATE_ORIGIN_KGMID = "/m/0fhzyr"
TEMPLATE_ORIGIN_BYTES = 8
BASE_URL = "https://www.google.com/travel/explore"
TCFS_PARIS = "EhEKCC9tLzA1cXRqEgVQYXJpc1IEYAJ4AQ"
TFU = "GgA"


def b64url_decode(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    pad = (4 - len(s) % 4) % 4
    s += "=" * pad
    return base64.b64decode(s)


def b64url_encode(b: bytes) -> str:
    return base64.b64encode(b).decode().rstrip("=").replace("+", "-").replace("/", "_")


def build_zagreb_to_paris_template() -> bytes:
    """From Zagreb->Europe tfs, replace destination Europe with Paris; return raw bytes."""
    raw = b64url_decode(TFS_ZAGREB_EUROPE_B64)
    europe = b"/m/02j9z"
    paris_b = PARIS_KGMID.encode()
    idx = raw.find(europe)
    if idx < 0:
        raise ValueError("Europe kgmid not found in template")
    len_byte_idx = idx - 1
    # Replace length (1) + Europe (8) = 9 bytes with length (1) + Paris (7) = 8 bytes
    new_raw = (
        raw[:len_byte_idx]
        + bytes([len(paris_b)])
        + paris_b
        + raw[len_byte_idx + 1 + 8 :]
    )
    if len(paris_b) < 8 and new_raw[5] > 0:
        new_raw = new_raw[:5] + bytes([new_raw[5] - 1]) + new_raw[6:]
    return new_raw


def build_tfs_for_origin(origin_kgmid: str, template_raw: bytes) -> bytes:
    """Replace the full TEMPLATE_ORIGIN_BYTES (8) slot with length byte + origin kgmid."""
    origin_b = origin_kgmid.encode()
    template_origin_b = TEMPLATE_ORIGIN_KGMID.encode()
    idx = template_raw.find(template_origin_b)
    if idx < 0:
        raise ValueError(f"Template origin {TEMPLATE_ORIGIN_KGMID!r} not found in template")
    len_idx = idx - 1
    new_len = len(origin_b)
    delta = new_len - TEMPLATE_ORIGIN_BYTES
    new_raw = (
        template_raw[:len_idx]
        + bytes([new_len])
        + origin_b
        + template_raw[len_idx + 1 + TEMPLATE_ORIGIN_BYTES :]
    )
    if delta != 0 and 0 <= new_raw[5] + delta <= 0xFF:
        new_raw = new_raw[:5] + bytes([new_raw[5] + delta]) + new_raw[6:]
    return new_raw


def build_url(origin_kgmid: str, template_raw: bytes) -> str:
    tfs_raw = build_tfs_for_origin(origin_kgmid, template_raw)
    tfs_b64 = b64url_encode(tfs_raw)
    return f"{BASE_URL}?tfs={tfs_b64}&tfu={TFU}&tcfs={TCFS_PARIS}"


def main():
    with open(CITIES_KGMID_PATH, encoding="utf-8") as f:
        cities = json.load(f)
    template_raw = build_zagreb_to_paris_template()
    results = []
    skipped = 0
    for c in cities:
        kgmid = c.get("kgmid")
        if not kgmid or not kgmid.startswith("/m/"):
            skipped += 1
            continue
        try:
            url = build_url(kgmid, template_raw)
        except Exception:
            skipped += 1
            continue
        results.append(
            {"city": c["name"], "country": c["country"], "kgmid": kgmid, "url": url}
        )
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    with open(OUTPUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["city", "country", "kgmid", "url"])
        w.writeheader()
        w.writerows(results)
    print(f"Wrote {OUTPUT_JSON} and {OUTPUT_CSV} with {len(results)} links.")
    if skipped:
        print(f"Skipped {skipped} cities (no kgmid or encoding error).")


if __name__ == "__main__":
    main()
