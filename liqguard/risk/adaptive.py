"""
Volatility-adaptive alert tiers.

The user's tiers are the floor. Each tier must also be wide enough for the
symbol's own wicks, then the whole set is widened when the market is thin:

    emergency ≥ deepest 1m wick (30 d)
    critical  ≥ deepest 5m wick
    warning   ≥ 2 × deepest 5m wick
    all tiers × session factor (weekend, low-liquidity hours, stock market closed; capped)

So a symbol that has wicked 6% in five minutes warns before it is one wick
from liquidation, and the same position warns earlier on Saturday night.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Optional

from liqguard.i18n import Msg
from liqguard.models import Position, Side, UserThreshold, WickProfile

from .sessions import SessionInfo

TIER_CAPS = {"emergency": 25.0, "critical": 40.0, "warning": 60.0}
MIN_GAP_PCT = 1.0


@dataclass(frozen=True)
class AdaptiveThreshold:
    base: UserThreshold
    threshold: UserThreshold
    floors: dict[str, float] = field(default_factory=dict)
    session: Optional[SessionInfo] = None
    reasons: list[str] = field(default_factory=list)

    @property
    def adapted(self) -> bool:
        return self.tiers() != _tiers(self.base)

    def tiers(self) -> dict[str, float]:
        return _tiers(self.threshold)

    def to_dict(self) -> dict:
        return {
            "base": {k: round(v, 2) for k, v in _tiers(self.base).items()},
            "effective": {k: round(v, 2) for k, v in self.tiers().items()},
            "floors": {k: round(v, 2) for k, v in self.floors.items()},
            "session": self.session.to_dict() if self.session else None,
            "reasons": self.reasons,
            "adapted": self.adapted,
        }


def _tiers(t: UserThreshold) -> dict[str, float]:
    return {"warning": t.warning_distance_pct, "critical": t.critical_distance_pct, "emergency": t.emergency_distance_pct}


def adaptive_threshold(
    base: UserThreshold,
    position: Position,
    profile: Optional[WickProfile] = None,
    session: Optional[SessionInfo] = None,
) -> AdaptiveThreshold:
    if not base.adaptive_thresholds:
        return AdaptiveThreshold(base, base, session=session, reasons=[Msg("risk.adaptive.off")])
    long = position.side == Side.LONG
    tiers = _tiers(base)
    floors: dict[str, float] = {}
    reasons: list[str] = []
    one = profile.stat("1m") if profile else None
    five = profile.stat("5m") if profile else None
    if one is not None:
        floors["emergency"] = one.max_pct(long)
    if five is not None:
        floors["critical"] = five.max_pct(long)
        floors["warning"] = 2 * five.max_pct(long)
    for tier, floor in floors.items():
        if floor > tiers[tier]:
            reasons.append(Msg("risk.adaptive.floor_x2" if tier == "warning" else "risk.adaptive.floor",
                               tier=Msg(f"common.tier.{tier}"), floor=floor, interval="1m" if tier == "emergency" else "5m"))
            tiers[tier] = floor
    if session is not None and session.factor > 1:
        tiers = {k: v * session.factor for k, v in tiers.items()}
        reasons.extend(session.reasons)
    tiers["emergency"] = min(tiers["emergency"], TIER_CAPS["emergency"])
    tiers["critical"] = min(max(tiers["critical"], tiers["emergency"] + MIN_GAP_PCT), TIER_CAPS["critical"])
    tiers["warning"] = min(max(tiers["warning"], tiers["critical"] + MIN_GAP_PCT), TIER_CAPS["warning"])
    threshold = dataclasses.replace(
        base,
        warning_distance_pct=tiers["warning"],
        critical_distance_pct=tiers["critical"],
        emergency_distance_pct=tiers["emergency"],
    )
    return AdaptiveThreshold(base, threshold, floors, session, reasons)
