"""
Demo market data for the simulation and the demo book, all labelled "demo".
Live accounts use wick statistics computed from real klines instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from liqguard.models import STOP_MARKET, OpenOrder, Side, Venue, WickProfile, WickStat

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _profile(venue: Venue, symbol: str, one: tuple[float, float, float, float], five: tuple[float, float, float, float]) -> WickProfile:
    """`one` / `five`: (long typical, long max, short typical, short max) in %."""
    return WickProfile(
        venue=venue,
        symbol=symbol,
        stats={
            "1m": WickStat("1m", 43_200, 30.0, *one),
            "5m": WickStat("5m", 8_640, 30.0, *five),
        },
        fetched_at=_EPOCH,
        source="demo",
    )


# SOL has the violent wicks (a 5.8% 1m and 11.5% 5m flush in the window), so its
# tiers widen for volatility on top of the weekend/thin-hours factor.
DEMO_WICKS: dict[str, WickProfile] = {
    "BTCUSDT": _profile(Venue.BINANCE, "BTCUSDT", (0.18, 1.9, 0.17, 1.6), (0.42, 3.6, 0.40, 3.1)),
    "ETH": _profile(Venue.HYPERLIQUID, "ETH", (0.24, 2.4, 0.22, 2.6), (0.58, 4.4, 0.55, 4.9)),
    "ETHUSDT": _profile(Venue.BINANCE, "ETHUSDT", (0.24, 2.4, 0.22, 2.6), (0.58, 4.4, 0.55, 4.9)),
    "SOLUSDT": _profile(Venue.BINANCE, "SOLUSDT", (0.35, 5.8, 0.33, 4.1), (0.85, 11.5, 0.80, 7.9)),
    "TSLAUSDT": _profile(Venue.BINANCE, "TSLAUSDT", (0.12, 1.4, 0.12, 1.2), (0.30, 2.9, 0.28, 2.4)),
    "DOGEUSDT": _profile(Venue.BINANCE, "DOGEUSDT", (0.32, 4.6, 0.30, 3.8), (0.78, 9.2, 0.74, 7.1)),
    "AVAXUSDT": _profile(Venue.BINANCE, "AVAXUSDT", (0.30, 4.1, 0.29, 3.5), (0.74, 8.4, 0.70, 6.6)),
}

# BTC's stop sits below its liquidation price from the start; ETH has no stop;
# SOL's stop is fine at first but BTC's losses drag SOL's liquidation price up
# through it; TSLA's stop is fine.
DEMO_SIM_ORDERS: tuple[OpenOrder, ...] = (
    OpenOrder(Venue.BINANCE, "BTCUSDT", Side.SHORT, STOP_MARKET, trigger_price=45_000.0, close_position=True, order_id="demo-1"),
    OpenOrder(Venue.BINANCE, "SOLUSDT", Side.SHORT, STOP_MARKET, trigger_price=128.0, quantity=150.0, reduce_only=True, order_id="demo-2"),
    OpenOrder(Venue.BINANCE, "TSLAUSDT", Side.SHORT, STOP_MARKET, trigger_price=236.0, quantity=40.0, reduce_only=True, order_id="demo-3"),
)


# "5 longs, one wallet": BTC's stop starts far above its (displayed) liquidation
# price, but as all five coins fall together the shared wallet drains and the
# liquidation price climbs through it. The other four have no stop at all.
CROSS_LONGS_ORDERS: tuple[OpenOrder, ...] = (
    OpenOrder(Venue.BINANCE, "BTCUSDT", Side.SHORT, STOP_MARKET, trigger_price=55_500.0, close_position=True, order_id="demo-11"),
)


def demo_clock_start(now: datetime) -> datetime:
    """Most recent Saturday 20:00 UTC: the run covers a weekend night through the thin 21:00–01:00 UTC hours."""
    base = now.astimezone(timezone.utc).replace(hour=20, minute=0, second=0, microsecond=0)
    base -= timedelta(days=(base.weekday() - 5) % 7)
    if base > now:
        base -= timedelta(days=7)
    return base
