"""Open orders, normalized across venues (read-only: LiqGuard never places or edits orders on mainnet)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .position import Side, Venue

STOP_MARKET = "stop_market"
STOP_LIMIT = "stop_limit"
TRAILING_STOP = "trailing_stop"
TAKE_PROFIT = "take_profit"
LIMIT = "limit"
OTHER = "other"

STOP_TYPES = frozenset({STOP_MARKET, STOP_LIMIT, TRAILING_STOP})


@dataclass(frozen=True)
class OpenOrder:
    """
    One resting order. `side` is the order's side (a long is closed by a SELL
    order, i.e. `Side.SHORT` here). `trigger_price` is set for conditional
    orders; trailing stops may only carry an activation price.
    """

    venue: Venue
    symbol: str
    side: Side
    order_type: str
    trigger_price: Optional[float] = None
    limit_price: Optional[float] = None
    quantity: Optional[float] = None
    reduce_only: bool = False
    close_position: bool = False
    position_side: Optional[str] = None
    order_id: Optional[str] = None

    @property
    def is_stop(self) -> bool:
        return self.order_type in STOP_TYPES

    @property
    def base_symbol(self) -> str:
        return self.symbol.removesuffix("-PERP")

    def to_dict(self) -> dict:
        return {
            "venue": self.venue.value,
            "symbol": self.symbol,
            "side": "sell" if self.side == Side.SHORT else "buy",
            "order_type": self.order_type,
            "trigger_price": self.trigger_price,
            "limit_price": self.limit_price,
            "quantity": self.quantity,
            "reduce_only": self.reduce_only,
            "close_position": self.close_position,
        }
