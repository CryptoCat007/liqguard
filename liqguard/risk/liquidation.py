"""
True liquidation price for isolated and cross-margin positions. Pure math, no I/O.

Model (one-way mode, one margin pool, other positions held at their current marks):

    equity(P)      = W + U_other + s·Q·(P − E)
    maintenance(P) = MM_other + Q·P·mmr_b − cum_b          (b = tier containing Q·P)
    liquidation    : equity(P) = maintenance(P)

    ⇒  P = (W + U_other − MM_other + cum_b − s·Q·E) / (Q·mmr_b − s·Q)

where s = +1 long / −1 short, Q = size, E = entry price and W = the cross
wallet balance (cross) or the position's isolated wallet (isolated). For
isolated positions U_other = MM_other = 0.

- Binance USDⓈ-M: this is Binance's published formula
  LP = (WB − TMM1 + UPNL1 + cumB − Side1·Position1·EP1) / (Position1·MMR_B − Side1·Position1),
  with tiers from /fapi/v1/leverageBracket (fallback tables below).
- Hyperliquid: maintenance margin = notional / (2 · max leverage), cum = 0.
  This is equivalent to Hyperliquid's documented
  liq_price = price − side · margin_available / position_size / (1 − l · side), l = 1 / maintenance leverage.

The tier is chosen so that Q·P actually falls in it; because cum makes the
maintenance schedule continuous, exactly one tier is self-consistent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

from liqguard.models import (
    CROSS,
    ISOLATED,
    Brackets,
    MaintenanceBracket,
    MarginAccount,
    Position,
    Side,
    Venue,
)


def build_brackets(rows: list[tuple[float, float]]) -> Brackets:
    """
    Tiers from (notional_floor, mmr) rows sorted by floor. `cum` is derived so
    maintenance margin is continuous across tiers, exactly as Binance's
    `cum` column (cum_i = cum_{i−1} + floor_i · (mmr_i − mmr_{i−1})).
    """
    out: list[MaintenanceBracket] = []
    cum = 0.0
    for i, (floor, mmr) in enumerate(rows):
        if i:
            cum += floor * (mmr - rows[i - 1][1])
        cap = rows[i + 1][0] if i + 1 < len(rows) else math.inf
        out.append(MaintenanceBracket(floor, cap, mmr, round(cum, 8)))
    return tuple(out)


# Fallback tiers, used only when live brackets are unavailable (e.g. demo or
# screenshot positions). Live Binance accounts fetch /fapi/v1/leverageBracket.
BINANCE_FALLBACK_BRACKETS: dict[str, Brackets] = {
    "BTCUSDT": build_brackets([
        (0, 0.004), (50_000, 0.005), (250_000, 0.01), (1_000_000, 0.025), (10_000_000, 0.05),
        (20_000_000, 0.10), (50_000_000, 0.125), (100_000_000, 0.15), (200_000_000, 0.25), (300_000_000, 0.50),
    ]),
    "ETHUSDT": build_brackets([
        (0, 0.005), (10_000, 0.0065), (100_000, 0.01), (500_000, 0.02), (1_000_000, 0.05),
        (2_000_000, 0.10), (5_000_000, 0.125), (10_000_000, 0.15), (20_000_000, 0.25), (50_000_000, 0.50),
    ]),
}
BINANCE_DEFAULT_BRACKETS: Brackets = build_brackets([
    (0, 0.01), (5_000, 0.015), (25_000, 0.02), (100_000, 0.05), (250_000, 0.10), (1_000_000, 0.125), (5_000_000, 0.25),
])
HYPERLIQUID_DEFAULT_MAX_LEVERAGE = 20.0


def hyperliquid_brackets(max_leverage: float) -> Brackets:
    """Hyperliquid maintenance margin is half the initial margin at max leverage."""
    return (MaintenanceBracket(0.0, math.inf, 1 / (2 * max_leverage), 0.0),)


def default_brackets(venue: Venue, symbol: str) -> Brackets:
    if venue == Venue.HYPERLIQUID:
        return hyperliquid_brackets(HYPERLIQUID_DEFAULT_MAX_LEVERAGE)
    return BINANCE_FALLBACK_BRACKETS.get(symbol, BINANCE_DEFAULT_BRACKETS)


def bracket_for(brackets: Brackets, notional: float) -> MaintenanceBracket:
    for b in brackets:
        if b.contains(notional):
            return b
    return brackets[-1]


def maintenance_margin(notional: float, brackets: Brackets) -> float:
    b = bracket_for(brackets, abs(notional))
    return max(abs(notional) * b.mmr - b.cum, 0.0)


def distance_pct(side: Side, mark_price: float, liquidation_price: Optional[float]) -> float:
    """Distance from mark to liquidation in %, `inf` if the position cannot be liquidated."""
    if liquidation_price is None or liquidation_price <= 0 or mark_price <= 0:
        return math.inf
    if side == Side.LONG:
        return (mark_price - liquidation_price) / mark_price * 100
    return (liquidation_price - mark_price) / mark_price * 100


def target_liquidation_price(side: Side, mark_price: float, target_distance_pct: float) -> float:
    t = target_distance_pct / 100
    return mark_price * (1 - t) if side == Side.LONG else mark_price * (1 + t)


def solve_liquidation_price(
    side: Side,
    size: float,
    entry_price: float,
    wallet: float,
    brackets: Brackets,
    other_upnl: float = 0.0,
    other_maintenance: float = 0.0,
) -> Optional[float]:
    """Closed-form liquidation price (see module docstring). None if no positive price liquidates."""
    if size <= 0:
        return None
    s = 1 if side == Side.LONG else -1
    k = wallet + other_upnl - other_maintenance
    for b in brackets:
        denom = size * b.mmr - s * size
        if denom == 0:
            continue
        price = (k + b.cum - s * size * entry_price) / denom
        if price > 0 and b.contains(size * price):
            return price
    return None


def solve_by_bisection(
    surplus: Callable[[float], float], start: float, adverse_up: bool, max_factor: float = 50.0, iterations: int = 200
) -> Optional[float]:
    """
    Price where `surplus` (equity − maintenance) first reaches 0 moving from
    `start` in the adverse direction. Used for multi-leg books (hedges)
    where no closed form applies. None if it never reaches 0.
    """
    if surplus(start) <= 0:
        return start
    lo, hi = (start, start * max_factor) if adverse_up else (start, 0.0)
    if surplus(hi) > 0:
        return None
    for _ in range(iterations):
        mid = (lo + hi) / 2
        if surplus(mid) > 0:
            lo = mid
        else:
            hi = mid
        if abs(hi - lo) <= 1e-9 * max(start, 1.0):
            break
    return (lo + hi) / 2


@dataclass(frozen=True)
class LiquidationResult:
    """Computed liquidation for one position, with the inputs that produced it."""

    symbol: str
    side: Side
    margin_mode: str
    mark_price: float
    price: Optional[float]
    distance_pct: float
    wallet_balance: float
    other_upnl: float
    other_maintenance: float
    maintenance_rate: float
    maintenance_margin: float
    equity: float
    exchange_price: Optional[float]
    estimated_tiers: bool

    @property
    def margin_buffer(self) -> float:
        """Equity above maintenance in this margin pool: how much loss it can absorb."""
        return self.equity - self.maintenance_margin

    @property
    def exchange_distance_pct(self) -> float:
        return distance_pct(self.side, self.mark_price, self.exchange_price)

    def to_dict(self) -> dict:
        def r(v, dp=4):
            return None if v is None or (isinstance(v, float) and math.isinf(v)) else round(v, dp)

        return {
            "price": r(self.price),
            "distance_pct": r(self.distance_pct, 3),
            "exchange_price": r(self.exchange_price),
            "margin_mode": self.margin_mode,
            "wallet_balance": r(self.wallet_balance, 2),
            "other_upnl": r(self.other_upnl, 2),
            "other_maintenance": r(self.other_maintenance, 2),
            "maintenance_rate": self.maintenance_rate,
            "maintenance_margin": r(self.maintenance_margin, 2),
            "equity": r(self.equity, 2),
            "margin_buffer": r(self.margin_buffer, 2),
            "estimated_tiers": self.estimated_tiers,
        }


def brackets_for(position: Position, account: Optional[MarginAccount]) -> tuple[Brackets, bool]:
    """(tiers, estimated) for a position: the account's live tiers, else the documented fallback."""
    if account is not None:
        live = account.schedules.get(position.symbol) or account.schedules.get(position.base_symbol)
        if live:
            return live, False
    return default_brackets(position.venue, position.symbol), True


