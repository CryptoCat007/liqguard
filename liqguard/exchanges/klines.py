"""
Public kline (candlestick) sources for the wick stress test, with request
weights accounted against a per-venue budget.

- Binance USDⓈ-M: GET /fapi/v1/klines (≤ 1500 candles per request; weight 1/2/5/10
  by `limit`, IP budget 2400/min). 30 days of 1m candles is 29 requests.
- Hyperliquid: POST /info {"type": "candleSnapshot"} (weight 20 plus 1 per 60
  candles returned; IP budget 1200/min). Only the most recent 5000 candles per
  interval are served (about 3.5 days of 1m, 17 days of 5m), so LiqGuard
  fetches 1m and 5m separately and keeps what it has seen in its cache.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable, Optional

import httpx

from liqguard.models import Venue

from .funding import BINANCE_FUTURES_URL, HYPERLIQUID_INFO_URL

logger = logging.getLogger(__name__)

Candle = tuple[int, float, float, float, float]
INTERVAL_MS = {"1m": 60_000, "5m": 300_000}
BINANCE_PAGE = 1500
HYPERLIQUID_MAX_CANDLES = 5000


class RateLimiter:
    """Sliding-window weight budget. `acquire(w)` waits until `w` more weight fits in the last `window` seconds."""

    def __init__(self, budget: int, window: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.budget = budget
        self.window = window
        self._clock = clock
        self._sleep = sleep
        self._spent: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()
        self.waited = 0.0

    def used(self) -> int:
        now = self._clock()
        while self._spent and now - self._spent[0][0] >= self.window:
            self._spent.popleft()
        return sum(w for _, w in self._spent)

    async def acquire(self, weight: int) -> None:
        weight = min(weight, self.budget)
        async with self._lock:
            while self.used() + weight > self.budget:
                wait = self.window - (self._clock() - self._spent[0][0]) + 0.01
                self.waited += wait
                await self._sleep(max(wait, 0.01))
            self._spent.append((self._clock(), weight))


def binance_kline_weight(limit: int) -> int:
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


class BinanceKlineSource:
    venue = Venue.BINANCE
    native_intervals = ("1m",)

    def __init__(self, base_url: str = BINANCE_FUTURES_URL, limiter: Optional[RateLimiter] = None, timeout: float = 20.0):
        self.base_url = base_url
        self.limiter = limiter or RateLimiter(1200)
        self.timeout = timeout

    async def fetch(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        step = INTERVAL_MS[interval]
        out: list[Candle] = []
        cursor = start_ms
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            while cursor < end_ms:
                limit = int(min(BINANCE_PAGE, max(1, (end_ms - cursor) // step + 1)))
                await self.limiter.acquire(binance_kline_weight(limit))
                response = await client.get(f"{self.base_url}/fapi/v1/klines", params={
                    "symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": limit,
                })
                response.raise_for_status()
                rows = response.json()
                if not rows:
                    break
                out.extend((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows)
                last = int(rows[-1][0])
                if len(rows) < limit or last + step <= cursor:
                    break
                cursor = last + step
        return out


class HyperliquidKlineSource:
    venue = Venue.HYPERLIQUID
    native_intervals = ("1m", "5m")

    def __init__(self, url: str = HYPERLIQUID_INFO_URL, limiter: Optional[RateLimiter] = None, timeout: float = 20.0):
        self.url = url
        self.limiter = limiter or RateLimiter(600)
        self.timeout = timeout

    async def fetch(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        expected = min(HYPERLIQUID_MAX_CANDLES, (end_ms - start_ms) // INTERVAL_MS[interval] + 1)
        await self.limiter.acquire(20 + int(expected) // 60)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.url, json={
                "type": "candleSnapshot",
                "req": {"coin": symbol, "interval": interval, "startTime": int(start_ms), "endTime": int(end_ms)},
            })
            response.raise_for_status()
            rows = response.json() or []
        out = []
        for r in rows:
            try:
                out.append((int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out
