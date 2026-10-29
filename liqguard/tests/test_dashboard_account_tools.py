"""Dashboard: per-position quick actions, 🔔 通知設定, self-serve 🔑 綁定帳號 and quiet hours in the monitor."""

import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from liqguard.ai import MetaLLMClient
from liqguard.alerts import AlertManager, PositionMonitor
from liqguard.core.binding import BindingService
from liqguard.core.position_tools import TOOLS
from liqguard.exchanges.base import KeyCheck
from liqguard.i18n import EN, t
from liqguard.models import Position, Side, User, UserThreshold, Venue
from liqguard.web import create_app
from liqguard.web.auth import SESSION_COOKIE

from .conftest import ALICE, BOB

CSRF = {"X-LiqGuard-CSRF": "1"}
API_KEY = "Kq7" + "x" * 50 + "AB12"
API_SECRET = "Sz9" + "y" * 50 + "CD34"
WALLET = "0x" + "cd" * 20


class FakeAdapter:
    check = KeyCheck(True, "Read-only key accepted", ["READ"], True)

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def check_key(self):
        return FakeAdapter.check


@pytest.fixture
async def users(ctx):
    for uid in (ALICE, BOB):
        await ctx.storage.save_user(User(telegram_id=uid, threshold=ctx.settings.default_threshold()))


@pytest.fixture
def dashboard(ctx, users):
    FakeAdapter.check = KeyCheck(True, "Read-only key accepted", ["READ"], True)
    app = create_app(ctx)
    app.state.binding = BindingService(ctx.storage, binance_factory=FakeAdapter, hyperliquid_factory=FakeAdapter,
                                       default_threshold=ctx.settings.default_threshold)
    with TestClient(app, base_url="https://testserver") as client:
        yield client


def sign_in(client: TestClient, ctx, user_id: int) -> None:
    client.cookies.set(SESSION_COOKIE, ctx.sessions.issue(user_id, 0))


class TestQuickActions:
    @pytest.mark.parametrize("tool", sorted(TOOLS))
    def test_public_demo_tools(self, dashboard, tool):
        r = dashboard.get(f"/api/demo/position-tool?symbol=BTCUSDT&tool={tool}")
        assert r.status_code == 200
        body = r.json()
        assert body["title"].startswith(t(EN, f"tools.name.{tool}")) and "BTCUSDT" in body["title"]
        assert t(EN, "common.demo_data").strip() in body["title"] and body["lang"] == EN
        assert body["lines"] and all(isinstance(line, str) for line in body["lines"])

    def test_unknown_tool_or_symbol(self, dashboard):
        assert dashboard.get("/api/demo/position-tool?symbol=BTCUSDT&tool=execute").status_code == 422
        assert dashboard.get("/api/demo/position-tool?symbol=NOPEUSDT&tool=stop").status_code == 404

    async def test_same_report_as_telegram(self, ctx, dashboard):
        from liqguard.telegram.ui import TelegramUI

        await ctx.storage.set_demo_mode(ALICE, True)
        sign_in(dashboard, ctx, ALICE)
        sim = ctx.simulations.get_or_create(ALICE)
        await sim.start(source="test")
        await sim.wait()
        ui = TelegramUI(ctx)
        for tool, action in (("stop", "ps"), ("wick", "pw"), ("rescue", "pr")):
            web = dashboard.get(f"/api/me/position-tool?symbol=BTCUSDT&tool={tool}").json()
            screen = await ui.router.run(ALICE, action, ("sim", "BTCUSDT"))
            assert screen.text == web["title"] + "\n\n" + "\n".join(web["lines"]).strip()

    async def test_live_tools_only_for_own_positions(self, ctx, dashboard):
        await ctx.storage.set_demo_mode(ALICE, True)
        ctx.positions.demo_adapter(ALICE, "critical")
        sign_in(dashboard, ctx, ALICE)
        for tool in TOOLS:
            r = dashboard.get(f"/api/me/position-tool/live?symbol=BTCUSDT&venue=demo&tool={tool}")
            assert r.status_code == 200 and r.json()["symbol"] == "BTCUSDT"
        sign_in(dashboard, ctx, BOB)
        assert dashboard.get("/api/me/position-tool/live?symbol=BTCUSDT&venue=demo&tool=stop").status_code == 404

    def test_requires_sign_in(self, dashboard):
        assert dashboard.get("/api/me/position-tool/live?symbol=BTCUSDT&venue=demo&tool=stop").status_code == 401


