"""Batch mode for the BEST Calendar: find the cheapest trip from Zagreb to each upcoming event.

Reads the calendar's ``data/events.json`` and writes ``data/flights.json``, which the site shows in
each event's details. Run by the calendar's hourly sync routine:

    zagflights events data/events.json --out data/flights.json

Only in-person events are searched: by bus + flight when they list destination airports (the
``airports`` field, e.g. ``["ATH"]``), or by FlixBus alone when they name a city within bus reach
(the ``bus`` field, e.g. ``"Ljubljana"``). Results are refreshed when older than
``--max-age-hours``, at most ``--limit`` events per run (stalest first), so one run stays short.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from . import fmt
from .config import Config
from .models import BusRide, Leg, PlanResult, Trip
from .planner import Query, combine

TZ = ZoneInfo("Europe/Zagreb")
SKIP_TAGS = {"deadline"}
OPTIONS = 3  # trips kept for the event's own dates
FORMAT = 3  # bump when the entry shape changes, so older entries are searched again
BusSearch = Callable[[str, str, date], Awaitable[list[BusRide]]]  # (from city, to city, day) → rides


def now_local() -> datetime:
    return datetime.now(TZ).replace(second=0, microsecond=0)


def iso_local(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def plain_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Target:
    """An event we can search flights for."""

    id: str
    title: str
    airports: tuple[str, ...]
    out: date  # arrive by this day (the event's first day)
    back: date  # fly home on this day (the event's last day)
    bus: str | None = None  # FlixBus city for events within bus reach (searched by bus only)

    @property
    def destination(self) -> str:
        return f"bus:{self.bus}" if self.bus else ",".join(self.airports)

    @property
    def key(self) -> dict:
        """What the cached result depends on; a change forces a new search."""
        return {"destination": self.destination, "out": self.out.isoformat(), "back": self.back.isoformat(),
                "format": FORMAT}


def targets(events: list[dict], today: date, horizon_days: int) -> tuple[list[Target], list[tuple[str, str]]]:
    """Events worth a flight search, plus (id, reason) for in-person events we had to skip."""
    found, skipped = [], []
    for e in events:
        if e.get("tag") in SKIP_TAGS or e.get("status") == "cancelled":
            continue
        loc = e.get("location") or {}
        if not loc.get("name") or loc["name"].strip().lower() == "online":
            continue
        start = plain_date(e.get("start"))
        end = plain_date(e.get("end")) or start
        if not start or start <= today or start > today + timedelta(days=horizon_days):
            continue
        airports = tuple(a.strip().upper() for a in e.get("airports") or [] if isinstance(a, str) and len(a.strip()) == 3)
        bus = e.get("bus").strip() if isinstance(e.get("bus"), str) else ""
        if not airports and not bus:
            skipped.append((e.get("id", "?"), "no airports or bus field"))
            continue
        found.append(Target(e["id"], e.get("title", ""), airports, start, max(end, start), bus=None if airports else bus))
    return found, skipped


# ------------------------------------------------------------------ JSON for the website

def _t(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _flight(f, hop: bool = False) -> dict:
    return {
        "kind": "flight", "hop": hop, "from": f.origin, "to": f.destination,
        "fromName": f.origin_name, "toName": f.destination_name,
        "dep": _t(f.departure), "arr": _t(f.arrival), "approxArrival": f.approx_arrival, "minutes": f.minutes,
        "price": f.price, "carriers": list(f.carriers), "via": list(f.route[1:-1]) if f.stops else [],
        "source": f.source, "url": f.booking_url,
    }


def leg_json(leg: Leg) -> dict:
    steps: list[dict] = []
    if leg.bus is None:
        where = "Home → Zagreb airport" if leg.direction == "out" else "Zagreb airport → home"
        steps.append({"kind": "transfer", "text": where, "minutes": leg.ground_minutes, "price": leg.ground_cost})
    else:
        b, stop = leg.bus, leg.stop
        steps.append({
            "kind": "bus", "from": b.from_station, "to": b.to_station, "dep": _t(b.departure), "arr": _t(b.arrival),
            "price": b.price, "changes": b.transfers, "url": b.booking_url,
            "transfer": {"minutes": stop.transfer_min, "price": stop.transfer_cost} if stop and (stop.transfer_min or stop.transfer_cost) else None,
        })
    flights = [_flight(leg.flight)]
    if leg.hop:
        flights = [_flight(leg.hop, hop=True), *flights] if leg.direction == "out" else [*flights, _flight(leg.hop, hop=True)]
    steps = [*steps, *flights] if leg.direction == "out" else [*flights, *steps]
    wait = fmt.wait_line(leg).lstrip("⏱ ").strip()
    return {
        "airport": leg.hub.code, "airportName": leg.hub.name, "viaFlight": leg.hub.via_flight,
        "total": leg.total, "wait": wait or None, "overnight": leg.overnight, "steps": steps,
    }


def trip_json(t: Trip) -> dict:
    return {"total": t.total, "nights": t.nights, "out": leg_json(t.out), "back": leg_json(t.back)}


def _out_on_time(leg: Leg, target: Target) -> bool:
    """Flies out on the event's first day and lands that day."""
    return leg.flight.departure.date() == target.out and leg.flight.arrival.date() <= target.out


