"""Direct Ryanair / Wizz Air fares, FX conversion and merging with Kiwi results."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta

import httpx
import pytest
import respx
from conftest import FIXTURES, flight

from zagflights.cache import Cache
from zagflights.pricing import DirectFares, family, merge
from zagflights.providers.fx import ECB_URL, FxRates, parse_ecb
from zagflights.providers.ryanair import (FARES_URL, ROUTES_URL, RyanairClient, booking_url,
                                          parse_cheapest_per_day)
from zagflights.providers.wizz import (API, HOME, WizzClient, distance_km, estimate_duration, parse_timetable,
                                       parse_version)

ECB = (FIXTURES / "ecb_daily.xml").read_text()
RYANAIR = json.loads((FIXTURES / "ryanair_bud_ath_nov.json").read_text())
ROUTES = json.loads((FIXTURES / "ryanair_routes_zag.json").read_text())
WIZZ_TT = json.loads((FIXTURES / "wizz_timetable_bud_ath.json").read_text())
WIZZ_MAP = json.loads((FIXTURES / "wizz_map_subset.json").read_text())
NOV = {date(2026, 11, d) for d in range(8, 13)}


# ------------------------------------------------------------------ FX

def test_parse_ecb_rates():
    rates = parse_ecb(ECB)
    assert rates["HUF"] == pytest.approx(365.13) and rates["EUR"] == 1.0 and rates["BAM"] == 1.95583
    assert "RSD" not in rates  # the ECB doesn't publish dinars → Wizz BEG fares fall back to Kiwi


@respx.mock
def test_fx_to_eur():
    respx.get(ECB_URL).mock(return_value=httpx.Response(200, text=ECB))

    async def go():
        async with FxRates() as fx:
            return await fx.to_eur(8669, "HUF"), await fx.to_eur(12.5, "EUR"), await fx.to_eur(1000, "RSD")

    huf, eur, rsd = asyncio.run(go())
    assert huf == pytest.approx(23.74, abs=0.01) and eur == 12.5 and rsd is None


# ------------------------------------------------------------------ Ryanair

def test_parse_ryanair_cheapest_per_day():
    flights = parse_cheapest_per_day(RYANAIR, "BUD", "ATH", adults=1)
    by_day = {f.departure.date(): f for f in flights}
    nov9 = by_day[date(2026, 11, 9)]
    assert nov9.price == 29.99 and nov9.departure == datetime(2026, 11, 9, 6, 30) and nov9.arrival == datetime(2026, 11, 9, 9, 30)
    assert nov9.source == "ryanair" and nov9.carrier_codes == ("FR",) and "originIata=BUD" in nov9.booking_url
    assert date(2026, 11, 10) not in by_day  # "unavailable" days are skipped
    assert parse_cheapest_per_day(RYANAIR, "BUD", "ATH", adults=2)[0].price == round(flights[0].price * 2, 2)


@respx.mock
def test_ryanair_client_routes_and_fares(tmp_path):
    respx.get(ROUTES_URL.format(iata="ZAG")).mock(return_value=httpx.Response(200, json=ROUTES))
    fares = respx.get(FARES_URL.format(o="BUD", d="ATH")).mock(return_value=httpx.Response(200, json=RYANAIR))

    async def go():
        async with RyanairClient(cache=Cache(tmp_path)) as ra:
            routes = await ra.routes("ZAG")
            a = await ra.cheapest_per_day("BUD", "ATH", NOV)
            b = await ra.cheapest_per_day("BUD", "ATH", {date(2026, 11, 9)})
            return routes, a, b

    routes, a, b = asyncio.run(go())
    assert routes == {r["arrivalAirport"]["code"] for r in ROUTES}
    assert {f.departure.date() for f in a} <= NOV and [f.price for f in b] == [29.99]
    assert fares.call_count == 1 and fares.calls[0].request.url.params["outboundMonthOfDate"] == "2026-11-01"


def test_ryanair_booking_url():
    assert booking_url("VCE", "ATH", date(2026, 11, 9), 2) == (
        "https://www.ryanair.com/gb/en/trip/flights/select?adults=2&teens=0&children=0&infants=0"
        "&dateOut=2026-11-09&isReturn=false&originIata=VCE&destinationIata=ATH")


# ------------------------------------------------------------------ Wizz

def test_wizz_parsers():
    assert parse_version('<script src="https://be.wizzair.com/29.20.0/Api"></script>') == "29.20.0"
    assert parse_version("<html>nothing</html>") is None
    rows = parse_timetable(WIZZ_TT)
    assert rows[1] == (datetime(2026, 11, 9, 6, 0), 8669.0, "HUF")
    km = distance_km((47.43694, 19.25556), (37.93636, 23.94447))  # BUD → ATH ≈ 1120 km
    assert 1050 < km < 1200 and timedelta(hours=2) < estimate_duration(km) < timedelta(hours=2, minutes=30)


def _mock_wizz(timetable=WIZZ_TT):
    respx.get(HOME).mock(return_value=httpx.Response(200, text='<a href="https://be.wizzair.com/29.20.0/x">'))
    respx.get(f"{API.format(ver='29.20.0')}/asset/map").mock(return_value=httpx.Response(200, json=WIZZ_MAP))
    respx.get(ECB_URL).mock(return_value=httpx.Response(200, text=ECB))
    return respx.post(f"{API.format(ver='29.20.0')}/search/timetable")


@respx.mock
def test_wizz_fares_convert_currency_and_use_kiwi_duration(monkeypatch):
    monkeypatch.setattr(WizzClient, "MIN_INTERVAL", 0)
    tt = _mock_wizz()
    tt.mock(return_value=httpx.Response(200, json=WIZZ_TT, headers={"set-cookie": "RequestVerificationToken=abc"}))

    async def go():
        async with FxRates() as fx, WizzClient(fx) as wizz:
            assert await wizz.flies("BUD", "ATH") and not await wizz.flies("ATH", "LON")  # LON is a fake city station
            exact = await wizz.fares("BUD", "ATH", NOV, known_duration=timedelta(hours=2, minutes=55))
            est = await wizz.fares("BUD", "ATH", {date(2026, 11, 9)})
            return exact, est

    exact, est = asyncio.run(go())
    nov9 = next(f for f in exact if f.departure.date() == date(2026, 11, 9))
    assert nov9.price == pytest.approx(23.74, abs=0.01) and nov9.source == "wizz"
    assert nov9.arrival == datetime(2026, 11, 9, 8, 55) and not nov9.approx_arrival
    assert len(est) == 1 and est[0].approx_arrival
    assert 120 < est[0].minutes < 150 and nov9.minutes is None  # estimate's flight time; exact ones come from Kiwi
    body = json.loads(tt.calls[0].request.content)
    assert body["flightList"][0] == {"departureStation": "BUD", "arrivalStation": "ATH", "from": "2026-11-08", "to": "2026-11-12"}
    # Wizz rejects follow-up requests that echo its verification cookie, so none may be sent
    assert all("cookie" not in c.request.headers for c in tt.calls)


@respx.mock
def test_wizz_retries_throttling(monkeypatch):
    monkeypatch.setattr(WizzClient, "MIN_INTERVAL", 0)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    tt = _mock_wizz()
    tt.mock(side_effect=[httpx.Response(400, json={"handlerError": "InvalidProtocol"}), httpx.Response(200, json=WIZZ_TT)])

    async def go():
        async with FxRates() as fx, WizzClient(fx) as wizz:
            return await wizz.fares("BUD", "ATH", NOV, known_duration=timedelta(hours=3))

    assert asyncio.run(go()) and tt.call_count == 2


async def _no_sleep(*_a, **_k):
    return None


# ------------------------------------------------------------------ merging

def test_family():
    assert family(("FR",)) == "ryanair" and family(("AL",)) == "ryanair"
    assert family(("W6",)) == "wizz" and family(("W4",)) == "wizz"
    assert family(("FR", "W6")) is None and family(("OU",)) is None and family(()) is None


def test_merge_reprices_matching_flight_and_adds_others():
    kiwi_fr = flight("VCE", "ATH", "2026-11-09T20:10", "2026-11-09T23:20", 21, num="FR5825", codes=("FR",))
    kiwi_other = flight("VCE", "ATH", "2026-11-09T12:00", "2026-11-09T15:00", 30, codes=("A3",))
    direct_same = flight("VCE", "ATH", "2026-11-09T20:10", "2026-11-09T23:20", 18.99, num="", codes=("FR",), source="ryanair")
    direct_new = flight("VCE", "ATH", "2026-11-10T20:10", "2026-11-10T23:20", 17.99, num="", codes=("FR",), source="ryanair")
    merged = merge([kiwi_fr, kiwi_other], [direct_same, direct_new])
    assert [f.price for f in merged] == [17.99, 18.99, 30]
    repriced = next(f for f in merged if f.price == 18.99)
    assert repriced.flight_numbers == ("FR5825",) and repriced.source == "ryanair"  # Kiwi details kept
    # a dearer direct fare doesn't replace Kiwi's price
    assert merge([kiwi_fr], [flight("VCE", "ATH", "2026-11-09T20:10", "2026-11-09T23:20", 25, codes=("FR",), source="ryanair")])[0].price == 21


class _FakeAirline:
    def __init__(self, routes, fares, fail=False):
        self._routes, self._fares, self.fail = routes, fares, fail

    async def routes(self, o):
        return self._routes.get(o, set())

    async def flies(self, o, d):
        return d in self._routes.get(o, set())

    async def cheapest_per_day(self, o, d, days, adults=1):
        if self.fail:
            raise RuntimeError("blocked")
        return [f for f in self._fares if (f.origin, f.destination) == (o, d) and f.departure.date() in days]

    async def fares(self, o, d, days, adults=1, known_duration=None):
        return await self.cheapest_per_day(o, d, days, adults)


def test_direct_fares_only_queries_flown_routes_and_summarises_failures():
    fr = flight("BUD", "ATH", "2026-11-09T06:30", "2026-11-09T09:30", 29.99, num="", codes=("FR",), source="ryanair")
    ra = _FakeAirline({"BUD": {"ATH"}}, [fr])
    wz = _FakeAirline({"VIE": {"ATH"}}, [], fail=True)
    wz.fares = wz.cheapest_per_day  # make Wizz fail too
    direct = DirectFares(ra, wz)
    out = asyncio.run(direct.improve([], {("BUD", "ATH"), ("VIE", "ATH"), ("ZAG", "ATH")}, NOV, 1))
    assert out == [fr]
    assert direct.summary() == ["Wizz Air direct prices unavailable for 1 route(s) (VIE→ATH); Kiwi prices used there."]


def test_direct_only_fares_get_the_routes_real_flight_time_from_kiwi():
    kiwi = replace(flight("BTS", "ATH", "2026-11-09T12:00", "2026-11-09T15:10", 60, codes=("A3",)), minutes=130)
    fr = flight("BTS", "ATH", "2026-11-09T16:25", "2026-11-09T19:35", 41.19, num="", codes=("FR",), source="ryanair")
    other = flight("VIE", "ATH", "2026-11-09T16:25", "2026-11-09T19:35", 41.19, num="", codes=("FR",), source="ryanair")
    ra = _FakeAirline({"BTS": {"ATH"}, "VIE": {"ATH"}}, [fr, other])
    out = asyncio.run(DirectFares(ra, None).improve([kiwi], {("BTS", "ATH"), ("VIE", "ATH")}, NOV, 1))
    minutes = {(f.origin, f.departure.hour): f.minutes for f in out}
    assert minutes == {("BTS", 12): 130, ("BTS", 16): 130, ("VIE", 16): None}  # VIE: no Kiwi flight to learn from
