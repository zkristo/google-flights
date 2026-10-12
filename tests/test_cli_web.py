from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import flight
from fastapi.testclient import TestClient
from rich.console import Console

from zagflights import cli
from zagflights.config import default_config_text, load_config, parse_config
from zagflights.models import Leg
from zagflights.planner import Query, combine
from zagflights.web import app as web_app


def _result(config):
    zag = config.hubs["ZAG"]
    out = Leg("out", zag, flight("ZAG", "ATH", "2026-11-10T10:00", "2026-11-10T12:00", 50), None, None, 8, 45,
              timedelta(0), False)
    back = Leg("back", zag, flight("ATH", "ZAG", "2026-11-15T10:00", "2026-11-15T12:00", 40), None, None, 8, 45,
               timedelta(0), False)
    q = Query("ATH", date(2026, 11, 10), date(2026, 11, 15))
    return q, combine(q, [out], [back])


def test_default_config_parses():
    cfg = parse_config(default_config_text())
    assert "ZAG" in cfg.hubs and cfg.hubs["ZAG"].home_transfer
    assert any(s.flixbus == "Budapest Airport" for s in cfg.hubs["BUD"].stops)
    assert [h.code for h in cfg.select(["bud", "vce"])] == ["BUD", "VCE"]
    assert "BEG" not in [h.code for h in cfg.select(exclude=["BEG"])]
    assert {"ZAD", "SPU", "OSI"} <= set(cfg.hubs)
    assert any(s.flixbus.startswith("Split Airport") for s in cfg.hubs["SPU"].stops)
    assert cfg.settings.max_wait_hours < 8 and cfg.settings.hop_max_price == 50 and cfg.settings.direct_airlines
    assert cfg.home_hub.code == "ZAG"


def test_select_unknown_hub_raises():
    with pytest.raises(ValueError):
        load_config().select(["XXX"])


def test_render_and_json(config):
    q, res = _result(config)
    console = Console(record=True, width=160)
    cli.render(console, q, res, links=1)
    text = console.export_text()
    assert "€106" in text and "ZAG 10:00 → ATH 12:00" in text and "kiwi.test" in text
    data = cli.to_json(res)
    assert data["trips"][0]["total"] == 106 and data["trips"][0]["out"]["hub"] == "ZAG"


def test_cli_rejects_bad_dates(capsys):
    assert cli.main(["search", "ATH", "2026-11-15", "2026-11-10"]) == 2


def test_web_form_and_results(config, monkeypatch):
    q, res = _result(config)

    async def fake_find(query, cfg, only_hubs=None, exclude_hubs=None, use_cache=True, progress=None):
        assert query.destination == "ATH" and query.flex == 2
        return res

    monkeypatch.setattr(web_app, "find_trips", fake_find)
    client = TestClient(web_app.create_app())
    page = client.get("/")
    assert page.status_code == 200 and "zagflights" in page.text
    assert 'name="airline_direct" value="1" checked' in page.text and 'name="direct" value="1" >' in page.text
    page = client.get("/", params={"dest": "ath", "out": "2026-11-10", "back": "2026-11-15", "flex": "2"})
    assert page.status_code == 200 and "€106" in page.text and "Flight on Kiwi.com" in page.text
    api = client.get("/api/search", params={"dest": "ATH", "out": "2026-11-10", "back": "2026-11-15", "flex": "2"})
    assert api.json()["trips"][0]["total"] == 106
    bad = client.get("/", params={"dest": "ATH", "out": "2026-11-15", "back": "2026-11-10"})
    assert "before the outbound" in bad.text
