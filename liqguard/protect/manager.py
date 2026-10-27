"""
Protective actions, tier 1: risk-reducing actions that run only after the
owning user confirms them in Telegram.

Guardrails:
- only risk-reducing actions (add margin, reduce position)
- per-action and per-user, per-UTC-day amount caps
- one-tap per-user kill switch
- every lookup is scoped by user id; another user's action id is "not found"
- execution only on testnet positions, through an injected executor
- every step is audited to the owning user's audit trail

Tier 2 (rules) and tier 3 (auto) are documented future work.
"""

from __future__ import annotations

import logging
import secrets
from datetime import date, datetime, timezone
from typing import Optional, Protocol

from liqguard.models import TESTNET, Position
from liqguard.risk import ActionableAdvice, ActionType

from .audit import AuditLogger
from .models import (
    ActionStatus,
    AuditLogEntry,
    ExecutionTier,
    ProtectiveAction,
    ProtectiveActionConfig,
)

logger = logging.getLogger(__name__)

ALLOWED_ACTIONS = (ActionType.ADD_MARGIN, ActionType.REDUCE_POSITION)
OPEN_STATUSES = (ActionStatus.PROPOSED, ActionStatus.CONFIRMED)


class ActionExecutor(Protocol):
    """Carries out a confirmed action with the owning user's testnet credentials."""

    async def execute(self, action: ProtectiveAction) -> tuple[bool, str]: ...


def _today() -> date:
    return datetime.now(timezone.utc).date()


