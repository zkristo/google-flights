"""Combine buses and flights into the cheapest door-to-door trips from home and back.

Outbound and return are planned independently, since each direction may use a different airport
(e.g. bus to Budapest, fly BUD→ATH, fly ATH→VCE, bus Venice→Zagreb). They are then paired up and ranked.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Protocol

from .config import Config, Hub
from .models import BusRide, Flight, Leg, PlanResult, Trip

KEEP_PER_DATE = 6  # cheapest feasible legs kept per travel date and direction
BATCH = 6  # flights checked for bus connections at a time
PER_HUB_TRIES = 9  # flights tried per hub to find that hub's cheapest feasible leg


def spans_night(start: datetime, end: datetime) -> bool:
    """True if the wait from ``start`` to ``end`` covers any of the 00:00–06:00 night hours."""
    d = start.date()
    while d <= end.date():
        night_start = datetime.combine(d, time(0))
        if start < night_start + timedelta(hours=6) and end > night_start:
            return True
        d += timedelta(days=1)
    return False


class FlightSource(Protocol):
    async def search(self, fly_from: str, fly_to: str, day: date, flex_days: int = 0, adults: int = 1,
                     max_stops: int | None = None, cabin_bag: bool = False, checked_bag: bool = False) -> list[Flight]: ...


class BusSource(Protocol):
    async def search(self, from_city: str, to_city: str, day: date, adults: int = 1) -> list[BusRide]: ...


@dataclass(frozen=True)
class Query:
    destination: str
    out_date: date
    back_date: date
    flex: int = 0
    adults: int = 1
    max_stops: int | None = None
    cabin_bag: bool = False
    checked_bag: bool = False
    min_nights: int | None = None
    top: int = 10

    def window(self, center: date) -> set[date]:
        return {center + timedelta(days=k) for k in range(-self.flex, self.flex + 1)}


class Planner:
    def __init__(self, config: Config, hubs: list[Hub], flights: FlightSource, buses: BusSource,
                 progress: Callable[[str], None] | None = None):
        self.config = config
        self.settings = config.settings
        self.hubs = {h.code: h for h in hubs}
        self.flights = flights
        self.buses = buses
        self.progress = progress or (lambda _msg: None)
        self.warnings: list[str] = []
        self._memo: dict[tuple, asyncio.Task] = {}

    def _warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    # ---------------------------------------------------------------- flights

    async def _fetch_flights(self, q: Query) -> tuple[list[tuple[Hub, Flight]], list[tuple[Hub, Flight]]]:
        codes = list(self.hubs)
        self.progress(f"Searching flights {len(codes)} airports ⇄ {q.destination} …")
        opts = dict(adults=q.adults, max_stops=q.max_stops, cabin_bag=q.cabin_bag, checked_bag=q.checked_bag)
        out_calls = [self.flights.search(c, q.destination, q.out_date, q.flex, **opts) for c in codes]
        back_calls = [self.flights.search(q.destination, c, q.back_date, q.flex, **opts) for c in codes]
        results = await asyncio.gather(*out_calls, *back_calls, return_exceptions=True)
        out_res, back_res = results[: len(codes)], results[len(codes):]

        def collect(batches, attr: str, window: set[date], label: str) -> list[tuple[Hub, Flight]]:
            best: dict[tuple, tuple[Hub, Flight]] = {}
            for code, res in zip(codes, batches):
                if isinstance(res, BaseException):
                    self._warn(f"Flight search {label} {code} failed: {res}")
                    continue
                for f in res:
                    hub = self.hubs.get(getattr(f, attr))  # Kiwi may answer "VCE" with TSF flights, etc.
                    if hub is None or f.departure.date() not in window:
                        continue
                    if f.key not in best or f.price < best[f.key][1].price:
                        best[f.key] = (hub, f)
            return sorted(best.values(), key=lambda hf: hf[1].price)

        out = collect(out_res, "origin", q.window(q.out_date), "from")
        back = collect(back_res, "destination", q.window(q.back_date), "to")
        return out, back

    # ---------------------------------------------------------------- buses

    async def _rides(self, from_city: str, to_city: str, day: date, adults: int) -> list[BusRide]:
        try:
            return await self.buses.search(from_city, to_city, day, adults)
        except Exception as exc:  # one broken stop shouldn't sink the whole search
            self._warn(f"Bus search {from_city} → {to_city} on {day} failed: {exc}")
            return []

    async def _evaluate(self, direction: str, hub: Hub, f: Flight, adults: int) -> Leg | None:
        """Cheapest way to connect home with this flight (or None if no bus fits)."""
        s = self.settings
        options: list[Leg] = []
        if hub.home_transfer:
            options.append(Leg(direction, hub, f, None, None, round(hub.home_transfer.cost * adults, 2),
                               hub.home_transfer.minutes, timedelta(0), False))
        max_wait = timedelta(hours=s.max_wait_hours)
        for stop in hub.stops:
            transfer = timedelta(minutes=stop.transfer_min)
            if direction == "out":
                dep = f.departure
                latest_ready = dep - timedelta(minutes=s.checkin_buffer_min)
                days = [dep.date(), dep.date() - timedelta(days=1)]
                rides = await asyncio.gather(*(self._rides(s.home, stop.flixbus, d, adults) for d in days))
                for r in (r for batch in rides for r in batch):
                    ready = r.arrival + transfer
                    if ready > latest_ready or dep - ready > max_wait:
                        continue
                    options.append(self._leg(direction, hub, f, r, stop, adults, dep - ready, spans_night(ready, dep)))
            else:
                ready = f.arrival + timedelta(minutes=s.arrival_buffer_min) + transfer
                days = [f.arrival.date(), f.arrival.date() + timedelta(days=1)]
                rides = await asyncio.gather(*(self._rides(stop.flixbus, s.home, d, adults) for d in days))
                for r in (r for batch in rides for r in batch):
                    if r.departure < ready or r.departure - f.arrival > max_wait:
                        continue
                    options.append(self._leg(direction, hub, f, r, stop, adults, r.departure - f.arrival,
                                             spans_night(f.arrival, r.departure)))
        if not options:
            return None
        return min(options, key=lambda leg: (leg.score, leg.total, leg.wait))

    def _leg(self, direction, hub, f, ride, stop, adults, wait, overnight) -> Leg:
        s = self.settings
        extra_hours = max(0.0, wait.total_seconds() / 3600 - s.free_wait_hours)
        return Leg(direction, hub, f, ride, stop,
                   ground_cost=round(ride.price + stop.transfer_cost * adults, 2),
                   ground_minutes=stop.transfer_min, wait=wait, overnight=overnight,
                   overnight_cost=round(s.overnight_cost * adults, 2) if overnight else 0.0,
                   penalty=round(extra_hours * s.wait_cost_per_hour, 2))

    async def _leg_for(self, direction: str, hub: Hub, f: Flight, adults: int) -> Leg | None:
        key = (direction, f.key)
        if key not in self._memo:
            self._memo[key] = asyncio.ensure_future(self._evaluate(direction, hub, f, adults))
        return await self._memo[key]

    async def _legs(self, direction: str, flights: list[tuple[Hub, Flight]], adults: int) -> list[Leg]:
        """Evaluate just enough flights: the cheapest few per date, plus each hub's cheapest feasible one."""
        by_date: dict[date, list[tuple[Hub, Flight]]] = defaultdict(list)
        by_hub: dict[str, list[tuple[Hub, Flight]]] = defaultdict(list)
        for hub, f in flights:  # already sorted by price
            by_date[f.departure.date()].append((hub, f))
            by_hub[hub.code].append((hub, f))

        async def per_date(cands: list[tuple[Hub, Flight]]) -> None:
            kept: list[Leg] = []
            for i in range(0, len(cands), BATCH):
                if len(kept) >= KEEP_PER_DATE and cands[i][1].price >= kept[KEEP_PER_DATE - 1].score:
                    break  # buses and penalties are ≥ 0, so no better leg can follow
                legs = await asyncio.gather(*(self._leg_for(direction, h, f, adults) for h, f in cands[i:i + BATCH]))
                kept = sorted(kept + [leg for leg in legs if leg], key=lambda leg: leg.score)

        async def per_hub(cands: list[tuple[Hub, Flight]]) -> None:
            cands = cands[:PER_HUB_TRIES]
            for i in range(0, len(cands), 3):
                legs = await asyncio.gather(*(self._leg_for(direction, h, f, adults) for h, f in cands[i:i + 3]))
                if any(legs):
                    return

        await asyncio.gather(*(per_date(c) for c in by_date.values()), *(per_hub(c) for c in by_hub.values()))
        legs = [t.result() for (d, _), t in self._memo.items()
                if d == direction and t.done() and not t.cancelled() and t.exception() is None and t.result()]
        return sorted(legs, key=lambda leg: (leg.score, leg.total))

    # ---------------------------------------------------------------- combine

    async def run(self, q: Query) -> PlanResult:
        out_flights, back_flights = await self._fetch_flights(q)
        if not out_flights:
            self._warn(f"No outbound flights found to {q.destination} around {q.out_date}.")
        if not back_flights:
            self._warn(f"No return flights found from {q.destination} around {q.back_date}.")
        self.progress(f"Matching buses to {len(out_flights)} outbound and {len(back_flights)} return flights …")
        out_legs, back_legs = await asyncio.gather(
            self._legs("out", out_flights, q.adults), self._legs("back", back_flights, q.adults))
        return combine(q, out_legs, back_legs, self.warnings)


