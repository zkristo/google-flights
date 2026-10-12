from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from zagflights.config import Config, HomeTransfer, Hub, Settings, Stop
from zagflights.models import BusRide, Flight

FIXTURES = Path(__file__).parent / "fixtures"


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def flight(origin: str, dest: str, dep: str, arr: str, price: float, num: str = "", codes: tuple = ("TA",),
           source: str = "kiwi") -> Flight:
    return Flight(
        id=f"{origin}{dest}{dep}", origin=origin, destination=dest, origin_name=origin, destination_name=dest,
        departure=dt(dep), arrival=dt(arr), price=price, carriers=("Test Air",),
        flight_numbers=(num or f"TA{origin}{dep[-5:]}",), stops=0, booking_url=f"https://kiwi.test/{origin}{dest}",
        route=(origin, dest), carrier_codes=codes, source=source,
    )


def bus(frm: str, to: str, dep: str, arr: str, price: float) -> BusRide:
    return BusRide(uid=f"{frm}-{to}-{dep}", from_city=frm, to_city=to, from_station=frm, to_station=to,
                   departure=dt(dep), arrival=dt(arr), price=price, transfers=0, booking_url="https://flix.test")


class FakeFlights:
    """Answers search(fly_from, fly_to, ...) from a list of flights, like Kiwi would."""

    def __init__(self, flights: list[Flight], alias: dict[str, list[str]] | None = None):
        self.flights = flights
        self.alias = alias or {}  # e.g. {"VCE": ["VCE", "TSF"]} to mimic Kiwi's city handling
        self.calls: list[tuple] = []

    async def search(self, fly_from, fly_to, day, flex_days=0, adults=1, max_stops=None, cabin_bag=False,
                     checked_bag=False, price_to=None, one_for_city=False):
        self.calls.append((fly_from, fly_to, day, flex_days))

        def match(code, place):  # "Europe" behaves like Kiwi's region search
            return place == "Europe" or code in self.alias.get(place, [place])

        lo, hi = day.toordinal() - flex_days, day.toordinal() + flex_days
        found = sorted((f for f in self.flights if match(f.origin, fly_from) and match(f.destination, fly_to)
                        and lo <= f.departure.date().toordinal() <= hi
                        and (max_stops is None or f.stops <= max_stops)
                        and (price_to is None or f.price <= price_to)), key=lambda f: f.price)
        if one_for_city:
            seen, unique = set(), []
            for f in found:
                if f.destination not in seen:
                    seen.add(f.destination)
                    unique.append(f)
            found = unique
        return found


class FakeBuses:
    def __init__(self, rides: list[BusRide], fail: set[str] | None = None):
        self.rides = rides
        self.fail = fail or set()
        self.calls: list[tuple] = []

    async def search(self, from_city, to_city, day: date, adults=1):
        self.calls.append((from_city, to_city, day))
        if from_city in self.fail or to_city in self.fail:
            raise RuntimeError("boom")
        return [r for r in self.rides if r.from_city == from_city and r.to_city == to_city and r.departure.date() == day]


@pytest.fixture
def config() -> Config:
    hubs = {
        "ZAG": Hub("ZAG", "Zagreb", home_transfer=HomeTransfer(45, 8)),
        "BUD": Hub("BUD", "Budapest", stops=(Stop("Budapest Airport", 5, 0), Stop("Budapest", 60, 6))),
        "VCE": Hub("VCE", "Venice", stops=(Stop("Venice Airport", 5, 0),)),
        "TSF": Hub("TSF", "Treviso", stops=(Stop("Treviso Airport", 5, 0),)),
    }
    return Config(settings=Settings(home="Zagreb", checkin_buffer_min=120, arrival_buffer_min=45,
                                    max_wait_hours=20, overnight_cost=0), hubs=hubs)
