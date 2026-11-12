"""Hotfix: real-shaped PayPal captures, sessionless return URLs, Muse on px/rescue."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from liqguard.ai import MetaLLMClient
from liqguard.config import Settings
from liqguard.core import AppContext
from liqguard.core.wicks import WickService
from liqguard.i18n import EN, t
from liqguard.models import User
from liqguard.payments import PayPalError
from liqguard.payments.paypal_client import SubscriptionDetails
from liqguard.storage import EncryptedStorage
from liqguard.web import create_app
from liqguard.web.auth import SESSION_COOKIE

from .conftest import ALICE, BOT_TOKEN
from .test_hackathon_e2e import CSRF, PAYPAL_HEADERS

SANDBOX_CAPTURE = {
    "id": "WH-2W4653713K318561S-8WB50123PY476835G",
    "event_type": "PAYMENT.CAPTURE.COMPLETED",
    "resource_type": "capture",
    "resource": {
        "id": "3C679366HH908993F",
        "status": "COMPLETED",
        "amount": {"currency_code": "USD", "value": "2.99"},
        "final_capture": True,
        "custom_id": str(ALICE),
        "supplementary_data": {"related_ids": {"order_id": "5O190127TN364715T"}},
    },
}


@pytest.fixture
async def paypal_ctx(tmp_path):
    settings = Settings(
        telegram_token=BOT_TOKEN,
        db_path=str(tmp_path / "liqguard.db"),
        key_file=tmp_path / "master.key",
        public_url="https://liqguard.test",
        bot_username="liqguard_test_bot",
        paypal_client_id="sandbox-client",
        paypal_client_secret="sandbox-secret",
        paypal_webhook_id="WH-TEST",
        paypal_plan_id="P-TEST",
        paypal_env="sandbox",
    )
    storage = EncryptedStorage(db_path=settings.db_path, encryption_key=EncryptedStorage.generate_key())
    ctx = await AppContext.create(
        settings, storage=storage, llm=MetaLLMClient(use_mock=True),
        simulation_options={"tick_interval": 0, "escalation_seconds_per_minute": 0.05},
        wicks=WickService(sources=[]),
    )
    await ctx.storage.save_user(User(telegram_id=ALICE, threshold=ctx.settings.default_threshold()))
    yield ctx
    await ctx.close()


def _client(ctx) -> TestClient:
    return TestClient(create_app(ctx), base_url="https://testserver")


class TestWebhookHttp:
    @pytest.mark.asyncio
    async def test_real_shaped_capture_activates_rescue(self, paypal_ctx):
        with _client(paypal_ctx) as client:
            paypal = client.app.state.paypal
            paypal.verify_webhook_online = AsyncMock(return_value=True)
            granted = client.post("/webhooks/paypal", headers=PAYPAL_HEADERS, json=SANDBOX_CAPTURE)
            assert granted.status_code == 200 and granted.json()["processed"] is True
            client.cookies.set(SESSION_COOKIE, paypal_ctx.sessions.issue(ALICE, 0))
            billed = client.get("/api/me/billing").json()
            assert billed["is_rescue"] is True and billed["rescue_until"]

    @pytest.mark.asyncio
    async def test_unattributed_capture_returns_503_and_can_retry(self, paypal_ctx):
        orphan = {
            "id": "WH-ORPHAN-HTTP",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-ORPHAN",
                "status": "COMPLETED",
                "supplementary_data": {"related_ids": {"order_id": "ORD-LOOKUP"}},
            },
        }
        with _client(paypal_ctx) as client:
            paypal = client.app.state.paypal
            paypal.verify_webhook_online = AsyncMock(return_value=True)
            paypal.get_order = AsyncMock(side_effect=PayPalError("not yet", 404))
            first = client.post("/webhooks/paypal", headers=PAYPAL_HEADERS, json=orphan)
            assert first.status_code == 503
            assert await paypal_ctx.storage.has_event("WH-ORPHAN-HTTP") is False

            paypal.get_order = AsyncMock(return_value={
                "id": "ORD-LOOKUP",
                "purchase_units": [{"custom_id": str(ALICE)}],
            })
            second = client.post("/webhooks/paypal", headers=PAYPAL_HEADERS, json=orphan)
            assert second.status_code == 200 and second.json()["processed"] is True
            client.cookies.set(SESSION_COOKIE, paypal_ctx.sessions.issue(ALICE, 0))
            assert client.get("/api/me/billing").json()["is_rescue"] is True


class TestSessionlessReturn:
    @pytest.mark.asyncio
    async def test_rescue_return_activates_without_session(self, paypal_ctx):
        notified: list[tuple] = []

        async def hook(telegram_id, kind, resource_id):
            notified.append((telegram_id, kind, resource_id))

        paypal_ctx.billing_notify = hook
        with _client(paypal_ctx) as client:
            paypal = client.app.state.paypal
            paypal.capture_order = AsyncMock(return_value={
                "id": "5O190127TN364715T",
                "status": "COMPLETED",
                "purchase_units": [{"custom_id": str(ALICE)}],
            })
            paypal.verify_webhook_online = AsyncMock(return_value=True)
            resp = client.get("/api/me/billing/rescue-return", params={"token": "5O190127TN364715T"})
            assert resp.status_code == 200
            assert t(EN, "pro.return.body") in resp.text
            assert notified == [(ALICE, "rescue_pass_purchased", "5O190127TN364715T")]

            client.cookies.set(SESSION_COOKIE, paypal_ctx.sessions.issue(ALICE, 0))
            billed = client.get("/api/me/billing").json()
            assert billed["is_rescue"] is True
            assert billed["rescue_until_taipei"] in resp.text
            assert "," in billed["rescue_until_taipei"]
            assert "/" in billed["rescue_until_taipei_zh"]
            until, remaining = billed["rescue_until"], billed["rescue_passes_remaining"]

            again = client.post("/webhooks/paypal", headers=PAYPAL_HEADERS, json=SANDBOX_CAPTURE)
            assert again.status_code == 200
            billed2 = client.get("/api/me/billing").json()
            assert billed2["rescue_until"] == until
            assert billed2["rescue_passes_remaining"] == remaining
            assert notified == [(ALICE, "rescue_pass_purchased", "5O190127TN364715T")]

    @pytest.mark.asyncio
    async def test_rescue_return_already_captured_uses_get_order(self, paypal_ctx):
        with _client(paypal_ctx) as client:
            paypal = client.app.state.paypal
            paypal.capture_order = AsyncMock(side_effect=PayPalError(
                "already", 422,
                {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]},
            ))
            paypal.get_order = AsyncMock(return_value={
                "id": "ORD-AC",
                "status": "COMPLETED",
                "purchase_units": [{"custom_id": str(ALICE)}],
            })
            resp = client.get("/api/me/billing/rescue-return", params={"token": "ORD-AC"})
            assert resp.status_code == 200
            assert t(EN, "pro.return.body") in resp.text
            client.cookies.set(SESSION_COOKIE, paypal_ctx.sessions.issue(ALICE, 0))
            assert client.get("/api/me/billing").json()["is_rescue"] is True

    @pytest.mark.asyncio
    async def test_pro_return_activates_without_session(self, paypal_ctx):
        with _client(paypal_ctx) as client:
            paypal = client.app.state.paypal
            paypal.get_subscription = AsyncMock(return_value=SubscriptionDetails(
                "I-HOTFIX", "P-TEST", "ACTIVE", payer_email="a@b.c", custom_id=str(ALICE),
            ))
            resp = client.get("/api/me/billing/return", params={"subscription_id": "I-HOTFIX"})
            assert resp.status_code == 200
            assert t(EN, "pro.return.body") in resp.text
            assert t(EN, "pro.return.pro") in resp.text
            client.cookies.set(SESSION_COOKIE, paypal_ctx.sessions.issue(ALICE, 0))
            billed = client.get("/api/me/billing").json()
            assert billed["is_pro"] is True and billed["subscription_id"] == "I-HOTFIX"

    @pytest.mark.asyncio
    async def test_rescue_return_with_session_redirects_to_dashboard_notice(self, paypal_ctx):
        with _client(paypal_ctx) as client:
            client.cookies.set(SESSION_COOKIE, paypal_ctx.sessions.issue(ALICE, 0))
            paypal = client.app.state.paypal
            paypal.capture_order = AsyncMock(return_value={
                "id": "ORD-SESS",
                "status": "COMPLETED",
                "purchase_units": [{"custom_id": str(ALICE)}],
            })
            resp = client.get("/api/me/billing/rescue-return", params={"token": "ORD-SESS"}, follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"] == "/me?rescue=success"
            page = client.get("/me?rescue=success")
            assert page.status_code == 200
            assert 'id="paymentNotice"' in page.text
            billed = client.get("/api/me/billing").json()
            assert billed["is_rescue"] is True
            assert "," in billed["rescue_until_taipei"]


class TestMuseOnForcedProduct:
    @pytest.mark.asyncio
    async def test_agent_endpoint_records_muse_source_for_rescue(self, paypal_ctx):
        class Fn:
            name = "create_order"

        class Call:
            function = Fn()

        class Msg:
            tool_calls = [Call()]
            content = "You're inside the danger band — Rescue Pass keeps Muse unlimited for 24h."

        class Choice:
            message = Msg()

        class Live:
            is_mock = False
            client = object()
            model = "muse-spark-1.3"

            async def _create(self, kwargs):
                return type("R", (), {"choices": [Choice()]})()

        paypal_ctx.llm = Live()
        with _client(paypal_ctx) as client:
            paypal = client.app.state.paypal
            paypal.create_order = AsyncMock(return_value={
                "order_id": "ORDER-MUSE",
                "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=muse",
            })
            client.cookies.set(SESSION_COOKIE, paypal_ctx.sessions.issue(ALICE, 0))
            body = client.post(
                "/api/me/billing/agent", headers=CSRF,
                json={"product": "rescue", "text": "liquidation-risk alert, get a Rescue Pass"},
            ).json()
            assert body["source"] == "muse" and body["product"] == "rescue"
            assert "danger band" in (body.get("note") or "")
            log = client.get("/api/me/billing").json()["agent_actions"]
            assert log and log[0]["source"] == "muse" and log[0]["product"] == "rescue"

    @pytest.mark.asyncio
    async def test_fallback_copy_omits_unavailable(self):
        from liqguard.i18n import ZH

        assert "unavailable" not in t(EN, "pro.agent.source_rules").lower()
        assert "不可用" not in t(ZH, "pro.agent.source_rules")
        assert "unavailable" not in t(EN, "pro.agent.source_timeout").lower()
