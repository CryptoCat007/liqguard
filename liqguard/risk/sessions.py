"""
When is the market thin? Weekends, low-liquidity hours (UTC) and, for stock
perpetuals, the hours the US stock market is closed and the perp trades
without its underlying (it can gap at the open).

Stock perps are detected from the symbol: a Hyperliquid HIP-3 dex prefix
(`xyz:TSLA`) or a known US equity ticker as the base (`TSLAUSDT`). Exchange
holidays are not modelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from liqguard.i18n import Msg

NEW_YORK = ZoneInfo("America/New_York")
US_OPEN, US_CLOSE = time(9, 30), time(16, 0)

WEEKEND_FACTOR = 1.15
LOW_LIQUIDITY_FACTOR = 1.10
STOCK_CLOSED_FACTOR = 1.5
MAX_FACTOR = 1.5
DEFAULT_LOW_LIQUIDITY_HOURS = (21, 1)

US_EQUITY_TICKERS = frozenset({
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AMD", "NFLX", "INTC", "AVGO", "ORCL",
    "COIN", "MSTR", "HOOD", "PLTR", "CRCL", "SPY", "QQQ", "BABA", "UBER", "SHOP", "SNOW", "CRWV", "TSM",
    "BRK.B", "JPM", "DIS", "NKE", "PYPL", "RIVN", "LCID", "SMCI", "ARM", "MU",
})
_QUOTES = ("USDT", "USDC", "USD", "-PERP")


def underlying(symbol: str) -> str:
    """`xyz:TSLA` → TSLA, `TSLAUSDT` → TSLA, `ETH-PERP` → ETH."""
    base = symbol.split(":", 1)[1] if ":" in symbol else symbol
    base = base.upper()
    for suffix in _QUOTES:
        if base.endswith(suffix) and len(base) > len(suffix):
            return base[: -len(suffix)]
    return base


def is_stock_perp(symbol: str) -> bool:
    return underlying(symbol) in US_EQUITY_TICKERS


def us_equity_market_open(now: datetime) -> bool:
    local = now.astimezone(NEW_YORK)
    return local.weekday() < 5 and US_OPEN <= local.time() < US_CLOSE


def _in_hours(hour: int, window: tuple[int, int]) -> bool:
    start, end = window
    return start <= hour < end if start < end else hour >= start or hour < end


@dataclass(frozen=True)
class SessionInfo:
    weekend: bool
    low_liquidity: bool
    stock_perp: bool
    stock_market_closed: bool
    factor: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "weekend": self.weekend,
            "low_liquidity": self.low_liquidity,
            "stock_perp": self.stock_perp,
            "stock_market_closed": self.stock_market_closed,
            "factor": round(self.factor, 3),
            "reasons": self.reasons,
        }


def market_session(now: datetime, symbol: str, low_liquidity_hours: tuple[int, int] = DEFAULT_LOW_LIQUIDITY_HOURS) -> SessionInfo:
    """Session flags at `now` (UTC-aware) and the factor by which alert tiers widen."""
    weekend = now.weekday() >= 5
    thin = _in_hours(now.hour, low_liquidity_hours)
    stock = is_stock_perp(symbol)
    closed = stock and not us_equity_market_open(now)
    factor, reasons = 1.0, []
    if weekend:
        factor *= WEEKEND_FACTOR
        reasons.append(Msg("risk.session.weekend", factor=WEEKEND_FACTOR))
    if thin:
        factor *= LOW_LIQUIDITY_FACTOR
        start, end = low_liquidity_hours
        reasons.append(Msg("risk.session.low_liquidity", start=start, end=end, factor=LOW_LIQUIDITY_FACTOR))
    if closed:
        factor *= STOCK_CLOSED_FACTOR
        reasons.append(Msg("risk.session.stock_closed", underlying=underlying(symbol), factor=STOCK_CLOSED_FACTOR))
    if factor > MAX_FACTOR:
        factor = MAX_FACTOR
        reasons.append(Msg("risk.session.capped", factor=MAX_FACTOR))
    return SessionInfo(weekend, thin, stock, closed, factor, reasons)


def parse_hours(value: str) -> tuple[int, int]:
    """`"21-1"` → (21, 1). Falls back to the default window on bad input."""
    try:
        start, end = (int(x) % 24 for x in value.split("-", 1))
        return start, end
    except (ValueError, AttributeError):
        return DEFAULT_LOW_LIQUIDITY_HOURS