def combine(q: Query, out_legs: list[Leg], back_legs: list[Leg], warnings: list[str] | None = None) -> PlanResult:
    trips: list[Trip] = []
    for o in out_legs:
        for b in back_legs:
            if b.flight.departure <= o.flight.arrival:
                continue
            trip = Trip(o, b)
            if q.min_nights is not None and trip.nights < q.min_nights:
                continue
            trips.append(trip)
    trips.sort(key=lambda t: (t.score, t.total, t.out.wait + t.back.wait))

    matrix: dict[tuple[date, date], float] = {}
    best_combo: dict[tuple, Trip] = {}
    for t in trips:
        cell = (t.out.flight_date, t.back.flight_date)
        matrix[cell] = min(matrix.get(cell, t.total), t.total)
        best_combo.setdefault((t.out.hub.code, t.back.hub.code, *cell), t)
    ranked = sorted(best_combo.values(), key=lambda t: (t.score, t.total, t.out.wait + t.back.wait))[: q.top]

    def best_by_hub(legs: list[Leg]) -> dict[str, Leg]:
        best: dict[str, Leg] = {}
        for leg in legs:
            if leg.hub.code not in best or leg.score < best[leg.hub.code].score:
                best[leg.hub.code] = leg
        return dict(sorted(best.items(), key=lambda kv: kv[1].score))

    baseline = next((t for t in trips if t.out.hub.home_transfer and t.back.hub.home_transfer), None)
    return PlanResult(
        destination=q.destination,
        trips=ranked,
        out_legs=out_legs,
        back_legs=back_legs,
        date_matrix=matrix,
        best_out_by_hub=best_by_hub(out_legs),
        best_back_by_hub=best_by_hub(back_legs),
        baseline=baseline,
        warnings=list(warnings or []),
    )
