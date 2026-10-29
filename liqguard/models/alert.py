"""Alert models for liquidation warnings."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def _utcnow():
    """Get current UTC time in timezone-aware format."""
    return datetime.now(timezone.utc)


class AlertSeverity(str, Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


class AlertType(str, Enum):
    """Types of alerts."""
    DISTANCE_THRESHOLD = "distance_threshold"
    MARGIN_RATIO = "margin_ratio"
    RAPID_MOVEMENT = "rapid_movement"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"


@dataclass
class Alert:
    """Alert for a position risk event."""

    alert_type: AlertType
    severity: AlertSeverity
    symbol: str
    venue: str
    message: str
    distance_to_liquidation_pct: float
    margin_to_add_for_safety: float
    timestamp: datetime = field(default_factory=_utcnow)

    position_side: Optional[str] = None
    position_size: Optional[float] = None
    mark_price: Optional[float] = None
    liquidation_price: Optional[float] = None
    leverage: Optional[float] = None

    llm_explanation: Optional[str] = None
    llm_explanation_zh: Optional[str] = None
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "alert_type": self.alert_type.value,
            "severity": self.severity.value,
            "symbol": self.symbol,
            "venue": self.venue,
            "message": self.message,
            "distance_to_liquidation_pct": self.distance_to_liquidation_pct,
            "margin_to_add_for_safety": self.margin_to_add_for_safety,
            "timestamp": self.timestamp.isoformat(),
            "position_side": self.position_side,
            "position_size": self.position_size,
            "mark_price": self.mark_price,
            "liquidation_price": self.liquidation_price,
            "leverage": self.leverage,
            "llm_explanation": self.llm_explanation,
            "llm_explanation_zh": self.llm_explanation_zh,
            "actions": self.actions,
        }


@dataclass
class UserThreshold:
    """User-configurable alert thresholds."""

    warning_distance_pct: float = 20.0
    critical_distance_pct: float = 10.0
    emergency_distance_pct: float = 5.0

    margin_ratio_warning: float = 50.0
    margin_ratio_critical: float = 30.0
    margin_ratio_emergency: float = 15.0

    safety_target_pct: float = 30.0

    cooldown_seconds: int = 300

    # Wake-up escalation: unacknowledged critical/emergency alerts are resent every N minutes, up to a cap.
    escalation_enabled: bool = True
    escalation_interval_minutes: float = 2.0
    escalation_max_reminders: int = 5

    # Widen the tiers for volatile symbols, weekends, thin hours and closed stock markets.
    adaptive_thresholds: bool = True
    stop_loss_checks: bool = True

    # Position sizing: max loss at the exit (stop, else liquidation) and max margin per position, as % of account equity.
    risk_per_trade_pct: float = 2.0
    max_margin_pct: float = 25.0

    # Quiet hours (local hours at `quiet_utc_offset_hours`, default Taipei): warning-tier and
    # informational alerts are held back; critical and emergency alerts always go out.
    quiet_hours_start: Optional[int] = None
    quiet_hours_end: Optional[int] = None
    quiet_utc_offset_hours: float = 8.0

    def in_quiet_hours(self, now: datetime) -> bool:
        if self.quiet_hours_start is None or self.quiet_hours_end is None or self.quiet_hours_start == self.quiet_hours_end:
            return False
        hour = (now.astimezone(timezone.utc).hour + now.minute / 60 + self.quiet_utc_offset_hours) % 24
        start, end = self.quiet_hours_start, self.quiet_hours_end
        return start <= hour < end if start < end else hour >= start or hour < end

    def get_severity_for_distance(self, distance_pct: float) -> Optional[AlertSeverity]:
        """Get alert severity based on distance to liquidation."""
        if distance_pct <= self.emergency_distance_pct:
            return AlertSeverity.EMERGENCY
        elif distance_pct <= self.critical_distance_pct:
            return AlertSeverity.CRITICAL
        elif distance_pct <= self.warning_distance_pct:
            return AlertSeverity.WARNING
        return None

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "warning_distance_pct": self.warning_distance_pct,
            "critical_distance_pct": self.critical_distance_pct,
            "emergency_distance_pct": self.emergency_distance_pct,
            "margin_ratio_warning": self.margin_ratio_warning,
            "margin_ratio_critical": self.margin_ratio_critical,
            "margin_ratio_emergency": self.margin_ratio_emergency,
            "safety_target_pct": self.safety_target_pct,
            "cooldown_seconds": self.cooldown_seconds,
            "escalation_enabled": self.escalation_enabled,
            "escalation_interval_minutes": self.escalation_interval_minutes,
            "escalation_max_reminders": self.escalation_max_reminders,
            "adaptive_thresholds": self.adaptive_thresholds,
            "stop_loss_checks": self.stop_loss_checks,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_margin_pct": self.max_margin_pct,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "quiet_utc_offset_hours": self.quiet_utc_offset_hours,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserThreshold":
        """Create from dictionary."""
        return cls(
            warning_distance_pct=data.get("warning_distance_pct", 20.0),
            critical_distance_pct=data.get("critical_distance_pct", 10.0),
            emergency_distance_pct=data.get("emergency_distance_pct", 5.0),
            margin_ratio_warning=data.get("margin_ratio_warning", 50.0),
            margin_ratio_critical=data.get("margin_ratio_critical", 30.0),
            margin_ratio_emergency=data.get("margin_ratio_emergency", 15.0),
            safety_target_pct=data.get("safety_target_pct", 30.0),
            cooldown_seconds=data.get("cooldown_seconds", 300),
            escalation_enabled=bool(data.get("escalation_enabled", True)),
            escalation_interval_minutes=float(data.get("escalation_interval_minutes", 2.0)),
            escalation_max_reminders=int(data.get("escalation_max_reminders", 5)),
            adaptive_thresholds=bool(data.get("adaptive_thresholds", True)),
            stop_loss_checks=bool(data.get("stop_loss_checks", True)),
            risk_per_trade_pct=float(data.get("risk_per_trade_pct", 2.0)),
            max_margin_pct=float(data.get("max_margin_pct", 25.0)),
            quiet_hours_start=_opt_hour(data.get("quiet_hours_start")),
            quiet_hours_end=_opt_hour(data.get("quiet_hours_end")),
            quiet_utc_offset_hours=float(data.get("quiet_utc_offset_hours", 8.0)),
        )


def _opt_hour(value) -> Optional[int]:
    try:
        return None if value is None else int(value) % 24
    except (TypeError, ValueError):
        return None
