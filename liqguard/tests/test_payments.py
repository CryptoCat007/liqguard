"""
Tests for PayPal integration: entitlements, webhook handling, and idempotency.

All tests use mocked PayPal responses (no live API calls).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from liqguard.models import User
from liqguard.payments import EntitlementService, PayPalClient, ProStatus, Tier
from liqguard.payments.entitlements import TAIPEI, format_taipei_expiry, format_taipei_hm
from liqguard.payments.webhooks import WebhookHandler

# ---------------------------------------------------------------- fixtures


class MockSubscriptionStore:
    """In-memory subscription store for testing."""

    def __init__(self):
        self.subscriptions: dict[int, dict] = {}
        self.rescue_passes: dict[int, int] = {}
        self.rescue_until: dict[int, datetime] = {}
        self.muse_usage: dict[tuple[int, str], int] = {}
        self.agent_actions: list = []

    async def get_active_subscription(self, telegram_id: int) -> dict | None:
        sub = self.subscriptions.get(telegram_id)
        if sub and sub.get("status") == "ACTIVE":
            return sub
        return None

    async def save_subscription(self, telegram_id: int, subscription_id: str, plan_id: str, status: str, **kwargs):
        self.subscriptions[telegram_id] = {
            "telegram_id": telegram_id,
            "subscription_id": subscription_id,
            "plan_id": plan_id,
            "status": status,
            **kwargs,
        }

    async def update_subscription_status(self, subscription_id: str, status: str):
        for sub in self.subscriptions.values():
            if sub.get("subscription_id") == subscription_id:
                sub["status"] = status
                break

    async def get_user_by_subscription(self, subscription_id: str) -> int | None:
        for uid, sub in self.subscriptions.items():
            if sub.get("subscription_id") == subscription_id:
                return uid
        return None

    async def get_user_by_custom_id(self, custom_id: str) -> int | None:
        try:
            return int(custom_id)
        except ValueError:
            return None

    async def get_rescue_passes(self, telegram_id: int) -> int:
        return self.rescue_passes.get(telegram_id, 0)

    async def use_rescue_pass(self, telegram_id: int) -> bool:
        if self.rescue_passes.get(telegram_id, 0) > 0:
            self.rescue_passes[telegram_id] -= 1
            return True
        return False

    async def add_rescue_passes(self, telegram_id: int, count: int):
        self.rescue_passes[telegram_id] = self.rescue_passes.get(telegram_id, 0) + count

    async def get_rescue_until(self, telegram_id: int):
        return self.rescue_until.get(telegram_id)

    async def activate_rescue_mode(self, telegram_id: int, hours: int = 24, now: datetime | None = None):
        moment = now or datetime.now(timezone.utc)
        current = self.rescue_until.get(telegram_id)
        base = current if current is not None and current > moment else moment
        until = base + timedelta(hours=hours)
        self.rescue_until[telegram_id] = until
        self.rescue_passes[telegram_id] = self.rescue_passes.get(telegram_id, 0) + 1
        return until

    async def get_muse_usage(self, telegram_id: int, day: str) -> int:
        return self.muse_usage.get((telegram_id, day), 0)

    async def increment_muse_usage(self, telegram_id: int, day: str) -> int:
        key = (telegram_id, day)
        self.muse_usage[key] = self.muse_usage.get(key, 0) + 1
        return self.muse_usage[key]

    async def save_agent_action(self, telegram_id: int, checkout):
        data = checkout.to_dict() if hasattr(checkout, "to_dict") else dict(checkout)
        self.agent_actions.append((telegram_id, data))


class MockPaymentEventStore:
    """In-memory event store for idempotency testing."""

    def __init__(self):
        self.events: dict[str, dict] = {}

    async def has_event(self, event_id: str) -> bool:
        return event_id in self.events

    async def save_event(self, event_id: str, event_type: str, resource_id: str, telegram_id: int | None, payload: dict):
        self.events[event_id] = {
            "event_id": event_id,
            "event_type": event_type,
            "resource_id": resource_id,
            "telegram_id": telegram_id,
            "payload": payload,
        }


@pytest.fixture
def subscription_store():
    return MockSubscriptionStore()


@pytest.fixture
def event_store():
    return MockPaymentEventStore()


@pytest.fixture
def entitlements(subscription_store):
    return EntitlementService(
        store=subscription_store,
        paypal_enabled=True,
        free_muse_daily_limit=5,
        pro_poll_interval=10,
        free_poll_interval=30,
    )


@pytest.fixture
def webhook_handler(event_store, subscription_store):
    return WebhookHandler(
        event_store=event_store,
        subscription_store=subscription_store,
        notify=None,
    )


# ---------------------------------------------------------------- entitlements tests


class TestEntitlementService:

    @pytest.mark.asyncio
    async def test_free_user_status(self, entitlements):
        """Users without subscriptions are on free tier."""
        status = await entitlements.get_status(12345)
        assert status.tier == Tier.FREE
        assert not status.is_pro
        assert status.subscription_id is None

    @pytest.mark.asyncio
    async def test_pro_user_status(self, entitlements, subscription_store):
        """Users with active subscriptions are on Pro tier."""
        await subscription_store.save_subscription(12345, "sub_123", "plan_abc", "ACTIVE")

        status = await entitlements.get_status(12345)
        assert status.tier == Tier.PRO
        assert status.is_pro
        assert status.subscription_id == "sub_123"

    @pytest.mark.asyncio
    async def test_cancelled_subscription_is_free(self, entitlements, subscription_store):
        """Cancelled subscriptions revert to free tier."""
        await subscription_store.save_subscription(12345, "sub_123", "plan_abc", "CANCELLED")

        status = await entitlements.get_status(12345)
        assert status.tier == Tier.FREE
        assert not status.is_pro

    @pytest.mark.asyncio
    async def test_suspended_subscription_is_free(self, entitlements, subscription_store):
        """Suspended subscriptions revert to free tier."""
        await subscription_store.save_subscription(12345, "sub_123", "plan_abc", "SUSPENDED")

        status = await entitlements.get_status(12345)
        assert status.tier == Tier.FREE
        assert not status.is_pro

    @pytest.mark.asyncio
    async def test_activate_subscription(self, entitlements, subscription_store):
        """Activating a subscription grants Pro tier."""
        status = await entitlements.activate_subscription(12345, "sub_new", "plan_abc", "test@example.com")

        assert status.is_pro
        assert status.subscription_id == "sub_new"

        stored = await subscription_store.get_active_subscription(12345)
        assert stored["status"] == "ACTIVE"
        assert stored["payer_email"] == "test@example.com"

    @pytest.mark.asyncio
    async def test_cancel_subscription(self, entitlements, subscription_store):
        """Cancelling a subscription revokes Pro tier."""
        await subscription_store.save_subscription(12345, "sub_123", "plan_abc", "ACTIVE")

        result = await entitlements.cancel_subscription("sub_123")
        assert result

        stored = subscription_store.subscriptions.get(12345)
        assert stored["status"] == "CANCELLED"

    @pytest.mark.asyncio
    async def test_muse_limit_free_user(self, entitlements):
        """Free users have daily Muse limits."""
        allowed, used, limit = await entitlements.check_muse_limit(12345)
        assert allowed
        assert used == 0
        assert limit == 5

        for _ in range(5):
            await entitlements.record_muse_usage(12345)

        allowed, used, limit = await entitlements.check_muse_limit(12345)
        assert not allowed
        assert used == 5

    @pytest.mark.asyncio
    async def test_muse_unlimited_pro_user(self, entitlements, subscription_store):
        """Pro users have unlimited Muse access."""
        await subscription_store.save_subscription(12345, "sub_123", "plan_abc", "ACTIVE")

        allowed, used, limit = await entitlements.check_muse_limit(12345)
        assert allowed
        assert limit == -1  # unlimited

    @pytest.mark.asyncio
    async def test_poll_interval_by_tier(self, entitlements, subscription_store):
        """Poll intervals differ by tier."""
        assert entitlements.poll_interval_for(is_pro=False) == 30
        assert entitlements.poll_interval_for(is_pro=True) == 10

    @pytest.mark.asyncio
    async def test_rescue_passes(self, entitlements, subscription_store):
        """Rescue passes can be purchased and used."""
        await subscription_store.add_rescue_passes(12345, 2)

        status = await entitlements.get_status(12345)
        assert status.rescue_passes_remaining == 2

        used = await entitlements.use_rescue_pass(12345)
        assert used

        status = await entitlements.get_status(12345)
        assert status.rescue_passes_remaining == 1

    @pytest.mark.asyncio
    async def test_max_accounts_by_tier(self, entitlements):
        """Account limits differ by tier."""
        assert entitlements.max_accounts(is_pro=False) == 1
        assert entitlements.max_accounts(is_pro=True) == 5

    @pytest.mark.asyncio
    async def test_entitlements_disabled_returns_free(self):
        """When PayPal is disabled, all users are free tier and Muse stays unlimited."""
        service = EntitlementService(store=None, paypal_enabled=False)
        status = await service.get_status(12345)
        assert status.tier == Tier.FREE
        assert not status.is_pro
        allowed, used, limit = await service.check_muse_limit(12345)
        assert allowed and limit == -1

    @pytest.mark.asyncio
    async def test_muse_limit_hit_then_rescue_unlimited(self, entitlements):
        """Free users are blocked after 5 Muse calls; Rescue Mode lifts the cap."""
        uid = 12345
        for _ in range(5):
            allowed, used, limit = await entitlements.check_muse_limit(uid)
            assert allowed and limit == 5
            await entitlements.record_muse_usage(uid)
        allowed, used, limit = await entitlements.check_muse_limit(uid)
        assert not allowed and used == 5 and limit == 5

        until = await entitlements.activate_rescue_mode(uid)
        allowed, used, limit = await entitlements.check_muse_limit(uid)
        assert allowed and limit == -1
        assert until > entitlements._now()
        status = await entitlements.get_status(uid)
        assert status.is_rescue
        assert format_taipei_hm(until) == until.astimezone(TAIPEI).strftime("%H:%M")
        local = until.astimezone(TAIPEI)
        en_until = format_taipei_expiry(until, "en")
        assert local.strftime("%H:%M") in en_until and str(local.day) in en_until and "," in en_until
        d = status.to_dict()
        assert d["rescue_until_taipei"] == en_until
        assert d["rescue_until_taipei_zh"] == format_taipei_expiry(until, "zh-Hant")

    @pytest.mark.asyncio
    async def test_rescue_mode_extends_and_expires(self, entitlements):
        """A second purchase extends 24h; after expiry the user is free-tier again."""
        uid = 99
        first = await entitlements.activate_rescue_mode(uid)
        second = await entitlements.activate_rescue_mode(uid)
        assert second - first >= timedelta(hours=23, minutes=50)

        entitlements.clock = lambda: second + timedelta(hours=1)
        assert await entitlements.rescue_until(uid) is None
        allowed, _, limit = await entitlements.check_muse_limit(uid)
        assert allowed and limit == 5
        assert await entitlements.poll_interval_for_user(uid) == 30

    @pytest.mark.asyncio
    async def test_poll_interval_during_rescue(self, entitlements, subscription_store):
        """Rescue Mode uses the Pro 10s poll interval; free is 30s."""
        uid = 12345
        assert await entitlements.poll_interval_for_user(uid) == 30
        await entitlements.activate_rescue_mode(uid)
        assert await entitlements.poll_interval_for_user(uid) == 10
        await subscription_store.save_subscription(uid, "sub_123", "plan_abc", "ACTIVE")
        assert await entitlements.poll_interval_for_user(uid) == 10

    @pytest.mark.asyncio
    async def test_account_limit_free_vs_pro(self, entitlements, subscription_store):
        """Free: 1 account. Replacing the same slot is allowed. Pro: 5."""
        uid = 7
        allowed, used, limit = await entitlements.check_account_limit(uid, 1, replacing=False)
        assert not allowed and used == 1 and limit == 1
        allowed, _, limit = await entitlements.check_account_limit(uid, 1, replacing=True)
        assert allowed and limit == 1
        await subscription_store.save_subscription(uid, "sub_123", "plan_abc", "ACTIVE")
        allowed, used, limit = await entitlements.check_account_limit(uid, 1, replacing=False)
        assert allowed and limit == 5
        allowed, _, limit = await entitlements.check_account_limit(uid, 5, replacing=False)
        assert not allowed and limit == 5

    @pytest.mark.asyncio
    async def test_full_rescue_locked_for_free(self, entitlements):
        uid = 3
        assert not await entitlements.can_use_full_rescue(uid)
        await entitlements.activate_rescue_mode(uid)
        assert await entitlements.can_use_full_rescue(uid)

    @pytest.mark.asyncio
    async def test_webhook_capture_activates_rescue_mode(self, webhook_handler, subscription_store):
        event = {
            "id": "evt_rescue_mode",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"id": "order_rm", "purchase_units": [{"custom_id": "12345"}]},
        }
        result = await webhook_handler.process_event(event)
        assert result.success and result.telegram_id == 12345
        until = await subscription_store.get_rescue_until(12345)
        assert until is not None and until > datetime.now(timezone.utc)
        assert subscription_store.agent_actions
        assert subscription_store.agent_actions[-1][1]["status"] == "rescue_mode"


# ---------------------------------------------------------------- webhook tests


class TestWebhookHandler:

    @pytest.mark.asyncio
    async def test_subscription_activated(self, webhook_handler, subscription_store):
        """BILLING.SUBSCRIPTION.ACTIVATED grants Pro tier."""
        event = {
            "id": "evt_123",
            "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
            "resource": {
                "id": "sub_abc",
                "plan_id": "plan_xyz",
                "custom_id": "12345",
                "subscriber": {"email_address": "test@example.com"},
            },
        }

        result = await webhook_handler.process_event(event)

        assert result.success
        assert result.event_type == "BILLING.SUBSCRIPTION.ACTIVATED"
        assert result.telegram_id == 12345

        stored = await subscription_store.get_active_subscription(12345)
        assert stored["subscription_id"] == "sub_abc"
        assert stored["status"] == "ACTIVE"

    @pytest.mark.asyncio
    async def test_subscription_cancelled(self, webhook_handler, subscription_store):
        """BILLING.SUBSCRIPTION.CANCELLED revokes Pro tier."""
        await subscription_store.save_subscription(12345, "sub_abc", "plan_xyz", "ACTIVE")

        event = {
            "id": "evt_456",
            "event_type": "BILLING.SUBSCRIPTION.CANCELLED",
            "resource": {"id": "sub_abc"},
        }

        result = await webhook_handler.process_event(event)

        assert result.success
        assert result.telegram_id == 12345

        stored = subscription_store.subscriptions.get(12345)
        assert stored["status"] == "CANCELLED"

    @pytest.mark.asyncio
    async def test_subscription_suspended(self, webhook_handler, subscription_store):
        """BILLING.SUBSCRIPTION.SUSPENDED suspends Pro tier."""
        await subscription_store.save_subscription(12345, "sub_abc", "plan_xyz", "ACTIVE")

        event = {
            "id": "evt_789",
            "event_type": "BILLING.SUBSCRIPTION.SUSPENDED",
            "resource": {"id": "sub_abc"},
        }

        result = await webhook_handler.process_event(event)

        assert result.success
        stored = subscription_store.subscriptions.get(12345)
        assert stored["status"] == "SUSPENDED"

    @pytest.mark.asyncio
    async def test_payment_capture_grants_rescue_pass(self, webhook_handler, subscription_store):
        """PAYMENT.CAPTURE.COMPLETED grants a Rescue Pass."""
        event = {
            "id": "evt_pay_123",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "order_xyz",
                "purchase_units": [{"custom_id": "12345"}],
            },
        }

        result = await webhook_handler.process_event(event)

        assert result.success
        assert result.telegram_id == 12345

        passes = await subscription_store.get_rescue_passes(12345)
        assert passes == 1

    @pytest.mark.asyncio
    async def test_idempotency_duplicate_event(self, webhook_handler, event_store, subscription_store):
        """Processing the same event twice is safe (idempotent)."""
        event = {
            "id": "evt_duplicate",
            "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
            "resource": {
                "id": "sub_dup",
                "plan_id": "plan_xyz",
                "custom_id": "12345",
            },
        }

        result1 = await webhook_handler.process_event(event)
        assert result1.success
        assert result1.message != "Already processed (idempotent)"

        result2 = await webhook_handler.process_event(event)
        assert result2.success
        assert result2.message == "Already processed (idempotent)"

        # Subscription should only be created once
        assert len([e for e in event_store.events.values() if e["event_type"] == "BILLING.SUBSCRIPTION.ACTIVATED"]) == 1

    @pytest.mark.asyncio
    async def test_unhandled_event_type(self, webhook_handler):
        """Unknown event types are acknowledged but not processed."""
        event = {
            "id": "evt_unknown",
            "event_type": "SOME.UNKNOWN.EVENT",
            "resource": {},
        }

        result = await webhook_handler.process_event(event)

        assert result.success
        assert "not handled" in result.message

    @pytest.mark.asyncio
    async def test_missing_event_id(self, webhook_handler):
        """Events without event_id are rejected."""
        event = {"event_type": "BILLING.SUBSCRIPTION.ACTIVATED", "resource": {}}

        result = await webhook_handler.process_event(event)

        assert not result.success
        assert "Missing" in result.message

    @pytest.mark.asyncio
    async def test_missing_user_for_activation(self, webhook_handler):
        """Activation without custom_id or existing subscription is handled gracefully."""
        event = {
            "id": "evt_no_user",
            "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
            "resource": {
                "id": "sub_orphan",
                "plan_id": "plan_xyz",
            },
        }

        result = await webhook_handler.process_event(event)

        assert result.success
        assert result.telegram_id is None
        assert "no user found" in result.message

    @pytest.mark.asyncio
    async def test_sandbox_capture_resource_custom_id(self, webhook_handler, subscription_store, event_store):
        """Real PAYMENT.CAPTURE.COMPLETED puts custom_id on the resource, not purchase_units."""
        event = {
            "id": "WH-2W4653713K318561S-8WB50123PY476835G",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource_type": "capture",
            "resource": {
                "id": "3C679366HH908993F",
                "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "2.99"},
                "final_capture": True,
                "custom_id": "12345",
                "supplementary_data": {"related_ids": {"order_id": "5O190127TN364715T"}},
            },
        }
        result = await webhook_handler.process_event(event)
        assert result.success and result.telegram_id == 12345
        assert await subscription_store.get_rescue_passes(12345) == 1
        assert await event_store.has_event("order:5O190127TN364715T")

    @pytest.mark.asyncio
    async def test_capture_falls_back_to_purchase_units(self, webhook_handler, subscription_store):
        event = {
            "id": "WH-PU-FALLBACK",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"id": "CAP-PU", "purchase_units": [{"custom_id": "12345"}]},
        }
        result = await webhook_handler.process_event(event)
        assert result.success and result.telegram_id == 12345

    @pytest.mark.asyncio
    async def test_capture_looks_up_order_when_custom_id_missing(
        self, event_store, subscription_store,
    ):
        class Paypal:
            async def get_order(self, order_id):
                assert order_id == "5O190127TN364715T"
                return {"id": order_id, "purchase_units": [{"custom_id": "12345"}]}

        handler = WebhookHandler(event_store, subscription_store, paypal=Paypal())
        event = {
            "id": "WH-LOOKUP",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "3C679366HH908993F",
                "status": "COMPLETED",
                "supplementary_data": {"related_ids": {"order_id": "5O190127TN364715T"}},
            },
        }
        result = await handler.process_event(event)
        assert result.success and result.telegram_id == 12345
        assert await subscription_store.get_rescue_passes(12345) == 1

    @pytest.mark.asyncio
    async def test_unattributed_capture_is_not_saved(self, webhook_handler, event_store):
        event = {
            "id": "WH-ORPHAN-CAP",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"id": "CAP-ORPHAN", "status": "COMPLETED"},
        }
        result = await webhook_handler.process_event(event)
        assert not result.success
        assert "no custom_id" in result.message
        assert "WH-ORPHAN-CAP" not in event_store.events
        retry = await webhook_handler.process_event(event)
        assert not retry.success
        assert retry.message != "Already processed (idempotent)"

    @pytest.mark.asyncio
    async def test_checkout_order_completed_grants_rescue(self, webhook_handler, subscription_store):
        event = {
            "id": "WH-ORDER-DONE",
            "event_type": "CHECKOUT.ORDER.COMPLETED",
            "resource": {
                "id": "5O190127TN364715T",
                "status": "COMPLETED",
                "purchase_units": [{"custom_id": "12345"}],
            },
        }
        result = await webhook_handler.process_event(event)
        assert result.success and result.telegram_id == 12345
        assert await subscription_store.get_rescue_passes(12345) == 1

    @pytest.mark.asyncio
    async def test_checkout_order_approved_does_not_grant(self, webhook_handler, subscription_store):
        event = {
            "id": "WH-ORDER-APPR",
            "event_type": "CHECKOUT.ORDER.APPROVED",
            "resource": {
                "id": "5O190127TN364715T",
                "status": "APPROVED",
                "purchase_units": [{"custom_id": "12345"}],
            },
        }
        result = await webhook_handler.process_event(event)
        assert result.success and result.telegram_id is None
        assert await subscription_store.get_rescue_passes(12345) == 0

    @pytest.mark.asyncio
    async def test_capture_and_order_completed_are_idempotent(
        self, webhook_handler, subscription_store, event_store,
    ):
        capture = {
            "id": "WH-CAP-1",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-1",
                "custom_id": "12345",
                "supplementary_data": {"related_ids": {"order_id": "ORD-1"}},
            },
        }
        order = {
            "id": "WH-ORD-1",
            "event_type": "CHECKOUT.ORDER.COMPLETED",
            "resource": {"id": "ORD-1", "custom_id": "12345"},
        }
        first = await webhook_handler.process_event(capture)
        second = await webhook_handler.process_event(order)
        assert first.success and second.success
        assert await subscription_store.get_rescue_passes(12345) == 1
        until = await subscription_store.get_rescue_until(12345)
        assert until is not None


# ---------------------------------------------------------------- PayPal client tests (mocked)


class TestPayPalClient:

    @pytest.mark.asyncio
    async def test_create_subscription_success(self):
        """Creating a subscription returns approval URL."""
        client = PayPalClient("client_id", "client_secret")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "id": "sub_new",
                "status": "APPROVAL_PENDING",
                "links": [
                    {"rel": "self", "href": "https://api.paypal.com/v1/billing/subscriptions/sub_new"},
                    {"rel": "approve", "href": "https://www.paypal.com/checkoutnow?token=abc"},
                ],
            }

            result = await client.create_subscription(
                plan_id="plan_123",
                return_url="https://example.com/return",
                cancel_url="https://example.com/cancel",
                custom_id="12345",
            )

            assert result["subscription_id"] == "sub_new"
            assert "paypal.com" in result["approve_url"]

    @pytest.mark.asyncio
    async def test_get_subscription_details(self):
        """Getting subscription details returns parsed SubscriptionDetails."""
        client = PayPalClient("client_id", "client_secret")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "id": "sub_123",
                "plan_id": "plan_abc",
                "status": "ACTIVE",
                "subscriber": {"email_address": "test@example.com"},
                "billing_info": {"next_billing_time": "2026-12-01T00:00:00Z"},
            }

            details = await client.get_subscription("sub_123")

            assert details.subscription_id == "sub_123"
            assert details.status == "ACTIVE"
            assert details.is_active()
            assert details.payer_email == "test@example.com"

    @pytest.mark.asyncio
    async def test_cancel_subscription(self):
        """Cancelling a subscription succeeds."""
        client = PayPalClient("client_id", "client_secret")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {}

            result = await client.cancel_subscription("sub_123", "User requested")

            assert result
            mock_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_order_for_rescue_pass(self):
        """Creating a one-time order returns approval URL."""
        client = PayPalClient("client_id", "client_secret")

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = {
                "id": "order_xyz",
                "status": "CREATED",
                "links": [
                    {"rel": "approve", "href": "https://www.paypal.com/checkoutnow?token=xyz"},
                ],
            }

            result = await client.create_order(
                amount_usd="2.99",
                description="LiqGuard Rescue Pass",
                return_url="https://example.com/return",
                cancel_url="https://example.com/cancel",
                custom_id="12345",
            )

            assert result["order_id"] == "order_xyz"
            assert "paypal.com" in result["approve_url"]

    def test_webhook_verification_without_webhook_id(self):
        """Missing PAYPAL_WEBHOOK_ID must reject, never skip verification."""
        client = PayPalClient("client_id", "client_secret", webhook_id=None)

        result = client.verify_webhook_signature(
            "trans_id", "2026-01-01T00:00:00Z", "https://cert.url", "SHA256withRSA", "sig", b"{}"
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_webhook_online_verification_without_webhook_id(self):
        client = PayPalClient("client_id", "client_secret", webhook_id=None)
        result = await client.verify_webhook_online(
            "trans_id", "2026-01-01T00:00:00Z", "https://cert.url", "SHA256withRSA", "sig", {"id": "evt"}
        )
        assert result is False


# ---------------------------------------------------------------- ProStatus tests


class TestProStatus:

    def test_free_status(self):
        """Free status creation."""
        status = ProStatus.free()
        assert status.tier == Tier.FREE
        assert not status.is_pro
        assert status.subscription_id is None

    def test_pro_status(self):
        """Pro status creation."""
        status = ProStatus.pro("sub_123", "ACTIVE", "plan_abc")
        assert status.tier == Tier.PRO
        assert status.is_pro
        assert status.subscription_id == "sub_123"

    def test_status_to_dict(self):
        """Status serialization."""
        status = ProStatus.pro("sub_123", "ACTIVE", "plan_abc")
        d = status.to_dict()

        assert d["tier"] == "pro"
        assert d["is_pro"] is True
        assert d["subscription_id"] == "sub_123"


def test_format_taipei_expiry_shows_calendar_date():
    """HH:MM alone looks expired when Rescue Mode ends tomorrow at the same clock time."""
    from liqguard.i18n import EN, ZH, t

    dt = datetime(2026, 11, 13, 22, 49, tzinfo=TAIPEI)
    en, zh = format_taipei_expiry(dt, "en"), format_taipei_expiry(dt, "zh-Hant")
    assert en == "Nov 13, 22:49"
    assert zh == "11/13 22:49"
    assert format_taipei_hm(dt) == "22:49"
    en_msg = t(EN, "pro.notifications.rescue_mode_active", until=en)
    zh_msg = t(ZH, "pro.notifications.rescue_mode_active", until=zh)
    assert "Nov 13, 22:49" in en_msg and "(Taipei)" in en_msg
    assert "11/13 22:49" in zh_msg and "台北" in zh_msg
    assert t(EN, "pro.rescue_mode_until", until=en) == "Rescue Mode active until Nov 13, 22:49 (Taipei)"
    assert t(ZH, "pro.rescue_mode_until", until=zh) == "救援模式有效至 11/13 22:49（台北）"
    assert t(EN, "web.rescue_paid", until=en).startswith("Payment received")
    assert "Telegram" in t(EN, "web.rescue_paid", until=en)
    assert "Telegram" in t(ZH, "web.rescue_paid", until=zh)


# ---------------------------------------------------------------- upgrade suggestion tests


def test_upgrade_suggestion_for_free_user():
    """Free users get upgrade suggestions."""
    from liqguard.payments.entitlements import upgrade_suggestion

    status = ProStatus.free()

    en = upgrade_suggestion(status, "en")
    assert "Pro" in en
    assert "upgrade" in en.lower() or "Upgrade" in en

    zh = upgrade_suggestion(status, "zh-Hant")
    assert "Pro" in zh


def test_no_upgrade_suggestion_for_pro_user():
    """Pro users don't get upgrade suggestions."""
    from liqguard.payments.entitlements import upgrade_suggestion

    status = ProStatus.pro("sub_123")
    assert upgrade_suggestion(status) is None


