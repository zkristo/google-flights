from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta

from conftest import bus, dt, flight

from zagflights import cli
from zagflights import events as ev
from zagflights.config import Hub
from zagflights.models import Leg
from zagflights.planner import Query, combine

TODAY = date(2026, 10, 12)
NOW = datetime(2026, 10, 12, 19, 0, tzinfo=ev.TZ)


def event(id, start, end=None, airports=("ATH",), tag="meeting", location="Hotel Athens", **extra):
    e = {"id": id, "title": f"Event {id}", "tag": tag, "start": start, "end": end,
         "location": {"name": location} if location else None, **extra}
    if airports is not None:
        e["airports"] = list(airports)
    return e


def test_targets_picks_upcoming_in_person_events_with_airports():
    found, skipped = ev.targets([
        event("dl", "2026-11-01", tag="deadline"),
        event("cancelled", "2026-11-06", status="cancelled"),
        event("online", "2026-11-06", location="Online"),
        event("nowhere", "2026-11-06", location=None),
        event("today", "2026-10-12"),
        event("far", "2027-06-01"),
        event("no-airports", "2026-11-06", airports=None),
        event("ok", "2026-11-06T18:00+01:00", "2026-11-09", airports=("ath", "gpa", "nonsense")),
        event("one-day", "2026-11-20"),
    ], TODAY, horizon_days=180)
    assert [t.id for t in found] == ["ok", "one-day"]
    ok, one = found
    assert ok.airports == ("ATH", "GPA") and ok.destination == "ATH,GPA"
    assert (ok.out, ok.back) == (date(2026, 11, 6), date(2026, 11, 9))
    assert one.back == one.out == date(2026, 11, 20)
    assert ok.key == {"destination": "ATH,GPA", "out": "2026-11-06", "back": "2026-11-09"}
    assert skipped == [("no-airports", "no airports field")]


def _plan(config):
    """Out on 6 Nov, back on 9 Nov, with cheaper options that miss the event's days."""
    zag, bud, vce = config.hubs["ZAG"], config.hubs["BUD"], config.hubs["VCE"]
    bgy = Hub("BGY", "Bergamo", via_flight=True)
    out_bus = Leg("out", bud, flight("BUD", "ATH", "2026-11-06T09:00", "2026-11-06T12:00", 20),
                  bus("Zagreb", "Budapest Airport", "2026-11-05T23:00", "2026-11-06T05:00", 15),
                  bud.stops[0], 15, 5, timedelta(hours=4), True)
    out_hop = Leg("out", bgy, flight("BGY", "ATH", "2026-11-06T11:00", "2026-11-06T14:00", 25), None, None,
                  8, 45, timedelta(hours=3, minutes=30), False,
                  hop=flight("ZAG", "BGY", "2026-11-06T06:00", "2026-11-06T07:30", 15))
    out_late = Leg("out", zag, flight("ZAG", "ATH", "2026-11-06T22:00", "2026-11-07T00:30", 10), None, None,
                   8, 45, timedelta(0), False)  # lands the day after the event starts
    out_early = Leg("out", zag, flight("ZAG", "ATH", "2026-11-05T10:00", "2026-11-05T12:00", 5), None, None,
                    8, 45, timedelta(0), False)  # a day early
    back_bus = Leg("back", vce, flight("ATH", "VCE", "2026-11-09T20:00", "2026-11-09T21:30", 30),
                   bus("Venice Airport", "Zagreb", "2026-11-10T03:00", "2026-11-10T07:00", 20),
                   vce.stops[0], 20, 5, timedelta(hours=5, minutes=30), True)
    back_early = Leg("back", zag, flight("ATH", "ZAG", "2026-11-08T10:00", "2026-11-08T12:00", 10), None, None,
                     8, 45, timedelta(0), False)
    q = Query("ATH", date(2026, 11, 6), date(2026, 11, 9), flex=1, top=20)
    return combine(q, [out_bus, out_hop, out_late, out_early], [back_bus, back_early])


def test_summarise_keeps_event_dates_and_offers_flexible(config):
    target = ev.Target("ok", "aRM Tomato", ("ATH",), date(2026, 11, 6), date(2026, 11, 9))
    entry = ev.summarise(target, _plan(config), NOW)
    assert entry["destination"] == "ATH" and entry["out"] == "2026-11-06" and entry["back"] == "2026-11-09"
    assert entry["searchedAt"] == "2026-10-12T19:00+02:00"
    assert [t["total"] for t in entry["best"]] == [85, 98]  # bus via BUD, then the BGY hop; both back via VCE
    assert entry["baseline"] is None  # no ZAG flight fits the event's days
    assert entry["cheapestFlexible"]["total"] == 31  # out 5 Nov, back 8 Nov
    assert {"out": "2026-11-05", "back": "2026-11-08", "total": 31} in entry["grid"]

    best = entry["best"][0]
    assert [s["kind"] for s in best["out"]["steps"]] == ["bus", "flight"]
    assert [s["kind"] for s in best["back"]["steps"]] == ["flight", "bus"]
    out_bus, out_flight = best["out"]["steps"]
    assert out_bus["dep"] == "2026-11-05T23:00" and out_bus["price"] == 15 and out_bus["transfer"] == {"minutes": 5, "price": 0}
    assert out_flight["from"] == "BUD" and out_flight["hop"] is False and out_flight["url"].startswith("https://")
    assert best["out"]["airport"] == "BUD" and "before take-off" in best["out"]["wait"]
    assert not best["out"]["wait"].startswith("⏱")

    hop = entry["best"][1]["out"]
    assert hop["viaFlight"] is True
    assert [(s["kind"], s.get("hop")) for s in hop["steps"]] == [("transfer", None), ("flight", True), ("flight", False)]
    assert hop["steps"][0] == {"kind": "transfer", "text": "Home → Zagreb airport", "minutes": 45, "price": 8}
    json.dumps(entry)  # plain JSON all the way down


