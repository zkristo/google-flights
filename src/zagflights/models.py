"""Plain data objects shared by providers, the planner and the renderers.

All datetimes are naive *local wall-clock* times at the place they happen. That is safe for
our comparisons because a bus and the flight it connects to are always in the same city.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import Hub, Stop


@dataclass(frozen=True)
class Flight:
    id: str
    origin: str
    destination: str
    origin_name: str
    destination_name: str
    departure: datetime
    arrival: datetime
    price: float
    carriers: tuple[str, ...]
    flight_numbers: tuple[str, ...]
    stops: int
    booking_url: str
    route: tuple[str, ...] = ()

    @property
    def key(self) -> tuple:
        """Identity independent of which search returned it (dedupes VCE/TSF overlap etc.)."""
        return (self.origin, self.destination, self.flight_numbers, self.departure)


@dataclass(frozen=True)
class BusRide:
    uid: str
    from_city: str
    to_city: str
    from_station: str
    to_station: str
    departure: datetime
    arrival: datetime
    price: float
    transfers: int
    booking_url: str


@dataclass(frozen=True)
class Leg:
    """One direction of the trip: home → destination ("out") or destination → home ("back")."""

    direction: str  # "out" | "back"
    hub: Hub
    flight: Flight
    bus: BusRide | None
    stop: Stop | None
    ground_cost: float  # bus fare + airport transfer (or home transfer for the home airport)
    ground_minutes: int  # airport transfer minutes (bus duration is in `bus`)
    wait: timedelta  # idle time between bus and flight
    overnight: bool
    overnight_cost: float = 0.0
    penalty: float = 0.0  # optional "value of your time" for long waits; ranks but is not money paid

    @property
    def total(self) -> float:
        return round(self.flight.price + self.ground_cost + self.overnight_cost, 2)

    @property
    def score(self) -> float:
        return self.total + self.penalty

    @property
    def flight_date(self) -> date:
        return self.flight.departure.date()

    @property
    def starts(self) -> datetime:
        """When this leg starts (leaving home for "out", leaving the destination for "back")."""
        if self.direction == "out" and self.bus:
            return self.bus.departure
        return self.flight.departure

    @property
    def ends(self) -> datetime:
        if self.direction == "back" and self.bus:
            return self.bus.arrival
        return self.flight.arrival


@dataclass(frozen=True)
class Trip:
    out: Leg
    back: Leg

    @property
    def total(self) -> float:
        return round(self.out.total + self.back.total, 2)

    @property
    def score(self) -> float:
        return self.out.score + self.back.score

    @property
    def nights(self) -> int:
        return (self.back.flight.departure.date() - self.out.flight.arrival.date()).days


@dataclass
class PlanResult:
    destination: str
    trips: list[Trip]
    out_legs: list[Leg]
    back_legs: list[Leg]
    date_matrix: dict[tuple[date, date], float]
    best_out_by_hub: dict[str, Leg]
    best_back_by_hub: dict[str, Leg]
    baseline: Trip | None  # cheapest trip flying from and to the home airport only
    warnings: list[str] = field(default_factory=list)