def test_no_upgrade_suggestion_when_paypal_disabled():
    from liqguard.payments.entitlements import upgrade_suggestion

    assert upgrade_suggestion(ProStatus.free(), paypal_enabled=False) is None


class TestEncryptedStorageSubscriptions:
    @pytest.mark.asyncio
    async def test_subscription_and_event_idempotency(self, ctx):
        await ctx.storage.save_user(User(telegram_id=12345, threshold=ctx.settings.default_threshold()))
        await ctx.storage.save_subscription(12345, "I-SUB", "P-PLAN", "ACTIVE", payer_email="a@b.c")
        assert (await ctx.storage.get_active_subscription(12345))["subscription_id"] == "I-SUB"
        assert await ctx.storage.get_user_by_subscription("I-SUB") == 12345

        handler = WebhookHandler(ctx.storage, ctx.storage)
        event = {
            "id": "WH-1",
            "event_type": "BILLING.SUBSCRIPTION.CANCELLED",
            "resource": {"id": "I-SUB"},
        }
        first = await handler.process_event(event)
        second = await handler.process_event(event)
        assert first.success and first.telegram_id == 12345
        assert second.success and second.message == "Already processed (idempotent)"
        assert (await ctx.storage.get_active_subscription(12345)) is None


class TestBillingDegradesWithoutPayPal:
    def test_public_billing_status_is_disabled(self):
        from fastapi.testclient import TestClient

        from liqguard.web import create_app

        with TestClient(create_app()) as client:
            body = client.get("/api/billing/status").json()
            assert body["enabled"] is False
            assert "ag-grid-community" in client.get("/").text
            assert "billingGrid" in client.get("/").text
            assert "positionsGrid" in client.get("/").text
            assert "alertsGrid" in client.get("/").text

    @pytest.mark.asyncio
    async def test_signed_in_billing_is_free_when_paypal_unset(self, ctx):
        from fastapi.testclient import TestClient

        from liqguard.web import create_app
        from liqguard.web.auth import SESSION_COOKIE

        await ctx.storage.save_user(User(telegram_id=1001, threshold=ctx.settings.default_threshold()))
        with TestClient(create_app(ctx), base_url="https://testserver") as client:
            client.cookies.set(SESSION_COOKIE, ctx.sessions.issue(1001, 0))
            body = client.get("/api/me/billing").json()
            assert body["enabled"] is False
            assert body["tier"] == "free"
            assert client.post("/webhooks/paypal", json={"id": "x", "event_type": "X"}).status_code == 503


