"""
Portfolio data layer - clean API for positions, risk metrics, and advice.

This module provides a unified interface for portfolio data that can be used by:
- Telegram bot handlers
- Future web dashboard
- API endpoints
- Background monitoring

All calculations are deterministic and separated from presentation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from liqguard.i18n import is_zh
from liqguard.models import AlertSeverity, Position, UserThreshold
from liqguard.risk.advice import ActionableAdvice, ActionType, AdviceCalculator
from liqguard.risk.context import RiskContext
from liqguard.risk.engine import RiskAssessment, RiskEngine, exact_margin_to_target
from liqguard.risk.funding import project_funding
from liqguard.risk.liquidation import target_liquidation_price


class RiskLevel(str, Enum):
    """Overall portfolio risk level."""
    SAFE = "safe"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class PositionRiskSummary:
    """Risk summary for a single position."""
    symbol: str
    venue: str
    side: str
    size: float
    notional_value: float

    entry_price: float
    mark_price: float
    liquidation_price: float

    unrealized_pnl: float
    unrealized_pnl_pct: float

    leverage: float
    margin: float

    distance_to_liquidation_pct: float
    risk_score: float
    severity: Optional[AlertSeverity]

    margin_advice: Optional[ActionableAdvice]
    reduction_advice: Optional[ActionableAdvice]

    margin_mode: str = "isolated"
    computed_liquidation: Optional[dict] = None
    funding: Optional[dict] = None

    @property
    def true_liquidation_price(self) -> Optional[float]:
        return (self.computed_liquidation or {}).get("price")

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "side": self.side,
            "size": self.size,
            "notional_value": round(self.notional_value, 2),
            "entry_price": round(self.entry_price, 2),
            "mark_price": round(self.mark_price, 2),
            "liquidation_price": round(self.liquidation_price, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "unrealized_pnl_pct": round(self.unrealized_pnl_pct, 2),
            "leverage": self.leverage,
            "margin": round(self.margin, 2),
            "distance_to_liquidation_pct": round(self.distance_to_liquidation_pct, 2),
            "risk_score": round(self.risk_score, 2),
            "severity": self.severity.value if self.severity else None,
            "margin_advice": self.margin_advice.to_dict() if self.margin_advice else None,
            "reduction_advice": self.reduction_advice.to_dict() if self.reduction_advice else None,
            "margin_mode": self.margin_mode,
            "true_liquidation": self.computed_liquidation,
            "funding": self.funding,
        }


@dataclass
class PortfolioRiskOverview:
    """Complete portfolio risk overview."""

    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    total_positions: int = 0
    total_notional_value: float = 0.0
    total_margin_used: float = 0.0
    total_unrealized_pnl: float = 0.0

    positions_at_risk: int = 0
    positions_critical: int = 0
    positions_emergency: int = 0

    overall_risk_level: RiskLevel = RiskLevel.SAFE
    overall_risk_score: float = 0.0

    total_margin_needed_for_safety: float = 0.0

    weighted_avg_distance: float = 0.0
    min_distance_to_liquidation: float = float('inf')

    position_summaries: list[PositionRiskSummary] = field(default_factory=list)

    riskiest_position: Optional[PositionRiskSummary] = None

    long_exposure: float = 0.0
    short_exposure: float = 0.0
    net_exposure: float = 0.0

    venues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "summary": {
                "total_positions": self.total_positions,
                "total_notional_value": round(self.total_notional_value, 2),
                "total_margin_used": round(self.total_margin_used, 2),
                "total_unrealized_pnl": round(self.total_unrealized_pnl, 2),
            },
            "risk_metrics": {
                "overall_risk_level": self.overall_risk_level.value,
                "overall_risk_score": round(self.overall_risk_score, 2),
                "positions_at_risk": self.positions_at_risk,
                "positions_critical": self.positions_critical,
                "positions_emergency": self.positions_emergency,
                "min_distance_to_liquidation": round(self.min_distance_to_liquidation, 2) if self.min_distance_to_liquidation != float('inf') else None,
                "weighted_avg_distance": round(self.weighted_avg_distance, 2),
                "total_margin_needed_for_safety": round(self.total_margin_needed_for_safety, 2),
            },
            "exposure": {
                "long_exposure": round(self.long_exposure, 2),
                "short_exposure": round(self.short_exposure, 2),
                "net_exposure": round(self.net_exposure, 2),
            },
            "positions": [p.to_dict() for p in self.position_summaries],
            "riskiest_position": self.riskiest_position.to_dict() if self.riskiest_position else None,
            "venues": self.venues,
        }


class PortfolioAnalyzer:
    """
    Analyzer for portfolio risk metrics.

    Provides a clean API for calculating portfolio-level statistics
    that can be consumed by various frontends (Telegram, web, API).
    """

    def __init__(self, threshold: Optional[UserThreshold] = None, context: Optional[RiskContext] = None):
        """
        Initialize analyzer.

        Args:
            threshold: User's alert thresholds
            context: Margin accounts, funding and fees. Enables the computed
                (cross-margin aware) liquidation price and funding projections.
        """
        self.threshold = threshold or UserThreshold()
        self.context = context
        self.risk_engine = RiskEngine(self.threshold, context)

    def analyze_position(self, position: Position) -> PositionRiskSummary:
        """
        Analyze a single position and return comprehensive summary.
        """
        assessment = self.risk_engine.assess_position(position)

        computed, funding = None, None
        if self.context is not None and self.context.computable(position):
            account = self.context.account_for(position)
            computed = self.context.liquidation(position).to_dict()
            projection = project_funding(position, account, self.context.funding_for, self.context.now)
            funding = projection.to_dict() if projection else None
            margin_advice = self._exact_margin_advice(position, assessment)
        else:
            margin_advice = AdviceCalculator.calculate_margin_to_add(
                position, self.threshold.safety_target_pct
            )
        reduction_advice = AdviceCalculator.calculate_position_reduction(
            position, self.threshold.safety_target_pct
        )

        pnl_pct = 0.0
        if position.margin > 0:
            pnl_pct = (position.unrealized_pnl / position.margin) * 100

        return PositionRiskSummary(
            symbol=position.symbol,
            venue=position.venue.value,
            side=position.side.value,
            size=position.size,
            notional_value=position.notional_value,
            entry_price=position.entry_price,
            mark_price=position.mark_price,
            liquidation_price=position.liquidation_price,
            unrealized_pnl=position.unrealized_pnl,
            unrealized_pnl_pct=pnl_pct,
            leverage=position.leverage,
            margin=position.margin,
            distance_to_liquidation_pct=assessment.distance_to_liquidation_pct,
            risk_score=assessment.risk_score,
            severity=assessment.severity,
            margin_advice=margin_advice if margin_advice.margin_to_add_usd and margin_advice.margin_to_add_usd > 0 else None,
            reduction_advice=reduction_advice if reduction_advice.reduce_size_by and reduction_advice.reduce_size_by > 0 else None,
            margin_mode=computed["margin_mode"] if computed else position.margin_mode,
            computed_liquidation=computed,
            funding=funding,
        )

    def _exact_margin_advice(self, position: Position, assessment: RiskAssessment) -> ActionableAdvice:
        target = self.threshold.safety_target_pct
        amount = exact_margin_to_target(position, self.context, target)
        return ActionableAdvice(
            action_type=ActionType.ADD_MARGIN,
            symbol=position.symbol,
            venue=position.venue.value,
            current_distance_pct=assessment.distance_to_liquidation_pct,
            target_distance_pct=target,
            margin_to_add_usd=amount,
            new_liquidation_price=target_liquidation_price(position.side, position.mark_price, target) if amount else None,
            new_distance_pct=target if amount else assessment.distance_to_liquidation_pct,
            estimated_cost_usd=amount,
        )

    def analyze_portfolio(self, positions: list[Position]) -> PortfolioRiskOverview:
        """
        Analyze entire portfolio and return comprehensive overview.

        Args:
            positions: List of positions to analyze

        Returns:
            PortfolioRiskOverview with all metrics calculated
        """
        overview = PortfolioRiskOverview()

        if not positions:
            return overview

        overview.total_positions = len(positions)
        position_summaries = []
        venues = set()

        total_weighted_distance = 0.0
        total_weight = 0.0
        risk_scores = []

        for position in positions:
            summary = self.analyze_position(position)
            position_summaries.append(summary)

            venues.add(position.venue.value)

            overview.total_notional_value += summary.notional_value
            overview.total_margin_used += summary.margin
            overview.total_unrealized_pnl += summary.unrealized_pnl

            if summary.severity is not None:
                overview.positions_at_risk += 1
                if summary.severity == AlertSeverity.CRITICAL:
                    overview.positions_critical += 1
                elif summary.severity == AlertSeverity.EMERGENCY:
                    overview.positions_emergency += 1

            if summary.margin_advice:
                overview.total_margin_needed_for_safety += summary.margin_advice.margin_to_add_usd or 0

            if summary.distance_to_liquidation_pct < overview.min_distance_to_liquidation:
                overview.min_distance_to_liquidation = summary.distance_to_liquidation_pct

            weight = summary.notional_value
            total_weighted_distance += summary.distance_to_liquidation_pct * weight
            total_weight += weight

            risk_scores.append(summary.risk_score)

            if summary.side == "long":
                overview.long_exposure += summary.notional_value
            else:
                overview.short_exposure += summary.notional_value

        position_summaries.sort(key=lambda p: p.risk_score, reverse=True)
        overview.position_summaries = position_summaries

        if position_summaries:
            overview.riskiest_position = position_summaries[0]

        if total_weight > 0:
            overview.weighted_avg_distance = total_weighted_distance / total_weight

        if risk_scores:
            overview.overall_risk_score = sum(risk_scores) / len(risk_scores)

        overview.overall_risk_level = self._calculate_overall_risk_level(overview)

        overview.net_exposure = overview.long_exposure - overview.short_exposure

        overview.venues = sorted(venues)

        return overview

    def _calculate_overall_risk_level(self, overview: PortfolioRiskOverview) -> RiskLevel:
        """Calculate overall portfolio risk level."""
        if overview.positions_emergency > 0:
            return RiskLevel.CRITICAL

        if overview.positions_critical > 0:
            return RiskLevel.HIGH

        if overview.positions_at_risk > 0:
            return RiskLevel.MODERATE

        if overview.overall_risk_score > 50:
            return RiskLevel.MODERATE
        elif overview.overall_risk_score > 30:
            return RiskLevel.LOW

        return RiskLevel.SAFE

    def get_actionable_advice(
        self,
        positions: list[Position],
        max_actions: int = 5
    ) -> list[ActionableAdvice]:
        """
        Get prioritized list of actionable advice for the portfolio.

        Returns advice sorted by urgency (riskiest positions first).
        """
        advice_list = []

        assessments = self.risk_engine.assess_positions(positions)

        for assessment in assessments[:max_actions]:
            if assessment.severity is None:
                continue

            margin_advice = AdviceCalculator.calculate_margin_to_add(
                assessment.position, self.threshold.safety_target_pct
            )

            if margin_advice.margin_to_add_usd and margin_advice.margin_to_add_usd > 0:
                advice_list.append(margin_advice)

        return advice_list


def format_portfolio_overview_text(
    overview: PortfolioRiskOverview,
    language: str = "en"
) -> str:
    """
    Format portfolio overview as human-readable text.

    Args:
        overview: Portfolio risk overview
        language: Output language ('en' or 'zh-Hant'; any zh* tag counts as zh-Hant)

    Returns:
        Formatted text string
    """
    risk_emoji = {
        RiskLevel.SAFE: "✅",
        RiskLevel.LOW: "🟢",
        RiskLevel.MODERATE: "🟡",
        RiskLevel.HIGH: "🟠",
        RiskLevel.CRITICAL: "🔴",
    }

    if is_zh(language):
        text = f"""📊 **投資組合風險總覽**

