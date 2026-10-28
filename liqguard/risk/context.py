"""Everything beyond the positions themselves that exact risk math needs, for one user."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from liqguard.models import CROSS, FundingInfo, MarginAccount, OpenOrder, Position, WickProfile

from .fees import FeeSchedule
from .liquidation import LiquidationResult, true_liquidation
from .sessions import DEFAULT_LOW_LIQUIDITY_HOURS, SessionInfo, market_session
from .stops import DEFAULT_BUFFER_PCT


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RiskContext:
    """
    Margin accounts (wallet balances, tiers), open orders, funding and wick
    statistics for one user's book. Funding and wicks are public market data;
    accounts and orders belong to the user.

    `orders` is keyed by (venue, network) and only contains venues whose open
    orders were actually fetched: a missing key means "unknown", not "no stop".
    """

    accounts: list[MarginAccount] = field(default_factory=list)
    funding: dict[tuple[str, str], FundingInfo] = field(default_factory=dict)
    fees: FeeSchedule = field(default_factory=FeeSchedule)
    now: datetime = field(default_factory=_utcnow)
    orders: dict[tuple[str, str], list[OpenOrder]] = field(default_factory=dict)
    wicks: dict[tuple[str, str], WickProfile] = field(default_factory=dict)
    low_liquidity_hours: tuple[int, int] = DEFAULT_LOW_LIQUIDITY_HOURS
    stop_buffer_pct: float = DEFAULT_BUFFER_PCT

    def account_for(self, position: Position) -> Optional[MarginAccount]:
        for account in self.accounts:
            if account.venue == position.venue and account.network == position.network and account.holds(position):
                return account
        return None

    def funding_for(self, position: Position) -> Optional[FundingInfo]:
        return self.funding.get((position.venue.value, position.base_symbol)) or self.funding.get(
            (position.venue.value, position.symbol)
        )

    def computable(self, position: Position) -> bool:
        """Cross positions need their account (wallet + other positions); without it only the exchange figure is trustworthy."""
        return position.margin_mode != CROSS or self.account_for(position) is not None

    def liquidation(self, position: Position) -> LiquidationResult:
        return true_liquidation(position, self.account_for(position))

    def effective_distance(self, position: Position) -> float:
        """The closer of the exchange-reported and the computed liquidation distance (conservative)."""
        reported = position.distance_to_liquidation_pct
        if not self.computable(position):
            return reported
        computed = self.liquidation(position).distance_pct
        if position.liquidation_price <= 0:
            return computed
        return min(reported, computed)

    def orders_for(self, position: Position) -> Optional[list[OpenOrder]]:
        orders = self.orders.get((position.venue.value, position.network))
        if orders is None:
            return None
        return [o for o in orders if o.base_symbol == position.base_symbol]

    def wicks_for(self, position: Position) -> Optional[WickProfile]:
        return self.wicks.get((position.venue.value, position.base_symbol))

    def session(self, position: Position) -> SessionInfo:
        return market_session(self.now, position.symbol, self.low_liquidity_hours)

    def liquidation_price(self, position: Position) -> Optional[float]:
        """The liquidation price behind `effective_distance` (the closer one), or None if there is none."""
        reported = position.liquidation_price if position.liquidation_price > 0 else None
        if not self.computable(position):
            return reported
        computed = self.liquidation(position).price
        if reported is None or computed is None:
            return computed or reported
        return max(reported, computed) if position.sign > 0 else min(reported, computed)