class TestSettingsPanel:
    async def test_read_and_update(self, ctx, dashboard):
        sign_in(dashboard, ctx, ALICE)
        current = dashboard.get("/api/me/settings").json()
        assert current["quiet_label"] == t(EN, "settings.quiet_off") and current["presets"]["quiet"][0] == "off"
        r = dashboard.post("/api/me/settings", json={"key": "quiet", "value": "23-7"}, headers=CSRF)
        assert r.status_code == 200 and r.json()["quiet_label"] == "23:00–07:00"
        r = dashboard.post("/api/me/settings", json={"key": "interval", "value": "5"}, headers=CSRF)
        assert r.json()["interval"] == 5
        user = await ctx.storage.get_user(ALICE)
        assert (user.threshold.quiet_hours_start, user.threshold.quiet_hours_end) == (23, 7)
        assert user.threshold.escalation_interval_minutes == 5

    async def test_rejects_out_of_order_tiers(self, ctx, dashboard):
        sign_in(dashboard, ctx, ALICE)
        r = dashboard.post("/api/me/settings", json={"key": "warning", "value": "8"}, headers=CSRF)
        assert r.status_code == 400 and r.json()["detail"] == t(EN, "settings.invalid")

    def test_requires_csrf_and_session(self, ctx, dashboard):
        assert dashboard.post("/api/me/settings", json={"key": "max", "value": "3"}, headers=CSRF).status_code == 401
        sign_in(dashboard, ctx, ALICE)
        assert dashboard.post("/api/me/settings", json={"key": "max", "value": "3"}).status_code == 403

    async def test_same_rules_as_telegram(self, ctx, dashboard):
        from liqguard.telegram.ui import TelegramUI

        sign_in(dashboard, ctx, ALICE)
        dashboard.post("/api/me/settings", json={"key": "stops", "value": "off"}, headers=CSRF)
        screen = await TelegramUI(ctx).router.run(ALICE, "se")
        assert "Stop-loss checks: OFF" in screen.text


class TestBindingPanel:
    async def test_bind_binance_never_echoes_credentials(self, ctx, dashboard):
        sign_in(dashboard, ctx, ALICE)
        r = dashboard.post("/api/me/bindings/binance", json={"api_key": API_KEY, "api_secret": API_SECRET}, headers=CSRF)
        assert r.status_code == 200 and r.json()["ok"] and r.json()["hint"] == "…AB12"
        assert API_KEY not in r.text and API_SECRET not in r.text
        listing = dashboard.get("/api/me/bindings")
        assert API_KEY not in listing.text and listing.json()["bindings"][0]["api_key"] == "…AB12"
        assert listing.json()["security_note"] == t(EN, "binding.security_note")
        assert API_SECRET.encode() not in Path(ctx.settings.db_path).read_bytes()

    def test_trade_permission_rejected(self, ctx, dashboard):
        FakeAdapter.check = KeyCheck(False, "Rejected: key has FUTURES_TRADE permission", ["READ", "FUTURES_TRADE"], True)
        sign_in(dashboard, ctx, ALICE)
        r = dashboard.post("/api/me/bindings/binance", json={"api_key": API_KEY, "api_secret": API_SECRET}, headers=CSRF)
        assert r.status_code == 400 and r.json()["code"] == "rejected_permissions" and "FUTURES_TRADE" in r.json()["message"]
        assert dashboard.get("/api/me/bindings").json()["bindings"] == []

    def test_malformed_input_is_not_reflected(self, ctx, dashboard):
        sign_in(dashboard, ctx, ALICE)
        huge = "Z" * 600
        for body in ({"api_key": huge, "api_secret": API_SECRET}, {"api_key": 5, "api_secret": API_SECRET}, {"api_key": API_KEY}):
            r = dashboard.post("/api/me/bindings/binance", json=body, headers=CSRF)
            assert r.status_code == 400 and huge not in r.text and API_SECRET not in r.text
        r = dashboard.post("/api/me/bindings/binance", json={"api_key": "short", "api_secret": "tiny-secret"}, headers=CSRF)
        assert r.status_code == 400 and r.json()["code"] == "invalid_format" and "tiny-secret" not in r.text

    def test_seed_phrase_refused(self, ctx, dashboard):
        sign_in(dashboard, ctx, ALICE)
        phrase = " ".join(["legal"] * 12)
        r = dashboard.post("/api/me/bindings/hyperliquid", json={"address": phrase}, headers=CSRF)
        assert r.status_code == 400 and r.json()["code"] == "secret_material" and phrase not in r.text

    def test_hyperliquid_and_unbind_with_confirmation(self, ctx, dashboard):
        sign_in(dashboard, ctx, ALICE)
        assert dashboard.post("/api/me/bindings/hyperliquid", json={"address": "0x12"}, headers=CSRF).status_code == 400
        r = dashboard.post("/api/me/bindings/hyperliquid", json={"address": WALLET}, headers=CSRF)
        assert r.status_code == 200 and WALLET not in r.text
        body = {"venue": "hyperliquid", "network": "mainnet"}
        assert dashboard.post("/api/me/bindings/unbind", json=body, headers=CSRF).status_code == 400
        assert dashboard.post("/api/me/bindings/unbind", json={**body, "confirm": True}, headers=CSRF).status_code == 200
        assert dashboard.get("/api/me/bindings").json()["bindings"] == []
        assert dashboard.post("/api/me/bindings/unbind", json={**body, "confirm": True}, headers=CSRF).status_code == 404

    def test_cannot_unbind_another_user(self, ctx, dashboard):
        sign_in(dashboard, ctx, ALICE)
        dashboard.post("/api/me/bindings/hyperliquid", json={"address": WALLET}, headers=CSRF)
        sign_in(dashboard, ctx, BOB)
        r = dashboard.post("/api/me/bindings/unbind", json={"venue": "hyperliquid", "confirm": True}, headers=CSRF)
        assert r.status_code == 404
        sign_in(dashboard, ctx, ALICE)
        assert len(dashboard.get("/api/me/bindings").json()["bindings"]) == 1

    def test_requires_csrf_and_session(self, ctx, dashboard):
        body = {"address": WALLET}
        assert dashboard.post("/api/me/bindings/hyperliquid", json=body, headers=CSRF).status_code == 401
        sign_in(dashboard, ctx, ALICE)
        assert dashboard.post("/api/me/bindings/hyperliquid", json=body).status_code == 403

    def test_public_demo_cannot_bind(self):
        app = create_app()
        paths = {r.path for r in app.routes}
        assert not any(p.startswith("/api/me/bindings") or p == "/api/me/settings" for p in paths)


