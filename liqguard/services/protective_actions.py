"""
Protective Actions - Tier 1 Implementation

Risk-reducing actions with user confirmation (Tier 1).
Tier 2 (rules-based auto) and Tier 3 (full-auto) are documented as future work.

Guardrails:
- Never withdrawal permission
- Per-action amount caps
- Daily amount caps per user
- Only risk-reducing actions (add margin, reduce position)
- Audit log of every step
- One-tap kill switch
- Testnet-only execution (mainnet disabled by config)
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from liqguard.models import Position
from liqguard.services.advice import ActionableAdvice, ActionType

logger = logging.getLogger(__name__)


class ExecutionTier(str, Enum):
    """Execution tiers for protective actions."""
    TIER_0_READONLY = "readonly"
    TIER_1_CONFIRM = "confirm"
    TIER_2_RULES = "rules"
    TIER_3_AUTO = "auto"


class ActionStatus(str, Enum):
    """Status of a protective action."""
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class ActionLimits:
    """Per-user action limits."""
    max_single_action_usd: float = 1000.0
    max_daily_total_usd: float = 5000.0
    max_position_reduction_pct: float = 50.0
    cooldown_seconds: int = 60


@dataclass
class ProtectiveActionConfig:
    """Configuration for protective actions system."""
    enabled: bool = False
    tier: ExecutionTier = ExecutionTier.TIER_0_READONLY
    
    mainnet_enabled: bool = False
    testnet_only: bool = True
    
    binance_testnet_url: str = "https://testnet.binancefuture.com"
    hyperliquid_testnet: bool = True
    
    limits: ActionLimits = field(default_factory=ActionLimits)
    
    audit_log_path: str = "audit.log"
    
    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "tier": self.tier.value,
            "mainnet_enabled": self.mainnet_enabled,
            "testnet_only": self.testnet_only,
            "limits": {
                "max_single_action_usd": self.limits.max_single_action_usd,
                "max_daily_total_usd": self.limits.max_daily_total_usd,
                "max_position_reduction_pct": self.limits.max_position_reduction_pct,
                "cooldown_seconds": self.limits.cooldown_seconds,
            }
        }


@dataclass
class AuditLogEntry:
    """Audit log entry for a protective action."""
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
    """A protective action pending or completed."""
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


class AuditLogger:
    """Audit logger for all protective actions."""
    
    def __init__(self, log_path: str = "audit.log"):
        self.log_path = Path(log_path)
        self._entries: list[AuditLogEntry] = []
    
    def log(self, entry: AuditLogEntry) -> None:
        """Log an audit entry."""
        self._entries.append(entry)
        
        try:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(entry.to_dict()) + "\n")
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")
        
        logger.info(f"AUDIT: {entry.action_type.value} {entry.status.value} for user {entry.user_id}")
    
    def get_user_entries(
        self, 
        user_id: int, 
        since: Optional[datetime] = None
    ) -> list[AuditLogEntry]:
        """Get audit entries for a user."""
        entries = [e for e in self._entries if e.user_id == user_id]
        if since:
            entries = [e for e in entries if e.timestamp >= since]
        return entries


class ProtectiveActionsManager:
    """
    Manager for protective actions with guardrails.
    
    Tier 1 (current): User confirmation required for every action.
    Tier 2 (future): Rules-based automation with user-defined conditions.
    Tier 3 (future): Full automation with AI-driven decisions.
    """
    
    def __init__(self, config: Optional[ProtectiveActionConfig] = None):
        self.config = config or ProtectiveActionConfig()
        self.audit = AuditLogger(self.config.audit_log_path)
        
        self._pending_actions: dict[str, ProtectiveAction] = {}
        self._user_daily_totals: dict[int, float] = {}
        self._user_kill_switch: set[int] = set()
        
        self._action_counter = 0
    
    def _generate_action_id(self) -> str:
        """Generate unique action ID."""
        self._action_counter += 1
        return f"pa_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{self._action_counter:04d}"
    
    def is_enabled(self) -> bool:
        """Check if protective actions are enabled."""
        return self.config.enabled and self.config.tier != ExecutionTier.TIER_0_READONLY
    
    def is_execution_allowed(self, venue: str) -> bool:
        """Check if execution is allowed for a venue."""
        if not self.is_enabled():
            return False
        
        if self.config.testnet_only and not self.config.mainnet_enabled:
            return True
        
        return False
    
    def activate_kill_switch(self, user_id: int) -> None:
        """Activate kill switch for a user - stops all actions immediately."""
        self._user_kill_switch.add(user_id)
        
        for action in self._pending_actions.values():
            if action.user_id == user_id and action.status in [ActionStatus.PROPOSED, ActionStatus.CONFIRMED]:
                action.status = ActionStatus.CANCELLED
                self.audit.log(AuditLogEntry(
                    timestamp=datetime.now(timezone.utc),
                    user_id=user_id,
                    action_id=action.action_id,
                    action_type=action.advice.action_type,
                    symbol=action.advice.symbol,
                    venue=action.advice.venue,
                    status=ActionStatus.CANCELLED,
                    amount_usd=action.advice.estimated_cost_usd,
                    details={"reason": "kill_switch_activated"},
                ))
        
        logger.warning(f"Kill switch activated for user {user_id}")
    
    def deactivate_kill_switch(self, user_id: int) -> None:
        """Deactivate kill switch for a user."""
        self._user_kill_switch.discard(user_id)
        logger.info(f"Kill switch deactivated for user {user_id}")
    
    def is_kill_switch_active(self, user_id: int) -> bool:
        """Check if kill switch is active for a user."""
        return user_id in self._user_kill_switch
    
    def validate_action(
        self, 
        user_id: int, 
        advice: ActionableAdvice
    ) -> tuple[bool, str]:
        """
        Validate an action against guardrails.
        
        Returns (is_valid, reason).
        """
        if self.is_kill_switch_active(user_id):
            return False, "Kill switch is active. Deactivate it first."
        
        if not self.is_enabled():
            return False, "Protective actions are disabled."
        
        if advice.action_type not in [ActionType.ADD_MARGIN, ActionType.REDUCE_POSITION]:
            return False, "Only risk-reducing actions are allowed."
        
        amount = advice.estimated_cost_usd or advice.margin_to_add_usd or 0
        if amount > self.config.limits.max_single_action_usd:
            return False, f"Amount ${amount:.2f} exceeds single action limit ${self.config.limits.max_single_action_usd:.2f}"
        
        daily_total = self._user_daily_totals.get(user_id, 0)
        if daily_total + amount > self.config.limits.max_daily_total_usd:
            return False, f"Would exceed daily limit. Used: ${daily_total:.2f}, Limit: ${self.config.limits.max_daily_total_usd:.2f}"
        
        if advice.reduce_size_pct and advice.reduce_size_pct > self.config.limits.max_position_reduction_pct:
            return False, f"Reduction {advice.reduce_size_pct:.1f}% exceeds limit {self.config.limits.max_position_reduction_pct:.1f}%"
        
        return True, "Validation passed"
    
    def propose_action(
        self,
        user_id: int,
        advice: ActionableAdvice,
        position: Position
    ) -> tuple[Optional[ProtectiveAction], str]:
        """
        Propose a protective action for user confirmation.
        
        Returns (action, message).
        """
        is_valid, reason = self.validate_action(user_id, advice)
        if not is_valid:
            self.audit.log(AuditLogEntry(
                timestamp=datetime.now(timezone.utc),
                user_id=user_id,
                action_id="rejected",
                action_type=advice.action_type,
                symbol=advice.symbol,
                venue=advice.venue,
                status=ActionStatus.REJECTED,
                amount_usd=advice.estimated_cost_usd,
                details={"reason": reason},
            ))
            return None, reason
        
        action = ProtectiveAction(
            action_id=self._generate_action_id(),
            user_id=user_id,
            advice=advice,
            position=position,
        )
        
        self._pending_actions[action.action_id] = action
        
        self.audit.log(AuditLogEntry(
            timestamp=datetime.now(timezone.utc),
            user_id=user_id,
            action_id=action.action_id,
            action_type=advice.action_type,
            symbol=advice.symbol,
            venue=advice.venue,
            status=ActionStatus.PROPOSED,
            amount_usd=advice.estimated_cost_usd,
            details={"position": position.to_dict()},
        ))
        
        return action, "Action proposed. Tap confirm to execute."
    
    def confirm_action(self, action_id: str, user_id: int) -> tuple[bool, str]:
        """
        Confirm a proposed action (Tier 1 user confirmation).
        
        Returns (success, message).
        """
        action = self._pending_actions.get(action_id)
        if not action:
            return False, "Action not found."
        
        if action.user_id != user_id:
            return False, "Action belongs to another user."
        
        if action.status != ActionStatus.PROPOSED:
            return False, f"Action is already {action.status.value}."
        
        if self.is_kill_switch_active(user_id):
            action.status = ActionStatus.CANCELLED
            return False, "Kill switch is active."
        
        action.status = ActionStatus.CONFIRMED
        action.confirmed_at = datetime.now(timezone.utc)
        
        self.audit.log(AuditLogEntry(
            timestamp=datetime.now(timezone.utc),
            user_id=user_id,
            action_id=action.action_id,
            action_type=action.advice.action_type,
            symbol=action.advice.symbol,
            venue=action.advice.venue,
            status=ActionStatus.CONFIRMED,
            amount_usd=action.advice.estimated_cost_usd,
            details={},
        ))
        
        return True, "Action confirmed. Executing..."
    
    async def execute_action(self, action_id: str) -> tuple[bool, str]:
        """
        Execute a confirmed action.
        
        Currently only supports testnet execution.
        """
        action = self._pending_actions.get(action_id)
        if not action:
            return False, "Action not found."
        
        if action.status != ActionStatus.CONFIRMED:
            return False, f"Action must be confirmed first. Current status: {action.status.value}"
        
        if not self.is_execution_allowed(action.advice.venue):
            action.status = ActionStatus.FAILED
            action.error_message = "Execution not allowed for this venue/network"
            return False, "Execution not allowed. Only testnet is enabled."
        
        action.status = ActionStatus.EXECUTING
        
        try:
            if action.advice.venue == "binance":
                result = await self._execute_binance_testnet(action)
            elif action.advice.venue == "hyperliquid":
                result = await self._execute_hyperliquid_testnet(action)
            else:
                result = False, f"Venue {action.advice.venue} not supported for execution"
            
            if result[0]:
                action.status = ActionStatus.COMPLETED
                action.executed_at = datetime.now(timezone.utc)
                
                amount = action.advice.estimated_cost_usd or 0
                self._user_daily_totals[action.user_id] = self._user_daily_totals.get(action.user_id, 0) + amount
            else:
                action.status = ActionStatus.FAILED
                action.error_message = result[1]
            
            self.audit.log(AuditLogEntry(
                timestamp=datetime.now(timezone.utc),
                user_id=action.user_id,
                action_id=action.action_id,
                action_type=action.advice.action_type,
                symbol=action.advice.symbol,
                venue=action.advice.venue,
                status=action.status,
                amount_usd=action.advice.estimated_cost_usd,
                details={"result": result[1]},
            ))
            
            return result
            
        except Exception as e:
            action.status = ActionStatus.FAILED
            action.error_message = str(e)
            logger.error(f"Action execution failed: {e}")
            return False, str(e)
    
    async def _execute_binance_testnet(self, action: ProtectiveAction) -> tuple[bool, str]:
        """Execute action on Binance Futures testnet."""
        return False, "Binance testnet execution not yet implemented"
    
    async def _execute_hyperliquid_testnet(self, action: ProtectiveAction) -> tuple[bool, str]:
        """Execute action on Hyperliquid testnet."""
        return False, "Hyperliquid testnet execution not yet implemented"
    
    def cancel_action(self, action_id: str, user_id: int) -> tuple[bool, str]:
        """Cancel a pending action."""
        action = self._pending_actions.get(action_id)
        if not action:
            return False, "Action not found."
        
        if action.user_id != user_id:
            return False, "Action belongs to another user."
        
        if action.status not in [ActionStatus.PROPOSED, ActionStatus.CONFIRMED]:
            return False, f"Cannot cancel action in {action.status.value} status."
        
        action.status = ActionStatus.CANCELLED
        
        self.audit.log(AuditLogEntry(
            timestamp=datetime.now(timezone.utc),
            user_id=user_id,
            action_id=action.action_id,
            action_type=action.advice.action_type,
            symbol=action.advice.symbol,
            venue=action.advice.venue,
            status=ActionStatus.CANCELLED,
            amount_usd=action.advice.estimated_cost_usd,
            details={"cancelled_by": "user"},
        ))
        
        return True, "Action cancelled."
    
    def get_pending_actions(self, user_id: int) -> list[ProtectiveAction]:
        """Get pending actions for a user."""
        return [
            a for a in self._pending_actions.values()
            if a.user_id == user_id and a.status in [ActionStatus.PROPOSED, ActionStatus.CONFIRMED]
        ]
    
    def get_user_stats(self, user_id: int) -> dict:
        """Get action statistics for a user."""
        entries = self.audit.get_user_entries(user_id)
        return {
            "total_actions": len(entries),
            "completed": len([e for e in entries if e.status == ActionStatus.COMPLETED]),
            "failed": len([e for e in entries if e.status == ActionStatus.FAILED]),
            "cancelled": len([e for e in entries if e.status == ActionStatus.CANCELLED]),
            "daily_total_usd": self._user_daily_totals.get(user_id, 0),
            "daily_limit_usd": self.config.limits.max_daily_total_usd,
            "kill_switch_active": self.is_kill_switch_active(user_id),
        }
