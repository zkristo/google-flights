from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, timedelta

from conftest import FakeBuses, FakeFlights, bus, dt, flight

from zagflights.planner import Planner, Query, spans_night


def plan(config, flights, buses, query, hubs=None, settings=None):
    if settings:
        config = replace(config, settings=replace(config.settings, **settings))
    hub_list = [config.hubs[c] for c in (hubs or config.hubs)]
    return asyncio.run(Planner(config, hub_list, flights, buses).run(query))


Q = Query("ATH", date(2026, 11, 10), date(2026, 11, 15))


def test_open_jaw_beats_home_airport(config):
    flights = FakeFlights([
        flight("ZAG", "ATH", "2026-11-10T10:00", "2026-11-10T12:00", 120),
        flight("BUD", "ATH", "2026-11-10T18:00", "2026-11-10T20:50", 24),
        flight("ATH", "VCE", "2026-11-15T12:00", "2026-11-15T13:30", 30),
        flight("ATH", "ZAG", "2026-11-15T12:00", "2026-11-15T13:30", 110),
    ])
    buses = FakeBuses([
        bus("Zagreb", "Budapest Airport", "2026-11-10T07:30", "2026-11-10T12:20", 20),
        bus("Venice Airport", "Zagreb", "2026-11-15T16:00", "2026-11-15T21:00", 22),
    ])
    res = plan(config, flights, buses, Q)
    best = res.trips[0]
    assert (best.out.hub.code, best.back.hub.code) == ("BUD", "VCE")
    assert best.total == 24 + 20 + 30 + 22
    assert res.baseline is not None and res.baseline.total == 120 + 8 + 110 + 8
    assert {"ZAG", "BUD"} <= set(res.best_out_by_hub)


def test_bus_must_arrive_before_checkin_buffer(config):
    flights = FakeFlights([flight("BUD", "ATH", "2026-11-10T18:00", "2026-11-10T20:50", 24)])
    buses = FakeBuses([
        bus("Zagreb", "Budapest Airport", "2026-11-10T11:00", "2026-11-10T16:30", 5),  # ready 16:35 > 16:00 → too late
        bus("Zagreb", "Budapest Airport", "2026-11-10T07:30", "2026-11-10T12:20", 20),
    ])
    res = plan(config, flights, buses, replace(Q, back_date=Q.out_date), hubs=["BUD"])
    leg = res.out_legs[0]
    assert leg.bus.price == 20
    assert leg.wait == timedelta(hours=5, minutes=35)  # 12:25 ready → 18:00
    assert not leg.overnight


def test_city_stop_transfer_cost_counts(config):
    flights = FakeFlights([flight("BUD", "ATH", "2026-11-10T18:00", "2026-11-10T20:50", 24)])
    buses = FakeBuses([
        bus("Zagreb", "Budapest Airport", "2026-11-10T07:30", "2026-11-10T12:20", 25),
        bus("Zagreb", "Budapest", "2026-11-10T07:30", "2026-11-10T12:00", 15),  # 15 + 6 transfer = 21 < 25
    ])
    res = plan(config, flights, buses, replace(Q, back_date=Q.out_date), hubs=["BUD"])
    leg = res.out_legs[0]
    assert leg.stop.flixbus == "Budapest" and leg.ground_cost == 21 and leg.total == 45


def test_early_flight_uses_bus_the_day_before_and_is_overnight(config):
    flights = FakeFlights([flight("BUD", "ATH", "2026-11-10T06:00", "2026-11-10T08:55", 24)])
    buses = FakeBuses([bus("Zagreb", "Budapest Airport", "2026-11-09T14:10", "2026-11-09T19:00", 20)])
    res = plan(config, flights, buses, replace(Q, back_date=Q.out_date), hubs=["BUD"])
    leg = res.out_legs[0]
    assert leg.overnight and leg.bus.departure.date() == date(2026, 11, 9)
    # …unless the wait is longer than allowed
    res = plan(config, flights, buses, replace(Q, back_date=Q.out_date), hubs=["BUD"], settings={"max_wait_hours": 10})
    assert res.out_legs == []


