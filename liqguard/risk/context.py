"""Everything beyond the positions themselves that exact risk math needs, for one user."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from liqguard.models import CROSS, FundingInfo, MarginAccount, Position

from .fees import FeeSchedule
from .liquidation import LiquidationResult, true_liquidation


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RiskContext:
    """
    Margin accounts (wallet balances, tiers) and funding for one user's book.
    Funding is public market data; accounts belong to the user.
    """

    accounts: list[MarginAccount] = field(default_factory=list)
    funding: dict[tuple[str, str], FundingInfo] = field(default_factory=dict)
    fees: FeeSchedule = field(default_factory=FeeSchedule)
    now: datetime = field(default_factory=_utcnow)

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
