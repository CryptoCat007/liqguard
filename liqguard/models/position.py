"""Normalized position model for all exchanges."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Venue(str, Enum):
    """Supported trading venues."""
    BINANCE = "binance"
    HYPERLIQUID = "hyperliquid"
    DEMO = "demo"


class Side(str, Enum):
    """Position side."""
    LONG = "long"
    SHORT = "short"


@dataclass
class Position:
    """
    Normalized position format across all venues.

    All monetary values are in USD equivalent.
    """
    venue: Venue
    symbol: str
    side: Side
    size: float
    entry_price: float
    mark_price: float
    liquidation_price: float
    margin: float
    leverage: float
    unrealized_pnl: float
    margin_ratio: Optional[float] = None
    timestamp: datetime = None
    network: str = "mainnet"

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)

    @property
    def notional_value(self) -> float:
        """Position notional value."""
        return abs(self.size * self.mark_price)

    @property
    def distance_to_liquidation_pct(self) -> float:
        """
        Calculate distance from mark price to liquidation price as percentage.

        For LONG: (mark - liq) / mark * 100
        For SHORT: (liq - mark) / mark * 100

        Positive = safe, negative = already past liquidation (should never happen).
        """
        if self.liquidation_price <= 0 or self.mark_price <= 0:
            return float('inf')

        if self.side == Side.LONG:
            return ((self.mark_price - self.liquidation_price) / self.mark_price) * 100
        else:
            return ((self.liquidation_price - self.mark_price) / self.mark_price) * 100

    @property
    def is_at_risk(self) -> bool:
        """Check if position is at significant risk (< 20% from liquidation)."""
        return self.distance_to_liquidation_pct < 20

    def margin_to_reach_distance(self, target_distance_pct: float) -> float:
        """
        Calculate additional margin needed to reach a target distance from liquidation.

        Assumes isolated margin: adding M USD of margin moves the liquidation
        price by M / size. Exchanges differ slightly (maintenance tiers, fees).

        Args:
            target_distance_pct: Target distance from liquidation as percentage (e.g., 30 for 30%)

        Returns:
            Additional margin required in USD. Returns 0 if already at or above target.
        """
        current_distance = self.distance_to_liquidation_pct

        if current_distance >= target_distance_pct:
            return 0.0

        distance_needed = target_distance_pct - current_distance
        price_gap_needed = (distance_needed / 100) * self.mark_price
        additional_margin = price_gap_needed * abs(self.size)

        return max(0.0, additional_margin)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "venue": self.venue.value,
            "symbol": self.symbol,
            "side": self.side.value,
            "size": self.size,
            "entry_price": self.entry_price,
            "mark_price": self.mark_price,
            "liquidation_price": self.liquidation_price,
            "margin": self.margin,
            "leverage": self.leverage,
            "unrealized_pnl": self.unrealized_pnl,
            "margin_ratio": self.margin_ratio,
            "notional_value": self.notional_value,
            "distance_to_liquidation_pct": self.distance_to_liquidation_pct,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "network": self.network,
        }
