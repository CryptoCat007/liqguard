"""
Priority triage: with several positions in trouble, which one first?

Each position gets an urgency score from 0 to 100:

- 55% **time to liquidation at typical volatility**. A 5-minute move has a
  typical size σ (the symbol's typical 5m adverse wick / 1.645, or a default
  per asset class). A random walk needs about (distance / σ)² five-minute
  steps to cover the distance, so a volatile coin 6% away can be more urgent
  than a calm one 4% away. u = 1 / (1 + minutes / 720).
- 30% **distance against its own (adaptive) tiers**. u = 1 - distance / warning
  tier, clamped to 0..1.
- 15% **size**: notional relative to the largest position. (In one cross
  wallet every position's loss-at-liquidation is the same shared buffer, so
  notional is what tells them apart.)

Positions inside a tier rank above those that are not; the top one is
labelled "Handle this first".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from liqguard.models import Position, Side, UserThreshold, WickProfile

from .sessions import is_stock_perp, underlying

HANDLE_FIRST = "Handle this first"
Z_TYPICAL = 1.645
DEFAULT_SIGMA_5M = {"BTC": 0.22, "ETH": 0.28}
DEFAULT_CRYPTO_SIGMA_5M = 0.40
DEFAULT_STOCK_SIGMA_5M = 0.15
WEIGHTS = (0.55, 0.30, 0.15)
TIME_SCALE_MINUTES = 720.0


def sigma_5m(position: Position, profile: Optional[WickProfile]) -> tuple[float, str]:
    stat = profile.stat("5m") if profile else None
    if stat is not None:
        typical = stat.typical_pct(position.side == Side.LONG)
        if typical > 0:
            return typical / Z_TYPICAL, "wicks"
    base = underlying(position.symbol)
    if base in DEFAULT_SIGMA_5M:
        return DEFAULT_SIGMA_5M[base], "default"
    return (DEFAULT_STOCK_SIGMA_5M if is_stock_perp(position.symbol) else DEFAULT_CRYPTO_SIGMA_5M), "default"


def time_to_liquidation_minutes(distance: float, sigma: float) -> float:
    if math.isinf(distance) or sigma <= 0:
        return math.inf
    return 5.0 * (max(distance, 0.0) / sigma) ** 2


@dataclass(frozen=True)
class TriageItem:
    symbol: str
    venue: str
    side: str
    distance_pct: float
    severity: Optional[str]
    warning_tier_pct: float
    sigma_5m_pct: float
    sigma_source: str
    minutes_to_liq: float
    loss_at_liq_usd: float
    notional: float
    score: float
    rank: int = 0
    of: int = 0
    label: Optional[str] = None

    @property
    def at_risk(self) -> bool:
        return self.severity is not None

    @property
    def typical_moves(self) -> float:
        """Distance in typical 5m moves: the volatility-adjusted distance the ranking is mostly built on."""
        if math.isinf(self.distance_pct) or self.sigma_5m_pct <= 0:
            return math.inf
        return max(self.distance_pct, 0.0) / self.sigma_5m_pct

    def reason(self) -> str:
        if math.isinf(self.minutes_to_liq):
            return "no liquidation price"
        t = self.minutes_to_liq
        when = f"~{t:.0f} min" if t < 120 else f"~{t / 60:.1f} h" if t < 72 * 60 else f"~{t / 1440:.0f} d"
        return (f"{self.distance_pct:.1f}% from liq ≈ {self.distance_pct / self.sigma_5m_pct:.1f} typical 5m moves "
                f"({when} at typical volatility), ${self.notional:,.0f} notional")

    def to_dict(self) -> dict:
        inf = math.isinf(self.minutes_to_liq)
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "side": self.side,
            "rank": self.rank,
            "of": self.of,
            "label": self.label,
            "score": round(self.score, 1),
            "severity": self.severity,
            "distance_pct": None if math.isinf(self.distance_pct) else round(self.distance_pct, 2),
            "warning_tier_pct": round(self.warning_tier_pct, 2),
            "sigma_5m_pct": round(self.sigma_5m_pct, 3),
            "sigma_source": self.sigma_source,
            "typical_moves": None if math.isinf(self.typical_moves) else round(self.typical_moves, 1),
            "minutes_to_liq": None if inf else round(self.minutes_to_liq, 1),
            "loss_at_liq_usd": round(self.loss_at_liq_usd, 2),
            "notional": round(self.notional, 2),
            "at_risk": self.at_risk,
            "reason": self.reason(),
        }


def triage(entries: list[tuple[Position, float, Optional[float], UserThreshold, Optional[WickProfile]]]) -> list[TriageItem]:
    """`entries`: (position, distance %, liquidation price, effective tiers, wick profile). Returns the ranked list."""
    raw = []
    for position, distance, liq, tiers, profile in entries:
        sigma, source = sigma_5m(position, profile)
        severity = tiers.get_severity_for_distance(distance) if not math.isinf(distance) else None
        loss = abs(position.size) * abs(position.mark_price - liq) if liq else 0.0
        raw.append((position, distance, severity, tiers.warning_distance_pct, sigma, source,
                    time_to_liquidation_minutes(distance, sigma), loss))
    max_notional = max((r[0].notional_value for r in raw), default=0.0) or 1.0
    items = []
    for position, distance, severity, warn, sigma, source, minutes, loss in raw:
        u_time = 0.0 if math.isinf(minutes) else 1 / (1 + minutes / TIME_SCALE_MINUTES)
        u_dist = 0.0 if math.isinf(distance) else min(max(1 - distance / warn, 0.0), 1.0) if warn > 0 else 0.0
        score = 100 * (WEIGHTS[0] * u_time + WEIGHTS[1] * u_dist + WEIGHTS[2] * position.notional_value / max_notional)
        items.append(TriageItem(
            symbol=position.symbol, venue=position.venue.value, side=position.side.value,
            distance_pct=distance, severity=severity.value if severity else None, warning_tier_pct=warn,
            sigma_5m_pct=sigma, sigma_source=source, minutes_to_liq=minutes, loss_at_liq_usd=loss,
            notional=position.notional_value, score=score,
        ))
    items.sort(key=lambda i: (not i.at_risk, -i.score, i.distance_pct))
    out = []
    for n, item in enumerate(items, start=1):
        label = HANDLE_FIRST if n == 1 and item.at_risk else None
        out.append(TriageItem(**{**item.__dict__, "rank": n, "of": len(items), "label": label}))
    return out


def focus(items: list[TriageItem]) -> Optional[dict]:
    """
    The one position every panel names as "riskiest" (triage rank #1), with
    the reason it outranks the position nearest in raw %. Summary, Muse,
    toasts and the triage card all read this, so they cannot disagree.
    """
    if not items:
        return None
    top = items[0]
    finite = [i for i in items if not math.isinf(i.distance_pct)]
    nearest = min(finite, key=lambda i: i.distance_pct) if finite else top
    same = (nearest.venue, nearest.symbol) == (top.venue, top.symbol)
    fewest_moves = min(items, key=lambda i: i.typical_moves)
    if len(items) == 1:
        basis = "only position"
    elif same:
        basis = "closest to liquidation"
    elif (fewest_moves.venue, fewest_moves.symbol) == (top.venue, top.symbol):
        basis = "closest in volatility-adjusted terms"
    elif top.at_risk and not nearest.at_risk:
        basis = "inside an alert tier"
    else:
        basis = "highest combined urgency"

    def moves(i: TriageItem) -> str:
        return "no liq" if math.isinf(i.typical_moves) else f"{round(i.typical_moves, 1):.1f} typical 5m moves"

    def dist(i: TriageItem) -> str:
        return "n/a" if math.isinf(i.distance_pct) else f"{round(i.distance_pct, 2):.1f}%"

    if same or len(items) == 1:
        why = f"{top.symbol}: {basis} ({dist(top)}, {moves(top)})"
    elif basis == "closest in volatility-adjusted terms":
        why = (f"{top.symbol}: {basis} — {dist(top)} is only {moves(top)}; {nearest.symbol} is nearer in % "
               f"({dist(nearest)}) but calmer, {moves(nearest)}")
    else:
        why = (f"{top.symbol}: {basis} (time to liq, distance vs tiers and size); {nearest.symbol} is nearer in % "
               f"({dist(nearest)}, {moves(nearest)}) vs {dist(top)}, {moves(top)}")
    return {
        "symbol": top.symbol,
        "venue": top.venue,
        "side": top.side,
        "rank": top.rank,
        "of": top.of,
        "label": top.label,
        "at_risk": top.at_risk,
        "severity": top.severity,
        "distance_pct": None if math.isinf(top.distance_pct) else round(top.distance_pct, 2),
        "typical_moves": None if math.isinf(top.typical_moves) else round(top.typical_moves, 1),
        "basis": basis,
        "why": why,
        "nearest_symbol": nearest.symbol,
        "nearest_distance_pct": None if math.isinf(nearest.distance_pct) else round(nearest.distance_pct, 2),
    }
