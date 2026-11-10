"""
Entitlements service for LiqGuard Pro tier gating.

Free tier: basic alerts, limited Muse suggestions, single account.
Pro tier: faster polling (10s vs 30s), multi-account, unlimited Muse rescue advice.

Entitlements are derived from active PayPal subscriptions or one-time Rescue Pass purchases.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


class Tier(str, Enum):
    """User subscription tier."""

    FREE = "free"
    PRO = "pro"


class SubscriptionStatus(str, Enum):
    """PayPal subscription status."""

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@dataclass
class ProStatus:
    """User's Pro tier status and entitlements."""

    tier: Tier
    is_pro: bool
    subscription_id: Optional[str] = None
    subscription_status: Optional[str] = None
    plan_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    rescue_passes_remaining: int = 0

    @classmethod
    def free(cls) -> "ProStatus":
        return cls(tier=Tier.FREE, is_pro=False)

    @classmethod
    def pro(
        cls,
        subscription_id: str,
        subscription_status: str = "ACTIVE",
        plan_id: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> "ProStatus":
        return cls(
            tier=Tier.PRO,
            is_pro=True,
            subscription_id=subscription_id,
            subscription_status=subscription_status,
            plan_id=plan_id,
            expires_at=expires_at,
        )

    def to_dict(self) -> dict:
        return {
            "tier": self.tier.value,
            "is_pro": self.is_pro,
            "subscription_id": self.subscription_id,
            "subscription_status": self.subscription_status,
            "plan_id": self.plan_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "rescue_passes_remaining": self.rescue_passes_remaining,
        }


class SubscriptionStore(Protocol):
    """Protocol for subscription storage."""

    async def get_active_subscription(self, telegram_id: int) -> Optional[dict]: ...
    async def save_subscription(
        self, telegram_id: int, subscription_id: str, plan_id: str, status: str, **kwargs
    ) -> None: ...
    async def update_subscription_status(self, subscription_id: str, status: str) -> None: ...
    async def get_rescue_passes(self, telegram_id: int) -> int: ...
    async def use_rescue_pass(self, telegram_id: int) -> bool: ...
    async def add_rescue_passes(self, telegram_id: int, count: int) -> None: ...


class EntitlementService:
    """
    Service for managing user entitlements and Pro tier features.

    Integrates with subscription storage to check and update user tiers.
    Supports graceful degradation when PayPal is not configured.
    """

    def __init__(
        self,
        store: Optional[SubscriptionStore] = None,
        paypal_enabled: bool = False,
        free_muse_daily_limit: int = 5,
        pro_poll_interval: int = 10,
        free_poll_interval: int = 30,
    ):
        self.store = store
        self.paypal_enabled = paypal_enabled
        self.free_muse_daily_limit = free_muse_daily_limit
        self.pro_poll_interval = pro_poll_interval
        self.free_poll_interval = free_poll_interval
        self._muse_usage: dict[int, tuple[str, int]] = {}  # telegram_id -> (date, count)

    async def get_status(self, telegram_id: int) -> ProStatus:
        """Get user's current Pro status from storage."""
        if not self.paypal_enabled or self.store is None:
            return ProStatus.free()

        sub = await self.store.get_active_subscription(telegram_id)
        if sub is None:
            rescue_passes = await self.store.get_rescue_passes(telegram_id)
            status = ProStatus.free()
            status.rescue_passes_remaining = rescue_passes
            return status

        expires_at = None
        if sub.get("expires_at"):
            try:
                expires_at = datetime.fromisoformat(sub["expires_at"])
            except (ValueError, TypeError):
                pass

        status = ProStatus.pro(
            subscription_id=sub["subscription_id"],
            subscription_status=sub.get("status", "ACTIVE"),
            plan_id=sub.get("plan_id"),
            expires_at=expires_at,
        )
        status.rescue_passes_remaining = await self.store.get_rescue_passes(telegram_id)
        return status

    async def is_pro(self, telegram_id: int) -> bool:
        """Check if user has Pro tier access."""
        status = await self.get_status(telegram_id)
        return status.is_pro

    async def activate_subscription(
        self,
        telegram_id: int,
        subscription_id: str,
        plan_id: str,
        payer_email: Optional[str] = None,
    ) -> ProStatus:
        """Activate Pro subscription for user."""
        if self.store is None:
            return ProStatus.free()

        await self.store.save_subscription(
            telegram_id,
            subscription_id,
            plan_id,
            SubscriptionStatus.ACTIVE.value,
            payer_email=payer_email,
        )
        logger.info("Activated Pro subscription %s for user %s", subscription_id, telegram_id)
        return await self.get_status(telegram_id)

    async def cancel_subscription(self, subscription_id: str) -> bool:
        """Cancel subscription by ID (webhook-triggered)."""
        if self.store is None:
            return False

        await self.store.update_subscription_status(subscription_id, SubscriptionStatus.CANCELLED.value)
        logger.info("Cancelled subscription %s", subscription_id)
        return True

    async def suspend_subscription(self, subscription_id: str) -> bool:
        """Suspend subscription by ID (webhook-triggered, e.g. payment failed)."""
        if self.store is None:
            return False

        await self.store.update_subscription_status(subscription_id, SubscriptionStatus.SUSPENDED.value)
        logger.info("Suspended subscription %s", subscription_id)
        return True

    def poll_interval_for(self, is_pro: bool) -> int:
        """Get polling interval based on tier."""
        return self.pro_poll_interval if is_pro else self.free_poll_interval

    async def check_muse_limit(self, telegram_id: int) -> tuple[bool, int, int]:
        """
        Check if user can make a Muse request.

        When PayPal is not configured, Muse stays unlimited (the Meta demo path).
        Returns:
            (allowed, used_today, daily_limit)  daily_limit is -1 when unlimited.
        """
        if not self.paypal_enabled:
            return True, 0, -1

        status = await self.get_status(telegram_id)
        if status.is_pro:
            return True, 0, -1  # unlimited

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cached = self._muse_usage.get(telegram_id)

        if cached is None or cached[0] != today:
            used = 0
        else:
            used = cached[1]

        return used < self.free_muse_daily_limit, used, self.free_muse_daily_limit

    async def record_muse_usage(self, telegram_id: int) -> None:
        """Record a Muse request for rate limiting."""
        status = await self.get_status(telegram_id)
        if status.is_pro:
            return  # no tracking for Pro users

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cached = self._muse_usage.get(telegram_id)

        if cached is None or cached[0] != today:
            self._muse_usage[telegram_id] = (today, 1)
        else:
            self._muse_usage[telegram_id] = (today, cached[1] + 1)

    async def use_rescue_pass(self, telegram_id: int) -> bool:
        """Use a one-time Rescue Pass for unlimited Muse advice. Returns True if consumed."""
        if self.store is None:
            return False

        return await self.store.use_rescue_pass(telegram_id)

    async def add_rescue_passes(self, telegram_id: int, count: int = 1) -> None:
        """Add Rescue Passes to user account (from one-time purchase)."""
        if self.store is not None:
            await self.store.add_rescue_passes(telegram_id, count)

    def can_bind_multiple_accounts(self, is_pro: bool) -> bool:
        """Check if user can bind multiple exchange accounts."""
        return is_pro

    def max_accounts(self, is_pro: bool) -> int:
        """Maximum number of exchange accounts user can bind."""
        return 5 if is_pro else 1

    async def upgrade_hint(self, telegram_id: int, lang: str, *, high_risk: bool = False) -> Optional[str]:
        """Muse CTA when risk is high and the user is on the free tier (PayPal configured)."""
        if not self.paypal_enabled or not high_risk:
            return None
        status = await self.get_status(telegram_id)
        return upgrade_suggestion(status, lang, paypal_enabled=True)


def upgrade_suggestion(status: ProStatus, lang: str = "en", *, paypal_enabled: bool = True) -> Optional[str]:
    """
    Generate an upgrade suggestion for free tier users.

    Returns localized text suggesting Pro upgrade, or None for Pro users
    and when PayPal billing is not configured (Meta demo / free-tier degrade).
    """
    from liqguard.i18n import t

    if not paypal_enabled or status.is_pro:
        return None
    return t(lang, "pro.upgrade.suggest")


def rescue_pass_suggestion(status: ProStatus, lang: str = "en", *, paypal_enabled: bool = True) -> Optional[str]:
    """
    Generate a Rescue Pass suggestion when user hits their limit.

    Returns localized text, or None if user has Pro or passes remaining.
    """
    from liqguard.i18n import t

    if not paypal_enabled or status.is_pro or status.rescue_passes_remaining > 0:
        return None
    return t(lang, "pro.upgrade.rescue_pass_suggest")
