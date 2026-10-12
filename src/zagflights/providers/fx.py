"""Euro exchange rates from the European Central Bank's daily reference rates."""

from __future__ import annotations

import re

from .base import HttpClient

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FX_TTL = 24 * 60 * 60
FIXED = {"EUR": 1.0, "BAM": 1.95583}  # BAM is pegged to the euro; ECB doesn't list it


def parse_ecb(xml: str) -> dict[str, float]:
    rates = {cur: float(rate) for cur, rate in re.findall(r"currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([\d.]+)['\"]", xml)}
    return {**rates, **FIXED}


class FxRates(HttpClient):
    name = "ECB"

    async def rates(self) -> dict[str, float]:
        async def fetch():
            resp = await self.request("GET", ECB_URL)
            return parse_ecb(resp.text)

        return await self.cached("fx", "ecb-daily", FX_TTL, fetch)

    async def to_eur(self, amount: float, currency: str) -> float | None:
        """Convert ``amount`` in ``currency`` to EUR, or None if the ECB has no rate (e.g. RSD)."""
        currency = currency.upper()
        if currency == "EUR":
            return round(amount, 2)
        rate = (await self.rates()).get(currency)
        return round(amount / rate, 2) if rate else None
