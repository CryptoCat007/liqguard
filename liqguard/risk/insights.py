"""Portfolio-level checks for one user's book: triage, stress test, platform vs correlated liq, concentration and sizing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from liqguard.models import CROSS, Position, UserThreshold

from .context import RiskContext
from .protection import PositionProtection, position_protection
from .sizing import OVERSIZED, SizingCheck, size_check
from .stress import (
    AccountStress,
    Concentration,
    LiqComparison,
    concentration,
    liq_comparison,
    stress_account,
)
from .triage import TriageItem, focus, triage


def _key(p: Position) -> tuple[str, str]:
    return p.venue.value, p.symbol


@dataclass(frozen=True)
class PortfolioInsights:
    triage: list[TriageItem]
    stress: list[AccountStress]
    comparisons: list[LiqComparison]
    concentration: Concentration
    sizing: list[SizingCheck]

    @property
    def top(self) -> Optional[TriageItem]:
        return self.triage[0] if self.triage and self.triage[0].label else None

    @property
    def focus(self) -> Optional[dict]:
        return focus(self.triage)

    @property
    def at_risk(self) -> list[TriageItem]:
        return [t for t in self.triage if t.at_risk]

    @property
    def oversized(self) -> list[SizingCheck]:
        return [s for s in self.sizing if s.status == OVERSIZED]

    def priority(self, position: Position) -> Optional[TriageItem]:
        return next((t for t in self.triage if (t.venue, t.symbol) == _key(position)), None)

    def comparison(self, position: Position) -> Optional[LiqComparison]:
        return next((c for c in self.comparisons if (c.venue, c.symbol) == _key(position)), None)

    def sizing_for(self, position: Position) -> Optional[SizingCheck]:
        return next((s for s in self.sizing if (s.venue, s.symbol) == _key(position)), None)

    def worst_stress(self) -> Optional[AccountStress]:
        moves = [(abs(s.first_liquidation), s) for s in self.stress if s.first_liquidation is not None]
        return min(moves, key=lambda x: x[0])[1] if moves else None

    def to_dict(self) -> dict:
        worst = self.worst_stress()
        return {
            "triage": [t.to_dict() for t in self.triage],
            "top": self.top.to_dict() if self.top else None,
            "focus": self.focus,
            "at_risk_count": len(self.at_risk),
            "stress": [s.to_dict() for s in self.stress],
            "worst_account": None if worst is None else {
                "venue": worst.venue, "network": worst.network,
                "first_liquidation_move_pct": round(worst.first_liquidation, 2),
                "uniform_liq_move_down_pct": None if worst.uniform_liq_move_down_pct is None else round(worst.uniform_liq_move_down_pct, 2),
            },
            "comparisons": [c.to_dict() for c in self.comparisons],
            "concentration": self.concentration.to_dict(),
            "sizing": [s.to_dict() for s in self.sizing],
            "oversized": [s.symbol for s in self.oversized],
        }


def portfolio_insights(
    positions: list[Position],
    context: RiskContext,
    threshold: UserThreshold,
    protections: Optional[dict[tuple[str, str], PositionProtection]] = None,
) -> PortfolioInsights:
    protections = dict(protections or {})
    for p in positions:
        if _key(p) not in protections:
            protections[_key(p)] = position_protection(p, context, threshold)

    stresses: dict[int, AccountStress] = {}
    for account in context.accounts:
        if any(account.holds(p) for p in positions):
            s = stress_account(account)
            if s is not None:
                stresses[id(account)] = s

    comparisons, sizing, entries = [], [], []
    for p in positions:
        account = context.account_for(p)
        prot = protections[_key(p)]
        computed = context.liquidation(p).price if context.computable(p) else None
        true_price = computed if computed is not None else (p.liquidation_price or None)
        comparison = liq_comparison(p, account, true_price, stresses.get(id(account)) if account else None)
        comparisons.append(comparison)
        liq = context.liquidation_price(p)
        if comparison.correlated_market_move_pct is not None and comparison.correlated_price and p.margin_mode == CROSS:
            check = size_check(p, account, threshold, comparison.correlated_price, prot.stop, "correlated liquidation")
        else:
            check = size_check(p, account, threshold, liq, prot.stop)
        if check is not None:
            sizing.append(check)
        entries.append((p, prot.distance_pct, liq, prot.adaptive.threshold, context.wicks_for(p)))

    return PortfolioInsights(
        triage=triage(entries),
        stress=list(stresses.values()),
        comparisons=comparisons,
        concentration=concentration(positions),
        sizing=sizing,
    )
