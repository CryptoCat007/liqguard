"""Pure risk calculations: liquidation (isolated and cross), funding, fees, rescue options, portfolio aggregation.

Depends only on `liqguard.models`; no I/O, no network, no framework imports.
"""

from .advice import ActionableAdvice, ActionType, AdviceCalculator
from .context import RiskContext
from .engine import RiskAssessment, RiskEngine, exact_margin_to_target
from .fees import DEFAULT_TAKER_FEES, FeeSchedule
from .funding import (
    PROJECTION_LABEL,
    FundingProjection,
    PreFundingWarning,
    funding_payment,
    next_settlement,
    pre_funding_warning,
    project_funding,
    settlements_within,
)
from .liquidation import (
    LiquidationResult,
    build_brackets,
    default_brackets,
    distance_pct,
    hyperliquid_brackets,
    maintenance_margin,
    solve_liquidation_price,
    true_liquidation,
)
from .portfolio import (
    PortfolioAnalyzer,
    PortfolioRiskOverview,
    PositionRiskSummary,
    RiskLevel,
    format_portfolio_overview_text,
)
from .rescue import ADD_MARGIN, HEDGE, REDUCE, RescueOption, RescuePlan, RescueSimulator

__all__ = [
    "ActionableAdvice",
    "ActionType",
    "AdviceCalculator",
    "RiskContext",
    "RiskAssessment",
    "RiskEngine",
    "exact_margin_to_target",
    "DEFAULT_TAKER_FEES",
    "FeeSchedule",
    "PROJECTION_LABEL",
    "FundingProjection",
    "PreFundingWarning",
    "funding_payment",
    "next_settlement",
    "pre_funding_warning",
    "project_funding",
    "settlements_within",
    "LiquidationResult",
    "build_brackets",
    "default_brackets",
    "distance_pct",
    "hyperliquid_brackets",
    "maintenance_margin",
    "solve_liquidation_price",
    "true_liquidation",
    "PortfolioAnalyzer",
    "PortfolioRiskOverview",
    "PositionRiskSummary",
    "RiskLevel",
    "format_portfolio_overview_text",
    "ADD_MARGIN",
    "HEDGE",
    "REDUCE",
    "RescueOption",
    "RescuePlan",
    "RescueSimulator",
]