def test_overnight_cost_is_added(config):
    flights = FakeFlights([flight("BUD", "ATH", "2026-11-10T06:00", "2026-11-10T08:55", 24)])
    buses = FakeBuses([bus("Zagreb", "Budapest Airport", "2026-11-09T14:10", "2026-11-09T19:00", 20)])
    res = plan(config, flights, buses, replace(Q, back_date=Q.out_date), hubs=["BUD"], settings={"overnight_cost": 15})
    assert res.out_legs[0].total == 24 + 20 + 15


def test_return_bus_after_landing_buffer(config):
    flights = FakeFlights([flight("ATH", "BUD", "2026-11-15T10:05", "2026-11-15T11:15", 44)])
    buses = FakeBuses([
        bus("Budapest Airport", "Zagreb", "2026-11-15T11:45", "2026-11-15T17:00", 10),  # before 11:15+45+5
        bus("Budapest Airport", "Zagreb", "2026-11-15T14:00", "2026-11-15T19:00", 22),
    ])
    res = plan(config, flights, buses, replace(Q, out_date=Q.back_date), hubs=["BUD"])
    leg = res.back_legs[0]
    assert leg.bus.price == 22 and leg.wait == timedelta(hours=2, minutes=45)


def test_wait_cost_prefers_shorter_wait(config):
    flights = FakeFlights([flight("ATH", "BUD", "2026-11-15T10:05", "2026-11-15T11:15", 44)])
    buses = FakeBuses([
        bus("Budapest Airport", "Zagreb", "2026-11-15T14:00", "2026-11-15T19:00", 26),  # 2h45 wait
        bus("Budapest Airport", "Zagreb", "2026-11-16T00:55", "2026-11-16T05:45", 22),  # 13h40 wait
    ])
    q = replace(Q, out_date=Q.back_date)
    assert plan(config, flights, buses, q, hubs=["BUD"]).back_legs[0].bus.price == 22
    leg = plan(config, flights, buses, q, hubs=["BUD"], settings={"wait_cost_per_hour": 2}).back_legs[0]
    assert leg.bus.price == 26 and leg.total == 44 + 26 and leg.penalty == 0


def test_kiwi_city_alias_maps_to_actual_airport_and_dedupes(config):
    tsf = flight("TSF", "ATH", "2026-11-10T09:00", "2026-11-10T12:00", 19, num="FR1")
    flights = FakeFlights([tsf], alias={"VCE": ["VCE", "TSF"]})
    buses = FakeBuses([bus("Zagreb", "Treviso Airport", "2026-11-10T01:00", "2026-11-10T06:30", 18)])
    res = plan(config, flights, buses, replace(Q, back_date=Q.out_date), hubs=["VCE", "TSF"])
    assert [leg.hub.code for leg in res.out_legs] == ["TSF"]


def test_return_must_leave_after_outbound_lands_and_min_nights(config):
    flights = FakeFlights([
        flight("ZAG", "ATH", "2026-11-12T10:00", "2026-11-12T12:00", 50),
        flight("ATH", "ZAG", "2026-11-11T10:00", "2026-11-11T12:00", 10),  # before arrival → invalid pair
        flight("ATH", "ZAG", "2026-11-13T10:00", "2026-11-13T12:00", 40),
        flight("ATH", "ZAG", "2026-11-16T10:00", "2026-11-16T12:00", 60),
    ])
    q = Query("ATH", date(2026, 11, 12), date(2026, 11, 13), flex=3)
    res = plan(config, flights, FakeBuses([]), q, hubs=["ZAG"])
    assert [t.back.flight.price for t in res.trips] == [40, 60]
    res = plan(config, flights, FakeBuses([]), replace(q, min_nights=2), hubs=["ZAG"])
    assert [t.back.flight.price for t in res.trips] == [60]