def _same(a: Position, b: Position) -> bool:
    return a is b or (a.symbol == b.symbol and a.side == b.side and a.venue == b.venue)


def true_liquidation(
    position: Position,
    account: Optional[MarginAccount] = None,
    wallet_delta: float = 0.0,
) -> LiquidationResult:
    """
    Liquidation price accounting for the margin pool the position actually
    shares. `wallet_delta` shifts the wallet (e.g. projected funding payments).
    """
    brackets, estimated = brackets_for(position, account)
    q, mark = abs(position.size), position.mark_price

    if position.margin_mode == CROSS and account is not None:
        others = [p for p in account.cross_positions() if not _same(p, position)]
        wallet = account.wallet_balance + wallet_delta
        other_upnl = sum(p.unrealized_pnl for p in others)
        other_mm = 0.0
        for p in others:
            pb, p_est = brackets_for(p, account)
            estimated = estimated or p_est
            other_mm += maintenance_margin(p.notional_value, pb)
    else:
        wallet = (position.isolated_wallet if position.isolated_wallet is not None else position.margin) + wallet_delta
        other_upnl = other_mm = 0.0

    price = solve_liquidation_price(position.side, q, position.entry_price, wallet, brackets, other_upnl, other_mm)
    own_upnl = position.sign * q * (mark - position.entry_price)
    own_mm = maintenance_margin(q * mark, brackets)
    return LiquidationResult(
        symbol=position.symbol,
        side=position.side,
        margin_mode=CROSS if (position.margin_mode == CROSS and account is not None) else ISOLATED,
        mark_price=mark,
        price=price,
        distance_pct=distance_pct(position.side, mark, price),
        wallet_balance=wallet,
        other_upnl=other_upnl,
        other_maintenance=other_mm,
        maintenance_rate=bracket_for(brackets, q * mark).mmr,
        maintenance_margin=other_mm + own_mm,
        equity=wallet + other_upnl + own_upnl,
        exchange_price=position.liquidation_price if position.liquidation_price > 0 else None,
        estimated_tiers=estimated,
    )


def wallet_for_target(
    position: Position, account: Optional[MarginAccount], target_price: float
) -> float:
    """Wallet balance at which the liquidation price would be exactly `target_price`."""
    base = true_liquidation(position, account)
    brackets, _ = brackets_for(position, account)
    q = abs(position.size)
    mm_at_target = base.other_maintenance + maintenance_margin(q * target_price, brackets)
    return mm_at_target - base.other_upnl - position.sign * q * (target_price - position.entry_price)
