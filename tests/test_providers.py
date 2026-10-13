from __future__ import annotations

import asyncio
import json
from datetime import date, datetime

import httpx
import pytest
import respx
from conftest import FIXTURES

from zagflights.cache import Cache
from zagflights.providers.flixbus import API, FlixBusClient, parse_rides, pick_city
from zagflights.providers.kiwi import (KIWI_MCP_URL, KiwiClient, ProviderError, itinerary_to_flight,
                                       parse_mcp_response, parse_search_result)

SSE = (FIXTURES / "kiwi_multi_origin_ath.sse").read_text()
FLIX = json.loads((FIXTURES / "flixbus_zagreb_budapest.json").read_text())


def test_parse_kiwi_sse_fixture():
    data = parse_search_result(parse_mcp_response(SSE, "text/event-stream"))
    flights = [itinerary_to_flight(it) for it in data["itineraries"]]
    assert len(flights) == 15
    cheapest = min(flights, key=lambda f: f.price)
    assert (cheapest.origin, cheapest.destination, cheapest.price) == ("VCE", "ATH", 21.0)
    assert cheapest.departure == datetime(2026, 11, 9, 20, 10)
    assert cheapest.carriers == ("Ryanair",) and cheapest.flight_numbers == ("FR5825",)
    assert cheapest.booking_url.startswith("https://kiwi.com/")
    assert cheapest.minutes == 130  # 2h10 in the air, though local clocks say 20:10 → 23:20 (Athens is an hour ahead)


def test_parse_kiwi_errors():
    with pytest.raises(ProviderError):
        parse_search_result({"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": [{"type": "text", "text": "bad date"}]}})
    with pytest.raises(ProviderError):
        parse_search_result({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "nope"}})
    with pytest.raises(ProviderError):
        parse_mcp_response("event: ping\n\n", "text/event-stream")
    plain = parse_mcp_response(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}), "application/json")
    assert plain["id"] == 1


def test_parse_flixbus_fixture():
    rides = parse_rides(FLIX, "Zagreb", "Budapest", "from-id", "to-id", date(2026, 11, 8), 1)
    assert rides and rides[0].departure == datetime(2026, 11, 8, 7, 30)
    assert rides[0].price == 20.98  # what you actually pay, incl. platform fee
    assert rides[0].from_station == "Zagreb"
    assert "rideDate=08.11.2026" in rides[0].booking_url


def test_pick_city_prefers_exact_name():
    hits = [{"name": "Venice", "id": "city", "is_flixbus_city": True},
            {"name": "Venice Airport", "id": "apt", "is_flixbus_city": True}]
    assert pick_city(hits, "venice airport")["id"] == "apt"
    assert pick_city(hits, "Venezia")["id"] == "city"
    assert pick_city([], "x") is None


@respx.mock
def test_kiwi_client_request_and_cache(tmp_path):
    route = respx.post(KIWI_MCP_URL).mock(return_value=httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"}))

    async def go():
        async with KiwiClient(cache=Cache(tmp_path)) as kiwi:
            a = await kiwi.search("VCE", "ATH", date(2026, 11, 10), flex_days=2, cabin_bag=True)
            b = await kiwi.search("VCE", "ATH", date(2026, 11, 10), flex_days=2, cabin_bag=True)
            return a, b

    a, b = asyncio.run(go())
    assert a == b and a[0].price == 21.0
    assert route.call_count == 1  # second search served from the cache
    sent = json.loads(route.calls[0].request.content)
    args = sent["params"]["arguments"]
    assert sent["method"] == "tools/call" and sent["params"]["name"] == "search-flight"
    assert args["departureDate"] == "10/11/2026" and args["departureDateFlexDays"] == 2
    assert args["adults_hand_bags"] == [1]


@respx.mock
def test_kiwi_client_retries_server_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    route = respx.post(KIWI_MCP_URL).mock(side_effect=[
        httpx.Response(503), httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})])

    async def go():
        async with KiwiClient(cache=Cache(None, enabled=False)) as kiwi:
            return await kiwi.search("VCE", "ATH", date(2026, 11, 10))

    assert asyncio.run(go())[0].price == 21.0 and route.call_count == 2


@respx.mock
def test_flixbus_client_resolves_cities_and_searches(tmp_path):
    def autocomplete(request):
        q = request.url.params["q"]
        return httpx.Response(200, json=[{"name": q, "id": f"id-{q}", "is_flixbus_city": True}])

    respx.get(f"{API}/search/autocomplete/cities").mock(side_effect=autocomplete)
    search = respx.get(f"{API}/search/service/v4/search").mock(return_value=httpx.Response(200, json=FLIX))

    async def go():
        async with FlixBusClient(cache=Cache(tmp_path)) as flix:
            # concurrent identical requests share one HTTP call
            return await asyncio.gather(*(flix.search("Zagreb", "Budapest", date(2026, 11, 8)) for _ in range(3)))

    results = asyncio.run(go())
    assert all(r == results[0] for r in results) and results[0][0].price == 20.98
    assert search.call_count == 1
    params = search.calls[0].request.url.params
    assert params["from_city_id"] == "id-Zagreb" and params["departure_date"] == "08.11.2026"


async def _no_sleep(*_a, **_k):
    return None