def test_date_matrix_and_flex_window(config):
    flights = FakeFlights([
        flight("ZAG", "ATH", "2026-11-09T10:00", "2026-11-09T12:00", 30),
        flight("ZAG", "ATH", "2026-11-10T10:00", "2026-11-10T12:00", 50),
        flight("ZAG", "ATH", "2026-11-13T10:00", "2026-11-13T12:00", 1),  # outside ±1 window
        flight("ATH", "ZAG", "2026-11-15T10:00", "2026-11-15T12:00", 40),
    ])
    res = plan(config, flights, FakeBuses([]), Query("ATH", date(2026, 11, 10), date(2026, 11, 15), flex=1), hubs=["ZAG"])
    assert res.date_matrix == {(date(2026, 11, 9), date(2026, 11, 15)): 30 + 8 + 40 + 8,
                               (date(2026, 11, 10), date(2026, 11, 15)): 50 + 8 + 40 + 8}


def test_failing_bus_stop_becomes_warning(config):
    flights = FakeFlights([flight("BUD", "ATH", "2026-11-10T18:00", "2026-11-10T20:50", 24)])
    buses = FakeBuses([bus("Zagreb", "Budapest", "2026-11-10T07:30", "2026-11-10T12:00", 15)], fail={"Budapest Airport"})
    res = plan(config, flights, buses, replace(Q, back_date=Q.out_date), hubs=["BUD"])
    assert res.out_legs[0].stop.flixbus == "Budapest"
    assert any("Budapest Airport" in w for w in res.warnings)


def test_only_cheap_flights_get_bus_lookups(config):
    """Pruning: with many pricey flights on one date, we stop checking buses once enough cheap legs exist."""
    many = [flight("ZAG", "ATH", f"2026-11-10T{h:02d}:00", f"2026-11-10T{h + 2:02d}:00", 10 + h) for h in range(6, 12)]
    many += [flight("BUD", "ATH", f"2026-11-10T{h:02d}:00", f"2026-11-10T{h + 2:02d}:00", 500 + h) for h in range(12, 22)]
    buses = FakeBuses([])
    plan(config, FakeFlights(many), buses, replace(Q, back_date=Q.out_date), hubs=["ZAG", "BUD"])
    # BUD flights cost ≥ 512 > the 6 ZAG legs (≤ 27), so only the per-hub probe (≤ 9 flights × 2 stops × 2 days) runs
    assert len(buses.calls) <= 9 * 2 * 2


def test_spans_night():
    assert spans_night(dt("2026-11-09T23:55"), dt("2026-11-10T06:30"))
    assert spans_night(dt("2026-11-10T01:05"), dt("2026-11-10T14:40"))
    assert not spans_night(dt("2026-11-10T07:00"), dt("2026-11-10T22:00"))
    assert not spans_night(dt("2026-11-10T12:25"), dt("2026-11-10T18:00"))


# ------------------------------------------------------------------ cheap first hops (fly ZAG → X → destination)

HOP_Q = Query("ATH", date(2026, 11, 10), date(2026, 11, 15))


def test_hop_leg_outbound(config):
    flights = FakeFlights([
        flight("ZAG", "BGY", "2026-11-10T08:00", "2026-11-10T09:20", 19),
        flight("BGY", "ATH", "2026-11-10T15:00", "2026-11-10T18:00", 20),
        flight("BGY", "ATH", "2026-11-10T10:30", "2026-11-10T13:30", 5),  # only 1h10 after landing → too tight
        flight("ATH", "ZAG", "2026-11-15T10:00", "2026-11-15T12:00", 40),
    ])
    res = plan(config, flights, FakeBuses([]), HOP_Q, hubs=["ZAG"])
    hop_legs = [leg for leg in res.out_legs if leg.hub.via_flight]
    assert len(hop_legs) == 1
    leg = hop_legs[0]
    assert leg.hub.code == "BGY" and leg.hop.price == 19 and leg.flight.price == 20
    assert leg.total == 8 + 19 + 20 and leg.wait == timedelta(hours=5, minutes=40)
    assert res.trips[0].out is leg  # €47 beats nothing else here
    assert "BGY ✈" in res.best_out_by_hub


