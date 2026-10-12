"""Batch mode for the BEST Calendar: find the cheapest trip from Zagreb to each upcoming event.

Reads the calendar's ``data/events.json`` and writes ``data/flights.json``, which the site shows in
each event's details. Run by the calendar's hourly sync routine:

    zagflights events data/events.json --out data/flights.json

Only in-person events with destination airports (the ``airports`` field, e.g. ``["ATH"]``) are
searched. Results are refreshed when older than ``--max-age-hours``, at most ``--limit`` events
per run (stalest first), so one run stays short.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import fmt
from .config import Config
from .models import Leg, PlanResult, Trip
from .planner import Query, combine

TZ = ZoneInfo("Europe/Zagreb")
SKIP_TAGS = {"deadline"}
OPTIONS = 3  # trips shown per event


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

    @property
    def destination(self) -> str:
        return ",".join(self.airports)

    @property
    def key(self) -> dict:
        """What the cached result depends on; a change forces a new search."""
        return {"destination": self.destination, "out": self.out.isoformat(), "back": self.back.isoformat()}


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
        if not airports:
            skipped.append((e.get("id", "?"), "no airports field"))
            continue
        found.append(Target(e["id"], e.get("title", ""), airports, start, max(end, start)))
    return found, skipped


# ------------------------------------------------------------------ JSON for the website

def _t(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _flight(f, hop: bool = False) -> dict:
    return {
        "kind": "flight", "hop": hop, "from": f.origin, "to": f.destination,
        "fromName": f.origin_name, "toName": f.destination_name,
        "dep": _t(f.departure), "arr": _t(f.arrival), "approxArrival": f.approx_arrival,
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


def summarise(target: Target, res: PlanResult, searched_at: datetime) -> dict:
    """The event's entry in flights.json."""
    out_ok = [leg for leg in res.out_legs
              if leg.flight.departure.date() == target.out and leg.flight.arrival.date() <= target.out]
    back_ok = [leg for leg in res.back_legs if leg.flight.departure.date() == target.back]
    primary = combine(Query(target.destination, target.out, target.back, top=OPTIONS), out_ok, back_ok)
    best = primary.trips
    flexible = res.trips[0] if res.trips and (not best or res.trips[0].total < best[0].total) else None
    return {
        "title": target.title,
        **target.key,
        "searchedAt": iso_local(searched_at),
        "best": [trip_json(t) for t in best],
        "baseline": primary.baseline.total if primary.baseline else None,
        "cheapestFlexible": trip_json(flexible) if flexible else None,
        "grid": [{"out": o.isoformat(), "back": b.isoformat(), "total": v} for (o, b), v in sorted(res.date_matrix.items())],
        "warnings": res.warnings,
    }


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
        progress(f"✈ {t.title}: {t.destination} {t.out} → {t.back}")
        q = Query(t.destination, t.out, t.back, flex=flex, top=20)
        try:
            res = await search(q, config)
            entries[t.id] = summarise(t, res, now_local())
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
