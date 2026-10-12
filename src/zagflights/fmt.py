"""Human-readable formatting shared by the terminal and web output."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .models import Flight, Leg


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


SOURCE_LABEL = {"ryanair": "ryanair.com", "wizz": "wizzair.com"}


def flight_text(f: Flight, hop: bool = False) -> str:
    via = f" via {'/'.join(f.route[1:-1])}" if f.stops and len(f.route) > 2 else ""
    arrival = ("≈" if f.approx_arrival else "") + time_with_day(f.arrival, f.departure)
    source = f" ({SOURCE_LABEL[f.source]})" if f.source in SOURCE_LABEL else ""
    tag = " · hop" if hop else ""
    return (f"✈ {f.origin} {hm(f.departure)} → {f.destination} {arrival}{via} · {', '.join(f.carriers)} · "
            f"{money(f.price)}{source}{tag}")


def flight_line(leg: Leg) -> str:
    return flight_text(leg.flight)


def ground_line(leg: Leg) -> str:
    if leg.bus is None:
        where = "home → airport" if leg.direction == "out" else "airport → home"
        return f"🚕 {where} ({dur(timedelta(minutes=leg.ground_minutes))}) · {money(leg.ground_cost)}"
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
    night = " · 🌙 overnight wait" if leg.overnight else ""
    extra = f" (+{money(leg.overnight_cost)})" if leg.overnight_cost else ""
    if leg.hop is not None:
        return f"⏱ {dur(leg.wait)} connection in {leg.hub.code} (separate tickets){night}{extra}"
    if leg.bus is None:
        return ""
    if leg.direction == "out":
        text = f"⏱ {dur(leg.wait)} at the airport before take-off"
    else:
        text = f"⏱ bus leaves {dur(leg.wait)} after landing"
    return f"{text}{night}{extra}"


def leg_lines(leg: Leg) -> list[str]:
    via = f"fly via {leg.hub.code} {leg.hub.name}" if leg.hub.via_flight else f"{leg.hub.code} {leg.hub.name}"
    head = f"{day(leg.flight.departure)} · {via} · {money(leg.total)}"
    hop = [flight_text(leg.hop, hop=True)] if leg.hop else []
    if leg.direction == "out":
        body = [ground_line(leg), *hop, flight_line(leg)]
    else:
        body = [flight_line(leg), *hop, ground_line(leg)]
    w = wait_line(leg)
    return [head, *body, *([w] if w else [])]
