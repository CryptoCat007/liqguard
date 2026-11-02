"""
Stop-loss sanity check: is there a stop at all, and would it fire before the
liquidation engine does?

A stop that sits beyond the liquidation price never triggers: the exchange
liquidates first and the whole margin is lost (plus the liquidation fee). A
stop that sits just in front of the liquidation price is barely better: a
stop-market order fills after the trigger, and in a wick the fill can land
past the liquidation price. The required gap is the larger of a fixed buffer
and the symbol's typical (p95) 5m wick.

Fixes are computed here and only phrased by Muse:
- move the stop to the closest safe price, or
- keep the stop and change the position (add margin / reduce size) so the
  liquidation price ends up the required gap beyond the stop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from liqguard.i18n import Msg
from liqguard.models import FundingInfo, MarginAccount, OpenOrder, Position, Side, WickProfile

from .fees import FeeSchedule
from .rescue import ADD_MARGIN, REDUCE, RescueSimulator

NO_STOP = "no_stop"
BEYOND_LIQUIDATION = "beyond_liquidation"
TOO_CLOSE = "too_close"
PARTIAL = "partial"
OK = "ok"
UNKNOWN = "unknown"

SEVERITY = {BEYOND_LIQUIDATION: "critical", NO_STOP: "warning", TOO_CLOSE: "warning", PARTIAL: "info", OK: None, UNKNOWN: None}
DEFAULT_BUFFER_PCT = 0.5


def _closing_side(position: Position) -> Side:
    return Side.SHORT if position.side == Side.LONG else Side.LONG


def _same_symbol(order: OpenOrder, position: Position) -> bool:
    return order.venue == position.venue and order.base_symbol == position.base_symbol


def protective_stops(position: Position, orders: list[OpenOrder]) -> list[OpenOrder]:
    """Stop orders that would close (part of) this position when price moves against it."""
    out = []
    for o in orders:
        if not (o.is_stop and _same_symbol(o, position) and o.side == _closing_side(position)):
            continue
        if o.position_side in ("LONG", "SHORT") and o.position_side.lower() != position.side.value:
            continue
        if o.trigger_price is not None:
            adverse = o.trigger_price < position.mark_price if position.side == Side.LONG else o.trigger_price > position.mark_price
            if not adverse:
                continue
        out.append(o)
    return out


def gap_pct(side: Side, stop_price: float, liquidation_price: float) -> float:
    """How far in front of the liquidation price the stop sits, in % of the stop price (negative: beyond it)."""
    if side == Side.LONG:
        return (stop_price - liquidation_price) / stop_price * 100
    return (liquidation_price - stop_price) / stop_price * 100


def safe_stop_price(side: Side, liquidation_price: float, required_gap_pct: float) -> float:
    """Closest stop price that still leaves `required_gap_pct` before liquidation."""
    if side == Side.LONG:
        return liquidation_price / (1 - required_gap_pct / 100)
    return liquidation_price / (1 + required_gap_pct / 100)


def pnl_at(position: Position, price: float, fees: FeeSchedule) -> float:
    """Realized PnL if the whole position closed at `price` (taker fee included)."""
    q = abs(position.size)
    return position.sign * q * (price - position.entry_price) - fees.taker_fee(position.venue, q * price)


@dataclass(frozen=True)
class StopFix:
    kind: str
    title: str
    stop_price: Optional[float] = None
    pnl_at_stop_usd: Optional[float] = None
    add_margin_usd: Optional[float] = None
    reduce_pct: Optional[float] = None
    reduce_size: Optional[float] = None
    new_size: Optional[float] = None
    new_liquidation_price: Optional[float] = None
    fees_usd: float = 0.0

    def to_dict(self) -> dict:
        def r(x, dp=2):
            return None if x is None else round(x, dp)
        return {
            "kind": self.kind,
            "title": self.title,
            "stop_price": r(self.stop_price, 4),
            "pnl_at_stop_usd": r(self.pnl_at_stop_usd),
            "add_margin_usd": r(self.add_margin_usd),
            "reduce_pct": r(self.reduce_pct, 1),
            "reduce_size": r(self.reduce_size, 6),
            "new_size": r(self.new_size, 6),
            "new_liquidation_price": r(self.new_liquidation_price, 4),
            "fees_usd": r(self.fees_usd),
        }


@dataclass(frozen=True)
class StopCheck:
    symbol: str
    venue: str
    side: str
    status: str
    mark_price: float
    liquidation_price: Optional[float]
    required_gap_pct: float
    stops: list[OpenOrder] = field(default_factory=list)
    stop_price: Optional[float] = None
    gap_pct: Optional[float] = None
    covered_pct: Optional[float] = None
    pnl_at_stop_usd: Optional[float] = None
    loss_if_liquidated_usd: Optional[float] = None
    fixes: list[StopFix] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def severity(self) -> Optional[str]:
        return SEVERITY.get(self.status)

    @property
    def needs_warning(self) -> bool:
        return self.status in (NO_STOP, BEYOND_LIQUIDATION, TOO_CLOSE)

    def fix(self, kind: str) -> Optional[StopFix]:
        return next((f for f in self.fixes if f.kind == kind), None)

    def to_dict(self) -> dict:
        def r(x, dp=2):
            return None if x is None else round(x, dp)
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "side": self.side,
            "status": self.status,
            "severity": self.severity,
            "needs_warning": self.needs_warning,
            "mark_price": r(self.mark_price, 4),
            "liquidation_price": r(self.liquidation_price, 4),
            "required_gap_pct": r(self.required_gap_pct, 3),
            "stops": [o.to_dict() for o in self.stops],
            "stop_price": r(self.stop_price, 4),
            "gap_pct": r(self.gap_pct, 3),
            "covered_pct": r(self.covered_pct, 1),
            "pnl_at_stop_usd": r(self.pnl_at_stop_usd),
            "loss_if_liquidated_usd": r(self.loss_if_liquidated_usd),
            "fixes": [f.to_dict() for f in self.fixes],
            "notes": self.notes,
        }


def required_gap(profile: Optional[WickProfile], side: Side, buffer_pct: float = DEFAULT_BUFFER_PCT) -> float:
    five = profile.stat("5m") if profile else None
    wick = five.typical_pct(side == Side.LONG) if five else 0.0
    return max(buffer_pct, wick)


def _first_trigger(position: Position, stops: list[OpenOrder]) -> Optional[float]:
    prices = [o.trigger_price for o in stops if o.trigger_price is not None]
    if not prices:
        return None
    return max(prices) if position.side == Side.LONG else min(prices)


def _coverage(position: Position, stops: list[OpenOrder]) -> float:
    if any(o.close_position for o in stops):
        return 100.0
    qty = sum(o.quantity or 0.0 for o in stops)
    return min(100.0, qty / abs(position.size) * 100) if position.size else 0.0


def _liquidation_loss(position: Position, liquidation_price: float, account: Optional[MarginAccount]) -> float:
    """PnL lost if liquidated at `liquidation_price` (isolated: the position's margin; cross: the position's loss)."""
    loss = position.sign * abs(position.size) * (position.entry_price - liquidation_price)
    if account is None or position.margin_mode != "cross":
        wallet = position.isolated_wallet if position.isolated_wallet is not None else position.margin
        return max(loss, wallet)
    return max(loss, 0.0)


def check_stop_loss(
    position: Position,
    orders: list[OpenOrder],
    liquidation_price: Optional[float],
    account: Optional[MarginAccount] = None,
    fees: Optional[FeeSchedule] = None,
    profile: Optional[WickProfile] = None,
    funding: Optional[FundingInfo] = None,
    now: Optional[datetime] = None,
    buffer_pct: float = DEFAULT_BUFFER_PCT,
) -> StopCheck:
    fees = fees or FeeSchedule()
    side = position.side
    need = required_gap(profile, side, buffer_pct)
    stops = protective_stops(position, orders)
    liq = liquidation_price if liquidation_price and liquidation_price > 0 else None
    base = dict(
        symbol=position.symbol, venue=position.venue.value, side=side.value,
        mark_price=position.mark_price, liquidation_price=liq, required_gap_pct=need, stops=stops,
    )
    loss_if_liquidated = _liquidation_loss(position, liq, account) if liq else None

    def placement_fix() -> list[StopFix]:
        if liq is None:
            return []
        price = safe_stop_price(side, liq, need)
        in_front = price < position.mark_price if side == Side.LONG else price > position.mark_price
        if not in_front:
            return []
        return [StopFix("move_stop", Msg("risk.stop.fix_move", price=price), stop_price=price, pnl_at_stop_usd=pnl_at(position, price, fees))]

    if not stops:
        notes = [] if liq else [Msg("risk.stop.note_no_liq")]
        return StopCheck(status=NO_STOP if liq else OK, fixes=placement_fix(), loss_if_liquidated_usd=loss_if_liquidated,
                         notes=notes, **base)

    stop = _first_trigger(position, stops)
    covered = _coverage(position, stops)
    if stop is None:
        return StopCheck(status=OK, covered_pct=covered, notes=[Msg("risk.stop.note_trailing")], **base)
    if liq is None:
        return StopCheck(status=OK, stop_price=stop, covered_pct=covered, pnl_at_stop_usd=pnl_at(position, stop, fees), **base)

    gap = gap_pct(side, stop, liq)
    if gap <= 0:
        status = BEYOND_LIQUIDATION
    elif gap < need:
        status = TOO_CLOSE
    elif covered < 99.9:
        status = PARTIAL
    else:
        status = OK

    fixes: list[StopFix] = []
    notes: list[str] = []
    if status in (BEYOND_LIQUIDATION, TOO_CLOSE):
        fixes.extend(placement_fix())
        fixes.extend(_keep_stop_fixes(position, stop, need, account, fees, funding, now))
        if status == BEYOND_LIQUIDATION:
            notes.append(Msg("risk.stop.note_beyond"))
        else:
            notes.append(Msg("risk.stop.note_slip"))
    elif status == PARTIAL:
        notes.append(Msg("risk.stop.note_partial", covered=covered))
    return StopCheck(
        status=status, stop_price=stop, gap_pct=gap, covered_pct=covered,
        pnl_at_stop_usd=pnl_at(position, stop, fees), loss_if_liquidated_usd=loss_if_liquidated,
        fixes=fixes, notes=notes, **base,
    )


def _keep_stop_fixes(position, stop, need, account, fees, funding, now) -> list[StopFix]:
    """Keep the stop where it is; move the liquidation price `need`% beyond it (add margin, or a smaller position)."""
    target_liq = stop * (1 - need / 100) if position.side == Side.LONG else stop * (1 + need / 100)
    mark = position.mark_price
    target = (mark - target_liq) / mark * 100 if position.side == Side.LONG else (target_liq - mark) / mark * 100
    if target <= 0 or math.isinf(target):
        return []
    plan = RescueSimulator(fees, target).simulate(position, account, funding, now)
    out = []
    add = plan.option(ADD_MARGIN)
    if add and add.reaches_target and add.params.get("amount_usd", 0) > 0:
        out.append(StopFix(
            "add_margin", Msg("risk.stop.fix_keep_add", amount=add.params["amount_usd"]),
            stop_price=stop, add_margin_usd=add.params["amount_usd"], new_liquidation_price=add.new_liquidation_price,
        ))
    reduce = plan.option(REDUCE)
    if reduce and reduce.reaches_target and reduce.params.get("reduce_pct", 100.0) < 99.9:
        pct = reduce.params["reduce_pct"]
        size = reduce.params.get("reduce_size", abs(position.size))
        out.append(StopFix(
            "reduce", Msg("risk.stop.fix_keep_reduce", pct=pct),
            stop_price=stop, reduce_pct=pct, reduce_size=size, new_size=abs(position.size) - size,
            new_liquidation_price=reduce.new_liquidation_price, fees_usd=reduce.fees_usd,
        ))
    return out
