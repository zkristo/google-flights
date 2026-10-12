"""Wizz Air's own fares from the timetable API that wizzair.com's fare calendar uses.

The timetable gives the cheapest fare per day and the departure times, priced in the departure
airport's currency and without arrival times. We convert with ECB rates and take the flight
duration from Kiwi when it knows the route, else estimate it from the great-circle distance.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import date, datetime, timedelta

from ..models import Flight
from .base import HttpClient
from .fx import FxRates
from .kiwi import ProviderError

HOME = "https://wizzair.com/en-gb"
API = "https://be.wizzair.com/{ver}/Api"
VERSION_TTL = 24 * 60 * 60
MAP_TTL = 7 * 24 * 60 * 60
FARE_TTL = 60 * 60
HEADERS = {"Origin": "https://wizzair.com", "Referer": "https://wizzair.com/", "Accept": "application/json"}

WIZZ_CODES = ("W6", "W4", "W9", "5W")  # Wizz Air Hungary, Malta, UK, Abu Dhabi


def parse_version(html: str) -> str | None:
    m = re.search(r"be\.wizzair\.com/(\d+\.\d+\.\d+)", html)
    return m.group(1) if m else None


def booking_url(o: str, d: str, day: date, adults: int) -> str:
    return f"https://wizzair.com/en-gb/booking/select-flight/{o}/{d}/{day:%Y-%m-%d}/null/{adults}/0/0/null"


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def estimate_duration(km: float) -> timedelta:
    """Rough block time: taxi/climb overhead + cruise. Good to ~±20 min on European routes."""
    return timedelta(minutes=35 + km / 780 * 60)


def parse_timetable(data: dict) -> list[tuple[datetime, float, str]]:
    """(departure, price, currency) for every departure in a timetable response."""
    out = []
    for f in data.get("outboundFlights") or []:
        price = f.get("price") or {}
        if price.get("amount") is None or f.get("priceType") == "soldOut":
            continue
        for dep in f.get("departureDates") or []:
            out.append((datetime.fromisoformat(dep), float(price["amount"]), price.get("currencyCode") or "EUR"))
    return out


class WizzClient(HttpClient):
    name = "Wizz Air"

    MIN_INTERVAL = 0.4  # seconds between requests, to stay polite

    def __init__(self, fx: FxRates, **kw):
        super().__init__(concurrency=1, **kw)
        self.fx = fx
        self._pace = asyncio.Lock()
        self._last = 0.0

    async def request(self, method: str, url: str, **kwargs):
        """Paced, cookie-less requests, retrying throttling responses (400/429) with a back-off.

        Wizz sets a RequestVerificationToken cookie and then rejects follow-up requests that carry
        it without the matching header ("InvalidProtocol"), so every call starts without cookies.
        """
        for attempt in range(3):
            async with self._pace:
                delay = self._last + self.MIN_INTERVAL - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last = time.monotonic()
                if self._http is not None:
                    self._http.cookies.clear()
            try:
                return await super().request(method, url, **kwargs)
            except ProviderError as exc:
                if attempt == 2 or not any(code in str(exc) for code in ("HTTP 400", "HTTP 429")):
                    raise
                await asyncio.sleep(2.0 * (attempt + 1))

    async def version(self) -> str:
        async def fetch():
            resp = await self.request("GET", HOME, headers={"Accept": "text/html"})
            ver = parse_version(resp.text)
            if not ver:
                raise ProviderError("Wizz Air: could not find the API version on wizzair.com")
            return ver

        return await self.cached("wizz-version", "v", VERSION_TTL, fetch)

    async def stations(self) -> dict[str, dict]:
        """iata → {currency, lat, lon, connections:[iata…]} from Wizz's route map."""
        async def fetch():
            ver = await self.version()
            resp = await self.request("GET", f"{API.format(ver=ver)}/asset/map", params={"languageCode": "en-gb"}, headers=HEADERS)
            return {
                c["iata"]: {
                    "currency": c.get("currencyCode") or "EUR",
                    "coords": [c.get("latitude"), c.get("longitude")],
                    "connections": [x["iata"] for x in c.get("connections", []) if x.get("isDirectFlight", True)],
                    "fake": bool(c.get("isFakeStation")),
                }
                for c in resp.json().get("cities", [])
            }

        return await self.cached("wizz-map", "map", MAP_TTL, fetch)

    async def flies(self, o: str, d: str) -> bool:
        try:
            st = await self.stations()
        except Exception:
            return False
        return d in (st.get(o) or {}).get("connections", []) and not (st.get(d) or {}).get("fake")

    async def fares(self, o: str, d: str, days: set[date], adults: int = 1,
                    known_duration: timedelta | None = None) -> list[Flight]:
        """Cheapest Wizz fare per day o → d on ``days``, converted to EUR."""
        if not days:
            return []
        ver, st = await self.version(), await self.stations()
        lo, hi = min(days), max(days)
        body = {"flightList": [{"departureStation": o, "arrivalStation": d, "from": lo.isoformat(), "to": hi.isoformat()}],
                "priceType": "regular", "adultCount": 1, "childCount": 0, "infantCount": 0}

        async def fetch():
            resp = await self.request("POST", f"{API.format(ver=ver)}/search/timetable", json=body, headers=HEADERS)
            return resp.json()

        data = await self.cached("wizz-fares", (o, d, lo.isoformat(), hi.isoformat()), FARE_TTL, fetch)
        duration, approx = known_duration, False
        if duration is None:
            a, b = (st.get(o) or {}).get("coords"), (st.get(d) or {}).get("coords")
            if not (a and b and None not in a + b):
                return []
            duration, approx = estimate_duration(distance_km(tuple(a), tuple(b))), True

        flights = []
        for dep, amount, currency in parse_timetable(data):
            if dep.date() not in days:
                continue
            eur = await self.fx.to_eur(amount, currency)
            if eur is None:
                continue  # no exchange rate (e.g. RSD) → leave this route to Kiwi
            flights.append(Flight(
                id=f"W6-{o}-{d}-{dep:%Y%m%d%H%M}", origin=o, destination=d, origin_name=o, destination_name=d,
                departure=dep, arrival=dep + duration, price=round(eur * adults, 2), carriers=("Wizz Air",),
                flight_numbers=(), stops=0, booking_url=booking_url(o, d, dep.date(), adults), route=(o, d),
                carrier_codes=("W6",), source="wizz", approx_arrival=approx,
            ))
        return flights
