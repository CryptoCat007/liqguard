"""Pure risk calculations: liquidation (isolated and cross), funding, fees, rescue options, portfolio aggregation.

Depends only on `liqguard.models`; no I/O, no network, no framework imports.
"""

from .adaptive import AdaptiveThreshold, adaptive_threshold
from .adl import ADL_WARN_LIGHTS, AdlStatus, adl_status, lights_from_quantile
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
from .protection import PositionProtection, position_protection
from .rescue import ADD_MARGIN, HEDGE, REDUCE, RescueOption, RescuePlan, RescueSimulator
from .sessions import (
    SessionInfo,
    is_stock_perp,
    market_session,
    parse_hours,
    underlying,
    us_equity_market_open,
)
from .stops import (
    BEYOND_LIQUIDATION,
    NO_STOP,
    TOO_CLOSE,
    StopCheck,
    StopFix,
    check_stop_loss,
    protective_stops,
)
from .wicks import WickExposure, aggregate, stress_summary, wick_exposure, wick_stat

__all__ = [
    "AdaptiveThreshold",
    "adaptive_threshold",
    "ADL_WARN_LIGHTS",
    "AdlStatus",
    "adl_status",
    "lights_from_quantile",
    "SessionInfo",
    "is_stock_perp",
    "market_session",
    "parse_hours",
    "underlying",
    "us_equity_market_open",
    "BEYOND_LIQUIDATION",
    "NO_STOP",
    "TOO_CLOSE",
    "StopCheck",
    "StopFix",
    "check_stop_loss",
    "protective_stops",
    "PositionProtection",
    "position_protection",
    "WickExposure",
    "aggregate",
    "stress_summary",
    "wick_exposure",
    "wick_stat",
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
