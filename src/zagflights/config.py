"""Hub configuration: which airports to consider and how to reach them from home."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class Stop:
    """A FlixBus city near an airport, plus the transfer between that stop and the terminal."""

    flixbus: str
    transfer_min: int = 0
    transfer_cost: float = 0.0


@dataclass(frozen=True)
class HomeTransfer:
    minutes: int
    cost: float


@dataclass(frozen=True)
class Hub:
    code: str
    name: str
    stops: tuple[Stop, ...] = ()
    home_transfer: HomeTransfer | None = None
    enabled: bool = True
    via_flight: bool = False  # a "first hop" airport you fly to from home, not a bus hub

    @property
    def label(self) -> str:
        return f"{self.code} ✈" if self.via_flight else self.code


@dataclass(frozen=True)
class Settings:
    home: str = "Zagreb"
    checkin_buffer_min: int = 120
    arrival_buffer_min: int = 45
    max_wait_hours: float = 7
    overnight_cost: float = 0.0
    wait_cost_per_hour: float = 0.0
    free_wait_hours: float = 3.0
    hop_max_price: float = 50.0
    self_transfer_min: int = 180
    direct_airlines: bool = True


@dataclass(frozen=True)
class Config:
    settings: Settings
    hubs: dict[str, Hub] = field(default_factory=dict)

    def select(self, only: list[str] | None = None, exclude: list[str] | None = None) -> list[Hub]:
        codes = [c.upper() for c in only] if only else [c for c, h in self.hubs.items() if h.enabled]
        unknown = [c for c in codes if c not in self.hubs]
        if unknown:
            raise ValueError(f"Unknown hub(s): {', '.join(unknown)}. Known: {', '.join(self.hubs)}")
        skip = {c.upper() for c in exclude or []}
        return [self.hubs[c] for c in codes if c not in skip]

    @property
    def home_hub(self) -> Hub | None:
        """The home airport (the hub reached by a local transfer rather than a bus)."""
        return next((h for h in self.hubs.values() if h.home_transfer), None)


USER_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "zagflights" / "hubs.toml"


def default_config_text() -> str:
    return resources.files("zagflights").joinpath("hubs.toml").read_text(encoding="utf-8")


def parse_config(text: str) -> Config:
    raw = tomllib.loads(text)
    settings = Settings(**raw.get("settings", {}))
    hubs: dict[str, Hub] = {}
    for code, h in raw.get("hubs", {}).items():
        ht = h.get("home_transfer")
        hubs[code.upper()] = Hub(
            code=code.upper(),
            name=h.get("name", code.upper()),
            stops=tuple(Stop(**s) for s in h.get("stops", [])),
            home_transfer=HomeTransfer(**ht) if ht else None,
            enabled=h.get("enabled", True),
        )
    return Config(settings=settings, hubs=hubs)


def load_config(path: str | Path | None = None) -> Config:
    """Load hubs from an explicit path, else ~/.config/zagflights/hubs.toml, else the bundled default."""
    if path:
        return parse_config(Path(path).read_text(encoding="utf-8"))
    if USER_CONFIG.exists():
        return parse_config(USER_CONFIG.read_text(encoding="utf-8"))
    return parse_config(default_config_text())