class TestLiveSnooze:
    def test_only_the_owner_can_snooze(self, ctx, dashboard):
        ack = ctx.escalations.open(ALICE, "binance", "BTCUSDT", "critical", "x", time.time(), 60, 5)
        sign_in(dashboard, ctx, BOB)
        assert dashboard.post("/api/me/live/snooze", json={"ack_id": ack.id}, headers=CSRF).status_code == 404
        assert ack.snoozed_until is None
        sign_in(dashboard, ctx, ALICE)
        r = dashboard.post("/api/me/live/snooze", json={"ack_id": ack.id}, headers=CSRF)
        assert r.status_code == 200 and r.json()["minutes"] == 30 and ack.snoozed_until > time.time() + 29 * 60


def position(distance_pct: float) -> Position:
    mark = 100.0
    return Position(venue=Venue.BINANCE, symbol="BTCUSDT", side=Side.LONG, size=1.0, entry_price=101.0, mark_price=mark,
                    liquidation_price=mark * (1 - distance_pct / 100), margin=10.0, leverage=10.0, unrealized_pnl=-1.0)


class TestQuietHours:
    TAIPEI_2AM = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)
    TAIPEI_2PM = datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc)

    def test_window_wraps_midnight(self):
        t = UserThreshold(quiet_hours_start=23, quiet_hours_end=7)
        assert t.in_quiet_hours(self.TAIPEI_2AM) and not t.in_quiet_hours(self.TAIPEI_2PM)
        assert not UserThreshold().in_quiet_hours(self.TAIPEI_2AM)

    @pytest.mark.parametrize("distance,clock,expect_sent", [
        (15.0, TAIPEI_2AM, False),
        (15.0, TAIPEI_2PM, True),
        (4.0, TAIPEI_2AM, True),
    ])
    async def test_only_critical_and_emergency_break_quiet_hours(self, distance, clock, expect_sent):
        user = User(telegram_id=ALICE, threshold=UserThreshold(quiet_hours_start=23, quiet_hours_end=7))

        async def users():
            return [user]

        async def positions(_user):
            return [position(distance)]

        notifier = AsyncMock()
        monitor = PositionMonitor(users, positions, notifier, AlertManager(), MetaLLMClient(use_mock=True), clock=lambda: clock)
        await monitor.check_all_users()
        assert bool(notifier.notify.await_count) is expect_sent
        if expect_sent:
            assert all(c.args[0] == ALICE for c in notifier.notify.await_args_list)

    async def test_held_warning_goes_out_after_quiet_hours(self):
        user = User(telegram_id=ALICE, threshold=UserThreshold(quiet_hours_start=23, quiet_hours_end=7))
        now = {"t": self.TAIPEI_2AM}

        async def users():
            return [user]

        async def positions(_user):
            return [position(15.0)]

        notifier = AsyncMock()
        monitor = PositionMonitor(users, positions, notifier, AlertManager(), MetaLLMClient(use_mock=True), clock=lambda: now["t"])
        await monitor.check_all_users()
        assert notifier.notify.await_count == 0
        now["t"] = self.TAIPEI_2PM
        await monitor.check_all_users()
        assert notifier.notify.await_count >= 1
