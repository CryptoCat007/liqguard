"""Dispatcher-style PayPal / AG Grid E2E (no live Telegram poller, no live PayPal)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from liqguard.ai import MetaLLMClient
from liqguard.config import Settings
from liqguard.core import AppContext
from liqguard.core.wicks import WickService
from liqguard.i18n import EN, ZH, web_catalog
from liqguard.models import User
from liqguard.payments import PayPalClient
from liqguard.payments.paypal_client import SubscriptionDetails
from liqguard.storage import EncryptedStorage
from liqguard.testing.prerelease import USER, Session, run_one
from liqguard.testing.telegram import FakeTelegram
from liqguard.web import create_app
from liqguard.web.auth import SESSION_COOKIE
from liqguard.web.security import CSRF_HEADER

from .conftest import ALICE, BOT_TOKEN

CSRF = {CSRF_HEADER: "1"}
PAYPAL_HEADERS = {
    "paypal-transmission-id": "t1",
    "paypal-transmission-time": "2026-01-01T00:00:00Z",
    "paypal-cert-url": "https://example.test/cert",
    "paypal-auth-algo": "SHA256withRSA",
    "paypal-transmission-sig": "sig",
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


class TestDispatcherPayPalUnset:
    @pytest.mark.parametrize("lang", [EN, ZH])
    async def test_pro_billing_and_crawl(self, lang):
        for name in ("pro-billing", "crawl-buttons"):
            result = await run_one(name, lang)
            assert result.ok, f"{name} [{lang}]: {result.detail}"

    async def test_callbacks_log_handled_without_payload(self, caplog, tmp_path):
        caplog.set_level(logging.INFO, logger="liqguard.telegram.callbacks")
        async with Session(EN, tmp_path) as s:
            menu = await s.main_menu()
            account = await s.tap_action(menu, "ac", "account")
            await s.tap_action(account, "pu", "pro")
            sign = s.tg.app.ui.signer(USER)
            host = await s.main_menu()
            await s.tap(host, sign("pm"), "manage")
            await s.tap(host, sign("rp"), "rescue")
            await s.tap(host, sign("px", "auto"), "muse checkout")

        lines = [r.getMessage() for r in caplog.records if r.name == "liqguard.telegram.callbacks"]
        done = [ln for ln in lines if ln.startswith("Callback done:")]
        assert done, lines
        assert all("outcome=handled" in ln for ln in done), done
        assert not any("UNHANDLED" in ln or "outcome=error" in ln for ln in lines)
        for action in ("pu", "pm", "rp", "px", "ac"):
            assert any(f"action=ui:{action} outcome=handled" in ln for ln in done), (action, done)


class TestDispatcherPayPalMocked:
    async def test_upgrade_offers_sandbox_url_button(self, paypal_ctx):
        approve = "https://www.sandbox.paypal.com/checkoutnow?token=fake-sub"
        with patch.object(PayPalClient, "create_subscription", new_callable=AsyncMock) as create_sub:
            create_sub.return_value = {"subscription_id": "I-MOCK", "approve_url": approve}
            tg = FakeTelegram.create(paypal_ctx)
            async with tg:
                await tg.send_text(ALICE, "/start", "en")
                menu = tg.to(ALICE)[-1]
                await tg.press(ALICE, menu, "Account", "en")
                account = tg.to(ALICE)[-1]
                await tg.press(ALICE, account, "Upgrade to Pro", "en")
                pro = tg.to(ALICE)[-1]
                urls = [b.url for row in (pro.keyboard.inline_keyboard if pro.keyboard else []) for b in row if b.url]
                assert approve in urls
                create_sub.assert_awaited()

    async def test_agent_checkout_button_and_text(self, paypal_ctx):
        approve_pro = "https://www.sandbox.paypal.com/webapps/billing/subscriptions?ba_token=agent-pro"
        approve_rp = "https://www.sandbox.paypal.com/checkoutnow?token=agent-rp"
        with patch.object(PayPalClient, "create_subscription", new_callable=AsyncMock) as create_sub, \
             patch.object(PayPalClient, "create_order", new_callable=AsyncMock) as create_order:
            create_sub.return_value = {"subscription_id": "I-AGENT", "approve_url": approve_pro}
            create_order.return_value = {"order_id": "ORDER-A", "approve_url": approve_rp}
            tg = FakeTelegram.create(paypal_ctx)
            async with tg:
                await tg.send_text(ALICE, "/start", "en")
                menu = tg.to(ALICE)[-1]
                await tg.press(ALICE, menu, "Account", "en")
                account = tg.to(ALICE)[-1]
                await tg.press(ALICE, account, "Muse checkout", "en")
                screen = tg.to(ALICE)[-1]
                urls = [b.url for row in (screen.keyboard.inline_keyboard if screen.keyboard else []) for b in row if b.url]
                assert approve_pro in urls
                create_sub.assert_awaited()
                await tg.send_text(ALICE, "get me a Rescue Pass", "en")
                text_screen = tg.to(ALICE)[-1]
                assert "Rescue Pass" in (text_screen.text or "")
                urls2 = [b.url for row in (text_screen.keyboard.inline_keyboard if text_screen.keyboard else []) for b in row if b.url]
                assert approve_rp in urls2
                create_order.assert_awaited()
                assert create_order.await_args.kwargs["amount_usd"] == "2.99"


class TestWebWithoutPayPal:
    def test_public_demo_grids_and_hidden_checkout(self):
        with TestClient(create_app()) as client:
            html = client.get("/").text
            assert "ag-grid-community" in html
            assert "ag-charts-community" in html
            assert 'id="positionsGrid"' in html
            assert 'id="alertsGrid"' in html
            assert 'id="billingGrid"' in html
            assert 'id="agentGrid"' in html
            assert 'id="gridFilter"' in html
            assert 'id="foldBilling"' in html
            status = client.get("/api/billing/status").json()
            assert status["enabled"] is False
            snap = client.get("/api/demo/snapshot").json()
            assert snap.get("overview", {}).get("positions")
            en = client.get("/api/i18n/en").json()["strings"]
            zh = client.get("/api/i18n/zh-Hant").json()["strings"]
            assert en["billing_title"] == web_catalog(EN)["billing_title"]
            assert zh["billing_title"] != en["billing_title"]

    @pytest.mark.asyncio
    async def test_signed_in_stays_free(self, ctx):
        await ctx.storage.save_user(User(telegram_id=ALICE, threshold=ctx.settings.default_threshold()))
        with _client(ctx) as client:
            client.cookies.set(SESSION_COOKIE, ctx.sessions.issue(ALICE, 0))
            body = client.get("/api/me/billing").json()
            assert body["enabled"] is False and body["tier"] == "free"
            assert client.post("/api/me/billing/subscribe", headers=CSRF, json={}).status_code == 503
            assert client.post("/api/me/billing/cancel", headers=CSRF, json={}).status_code == 503
            assert client.post("/api/me/billing/rescue-pass", headers=CSRF, json={}).status_code == 503
            assert client.post("/api/me/billing/agent", headers=CSRF, json={"text": "upgrade to Pro"}).status_code == 503
            assert client.post("/webhooks/paypal", json={"id": "x", "event_type": "X"}).status_code == 503
            filt = client.get("/api/grid-filter", params={"q": "positions within 5% of liquidation"}).json()
            assert filt["max_distance_pct"] == 5.0
            assert filt["source"] == "rules"


class TestWebMockedPayPal:
    @pytest.mark.asyncio
    async def test_subscribe_cancel_and_webhooks(self, paypal_ctx):
        with _client(paypal_ctx) as client:
            paypal = client.app.state.paypal
            assert paypal is not None
            paypal.create_subscription = AsyncMock(return_value={
                "subscription_id": "I-MOCK",
                "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=sub",
            })
            paypal.cancel_subscription = AsyncMock(return_value=True)
            paypal.create_order = AsyncMock(return_value={
                "order_id": "ORDER-1",
                "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=rp",
            })
            paypal.verify_webhook_online = AsyncMock(return_value=True)
            paypal.get_subscription = AsyncMock(return_value=SubscriptionDetails(
                "I-MOCK", "P-TEST", "ACTIVE", payer_email="a@b.c",
            ))

            client.cookies.set(SESSION_COOKIE, paypal_ctx.sessions.issue(ALICE, 0))
            public = client.get("/api/billing/status").json()
            assert public["enabled"] is True and public["plan_id"] == "P-TEST"

            free = client.get("/api/me/billing").json()
            assert free["enabled"] is True and free["is_pro"] is False

            sub = client.post("/api/me/billing/subscribe", headers=CSRF, json={})
            assert sub.status_code == 200
            assert sub.json()["approve_url"].startswith("https://www.sandbox.paypal.com/")

            activated = client.post("/webhooks/paypal", headers=PAYPAL_HEADERS, json={
                "id": "WH-ACT",
                "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
                "resource": {
                    "id": "I-MOCK",
                    "plan_id": "P-TEST",
                    "custom_id": str(ALICE),
                    "subscriber": {"email_address": "a@b.c"},
                },
            })
            assert activated.status_code == 200 and activated.json()["processed"] is True
            pro = client.get("/api/me/billing").json()
            assert pro["is_pro"] is True and pro["subscription_id"] == "I-MOCK"

            again = client.post("/webhooks/paypal", headers=PAYPAL_HEADERS, json={
                "id": "WH-ACT",
                "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
                "resource": {"id": "I-MOCK", "custom_id": str(ALICE)},
            })
            assert again.status_code == 200

            failed = client.post("/webhooks/paypal", headers=PAYPAL_HEADERS, json={
                "id": "WH-FAIL",
                "event_type": "BILLING.SUBSCRIPTION.PAYMENT.FAILED",
                "resource": {"id": "I-MOCK"},
            })
            assert failed.status_code == 200
            assert client.get("/api/me/billing").json()["is_pro"] is True

            cancel = client.post("/api/me/billing/cancel", headers=CSRF, json={})
            assert cancel.status_code == 200 and cancel.json()["cancelled"] is True
            assert client.get("/api/me/billing").json()["is_pro"] is False

            rp = client.post("/api/me/billing/rescue-pass", headers=CSRF, json={})
            assert rp.status_code == 200 and "approve_url" in rp.json()

            paypal.verify_webhook_online = AsyncMock(return_value=True)
            agent = client.post("/api/me/billing/agent", headers=CSRF, json={"text": "get me a Rescue Pass"})
            assert agent.status_code == 200
            body = agent.json()
            assert body["tool"] == "create_order" and body["status"] == "created"
            assert body["approve_url"].startswith("https://www.sandbox.paypal.com/")
            log = client.get("/api/me/billing").json()["agent_actions"]
            assert log and log[0]["tool"] == "create_order" and log[0]["product"] == "rescue"

            granted = client.post("/webhooks/paypal", headers=PAYPAL_HEADERS, json={
                "id": "WH-RP-AGENT",
                "event_type": "PAYMENT.CAPTURE.COMPLETED",
                "resource": {
                    "id": "ORDER-1",
                    "purchase_units": [{"custom_id": str(ALICE)}],
                },
            })
            assert granted.status_code == 200 and granted.json()["processed"] is True
            assert client.get("/api/me/billing").json()["rescue_passes_remaining"] >= 1

            paypal.verify_webhook_online = AsyncMock(return_value=False)
            denied = client.post("/webhooks/paypal", headers=PAYPAL_HEADERS, json={
                "id": "WH-BAD", "event_type": "BILLING.SUBSCRIPTION.ACTIVATED", "resource": {},
            })
            assert denied.status_code == 401

    @pytest.mark.asyncio
    async def test_webhook_rejected_when_webhook_id_missing(self, tmp_path):
        settings = Settings(
            telegram_token=BOT_TOKEN,
            db_path=str(tmp_path / "liqguard.db"),
            key_file=tmp_path / "master.key",
            public_url="https://liqguard.test",
            bot_username="liqguard_test_bot",
            paypal_client_id="sandbox-client",
            paypal_client_secret="sandbox-secret",
            paypal_webhook_id=None,
            paypal_plan_id="P-TEST",
            paypal_env="sandbox",
        )
        storage = EncryptedStorage(db_path=settings.db_path, encryption_key=EncryptedStorage.generate_key())
        ctx = await AppContext.create(
            settings, storage=storage, llm=MetaLLMClient(use_mock=True),
            wicks=WickService(sources=[]),
        )
        try:
            with _client(ctx) as client:
                denied = client.post("/webhooks/paypal", headers=PAYPAL_HEADERS, json={
                    "id": "WH-NO-ID", "event_type": "BILLING.SUBSCRIPTION.ACTIVATED", "resource": {},
                })
                assert denied.status_code == 503
        finally:
            await ctx.close()
