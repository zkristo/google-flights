"""Live bus fares from FlixBus's public web API (the one flixbus.com itself uses)."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from typing import Any

import httpx

from ..cache import Cache
from ..models import BusRide
from .kiwi import ProviderError

API = "https://global.api.flixbus.com"
SHOP = "https://shop.flixbus.com/search"
FARE_TTL = 60 * 60
CITY_TTL = 30 * 24 * 60 * 60
HEADERS = {"User-Agent": "Mozilla/5.0 (zagflights)", "Accept": "application/json"}


def pick_city(results: list[dict], name: str) -> dict | None:
    """Choose the autocomplete hit whose name matches exactly, else the best FlixBus city."""
    wanted = name.strip().casefold()
    exact = [c for c in results if c.get("name", "").casefold() == wanted]
    pool = exact or [c for c in results if c.get("is_flixbus_city")] or results
    return pool[0] if pool else None


def parse_rides(data: dict, from_city: str, to_city: str, from_id: str, to_id: str, day: date, adults: int) -> list[BusRide]:
    stations = data.get("stations") or {}
    link = f"{SHOP}?departureCity={from_id}&arrivalCity={to_id}&rideDate={day:%d.%m.%Y}&adult={adults}&_locale=en"
    rides: list[BusRide] = []
    for trip in data.get("trips", []):
        for uid, r in (trip.get("results") or {}).items():
            if r.get("status") != "available":
                continue
            price = r.get("price") or {}
            total = price.get("total_with_platform_fee") or price.get("total")
            if total is None:
                continue
            dep, arr = r["departure"], r["arrival"]
            rides.append(
                BusRide(
                    uid=uid,
                    from_city=from_city,
                    to_city=to_city,
                    from_station=(stations.get(dep.get("station_id")) or {}).get("name", from_city),
                    to_station=(stations.get(arr.get("station_id")) or {}).get("name", to_city),
                    departure=datetime.fromisoformat(dep["date"]).replace(tzinfo=None),
                    arrival=datetime.fromisoformat(arr["date"]).replace(tzinfo=None),
                    price=round(float(total), 2),
                    transfers=max(len(r.get("legs") or [1]) - 1, 0),
                    booking_url=link,
                )
            )
    return sorted(rides, key=lambda b: (b.departure, b.price))


class FlixBusClient:
    def __init__(self, cache: Cache | None = None, http: httpx.AsyncClient | None = None, concurrency: int = 6):
        self.cache = cache or Cache(enabled=False)
        self._http = http
        self._own_http = http is None
        self._sem = asyncio.Semaphore(concurrency)
        self._inflight: dict[Any, asyncio.Task] = {}
        self.calls = 0

    async def __aenter__(self) -> "FlixBusClient":
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), headers=HEADERS)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        assert self._http is not None, "use `async with FlixBusClient()`"
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with self._sem:
                    self.calls += 1
                    resp = await self._http.get(API + path, params=params, headers=HEADERS)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise ProviderError(f"FlixBus HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, ProviderError) as exc:
                last_exc = exc
                await asyncio.sleep(1.0 * (attempt + 1))
            except httpx.HTTPStatusError as exc:
                raise ProviderError(f"FlixBus {path} → HTTP {exc.response.status_code}") from exc
        raise ProviderError(f"FlixBus request failed: {last_exc}")

    async def _dedup(self, key: Any, factory):
        """Share one in-flight request between concurrent callers asking for the same thing."""
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(factory())
            self._inflight[key] = task
        return await task

    async def city_id(self, name: str) -> str:
        cached = self.cache.get("flix-city", name.casefold(), CITY_TTL)
        if cached:
            return cached

        async def fetch() -> str:
            results = await self._get_json("/search/autocomplete/cities", {"q": name, "lang": "en"})
            city = pick_city(results or [], name)
            if not city:
                raise ProviderError(f"FlixBus has no city called {name!r}")
            self.cache.set("flix-city", name.casefold(), city["id"])
            return city["id"]

        return await self._dedup(("city", name.casefold()), fetch)

    async def search(self, from_city: str, to_city: str, day: date, adults: int = 1) -> list[BusRide]:
        """All available rides from_city → to_city departing on ``day`` (local), cheapest data from FlixBus."""

        async def fetch() -> list[BusRide]:
            from_id, to_id = await asyncio.gather(self.city_id(from_city), self.city_id(to_city))
            params = {
                "from_city_id": from_id,
                "to_city_id": to_id,
                "departure_date": day.strftime("%d.%m.%Y"),
                "products": json.dumps({"adult": adults}, separators=(",", ":")),
                "currency": "EUR",
                "locale": "en",
                "search_by": "cities",
                "include_after_midnight_rides": 1,
            }
            data = self.cache.get("flix", params, FARE_TTL)
            if data is None:
                data = await self._get_json("/search/service/v4/search", params)
                self.cache.set("flix", params, data)
            return parse_rides(data, from_city, to_city, from_id, to_id, day, adults)

        return await self._dedup(("rides", from_city.casefold(), to_city.casefold(), day, adults), fetch)
