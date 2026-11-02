"""
Position sizing check: is a position too large for the account behind it?

Two limits from the user's settings:

- **Risk per trade** (`risk_per_trade_pct`, default 2%): the loss if the
  position is closed at its exit, as % of account equity. The exit is the
  protective stop when it fires before liquidation, else the liquidation
  price (without a working stop, liquidation *is* the exit).
- **Margin share** (`max_margin_pct`, default 25%): the margin the position
  ties up, as % of account equity.

The suggested size is the largest size that satisfies both. Account equity is
the venue account's cross wallet plus cross PnL, plus isolated margin and PnL.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from liqguard.i18n import Msg
from liqguard.models import CROSS, MarginAccount, Position, UserThreshold

from .stops import OK, PARTIAL, StopCheck

OVERSIZED = "oversized"
FITS = "ok"


def _r(v: Optional[float], dp: int = 2) -> Optional[float]:
    return None if v is None or (isinstance(v, float) and math.isinf(v)) else round(v, dp)


def account_equity(account: MarginAccount) -> float:
    equity = account.wallet_balance
    for p in account.positions:
        if p.margin_mode == CROSS:
            equity += p.unrealized_pnl
        else:
            equity += (p.isolated_wallet if p.isolated_wallet is not None else p.margin) + p.unrealized_pnl
    return equity


@dataclass(frozen=True)
class SizingCheck:
    symbol: str
    venue: str
    side: str
    size: float
    mark_price: float
    notional: float
    margin: float
    equity: float
    exit_price: Optional[float]
    exit_kind: str
    loss_at_exit_usd: Optional[float]
    risk_pct: Optional[float]
    margin_pct: float
    max_risk_pct: float
    max_margin_pct: float
    suggested_size: Optional[float]

    @property
    def status(self) -> str:
        if self.suggested_size is not None and self.size > self.suggested_size * 1.0001:
            return OVERSIZED
        return FITS

    @property
    def reduce_pct(self) -> Optional[float]:
        if self.status != OVERSIZED or not self.size:
            return None
        return (1 - self.suggested_size / abs(self.size)) * 100

    @property
    def reasons(self) -> list[str]:
        out = []
        if self.risk_pct is not None and self.risk_pct > self.max_risk_pct:
            out.append(Msg("risk.sizing.loses", risk=self.risk_pct, exit=Msg(f"risk.exit.{(self.exit_kind or 'none').replace(' ', '_')}"), limit=self.max_risk_pct))
        if self.margin_pct > self.max_margin_pct:
            out.append(Msg("risk.sizing.margin", margin=self.margin_pct, limit=self.max_margin_pct))
        return out

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "side": self.side,
            "size": self.size,
            "notional": _r(self.notional),
            "margin": _r(self.margin),
            "equity": _r(self.equity),
            "notional_x_equity": _r(self.notional / self.equity if self.equity > 0 else None),
            "exit_price": _r(self.exit_price, 6),
            "exit_kind": self.exit_kind,
            "exit_label": Msg(f"risk.exit.{(self.exit_kind or 'none').replace(' ', '_')}"),
            "loss_at_exit_usd": _r(self.loss_at_exit_usd),
            "risk_pct": _r(self.risk_pct),
            "margin_pct": _r(self.margin_pct, 1),
            "max_risk_pct": self.max_risk_pct,
            "max_margin_pct": self.max_margin_pct,
            "suggested_size": None if self.suggested_size is None else float(f"{self.suggested_size:.6g}"),
            "suggested_notional": _r(None if self.suggested_size is None else self.suggested_size * self.mark_price),
            "reduce_pct": _r(self.reduce_pct, 1),
            "status": self.status,
            "reasons": self.reasons,
        }


def size_check(
    position: Position,
    account: Optional[MarginAccount],
    threshold: UserThreshold,
    liquidation_price: Optional[float],
    stop: Optional[StopCheck] = None,
    liquidation_kind: str = "liquidation price",
) -> Optional[SizingCheck]:
    """None when the account's equity is unknown (no account data) or not positive."""
    if account is None:
        return None
    equity = account_equity(account)
    if equity <= 0 or position.mark_price <= 0:
        return None
    q, mark = abs(position.size), position.mark_price
    margin = position.margin if position.margin > 0 else (q * mark / position.leverage if position.leverage else 0.0)
    if stop is not None and stop.status in (OK, PARTIAL) and stop.stop_price is not None:
        exit_price, kind = stop.stop_price, "stop"
    elif liquidation_price:
        exit_price, kind = liquidation_price, liquidation_kind
    else:
        exit_price, kind = None, "none"
    per_unit = abs(mark - exit_price) if exit_price is not None else None
    loss = q * per_unit if per_unit is not None else None
    limits = []
    if per_unit:
        limits.append(threshold.risk_per_trade_pct / 100 * equity / per_unit)
    margin_per_unit = margin / q if q else 0.0
    if margin_per_unit > 0:
        limits.append(threshold.max_margin_pct / 100 * equity / margin_per_unit)
    return SizingCheck(
        symbol=position.symbol, venue=position.venue.value, side=position.side.value, size=q, mark_price=mark,
        notional=q * mark, margin=margin, equity=equity, exit_price=exit_price, exit_kind=kind,
        loss_at_exit_usd=loss, risk_pct=(loss / equity * 100) if loss is not None else None,
        margin_pct=margin / equity * 100, max_risk_pct=threshold.risk_per_trade_pct,
        max_margin_pct=threshold.max_margin_pct, suggested_size=min(limits) if limits else None,
    )
