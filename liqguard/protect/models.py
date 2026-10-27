"""Data types for protective actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from liqguard.models import Position
from liqguard.risk import ActionableAdvice, ActionType


class ExecutionTier(str, Enum):
    TIER_0_READONLY = "readonly"
    TIER_1_CONFIRM = "confirm"
    TIER_2_RULES = "rules"
    TIER_3_AUTO = "auto"


class ActionStatus(str, Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class ActionLimits:
    max_single_action_usd: float = 1000.0
    max_daily_total_usd: float = 5000.0
    max_position_reduction_pct: float = 50.0
    cooldown_seconds: int = 60


@dataclass
class ProtectiveActionConfig:
    """Execution is testnet-only; there is no mainnet switch."""

    enabled: bool = False
    tier: ExecutionTier = ExecutionTier.TIER_0_READONLY
    limits: ActionLimits = field(default_factory=ActionLimits)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "tier": self.tier.value,
            "testnet_only": True,
            "limits": {
                "max_single_action_usd": self.limits.max_single_action_usd,
                "max_daily_total_usd": self.limits.max_daily_total_usd,
                "max_position_reduction_pct": self.limits.max_position_reduction_pct,
                "cooldown_seconds": self.limits.cooldown_seconds,
            },
        }


@dataclass
class AuditLogEntry:
    timestamp: datetime
    user_id: int
    action_id: str
    action_type: ActionType
    symbol: str
    venue: str
    status: ActionStatus
    amount_usd: Optional[float]
    details: dict

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "user_id": self.user_id,
            "action_id": self.action_id,
            "action_type": self.action_type.value,
            "symbol": self.symbol,
            "venue": self.venue,
            "status": self.status.value,
            "amount_usd": self.amount_usd,
            "details": self.details,
        }


@dataclass
class ProtectiveAction:
    action_id: str
    user_id: int
    advice: ActionableAdvice
    position: Position
    status: ActionStatus = ActionStatus.PROPOSED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    confirmed_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "user_id": self.user_id,
            "advice": self.advice.to_dict(),
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
            "error_message": self.error_message,
        }
