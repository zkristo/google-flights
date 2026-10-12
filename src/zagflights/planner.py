"""Combine buses and flights into the cheapest door-to-door trips from home and back.

Outbound and return are planned independently, since each direction may use a different airport
(e.g. bus to Budapest, fly BUD→ATH, fly ATH→VCE, bus Venice→Zagreb). They are then paired up and ranked.

A direction can reach its departure airport in three ways:
  * the home airport (ZAG) with a local transfer,
  * a FlixBus to a nearby "hub" airport (BUD, VCE, SPU, …),
  * a cheap separate "hop" flight from ZAG to any airport (e.g. ZAG→BGY €19, then BGY→ATH).
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
HOP_CANDIDATES = 8  # cheapest "fly first" airports explored per direction
REGION = "Europe"  # where to look for cheap hop flights


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
                     max_stops: int | None = None, cabin_bag: bool = False, checked_bag: bool = False,
                     price_to: int | None = None, one_for_city: bool = False) -> list[Flight]: ...


class BusSource(Protocol):
    async def search(self, from_city: str, to_city: str, day: date, adults: int = 1) -> list[BusRide]: ...


class DirectSource(Protocol):
    async def improve(self, flights: list[Flight], pairs: set[tuple[str, str]], days: set[date], adults: int) -> list[Flight]: ...


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

    @property
    def bags(self) -> bool:
        return self.cabin_bag or self.checked_bag

    @property
    def destination_airport(self) -> str | None:
        d = self.destination.strip()
        return d.upper() if len(d) == 3 and d.isalpha() else None


class Planner:
    def __init__(self, config: Config, hubs: list[Hub], flights: FlightSource, buses: BusSource,
                 direct: DirectSource | None = None, progress: Callable[[str], None] | None = None):
        self.config = config
        self.settings = config.settings
        self.hubs = {h.code: h for h in hubs}
        self.flights = flights
        self.buses = buses
        self.direct = direct
        self.progress = progress or (lambda _msg: None)
        self.warnings: list[str] = []
        self._memo: dict[tuple, asyncio.Task] = {}

    def _warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def _use_direct(self, q: Query) -> bool:
        if not self.direct:
            return False
        if q.bags:
            self._warn("Ryanair/Wizz direct prices skipped because bags were requested (their base fares exclude bags); "
                       "prices are Kiwi's, bags included.")
            return False
        return True

    # ---------------------------------------------------------------- flights

    async def _search(self, label: str, *args, **kwargs) -> list[Flight]:
        try:
            return await self.flights.search(*args, **kwargs)
        except Exception as exc:
            self._warn(f"Flight search {label} failed: {exc}")
            return []

    async def _main_flights(self, q: Query) -> tuple[list[Flight], list[Flight]]:
        codes = list(self.hubs)
        self.progress(f"Searching flights {len(codes)} airports ⇄ {q.destination} …")
        opts = dict(adults=q.adults, max_stops=q.max_stops, cabin_bag=q.cabin_bag, checked_bag=q.checked_bag)
        results = await asyncio.gather(
            *(self._search(f"{c}→{q.destination}", c, q.destination, q.out_date, q.flex, **opts) for c in codes),
            *(self._search(f"{q.destination}→{c}", q.destination, c, q.back_date, q.flex, **opts) for c in codes))
        out = [f for batch in results[: len(codes)] for f in batch]
        back = [f for batch in results[len(codes):] for f in batch]
        return out, back

    def _collect(self, flights: list[Flight], attr: str, window: set[date]) -> list[tuple[Hub, Flight]]:
        """Attach each flight to the hub it actually uses (Kiwi may answer "VCE" with TSF flights)."""
        best: dict[tuple, tuple[Hub, Flight]] = {}
        for f in flights:
            hub = self.hubs.get(getattr(f, attr))
            if hub is None or f.departure.date() not in window:
                continue
            if f.key not in best or f.price < best[f.key][1].price:
                best[f.key] = (hub, f)
        return sorted(best.values(), key=lambda hf: hf[1].price)

    # ---------------------------------------------------------------- legs

    def _make_leg(self, direction: str, hub: Hub, f: Flight, adults: int, *, ground_cost: float, ground_minutes: int,
                  wait: timedelta, overnight: bool, bus: BusRide | None = None, stop=None, hop: Flight | None = None) -> Leg:
        s = self.settings
        extra_hours = max(0.0, wait.total_seconds() / 3600 - s.free_wait_hours)
        return Leg(direction, hub, f, bus, stop,
                   ground_cost=round(ground_cost, 2), ground_minutes=ground_minutes, wait=wait, overnight=overnight,
                   overnight_cost=round(s.overnight_cost * adults, 2) if overnight else 0.0,
                   penalty=round(extra_hours * s.wait_cost_per_hour, 2), hop=hop)

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
            options.append(self._make_leg(direction, hub, f, adults, ground_cost=hub.home_transfer.cost * adults,
                                          ground_minutes=hub.home_transfer.minutes, wait=timedelta(0), overnight=False))
        max_wait = timedelta(hours=s.max_wait_hours)
        for stop in hub.stops:
            transfer = timedelta(minutes=stop.transfer_min)
            ground = dict(ground_minutes=stop.transfer_min, stop=stop)
            if direction == "out":
                dep = f.departure
                latest_ready = dep - timedelta(minutes=s.checkin_buffer_min)
                days = [dep.date(), dep.date() - timedelta(days=1)]
                rides = await asyncio.gather(*(self._rides(s.home, stop.flixbus, d, adults) for d in days))
                for r in (r for batch in rides for r in batch):
                    ready = r.arrival + transfer
                    if ready > latest_ready or dep - ready > max_wait:
                        continue
                    options.append(self._make_leg(direction, hub, f, adults, ground_cost=r.price + stop.transfer_cost * adults,
                                                  wait=dep - ready, overnight=spans_night(ready, dep), bus=r, **ground))
            else:
                ready = f.arrival + timedelta(minutes=s.arrival_buffer_min) + transfer
                days = [f.arrival.date(), f.arrival.date() + timedelta(days=1)]
                rides = await asyncio.gather(*(self._rides(stop.flixbus, s.home, d, adults) for d in days))
                for r in (r for batch in rides for r in batch):
                    if r.departure < ready or r.departure - f.arrival > max_wait:
                        continue
                    options.append(self._make_leg(direction, hub, f, adults, ground_cost=r.price + stop.transfer_cost * adults,
                                                  wait=r.departure - f.arrival, overnight=spans_night(f.arrival, r.departure),
                                                  bus=r, **ground))
        if not options:
            return None
        return min(options, key=lambda leg: (leg.score, leg.total, leg.wait))

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

    # ---------------------------------------------------------------- hop flights

    async def _hop_legs(self, direction: str, q: Query, dest_airports: set[str], use_direct: bool) -> list[Leg]:
        """Legs that start with a cheap separate flight from home (or end with one back home)."""
        s, home = self.settings, self.config.home_hub
        if s.hop_max_price <= 0 or home is None:
            return []
        cap = s.hop_max_price * q.adults
        center = q.out_date if direction == "out" else q.back_date
        bags = dict(cabin_bag=q.cabin_bag, checked_bag=q.checked_bag)
        if direction == "out":
            cands = await self._search(f"{home.code}→{REGION} under €{cap:g}", home.code, REGION, center, q.flex,
                                       adults=q.adults, max_stops=0, price_to=int(cap), one_for_city=True, **bags)
            airports = [f.destination for f in cands]
        else:
            cands = await self._search(f"{REGION}→{home.code} under €{cap:g}", REGION, home.code, center, q.flex,
                                       adults=q.adults, max_stops=0, price_to=int(cap), **bags)
            airports = [f.origin for f in cands]
        skip = dest_airports | {home.code}
        xs = list(dict.fromkeys(a for a in airports if a not in skip))[:HOP_CANDIDATES]
        if xs:
            self.progress(f"Checking cheap first hops via {', '.join(xs)} …")
        legs = await asyncio.gather(*(self._hop_legs_via(direction, x, q, home, cap, dest_airports, use_direct) for x in xs))
        return [leg for batch in legs for leg in batch]

    async def _hop_legs_via(self, direction: str, x: str, q: Query, home: Hub, cap: float,
                            dest_airports: set[str], use_direct: bool) -> list[Leg]:
        s = self.settings
        center = q.out_date if direction == "out" else q.back_date
        window = q.window(center)
        opts = dict(adults=q.adults, cabin_bag=q.cabin_bag, checked_bag=q.checked_bag)
        if direction == "out":
            hops, onward = await asyncio.gather(
                self._search(f"{home.code}→{x}", home.code, x, center, q.flex, max_stops=0, **opts),
                self._search(f"{x}→{q.destination}", x, q.destination, center, q.flex, max_stops=q.max_stops, **opts))
            hop_pairs, onward_pairs = {(home.code, x)}, {(x, d) for d in dest_airports}
        else:
            onward, hops = await asyncio.gather(
                self._search(f"{q.destination}→{x}", q.destination, x, center, q.flex, max_stops=q.max_stops, **opts),
                self._search(f"{x}→{home.code}", x, home.code, center, q.flex, max_stops=0, **opts))
            hop_pairs, onward_pairs = {(x, home.code)}, {(d, x) for d in dest_airports}
        if use_direct and self.direct:
            hop_days = window | {min(window) - timedelta(days=1), max(window) + timedelta(days=1)}
            hops, onward = await asyncio.gather(self.direct.improve(hops, hop_pairs, hop_days, q.adults),
                                                self.direct.improve(onward, onward_pairs, window, q.adults))
        if direction == "out":
            hops = [h for h in hops if h.price <= cap and h.origin == home.code and h.destination == x]
            onward = [f for f in onward if f.origin == x and f.departure.date() in window]
        else:
            hops = [h for h in hops if h.price <= cap and h.origin == x and h.destination == home.code]
            onward = [f for f in onward if f.destination == x and f.departure.date() in window]
        if not hops or not onward:
            return []

        min_gap, max_gap = timedelta(minutes=s.self_transfer_min), timedelta(hours=s.max_wait_hours)
        names = [h.destination_name if direction == "out" else h.origin_name for h in hops]
        hub = Hub(code=x, name=next((n for n in names if n != x), x), via_flight=True)
        ht = home.home_transfer
        legs = []
        for f in onward:
            options = []
            for h in hops:
                start, end = (h.arrival, f.departure) if direction == "out" else (f.arrival, h.departure)
                gap = end - start
                if gap < min_gap or gap > max_gap:
                    continue
                options.append(self._make_leg(direction, hub, f, q.adults, ground_cost=ht.cost * q.adults,
                                              ground_minutes=ht.minutes, wait=gap, overnight=spans_night(start, end), hop=h))
            if options:
                legs.append(min(options, key=lambda leg: (leg.score, leg.total, leg.wait)))
        return legs

    # ---------------------------------------------------------------- run

    async def run(self, q: Query) -> PlanResult:
        out_raw, back_raw = await self._main_flights(q)
        dest_airports = {f.destination for f in out_raw} | {f.origin for f in back_raw}
        if q.destination_airport:
            dest_airports.add(q.destination_airport)
        use_direct = self._use_direct(q)
        if use_direct:
            self.progress("Checking Ryanair and Wizz Air prices directly …")
            out_pairs = {(f.origin, f.destination) for f in out_raw if f.stops == 0} | {(h, d) for h in self.hubs for d in dest_airports}
            back_pairs = {(f.origin, f.destination) for f in back_raw if f.stops == 0} | {(d, h) for h in self.hubs for d in dest_airports}
            out_raw, back_raw = await asyncio.gather(
                self.direct.improve(out_raw, out_pairs, q.window(q.out_date), q.adults),
                self.direct.improve(back_raw, back_pairs, q.window(q.back_date), q.adults))
        out_flights = self._collect(out_raw, "origin", q.window(q.out_date))
        back_flights = self._collect(back_raw, "destination", q.window(q.back_date))
        if not out_flights:
            self._warn(f"No outbound flights found from the nearby airports to {q.destination} around {q.out_date}.")
        if not back_flights:
            self._warn(f"No return flights found from {q.destination} to the nearby airports around {q.back_date}.")
        self.progress(f"Matching buses to {len(out_flights)} outbound and {len(back_flights)} return flights …")
        out_legs, back_legs, hop_out, hop_back = await asyncio.gather(
            self._legs("out", out_flights, q.adults), self._legs("back", back_flights, q.adults),
            self._hop_legs("out", q, dest_airports, use_direct), self._hop_legs("back", q, dest_airports, use_direct))
        for msg in getattr(self.direct, "summary", lambda: [])():
            self._warn(msg)
        return combine(q, out_legs + hop_out, back_legs + hop_back, self.warnings)


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
        best_combo.setdefault((t.out.hub.label, t.back.hub.label, *cell), t)
    ranked = sorted(best_combo.values(), key=lambda t: (t.score, t.total, t.out.wait + t.back.wait))[: q.top]

    def best_by_hub(legs: list[Leg]) -> dict[str, Leg]:
        best: dict[str, Leg] = {}
        for leg in legs:
            if leg.hub.label not in best or leg.score < best[leg.hub.label].score:
                best[leg.hub.label] = leg
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
