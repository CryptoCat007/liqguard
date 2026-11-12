"""
PayPal webhook handlers for subscription lifecycle events.

Handles:
- BILLING.SUBSCRIPTION.ACTIVATED
- BILLING.SUBSCRIPTION.CANCELLED
- BILLING.SUBSCRIPTION.SUSPENDED
- BILLING.SUBSCRIPTION.PAYMENT.FAILED
- PAYMENT.CAPTURE.COMPLETED (for Rescue Pass)
- CHECKOUT.ORDER.COMPLETED (Rescue Pass, same grant as capture)
- CHECKOUT.ORDER.APPROVED (ack only; wait for capture)

All handlers are idempotent: processing the same event twice is safe.
Unattributed capture/order-completed events are NOT stored, so PayPal can retry.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

# Events that must be attributed to a telegram user before they are acked.
_GRANT_EVENTS = frozenset({
    "PAYMENT.CAPTURE.COMPLETED",
    "CHECKOUT.ORDER.COMPLETED",
})


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


def extract_custom_id(resource: dict) -> str:
    """
    custom_id from a PayPal capture, order, or nested purchase_unit.

    Real PAYMENT.CAPTURE.COMPLETED payloads put it on resource.custom_id and
    often omit purchase_units. Orders v2 and some sandbox events still use
    purchase_units[0].custom_id (or a nested capture).
    """
    if not isinstance(resource, dict):
        return ""
    cid = resource.get("custom_id") or ""
    if cid:
        return str(cid)
    units = resource.get("purchase_units") or []
    if units and isinstance(units[0], dict):
        cid = units[0].get("custom_id") or ""
        if cid:
            return str(cid)
        payments = units[0].get("payments") or {}
        captures = payments.get("captures") or []
        if captures and isinstance(captures[0], dict):
            cid = captures[0].get("custom_id") or ""
            if cid:
                return str(cid)
    payments = resource.get("payments") or {}
    captures = payments.get("captures") or []
    if captures and isinstance(captures[0], dict):
        cid = captures[0].get("custom_id") or ""
        if cid:
            return str(cid)
    return ""


def extract_order_id(resource: dict, event_type: str = "") -> str:
    """Order id for grant idempotency. Capture resources use related_ids, not resource.id."""
    if not isinstance(resource, dict):
        return ""
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    oid = related.get("order_id") or ""
    if oid:
        return str(oid)
    if str(event_type).startswith("CHECKOUT.ORDER"):
        return str(resource.get("id") or "")
    return ""


def parse_telegram_id(custom_id: str) -> Optional[int]:
    """custom_id is our own telegram user id (string)."""
    if custom_id is None or custom_id == "":
        return None
    try:
        return int(str(custom_id).strip())
    except (TypeError, ValueError):
        return None


def rescue_grant_key(order_id: str, capture_id: str = "") -> str:
    if order_id:
        return f"order:{order_id}"
    if capture_id:
        return f"capture:{capture_id}"
    return ""


async def grant_rescue_once(
    event_store: PaymentEventStore,
    subscription_store: SubscriptionStore,
    telegram_id: int,
    *,
    order_id: str,
    source: str,
    notify: Optional[NotifyCallback] = None,
    capture_id: str = "",
) -> tuple[bool, Optional[datetime]]:
    """
    Activate Rescue Mode at most once per PayPal order (or capture) id.

    Shared by the webhook and the browser return URL so a capture COMPLETED
    plus a return hit cannot stack two 24h windows.
    """
    key = rescue_grant_key(order_id, capture_id)
    until = None
    if hasattr(subscription_store, "get_rescue_until"):
        until = await subscription_store.get_rescue_until(telegram_id)
    if key and await event_store.has_event(key):
        return False, until
    if hasattr(subscription_store, "activate_rescue_mode"):
        until = await subscription_store.activate_rescue_mode(telegram_id)
    else:
        await subscription_store.add_rescue_passes(telegram_id, 1)
    if key:
        await event_store.save_event(
            key, "RESCUE_MODE", order_id or capture_id, telegram_id,
            {"source": source, "order_id": order_id, "capture_id": capture_id},
        )
    if hasattr(subscription_store, "save_agent_action"):
        message = f"Rescue Mode until {until.isoformat()}" if until is not None else "Rescue Mode activated"
        await subscription_store.save_agent_action(telegram_id, {
            "tool": "rescue_mode",
            "product": "rescue",
            "source": source,
            "status": "rescue_mode",
            "resource_id": order_id or capture_id,
            "message": message,
        })
    if notify is not None:
        result = notify(telegram_id, "rescue_pass_purchased", order_id or capture_id)
        if asyncio.iscoroutine(result):
            await result
    return True, until


async def fulfill_rescue_order(
    event_store: PaymentEventStore,
    subscription_store: SubscriptionStore,
    *,
    order_id: str,
    payload: Optional[dict] = None,
    paypal: Any = None,
    notify: Optional[NotifyCallback] = None,
    source: str = "return",
    capture_id: str = "",
) -> tuple[Optional[int], bool, Optional[datetime]]:
    """Attribute an order/capture to a user and grant Rescue Mode once."""
    payload = payload or {}
    custom_id = extract_custom_id(payload)
    resolved_order = extract_order_id(payload, "CHECKOUT.ORDER.COMPLETED") or order_id
    if not custom_id and paypal is not None and resolved_order:
        try:
            order = await paypal.get_order(resolved_order)
            custom_id = extract_custom_id(order)
            resolved_order = str(order.get("id") or resolved_order)
        except Exception as e:
            logger.warning("PayPal GET order %s failed: %s", resolved_order, e)
    telegram_id = parse_telegram_id(custom_id)
    if telegram_id is None and custom_id and hasattr(subscription_store, "get_user_by_custom_id"):
        telegram_id = await subscription_store.get_user_by_custom_id(custom_id)
    if telegram_id is None:
        return None, False, None
    granted, until = await grant_rescue_once(
        event_store, subscription_store, telegram_id,
        order_id=resolved_order, source=source, notify=notify, capture_id=capture_id,
    )
    return telegram_id, granted, until


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
        paypal: Any = None,
    ):
        self.event_store = event_store
        self.subscription_store = subscription_store
        self.notify = notify
        self.paypal = paypal

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
            if telegram_id is None and event_type in _GRANT_EVENTS:
                # Do not ack-and-forget: PayPal must be able to retry once we can
                # look the order up or the payload is retried with custom_id.
                logger.warning("Unattributed %s %s: %s", event_type, event_id, message)
                return WebhookResult(
                    success=False,
                    event_id=event_id,
                    event_type=event_type,
                    message=message,
                    telegram_id=None,
                )
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
            "CHECKOUT.ORDER.COMPLETED": self._handle_order_completed,
            "CHECKOUT.ORDER.APPROVED": self._handle_order_approved,
        }
        return handlers.get(event_type)

    async def _resolve_custom_id(self, resource: dict, event_type: str) -> tuple[str, str]:
        custom_id = extract_custom_id(resource)
        order_id = extract_order_id(resource, event_type)
        if custom_id:
            return custom_id, order_id
        if self.paypal is not None and order_id:
            try:
                order = await self.paypal.get_order(order_id)
                custom_id = extract_custom_id(order)
                order_id = str(order.get("id") or order_id)
            except Exception as e:
                logger.warning("PayPal GET order %s failed: %s", order_id, e)
        return custom_id, order_id

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

    async def _grant_rescue(self, resource: dict, event: dict) -> tuple[Optional[int], str]:
        event_type = event.get("event_type", "")
        capture_id = str(resource.get("id") or "")
        custom_id, order_id = await self._resolve_custom_id(resource, event_type)
        label = order_id or capture_id
        if not custom_id:
            return None, f"Capture {label} completed but no custom_id"
        telegram_id = parse_telegram_id(custom_id)
        if telegram_id is None:
            telegram_id = await self.subscription_store.get_user_by_custom_id(custom_id)
        if telegram_id is None:
            return None, f"Capture {label} completed but invalid custom_id"
        granted, _until = await grant_rescue_once(
            self.event_store, self.subscription_store, telegram_id,
            order_id=order_id, source="webhook", notify=self.notify, capture_id=capture_id,
        )
        if granted:
            return telegram_id, f"Rescue Mode activated for user {telegram_id}"
        return telegram_id, f"Rescue Mode already recorded for order {label}"

    async def _handle_capture_completed(self, resource: dict, event: dict) -> tuple[Optional[int], str]:
        """Handle one-time payment capture: activate 24h Rescue Mode (extends if already active)."""
        return await self._grant_rescue(resource, event)

    async def _handle_order_completed(self, resource: dict, event: dict) -> tuple[Optional[int], str]:
        """CHECKOUT.ORDER.COMPLETED uses the same Rescue Mode grant, keyed by order id."""
        return await self._grant_rescue(resource, event)

    async def _handle_order_approved(self, resource: dict, event: dict) -> tuple[Optional[int], str]:
        """APPROVED is not captured — parse custom_id for logs, wait for COMPLETED."""
        custom_id, order_id = await self._resolve_custom_id(resource, event.get("event_type", ""))
        return None, (
            f"Order {order_id or resource.get('id', '')} approved "
            f"(waiting for capture); custom_id={'yes' if custom_id else 'no'}"
        )

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
