"""
Portfolio stress test: what happens to each margin account if the whole
market moves together.

A platform's liquidation price for a cross position assumes every *other*
position stays at its current price. In a fast move correlated coins fall
together, so the account is liquidated much earlier than any single
position's displayed line suggests. Here the market (BTC) moves `m`%, every
cross position moves `beta × m`% (see `correlation.py`; the "uniform" model
moves everything exactly `m`%), and the account is liquidated when equity
(wallet + PnL) drops to the maintenance margin of the shocked positions.

All pure functions; nothing here does I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from liqguard.models import CROSS, MarginAccount, Position, Side

from .correlation import CORRELATED, correlation, factor
from .liquidation import brackets_for, distance_pct, maintenance_margin

SHOCKS_PCT = (5.0, 10.0, 20.0)
MAX_DOWN_PCT = 95.0
MAX_UP_PCT = 300.0
SCAN_STEP_PCT = 0.5
CONCENTRATION_SHARE_PCT = 75.0

Model = Callable[[Position], float]


def correlated_model(position: Position) -> float:
    return factor(position.symbol).beta


def uniform_model(position: Position) -> float:
    return 1.0


def _r(v: Optional[float], dp: int = 2) -> Optional[float]:
    return None if v is None or (isinstance(v, float) and math.isinf(v)) else round(v, dp)


def shocked_price(position: Position, market_move_pct: float, model: Model) -> float:
    return max(position.mark_price * (1 + model(position) * market_move_pct / 100), position.mark_price * 1e-6)


def account_surplus(account: MarginAccount, market_move_pct: float, model: Model = correlated_model) -> tuple[float, float]:
    """(equity, maintenance) of the account's cross pool after the market moves `market_move_pct`."""
    equity, maintenance = account.wallet_balance, 0.0
    for p in account.cross_positions():
        q = abs(p.size)
        price = shocked_price(p, market_move_pct, model)
        equity += p.sign * q * (price - p.entry_price)
        brackets, _ = brackets_for(p, account)
        maintenance += maintenance_margin(q * price, brackets)
    return equity, maintenance


def liquidating_move(surplus: Callable[[float], float], direction: int, limit: float) -> Optional[float]:
    """Smallest |m| (in `direction`) where surplus(m) <= 0, by scan then bisection. None if not within `limit`."""
    if surplus(0.0) <= 0:
        return 0.0
    prev = 0.0
    m = SCAN_STEP_PCT
    while m <= limit + 1e-9:
        if surplus(direction * m) <= 0:
            lo, hi = prev, m
            for _ in range(40):
                mid = (lo + hi) / 2
                if surplus(direction * mid) > 0:
                    lo = mid
                else:
                    hi = mid
            return hi
        prev, m = m, m + SCAN_STEP_PCT
    return None


@dataclass(frozen=True)
class Shock:
    market_move_pct: float
    equity: float
    maintenance: float

    @property
    def liquidated(self) -> bool:
        return self.equity <= self.maintenance

    def to_dict(self) -> dict:
        return {
            "market_move_pct": self.market_move_pct,
            "equity": _r(self.equity),
            "maintenance": _r(self.maintenance),
            "margin_left": _r(self.equity - self.maintenance),
            "liquidated": self.liquidated,
        }


@dataclass(frozen=True)
class AccountStress:
    venue: str
    network: str
    symbols: list[str]
    wallet: float
    equity: float
    maintenance: float
    shocks: list[Shock]
    liq_move_down_pct: Optional[float]
    liq_move_up_pct: Optional[float]
    uniform_liq_move_down_pct: Optional[float]
    uniform_liq_move_up_pct: Optional[float]

    @property
    def first_liquidation(self) -> Optional[float]:
        """Signed market move (correlated model) that liquidates the account first."""
        down, up = self.liq_move_down_pct, self.liq_move_up_pct
        if down is None and up is None:
            return None
        if up is None or (down is not None and down <= up):
            return -down
        return up

    def to_dict(self) -> dict:
        return {
            "venue": self.venue,
            "network": self.network,
            "symbols": self.symbols,
            "wallet": _r(self.wallet),
            "equity": _r(self.equity),
            "maintenance": _r(self.maintenance),
            "shocks": [s.to_dict() for s in self.shocks],
            "liq_move_down_pct": _r(self.liq_move_down_pct),
            "liq_move_up_pct": _r(self.liq_move_up_pct),
            "uniform_liq_move_down_pct": _r(self.uniform_liq_move_down_pct),
            "uniform_liq_move_up_pct": _r(self.uniform_liq_move_up_pct),
            "first_liquidation_move_pct": _r(self.first_liquidation),
        }


def stress_account(account: MarginAccount, shocks: tuple[float, ...] = SHOCKS_PCT) -> Optional[AccountStress]:
    cross = account.cross_positions()
    if not cross:
        return None

    def surplus(model: Model) -> Callable[[float], float]:
        return lambda m: (lambda e: e[0] - e[1])(account_surplus(account, m, model))

    corr, uni = surplus(correlated_model), surplus(uniform_model)
    equity, maintenance = account_surplus(account, 0.0)
    moves = sorted([-s for s in shocks] + list(shocks))
    return AccountStress(
        venue=account.venue.value,
        network=account.network,
        symbols=[p.symbol for p in cross],
        wallet=account.wallet_balance,
        equity=equity,
        maintenance=maintenance,
        shocks=[Shock(m, *account_surplus(account, m)) for m in moves],
        liq_move_down_pct=liquidating_move(corr, -1, MAX_DOWN_PCT),
        liq_move_up_pct=liquidating_move(corr, 1, MAX_UP_PCT),
        uniform_liq_move_down_pct=liquidating_move(uni, -1, MAX_DOWN_PCT),
        uniform_liq_move_up_pct=liquidating_move(uni, 1, MAX_UP_PCT),
    )


