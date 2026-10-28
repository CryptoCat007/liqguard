"""Shared, periodically refreshed public market data (funding rates).

Public data only: one cache for all users is safe and nothing user-specific
ever enters it. A failed refresh keeps the last good data (marked stale) rather
than dropping funding from the risk math.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Protocol

from liqguard.exchanges.funding import BinanceFundingFeed, HyperliquidFundingFeed, funding_key
from liqguard.models import FundingInfo, Position, Venue

logger = logging.getLogger(__name__)


class FundingFeed(Protocol):
    venue: Venue

    async def fetch(self) -> dict[str, FundingInfo]: ...


class MarketDataService:
    def __init__(self, feeds: Optional[list[FundingFeed]] = None, refresh_seconds: int = 60):
        self.feeds: list[FundingFeed] = feeds if feeds is not None else [BinanceFundingFeed(), HyperliquidFundingFeed()]
        self.refresh_seconds = refresh_seconds
        self._funding: dict[tuple[str, str], FundingInfo] = {}
        self._updated: dict[str, float] = {}
        self._errors: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._attempted = float("-inf")
        self._task: Optional[asyncio.Task] = None

    @property
    def funding(self) -> dict[tuple[str, str], FundingInfo]:
        return dict(self._funding)

    def funding_for(self, position: Position) -> Optional[FundingInfo]:
        venue = position.venue.value if isinstance(position.venue, Venue) else str(position.venue)
        return self._funding.get(funding_key(venue, position.symbol))

    def is_stale(self, venue: str) -> bool:
        updated = self._updated.get(venue)
        return updated is None or time.monotonic() - updated > 3 * self.refresh_seconds

    def status(self) -> dict:
        return {
            feed.venue.value: {
                "symbols": sum(1 for k in self._funding if k[0] == feed.venue.value),
                "stale": self.is_stale(feed.venue.value),
                "error": self._errors.get(feed.venue.value),
            }
            for feed in self.feeds
        }

    async def refresh(self) -> None:
        async with self._lock:
            self._attempted = time.monotonic()
            for feed in self.feeds:
                venue = feed.venue.value
                try:
                    rows = await feed.fetch()
                except Exception as e:
                    self._errors[venue] = type(e).__name__
                    logger.warning("Funding refresh for %s failed (%s); keeping last data", venue, type(e).__name__)
                    continue
                if not rows:
                    continue
                for key in [k for k in self._funding if k[0] == venue]:
                    del self._funding[key]
                for symbol, info in rows.items():
                    self._funding[funding_key(venue, symbol)] = info
                self._updated[venue] = time.monotonic()
                self._errors.pop(venue, None)

    async def ensure_fresh(self) -> None:
        if self._lock.locked() or time.monotonic() - self._attempted < 10:
            return
        if any(self.is_stale(feed.venue.value) for feed in self.feeds):
            await self.refresh()

    async def _loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.refresh_seconds)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
