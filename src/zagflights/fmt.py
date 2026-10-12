"""Human-readable formatting shared by the terminal and web output."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .models import Leg


def money(x: float) -> str:
    return f"€{x:,.2f}".replace(".00", "")


def day(d: date | datetime) -> str:
    return d.strftime("%a %d %b")


def hm(t: datetime) -> str:
    return t.strftime("%H:%M")


def dur(td: timedelta) -> str:
    minutes = max(int(td.total_seconds() // 60), 0)
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h{m:02d}"
    return f"{h}h" if h else f"{m}m"


def time_with_day(t: datetime, ref: datetime) -> str:
    """'07:30', or '07:30 (+1)' / '22:10 (-1)' when on a different day than ``ref``."""
    delta = (t.date() - ref.date()).days
    return hm(t) + (f" ({delta:+d})" if delta else "")


def flight_line(leg: Leg) -> str:
    f = leg.flight
    via = f" via {'/'.join(f.route[1:-1])}" if f.stops and len(f.route) > 2 else ""
    return (f"✈ {f.origin} {hm(f.departure)} → {f.destination} {time_with_day(f.arrival, f.departure)}"
            f"{via} · {', '.join(f.carriers)} · {money(f.price)}")


def ground_line(leg: Leg) -> str:
    s = leg.hub
    if leg.bus is None:
        ht = s.home_transfer
        where = "home → airport" if leg.direction == "out" else "airport → home"
        return f"🚕 {where} ({dur(timedelta(minutes=ht.minutes))}) · {money(leg.ground_cost)}"
    b, stop = leg.bus, leg.stop
    ref = leg.flight.departure if leg.direction == "out" else leg.flight.arrival
    change = f", {b.transfers} change" + ("s" if b.transfers > 1 else "") if b.transfers else ""
    line = (f"🚌 {b.from_station} {time_with_day(b.departure, ref)} → {b.to_station} "
            f"{time_with_day(b.arrival, ref)}{change} · FlixBus {money(b.price)}")
    if stop and stop.transfer_cost:
        line += f" + {dur(timedelta(minutes=stop.transfer_min))} transfer {money(stop.transfer_cost)}"
    elif stop and stop.transfer_min:
        line += f" + {dur(timedelta(minutes=stop.transfer_min))} walk"
    return line


def wait_line(leg: Leg) -> str:
    if leg.bus is None:
        return ""
    if leg.direction == "out":
        text = f"⏱ {dur(leg.wait)} at the airport before take-off"
    else:
        text = f"⏱ bus leaves {dur(leg.wait)} after landing"
    night = " · 🌙 overnight wait" if leg.overnight else ""
    extra = f" (+{money(leg.overnight_cost)})" if leg.overnight_cost else ""
    return f"{text}{night}{extra}"


def leg_lines(leg: Leg) -> list[str]:
    head = f"{day(leg.flight.departure)} · {leg.hub.code} {leg.hub.name} · {money(leg.total)}"
    body = [ground_line(leg), flight_line(leg)] if leg.direction == "out" else [flight_line(leg), ground_line(leg)]
    w = wait_line(leg)
    return [head, *body, *([w] if w else [])]
