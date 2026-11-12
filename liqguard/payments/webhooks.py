"""
PayPal webhook handlers for subscription lifecycle events.

Handles:
- BILLING.SUBSCRIPTION.ACTIVATED
- BILLING.SUBSCRIPTION.CANCELLED
- BILLING.SUBSCRIPTION.SUSPENDED
- BILLING.SUBSCRIPTION.PAYMENT.FAILED
- PAYMENT.CAPTURE.COMPLETED (for Rescue Pass)

All handlers are idempotent: processing the same event twice is safe.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

logger = logging.getLogger(__name__)


class PaymentEventStore(Protocol):
    """Protocol for storing processed webhook events (idempotency)."""

    async def has_event(self, event_id: str) -> bool:
        """Check if event has already been processed."""
        ...

    async def save_event(
        self,
        event_id: str,
        event_type: str,
        resource_id: str,
        telegram_id: Optional[int],
        payload: dict,
    ) -> None:
        """Store processed event."""
        ...


class SubscriptionStore(Protocol):
    """Protocol for subscription storage."""

    async def get_user_by_subscription(self, subscription_id: str) -> Optional[int]:
        """Get telegram_id by subscription_id."""
        ...

    async def get_user_by_custom_id(self, custom_id: str) -> Optional[int]:
        """Get telegram_id from custom_id field (used in checkout)."""
        ...

    async def save_subscription(
        self, telegram_id: int, subscription_id: str, plan_id: str, status: str, **kwargs
    ) -> None: ...

    async def update_subscription_status(self, subscription_id: str, status: str) -> None: ...

    async def add_rescue_passes(self, telegram_id: int, count: int) -> None: ...

    async def activate_rescue_mode(self, telegram_id: int, hours: int = 24, now=None): ...


@dataclass
class WebhookResult:
    """Result of processing a webhook."""

    success: bool
    event_id: str
    event_type: str
    message: str
    telegram_id: Optional[int] = None


NotifyCallback = Callable[[int, str, str], None]


class WebhookHandler:
    """
    PayPal webhook event handler.

    Processes subscription lifecycle events and updates user entitlements.
    Ensures idempotency by tracking processed event IDs.
    """

    def __init__(
        self,
        event_store: PaymentEventStore,
        subscription_store: SubscriptionStore,
        notify: Optional[NotifyCallback] = None,
    ):
        self.event_store = event_store
        self.subscription_store = subscription_store
        self.notify = notify

    async def process_event(self, event: dict) -> WebhookResult:
        """
        Process a PayPal webhook event.

        Returns WebhookResult indicating success/failure and details.
        """
        event_id = event.get("id", "")
        event_type = event.get("event_type", "")
        resource = event.get("resource", {})

        if not event_id or not event_type:
            return WebhookResult(
                success=False,
                event_id=event_id,
                event_type=event_type,
                message="Missing event_id or event_type",
            )

        if await self.event_store.has_event(event_id):
            logger.info("Duplicate webhook event %s, skipping", event_id)
            return WebhookResult(
                success=True,
                event_id=event_id,
                event_type=event_type,
                message="Already processed (idempotent)",
            )

        handler = self._get_handler(event_type)
        if handler is None:
            logger.info("Unhandled event type: %s", event_type)
            return WebhookResult(
                success=True,
                event_id=event_id,
                event_type=event_type,
                message=f"Event type {event_type} not handled",
            )

        try:
            telegram_id, message = await handler(resource, event)
            await self.event_store.save_event(
                event_id, event_type, resource.get("id", ""), telegram_id, event
            )
            return WebhookResult(
                success=True,
                event_id=event_id,
                event_type=event_type,
                message=message,
                telegram_id=telegram_id,
            )
        except Exception as e:
            logger.exception("Error processing webhook %s: %s", event_id, e)
            return WebhookResult(
                success=False,
                event_id=event_id,
                event_type=event_type,
                message=f"Processing error: {e}",
            )

    def _get_handler(self, event_type: str):
        handlers = {
            "BILLING.SUBSCRIPTION.ACTIVATED": self._handle_subscription_activated,
            "BILLING.SUBSCRIPTION.CANCELLED": self._handle_subscription_cancelled,
            "BILLING.SUBSCRIPTION.SUSPENDED": self._handle_subscription_suspended,
            "BILLING.SUBSCRIPTION.PAYMENT.FAILED": self._handle_payment_failed,
            "PAYMENT.CAPTURE.COMPLETED": self._handle_capture_completed,
        }
        return handlers.get(event_type)

    async def _handle_subscription_activated(self, resource: dict, event: dict) -> tuple[Optional[int], str]:
        """Handle subscription activation: grant Pro tier."""
        subscription_id = resource.get("id", "")
        plan_id = resource.get("plan_id", "")
        custom_id = resource.get("custom_id", "")
        subscriber = resource.get("subscriber", {})
        payer_email = subscriber.get("email_address")

        telegram_id = None
        if custom_id:
            try:
                telegram_id = int(custom_id)
            except (ValueError, TypeError):
                telegram_id = await self.subscription_store.get_user_by_custom_id(custom_id)

        if telegram_id is None:
            telegram_id = await self.subscription_store.get_user_by_subscription(subscription_id)

        if telegram_id is None:
            logger.warning("No user found for subscription %s", subscription_id)
            return None, f"Subscription {subscription_id} activated but no user found"

        await self.subscription_store.save_subscription(
            telegram_id,
            subscription_id,
            plan_id,
            "ACTIVE",
            payer_email=payer_email,
            activated_at=datetime.now(timezone.utc).isoformat(),
        )

        await self._notify(telegram_id, "pro_activated", subscription_id)

        return telegram_id, f"Pro activated for user {telegram_id}"

    async def _handle_subscription_cancelled(self, resource: dict, event: dict) -> tuple[Optional[int], str]:
        """Handle subscription cancellation: revoke Pro tier."""
        subscription_id = resource.get("id", "")

        telegram_id = await self.subscription_store.get_user_by_subscription(subscription_id)
        if telegram_id is None:
            return None, f"Subscription {subscription_id} cancelled but no user found"

        await self.subscription_store.update_subscription_status(subscription_id, "CANCELLED")

        await self._notify(telegram_id, "pro_cancelled", subscription_id)

        return telegram_id, f"Pro cancelled for user {telegram_id}"

    async def _handle_subscription_suspended(self, resource: dict, event: dict) -> tuple[Optional[int], str]:
        """Handle subscription suspension: temporarily revoke Pro tier."""
        subscription_id = resource.get("id", "")

        telegram_id = await self.subscription_store.get_user_by_subscription(subscription_id)
        if telegram_id is None:
            return None, f"Subscription {subscription_id} suspended but no user found"

        await self.subscription_store.update_subscription_status(subscription_id, "SUSPENDED")

        await self._notify(telegram_id, "pro_suspended", subscription_id)

        return telegram_id, f"Pro suspended for user {telegram_id}"

    async def _handle_payment_failed(self, resource: dict, event: dict) -> tuple[Optional[int], str]:
        """Handle payment failure: notify user, keep Pro active until suspension."""
        subscription_id = resource.get("id", "")

        telegram_id = await self.subscription_store.get_user_by_subscription(subscription_id)
        if telegram_id is None:
            return None, f"Payment failed for {subscription_id} but no user found"

        await self._notify(telegram_id, "payment_failed", subscription_id)

        return telegram_id, f"Payment failed notification sent to user {telegram_id}"

    async def _handle_capture_completed(self, resource: dict, event: dict) -> tuple[Optional[int], str]:
        """Handle one-time payment capture: activate 24h Rescue Mode (extends if already active)."""
        order_id = resource.get("id", "")
        custom_id = ""

        purchase_units = resource.get("purchase_units", [])
        if purchase_units:
            custom_id = purchase_units[0].get("custom_id", "")

        if not custom_id:
            return None, f"Capture {order_id} completed but no custom_id"

        try:
            telegram_id = int(custom_id)
        except (ValueError, TypeError):
            return None, f"Capture {order_id} completed but invalid custom_id"

        until = None
        if hasattr(self.subscription_store, "activate_rescue_mode"):
            until = await self.subscription_store.activate_rescue_mode(telegram_id)
        else:
            await self.subscription_store.add_rescue_passes(telegram_id, 1)

        if hasattr(self.subscription_store, "save_agent_action"):
            message = f"Rescue Mode until {until.isoformat()}" if until is not None else "Rescue Mode activated"
            await self.subscription_store.save_agent_action(telegram_id, {
                "tool": "rescue_mode",
                "product": "rescue",
                "source": "webhook",
                "status": "rescue_mode",
                "resource_id": order_id,
                "message": message,
            })

        await self._notify(telegram_id, "rescue_pass_purchased", order_id)

        return telegram_id, f"Rescue Mode activated for user {telegram_id}"

    async def _notify(self, telegram_id: int, kind: str, resource_id: str) -> None:
        if self.notify is None:
            return
        result = self.notify(telegram_id, kind, resource_id)
        if asyncio.iscoroutine(result):
            await result


def extract_webhook_headers(headers: dict) -> dict:
    """Extract PayPal webhook signature headers."""
    return {
        "transmission_id": headers.get("paypal-transmission-id", ""),
        "transmission_time": headers.get("paypal-transmission-time", ""),
        "cert_url": headers.get("paypal-cert-url", ""),
        "auth_algo": headers.get("paypal-auth-algo", ""),
        "transmission_sig": headers.get("paypal-transmission-sig", ""),
    }
