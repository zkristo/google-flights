"""Flights from Kiwi.com via its public MCP server (https://mcp.kiwi.com).

This is the same server behind the Kiwi.com connector in Claude. It speaks MCP over
"streamable HTTP": we POST a JSON-RPC ``tools/call`` for ``search-flight`` and read the reply,
which arrives either as plain JSON or as a server-sent event (``data: {...}``). No API key needed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from itertools import count
from typing import Any

import httpx

from ..cache import Cache
from ..models import Flight

KIWI_MCP_URL = "https://mcp.kiwi.com"
FLIGHT_TTL = 60 * 60  # fares move; keep them for an hour


class ProviderError(RuntimeError):
    pass


def parse_mcp_response(body: str, content_type: str = "") -> dict:
    """Return the JSON-RPC message from a plain-JSON or SSE response body."""
    if "json" in content_type and not body.lstrip().startswith(("event:", "data:")):
        return json.loads(body)
    messages = [json.loads(line[5:].strip()) for line in body.splitlines() if line.startswith("data:")]
    replies = [m for m in messages if "result" in m or "error" in m]
    if not replies:
        raise ProviderError(f"Kiwi: no JSON-RPC reply in response: {body[:200]!r}")
    return replies[-1]


def parse_search_result(message: dict) -> dict:
    """Unwrap ``result.content[0].text`` (a JSON document) from a tools/call reply."""
    if "error" in message:
        raise ProviderError(f"Kiwi error: {message['error']}")
    result = message.get("result", {})
    texts = [c.get("text", "") for c in result.get("content", []) if c.get("type", "text") == "text"]
    if result.get("isError"):
        raise ProviderError(f"Kiwi tool error: {' '.join(texts)[:300]}")
    if not texts:
        raise ProviderError("Kiwi: empty tool result")
    data = json.loads(texts[0])
    if data.get("error"):
        raise ProviderError(f"Kiwi search error: {data['error']}")
    return data


def itinerary_to_flight(it: dict) -> Flight:
    leg = it["outbound"]
    segs = leg.get("segments") or []
    first, last = (segs[0], segs[-1]) if segs else ({}, {})
    carriers: list[str] = []
    codes: list[str] = []
    for s in segs:
        name = s.get("carrierName") or s.get("carrier") or "?"
        if name not in carriers:
            carriers.append(name)
        if s.get("carrier") and s["carrier"] not in codes:
            codes.append(s["carrier"])
    return Flight(
        id=str(it.get("id", "")),
        origin=first.get("from") or leg["from"],
        destination=last.get("to") or leg["to"],
        origin_name=first.get("fromName") or first.get("fromCity") or leg["from"],
        destination_name=last.get("toName") or last.get("toCity") or leg["to"],
        departure=datetime.fromisoformat(leg["departureTime"]).replace(tzinfo=None),
        arrival=datetime.fromisoformat(leg["arrivalTime"]).replace(tzinfo=None),
        price=float(it["price"]),
        carriers=tuple(carriers),
        flight_numbers=tuple(s.get("flightNumber", "") for s in segs),
        stops=int(leg.get("stops", max(len(segs) - 1, 0))),
        booking_url=it.get("bookingUrl", ""),
        route=tuple(leg.get("route") or ()),
        carrier_codes=tuple(codes),
        minutes=round(leg["durationSeconds"] / 60) if isinstance(leg.get("durationSeconds"), (int, float)) else None,
    )


class KiwiClient:
    def __init__(
        self,
        cache: Cache | None = None,
        http: httpx.AsyncClient | None = None,
        concurrency: int = 4,
        url: str = KIWI_MCP_URL,
    ):
        self.cache = cache or Cache(enabled=False)
        self._http = http
        self._own_http = http is None
        self._sem = asyncio.Semaphore(concurrency)
        self._ids = count(1)
        self.url = url
        self.calls = 0

    async def __aenter__(self) -> "KiwiClient":
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0))
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        assert self._http is not None, "use `async with KiwiClient()`"
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with self._sem:
                    self.calls += 1
                    resp = await self._http.post(self.url, json=body, headers=headers)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise ProviderError(f"Kiwi HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise ProviderError(f"Kiwi HTTP {resp.status_code}: {resp.text[:200]}")
                return parse_search_result(parse_mcp_response(resp.text, resp.headers.get("content-type", "")))
            except httpx.TransportError as exc:  # network hiccup → retry
                last_exc = exc
            except ProviderError as exc:
                if "HTTP 429" not in str(exc) and "HTTP 5" not in str(exc):
                    raise
                last_exc = exc
            await asyncio.sleep(1.5 * (attempt + 1))
        raise ProviderError(f"Kiwi request failed: {last_exc}")

    async def search(
        self,
        fly_from: str,
        fly_to: str,
        day: date,
        flex_days: int = 0,
        adults: int = 1,
        max_stops: int | None = None,
        cabin_bag: bool = False,
        checked_bag: bool = False,
        price_to: int | None = None,
        one_for_city: bool = False,
    ) -> list[Flight]:
        """One-way flights fly_from → fly_to departing on ``day`` ± ``flex_days``, cheapest first.

        ``fly_from``/``fly_to`` may also be a region such as "Europe" (with ``price_to`` and
        ``one_for_city`` this answers "where can I fly cheaply from ZAG that day?").
        """
        args: dict[str, Any] = {
            "flyFrom": fly_from,
            "flyTo": fly_to,
            "departureDate": day.strftime("%d/%m/%Y"),
            "departureDateFlexDays": flex_days,
            "adults": adults,
            "currency": "EUR",
            "sort": "price",
        }
        if max_stops is not None:
            args["max_sector_stopovers"] = max_stops
        if cabin_bag:
            args["adults_hand_bags"] = [1] * adults
        if checked_bag:
            args["adults_hold_bags"] = [1] * adults
        if price_to is not None:
            args["price_to"] = int(price_to)
        if one_for_city:
            args["one_for_city"] = True

        data = self.cache.get("kiwi", args, FLIGHT_TTL)
        if data is None:
            data = await self._call_tool("search-flight", args)
            self.cache.set("kiwi", args, data)
        flights = []
        for it in data.get("itineraries", []):
            try:
                flights.append(itinerary_to_flight(it))
            except (KeyError, ValueError, TypeError):
                continue
        return sorted(flights, key=lambda f: f.price)
