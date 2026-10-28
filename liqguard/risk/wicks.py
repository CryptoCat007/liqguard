"""
Wick stress test: how deep do adverse intra-candle moves get, and how many of
them fit between the mark and the liquidation price?

A candle's adverse move is measured from its open: open → low for a long,
open → high for a short. That is the move a position sitting at the open
would have had to survive inside one candle, which is what wipes out stops
and liquidates leveraged positions in a wick.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

from liqguard.models import Position, Side, WickProfile, WickStat

Candle = tuple[int, float, float, float, float]
"""(open time ms, open, high, low, close)"""

INTERVAL_MS = {"1m": 60_000, "5m": 300_000}
TYPICAL_PERCENTILE = 95.0


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (`q` in 0..100); 0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def adverse_moves(candle: Candle) -> tuple[float, float]:
    """(long adverse %, short adverse %) for one candle."""
    _, o, h, low, _ = candle
    if o <= 0:
        return 0.0, 0.0
    return max(0.0, (o - low) / o * 100), max(0.0, (h - o) / o * 100)


def aggregate(candles: Iterable[Candle], factor: int, base_interval_ms: int = 60_000) -> list[Candle]:
    """Build `factor`× candles (e.g. 5m from 1m) aligned to the bigger interval's boundaries."""
    size = base_interval_ms * factor
    out: list[Candle] = []
    bucket: Optional[list] = None
    for t, o, h, low, c in sorted(candles):
        start = t - t % size
        if bucket is None or bucket[0] != start:
            if bucket is not None:
                out.append(tuple(bucket))
            bucket = [start, o, h, low, c]
        else:
            bucket[2] = max(bucket[2], h)
            bucket[3] = min(bucket[3], low)
            bucket[4] = c
    if bucket is not None:
        out.append(tuple(bucket))
    return out


def wick_stat(candles: Sequence[Candle], interval: str) -> Optional[WickStat]:
    if not candles:
        return None
    longs, shorts = [], []
    long_max, short_max = (0.0, None), (0.0, None)
    for candle in candles:
        lo, hi = adverse_moves(candle)
        longs.append(lo)
        shorts.append(hi)
        if lo > long_max[0]:
            long_max = (lo, candle[0])
        if hi > short_max[0]:
            short_max = (hi, candle[0])
    span_ms = candles[-1][0] - candles[0][0] + INTERVAL_MS.get(interval, 60_000)

    def at(ms):
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms is not None else None

    return WickStat(
        interval=interval,
        candles=len(candles),
        window_days=span_ms / 86_400_000,
        long_typical_pct=percentile(longs, TYPICAL_PERCENTILE),
        long_max_pct=long_max[0],
        short_typical_pct=percentile(shorts, TYPICAL_PERCENTILE),
        short_max_pct=short_max[0],
        long_max_at=at(long_max[1]),
        short_max_at=at(short_max[1]),
    )


@dataclass(frozen=True)
class WickExposure:
    """How many wicks of one interval fit between the mark and the liquidation price."""

    interval: str
    typical_pct: float
    max_pct: float
    typical_wicks_away: Optional[float]
    max_wicks_away: Optional[float]
    window_days: float

    @property
    def one_max_wick_liquidates(self) -> bool:
        return self.max_wicks_away is not None and self.max_wicks_away < 1

    def to_dict(self) -> dict:
        def r(x):
            return None if x is None else round(x, 2)
        return {
            "interval": self.interval,
            "typical_pct": round(self.typical_pct, 4),
            "max_pct": round(self.max_pct, 4),
            "typical_wicks_away": r(self.typical_wicks_away),
            "max_wicks_away": r(self.max_wicks_away),
            "window_days": round(self.window_days, 2),
            "one_max_wick_liquidates": self.one_max_wick_liquidates,
        }


def wicks_away(distance_pct: float, wick_pct: float) -> Optional[float]:
    if distance_pct is None or math.isinf(distance_pct) or wick_pct <= 0:
        return None
    return max(distance_pct, 0.0) / wick_pct


def wick_exposure(position: Position, distance_pct: float, profile: Optional[WickProfile]) -> list[WickExposure]:
    """Per interval: typical (p95) and max adverse wick for this side, and how many fit before liquidation."""
    if profile is None:
        return []
    long = position.side == Side.LONG
    out = []
    for interval in ("1m", "5m"):
        stat = profile.stat(interval)
        if stat is None:
            continue
        typical, worst = stat.typical_pct(long), stat.max_pct(long)
        out.append(WickExposure(
            interval, typical, worst, wicks_away(distance_pct, typical), wicks_away(distance_pct, worst), stat.window_days
        ))
    return out


def stress_summary(position: Position, distance_pct: float, profile: Optional[WickProfile]) -> Optional[dict]:
    exposures = wick_exposure(position, distance_pct, profile)
    if not exposures:
        return None
    worst = min((e for e in exposures if e.max_wicks_away is not None), key=lambda e: e.max_wicks_away, default=None)
    return {
        "source": profile.source,
        "exposures": [e.to_dict() for e in exposures],
        "worst_interval": worst.interval if worst else None,
        "max_wicks_away": round(worst.max_wicks_away, 2) if worst else None,
        "one_max_wick_liquidates": bool(worst and worst.one_max_wick_liquidates),
    }