{risk_emoji.get(overview.overall_risk_level, '❓')} 整體風險: **{overview.overall_risk_level.value.upper()}**
風險分數: {overview.overall_risk_score:.0f}/100

**持倉概要**
• 總持倉: {overview.total_positions}
• 總名義價值: ${overview.total_notional_value:,.2f}
• 已用保證金: ${overview.total_margin_used:,.2f}
• 未實現損益: ${overview.total_unrealized_pnl:,.2f}

**風險指標**
• 風險持倉: {overview.positions_at_risk}
• 嚴重風險: {overview.positions_critical}
• 緊急風險: {overview.positions_emergency}
• 最小清算距離: {overview.min_distance_to_liquidation:.1f}%
• 達到安全所需保證金: ${overview.total_margin_needed_for_safety:,.2f}

**曝險分析**
• 多頭曝險: ${overview.long_exposure:,.2f}
• 空頭曝險: ${overview.short_exposure:,.2f}
• 淨曝險: ${overview.net_exposure:,.2f}
"""
    else:
        text = f"""📊 **Portfolio Risk Overview**

{risk_emoji.get(overview.overall_risk_level, '❓')} Overall Risk: **{overview.overall_risk_level.value.upper()}**
Risk Score: {overview.overall_risk_score:.0f}/100

