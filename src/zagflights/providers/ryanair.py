"""Ryanair's own fares: the cheapest flight per day on a route, straight from ryanair.com's API."""

from __future__ import annotations

from datetime import date, datetime

from ..models import Flight
from .base import HttpClient

FARES_URL = "https://services-api.ryanair.com/farfnd/v4/oneWayFares/{o}/{d}/cheapestPerDay"
ROUTES_URL = "https://www.ryanair.com/api/views/locate/searchWidget/routes/en/airport/{iata}"
FARE_TTL = 60 * 60
ROUTES_TTL = 7 * 24 * 60 * 60

RYANAIR_CODES = ("FR", "RK", "AL", "OE", "RR")  # Ryanair, Ryanair UK, Malta Air, Lauda, Buzz


def booking_url(o: str, d: str, day: date, adults: int) -> str:
    return (f"https://www.ryanair.com/gb/en/trip/flights/select?adults={adults}&teens=0&children=0&infants=0"
            f"&dateOut={day:%Y-%m-%d}&isReturn=false&originIata={o}&destinationIata={d}")


def parse_cheapest_per_day(data: dict, o: str, d: str, adults: int) -> list[Flight]:
    flights = []
    for fare in (data.get("outbound") or {}).get("fares", []):
        price = (fare.get("price") or {}).get("value")
        if fare.get("unavailable") or fare.get("soldOut") or price is None or not fare.get("departureDate"):
            continue
        dep = datetime.fromisoformat(fare["departureDate"])
        arr = datetime.fromisoformat(fare["arrivalDate"]) if fare.get("arrivalDate") else dep
        flights.append(Flight(
            id=f"FR-{o}-{d}-{dep:%Y%m%d%H%M}", origin=o, destination=d, origin_name=o, destination_name=d,
            departure=dep, arrival=arr, price=round(float(price) * adults, 2), carriers=("Ryanair",),
            flight_numbers=(), stops=0, booking_url=booking_url(o, d, dep.date(), adults), route=(o, d),
            carrier_codes=("FR",), source="ryanair",
        ))
    return flights


class RyanairClient(HttpClient):
    name = "Ryanair"

    async def routes(self, iata: str) -> set[str]:
        async def fetch():
            resp = await self.request("GET", ROUTES_URL.format(iata=iata))
            return sorted({r["arrivalAirport"]["code"] for r in resp.json() if r.get("arrivalAirport")})

        try:
            return set(await self.cached("ryanair-routes", iata, ROUTES_TTL, fetch))
        except Exception:
            return set()  # unknown airport for Ryanair → no direct fares

    async def cheapest_per_day(self, o: str, d: str, days: set[date], adults: int = 1) -> list[Flight]:
        """Cheapest Ryanair flight o → d on each of ``days`` (if it flies that day)."""
        flights: list[Flight] = []
        for month in sorted({day.replace(day=1) for day in days}):
            async def fetch(month=month):
                resp = await self.request("GET", FARES_URL.format(o=o, d=d),
                                          params={"outboundMonthOfDate": month.isoformat(), "currency": "EUR"})
                return resp.json()

            data = await self.cached("ryanair-fares", (o, d, month.isoformat()), FARE_TTL, fetch)
            flights += [f for f in parse_cheapest_per_day(data, o, d, adults) if f.departure.date() in days]
        return flights