class ProtectiveActionsManager:
    def __init__(
        self,
        config: Optional[ProtectiveActionConfig] = None,
        audit: Optional[AuditLogger] = None,
        executor: Optional[ActionExecutor] = None,
    ):
        self.config = config or ProtectiveActionConfig()
        self.audit = audit or AuditLogger()
        self.executor = executor
        self._actions: dict[int, dict[str, ProtectiveAction]] = {}
        self._daily_totals: dict[tuple[int, date], float] = {}
        self._kill_switch: set[int] = set()

    # ------------------------------------------------------------- helpers

    def _audit(self, action_or_advice, user_id: int, status: ActionStatus, details: dict, action_id: str) -> None:
        advice = action_or_advice.advice if isinstance(action_or_advice, ProtectiveAction) else action_or_advice
        self.audit.log(AuditLogEntry(
            timestamp=datetime.now(timezone.utc),
            user_id=user_id,
            action_id=action_id,
            action_type=advice.action_type,
            symbol=advice.symbol,
            venue=advice.venue,
            status=status,
            amount_usd=advice.estimated_cost_usd,
            details=details,
        ))

    def _get(self, action_id: str, user_id: int) -> Optional[ProtectiveAction]:
        return self._actions.get(user_id, {}).get(action_id)

    def is_enabled(self) -> bool:
        return self.config.enabled and self.config.tier != ExecutionTier.TIER_0_READONLY

    def is_execution_allowed(self, position: Position) -> bool:
        return self.is_enabled() and self.executor is not None and position.network == TESTNET

    def daily_total(self, user_id: int) -> float:
        return self._daily_totals.get((user_id, _today()), 0.0)

    # ------------------------------------------------------------- kill switch

    def activate_kill_switch(self, user_id: int) -> None:
        self._kill_switch.add(user_id)
        for action in self._actions.get(user_id, {}).values():
            if action.status in OPEN_STATUSES:
                action.status = ActionStatus.CANCELLED
                self._audit(action, user_id, ActionStatus.CANCELLED, {"reason": "kill_switch_activated"}, action.action_id)
        logger.warning("Kill switch activated for user %s", user_id)

    def deactivate_kill_switch(self, user_id: int) -> None:
        self._kill_switch.discard(user_id)

    def is_kill_switch_active(self, user_id: int) -> bool:
        return user_id in self._kill_switch

    # ------------------------------------------------------------- lifecycle

    def validate_action(self, user_id: int, advice: ActionableAdvice) -> tuple[bool, str]:
        if self.is_kill_switch_active(user_id):
            return False, "Kill switch is active. Deactivate it first."
        if not self.is_enabled():
            return False, "Protective actions are disabled."
        if advice.action_type not in ALLOWED_ACTIONS:
            return False, "Only risk-reducing actions are allowed."
        limits = self.config.limits
        amount = advice.estimated_cost_usd or advice.margin_to_add_usd or 0
        if amount > limits.max_single_action_usd:
            return False, f"Amount ${amount:.2f} exceeds single action limit ${limits.max_single_action_usd:.2f}"
        used = self.daily_total(user_id)
        if used + amount > limits.max_daily_total_usd:
            return False, f"Would exceed daily limit. Used: ${used:.2f}, Limit: ${limits.max_daily_total_usd:.2f}"
        if advice.reduce_size_pct and advice.reduce_size_pct > limits.max_position_reduction_pct:
            return False, f"Reduction {advice.reduce_size_pct:.1f}% exceeds limit {limits.max_position_reduction_pct:.1f}%"
        return True, "Validation passed"

    def propose_action(
        self, user_id: int, advice: ActionableAdvice, position: Position
    ) -> tuple[Optional[ProtectiveAction], str]:
        ok, reason = self.validate_action(user_id, advice)
        if not ok:
            self._audit(advice, user_id, ActionStatus.REJECTED, {"reason": reason}, "rejected")
            return None, reason
        action = ProtectiveAction(
            action_id=f"pa_{secrets.token_hex(6)}",
            user_id=user_id,
            advice=advice,
            position=position,
        )
        self._actions.setdefault(user_id, {})[action.action_id] = action
        self._audit(action, user_id, ActionStatus.PROPOSED, {"network": position.network}, action.action_id)
        return action, "Action proposed. Tap confirm to execute."

    def confirm_action(self, action_id: str, user_id: int) -> tuple[bool, str]:
        action = self._get(action_id, user_id)
        if not action:
            return False, "Action not found."
        if action.status != ActionStatus.PROPOSED:
            return False, f"Action is already {action.status.value}."
        if self.is_kill_switch_active(user_id):
            action.status = ActionStatus.CANCELLED
            return False, "Kill switch is active."
        action.status = ActionStatus.CONFIRMED
        action.confirmed_at = datetime.now(timezone.utc)
        self._audit(action, user_id, ActionStatus.CONFIRMED, {}, action_id)
        return True, "Action confirmed. Executing..."

    async def execute_action(self, action_id: str, user_id: int) -> tuple[bool, str]:
        action = self._get(action_id, user_id)
        if not action:
            return False, "Action not found."
        if action.status != ActionStatus.CONFIRMED:
            return False, f"Action must be confirmed first. Current status: {action.status.value}"
        if self.is_kill_switch_active(user_id):
            action.status = ActionStatus.CANCELLED
            return False, "Kill switch is active."
        if not self.is_execution_allowed(action.position):
            action.status = ActionStatus.FAILED
            action.error_message = "Execution is only available for testnet positions."
            self._audit(action, user_id, ActionStatus.FAILED, {"reason": action.error_message}, action_id)
            return False, "Execution not allowed. Only testnet positions can be acted on."

        ok, reason = self.validate_action(user_id, action.advice)
        if not ok:
            action.status = ActionStatus.FAILED
            action.error_message = reason
            self._audit(action, user_id, ActionStatus.FAILED, {"reason": reason}, action_id)
            return False, reason

        action.status = ActionStatus.EXECUTING
        try:
            success, message = await self.executor.execute(action)
        except Exception as e:
            success, message = False, f"Executor error: {type(e).__name__}"
            logger.error("Action %s execution failed: %s", action_id, type(e).__name__)

        if success:
            action.status = ActionStatus.COMPLETED
            action.executed_at = datetime.now(timezone.utc)
            key = (user_id, _today())
            self._daily_totals[key] = self._daily_totals.get(key, 0.0) + (action.advice.estimated_cost_usd or 0)
        else:
            action.status = ActionStatus.FAILED
            action.error_message = message
        self._audit(action, user_id, action.status, {"result": message}, action_id)
        return success, message

    def cancel_action(self, action_id: str, user_id: int) -> tuple[bool, str]:
        action = self._get(action_id, user_id)
        if not action:
            return False, "Action not found."
        if action.status not in OPEN_STATUSES:
            return False, f"Cannot cancel action in {action.status.value} status."
        action.status = ActionStatus.CANCELLED
        self._audit(action, user_id, ActionStatus.CANCELLED, {"cancelled_by": "user"}, action_id)
        return True, "Action cancelled."

    def get_pending_actions(self, user_id: int) -> list[ProtectiveAction]:
        return [a for a in self._actions.get(user_id, {}).values() if a.status in OPEN_STATUSES]

    def get_user_stats(self, user_id: int) -> dict:
        entries = self.audit.get_user_entries(user_id)
        return {
            "total_actions": len(entries),
            "completed": sum(e.status == ActionStatus.COMPLETED for e in entries),
            "failed": sum(e.status == ActionStatus.FAILED for e in entries),
            "cancelled": sum(e.status == ActionStatus.CANCELLED for e in entries),
            "daily_total_usd": self.daily_total(user_id),
            "daily_limit_usd": self.config.limits.max_daily_total_usd,
            "kill_switch_active": self.is_kill_switch_active(user_id),
        }