def test_hop_respects_price_cap_max_wait_and_can_be_disabled(config):
    flights = FakeFlights([
        flight("ZAG", "BGY", "2026-11-10T06:00", "2026-11-10T07:20", 19),
        flight("BGY", "ATH", "2026-11-10T19:00", "2026-11-10T22:00", 20),  # 11h40 after landing > 7h max wait
        flight("ZAG", "MLA", "2026-11-10T08:00", "2026-11-10T10:00", 60),  # hop over €50 cap
        flight("MLA", "ATH", "2026-11-10T14:00", "2026-11-10T16:00", 10),
    ])
    res = plan(config, flights, FakeBuses([]), replace(HOP_Q, back_date=HOP_Q.out_date), hubs=["ZAG"],
               settings={"max_wait_hours": 7})
    assert not [leg for leg in res.out_legs if leg.hub.via_flight]
    res = plan(config, flights, FakeBuses([]), replace(HOP_Q, back_date=HOP_Q.out_date), hubs=["ZAG"],
               settings={"max_wait_hours": 7, "hop_max_price": 70})
    assert [leg.hub.code for leg in res.out_legs if leg.hub.via_flight] == ["MLA"]
    res = plan(config, flights, FakeBuses([]), replace(HOP_Q, back_date=HOP_Q.out_date), hubs=["ZAG"],
               settings={"hop_max_price": 0, "max_wait_hours": 20})
    assert not [leg for leg in res.out_legs if leg.hub.via_flight]


def test_hop_leg_return_is_mirrored(config):
    flights = FakeFlights([
        flight("ZAG", "ATH", "2026-11-10T10:00", "2026-11-10T12:00", 90),
        flight("ATH", "CRL", "2026-11-15T07:00", "2026-11-15T09:30", 25),
        flight("CRL", "ZAG", "2026-11-15T14:00", "2026-11-15T16:00", 15),
        flight("CRL", "ZAG", "2026-11-15T10:00", "2026-11-15T12:00", 9),  # leaves 30 min after landing → too tight
    ])
    res = plan(config, flights, FakeBuses([]), HOP_Q, hubs=["ZAG"])
    leg = next(leg for leg in res.back_legs if leg.hub.via_flight)
    assert (leg.flight.origin, leg.flight.destination, leg.hop.origin, leg.hop.destination) == ("ATH", "CRL", "CRL", "ZAG")
    assert leg.total == 25 + 15 + 8 and leg.wait == timedelta(hours=4, minutes=30)


class _FakeDirect:
    def __init__(self, cheaper):
        self.cheaper = cheaper
        self.calls = []

    async def improve(self, flights, pairs, days, adults):
        self.calls.append(pairs)
        return sorted([self.cheaper.get(f.key, f) for f in flights], key=lambda f: f.price)

    def summary(self):
        return []


def test_direct_prices_are_used_unless_bags_requested(config):
    kiwi = flight("ZAG", "ATH", "2026-11-10T10:00", "2026-11-10T12:00", 50)
    back = flight("ATH", "ZAG", "2026-11-15T10:00", "2026-11-15T12:00", 40)
    direct = _FakeDirect({kiwi.key: replace(kiwi, price=35, source="ryanair")})
    hub_list = [config.hubs["ZAG"]]
    res = asyncio.run(Planner(replace(config, settings=replace(config.settings, hop_max_price=0)), hub_list,
                              FakeFlights([kiwi, back]), FakeBuses([]), direct=direct).run(HOP_Q))
    assert res.trips[0].out.flight.price == 35 and res.trips[0].out.flight.source == "ryanair"
    assert ("ZAG", "ATH") in direct.calls[0]
    res = asyncio.run(Planner(config, hub_list, FakeFlights([kiwi, back]), FakeBuses([]), direct=_FakeDirect({}))
                      .run(replace(HOP_Q, cabin_bag=True)))
    assert res.trips[0].out.flight.price == 50 and any("bags" in w for w in res.warnings)