def _back_on_time(leg: Leg, target: Target) -> bool:
    return leg.flight.departure.date() == target.back


def summarise(target: Target, res: PlanResult, searched_at: datetime, flex: int = 1) -> dict:
    """The event's entry in flights.json: the best trips for the event's dates, plus the best ±`flex` day alternative."""
    out_ok = [leg for leg in res.out_legs if _out_on_time(leg, target)]
    back_ok = [leg for leg in res.back_legs if _back_on_time(leg, target)]
    primary = combine(Query(target.destination, target.out, target.back, top=OPTIONS), out_ok, back_ok)
    best = primary.trips
    # the cheapest trip that misses the event's dates by up to `flex` days (arrive a day late, leave a day
    # early, …): always shown, even when dearer
    def near(o: Leg, b: Leg) -> bool:
        return (abs((o.flight.arrival.date() - target.out).days) <= flex
                and abs((b.flight.departure.date() - target.back).days) <= flex)

    others = [Trip(o, b) for o in res.out_legs for b in res.back_legs
              if b.flight.departure > o.flight.arrival and near(o, b)
              and not (_out_on_time(o, target) and _back_on_time(b, target))]
    alt = min(others, key=lambda t: (t.total, t.score)) if others else None
    alternative = None
    if alt:
        alternative = {
            **trip_json(alt),
            "arriveShift": (alt.out.flight.arrival.date() - target.out).days,
            "leaveShift": (alt.back.flight.departure.date() - target.back).days,
            "saving": round(best[0].total - alt.total, 2) if best else None,
        }
    return {
        "title": target.title,
        **target.key,
        "mode": "flight",
        "searchedAt": iso_local(searched_at),
        "best": [trip_json(t) for t in best],
        "alternative": alternative,
        "baseline": primary.baseline.total if primary.baseline else None,
        "grid": [{"out": o.isoformat(), "back": b.isoformat(), "total": v} for (o, b), v in sorted(res.date_matrix.items())],
        "warnings": res.warnings,
    }


# ------------------------------------------------------------------ bus-only events

def _bus_leg(r: BusRide, city: str) -> dict:
    step = {"kind": "bus", "from": r.from_station, "to": r.to_station, "dep": _t(r.departure), "arr": _t(r.arrival),
            "price": r.price, "changes": r.transfers, "url": r.booking_url, "transfer": None}
    return {"airport": city, "airportName": city, "viaFlight": False, "mode": "bus", "total": round(r.price, 2),
            "wait": None, "overnight": r.arrival.date() > r.departure.date(), "steps": [step]}


NIGHT_RIDE_COST = 3.0  # ranking only: a bus leaving or arriving 00:00–06:00 must be this much cheaper to win


def _night(dt: datetime) -> bool:
    return dt.hour < 6


def summarise_bus(target: Target, outs: list[BusRide], backs: list[BusRide], searched_at: datetime,
                  flex: int = 1) -> dict:
    """flights.json entry for an event reached by FlixBus alone: cheapest bus there and back."""
    city = target.bus or ""

    def on_dates(o: BusRide, b: BusRide) -> bool:  # leave and arrive on the first day, head home on the last
        return o.departure.date() == o.arrival.date() == target.out and b.departure.date() == target.back

    def near(o: BusRide, b: BusRide) -> bool:
        return (abs((o.arrival.date() - target.out).days) <= flex
                and abs((b.departure.date() - target.back).days) <= flex)

    def rank(p: tuple[BusRide, BusRide]):
        o, b = p
        nights = sum(_night(x) for x in (o.departure, o.arrival, b.departure, b.arrival))
        # cheapest first, night rides count a little dearer; then arrive earliest and leave latest
        return (round(o.price + b.price + NIGHT_RIDE_COST * min(nights, 2), 2), o.arrival, -b.departure.timestamp())

    pairs = [(o, b) for o in outs for b in backs if b.departure > o.arrival and near(o, b)]
    best, used = [], set()
    for p in sorted((p for p in pairs if on_dates(*p)), key=rank):  # one option per outbound bus
        bus_key = (p[0].from_station, p[0].departure)  # the same bus can be listed once per drop-off stop
        if bus_key not in used and len(best) < OPTIONS:
            used.add(bus_key)
            best.append(p)
    others = [p for p in pairs if not on_dates(*p)]
    alt = min(others, key=rank) if others else None

    def trip(p: tuple[BusRide, BusRide]) -> dict:
        o, b = p
        return {"total": round(o.price + b.price, 2), "nights": (b.departure.date() - o.arrival.date()).days,
                "out": _bus_leg(o, city), "back": _bus_leg(b, city)}

    alternative = None
    if alt:
        alternative = {**trip(alt), "arriveShift": (alt[0].arrival.date() - target.out).days,
                       "leaveShift": (alt[1].departure.date() - target.back).days,
                       "saving": round(trip(best[0])["total"] - trip(alt)["total"], 2) if best else None}
    grid: dict[tuple[date, date], float] = {}
    for o, b in pairs:
        cell = (o.departure.date(), b.departure.date())
        grid[cell] = min(grid.get(cell, 1e9), round(o.price + b.price, 2))
    warnings = []
    if not outs:
        warnings.append(f"No FlixBus rides found to {city} around {target.out}.")
    if not backs:
        warnings.append(f"No FlixBus rides found from {city} around {target.back}.")
    return {
        "title": target.title,
        **target.key,
        "mode": "bus",
        "searchedAt": iso_local(searched_at),
        "best": [trip(p) for p in best],
        "alternative": alternative,
        "baseline": None,
        "grid": [{"out": o.isoformat(), "back": b.isoformat(), "total": v} for (o, b), v in sorted(grid.items())],
        "warnings": warnings,
    }