**Position Summary**
• Total Positions: {overview.total_positions}
• Total Notional Value: ${overview.total_notional_value:,.2f}
• Margin Used: ${overview.total_margin_used:,.2f}
• Unrealized PnL: ${overview.total_unrealized_pnl:,.2f}

**Risk Metrics**
• Positions at Risk: {overview.positions_at_risk}
• Critical: {overview.positions_critical}
• Emergency: {overview.positions_emergency}
• Min Distance to Liq: {overview.min_distance_to_liquidation:.1f}%
• Margin Needed for Safety: ${overview.total_margin_needed_for_safety:,.2f}

**Exposure Analysis**
• Long Exposure: ${overview.long_exposure:,.2f}
• Short Exposure: ${overview.short_exposure:,.2f}
• Net Exposure: ${overview.net_exposure:,.2f}
"""

    if overview.riskiest_position:
        rp = overview.riskiest_position
        if is_zh(language):
            text += f"""
**最高風險持倉**
{rp.symbol} {rp.side.upper()} @ {rp.leverage}x
距離清算: {rp.distance_to_liquidation_pct:.1f}%
"""
        else:
            text += f"""
**Riskiest Position**
{rp.symbol} {rp.side.upper()} @ {rp.leverage}x
Distance to Liq: {rp.distance_to_liquidation_pct:.1f}%
"""

    return text