def test_summarise_without_matching_trips(config):
    target = ev.Target("x", "X", ("ATH",), date(2026, 11, 7), date(2026, 11, 7))
    entry = ev.summarise(target, _plan(config), NOW)
    assert entry["best"] == [] and entry["cheapestFlexible"]["total"] == 31


class FakeSearch:
    def __init__(self, result, fail=()):
        self.result, self.fail, self.calls = result, set(fail), []

    async def __call__(self, q, cfg):
        self.calls.append((q.destination, q.out_date, q.back_date, q.flex))
        if q.destination in self.fail:
            raise RuntimeError("Kiwi is down")
        return self.result


def _write_events(path, items):
    path.write_text(json.dumps({"updatedAt": None, "events": items}), encoding="utf-8")


def _run(tmp_path, config, search, **kw):
    kw.setdefault("today", TODAY)
    return asyncio.run(ev.refresh(tmp_path / "events.json", tmp_path / "flights.json", config, search,
                                  progress=lambda m: None, **kw))


def test_refresh_searches_keeps_fresh_and_redoes_stale(tmp_path, config, monkeypatch):
    monkeypatch.setattr(ev, "now_local", lambda: NOW)
    _write_events(tmp_path / "events.json", [
        event("a", "2026-11-06", "2026-11-09"),
        event("b", "2026-11-06", "2026-11-09", airports=("ATH", "GPA")),
        event("c", "2026-11-06", "2026-11-09", airports=("SKG",)),
        event("bus-only", "2026-11-06", airports=None),
    ])
    search = FakeSearch(_plan(config))
    report = _run(tmp_path, config, search, limit=2)
    assert len(search.calls) == 2 and search.calls[0][3] == 1  # ±1 day around the event's dates
    assert report["pending"] and report["written"] and report["skipped"] == [("bus-only", "no airports field")]
    data = json.loads((tmp_path / "flights.json").read_text(encoding="utf-8"))
    assert data["origin"] == "Zagreb" and data["updatedAt"] == "2026-10-12T19:00+02:00"
    assert len(data["events"]) == 2 and data["events"][report["searched"][0]]["best"][0]["total"] == 85

    # next run: only the one never searched is due
    search.calls.clear()
    report = _run(tmp_path, config, search, limit=2)
    assert len(search.calls) == 1 and report["kept"] == 2 and not report["pending"]

    # nothing due → the file is left untouched (no commit for the routine)
    before = (tmp_path / "flights.json").read_text(encoding="utf-8")
    search.calls.clear()
    report = _run(tmp_path, config, search)
    assert search.calls == [] and report["written"] is False
    assert (tmp_path / "flights.json").read_text(encoding="utf-8") == before

    # a changed date re-searches that event at once; an old result is re-searched too
    _write_events(tmp_path / "events.json", [
        event("a", "2026-11-07", "2026-11-09"),
        event("b", "2026-11-06", "2026-11-09", airports=("ATH", "GPA")),
        event("c", "2026-11-06", "2026-11-09", airports=("SKG",)),
    ])
    monkeypatch.setattr(ev, "now_local", lambda: NOW + timedelta(hours=21))
    search.calls.clear()
    report = _run(tmp_path, config, search, limit=1)
    assert search.calls[0][1] == date(2026, 11, 7) and report["searched"] == ["a"] and len(report["pending"]) == 2


def test_refresh_keeps_old_prices_on_error_and_drops_past_events(tmp_path, config, monkeypatch):
    monkeypatch.setattr(ev, "now_local", lambda: NOW)
    _write_events(tmp_path / "events.json", [event("a", "2026-11-06", "2026-11-09"),
                                             event("gone", "2026-10-20", "2026-10-22")])
    _run(tmp_path, config, FakeSearch(_plan(config)))
    monkeypatch.setattr(ev, "now_local", lambda: NOW + timedelta(days=10))
    report = _run(tmp_path, config, FakeSearch(_plan(config), fail={"ATH"}), today=date(2026, 10, 22))
    assert report["failed"] == ["a"]
    data = json.loads((tmp_path / "flights.json").read_text(encoding="utf-8"))
    assert list(data["events"]) == ["a"]
    assert data["events"]["a"]["best"][0]["total"] == 85 and "Kiwi is down" in data["events"]["a"]["lastError"]


def test_cli_events_command(tmp_path, config, monkeypatch):
    monkeypatch.setattr(ev, "now_local", lambda: NOW)
    _write_events(tmp_path / "events.json", [event("a", "2026-11-06", "2026-11-09")])
    res = _plan(config)

    async def fake_find(q, cfg, **kw):
        return res

    monkeypatch.setattr(cli, "find_trips", fake_find)
    out = tmp_path / "flights.json"
    assert cli.main(["events", str(tmp_path / "events.json"), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["events"]["a"]["best"][0]["total"] == 85
