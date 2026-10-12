# zagflights ✈️🚌

Find the **cheapest door-to-door trip from Zagreb**, even when that means taking a bus to another airport first, or flying back into a different airport than you left from.

```
🚌 Zagreb → Venice Airport   FlixBus  €20.98
✈ VCE → ATH                 Ryanair  €21
          … holiday …
✈ ATH → BUD                 Wizz Air €44
🚌 Budapest Airport → Zagreb FlixBus  €22.48
```

For a destination and two dates (optionally ± a few days), it:

1. searches one-way flights **from every nearby airport** (ZAG, LJU, GRZ, TRS, VCE, TSF, VIE, BTS, BUD, BEG) to the destination and back, using Kiwi.com, which covers Ryanair, Wizz and the rest;
2. finds the **cheapest live FlixBus** between Zagreb and each airport that actually fits the flight times (check-in buffer, landing buffer, airport transfer, overnight waits);
3. picks the best outbound and return legs **independently** (open-jaw) and ranks the complete trips by total price, against the "just fly from Zagreb" baseline.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Requires Python 3.11+. No API keys needed.

## Use it

### Command line

```bash
zagflights search ATH 2026-11-10 2026-11-15            # exact dates
zagflights search ATH 2026-11-10 2026-11-15 --flex 2   # ±2 days on both dates
zagflights search Barcelona 2026-12-04 2026-12-08 --cabin-bag
zagflights search LON 2026-11-20 2026-11-23 --direct --hubs ZAG,VCE,BUD
```

Useful options:

| Option | What it does |
|---|---|
| `--flex N` | also try ±N days (0–3) around each date; prints a price grid by date |
| `--direct` / `--max-stops N` | non-stop only / limit stops per flight |
| `--cabin-bag`, `--checked-bag` | price flights with bags included |
| `--adults N` | price for N people (flights and buses) |
| `--hubs ZAG,BUD` / `--exclude BEG` | choose which airports to consider |
| `--min-nights N` | minimum nights at the destination |
| `--max-wait H` | max hours between bus and flight (default 20) |
| `--overnight-cost €` | add a cost when a wait runs through the night |
| `--wait-cost €` | value your time: € per hour of waiting beyond 3h. Prefers shorter waits; shown totals are still real money |
| `--json` | machine-readable output |
| `--no-cache` | ignore cached prices (they are cached for 1 hour) |

`zagflights hubs` lists the airports and how you reach them.

### Web page

```bash
zagflights serve          # → http://127.0.0.1:8000
```

Same search as the CLI, with a form, result cards, booking links, the date grid and a per-airport summary. There is also a JSON endpoint: `/api/search?dest=ATH&out=2026-11-10&back=2026-11-15&flex=2`.

## Customise the airports

```bash
zagflights init-config    # copies the defaults to ~/.config/zagflights/hubs.toml
```

Each airport lists the FlixBus stop(s) near it and the transfer from that stop to the terminal, for example:

```toml
[hubs.BUD]
name = "Budapest"
stops = [
    { flixbus = "Budapest Airport", transfer_min = 5,  transfer_cost = 0 },  # bus straight to the airport
    { flixbus = "Budapest",         transfer_min = 60, transfer_cost = 6 },  # city bus station + 100E bus
]
```

Add a hub (e.g. Bologna `BLQ`) with any FlixBus city name; disable one with `enabled = false`. The `[settings]` block holds the check-in buffer (120 min), the landing buffer (45 min), max wait, overnight cost and wait cost. Transfer times and costs are **estimates**, so adjust them to how you travel.

## How it works

- **Flights**: `src/zagflights/providers/kiwi.py` calls Kiwi.com's public MCP server (`https://mcp.kiwi.com`, the same one behind the Kiwi connector in Claude) with JSON-RPC `tools/call → search-flight`. Each call returns up to ~15 cheapest itineraries, so there is one call per airport and direction, run in parallel.
- **Buses**: `src/zagflights/providers/flixbus.py` uses FlixBus's public web API (city autocomplete + day search). Prices include the platform fee.
- **Planner**: `src/zagflights/planner.py` checks buses only for flights that can still make the top results: the cheapest few per date, plus each airport's cheapest. It then pairs outbound × return legs, where the return must leave after you land, and ranks them.
- A search takes ~20 s cold and is instant when cached (`~/.cache/zagflights`).

## Caveats

- Kiwi's MCP server and FlixBus's API are public but unofficial for this kind of use. They can rate-limit or change without notice.
- Prices are live snapshots and move quickly; always re-check on the booking page.
- Separate tickets mean **no protection if a bus or flight is late**. Leave buffers (the defaults are 2 h before take-off and 45 min after landing).
- Kiwi's prices can be a few euros above booking directly with the airline. Once you've picked a combo, check the airline's own site.

## Tests

```bash
pytest
```

Tests cover the planner rules (timing, overnight, open-jaw pairing, pruning, wait cost), the parsers (on recorded Kiwi/FlixBus responses), the HTTP clients (mocked) and the CLI and web page.
