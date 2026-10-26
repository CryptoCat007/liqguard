"""Risk engine for calculating liquidation distances and risk assessments."""

import logging
from dataclasses import dataclass
from typing import Optional

from liqguard.models import Alert, AlertSeverity, AlertType, Position, Side, UserThreshold

logger = logging.getLogger(__name__)


@dataclass
class RiskAssessment:
    """Risk assessment result for a position."""

    position: Position
    distance_to_liquidation_pct: float
    severity: Optional[AlertSeverity]
    margin_to_safety: float
    risk_score: float

    is_at_risk: bool
    is_critical: bool
    is_emergency: bool

    @property
    def risk_level_str(self) -> str:
        """Human-readable risk level."""
        if self.is_emergency:
            return "EMERGENCY"
        elif self.is_critical:
            return "CRITICAL"
        elif self.is_at_risk:
            return "WARNING"
        return "SAFE"


class RiskEngine:
    """
    Risk engine for calculating liquidation risk.

    Core calculations:
    1. Distance to liquidation (mark price to liq price)
    2. Margin required to reach safety target
    3. Risk scoring for prioritization
    """

    def __init__(self, threshold: Optional[UserThreshold] = None):
        """
        Initialize risk engine.

        Args:
            threshold: User's alert thresholds. Uses defaults if not provided.
        """
        self.threshold = threshold or UserThreshold()

    @staticmethod
    def calculate_distance_to_liquidation(
        mark_price: float,
        liquidation_price: float,
        side: Side
    ) -> float:
        """
        Calculate distance from mark price to liquidation price as percentage.

        Args:
            mark_price: Current mark price
            liquidation_price: Liquidation price
            side: Position side (LONG or SHORT)

        Returns:
            Distance as percentage. Positive = safe space, negative = past liquidation.

        Examples:
            LONG BTC @ mark=65000, liq=60000: (65000-60000)/65000 * 100 = 7.69%
            SHORT ETH @ mark=3200, liq=3500: (3500-3200)/3200 * 100 = 9.38%
        """
        if liquidation_price <= 0 or mark_price <= 0:
            return float('inf')

        if side == Side.LONG:
            return ((mark_price - liquidation_price) / mark_price) * 100
        else:
            return ((liquidation_price - mark_price) / mark_price) * 100

    @staticmethod
    def calculate_margin_to_reach_distance(
        position: Position,
        target_distance_pct: float
    ) -> float:
        """
        Calculate additional margin needed to reach a target distance from liquidation.

        This is an approximation. The exact calculation varies by exchange
        and depends on maintenance margin rates, funding rates, etc.

        Args:
            position: Position to analyze
            target_distance_pct: Target distance (e.g., 30 for 30%)

        Returns:
            Additional margin required in USD. Returns 0 if already safe.
        """
        current_distance = position.distance_to_liquidation_pct

        if current_distance >= target_distance_pct:
            return 0.0

        distance_gap = target_distance_pct - current_distance

        price_change_needed = (distance_gap / 100) * position.mark_price

        additional_margin = (price_change_needed * position.size) / position.leverage

        return max(0.0, additional_margin)

    @staticmethod
    def calculate_new_liquidation_price(
        current_liq_price: float,
        current_margin: float,
        additional_margin: float,
        size: float,
        side: Side
    ) -> float:
        """
        Estimate new liquidation price after adding margin.

        Args:
            current_liq_price: Current liquidation price
            current_margin: Current margin amount
            additional_margin: Additional margin to add
            size: Position size
            side: Position side

        Returns:
            Estimated new liquidation price
        """
        if current_margin <= 0 or size <= 0:
            return current_liq_price

        price_shift = additional_margin / size

        if side == Side.LONG:
            return current_liq_price - price_shift
        else:
            return current_liq_price + price_shift

    def calculate_risk_score(self, position: Position) -> float:
        """
        Calculate a risk score from 0 to 100.

        Higher score = more risk.

        Factors:
        - Distance to liquidation (primary)
        - Leverage (higher = more risk)
        - Position size / notional value
        """
        distance = position.distance_to_liquidation_pct

        if distance <= 0:
            return 100.0
        elif distance >= 50:
            base_score = 0.0
        else:
            base_score = ((50 - distance) / 50) * 70

        leverage_factor = min(position.leverage / 125, 1.0) * 15

        notional = position.notional_value
        if notional > 100000:
            size_factor = 15
        elif notional > 50000:
            size_factor = 10
        elif notional > 10000:
            size_factor = 5
        else:
            size_factor = 0

        return min(100.0, base_score + leverage_factor + size_factor)

    def assess_position(self, position: Position) -> RiskAssessment:
        """
        Perform full risk assessment on a position.

        Args:
            position: Position to assess

        Returns:
            RiskAssessment with all calculated metrics
        """
        distance = self.calculate_distance_to_liquidation(
            position.mark_price,
            position.liquidation_price,
            position.side
        )

        severity = self.threshold.get_severity_for_distance(distance)

        margin_to_safety = self.calculate_margin_to_reach_distance(
            position,
            self.threshold.safety_target_pct
        )

        risk_score = self.calculate_risk_score(position)

        is_at_risk = distance <= self.threshold.warning_distance_pct
        is_critical = distance <= self.threshold.critical_distance_pct
        is_emergency = distance <= self.threshold.emergency_distance_pct

        return RiskAssessment(
            position=position,
            distance_to_liquidation_pct=distance,
            severity=severity,
            margin_to_safety=margin_to_safety,
            risk_score=risk_score,
            is_at_risk=is_at_risk,
            is_critical=is_critical,
            is_emergency=is_emergency,
        )

    def assess_positions(
        self,
        positions: list[Position]
    ) -> list[RiskAssessment]:
        """
        Assess multiple positions and sort by risk.

        Args:
            positions: List of positions to assess

        Returns:
            List of RiskAssessments sorted by risk score (highest first)
        """
        assessments = [self.assess_position(pos) for pos in positions]
        return sorted(assessments, key=lambda a: a.risk_score, reverse=True)

    def create_alert(
        self,
        assessment: RiskAssessment,
        alert_type: AlertType = AlertType.DISTANCE_THRESHOLD
    ) -> Optional[Alert]:
        """
        Create an alert from a risk assessment if warranted.

        Args:
            assessment: Risk assessment result
            alert_type: Type of alert to create

        Returns:
            Alert if position is at risk, None otherwise
        """
        if assessment.severity is None:
            return None

        pos = assessment.position

        severity_emoji = {
            AlertSeverity.WARNING: "⚠️",
            AlertSeverity.CRITICAL: "🚨",
            AlertSeverity.EMERGENCY: "🆘",
        }.get(assessment.severity, "ℹ️")

        message = (
            f"{severity_emoji} {assessment.severity.value.upper()}: "
            f"{pos.symbol} {pos.side.value.upper()} position\n"
            f"Distance to liquidation: {assessment.distance_to_liquidation_pct:.2f}%"
        )

        return Alert(
            alert_type=alert_type,
            severity=assessment.severity,
            symbol=pos.symbol,
            venue=pos.venue.value,
            message=message,
            distance_to_liquidation_pct=assessment.distance_to_liquidation_pct,
            margin_to_add_for_safety=assessment.margin_to_safety,
            position_side=pos.side.value,
            position_size=pos.size,
            mark_price=pos.mark_price,
            liquidation_price=pos.liquidation_price,
            leverage=pos.leverage,
            actions=[
                f"Add ${assessment.margin_to_safety:.2f} margin to reach {self.threshold.safety_target_pct}% safety",
                "Reduce position size",
                "Add a stop loss order",
            ] if assessment.margin_to_safety > 0 else ["Position is at safe distance"],
        )
