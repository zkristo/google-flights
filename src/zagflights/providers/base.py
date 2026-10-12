"""Shared plumbing for the small JSON APIs: one httpx client, retries, a concurrency cap and
de-duplication of identical in-flight requests."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx

from ..cache import Cache
from .kiwi import ProviderError

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


class HttpClient:
    name = "http"

    def __init__(self, cache: Cache | None = None, http: httpx.AsyncClient | None = None, concurrency: int = 4):
        self.cache = cache or Cache(enabled=False)
        self._http = http
        self._own_http = http is None
        self._sem = asyncio.Semaphore(concurrency)
        self._inflight: dict[Any, asyncio.Future] = {}
        self.calls = 0

    async def __aenter__(self):
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True,
                                           headers={"User-Agent": BROWSER_UA})
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        assert self._http is not None, f"use `async with {type(self).__name__}()`"
        last: Exception | None = None
        for attempt in range(3):
            try:
                async with self._sem:
                    self.calls += 1
                    resp = await self._http.request(method, url, **kwargs)
                if resp.status_code in (500, 502, 503, 504):
                    raise ProviderError(f"{self.name} HTTP {resp.status_code}")
                if resp.status_code >= 400:  # 4xx (incl. 429 bot protection): retrying won't help
                    raise ProviderError(f"{self.name} {url.split('?')[0]} → HTTP {resp.status_code}")
                return resp
            except httpx.TransportError as exc:
                last = exc
            except ProviderError as exc:
                if "HTTP 5" not in str(exc):
                    raise
                last = exc
            await asyncio.sleep(1.0 * (attempt + 1))
        raise ProviderError(f"{self.name} request failed: {last}")

    async def dedup(self, key: Any, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Run ``factory`` once for concurrent callers asking for the same ``key``."""
        fut = self._inflight.get(key)
        if fut is None:
            fut = asyncio.ensure_future(factory())
            self._inflight[key] = fut
        return await fut

    async def cached(self, namespace: str, key: Any, ttl: float, fetch: Callable[[], Awaitable[Any]]) -> Any:
        """Disk cache + in-flight de-duplication around ``fetch`` (which returns JSON-able data)."""
        hit = self.cache.get(namespace, key, ttl)
        if hit is not None:
            return hit

        async def run():
            value = await fetch()
            self.cache.set(namespace, key, value)
            return value

        return await self.dedup((namespace, repr(key)), run)
