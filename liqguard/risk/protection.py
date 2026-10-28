"""One position's protection status: wick stress, adaptive tiers, stop-loss check and ADL (what /positions, /risk and the dashboard show)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from liqguard.models import Position, UserThreshold

from .adaptive import AdaptiveThreshold, adaptive_threshold
from .adl import AdlStatus, adl_status
from .context import RiskContext
from .stops import StopCheck, check_stop_loss
from .wicks import stress_summary


@dataclass(frozen=True)
class PositionProtection:
    symbol: str
    venue: str
    distance_pct: float
    adaptive: AdaptiveThreshold
    wicks: Optional[dict]
    stop: Optional[StopCheck]
    adl: AdlStatus

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "distance_pct": None if self.distance_pct == float("inf") else round(self.distance_pct, 3),
            "thresholds": self.adaptive.to_dict(),
            "wicks": self.wicks,
            "stop": self.stop.to_dict() if self.stop else None,
            "adl": self.adl.to_dict(),
        }


def position_protection(position: Position, context: RiskContext, base: UserThreshold) -> PositionProtection:
    distance = context.effective_distance(position)
    profile = context.wicks_for(position)
    adaptive = adaptive_threshold(base, position, profile, context.session(position))
    stop = None
    orders = context.orders_for(position)
    if orders is not None and base.stop_loss_checks:
        stop = check_stop_loss(
            position, orders, context.liquidation_price(position), context.account_for(position),
            context.fees, profile, context.funding_for(position), context.now, context.stop_buffer_pct,
        )
    return PositionProtection(
        symbol=position.symbol,
        venue=position.venue.value,
        distance_pct=distance,
        adaptive=adaptive,
        wicks=stress_summary(position, distance, profile),
        stop=stop,
        adl=adl_status(position),
    )
