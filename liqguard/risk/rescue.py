"""
Rescue simulator: compare add-margin, reduce and hedge for one at-risk position.

Everything is computed here, deterministically; Muse only explains the
trade-offs. Each option reports the new liquidation price and distance
plus its cost: taker fees, funding over the next 24 h at the current rate,
realized loss, and capital that has to be committed.

Venue rules that matter:
- Cross margin: realized PnL from a partial close goes to the shared wallet,
  so reducing size moves the liquidation price; an opposite position on the
  same symbol (Binance Hedge Mode) shares the wallet and offsets losses.
- Isolated margin: margin is released pro rata on a partial close, so the
  liquidation price barely moves; only closing removes the risk.
- Hyperliquid nets positions per asset, so a hedge has to live on another
  venue, where it protects PnL but does not move this liquidation price.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from liqguard.i18n import Msg
from liqguard.models import CROSS, FundingInfo, MarginAccount, Position, Side, Venue

from .advice import ActionableAdvice, ActionType
from .fees import FeeSchedule
from .funding import PROJECTION_LABEL, funding_payment, settlements_within
from .liquidation import (
    LiquidationResult,
    brackets_for,
    distance_pct,
    maintenance_margin,
    solve_by_bisection,
    target_liquidation_price,
    true_liquidation,
    wallet_for_target,
)

ADD_MARGIN, REDUCE, HEDGE = "add_margin", "reduce", "hedge"
OPTION_ORDER = (ADD_MARGIN, REDUCE, HEDGE)
NO_RESCUE = "none"
"""`RescuePlan.recommended` when there is nothing worth doing (already safe, or every option is a no-op)."""
MIN_ACTION_USD = 0.01
MIN_ACTION_PCT = 1e-6
FUNDING_HORIZON_HOURS = 24


@dataclass(frozen=True)
class RescueOption:
    kind: str
    title: str
    new_liquidation_price: Optional[float]
    new_distance_pct: float
    reaches_target: bool
    protects_liquidation: bool
    fees_usd: float
    funding_24h_usd: float
    realized_loss_usd: float
    capital_required_usd: float
    params: dict = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def total_cost_usd(self) -> float:
        """Fees + realized loss + 24 h funding (negative funding = received)."""
        return self.fees_usd + self.realized_loss_usd + self.funding_24h_usd

    @property
    def is_noop(self) -> bool:
        """Adds no margin, closes nothing and hedges nothing (e.g. "add $0.00"): never recommended nor explained."""
        if self.kind == ADD_MARGIN:
            return abs(float(self.params.get("amount_usd") or 0.0)) < MIN_ACTION_USD
        share = {REDUCE: "reduce_pct", HEDGE: "hedge_pct"}.get(self.kind)
        return share is not None and abs(float(self.params.get(share) or 0.0)) < MIN_ACTION_PCT

    def to_dict(self) -> dict:
        inf = math.isinf(self.new_distance_pct)
        return {
            "kind": self.kind,
            "title": self.title,
            "params": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.params.items()},
            "new_liquidation_price": round(self.new_liquidation_price, 4) if self.new_liquidation_price else None,
            "new_distance_pct": None if inf else round(self.new_distance_pct, 2),
            "no_liquidation": inf,
            "reaches_target": self.reaches_target,
            "protects_liquidation": self.protects_liquidation,
            "costs": {
                "fees_usd": round(self.fees_usd, 2),
                "funding_24h_usd": round(self.funding_24h_usd, 2),
                "realized_loss_usd": round(self.realized_loss_usd, 2),
                "capital_required_usd": round(self.capital_required_usd, 2),
                "total_cost_usd": round(self.total_cost_usd, 2),
            },
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class RescuePlan:
    symbol: str
    venue: str
    side: str
    margin_mode: str
    mark_price: float
    current: LiquidationResult
    target_distance_pct: float
    funding: Optional[FundingInfo]
    options: tuple[RescueOption, ...]
    recommended: str

    def option(self, kind: str) -> Optional[RescueOption]:
        return next((o for o in self.options if o.kind == kind), None)

    @property
    def already_safe(self) -> bool:
        """Already at or beyond the target: every option is a no-op, so there is nothing to recommend."""
        return _safe(self.current.distance_pct, self.target_distance_pct)

    def margin_advice(self) -> ActionableAdvice:
        """The add-margin option as `ActionableAdvice` (what alerts, Muse and the confirm flow consume)."""
        add, reduce = self.option(ADD_MARGIN), self.option(REDUCE)
        amount = add.params.get("amount_usd", 0.0) if add else 0.0
        return ActionableAdvice(
            action_type=ActionType.ADD_MARGIN,
            symbol=self.symbol,
            venue=self.venue,
            current_distance_pct=self.current.distance_pct,
            target_distance_pct=self.target_distance_pct,
            margin_to_add_usd=amount,
            reduce_size_by=reduce.params.get("reduce_size") if reduce else None,
            reduce_size_pct=reduce.params.get("reduce_pct") if reduce else None,
            new_liquidation_price=add.new_liquidation_price if add and amount else None,
            new_distance_pct=add.new_distance_pct if add and amount else self.current.distance_pct,
            estimated_cost_usd=amount,
        )

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "side": self.side,
            "margin_mode": self.margin_mode,
            "mark_price": round(self.mark_price, 4),
            "current": self.current.to_dict(),
            "target_distance_pct": self.target_distance_pct,
            "funding": self.funding.to_dict() if self.funding else None,
            "funding_label": PROJECTION_LABEL,
            "options": [o.to_dict() for o in self.options],
            "recommended": self.recommended,
            "already_safe": self.already_safe,
        }


def _replace_in(account: Optional[MarginAccount], old: Position, new: Optional[Position], wallet: float) -> Optional[MarginAccount]:
    if account is None:
        return None
    positions = [p for p in account.positions if not (p is old or (p.symbol == old.symbol and p.side == old.side))]
    if new is not None:
        positions.append(new)
    return dataclasses.replace(account, positions=positions, wallet_balance=wallet)


def _scaled(position: Position, fraction_left: float, **changes) -> Position:
    size = abs(position.size) * fraction_left
    return dataclasses.replace(
        position,
        size=size,
        margin=position.margin * fraction_left,
        unrealized_pnl=position.unrealized_pnl * fraction_left,
        **changes,
    )


def _bisect_fraction(reaches, iterations: int = 60) -> Optional[float]:
    """Smallest x in (0, 1] with reaches(x) True, assuming monotonic; None if reaches(1) is False."""
    if not reaches(1.0):
        return None
    lo, hi = 0.0, 1.0
    for _ in range(iterations):
        mid = (lo + hi) / 2
        if reaches(mid):
            hi = mid
        else:
            lo = mid
    return hi


class RescueSimulator:
    def __init__(self, fees: Optional[FeeSchedule] = None, target_distance_pct: float = 30.0):
        self.fees = fees or FeeSchedule()
        self.target = target_distance_pct

    # ------------------------------------------------------------------ helpers

    def _funding_24h(self, position: Position, size: float, funding: Optional[FundingInfo], now: datetime) -> float:
        if funding is None or size <= 0:
            return 0.0
        n = settlements_within(funding, now, FUNDING_HORIZON_HOURS)
        return funding_payment(dataclasses.replace(position, size=size), funding.rate) * n

    def _is_cross(self, position: Position, account: Optional[MarginAccount]) -> bool:
        return position.margin_mode == CROSS and account is not None

    # ------------------------------------------------------------------ options

    def add_margin(self, position: Position, account: Optional[MarginAccount], funding, now) -> RescueOption:
        current = true_liquidation(position, account)
        target_price = target_liquidation_price(position.side, position.mark_price, self.target)
        needed_wallet = wallet_for_target(position, account, target_price)
        amount = max(0.0, needed_wallet - current.wallet_balance)

        if self._is_cross(position, account):
            after = true_liquidation(position, _replace_in(account, position, position, account.wallet_balance + amount))
            where = Msg("risk.rescue.where_cross")
        else:
            wallet = (position.isolated_wallet if position.isolated_wallet is not None else position.margin) + amount
            after = true_liquidation(dataclasses.replace(position, isolated_wallet=wallet), None)
            where = Msg("risk.rescue.where_isolated")
        return RescueOption(
            kind=ADD_MARGIN,
            title=Msg("risk.rescue.title_add", amount=amount, where=where),
            new_liquidation_price=after.price,
            new_distance_pct=after.distance_pct,
            reaches_target=after.distance_pct >= self.target - 1e-6,
            protects_liquidation=True,
            fees_usd=0.0,
            funding_24h_usd=self._funding_24h(position, abs(position.size), funding, now),
            realized_loss_usd=0.0,
            capital_required_usd=amount,
            params={"amount_usd": amount},
            notes=(Msg("risk.rescue.note_add"),),
        )

    def _reduce_result(self, position: Position, account: Optional[MarginAccount], fraction: float):
        q, mark = abs(position.size), position.mark_price
        fee = self.fees.taker_fee(position.venue, fraction * q * mark)
        realized = fraction * position.unrealized_pnl
        if fraction >= 1.0 - 1e-12:
            return math.inf, None, fee, realized
        left = 1 - fraction
        if self._is_cross(position, account):
            new_pos = _scaled(position, left)
            new_wallet = account.wallet_balance + realized - fee
            after = true_liquidation(new_pos, _replace_in(account, position, new_pos, new_wallet))
        else:
            wallet = position.isolated_wallet if position.isolated_wallet is not None else position.margin
            new_pos = _scaled(position, left, isolated_wallet=wallet * left)
            after = true_liquidation(new_pos, None)
        return after.distance_pct, after.price, fee, realized

    def reduce(self, position: Position, account: Optional[MarginAccount], funding, now) -> RescueOption:
        fraction = _bisect_fraction(lambda f: self._reduce_result(position, account, f)[0] >= self.target)
        fraction = 1.0 if fraction is None else fraction
        dist, liq, fee, realized = self._reduce_result(position, account, fraction)
        q = abs(position.size)
        notes = []
        if not self._is_cross(position, account):
            notes.append(Msg("risk.rescue.note_isolated_reduce"))
        if fraction >= 1.0 - 1e-9:
            title = Msg("risk.rescue.title_close", symbol=position.symbol)
        else:
            title = Msg("risk.rescue.title_reduce", pct=fraction * 100, size=fraction * q,
                        coin=position.base_symbol.removesuffix("USDT"))
        return RescueOption(
            kind=REDUCE,
            title=title,
            new_liquidation_price=liq,
            new_distance_pct=dist,
            reaches_target=dist >= self.target - 1e-6,
            protects_liquidation=True,
            fees_usd=fee,
            funding_24h_usd=self._funding_24h(position, q * (1 - fraction), funding, now),
            realized_loss_usd=max(0.0, -realized),
            capital_required_usd=0.0,
            params={"reduce_pct": fraction * 100, "reduce_size": fraction * q},
            notes=tuple(notes),
        )

    def _hedged_liquidation(self, position: Position, account: MarginAccount, hedge_size: float, fee: float):
        """Binance Hedge Mode: the opposite leg sits in the same cross wallet and offsets losses."""
        s, q, e, mark = position.sign, abs(position.size), position.entry_price, position.mark_price
        base = true_liquidation(position, account)
        brackets, _ = brackets_for(position, account)
        wallet = account.wallet_balance - fee

        def surplus(p: float) -> float:
            equity = wallet + base.other_upnl + s * q * (p - e) - s * hedge_size * (p - mark)
            mm = base.other_maintenance + maintenance_margin(q * p, brackets) + maintenance_margin(hedge_size * p, brackets)
            return equity - mm

        liq = solve_by_bisection(surplus, mark, adverse_up=position.side == Side.SHORT)
        return distance_pct(position.side, mark, liq), liq

    def hedge(self, position: Position, account: Optional[MarginAccount], funding, now) -> RescueOption:
        q, mark = abs(position.size), position.mark_price
        opposite = "short" if position.side == Side.LONG else "long"
        if position.venue == Venue.BINANCE and self._is_cross(position, account):
            rate = self.fees.taker_rate(position.venue)

            def result(fraction: float):
                h = fraction * q
                return self._hedged_liquidation(position, account, h, h * mark * rate)

            fraction = _bisect_fraction(lambda f: result(f)[0] >= self.target)
            fraction = 1.0 if fraction is None else fraction
            h = fraction * q
            dist, liq = result(fraction)
            open_fee = h * mark * rate
            return RescueOption(
                kind=HEDGE,
                title=Msg("risk.rescue.title_hedge_same", size=h, side=Msg(f"common.side.{opposite}"), symbol=position.symbol),
                new_liquidation_price=liq,
                new_distance_pct=dist,
                reaches_target=dist >= self.target - 1e-6,
                protects_liquidation=True,
                fees_usd=2 * open_fee,
                funding_24h_usd=self._funding_24h(position, q - h, funding, now),
                realized_loss_usd=0.0,
                capital_required_usd=h * mark / max(position.leverage, 1),
                params={"hedge_size": h, "hedge_pct": fraction * 100, "hedge_venue": position.venue.value},
                notes=(Msg("risk.rescue.note_hedge_mode"), Msg("risk.rescue.note_hedge_lock")),
            )

        other = Venue.BINANCE if position.venue == Venue.HYPERLIQUID else Venue.HYPERLIQUID
        current = true_liquidation(position, account)
        open_fee = self.fees.taker_fee(other, q * mark)
        reason = Msg("risk.rescue.note_hl_nets" if position.venue == Venue.HYPERLIQUID else "risk.rescue.note_isolated_hedge")
        return RescueOption(
            kind=HEDGE,
            title=Msg("risk.rescue.title_hedge_other", size=q, side=Msg(f"common.side.{opposite}"), symbol=position.base_symbol,
                      venue=other.value.title()),
            new_liquidation_price=current.price,
            new_distance_pct=current.distance_pct,
            reaches_target=False,
            protects_liquidation=False,
            fees_usd=2 * open_fee,
            funding_24h_usd=0.0,
            realized_loss_usd=0.0,
            capital_required_usd=q * mark / max(position.leverage, 1),
            params={"hedge_size": q, "hedge_pct": 100.0, "hedge_venue": other.value},
            notes=(
                reason,
                Msg("risk.rescue.note_hedge_other"),
            ),
        )

    # ------------------------------------------------------------------ plan

    def simulate(
        self,
        position: Position,
        account: Optional[MarginAccount] = None,
        funding: Optional[FundingInfo] = None,
        now: Optional[datetime] = None,
    ) -> RescuePlan:
        now = now or datetime.now(timezone.utc)
        options = (
            self.add_margin(position, account, funding, now),
            self.reduce(position, account, funding, now),
            self.hedge(position, account, funding, now),
        )
        current = true_liquidation(position, account)
        return RescuePlan(
            symbol=position.symbol,
            venue=position.venue.value,
            side=position.side.value,
            margin_mode=current.margin_mode,
            mark_price=position.mark_price,
            current=current,
            target_distance_pct=self.target,
            funding=funding,
            options=options,
            recommended=NO_RESCUE if _safe(current.distance_pct, self.target) else recommend(options),
        )


def _safe(distance_pct: Optional[float], target: float) -> bool:
    return distance_pct is None or distance_pct >= target


def recommend(options: tuple[RescueOption, ...]) -> str:
    """Cheapest option that reaches the target and actually protects against liquidation; NO_RESCUE if every option is a no-op."""
    options = tuple(o for o in options if not o.is_noop)
    if not options:
        return NO_RESCUE
    viable = [o for o in options if o.reaches_target and o.protects_liquidation]
    if viable:
        return min(viable, key=lambda o: (round(o.total_cost_usd, 2), OPTION_ORDER.index(o.kind))).kind
    protective = [o for o in options if o.protects_liquidation]
    return max(protective or options, key=lambda o: o.new_distance_pct).kind
