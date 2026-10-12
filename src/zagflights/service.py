"""Wire config + providers + planner together (used by both the CLI and the web app)."""

from __future__ import annotations

from typing import Callable

from .cache import DEFAULT_DIR, Cache
from .config import Config
from .models import PlanResult
from .planner import Planner, Query
from .providers import FlixBusClient, KiwiClient


async def find_trips(
    query: Query,
    config: Config,
    only_hubs: list[str] | None = None,
    exclude_hubs: list[str] | None = None,
    use_cache: bool = True,
    progress: Callable[[str], None] | None = None,
) -> PlanResult:
    hubs = config.select(only_hubs, exclude_hubs)
    if not hubs:
        raise ValueError("No airports selected.")
    cache = Cache(DEFAULT_DIR, enabled=use_cache)
    async with KiwiClient(cache=cache) as kiwi, FlixBusClient(cache=cache) as flix:
        planner = Planner(config, hubs, kiwi, flix, progress=progress)
        return await planner.run(query)
