"""
Entitlements service for LiqGuard Pro tier gating.

Free: 5 Muse requests / Taipei day, 1 bound account, 30s scans, rescue-calc preview.
Pro: unlimited Muse, 5 accounts, 10s scans, full rescue calculator.
Rescue Mode (24h from a Rescue Pass): unlimited Muse, 10s scans, full rescue calc.
  Buying again while active extends by 24h. Does not raise the account cap.
  Pro users do not need it.

When PayPal is not configured, Muse and binds stay unlimited (the Meta demo path).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Callable, Optional, Protocol
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

try:
    TAIPEI = ZoneInfo("Asia/Taipei")
except Exception:  # pragma: no cover - platform without tzdata
    TAIPEI = timezone(timedelta(hours=8))
RESCUE_HOURS = 24
FREE_ACCOUNTS = 1
PRO_ACCOUNTS = 5


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def taipei_day(now: datetime) -> str:
    return aware(now).astimezone(TAIPEI).date().isoformat()


def muse_reset_at(now: datetime) -> datetime:
    local = aware(now).astimezone(TAIPEI)
    nxt = local.date() + timedelta(days=1)
    return datetime.combine(nxt, time.min, tzinfo=TAIPEI)


def format_taipei_hm(dt: datetime) -> str:
    return aware(dt).astimezone(TAIPEI).strftime("%H:%M")


_EN_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def format_taipei_expiry(dt: datetime, lang: str = "en") -> str:
    """Rescue Mode expiry for display: 'Nov 13, 22:49' / '11/13 22:49' (Taipei)."""
    local = aware(dt).astimezone(TAIPEI)
    hm = local.strftime("%H:%M")
    if str(lang).lower().startswith("zh"):
        return f"{local.month}/{local.day} {hm}"
    return f"{_EN_MONTHS[local.month - 1]} {local.day}, {hm}"


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
    is_rescue: bool = False
    rescue_until: Optional[datetime] = None
    muse_used: int = 0
    muse_limit: int = 5
    muse_reset_at: Optional[datetime] = None
    account_limit: int = FREE_ACCOUNTS

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
            muse_limit=-1,
            account_limit=PRO_ACCOUNTS,
        )

    def to_dict(self) -> dict:
        remaining_s = None
        if self.is_rescue and self.rescue_until is not None:
            remaining_s = max(0, int((aware(self.rescue_until) - datetime.now(timezone.utc)).total_seconds()))
        return {
            "tier": self.tier.value,
            "is_pro": self.is_pro,
            "subscription_id": self.subscription_id,
            "subscription_status": self.subscription_status,
            "plan_id": self.plan_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "rescue_passes_remaining": self.rescue_passes_remaining,
            "is_rescue": self.is_rescue,
            "rescue_until": self.rescue_until.isoformat() if self.rescue_until else None,
            "rescue_until_taipei": format_taipei_expiry(self.rescue_until, "en") if self.rescue_until else None,
            "rescue_until_taipei_zh": format_taipei_expiry(self.rescue_until, "zh-Hant") if self.rescue_until else None,
            "rescue_remaining_seconds": remaining_s,
            "muse_used": self.muse_used,
            "muse_limit": self.muse_limit,
            "muse_reset_at": self.muse_reset_at.isoformat() if self.muse_reset_at else None,
            "muse_reset_taipei": "00:00",
            "account_limit": self.account_limit,
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
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.store = store
        self.paypal_enabled = paypal_enabled
        self.free_muse_daily_limit = free_muse_daily_limit
        self.pro_poll_interval = pro_poll_interval
        self.free_poll_interval = free_poll_interval
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._muse_usage: dict[int, tuple[str, int]] = {}  # telegram_id -> (taipei-day, count)
        self._rescue_until: dict[int, datetime] = {}

    def _now(self) -> datetime:
        return aware(self.clock()) or datetime.now(timezone.utc)

    def reset_label(self, now: Optional[datetime] = None) -> str:
        """Human reset time shown in upsell copy: always next 00:00 Taipei."""
        reset = muse_reset_at(now or self._now())
        return f"{format_taipei_hm(reset)} Taipei"

    async def get_status(self, telegram_id: int) -> ProStatus:
        """Get user's current Pro status from storage."""
        if not self.paypal_enabled or self.store is None:
            status = ProStatus.free()
            status.muse_limit = -1
            return status

        sub = await self.store.get_active_subscription(telegram_id)
        if sub is None:
            status = ProStatus.free()
        else:
            expires_at = None
            if sub.get("expires_at"):
                try:
                    expires_at = aware(datetime.fromisoformat(sub["expires_at"]))
                except (ValueError, TypeError):
                    pass
            status = ProStatus.pro(
                subscription_id=sub["subscription_id"],
                subscription_status=sub.get("status", "ACTIVE"),
                plan_id=sub.get("plan_id"),
                expires_at=expires_at,
            )

        status.rescue_passes_remaining = await self.store.get_rescue_passes(telegram_id)
        until = await self.rescue_until(telegram_id)
        status.rescue_until = until
        status.is_rescue = until is not None
        if status.is_pro or status.is_rescue:
            status.muse_limit = -1
            status.muse_used = 0
        else:
            status.muse_limit = self.free_muse_daily_limit
            status.muse_used = await self._used_today(telegram_id)
        status.muse_reset_at = muse_reset_at(self._now())
        status.account_limit = self.max_accounts(status.is_pro)
        return status

    async def is_pro(self, telegram_id: int) -> bool:
        """Check if user has Pro tier access."""
        status = await self.get_status(telegram_id)
        return status.is_pro

    async def is_rescue(self, telegram_id: int) -> bool:
        return (await self.rescue_until(telegram_id)) is not None

    async def rescue_until(self, telegram_id: int) -> Optional[datetime]:
        """Active Rescue Mode expiry, or None if expired / never purchased."""
        now = self._now()
        until: Optional[datetime] = None
        if self.store is not None and hasattr(self.store, "get_rescue_until"):
            until = aware(await self.store.get_rescue_until(telegram_id))
        if until is None:
            until = aware(self._rescue_until.get(telegram_id))
        if until is None and self.store is not None:
            remaining = await self.store.get_rescue_passes(telegram_id)
            if remaining > 0:
                until = now + timedelta(hours=RESCUE_HOURS * remaining)
                self._rescue_until[telegram_id] = until
        if until is not None and until > now:
            return until
        return None

    async def activate_rescue_mode(self, telegram_id: int, hours: int = RESCUE_HOURS) -> datetime:
        """Start Rescue Mode, or extend an active window by `hours`."""
        now = self._now()
        current = await self.rescue_until(telegram_id)
        base = current if current is not None and current > now else now
        until = base + timedelta(hours=hours)
        self._rescue_until[telegram_id] = until
        if self.store is not None:
            if hasattr(self.store, "activate_rescue_mode"):
                stored = await self.store.activate_rescue_mode(telegram_id, hours=hours, now=now)
                if stored is not None:
                    until = aware(stored) or until
                    self._rescue_until[telegram_id] = until
            elif hasattr(self.store, "set_rescue_until"):
                await self.store.set_rescue_until(telegram_id, until)
                if hasattr(self.store, "add_rescue_passes"):
                    await self.store.add_rescue_passes(telegram_id, 1)
            elif hasattr(self.store, "add_rescue_passes"):
                await self.store.add_rescue_passes(telegram_id, 1)
        logger.info("Rescue Mode for user %s until %s", telegram_id, until.isoformat())
        return until

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
        """Get polling interval based on Pro flag (Rescue Mode uses poll_interval_for_user)."""
        return self.pro_poll_interval if is_pro else self.free_poll_interval

    async def poll_interval_for_user(self, telegram_id: int) -> int:
        """10s for Pro or active Rescue Mode; 30s otherwise. PayPal off → free interval."""
        if not self.paypal_enabled:
            return self.free_poll_interval
        status = await self.get_status(telegram_id)
        if status.is_pro or status.is_rescue:
            return self.pro_poll_interval
        return self.free_poll_interval

    async def _used_today(self, telegram_id: int) -> int:
        day = taipei_day(self._now())
        if self.store is not None and hasattr(self.store, "get_muse_usage"):
            return int(await self.store.get_muse_usage(telegram_id, day))
        cached = self._muse_usage.get(telegram_id)
        if cached is None or cached[0] != day:
            return 0
        return cached[1]

    async def check_muse_limit(self, telegram_id: int) -> tuple[bool, int, int]:
        """
        Check if user can make a Muse request.

        When PayPal is not configured, Muse stays unlimited (the Meta demo path).
        Pro and active Rescue Mode are unlimited.
        Returns:
            (allowed, used_today, daily_limit)  daily_limit is -1 when unlimited.
        """
        if not self.paypal_enabled:
            return True, 0, -1

        status = await self.get_status(telegram_id)
        if status.is_pro or status.is_rescue:
            return True, 0, -1

        used = await self._used_today(telegram_id)
        return used < self.free_muse_daily_limit, used, self.free_muse_daily_limit

    async def record_muse_usage(self, telegram_id: int) -> None:
        """Record a Muse request for rate limiting. No-op for Pro / Rescue Mode / PayPal off."""
        if not self.paypal_enabled:
            return
        status = await self.get_status(telegram_id)
        if status.is_pro or status.is_rescue:
            return

        day = taipei_day(self._now())
        if self.store is not None and hasattr(self.store, "increment_muse_usage"):
            used = int(await self.store.increment_muse_usage(telegram_id, day))
            self._muse_usage[telegram_id] = (day, used)
            return
        cached = self._muse_usage.get(telegram_id)
        if cached is None or cached[0] != day:
            self._muse_usage[telegram_id] = (day, 1)
        else:
            self._muse_usage[telegram_id] = (day, cached[1] + 1)

    async def use_rescue_pass(self, telegram_id: int) -> bool:
        """Legacy remaining-count consume. Rescue Mode is time-based; prefer activate_rescue_mode."""
        if self.store is None:
            return False
        return await self.store.use_rescue_pass(telegram_id)

    async def add_rescue_passes(self, telegram_id: int, count: int = 1) -> None:
        """Each pass activates / extends Rescue Mode by 24h."""
        for _ in range(max(1, count)):
            await self.activate_rescue_mode(telegram_id)

    def can_bind_multiple_accounts(self, is_pro: bool) -> bool:
        """Check if user can bind multiple exchange accounts."""
        return is_pro

    def max_accounts(self, is_pro: bool) -> int:
        """Maximum number of exchange accounts user can bind. Rescue Mode does not raise this."""
        return PRO_ACCOUNTS if is_pro else FREE_ACCOUNTS

    async def check_account_limit(
        self, telegram_id: int, current_count: int, *, replacing: bool = False,
    ) -> tuple[bool, int, int]:
        """
        Whether the user may bind another account.
        Replacing an existing venue+network does not consume a slot.
        PayPal off → unlimited.
        """
        if not self.paypal_enabled:
            return True, current_count, -1
        status = await self.get_status(telegram_id)
        limit = self.max_accounts(status.is_pro)
        if replacing:
            return True, current_count, limit
        return current_count < limit, current_count, limit

    async def can_use_full_rescue(self, telegram_id: int) -> bool:
        """Full rescue calculator: Pro, Rescue Mode, or PayPal unset."""
        if not self.paypal_enabled:
            return True
        status = await self.get_status(telegram_id)
        return status.is_pro or status.is_rescue

    async def upgrade_hint(self, telegram_id: int, lang: str, *, high_risk: bool = False) -> Optional[str]:
        """Muse CTA when risk is high and the user is on the free tier (PayPal configured)."""
        if not self.paypal_enabled or not high_risk:
            return None
        status = await self.get_status(telegram_id)
        if status.is_pro or status.is_rescue:
            return None
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

    Returns localized text, or None if user has Pro, Rescue Mode, or remaining passes.
    """
    from liqguard.i18n import t

    if not paypal_enabled or status.is_pro or status.is_rescue or status.rescue_passes_remaining > 0:
        return None
    return t(lang, "pro.upgrade.rescue_pass_suggest")
