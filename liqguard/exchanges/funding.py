"""
Live funding rates from public, unauthenticated endpoints. Nothing is hardcoded:
rates, intervals and next settlement times all come from the venue.

- Binance USDⓈ-M: GET /fapi/v1/premiumIndex (lastFundingRate, nextFundingTime,
  markPrice for every symbol) + GET /fapi/v1/fundingInfo (fundingIntervalHours
  for symbols whose interval differs from the 8 h default, e.g. 4 h).
- Hyperliquid: POST /info {"type": "metaAndAssetCtxs"} → [meta, asset contexts];
  `funding` is the hourly rate and settlement is every hour on the hour.
  Also yields each asset's maxLeverage (maintenance = 1 / (2 × maxLeverage)).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from liqguard.models import FundingInfo, Venue

logger = logging.getLogger(__name__)

BINANCE_FUTURES_URL = "https://fapi.binance.com"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
BINANCE_DEFAULT_INTERVAL_HOURS = 8.0
HYPERLIQUID_INTERVAL_HOURS = 1.0


def _ms(ts) -> datetime:
    return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)


class BinanceFundingFeed:
    venue = Venue.BINANCE

    def __init__(self, base_url: str = BINANCE_FUTURES_URL, timeout: float = 15.0):
        self.base_url = base_url
        self.timeout = timeout

    async def fetch(self) -> dict[str, FundingInfo]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            premium = await client.get(f"{self.base_url}/fapi/v1/premiumIndex")
            premium.raise_for_status()
            intervals: dict[str, float] = {}
            try:
                info = await client.get(f"{self.base_url}/fapi/v1/fundingInfo")
                info.raise_for_status()
                intervals = {
                    row["symbol"]: float(row["fundingIntervalHours"])
                    for row in info.json() if row.get("fundingIntervalHours")
                }
            except (httpx.HTTPError, ValueError, KeyError) as e:
                logger.warning("Binance fundingInfo unavailable (%s); using the 8 h default interval", type(e).__name__)

        now = datetime.now(timezone.utc)
        out: dict[str, FundingInfo] = {}
        for row in premium.json():
            try:
                symbol = row["symbol"]
                next_time = int(row.get("nextFundingTime") or 0)
                if not next_time:
                    continue
                out[symbol] = FundingInfo(
                    venue=Venue.BINANCE,
                    symbol=symbol,
                    rate=float(row["lastFundingRate"]),
                    interval_hours=intervals.get(symbol, BINANCE_DEFAULT_INTERVAL_HOURS),
                    next_funding_time=_ms(next_time),
                    mark_price=float(row["markPrice"]) if row.get("markPrice") else None,
                    fetched_at=now,
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out


class HyperliquidFundingFeed:
    venue = Venue.HYPERLIQUID

    def __init__(self, url: str = HYPERLIQUID_INFO_URL, timeout: float = 15.0):
        self.url = url
        self.timeout = timeout
        self.max_leverage: dict[str, float] = {}

    async def fetch(self) -> dict[str, FundingInfo]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.url, json={"type": "metaAndAssetCtxs"})
            response.raise_for_status()
            meta, contexts = response.json()

        now = datetime.now(timezone.utc)
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        out: dict[str, FundingInfo] = {}
        leverage: dict[str, float] = {}
        for asset, ctx in zip(meta.get("universe", []), contexts):
            try:
                name = asset["name"]
                if asset.get("maxLeverage"):
                    leverage[name] = float(asset["maxLeverage"])
                out[name] = FundingInfo(
                    venue=Venue.HYPERLIQUID,
                    symbol=name,
                    rate=float(ctx["funding"]),
                    interval_hours=HYPERLIQUID_INTERVAL_HOURS,
                    next_funding_time=next_hour,
                    mark_price=float(ctx["markPx"]) if ctx.get("markPx") else None,
                    fetched_at=now,
                )
            except (KeyError, TypeError, ValueError):
                continue
        self.max_leverage = leverage
        return out


def funding_key(venue: Venue | str, symbol: str) -> tuple[str, str]:
    v = venue.value if isinstance(venue, Venue) else venue
    return v, symbol.removesuffix("-PERP")


def lookup(table: dict[tuple[str, str], FundingInfo], venue: Venue | str, symbol: str) -> Optional[FundingInfo]:
    return table.get(funding_key(venue, symbol))
