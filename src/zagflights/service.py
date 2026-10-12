"""Wire config + providers + planner together (used by both the CLI and the web app)."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Callable

from .cache import DEFAULT_DIR, Cache
from .config import Config
from .models import PlanResult
from .planner import Planner, Query
from .pricing import DirectFares
from .providers import FlixBusClient, KiwiClient
from .providers.fx import FxRates
from .providers.ryanair import RyanairClient
from .providers.wizz import WizzClient


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
    async with AsyncExitStack() as stack:
        kiwi = await stack.enter_async_context(KiwiClient(cache=cache))
        flix = await stack.enter_async_context(FlixBusClient(cache=cache))
        direct = None
        if config.settings.direct_airlines:
            fx = await stack.enter_async_context(FxRates(cache=cache))
            ryanair = await stack.enter_async_context(RyanairClient(cache=cache))
            wizz = await stack.enter_async_context(WizzClient(fx, cache=cache))
            direct = DirectFares(ryanair, wizz)
        planner = Planner(config, hubs, kiwi, flix, direct=direct, progress=progress)
        return await planner.run(query)
