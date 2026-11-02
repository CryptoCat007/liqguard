"""
Funding projections and pre-funding warnings. Pure math, no I/O.

Funding payment per settlement = s · size · mark · rate (positive: the
position pays). Payments come out of the margin pool, so in cross margin
every cross position's funding moves every cross position's liquidation
price. Projections hold the *current* rate and mark constant and are
labelled "at current rate": the rate is re-fixed every interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from liqguard.i18n import Msg
from liqguard.models import CROSS, FundingInfo, MarginAccount, Position, UserThreshold

from .liquidation import LiquidationResult, true_liquidation

FundingLookup = Callable[[Position], Optional[FundingInfo]]

PROJECTION_LABEL = Msg("risk.funding.at_current_rate")
DEFAULT_HORIZONS = (8, 24)


def next_settlement(info: FundingInfo, now: datetime) -> datetime:
    """First settlement strictly after `now` (rolls a stale `next_funding_time` forward)."""
    t, step = info.next_funding_time, timedelta(hours=info.interval_hours)
    while t <= now:
        t += step
    return t


def settlements_within(info: FundingInfo, now: datetime, hours: float) -> int:
    t, end, step, n = next_settlement(info, now), now + timedelta(hours=hours), timedelta(hours=info.interval_hours), 0
    while t <= end:
        n += 1
        t += step
    return n


def funding_payment(position: Position, rate: float, mark_price: Optional[float] = None) -> float:
    """USD paid by the position at one settlement (negative: received)."""
    mark = mark_price if mark_price is not None else position.mark_price
    return position.sign * abs(position.size) * mark * rate


def pool_payments(
    position: Position, account: Optional[MarginAccount], funding: FundingLookup, now: datetime, hours: float
) -> float:
    """Total funding paid out of `position`'s margin pool within `hours`."""
    pool = account.cross_positions() if (position.margin_mode == CROSS and account is not None) else [position]
    total = 0.0
    for p in pool:
        info = funding(p)
        if info is not None:
            total += funding_payment(p, info.rate) * settlements_within(info, now, hours)
    return total


@dataclass(frozen=True)
class FundingDrift:
    horizon_hours: float
    settlements: int
    position_payment_usd: float
    pool_payment_usd: float
    liquidation_price: Optional[float]
    distance_pct: float

    def to_dict(self) -> dict:
        return {
            "horizon_hours": self.horizon_hours,
            "settlements": self.settlements,
            "position_payment_usd": round(self.position_payment_usd, 2),
            "pool_payment_usd": round(self.pool_payment_usd, 2),
            "liquidation_price": round(self.liquidation_price, 4) if self.liquidation_price else None,
            "distance_pct": round(self.distance_pct, 3) if self.distance_pct != float("inf") else None,
        }


@dataclass(frozen=True)
class FundingProjection:
    info: FundingInfo
    next_settlement: datetime
    minutes_to_settlement: float
    next_payment_usd: float
    current: LiquidationResult
    drifts: tuple[FundingDrift, ...]
    label: str = PROJECTION_LABEL

    def to_dict(self) -> dict:
        return {
            **self.info.to_dict(),
            "next_funding_time": self.next_settlement.isoformat(),
            "minutes_to_settlement": round(self.minutes_to_settlement, 1),
            "next_payment_usd": round(self.next_payment_usd, 2),
            "drift": [d.to_dict() for d in self.drifts],
            "label": self.label,
        }


def project_funding(
    position: Position,
    account: Optional[MarginAccount],
    funding: FundingLookup,
    now: datetime,
    horizons: tuple[float, ...] = DEFAULT_HORIZONS,
) -> Optional[FundingProjection]:
    """How liquidation distance drifts over each horizon if funding stays at the current rate."""
    info = funding(position)
    if info is None:
        return None
    nxt = next_settlement(info, now)
    drifts = []
    for h in horizons:
        n = settlements_within(info, now, h)
        pool = pool_payments(position, account, funding, now, h)
        after = true_liquidation(position, account, wallet_delta=-pool)
        drifts.append(FundingDrift(h, n, funding_payment(position, info.rate) * n, pool, after.price, after.distance_pct))
    return FundingProjection(
        info=info,
        next_settlement=nxt,
        minutes_to_settlement=(nxt - now).total_seconds() / 60,
        next_payment_usd=funding_payment(position, info.rate),
        current=true_liquidation(position, account),
        drifts=tuple(drifts),
    )


@dataclass(frozen=True)
class PreFundingWarning:
    symbol: str
    venue: str
    side: str
    settlement_time: datetime
    minutes_to_settlement: float
    payment_usd: float
    rate: float
    interval_hours: float
    distance_before_pct: float
    distance_after_pct: float
    liquidation_before: Optional[float]
    liquidation_after: Optional[float]
    margin_buffer_usd: float
    buffer_share_pct: float
    crosses_tier: bool

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "side": self.side,
            "settlement_time": self.settlement_time.isoformat(),
            "minutes_to_settlement": round(self.minutes_to_settlement, 1),
            "payment_usd": round(self.payment_usd, 2),
            "rate_pct": round(self.rate * 100, 6),
            "interval_hours": self.interval_hours,
            "distance_before_pct": round(self.distance_before_pct, 3),
            "distance_after_pct": round(self.distance_after_pct, 3),
            "liquidation_before": round(self.liquidation_before, 4) if self.liquidation_before else None,
            "liquidation_after": round(self.liquidation_after, 4) if self.liquidation_after else None,
            "margin_buffer_usd": round(self.margin_buffer_usd, 2),
            "buffer_share_pct": round(self.buffer_share_pct, 2),
            "crosses_tier": self.crosses_tier,
        }


def pre_funding_warning(
    position: Position,
    account: Optional[MarginAccount],
    funding: FundingLookup,
    now: datetime,
    threshold: UserThreshold,
    lead_minutes: float = 30.0,
    min_buffer_share: float = 0.05,
) -> Optional[PreFundingWarning]:
    """
    Warn before settlement when a near-liquidation position is about to pay
    a large funding payment. "Near" = within the user's warning distance;
    "large" = the pool's payment eats at least `min_buffer_share` of the
    margin buffer, or pushes the position into a worse alert tier.
    """
    info = funding(position)
    if info is None:
        return None
    nxt = next_settlement(info, now)
    minutes = (nxt - now).total_seconds() / 60
    if not 0 < minutes <= lead_minutes:
        return None
    payment = funding_payment(position, info.rate)
    if payment <= 0:
        return None

    before = true_liquidation(position, account)
    if before.distance_pct > threshold.warning_distance_pct:
        return None
    pool = pool_payments(position, account, funding, now, minutes / 60)
    after = true_liquidation(position, account, wallet_delta=-pool)
    buffer = before.margin_buffer
    share = pool / buffer if buffer > 0 else float("inf")
    crosses = threshold.get_severity_for_distance(after.distance_pct) != threshold.get_severity_for_distance(before.distance_pct)
    if share < min_buffer_share and not crosses:
        return None
    return PreFundingWarning(
        symbol=position.symbol,
        venue=position.venue.value,
        side=position.side.value,
        settlement_time=nxt,
        minutes_to_settlement=minutes,
        payment_usd=payment,
        rate=info.rate,
        interval_hours=info.interval_hours,
        distance_before_pct=before.distance_pct,
        distance_after_pct=after.distance_pct,
        liquidation_before=before.price,
        liquidation_after=after.price,
        margin_buffer_usd=buffer,
        buffer_share_pct=share * 100 if share != float("inf") else 100.0,
        crosses_tier=crosses,
    )
