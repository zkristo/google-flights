"""Command line: `zagflights search ATH 2026-11-10 2026-11-15 --flex 2`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, replace
from datetime import date

from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import fmt
from .config import USER_CONFIG, default_config_text, load_config
from .models import Leg, PlanResult, Trip
from .planner import Query
from .service import find_trips


def _date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {s!r}")


def _codes(s: str) -> list[str]:
    return [c.strip().upper() for c in s.split(",") if c.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zagflights", description="Cheapest bus + flight trips from Zagreb (open-jaw via nearby airports).")
    p.add_argument("--config", help=f"hubs.toml to use (default: {USER_CONFIG} if it exists, else built-in)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="find the cheapest trips to a destination")
    s.add_argument("destination", help="IATA code or city, e.g. ATH or Athens")
    s.add_argument("out_date", type=_date, help="outbound date YYYY-MM-DD")
    s.add_argument("back_date", type=_date, help="return date YYYY-MM-DD")
    s.add_argument("--flex", type=int, default=0, help="also try ±N days around each date (0–3, default 0)")
    s.add_argument("--adults", type=int, default=1)
    s.add_argument("--direct", action="store_true", help="non-stop flights only")
    s.add_argument("--max-stops", type=int, default=None, help="max stops per flight (default: any)")
    s.add_argument("--cabin-bag", action="store_true", help="include a cabin bag in flight prices")
    s.add_argument("--checked-bag", action="store_true", help="include a checked bag in flight prices")
    s.add_argument("--min-nights", type=int, default=None, help="minimum nights at the destination")
    s.add_argument("--max-wait", type=float, default=None, help="max hours waiting between bus and flight, or between two flights (default 7)")
    s.add_argument("--overnight-cost", type=float, default=None, help="€ to add when a wait goes through the night (default from hubs.toml: 0)")
    s.add_argument("--wait-cost", type=float, default=None, help="value of your time: € per hour waiting beyond 3h; prefers shorter waits (default 0)")
    s.add_argument("--hop-max", type=float, default=None, help="also try a cheap first flight ZAG → any airport under this € (default 50, 0 = off)")
    s.add_argument("--self-transfer", type=int, default=None, help="minimum minutes between two separate flights (default 180)")
    s.add_argument("--no-direct", action="store_true", help="don't check Ryanair/Wizz Air prices directly (Kiwi only)")
    s.add_argument("--hubs", type=_codes, help="only these airports, e.g. ZAG,BUD,VCE")
    s.add_argument("--exclude", type=_codes, help="skip these airports, e.g. BEG")
    s.add_argument("--top", type=int, default=10, help="how many trips to show")
    s.add_argument("--links", type=int, default=3, help="print booking links for the top N trips")
    s.add_argument("--no-cache", action="store_true", help="ignore cached prices")
    s.add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser("hubs", help="list the airports and how you get there")
    sub.add_parser("init-config", help=f"copy the default hubs.toml to {USER_CONFIG} for editing")

    w = sub.add_parser("serve", help="start the local web page")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    return p


# ------------------------------------------------------------------ rendering

def _leg_text(leg: Leg) -> Text:
    lines = fmt.leg_lines(leg)
    t = Text(lines[0], style="bold")
    for line in lines[1:]:
        t.append("\n" + line, style="dim" if line.startswith("⏱") else "")
    return t


def render(console: Console, q: Query, res: PlanResult, links: int) -> None:
    flex = f" ±{q.flex}d" if q.flex else ""
    console.rule(f"[bold]Zagreb ⇄ {q.destination}[/] · out {fmt.day(q.out_date)}{flex} · back {fmt.day(q.back_date)}{flex}"
                 f" · {q.adults} adult{'s' if q.adults > 1 else ''}")
    for w in res.warnings:
        console.print(f"[yellow]⚠ {w}[/]")
    if not res.trips:
        console.print("[red]No complete trips found.[/] Try --flex 2, more hubs, or other dates.")
        return

    table = Table(show_lines=True, header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Total", justify="right", style="bold green")
    table.add_column("Outbound")
    table.add_column("Return")
    for i, t in enumerate(res.trips, 1):
        table.add_row(str(i), fmt.money(t.total) + (f"\n{t.nights} night{'s' if t.nights != 1 else ''}" if t.nights else ""), _leg_text(t.out), _leg_text(t.back))
    console.print(table)

    best = res.trips[0]
    if res.baseline and res.baseline is not best:
        saving = res.baseline.total - best.total
        console.print(f"Flying from/to Zagreb only would cost [bold]{fmt.money(res.baseline.total)}[/]"
                      + (f" → the best combo saves [bold green]{fmt.money(saving)}[/]." if saving > 0 else "."))
    elif res.baseline is best:
        console.print("[green]The cheapest option is simply flying from/to Zagreb.[/]")

    if len(res.date_matrix) > 1:
        outs = sorted({o for o, _ in res.date_matrix})
        backs = sorted({b for _, b in res.date_matrix})
        cheapest = min(res.date_matrix.values())
        m = Table(title="Cheapest total by date (rows: outbound, columns: return)", header_style="bold")
        m.add_column("")
        for b in backs:
            m.add_column(fmt.day(b), justify="right")
        for o in outs:
            cells = []
            for b in backs:
                v = res.date_matrix.get((o, b))
                cells.append("" if v is None else (f"[bold green]{fmt.money(v)}[/]" if v == cheapest else fmt.money(v)))
            m.add_row(fmt.day(o), *cells)
        console.print(m)

    h = Table(title="Best leg per airport", header_style="bold")
    h.add_column("Airport")
    h.add_column("Outbound (getting there + flight)", justify="right")
    h.add_column("Return (flight + getting home)", justify="right")
    for label in sorted(set(res.best_out_by_hub) | set(res.best_back_by_hub),
                        key=lambda c: (res.best_out_by_hub.get(c) or res.best_back_by_hub[c]).score):
        o, b = res.best_out_by_hub.get(label), res.best_back_by_hub.get(label)
        hub = (o or b).hub
        h.add_row(f"{hub.code} {hub.name}" + (" (fly there)" if hub.via_flight else ""),
                  f"{fmt.money(o.total)} ({fmt.day(o.flight.departure)})" if o else "–",
                  f"{fmt.money(b.total)} ({fmt.day(b.flight.departure)})" if b else "–")
    console.print(h)

    for i, t in enumerate(res.trips[:links], 1):
        console.print(f"[bold]#{i} booking links[/]")
        for leg in (t.out, t.back):
            f, b, hop = leg.flight, leg.bus, leg.hop
            bus = f"  🚌 {b.from_city} → {b.to_city} {b.departure:%d %b}: {b.booking_url}" if b else None
            flight = f"  ✈ {f.origin} → {f.destination} {f.departure:%d %b}: {f.booking_url}"
            hop_line = f"  ✈ {hop.origin} → {hop.destination} {hop.departure:%d %b}: {hop.booking_url}" if hop else None
            for line in ([bus, hop_line, flight] if leg.direction == "out" else [flight, hop_line, bus]):
                if line:
                    console.print(line, soft_wrap=True, highlight=False)
    console.print("[dim]Prices are live snapshots (Kiwi.com, Ryanair, Wizz Air, FlixBus) and change quickly; "
                  "separate tickets = no protection if one leg is late. ≈ = estimated arrival time.[/]")


def to_json(res: PlanResult) -> dict:
    def leg(l: Leg) -> dict:
        d = {
            "hub": l.hub.code,
            "via_flight": l.hub.via_flight,
            "total": l.total,
            "flight": asdict(l.flight),
            "hop": asdict(l.hop) if l.hop else None,
            "bus": asdict(l.bus) if l.bus else None,
            "ground_cost": l.ground_cost,
            "transfer_minutes": l.ground_minutes,
            "wait_minutes": int(l.wait.total_seconds() // 60),
            "overnight": l.overnight,
            "overnight_cost": l.overnight_cost,
        }
        return d

    def trip(t: Trip) -> dict:
        return {"total": t.total, "nights": t.nights, "out": leg(t.out), "back": leg(t.back)}

    return {
        "destination": res.destination,
        "trips": [trip(t) for t in res.trips],
        "baseline": trip(res.baseline) if res.baseline else None,
        "date_matrix": [{"out": o.isoformat(), "back": b.isoformat(), "total": v} for (o, b), v in sorted(res.date_matrix.items())],
        "warnings": res.warnings,
    }


# ------------------------------------------------------------------ commands

def cmd_search(args, console: Console) -> int:
    if args.back_date < args.out_date:
        console.print("[red]Return date is before the outbound date.[/]")
        return 2
    if not 0 <= args.flex <= 3:
        console.print("[red]--flex must be between 0 and 3.[/]")
        return 2
    config = load_config(args.config)
    overrides = {k: v for k, v in (("max_wait_hours", args.max_wait), ("overnight_cost", args.overnight_cost),
                                              ("wait_cost_per_hour", args.wait_cost), ("hop_max_price", args.hop_max),
                                              ("self_transfer_min", args.self_transfer)) if v is not None}
    if args.no_direct:
        overrides["direct_airlines"] = False
    if overrides:
        config = replace(config, settings=replace(config.settings, **overrides))
    q = Query(
        destination=args.destination.strip().upper() if len(args.destination.strip()) == 3 else args.destination.strip(),
        out_date=args.out_date,
        back_date=args.back_date,
        flex=args.flex,
        adults=args.adults,
        max_stops=0 if args.direct else args.max_stops,
        cabin_bag=args.cabin_bag,
        checked_bag=args.checked_bag,
        min_nights=args.min_nights,
        top=args.top,
    )
    err = Console(stderr=True)
    with err.status("Searching …") as status:
        res = asyncio.run(find_trips(q, config, args.hubs, args.exclude, use_cache=not args.no_cache,
                                     progress=lambda m: status.update(m)))
    if args.json:
        print(json.dumps(to_json(res), indent=2, default=str))
    else:
        render(console, q, res, args.links)
        s = config.settings
        if s.wait_cost_per_hour:
            console.print(f"[dim]Ranked by price + {fmt.money(s.wait_cost_per_hour)}/h for waits over {s.free_wait_hours:g}h "
                          f"(totals shown are what you pay).[/]")
    return 0 if res.trips else 1


def cmd_hubs(args, console: Console) -> int:
    config = load_config(args.config)
    s = config.settings
    t = Table(title=f"Airports reachable from {s.home}", header_style="bold")
    t.add_column("Code")
    t.add_column("Airport")
    t.add_column("How you get there")
    t.add_column("On", justify="center")
    for hub in config.hubs.values():
        ways = []
        if hub.home_transfer:
            ways.append(f"home transfer {hub.home_transfer.minutes}m / {fmt.money(hub.home_transfer.cost)}")
        for st in hub.stops:
            ways.append(f"FlixBus to '{st.flixbus}' + {st.transfer_min}m / {fmt.money(st.transfer_cost)} to terminal")
        t.add_row(hub.code, hub.name, "\n".join(ways), "✓" if hub.enabled else "–")
    console.print(t)
    console.print(f"Check-in buffer {s.checkin_buffer_min}m · after landing {s.arrival_buffer_min}m · "
                  f"max wait {s.max_wait_hours}h · overnight cost {fmt.money(s.overnight_cost)} · "
                  f"wait cost {fmt.money(s.wait_cost_per_hour)}/h beyond {s.free_wait_hours}h · "
                  f"first hops under {fmt.money(s.hop_max_price)} (≥{s.self_transfer_min}m connection) · "
                  f"direct Ryanair/Wizz prices {'on' if s.direct_airlines else 'off'}")
    return 0


def cmd_init_config(args, console: Console) -> int:
    if USER_CONFIG.exists():
        console.print(f"{USER_CONFIG} already exists — edit it directly.")
        return 1
    USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    USER_CONFIG.write_text(default_config_text(), encoding="utf-8")
    console.print(f"Wrote {USER_CONFIG}")
    return 0


def cmd_serve(args, console: Console) -> int:
    import uvicorn

    from .web.app import create_app

    console.print(f"Open http://{args.host}:{args.port}")
    uvicorn.run(create_app(args.config), host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()
    handlers = {"search": cmd_search, "hubs": cmd_hubs, "init-config": cmd_init_config, "serve": cmd_serve}
    try:
        return handlers[args.cmd](args, console)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        return 2


if __name__ == "__main__":
    sys.exit(main())
