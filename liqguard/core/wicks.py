"""
Shared wick statistics (public market data, one cache for all users).

Klines are cached per (venue, symbol, interval) and refreshed incrementally:
after the first 30-day backfill only candles newer than the last cached one
are requested, and candles older than the window are dropped. Refreshes run in
the background, so a user's alert check never waits on kline downloads; until
a symbol's first fetch lands, its tiers simply aren't widened for volatility.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

from liqguard.exchanges.klines import (
    INTERVAL_MS,
    BinanceKlineSource,
    Candle,
    HyperliquidKlineSource,
)
from liqguard.models import Position, Venue, WickProfile
from liqguard.risk.wicks import aggregate, wick_stat

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000


class KlineSource(Protocol):
    venue: Venue
    native_intervals: tuple[str, ...]

    async def fetch(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]: ...


def wick_key(venue: Venue | str, symbol: str) -> tuple[str, str]:
    v = venue.value if isinstance(venue, Venue) else venue
    return v, symbol.removesuffix("-PERP")


class WickService:
    def __init__(
        self,
        sources: Optional[list[KlineSource]] = None,
        days: int = 30,
        refresh_seconds: int = 3600,
        clock: Callable[[], float] = time.time,
    ):
        self.sources = {s.venue.value: s for s in (sources if sources is not None else [BinanceKlineSource(), HyperliquidKlineSource()])}
        self.days = days
        self.refresh_seconds = refresh_seconds
        self.clock = clock
        self._candles: dict[tuple[str, str, str], list[Candle]] = {}
        self._profiles: dict[tuple[str, str], WickProfile] = {}
        self._refreshed: dict[tuple[str, str], float] = {}
        self._errors: dict[tuple[str, str], str] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}

    @property
    def profiles(self) -> dict[tuple[str, str], WickProfile]:
        return dict(self._profiles)

    def profile(self, venue: Venue | str, symbol: str) -> Optional[WickProfile]:
        return self._profiles.get(wick_key(venue, symbol))

    def _due(self, key: tuple[str, str]) -> bool:
        last = self._refreshed.get(key)
        return last is None or self.clock() - last >= self.refresh_seconds

    def request(self, positions: list[Position]) -> dict[tuple[str, str], WickProfile]:
        """Cached profiles for these positions; schedules a background refresh for missing or stale ones."""
        return self.request_keys([wick_key(p.venue, p.symbol) for p in positions])

    def request_keys(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], WickProfile]:
        out = {}
        for key in keys:
            if key[0] not in self.sources:
                continue
            if self._due(key) and not (key in self._tasks and not self._tasks[key].done()):
                try:
                    self._tasks[key] = asyncio.get_running_loop().create_task(self.refresh(*key))
                except RuntimeError:
                    pass
            if key in self._profiles:
                out[key] = self._profiles[key]
        return out

    async def refresh(self, venue: str, symbol: str) -> Optional[WickProfile]:
        key = (venue, symbol)
        source = self.sources.get(venue)
        if source is None:
            return None
        self._refreshed[key] = self.clock()
        now_ms = int(self.clock() * 1000)
        window_start = now_ms - self.days * DAY_MS
        try:
            stats = {}
            for interval in source.native_intervals:
                candles = await self._update(source, symbol, interval, window_start, now_ms)
                stat = wick_stat(candles, interval)
                if stat:
                    stats[interval] = stat
            if "5m" not in stats and "1m" in stats:
                five = aggregate(self._candles[(venue, symbol, "1m")], 5)
                stat = wick_stat(five, "5m")
                if stat:
                    stats["5m"] = stat
        except Exception as e:
            response = getattr(e, "response", None)
            self._errors[key] = f"HTTP {response.status_code}" if response is not None else type(e).__name__
            logger.warning("Wick refresh for %s/%s failed (%s); keeping last data", venue, symbol, self._errors[key])
            return self._profiles.get(key)
        if not stats:
            return self._profiles.get(key)
        profile = WickProfile(Venue(venue), symbol, stats, datetime.fromtimestamp(self.clock(), tz=timezone.utc))
        self._profiles[key] = profile
        self._errors.pop(key, None)
        return profile

    async def _update(self, source: KlineSource, symbol: str, interval: str, window_start: int, now_ms: int) -> list[Candle]:
        ck = (source.venue.value, symbol, interval)
        cached = [c for c in self._candles.get(ck, []) if c[0] >= window_start]
        start = cached[-1][0] + INTERVAL_MS[interval] if cached else window_start
        if start <= now_ms:
            fresh = await source.fetch(symbol, interval, start, now_ms)
            seen = {c[0] for c in cached}
            cached.extend(c for c in fresh if c[0] not in seen and c[0] >= window_start)
            cached.sort()
        self._candles[ck] = cached
        return cached

    def cached_candles(self, venue: str, symbol: str, interval: str) -> int:
        return len(self._candles.get((venue, symbol, interval), []))

    def status(self) -> dict:
        out: dict[str, dict] = {}
        for venue in self.sources:
            keys = [k for k in set(self._profiles) | set(self._errors) if k[0] == venue]
            out[venue] = {
                "symbols": sum(1 for k in self._profiles if k[0] == venue),
                "errors": {k[1]: self._errors[k] for k in keys if k in self._errors},
            }
        return out

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
