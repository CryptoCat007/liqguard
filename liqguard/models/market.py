"""Market and account data needed for exact liquidation math: margin tiers, funding, cross accounts."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .position import CROSS, Position, Venue


@dataclass(frozen=True)
class MaintenanceBracket:
    """
    One maintenance-margin tier: for notional in [floor, cap),
    maintenance margin = notional × mmr − cum.
    """

    notional_floor: float
    notional_cap: float
    mmr: float
    cum: float = 0.0

    def contains(self, notional: float) -> bool:
        return self.notional_floor <= notional < self.notional_cap

    def to_dict(self) -> dict:
        return {
            "notional_floor": self.notional_floor,
            "notional_cap": None if math.isinf(self.notional_cap) else self.notional_cap,
            "mmr": self.mmr,
            "cum": self.cum,
        }


Brackets = tuple[MaintenanceBracket, ...]


@dataclass(frozen=True)
class FundingInfo:
    """
    Funding for one perpetual. `rate` is the fraction paid per interval
    (positive: longs pay shorts). Sourced live from the venue's public API,
    or marked `source="demo"` inside the simulation.
    """

    venue: Venue
    symbol: str
    rate: float
    interval_hours: float
    next_funding_time: datetime
    mark_price: Optional[float] = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "live"

    @property
    def annualized_pct(self) -> float:
        return self.rate * (24 / self.interval_hours) * 365 * 100

    def to_dict(self) -> dict:
        return {
            "venue": self.venue.value,
            "symbol": self.symbol,
            "rate": self.rate,
            "rate_pct": round(self.rate * 100, 6),
            "interval_hours": self.interval_hours,
            "next_funding_time": self.next_funding_time.isoformat(),
            "annualized_pct": round(self.annualized_pct, 2),
            "source": self.source,
        }


@dataclass
class MarginAccount:
    """
    One margin account on one venue.

    `wallet_balance` is the cross-margin wallet *excluding* unrealized PnL
    (Binance `crossWalletBalance`; Hyperliquid cross account value minus the
    cross positions' unrealized PnL). Isolated positions carry their own
    wallet on the position. `schedules` holds maintenance tiers per symbol;
    missing symbols fall back to documented defaults (flagged as estimated).
    """

    venue: Venue
    wallet_balance: float
    positions: list[Position] = field(default_factory=list)
    schedules: dict[str, Brackets] = field(default_factory=dict)
    network: str = "mainnet"
    source: str = "exchange"

    def cross_positions(self) -> list[Position]:
        return [p for p in self.positions if p.margin_mode == CROSS]

    def holds(self, position: Position) -> bool:
        return any(p is position for p in self.positions) or any(
            p.symbol == position.symbol and p.side == position.side for p in self.positions
        )
