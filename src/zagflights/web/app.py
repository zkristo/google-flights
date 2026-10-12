"""Local web page: `zagflights serve` → http://127.0.0.1:8000."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .. import fmt
from ..cli import to_json
from ..config import load_config
from ..planner import Query
from ..providers import ProviderError
from ..service import find_trips

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _int(v: str | None, default: int | None) -> int | None:
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


def _float(v: str | None, default: float | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


def create_app(config_path: str | None = None) -> FastAPI:
    app = FastAPI(title="zagflights")
    base_config = load_config(config_path)

    async def run(params) -> tuple[dict, object | None, str | None]:
        """Parse the query string into a form state, and run the search if it is complete."""
        hubs_all = list(base_config.hubs.values())
        selected = params.getlist("hub") or [h.code for h in hubs_all if h.enabled]
        today = date.today()
        form = {
            "dest": (params.get("dest") or "").strip(),
            "out": params.get("out") or (today + timedelta(days=30)).isoformat(),
            "back": params.get("back") or (today + timedelta(days=35)).isoformat(),
            "flex": _int(params.get("flex"), 2),
            "adults": _int(params.get("adults"), 1),
            "direct": params.get("direct") == "1",
            "cabin_bag": params.get("cabin_bag") == "1",
            "checked_bag": params.get("checked_bag") == "1",
            "max_wait": _float(params.get("max_wait"), base_config.settings.max_wait_hours),
            "overnight_cost": _float(params.get("overnight_cost"), base_config.settings.overnight_cost),
            "wait_cost": _float(params.get("wait_cost"), base_config.settings.wait_cost_per_hour),
            "hop_max": _float(params.get("hop_max"), base_config.settings.hop_max_price),
            # checkbox: absent on a submitted form means "off"; on first load use the config default
            "airline_direct": (params.get("airline_direct") == "1") if params.get("dest") else base_config.settings.direct_airlines,
            "hubs": hubs_all,
            "selected": selected,
        }
        if not form["dest"]:
            return form, None, None
        try:
            out_d, back_d = date.fromisoformat(form["out"]), date.fromisoformat(form["back"])
        except ValueError:
            return form, None, "Dates must be YYYY-MM-DD."
        if back_d < out_d:
            return form, None, "The return date is before the outbound date."
        dest = form["dest"].upper() if len(form["dest"]) == 3 else form["dest"]
        q = Query(destination=dest, out_date=out_d, back_date=back_d, flex=max(0, min(form["flex"], 3)),
                  adults=max(1, min(form["adults"], 9)), max_stops=0 if form["direct"] else None,
                  cabin_bag=form["cabin_bag"], checked_bag=form["checked_bag"], top=12)
        config = replace(base_config, settings=replace(base_config.settings, max_wait_hours=form["max_wait"],
                                                       overnight_cost=form["overnight_cost"],
                                                       wait_cost_per_hour=form["wait_cost"],
                                                       hop_max_price=max(0.0, form["hop_max"]),
                                                       direct_airlines=form["airline_direct"]))
        try:
            res = await find_trips(q, config, only_hubs=selected)
        except (ValueError, ProviderError) as exc:
            return form, None, str(exc)
        return form, (q, res), None

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        form, result, error = await run(request.query_params)
        q, res = result if result else (None, None)
        matrix = None
        if res and len(res.date_matrix) > 1:
            outs = sorted({o for o, _ in res.date_matrix})
            backs = sorted({b for _, b in res.date_matrix})
            matrix = {"outs": outs, "backs": backs, "cells": res.date_matrix, "min": min(res.date_matrix.values())}
        return TEMPLATES.TemplateResponse(request, "index.html", {
            "form": form, "q": q, "res": res, "error": error, "matrix": matrix, "fmt": fmt,
        })

    @app.get("/api/search")
    async def api_search(request: Request):
        _form, result, error = await run(request.query_params)
        if error or not result:
            return JSONResponse({"error": error or "dest, out and back are required"}, status_code=400)
        return to_json(result[1])

    return app
