# zagflights ✈️🚌

Find the **cheapest door-to-door trip from Zagreb**, even when that means taking a bus to another airport first, hopping on a cheap flight to another airport, or flying back into a different airport than you left from.

```
🚌 Zagreb → Venice Airport   FlixBus  €20.98
✈ VCE → ATH                 Ryanair  €21
          … holiday …
✈ ATH → BUD                 Wizz Air €44
🚌 Budapest Airport → Zagreb FlixBus  €22.48
```

For a destination and two dates (optionally ± a few days), it:

1. searches one-way flights **from every nearby airport** to the destination and back:
   - Zagreb, Zadar, Split, Osijek
   - Ljubljana, Graz, Trieste, Venice, Treviso
   - Vienna, Bratislava, Budapest, Belgrade

   It uses Kiwi.com for all airlines, and reprices **Ryanair and Wizz Air flights with the airlines' own fares** (usually a few € cheaper than Kiwi).
2. finds the **cheapest live FlixBus** between Zagreb and each airport that actually fits the flight times (check-in buffer, landing buffer, airport transfer, max 7 h wait);
3. also tries **cheap first hops**: on the travel date, any flight ZAG → X under €50 (e.g. ZAG → Bergamo €19), then X → destination with ≥ 3 h to change. Mirrored on the way back (destination → X, X → ZAG under €50);
4. picks the best outbound and return legs **independently** (open-jaw) and ranks the complete trips by total price, against the "just fly from Zagreb" baseline.

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
| `--max-wait H` | max hours between bus and flight, or between two flights (default 7) |
| `--hop-max €` | price cap for the first hop ZAG → X (default 50; `0` turns hops off) |
| `--self-transfer MIN` | minimum time between two separately booked flights (default 180) |
| `--no-direct` | don't check Ryanair/Wizz Air directly (Kiwi prices only) |
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

### Events (BEST Calendar)

```bash
zagflights events data/events.json --out data/flights.json
```

Prices the cheapest trip to every upcoming event in a [BEST Calendar](https://github.com/zkristo/best-calendar) `events.json` and writes `flights.json`, which the calendar site shows in each event's details. The calendar's hourly routine runs it.

- **Which events:** in-person events starting tomorrow or later that have either:
  - an `airports` list (e.g. `["ATH"]` or `["BRU", "CRL"]`): searched by bus + flight as above; or
  - a `bus` city (e.g. `"Ljubljana"`, `"Budapest"`) for places within bus reach: searched by FlixBus alone, Zagreb ↔ that city. Night rides (leaving or arriving 00:00–06:00) only win when they're at least €3 cheaper, and each option uses a different outbound bus.

  Deadlines, online events and events with neither field are skipped.
- **Which dates:**
  - Main result: fly out on the event's first day, landing that day, and fly home on its last day. Up to 3 options are kept.
  - Second option, always saved as `alternative`: the cheapest trip that is a day off (`--flex`, default ±1), e.g. arriving a day late or leaving a day early. It includes how many days it shifts the arrival and departure, and how much cheaper (or dearer) it is than the main option.
  - Also saved: the price grid by date and the "fly from Zagreb" baseline.
- **How often:** an event is searched again when its prices are older than `--max-age-hours` (20) or its dates or airports changed. At most `--limit` (4) events are searched per run, stalest first, so a run stays short. Events beyond `--horizon-days` (180) wait.
- **Failures:** if a search fails, the previous prices are kept with a `lastError` note. Past events are dropped.
- **No-op runs:** if nothing changed, the file isn't rewritten, so there is nothing to commit.

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

Add a hub (e.g. Bologna `BLQ`) with any FlixBus city name; disable one with `enabled = false`. The `[settings]` block holds:

- the check-in buffer (120 min) and landing buffer (45 min);
- max wait (7 h), overnight cost and wait cost;
- the first-hop cap (€50) and minimum self-transfer (180 min);
- whether to check airlines directly.

Transfer times and costs are **estimates**, so adjust them to how you travel. Zadar, Split and Osijek only use FlixBus rides; other Croatian bus operators (e.g. Arriva) aren't searched.

## How it works

- **Flights**: `src/zagflights/providers/kiwi.py` calls Kiwi.com's public MCP server (`https://mcp.kiwi.com`, the same one behind the Kiwi connector in Claude) with JSON-RPC `tools/call → search-flight`. Each call returns up to ~15 cheapest itineraries, so there is one call per airport and direction, run in parallel.
- **Ryanair direct**: `providers/ryanair.py` uses the fare-finder API behind ryanair.com's fare calendar (`services-api.ryanair.com/farfnd/v4/oneWayFares/{from}/{to}/cheapestPerDay`). It gives the cheapest flight per day with times. Routes come from `ryanair.com/api/views/locate/…/routes`.
- **Wizz Air direct**: `providers/wizz.py` uses the timetable API behind wizzair.com's fare calendar (`be.wizzair.com/{version}/Api/search/timetable`).
  - Prices come in the departure airport's currency (BUD = HUF) and are converted with the **ECB's daily rates** (`providers/fx.py`). Currencies the ECB doesn't publish, e.g. RSD, fall back to Kiwi.
  - Wizz gives no arrival times, so the flight duration is taken from Kiwi when it knows the route; otherwise it is estimated from the distance and shown as `≈`.
  - A Kiwi flight that is the same flight (route + minute + airline) gets the cheaper direct price and the airline's booking link. Direct-only fares are added. With `--cabin-bag/--checked-bag`, direct prices are skipped, because the airlines' base fares exclude bags.
- **Buses (how FlixBus prices are obtained)**: `providers/flixbus.py` calls FlixBus's own public web API, the same one flixbus.com's search page uses.
  - `global.api.flixbus.com/search/autocomplete/cities?q=Zagreb` turns a city name into its ID.
  - `global.api.flixbus.com/search/service/v4/search?from_city_id=…&to_city_id=…&departure_date=DD.MM.YYYY` returns every ride that day, with times, stations, changes and the live price incl. the platform fee.
  - Results are cached for an hour.
- **First hops**: Kiwi is asked "ZAG → Europe under €50, non-stop, one per city" for the travel dates. The 8 cheapest airports are then searched ZAG → X and X → destination and paired with a ≥ 3 h self-transfer. The return direction is mirrored.
- **Planner**: `src/zagflights/planner.py` checks buses only for flights that can still make the top results: the cheapest few per date, plus each airport's cheapest. It then pairs outbound × return legs, where the return must leave after you land, and ranks them.
- A search takes ~30 s cold and is instant when cached (`~/.cache/zagflights`).

## Caveats

- Kiwi's MCP server and the Ryanair, Wizz Air and FlixBus APIs are public but unofficial for this kind of use. They can rate-limit or change without notice. If an airline's API fails, Kiwi's price is used and a warning says so.
- Prices are live snapshots and move quickly; always re-check on the booking page.
- Separate tickets mean **no protection if a bus or flight is late**. Leave buffers (the defaults are 2 h before take-off and 45 min after landing).
- Ryanair's API only exposes the cheapest flight of each day. Other Ryanair flights that day keep Kiwi's price.
- First hops are **separate tickets**: a delayed hop can make you miss the onward flight. That's why there's a 3 h minimum.

## Tests

```bash
pytest
```

Tests cover:

- the planner rules: timing, overnight, open-jaw pairing, pruning, wait cost, first hops, direct pricing;
- the parsers and merging, on recorded Kiwi, FlixBus, Ryanair, Wizz and ECB responses;
- the HTTP clients (mocked);
- the CLI and web page;
- the BEST Calendar batch mode (`events`).