@dataclass(frozen=True)
class LiqComparison:
    """The platform's displayed liquidation price next to LiqGuard's: others frozen vs coins moving together."""

    symbol: str
    venue: str
    side: str
    margin_mode: str
    mark_price: float
    platform_price: Optional[float]
    true_price: Optional[float]
    correlated_price: Optional[float]
    correlated_market_move_pct: Optional[float]

    @property
    def platform_distance_pct(self) -> float:
        return distance_pct(Side(self.side), self.mark_price, self.platform_price)

    @property
    def correlated_distance_pct(self) -> float:
        return distance_pct(Side(self.side), self.mark_price, self.correlated_price)

    @property
    def gap_pct(self) -> Optional[float]:
        """How much closer liquidation really is than the platform's line, in points of distance."""
        p, c = self.platform_distance_pct, self.correlated_distance_pct
        if math.isinf(c):
            return None
        return (p if not math.isinf(p) else 100.0) - c

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "side": self.side,
            "margin_mode": self.margin_mode,
            "mark_price": _r(self.mark_price, 6),
            "platform_price": _r(self.platform_price, 6),
            "platform_distance_pct": _r(self.platform_distance_pct),
            "true_price": _r(self.true_price, 6),
            "correlated_price": _r(self.correlated_price, 6),
            "correlated_distance_pct": _r(self.correlated_distance_pct),
            "correlated_market_move_pct": _r(self.correlated_market_move_pct),
            "gap_pct": _r(self.gap_pct),
        }


def liq_comparison(position: Position, account: Optional[MarginAccount], true_price: Optional[float],
                   stress: Optional[AccountStress]) -> LiqComparison:
    platform = position.liquidation_price if position.liquidation_price > 0 else true_price
    correlated, move = true_price, None
    if position.margin_mode == CROSS and account is not None and stress is not None:
        adverse = stress.liq_move_down_pct if position.side == Side.LONG else stress.liq_move_up_pct
        if adverse is not None:
            move = -adverse if position.side == Side.LONG else adverse
            correlated = shocked_price(position, move, correlated_model)
            if true_price is not None:
                closer = max if position.side == Side.LONG else min
                correlated = closer(correlated, true_price)
    elif true_price is not None:
        beta = factor(position.symbol).beta
        d = distance_pct(position.side, position.mark_price, true_price)
        move = (-d if position.side == Side.LONG else d) / beta if beta else None
    return LiqComparison(
        symbol=position.symbol, venue=position.venue.value, side=position.side.value,
        margin_mode=position.margin_mode, mark_price=position.mark_price,
        platform_price=platform, true_price=true_price, correlated_price=correlated, correlated_market_move_pct=move,
    )


@dataclass(frozen=True)
class Concentration:
    long_notional: float
    short_notional: float
    dominant_side: Optional[str]
    dominant_share_pct: float
    dominant_count: int
    avg_correlation: Optional[float]
    effective_bets: Optional[float]
    beta_weighted_net_usd: float
    positions: int
    flagged: bool = False
    symbols: list[str] = field(default_factory=list)

    def message(self) -> Optional[str]:
        if not self.flagged:
            return None
        return (
            f"{self.dominant_share_pct:.0f}% of exposure is {self.dominant_side} across {self.dominant_count} coins "
            f"that move together (avg correlation {self.avg_correlation:.2f}): your {self.positions} positions behave "
            f"like {self.effective_bets:.1f} independent bet{'s' if self.effective_bets >= 1.95 else ''}."
        )

    def to_dict(self) -> dict:
        return {
            "long_notional": _r(self.long_notional),
            "short_notional": _r(self.short_notional),
            "dominant_side": self.dominant_side,
            "dominant_share_pct": _r(self.dominant_share_pct, 1),
            "dominant_count": self.dominant_count,
            "avg_correlation": _r(self.avg_correlation),
            "effective_bets": _r(self.effective_bets),
            "beta_weighted_net_usd": _r(self.beta_weighted_net_usd),
            "positions": self.positions,
            "flagged": self.flagged,
            "symbols": self.symbols,
            "message": self.message(),
        }


def concentration(positions: list[Position], share_pct: float = CONCENTRATION_SHARE_PCT) -> Concentration:
    longs = [p for p in positions if p.side == Side.LONG]
    shorts = [p for p in positions if p.side == Side.SHORT]
    long_n, short_n = sum(p.notional_value for p in longs), sum(p.notional_value for p in shorts)
    total = long_n + short_n
    net_beta = sum(p.sign * p.notional_value * factor(p.symbol).beta for p in positions)
    if total <= 0:
        return Concentration(0.0, 0.0, None, 0.0, 0, None, None, 0.0, len(positions))
    side, group, share = ("long", longs, long_n / total * 100) if long_n >= short_n else ("short", shorts, short_n / total * 100)
    pairs = [(a, b) for i, a in enumerate(group) for b in group[i + 1:]]
    avg = sum(correlation(a.symbol, b.symbol) for a, b in pairs) / len(pairs) if pairs else None
    w = [p.sign * p.notional_value for p in positions]
    denom = sum(w[i] * w[j] * correlation(a.symbol, b.symbol) for i, a in enumerate(positions) for j, b in enumerate(positions))
    eff = (sum(abs(x) for x in w) ** 2 / denom) if denom > 0 else None
    flagged = share >= share_pct and len(group) >= 2 and avg is not None and avg >= CORRELATED
    return Concentration(long_n, short_n, side, share, len(group), avg, eff, net_beta, len(positions), flagged,
                         [p.symbol for p in group])
