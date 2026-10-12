"""Reprice Kiwi results with Ryanair's and Wizz Air's own fares (usually a few € cheaper),
and add the airlines' cheapest-of-the-day flights Kiwi didn't return."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, timedelta

from .models import Flight
from .providers.ryanair import RYANAIR_CODES
from .providers.wizz import WIZZ_CODES


def family(codes: tuple[str, ...]) -> str | None:
    if codes and all(c in RYANAIR_CODES for c in codes):
        return "ryanair"
    if codes and all(c in WIZZ_CODES for c in codes):
        return "wizz"
    return None


def merge(kiwi: list[Flight], direct: list[Flight]) -> list[Flight]:
    """Combine Kiwi flights with direct airline fares.

    A non-stop Kiwi flight on the same route, at the same minute, by the same airline group is the
    same flight: keep Kiwi's details (exact arrival, flight number) but take the cheaper price and
    the airline's booking link. Direct fares with no Kiwi twin are added as-is.
    """
    by_slot: dict[tuple, int] = {}
    out = list(kiwi)
    for i, f in enumerate(out):
        fam = family(f.carrier_codes)
        if f.stops == 0 and fam:
            by_slot.setdefault((f.origin, f.destination, f.departure, fam), i)
    for d in direct:
        i = by_slot.get((d.origin, d.destination, d.departure, d.source))
        if i is None:
            out.append(d)
            by_slot[(d.origin, d.destination, d.departure, d.source)] = len(out) - 1
        elif d.price < out[i].price:
            out[i] = replace(out[i], price=d.price, booking_url=d.booking_url, source=d.source)
    return sorted(out, key=lambda f: f.price)


class DirectFares:
    """Looks up Ryanair/Wizz fares for airport pairs, only where the airline flies the route."""

    def __init__(self, ryanair=None, wizz=None):
        self.ryanair = ryanair
        self.wizz = wizz
        self.failures: dict[str, set[str]] = {}  # airline → routes whose direct fares couldn't be fetched

    def _failed(self, airline: str, o: str, d: str) -> list[Flight]:
        self.failures.setdefault(airline, set()).add(f"{o}→{d}")
        return []

    def summary(self) -> list[str]:
        return [f"{airline} direct prices unavailable for {len(routes)} route(s) ({', '.join(sorted(routes)[:5])}"
                f"{', …' if len(routes) > 5 else ''}); Kiwi prices used there."
                for airline, routes in sorted(self.failures.items())]

    async def _ryanair(self, o: str, d: str, days: set[date], adults: int) -> list[Flight]:
        if not self.ryanair or d not in await self.ryanair.routes(o):
            return []
        try:
            return await self.ryanair.cheapest_per_day(o, d, days, adults)
        except Exception:
            return self._failed("Ryanair", o, d)

    async def _wizz(self, o: str, d: str, days: set[date], adults: int, duration: timedelta | None) -> list[Flight]:
        if not self.wizz or not await self.wizz.flies(o, d):
            return []
        try:
            return await self.wizz.fares(o, d, days, adults, known_duration=duration)
        except Exception:
            return self._failed("Wizz Air", o, d)

    async def improve(self, flights: list[Flight], pairs: set[tuple[str, str]], days: set[date], adults: int) -> list[Flight]:
        """Return ``flights`` merged with direct fares for every (origin, destination) in ``pairs``."""
        durations: dict[tuple[str, str], timedelta] = {}
        for f in flights:
            if f.stops == 0 and not f.approx_arrival:
                durations.setdefault((f.origin, f.destination), f.arrival - f.departure)
        tasks = []
        for o, d in sorted(pairs):
            if len(o) != 3 or len(d) != 3 or o == d:
                continue
            tasks.append(self._ryanair(o, d, days, adults))
            tasks.append(self._wizz(o, d, days, adults, durations.get((o, d))))
        results = await asyncio.gather(*tasks)
        direct = [f for batch in results for f in batch]
        return merge(flights, direct)