class TestWebhookSignatureHeaders:
    def test_extract_headers_is_case_insensitive_via_starlette_style(self):
        from liqguard.payments.webhooks import extract_webhook_headers

        headers = extract_webhook_headers({
            "paypal-transmission-id": "t1",
            "paypal-transmission-time": "2026-01-01T00:00:00Z",
            "paypal-cert-url": "https://example.test/cert",
            "paypal-auth-algo": "SHA256withRSA",
            "paypal-transmission-sig": "sig",
        })
        assert headers["transmission_id"] == "t1"
        assert headers["transmission_sig"] == "sig"


class TestPayPalAgent:
    def test_intent_from_text(self):
        from liqguard.payments.agent import intent_from_text

        assert intent_from_text("get me a Rescue Pass") == "rescue"
        assert intent_from_text("給我救援通行證") == "rescue"
        assert intent_from_text("upgrade to Pro") == "pro"
        assert intent_from_text("升級至 Pro") == "pro"
        assert intent_from_text("what is my risk?") is None

    def test_grid_filter(self):
        from liqguard.payments.agent import parse_grid_filter

        spec = parse_grid_filter("positions within 5% of liquidation")
        assert spec["max_distance_pct"] == 5.0
        zh = parse_grid_filter("距離強平 5% 以內的多單")
        assert zh["max_distance_pct"] == 5.0
        assert zh["side"] == "long"

    @pytest.mark.asyncio
    async def test_execute_tool_uses_fixed_price_and_ignores_model_amount(self):
        from liqguard.payments.agent import RESCUE_PASS_USD, execute_tool

        client = PayPalClient("id", "secret", "https://api-m.sandbox.paypal.com")
        client.create_order = AsyncMock(return_value={
            "order_id": "O-1", "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=x",
        })
        result = await execute_tool(
            client, "rescue", user_id=7, plan_id=None,
            return_url="https://x/r", cancel_url="https://x/c",
            model_args={"amount": "9999.00", "value": "50"},
        )
        assert result.status == "created" and result.tool == "create_order"
        assert client.create_order.await_args.kwargs["amount_usd"] == RESCUE_PASS_USD
        assert "amount" not in result.model_args

    @pytest.mark.asyncio
    async def test_execute_tool_refuses_live(self):
        from liqguard.payments.agent import execute_tool

        client = PayPalClient("id", "secret", "https://api-m.paypal.com")
        client.create_order = AsyncMock()
        result = await execute_tool(
            client, "rescue", user_id=7, plan_id=None,
            return_url="https://x/r", cancel_url="https://x/c",
        )
        assert result.status == "rejected"
        client.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_muse_tool_choice_falls_back_when_mock(self):
        from liqguard.payments.agent import muse_pick_tool

        class MockLLM:
            is_mock = True
            client = None

        pick = await muse_pick_tool(MockLLM(), "upgrade to Pro", "en")
        assert (pick.product, pick.source) == ("pro", "rules")

    @pytest.mark.asyncio
    async def test_muse_tool_choice_uses_toolkit_name(self):
        from liqguard.payments.agent import muse_pick_tool

        class Fn:
            name = "create_order"

        class Call:
            function = Fn()

        class Msg:
            tool_calls = [Call()]
            content = None

        class Choice:
            message = Msg()

        class LLM:
            is_mock = False
            client = object()
            model = "muse-spark-1.3"

            async def _create(self, kwargs):
                assert kwargs["tools"]
                assert kwargs["reasoning_effort"] == "low"
                assert kwargs["max_tokens"] >= 800
                return type("R", (), {"choices": [Choice()]})()

        pick = await muse_pick_tool(LLM(), "please charge my trading account $500", "en")
        assert (pick.product, pick.source) == ("rescue", "muse")

    @pytest.mark.asyncio
    async def test_muse_checkout_kwargs_leave_room_for_reasoning(self):
        from liqguard.payments.agent import run_checkout

        class Fn:
            name = "create_order"

        class Call:
            function = Fn()

        class Msg:
            tool_calls = [Call()]
            content = "Rescue Pass fits this alert."

        class Choice:
            message = Msg()

        class LLM:
            is_mock = False
            client = object()
            seen = None

            async def _create(self, kwargs):
                self.seen = kwargs
                return type("R", (), {"choices": [Choice()]})()

        client = PayPalClient("id", "secret", "https://api-m.sandbox.paypal.com")
        client.create_order = AsyncMock(return_value={
            "order_id": "O-1", "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=x",
        })
        llm = LLM()
        result = await run_checkout(
            client, llm, user_id=7, text="get me a Rescue Pass", lang="en",
            plan_id=None, dashboard_url="https://x", product="rescue",
        )
        assert result.source == "muse" and result.product == "rescue"
        assert llm.seen["reasoning_effort"] == "low"
        assert llm.seen["max_tokens"] >= 800

    @pytest.mark.asyncio
    async def test_forced_rescue_still_calls_muse(self):
        from liqguard.payments.agent import run_checkout

        class Fn:
            name = "create_order"

        class Call:
            function = Fn()

        class Msg:
            tool_calls = [Call()]
            content = "Rescue Pass covers the next 24h of Muse while you're this close to liq."

        class Choice:
            message = Msg()

        class LLM:
            is_mock = False
            client = object()
            called = False

            async def _create(self, kwargs):
                self.called = True
                assert "liquidation-risk" in kwargs["messages"][1]["content"]
                return type("R", (), {"choices": [Choice()]})()

        client = PayPalClient("id", "secret", "https://api-m.sandbox.paypal.com")
        client.create_order = AsyncMock(return_value={
            "order_id": "O-1", "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=x",
        })
        llm = LLM()
        result = await run_checkout(
            client, llm, user_id=7, text=(
                "I'm looking at a LiqGuard liquidation-risk alert. Get me a Rescue Pass now."
            ),
            lang="en", plan_id=None, dashboard_url="https://x", product="rescue",
        )
        assert llm.called
        assert result.product == "rescue" and result.source == "muse"
        assert result.note.startswith("Rescue Pass covers")
        assert client.create_order.await_args.kwargs["amount_usd"] == "2.99"

    @pytest.mark.asyncio
    async def test_forced_product_keeps_rescue_if_muse_picks_pro(self):
        from liqguard.payments.agent import run_checkout

        class Fn:
            name = "create_subscription"

        class Call:
            function = Fn()

        class Msg:
            tool_calls = [Call()]
            content = "Pro is the better long-term fit."

        class Choice:
            message = Msg()

        class LLM:
            is_mock = False
            client = object()

            async def _create(self, kwargs):
                return type("R", (), {"choices": [Choice()]})()

        client = PayPalClient("id", "secret", "https://api-m.sandbox.paypal.com")
        client.create_order = AsyncMock(return_value={
            "order_id": "O-1", "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=x",
        })
        result = await run_checkout(
            client, LLM(), user_id=7, text="upgrade to Pro", lang="en",
            plan_id="P-1", dashboard_url="https://x", product="rescue",
        )
        assert result.product == "rescue" and result.tool == "create_order"
        assert result.source == "muse"
        client.create_order.assert_awaited()

    @pytest.mark.asyncio
    async def test_muse_timeout_falls_back_without_unavailable(self):
        import asyncio

        from liqguard.i18n import EN, ZH, t
        from liqguard.payments import agent as agent_mod
        from liqguard.payments.agent import run_checkout

        class Slow:
            is_mock = False
            client = object()

            async def _create(self, kwargs):
                await asyncio.sleep(0.2)
                raise AssertionError("should have timed out")

        client = PayPalClient("id", "secret", "https://api-m.sandbox.paypal.com")
        client.create_order = AsyncMock(return_value={
            "order_id": "O-1", "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=x",
        })
        with patch.object(agent_mod, "MUSE_CHECKOUT_TIMEOUT", 0.01):
            result = await run_checkout(
                client, Slow(), user_id=7, text="get me a Rescue Pass", lang="en",
                plan_id=None, dashboard_url="https://x", product="rescue",
            )
        assert result.source == "timeout" and result.product == "rescue"
        assert "unavailable" not in t(EN, "pro.agent.source_rules").lower()
        assert "不可用" not in t(ZH, "pro.agent.source_rules")
        assert "unavailable" not in t(EN, "pro.agent.source_timeout").lower()