async def bus_trips(target: Target, home: str, bus_search: BusSearch, flex: int = 1) -> tuple[list[BusRide], list[BusRide]]:
    """Rides home → city around the first day (incl. the evening before) and back around the last day."""
    out_days = [target.out + timedelta(days=k) for k in range(-flex - 1, flex + 1)]
    back_days = [target.back + timedelta(days=k) for k in range(-flex, flex + 1)]
    outs = await asyncio.gather(*(bus_search(home, target.bus, d) for d in out_days))
    backs = await asyncio.gather(*(bus_search(target.bus, home, d) for d in back_days))
    return [r for rs in outs for r in rs], [r for rs in backs for r in rs]


# ------------------------------------------------------------------ batch run

def _age_hours(entry: dict | None, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(entry["searchedAt"])).total_seconds() / 3600
    except (TypeError, KeyError, ValueError):
        return float("inf")


async def refresh(
    events_path: Path,
    out_path: Path,
    config: Config,
    search: Callable,  # async (Query, Config) -> PlanResult
    bus_search: BusSearch | None = None,  # for events with a `bus` city
    max_age_hours: float = 20,
    limit: int = 4,
    horizon_days: int = 180,
    flex: int = 1,
    today: date | None = None,
    progress: Callable[[str], None] = print,
) -> dict:
    now = now_local()
    today = today or now.date()
    events = json.loads(events_path.read_text(encoding="utf-8")).get("events", [])
    old = {}
    if out_path.exists():
        try:
            old = json.loads(out_path.read_text(encoding="utf-8")).get("events", {})
        except ValueError:
            old = {}

    found, skipped = targets(events, today, horizon_days)
    entries: dict[str, dict] = {}
    due: list[tuple[float, Target]] = []
    for t in found:
        prev = old.get(t.id)
        same = prev is not None and all(prev.get(k) == v for k, v in t.key.items())
        if same:
            entries[t.id] = prev
        age = _age_hours(prev, now) if same else float("inf")
        if age >= max_age_hours:
            due.append((age, t))
    due.sort(key=lambda at: -at[0])  # stalest (or never searched) first

    searched, failed = [], []
    for _age, t in due[:limit]:
        progress(f"{'🚌' if t.bus else '✈'} {t.title}: {t.destination} {t.out} → {t.back}")
        try:
            if t.bus:
                if bus_search is None:
                    raise RuntimeError("no bus search available")
                outs, backs = await bus_trips(t, config.settings.home, bus_search, flex)
                entries[t.id] = summarise_bus(t, outs, backs, now_local(), flex=flex)
            else:
                res = await search(Query(t.destination, t.out, t.back, flex=flex, top=20), config)
                entries[t.id] = summarise(t, res, now_local(), flex=flex)
            searched.append(t.id)
        except Exception as exc:  # keep the last good result; note the error
            prev = entries.get(t.id) or {"title": t.title, **t.key, "best": [], "grid": []}
            entries[t.id] = {**prev, "lastError": f"{type(exc).__name__}: {exc}"[:300]}
            failed.append(t.id)

    events_out = dict(sorted(entries.items()))
    changed = events_out != old or not out_path.exists()
    if changed:  # leave the file alone otherwise, so the routine has nothing to commit
        data = {"updatedAt": iso_local(now_local()), "origin": config.settings.home, "events": events_out}
        out_path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"searched": searched, "failed": failed, "skipped": skipped, "written": changed,
            "pending": [t.id for _a, t in due[limit:]], "kept": len(entries) - len(searched)}
